"""
evaluate_recommender_v3.py - Precision@K / Recall@K / Hit Rate@K, multiple K.

Builds directly on evaluate_recommender_v2.py's time-aware setup (train on
data strictly before each user's held-out purchase, so there's no leakage).
Adds standard ranking metrics at K = 5, 10, 20 instead of just Hit Rate@10.

With exactly one relevant item per user (the held-out purchase):
    Recall@K   = 1 if the item is in the top K, else 0   (same as Hit Rate@K)
    Precision@K = 1/K if hit, else 0                      (only 1 of K slots is "correct")
These are reported separately anyway, since that's the standard way to
present a recommender evaluation, and it makes the K-sensitivity visible:
Recall should rise as K grows; Precision should fall.
"""

from collections import defaultdict

from evaluate_recommender_v2 import (
    get_connection,
    get_test_cases,
    recommend_for_user_before,
    popularity_before,
    category_baseline_before,
)

K_VALUES = [5, 10, 20]
MAX_K = max(K_VALUES)


def evaluate(test_cases, get_recs_fn, k_values):
    """get_recs_fn(user_id, case) must return a ranked list of up to MAX_K ids."""
    hits_at_k = {k: 0 for k in k_values}
    total = len(test_cases)

    for user_id, case in test_cases.items():
        ranked = get_recs_fn(user_id, case)
        target = case["held_out_product"]
        for k in k_values:
            if target in ranked[:k]:
                hits_at_k[k] += 1

    metrics = {}
    for k in k_values:
        recall = hits_at_k[k] / total if total else 0.0
        precision = recall / k  # exactly one relevant item, so hit -> 1/k, miss -> 0
        metrics[k] = {"recall": recall, "precision": precision, "hits": hits_at_k[k]}
    return metrics


def print_table(name, metrics, k_values):
    print(f"\n{name}")
    print(f"  {'K':>4} | {'Recall@K':>10} | {'Precision@K':>12} | hits")
    print("  " + "-" * 42)
    for k in k_values:
        m = metrics[k]
        print(f"  {k:>4} | {m['recall']:>10.3f} | {m['precision']:>12.3f} | {m['hits']}")


def main():
    print("=" * 60)
    print("EXTENDED EVALUATION: Precision@K / Recall@K, K = " + ", ".join(map(str, K_VALUES)))
    print("=" * 60)

    test_cases = get_test_cases()
    print(f"\nTest cases (users with 2+ purchases): {len(test_cases)}")
    if not test_cases:
        print("Not enough purchase history for evaluation.")
        return

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    def hybrid_recs(user_id, case):
        recs = recommend_for_user_before(cursor, user_id, case["cutoff"], n=MAX_K)
        return recs if recs else popularity_before(cursor, case["cutoff"], n=MAX_K)

    def popularity_recs(user_id, case):
        return popularity_before(cursor, case["cutoff"], n=MAX_K)

    def category_recs(user_id, case):
        recs = category_baseline_before(cursor, user_id, case["cutoff"], n=MAX_K)
        return recs if recs else popularity_before(cursor, case["cutoff"], n=MAX_K)

    print("\nEvaluating HYBRID...")
    hybrid_metrics = evaluate(test_cases, hybrid_recs, K_VALUES)

    print("Evaluating CATEGORY baseline...")
    category_metrics = evaluate(test_cases, category_recs, K_VALUES)

    print("Evaluating POPULARITY baseline...")
    popularity_metrics = evaluate(test_cases, popularity_recs, K_VALUES)

    cursor.close()
    conn.close()

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print_table("HYBRID", hybrid_metrics, K_VALUES)
    print_table("CATEGORY baseline", category_metrics, K_VALUES)
    print_table("POPULARITY baseline", popularity_metrics, K_VALUES)

    print(
        "\nReading these: Recall@K is the share of users whose next purchase\n"
        "showed up somewhere in the top K. Precision@K is lower by design here\n"
        "because each user only has ONE correct answer to find, so a hit at K=20\n"
        "still only fills 1 of 20 slots correctly (0.05) even though Recall@20 is high.\n"
        "Recall is the more meaningful number for this dataset; Precision is reported\n"
        "because it's the standard pairing, and because a real system with multiple\n"
        "'correct' next purchases per user would make Precision more informative."
    )


if __name__ == "__main__":
    main()
