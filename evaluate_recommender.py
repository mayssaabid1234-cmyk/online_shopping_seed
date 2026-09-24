import mysql.connector
from collections import defaultdict
from recommender import recommend_for_user, popular_products, DB_CONFIG


K = 10


def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


def get_test_purchases():
    """
    For each user, hold out their LAST purchased product as the test item.

    Purchases come from orders + order_items (the authoritative purchase
    record) rather than user_events, because a 'purchase' event in
    user_events represents one whole order (which can contain several
    products) and has no single product_id attached to it.

    Everything before that last item belongs to the user's
    historical/training data.
    """

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT
            o.user_id,
            oi.product_id,
            o.placed_at,
            oi.order_item_id
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.order_id
        WHERE o.user_id IS NOT NULL
        ORDER BY o.user_id, o.placed_at, oi.order_item_id
    """

    cursor.execute(query)
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    purchases_by_user = defaultdict(list)

    for row in rows:
        purchases_by_user[row["user_id"]].append(row)

    test_purchases = {}

    for user_id, purchases in purchases_by_user.items():

        # Need at least 2 purchased items:
        # earlier ones count as history, the last one is held out for testing.
        if len(purchases) >= 2:
            test_purchases[user_id] = purchases[-1]["product_id"]

    return test_purchases


def get_popular_recommendations(n=K):
    """
    Popularity baseline.
    """

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT
            product_id,
            COUNT(*) AS interaction_count
        FROM user_events
        WHERE product_id IS NOT NULL
        GROUP BY product_id
        ORDER BY interaction_count DESC
        LIMIT %s
    """

    cursor.execute(query, (n,))
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    return [row["product_id"] for row in rows]


def evaluate_hybrid(test_purchases):
    """
    Evaluate the current hybrid recommender.
    """

    hits = 0
    total = 0

    results = []

    for user_id, actual_product in test_purchases.items():

        recommendations = recommend_for_user(user_id, n=K)

        recommended_ids = [
            item["product_id"]
            for item in recommendations
        ]

        hit = actual_product in recommended_ids

        if hit:
            hits += 1

        total += 1

        results.append({
            "user_id": user_id,
            "actual_product": actual_product,
            "recommended_products": recommended_ids,
            "hit": hit
        })

    hit_rate = hits / total if total > 0 else 0

    return hit_rate, results


def evaluate_popularity(test_purchases):
    """
    Evaluate simple popularity baseline.
    """

    popular_ids = get_popular_recommendations(K)

    hits = 0
    total = len(test_purchases)

    for actual_product in test_purchases.values():

        if actual_product in popular_ids:
            hits += 1

    hit_rate = hits / total if total > 0 else 0

    return hit_rate, popular_ids


def main():

    print("=" * 60)
    print("RECOMMENDER EVALUATION")
    print("=" * 60)

    test_purchases = get_test_purchases()

    print(f"\nUsers evaluated: {len(test_purchases)}")

    if not test_purchases:
        print("Not enough purchase history for evaluation.")
        return

    print("\nEvaluating HYBRID recommender...")

    hybrid_hit_rate, hybrid_results = evaluate_hybrid(test_purchases)

    print("\nEvaluating POPULARITY baseline...")

    popularity_hit_rate, popular_ids = evaluate_popularity(test_purchases)

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)

    print(f"\nHybrid Hit Rate@{K}:      {hybrid_hit_rate:.3f}")
    print(f"Popularity Hit Rate@{K}: {popularity_hit_rate:.3f}")

    print("\nPopularity products:")
    print(popular_ids)

    print("\n" + "=" * 60)
    print("EXAMPLE TEST RESULTS")
    print("=" * 60)

    for result in hybrid_results[:10]:

        print(
            f"\nUser {result['user_id']}"
            f"\nActual product: {result['actual_product']}"
            f"\nRecommended: {result['recommended_products']}"
            f"\nHit: {result['hit']}"
        )


if __name__ == "__main__":
    main()
