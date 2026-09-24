import os
import math
from collections import defaultdict

import mysql.connector
from dotenv import load_dotenv

load_dotenv()


# ============================================================
# DATABASE
# ============================================================

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME", "online_shopping"),
}


def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


# ============================================================
# WEIGHTS — matched to the real event_type ENUM values
# ============================================================

EVENT_WEIGHTS = {
    "page_view": 0.5,
    "search": 1.0,
    "product_view": 1.0,
    "add_to_cart": 3.0,
    "remove_from_cart": -2.0,
    "wishlist_add": 4.0,
    "wishlist_remove": -1.0,
    "checkout_start": 2.0,
    "purchase": 6.0,
    "review": 2.5,
    # login/logout rows have product_id = NULL and never reach this dict
}


# ============================================================
# SHARED HELPERS
# ============================================================

def get_product_categories(cursor, product_ids):
    """product_id -> set(category_id), no fan-out, one row per link."""
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


def get_product_stats(cursor, product_ids):
    """product_id -> {name, brand, interaction_count, avg_rating}.
    Events and reviews are aggregated in separate subqueries first,
    so there's no event-x-review join fan-out at all."""
    if not product_ids:
        return {}
    placeholders = ",".join(["%s"] * len(product_ids))
    cursor.execute(
        f"""
        SELECT
            p.product_id,
            p.name,
            p.brand,
            COALESCE(e.interaction_count, 0) AS interaction_count,
            r.avg_rating
        FROM products p
        LEFT JOIN (
            SELECT product_id, COUNT(*) AS interaction_count
            FROM user_events
            WHERE product_id IN ({placeholders})
            GROUP BY product_id
        ) e ON p.product_id = e.product_id
        LEFT JOIN (
            SELECT product_id, AVG(rating) AS avg_rating
            FROM reviews
            WHERE product_id IN ({placeholders})
            GROUP BY product_id
        ) r ON p.product_id = r.product_id
        WHERE p.product_id IN ({placeholders})
        """,
        list(product_ids) + list(product_ids) + list(product_ids),
    )
    return {row["product_id"]: row for row in cursor.fetchall()}


# ============================================================
# PRODUCT -> PRODUCT RECOMMENDATIONS
# ============================================================

def recommend_products(product_id, n=10, cursor=None):
    """
    Recommend products related to a given product.
    Pass an open `cursor` to reuse a connection (avoids reopening
    MySQL connections in tight loops, e.g. from recommend_for_user).
    """
    owns_connection = cursor is None
    conn = None
    if owns_connection:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

    try:
        # 1. Users who interacted with the target product
        cursor.execute(
            "SELECT DISTINCT user_id FROM user_events "
            "WHERE product_id = %s AND user_id IS NOT NULL",
            (product_id,),
        )
        users = [row["user_id"] for row in cursor.fetchall()]
        if not users:
            return []

        # 2. Other products those users interacted with
        placeholders = ",".join(["%s"] * len(users))
        cursor.execute(
            f"""
            SELECT product_id, event_type
            FROM user_events
            WHERE user_id IN ({placeholders})
              AND product_id IS NOT NULL
              AND product_id != %s
            """,
            users + [product_id],
        )
        collaborative_scores = defaultdict(float)
        for row in cursor.fetchall():
            weight = EVENT_WEIGHTS.get(row["event_type"], 0.0)
            collaborative_scores[row["product_id"]] += weight

        # 3. Purchase co-occurrence ("bought together")
        cursor.execute(
            """
            SELECT DISTINCT oi2.product_id
            FROM order_items oi1
            JOIN order_items oi2 ON oi1.order_id = oi2.order_id
            WHERE oi1.product_id = %s AND oi2.product_id != %s
            """,
            (product_id, product_id),
        )
        bought_together = {row["product_id"] for row in cursor.fetchall()}

        # 4. Target product info
        cursor.execute(
            "SELECT product_id, brand FROM products WHERE product_id = %s",
            (product_id,),
        )
        target = cursor.fetchone()
        if not target:
            return []
        target_brand = target["brand"]
        target_categories = get_product_categories(cursor, [product_id]).get(product_id, set())

        # 5. Candidate pool: collaborative + bought-together + same brand/category
        candidates = set(collaborative_scores.keys()) | bought_together
        cursor.execute(
            "SELECT DISTINCT p.product_id FROM products p "
            "LEFT JOIN product_categories pc ON p.product_id = pc.product_id "
            "WHERE p.product_id != %s AND (p.brand = %s OR pc.category_id IN "
            f"({','.join(['%s'] * len(target_categories)) if target_categories else 'NULL'}))",
            [product_id, target_brand] + list(target_categories),
        )
        for row in cursor.fetchall():
            candidates.add(row["product_id"])
        if not candidates:
            return []

        # 6. Stats + categories for candidates, WITHOUT fan-out
        stats = get_product_stats(cursor, candidates)
        cats_by_product = get_product_categories(cursor, candidates)

        # 7. Score
        recommendations = []
        for pid, row in stats.items():
            score = 0.0
            score += collaborative_scores.get(pid, 0) * 0.45
            if pid in bought_together:
                score += 8.0
            if target_categories & cats_by_product.get(pid, set()):
                score += 5.0
            if target_brand and row["brand"] == target_brand:
                score += 4.0
            score += math.log1p(row["interaction_count"] or 0) * 0.8
            rating = row["avg_rating"]
            if rating is not None:
                score += float(rating) * 0.7
            recommendations.append({
                "product_id": pid,
                "name": row["name"],
                "brand": row["brand"],
                "score": round(score, 3),
                "rating": round(float(rating), 2) if rating is not None else None,
            })

        recommendations.sort(key=lambda x: x["score"], reverse=True)
        return recommendations[:n]

    finally:
        if owns_connection:
            cursor.close()
            conn.close()


# ============================================================
# USER PERSONALIZATION
# ============================================================

def recommend_for_user(user_id, n=10):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1. User history
        cursor.execute(
            "SELECT product_id, event_type FROM user_events "
            "WHERE user_id = %s AND product_id IS NOT NULL",
            (user_id,),
        )
        history = cursor.fetchall()
        if not history:
            return popular_products(n)

        interacted_products = {row["product_id"] for row in history}

        # 2. Purchased products (excluded from results)
        cursor.execute(
            "SELECT DISTINCT oi.product_id FROM orders o "
            "JOIN order_items oi ON o.order_id = oi.order_id WHERE o.user_id = %s",
            (user_id,),
        )
        purchased_products = {row["product_id"] for row in cursor.fetchall()}

        # 3. Category / brand preferences from history
        cats_by_product = get_product_categories(cursor, interacted_products)
        product_brand = {}
        if interacted_products:
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

        # 4. Collaborative candidates, reusing one cursor (no per-call reconnects)
        collaborative_scores = defaultdict(float)
        for pid in interacted_products:
            for item in recommend_products(pid, n=20, cursor=cursor):
                candidate = item["product_id"]
                if candidate not in purchased_products:
                    collaborative_scores[candidate] += item["score"]

        # 5. Full candidate pool: collaborative ∪ not-yet-purchased products
        cursor.execute(
            "SELECT product_id FROM products WHERE product_id NOT IN ("
            "SELECT DISTINCT oi.product_id FROM orders o "
            "JOIN order_items oi ON o.order_id = oi.order_id WHERE o.user_id = %s)",
            (user_id,),
        )
        candidates = {row["product_id"] for row in cursor.fetchall()}

        stats = get_product_stats(cursor, candidates)
        cand_cats = get_product_categories(cursor, candidates)

        # 6. Score
        recommendations = []
        for pid, row in stats.items():
            score = 0.0
            score += collaborative_scores.get(pid, 0) * 0.50
            for cat in cand_cats.get(pid, set()):
                score += category_scores.get(cat, 0) * 0.20
            score += brand_scores.get(row["brand"], 0) * 0.15
            score += math.log1p(row["interaction_count"] or 0) * 0.5
            rating = row["avg_rating"]
            if rating is not None:
                score += float(rating) * 0.5
            recommendations.append({
                "product_id": pid,
                "name": row["name"],
                "brand": row["brand"],
                "score": round(score, 3),
                "rating": round(float(rating), 2) if rating is not None else None,
            })

        recommendations.sort(key=lambda x: x["score"], reverse=True)
        return recommendations[:n]

    finally:
        cursor.close()
        conn.close()


# ============================================================
# POPULARITY FALLBACK (new users / cold start)
# ============================================================

def popular_products(n=10):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT
                p.product_id, p.name, p.brand,
                COALESCE(e.interactions, 0) AS interactions,
                r.avg_rating
            FROM products p
            LEFT JOIN (
                SELECT product_id, COUNT(*) AS interactions
                FROM user_events
                WHERE product_id IS NOT NULL
                GROUP BY product_id
            ) e ON p.product_id = e.product_id
            LEFT JOIN (
                SELECT product_id, AVG(rating) AS avg_rating
                FROM reviews
                GROUP BY product_id
            ) r ON p.product_id = r.product_id
            ORDER BY interactions DESC
            LIMIT %s
            """,
            (n,),
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":
    print("\nPRODUCT RECOMMENDATIONS (product_id=1)")
    print("=" * 50)
    for item in recommend_products(product_id=1, n=10):
        print(item)

    print("\nUSER RECOMMENDATIONS (user_id=1)")
    print("=" * 50)
    for item in recommend_for_user(user_id=1, n=10):
        print(item)
