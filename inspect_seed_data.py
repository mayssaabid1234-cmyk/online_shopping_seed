"""
inspect_seed_data.py — sanity-check the data produced by seed_data.py

Usage:
    python inspect_seed_data.py                # table counts + distributions
    python inspect_seed_data.py --scenarios     # also search for the 5 guaranteed journeys
"""

import argparse
import os
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME", "online_shopping"),
}

parser = argparse.ArgumentParser()
parser.add_argument("--scenarios", action="store_true")
args = parser.parse_args()

conn = mysql.connector.connect(**DB_CONFIG)
cur = conn.cursor()

TABLES = [
    "users", "addresses", "categories", "products", "product_categories",
    "carts", "cart_items", "orders", "order_items", "payments",
    "reviews", "wishlists", "user_events",
]


def q(sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()


def section(title):
    print(f"\n=== {title} ===")


def counts():
    section("TABLE COUNTS")
    for t in TABLES:
        n = q(f"SELECT COUNT(*) FROM {t}")[0][0]
        print(f"  {t:<22} {n}")


def distributions():
    section("ORDER STATUS DISTRIBUTION")
    for status, n in q("SELECT status, COUNT(*) FROM orders GROUP BY status ORDER BY n DESC" if False else
                        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC"):
        print(f"  {status:<12} {n}")

    section("PAYMENT STATUS DISTRIBUTION")
    for status, n in q("SELECT status, COUNT(*) AS n FROM payments GROUP BY status ORDER BY n DESC"):
        print(f"  {status:<12} {n}")

    section("EVENT TYPE DISTRIBUTION")
    for et, n in q("SELECT event_type, COUNT(*) AS n FROM user_events GROUP BY event_type ORDER BY n DESC"):
        print(f"  {et:<18} {n}")

    section("REVIEW RATING DISTRIBUTION")
    for r, n in q("SELECT rating, COUNT(*) AS n FROM reviews GROUP BY rating ORDER BY rating DESC"):
        print(f"  {r} stars: {n}")

    section("CART STATUS DISTRIBUTION")
    for s, n in q("SELECT status, COUNT(*) AS n FROM carts GROUP BY status ORDER BY n DESC"):
        print(f"  {s:<12} {n}")

    section("GUEST vs REGISTERED EVENTS")
    for label, n in q(
        "SELECT CASE WHEN user_id IS NULL THEN 'guest' ELSE 'registered' END AS label, COUNT(*) AS n "
        "FROM user_events GROUP BY label"
    ):
        print(f"  {label:<12} {n}")


def scenario(name, sql, params=None):
    rows = q(sql, params)
    if rows:
        print(f"  [FOUND]    {name}  (e.g. {rows[0]})")
    else:
        print(f"  [NOT FOUND] {name}")


def scenarios():
    section("SCENARIO CHECKS")
    # A: guest -> registration -> purchase (session_id shared between a guest event and a real order)
    scenario(
        "A: guest -> registration -> purchase",
        """
        SELECT DISTINCT ue.session_id, o.order_id
        FROM user_events ue
        JOIN carts c ON c.session_id = ue.session_id
        JOIN orders o ON o.user_id = c.user_id
        WHERE ue.user_id IS NULL AND c.user_id IS NOT NULL
        LIMIT 1
        """,
    )
    # B: failed payment -> retry -> completed (2+ payments on one order, ending paid)
    scenario(
        "B: failed payment -> retry -> completed",
        """
        SELECT order_id, COUNT(*) AS n_payments
        FROM payments
        GROUP BY order_id
        HAVING COUNT(*) > 1 AND SUM(status='failed') > 0 AND SUM(status='paid') > 0
        LIMIT 1
        """,
    )
    # C: cancelled -> paid -> refunded
    scenario(
        "C: cancelled after payment -> refunded",
        """
        SELECT o.order_id FROM orders o
        JOIN payments p ON p.order_id = o.order_id
        WHERE o.status = 'refunded' AND p.status = 'refunded'
        LIMIT 1
        """,
    )
    # D: delivered -> review
    scenario(
        "D: delivered order with a verified review",
        """
        SELECT o.order_id, r.review_id FROM orders o
        JOIN order_items oi ON oi.order_id = o.order_id
        JOIN reviews r ON r.product_id = oi.product_id AND r.user_id = o.user_id
        WHERE o.status = 'delivered' AND r.is_verified_purchase = 1
        LIMIT 1
        """,
    )
    # E: abandoned cart (no order)
    scenario(
        "E: abandoned cart with no matching order",
        """
        SELECT cart_id FROM carts WHERE status = 'abandoned' LIMIT 1
        """,
    )


def main():
    counts()
    distributions()
    if args.scenarios:
        scenarios()


if __name__ == "__main__":
    main()
    cur.close()
    conn.close()
