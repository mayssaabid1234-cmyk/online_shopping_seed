"""
evaluate_recommender_v2.py - time-aware evaluation of the recommender.

Fixes a real leakage bug in evaluate_recommender.py: that version let
recommend_for_user() exclude the held-out test purchase from candidates
(since it queries the FULL orders table, present-day, test item included),
guaranteeing the "correct" answer could never be recommended.

Here, for each user we pick a cutoff timestamp - the moment of their
last purchase - and rebuild everything (history, exclusions, popularity)
using ONLY data strictly before that cutoff. The held-out purchase itself
is never visible to the model. recommender.py is left untouched; the
scoring logic below is a self-contained, time-restricted reimplementation.
"""

import math
from collections import defaultdict

import mysql.connector
from recommender import DB_CONFIG, EVENT_WEIGHTS

K = 10


def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


# ============================================================
# STEP 1 - build test cases: (user_id, cutoff_time, held_out_product)
# ============================================================

def get_test_cases():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        """
        SELECT o.user_id, oi.product_id, o.placed_at, oi.order_item_id
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.order_id
        WHERE o.user_id IS NOT NULL AND o.placed_at IS NOT NULL
        ORDER BY o.user_id, o.placed_at, oi.order_item_id
        """
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    by_user = defaultdict(list)
    for row in rows:
        by_user[row["user_id"]].append(row)

    test_cases = {}
    for user_id, purchases in by_user.items():
        if len(purchases) >= 2:
            held_out = purchases[-1]
            test_cases[user_id] = {
                "held_out_product": held_out["product_id"],
                "cutoff": held_out["placed_at"],
            }
    return test_cases


# ============================================================
# STEP 2 - time-restricted building blocks
# ============================================================

def get_training_events(cursor, user_id, cutoff):
    cursor.execute(
        "SELECT product_id, event_type FROM user_events "
        "WHERE user_id = %s AND product_id IS NOT NULL AND created_at < %s",
        (user_id, cutoff),
    )
    return cursor.fetchall()


def get_training_purchases(cursor, user_id, cutoff):
    """Products this user had already bought strictly BEFORE cutoff -
    these are excluded from candidates. The held-out product is NOT in
    this set, since its own order is not before the cutoff."""
    cursor.execute(
        "SELECT DISTINCT oi.product_id FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.order_id "
        "WHERE o.user_id = %s AND o.placed_at < %s",
        (user_id, cutoff),
    )
    return {row["product_id"] for row in cursor.fetchall()}


def get_product_categories(cursor, product_ids):
    if not product_ids:
        return {}
    placeholders = ",".join(["%s"] * len(product_ids))
    cursor.execute(
        f"SELECT product_id, category_id FROM product_categories "
        f"WHERE product_id IN ({placeholders})",
        list(product_ids),
    )
    result = defaultdict(set)
    for row in cursor.fetchall():
        result[row["product_id"]].add(row["category_id"])
    return result


def get_product_stats_before(cursor, product_ids, cutoff):
    """interaction_count / avg_rating computed only from data before cutoff."""
    if not product_ids:
        return {}
    placeholders = ",".join(["%s"] * len(product_ids))
    cursor.execute(
        f"""
        SELECT p.product_id, p.name, p.brand,
               COALESCE(e.interaction_count, 0) AS interaction_count,
               r.avg_rating
        FROM products p
        LEFT JOIN (
            SELECT product_id, COUNT(*) AS interaction_count
            FROM user_events
            WHERE product_id IN ({placeholders}) AND created_at < %s
            GROUP BY product_id
        ) e ON p.product_id = e.product_id
        LEFT JOIN (
            SELECT product_id, AVG(rating) AS avg_rating
            FROM reviews
            WHERE product_id IN ({placeholders}) AND created_at < %s
            GROUP BY product_id
        ) r ON p.product_id = r.product_id
        WHERE p.product_id IN ({placeholders})
        """,
        list(product_ids) + [cutoff] + list(product_ids) + [cutoff] + list(product_ids),
    )
    return {row["product_id"]: row for row in cursor.fetchall()}


def collaborative_candidates_before(cursor, product_ids, exclude_products, cutoff):
    """For each product the user interacted with (pre-cutoff), find other
    users' pre-cutoff interactions with it, and score co-interacted products."""
    scores = defaultdict(float)
    if not product_ids:
        return scores

    placeholders = ",".join(["%s"] * len(product_ids))
    cursor.execute(
        f"SELECT DISTINCT user_id, product_id FROM user_events "
        f"WHERE product_id IN ({placeholders}) AND user_id IS NOT NULL AND created_at < %s",
        list(product_ids) + [cutoff],
    )
    users_by_product = defaultdict(set)
    for row in cursor.fetchall():
        users_by_product[row["product_id"]].add(row["user_id"])

    other_users = set()
    for pid in product_ids:
        other_users |= users_by_product.get(pid, set())
    if not other_users:
        return scores

    up = ",".join(["%s"] * len(other_users))
    cursor.execute(
        f"SELECT product_id, event_type FROM user_events "
        f"WHERE user_id IN ({up}) AND product_id IS NOT NULL AND created_at < %s",
        list(other_users) + [cutoff],
    )
    for row in cursor.fetchall():
        pid = row["product_id"]
        if pid in exclude_products:
            continue
        scores[pid] += EVENT_WEIGHTS.get(row["event_type"], 0.0)
    return scores


# ============================================================
# STEP 3 - time-aware personalized recommender
# ============================================================

def recommend_for_user_before(cursor, user_id, cutoff, n=K):
    history = get_training_events(cursor, user_id, cutoff)
    if not history:
        return []  # cold start -> caller falls back to popularity_before()

    interacted_products = {row["product_id"] for row in history}
    already_purchased = get_training_purchases(cursor, user_id, cutoff)

    cats_by_product = get_product_categories(cursor, interacted_products)
    placeholders = ",".join(["%s"] * len(interacted_products))
    cursor.execute(
        f"SELECT product_id, brand FROM products WHERE product_id IN ({placeholders})",
        list(interacted_products),
    )
    product_brand = {row["product_id"]: row["brand"] for row in cursor.fetchall()}

    category_scores = defaultdict(float)
    brand_scores = defaultdict(float)
    for event in history:
        pid = event["product_id"]
        weight = EVENT_WEIGHTS.get(event["event_type"], 0.0)
        for cat in cats_by_product.get(pid, set()):
            category_scores[cat] += weight
        brand = product_brand.get(pid)
        if brand:
            brand_scores[brand] += weight

    collaborative_scores = collaborative_candidates_before(
        cursor, interacted_products, already_purchased, cutoff
    )

    # candidate pool: anything not already purchased pre-cutoff
    cursor.execute(
        "SELECT product_id FROM products WHERE product_id NOT IN ("
        + (",".join(str(p) for p in already_purchased) if already_purchased else "0")
        + ")"
    )
    candidates = {row["product_id"] for row in cursor.fetchall()}

    stats = get_product_stats_before(cursor, candidates, cutoff)
    cand_cats = get_product_categories(cursor, candidates)

    scored = []
    for pid, row in stats.items():
        score = 0.0
        score += collaborative_scores.get(pid, 0) * 0.50
        for cat in cand_cats.get(pid, set()):
            score += category_scores.get(cat, 0) * 0.20
        score += brand_scores.get(row["brand"], 0) * 0.15
        score += math.log1p(row["interaction_count"] or 0) * 0.5
        if row["avg_rating"] is not None:
            score += float(row["avg_rating"]) * 0.5
        scored.append((pid, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [pid for pid, _ in scored[:n]]


# ============================================================
# STEP 4 - time-aware baselines
# ============================================================

def popularity_before(cursor, cutoff, n=K):
    cursor.execute(
        "SELECT product_id, COUNT(*) AS c FROM user_events "
        "WHERE product_id IS NOT NULL AND created_at < %s "
        "GROUP BY product_id ORDER BY c DESC LIMIT %s",
        (cutoff, n),
    )
    return [row["product_id"] for row in cursor.fetchall()]


def category_baseline_before(cursor, user_id, cutoff, n=K):
    """Recommend the most popular products (pre-cutoff) within the user's
    most-interacted category - a simpler baseline than the full hybrid."""
    history = get_training_events(cursor, user_id, cutoff)
    if not history:
        return []
    products = {row["product_id"] for row in history}
    cats = get_product_categories(cursor, products)
    cat_counts = defaultdict(int)
    for pid in products:
        for c in cats.get(pid, set()):
            cat_counts[c] += 1
    if not cat_counts:
        return []
    top_category = max(cat_counts, key=cat_counts.get)

    cursor.execute(
        """
        SELECT pc.product_id, COUNT(ue.event_id) AS c
        FROM product_categories pc
        LEFT JOIN user_events ue
            ON ue.product_id = pc.product_id AND ue.created_at < %s
        WHERE pc.category_id = %s
        GROUP BY pc.product_id
        ORDER BY c DESC
        LIMIT %s
        """,
        (cutoff, top_category, n),
    )
    return [row["product_id"] for row in cursor.fetchall()]


# ============================================================
# STEP 5 - metrics + main
# ============================================================

def hit_rate(test_cases, get_recs_fn):
    hits = 0
    for user_id, case in test_cases.items():
        recs = get_recs_fn(user_id, case)
        if case["held_out_product"] in recs:
            hits += 1
    return hits / len(test_cases) if test_cases else 0.0


def main():
    print("=" * 60)
    print("TIME-AWARE RECOMMENDER EVALUATION")
    print("=" * 60)

    test_cases = get_test_cases()
    print(f"\nTest cases (users with 2+ purchases): {len(test_cases)}")
    if not test_cases:
        print("Not enough purchase history for evaluation.")
        return

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    def hybrid_recs(user_id, case):
        recs = recommend_for_user_before(cursor, user_id, case["cutoff"], n=K)
        return recs if recs else popularity_before(cursor, case["cutoff"], n=K)

    def popularity_recs(user_id, case):
        return popularity_before(cursor, case["cutoff"], n=K)

    def category_recs(user_id, case):
        recs = category_baseline_before(cursor, user_id, case["cutoff"], n=K)
        return recs if recs else popularity_before(cursor, case["cutoff"], n=K)

    print("\nEvaluating HYBRID (time-aware)...")
    hybrid_rate = hit_rate(test_cases, hybrid_recs)

    print("Evaluating POPULARITY baseline (time-aware)...")
    pop_rate = hit_rate(test_cases, popularity_recs)

    print("Evaluating CATEGORY baseline (time-aware)...")
    cat_rate = hit_rate(test_cases, category_recs)

    cursor.close()
    conn.close()

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"Hybrid Hit Rate@{K}:     {hybrid_rate:.3f}")
    print(f"Category Hit Rate@{K}:   {cat_rate:.3f}")
    print(f"Popularity Hit Rate@{K}: {pop_rate:.3f}")
    print(
        "\n(With one held-out item per user, Precision@K = Recall@K = Hit Rate@K here,\n"
        "since there is exactly one relevant item to find.)"
    )


if __name__ == "__main__":
    main()
