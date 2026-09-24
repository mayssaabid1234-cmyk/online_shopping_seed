"""
seed_data.py — Realistic fake data generator for the `online_shopping` MySQL schema.

Usage:
    python seed_data.py --small     # 5 users, 10 products, 20 sessions (deterministic, for debugging)
    python seed_data.py             # full scale: 100 users, 80-150 products, 300-500 orders, etc.

Requires:
    pip install faker mysql-connector-python python-dotenv

Reads DB credentials from a .env file (see .env.example).
"""

import argparse
import random
import re
import json
from datetime import datetime, timedelta

import mysql.connector
from faker import Faker

# ---------------------------------------------------------------------------
# CONNECTION SETTINGS — loaded from .env, not hardcoded
# ---------------------------------------------------------------------------
import os
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME", "online_shopping"),
}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument("--small", action="store_true", help="small deterministic debug run")
args = parser.parse_args()

SEED = 42
random.seed(SEED)
fake = Faker()
Faker.seed(SEED)

if args.small:
    N_USERS = 5
    N_PRODUCTS = 10
    N_SESSIONS = 20
else:
    N_USERS = 100
    N_PRODUCTS = random.randint(80, 150)
    N_SESSIONS = random.randint(400, 600)

CATEGORY_NAMES = ["Electronics", "Fashion", "Beauty", "Sports", "Home", "Books", "Accessories"]
PROVIDERS = ["stripe", "paypal", "bank_transfer", "cash_on_delivery"]
EVENT_SOURCES = ["homepage", "search", "recommendation", "category_page", "email"]

NOW = datetime.now()
START = NOW - timedelta(days=300)  # ~10 months of history


def slugify(text, suffix=""):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return f"{s}-{suffix}" if suffix else s


def rand_dt(start=START, end=NOW):
    delta = end - start
    seconds = random.randint(0, int(delta.total_seconds()))
    return start + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# DB HELPERS
# ---------------------------------------------------------------------------
conn = mysql.connector.connect(**DB_CONFIG)
conn.autocommit = True
cur = conn.cursor()


def insert(table, row):
    cols = ", ".join(row.keys())
    placeholders = ", ".join(["%s"] * len(row))
    sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
    cur.execute(sql, list(row.values()))
    return cur.lastrowid


# ---------------------------------------------------------------------------
# SEED FUNCTIONS
# ---------------------------------------------------------------------------
def seed_categories():
    ids = []
    for name in CATEGORY_NAMES:
        cid = insert("categories", {
            "parent_category_id": None,
            "name": name,
            "slug": slugify(name),
            "description": fake.sentence(),
            "is_active": 1,
            "created_at": START,
        })
        ids.append(cid)
        # one or two subcategories per top-level category
        for _ in range(random.randint(1, 2)):
            sub_name = f"{name} {fake.word().capitalize()}"
            insert("categories", {
                "parent_category_id": cid,
                "name": sub_name,
                "slug": slugify(sub_name, suffix=str(cid)),
                "description": fake.sentence(),
                "is_active": 1,
                "created_at": START,
            })
    return ids  # top-level category ids only, used for product assignment


def seed_users(n):
    user_ids = []
    # a couple of admins
    n_admins = max(1, n // 25)
    for i in range(n):
        role = "admin" if i < n_admins else "customer"
        created = rand_dt()
        uid = insert("users", {
            "email": fake.unique.email(),
            "password_hash": fake.sha256(),
            "first_name": fake.first_name(),
            "last_name": fake.last_name(),
            "role": role,
            "is_active": 1,
            "created_at": created,
            "updated_at": created,
        })
        # spending_tendency: informal in-memory trait to bias order size/price tier
        spending = random.choice(["low", "medium", "medium", "high"])
        user_ids.append({"id": uid, "role": role, "spending": spending, "created_at": created})

        # 1-2 addresses per customer
        if role == "customer":
            n_addr = random.choice([1, 1, 2])
            for j in range(n_addr):
                insert("addresses", {
                    "user_id": uid,
                    "address_type": random.choice(["shipping", "billing", "both"]),
                    "first_name": fake.first_name(),
                    "last_name": fake.last_name(),
                    "address_line1": fake.street_address(),
                    "address_line2": None,
                    "city": fake.city(),
                    "state": fake.state(),
                    "postal_code": fake.postcode(),
                    "country_code": "IT",
                    "is_default": 1 if j == 0 else 0,
                    "created_at": created,
                })
    return user_ids


def seed_products(n, category_ids):
    products = []
    base_names = ["Wireless Headphones", "Running Shoes", "Face Serum", "Yoga Mat", "Desk Lamp",
                  "Novel", "Leather Wallet", "Bluetooth Speaker", "Denim Jacket", "Water Bottle",
                  "Backpack", "Sunglasses", "Coffee Maker", "Notebook", "Phone Case"]
    for i in range(n):
        base = random.choice(base_names)
        name = f"{base} {fake.word().capitalize()}"
        price = round(random.uniform(9.99, 499.99), 2)
        cost = round(price * random.uniform(0.4, 0.7), 2)
        compare_at = round(price * random.uniform(1.05, 1.4), 2) if random.random() < 0.3 else None
        popularity = random.choice(["popular", "popular", "niche"])  # skew toward popular
        stock = random.randint(0, 500)
        created = rand_dt()
        pid = insert("products", {
            "sku": f"SKU-{10000+i}",
            "name": name,
            "slug": slugify(name, suffix=str(i)),
            "description": fake.paragraph(nb_sentences=3),
            "brand": fake.company(),
            "price": price,
            "compare_at_price": compare_at,
            "cost_price": cost,
            "stock_quantity": stock,
            "rating_avg": 0,
            "rating_count": 0,
            "is_active": 1,
            "deleted_at": None,
            "created_at": created,
            "updated_at": created,
        })
        cat_id = random.choice(category_ids)
        insert("product_categories", {
            "product_id": pid, "category_id": cat_id, "is_primary": 1, "created_at": created,
        })
        if random.random() < 0.25:
            other = random.choice([c for c in category_ids if c != cat_id])
            insert("product_categories", {
                "product_id": pid, "category_id": other, "is_primary": 0, "created_at": created,
            })
        products.append({
            "id": pid, "name": name, "price": price, "stock": stock,
            "popularity": popularity, "base_name": base,
        })
    return products


def pick_products_for_user(products, user, k):
    weights = [3 if p["popularity"] == "popular" else 1 for p in products]
    return random.choices(products, weights=weights, k=k)


def create_session_and_order(user, products):
    """One customer journey: browsing -> maybe cart -> maybe checkout."""
    session_id = fake.uuid4()
    is_guest = user is None
    user_id = None if is_guest else user["id"]
    session_start = rand_dt()
    t = session_start

    def log_event(event_type, product_id=None, value=None, source=None):
        nonlocal t
        t += timedelta(seconds=random.randint(5, 300))
        insert("user_events", {
            "user_id": user_id,
            "session_id": session_id,
            "product_id": product_id,
            "event_type": event_type,
            "event_value": value,
            "metadata": json.dumps({"source": source or random.choice(EVENT_SOURCES)}),
            "created_at": t,
        })

    log_event("login" if not is_guest else "page_view")

    browsed = random.sample(products, k=min(random.randint(2, 6), len(products)))
    for p in browsed:
        log_event("product_view", product_id=p["id"])
        if random.random() < 0.4:
            log_event("search", product_id=p["id"])

    cart_candidates = [p for p in browsed if random.random() < 0.5]
    if not cart_candidates:
        log_event("logout" if not is_guest else "page_view")
        return  # pure browse session, no cart

    cart_id = insert("carts", {
        "user_id": user_id,
        "session_id": session_id,
        "status": "active",
        "created_at": t,
        "updated_at": t,
    })
    cart_items = []
    for p in cart_candidates:
        qty = random.randint(1, 3)
        insert("cart_items", {
            "cart_id": cart_id, "product_id": p["id"], "quantity": qty,
            "unit_price": p["price"], "created_at": t, "updated_at": t,
        })
        log_event("add_to_cart", product_id=p["id"])
        cart_items.append({"product": p, "qty": qty})
        if random.random() < 0.15:
            log_event("remove_from_cart", product_id=p["id"])

    if random.random() < 0.15 or is_guest and random.random() < 0.5:
        # 15% of carts abandoned outright (guests abandon more often)
        cur.execute("UPDATE carts SET status='abandoned' WHERE cart_id=%s", (cart_id,))
        log_event("logout" if not is_guest else "page_view")
        return

    log_event("checkout_start")

    # guest converting to a real registered user at checkout
    if is_guest:
        created = t
        user_id = insert("users", {
            "email": fake.unique.email(),
            "password_hash": fake.sha256(),
            "first_name": fake.first_name(),
            "last_name": fake.last_name(),
            "role": "customer",
            "is_active": 1,
            "created_at": created,
            "updated_at": created,
        })
        insert("addresses", {
            "user_id": user_id, "address_type": "both",
            "first_name": fake.first_name(), "last_name": fake.last_name(),
            "address_line1": fake.street_address(), "address_line2": None,
            "city": fake.city(), "state": fake.state(), "postal_code": fake.postcode(),
            "country_code": "IT", "is_default": 1, "created_at": created,
        })
        cur.execute("UPDATE carts SET user_id=%s WHERE cart_id=%s", (user_id, cart_id))
        is_guest = False  # now a real registered customer for the rest of this order
        ship_first, ship_last = fake.first_name(), fake.last_name()
        ship_line1, ship_city, ship_state, ship_post = (
            fake.street_address(), fake.city(), fake.state(), fake.postcode())
    else:
        ship_first, ship_last = user.get("first_name", fake.first_name()), fake.last_name()
        ship_line1, ship_city, ship_state, ship_post = (
            fake.street_address(), fake.city(), fake.state(), fake.postcode())

    subtotal = sum(ci["product"]["price"] * ci["qty"] for ci in cart_items)
    shipping_cost = 0 if subtotal > 75 else 5.99
    tax = round(subtotal * 0.1, 2)
    discount = round(subtotal * 0.05, 2) if random.random() < 0.1 else 0
    total = round(subtotal + shipping_cost + tax - discount, 2)

    # order lifecycle outcome
    outcome = random.choices(
        ["delivered", "shipped", "processing", "confirmed", "cancelled_before_pay",
         "cancelled_after_pay", "payment_failed_retry"],
        weights=[45, 10, 8, 7, 10, 10, 10], k=1,
    )[0]

    status_map = {
        "delivered": "delivered", "shipped": "shipped", "processing": "processing",
        "confirmed": "confirmed", "cancelled_before_pay": "cancelled",
        "cancelled_after_pay": "refunded", "payment_failed_retry": "confirmed",
    }
    order_status = status_map[outcome]
    order_number = f"ORD-{t.strftime('%Y%m%d')}-{random.randint(100000,999999)}"
    placed_at = t + timedelta(minutes=random.randint(1, 30))

    order_id = insert("orders", {
        "user_id": user_id, "order_number": order_number, "status": order_status,
        "currency": "EUR", "subtotal": subtotal, "shipping_cost": shipping_cost,
        "tax_amount": tax, "discount_amount": discount, "total_amount": total,
        "shipping_first_name": ship_first, "shipping_last_name": ship_last,
        "shipping_address_line1": ship_line1, "shipping_address_line2": None,
        "shipping_city": ship_city, "shipping_state": ship_state,
        "shipping_postal_code": ship_post, "shipping_country_code": "IT",
        "placed_at": placed_at,
        "shipped_at": placed_at + timedelta(days=1) if order_status in ("shipped", "delivered") else None,
        "delivered_at": placed_at + timedelta(days=random.randint(2, 6)) if order_status == "delivered" else None,
        "cancelled_at": placed_at + timedelta(hours=random.randint(1, 48)) if order_status in ("cancelled", "refunded") else None,
        "created_at": placed_at, "updated_at": placed_at,
    })

    for ci in cart_items:
        p = ci["product"]
        line_total = round(p["price"] * ci["qty"], 2)
        insert("order_items", {
            "order_id": order_id, "product_id": p["id"],
            "product_name_at_purchase": p["name"], "quantity": ci["qty"],
            "price_at_purchase": p["price"], "line_total": line_total, "created_at": placed_at,
        })
        # stock decrement, restored if cancelled before any payment
        if outcome != "cancelled_before_pay":
            cur.execute(
                "UPDATE products SET stock_quantity = "
                "CASE WHEN stock_quantity >= %s THEN stock_quantity - %s ELSE 0 END "
                "WHERE product_id=%s",
                (ci["qty"], ci["qty"], p["id"]),
            )

    cur.execute("UPDATE carts SET status='converted', updated_at=%s WHERE cart_id=%s", (placed_at, cart_id))
    log_event("purchase", value=total)

    # --- payments, tied to lifecycle outcome ---
    if outcome == "cancelled_before_pay":
        pass  # never paid
    elif outcome == "payment_failed_retry":
        insert("payments", {
            "order_id": order_id, "provider": random.choice(PROVIDERS),
            "transaction_reference": fake.uuid4(), "status": "failed",
            "amount": total, "currency": "EUR", "paid_at": None,
            "created_at": placed_at, "updated_at": placed_at,
        })
        retry_time = placed_at + timedelta(minutes=random.randint(5, 60))
        insert("payments", {
            "order_id": order_id, "provider": random.choice(PROVIDERS),
            "transaction_reference": fake.uuid4(), "status": "paid",
            "amount": total, "currency": "EUR", "paid_at": retry_time,
            "created_at": retry_time, "updated_at": retry_time,
        })
    elif outcome == "cancelled_after_pay":
        insert("payments", {
            "order_id": order_id, "provider": random.choice(PROVIDERS),
            "transaction_reference": fake.uuid4(), "status": "refunded",
            "amount": total, "currency": "EUR", "paid_at": placed_at,
            "created_at": placed_at, "updated_at": placed_at + timedelta(hours=2),
        })
    else:
        insert("payments", {
            "order_id": order_id, "provider": random.choice(PROVIDERS),
            "transaction_reference": fake.uuid4(), "status": "paid",
            "amount": total, "currency": "EUR", "paid_at": placed_at,
            "created_at": placed_at, "updated_at": placed_at,
        })

    # --- reviews only on delivered orders ---
    if order_status == "delivered" and not is_guest:
        for ci in cart_items:
            if random.random() < 0.5:
                p = ci["product"]
                rating = random.choices([5, 4, 3, 2, 1], weights=[40, 30, 15, 10, 5])[0]
                review_time = placed_at + timedelta(days=random.randint(7, 20))
                insert("reviews", {
                    "user_id": user_id, "product_id": p["id"], "rating": rating,
                    "title": fake.sentence(nb_words=5),
                    "review_text": fake.paragraph(nb_sentences=2),
                    "is_verified_purchase": 1, "is_approved": 1 if random.random() < 0.95 else 0,
                    "created_at": review_time, "updated_at": review_time,
                })
                log_event("review", product_id=p["id"])
                cur.execute(
                    "UPDATE products SET rating_count = rating_count + 1, "
                    "rating_avg = ROUND(((rating_avg * (rating_count)) + %s) / (rating_count + 1), 2) "
                    "WHERE product_id=%s",
                    (rating, p["id"]),
                )

    # --- occasional wishlist add, independent of cart ---
    if not is_guest and random.random() < 0.3:
        wp = random.choice(products)
        try:
            insert("wishlists", {
                "user_id": user_id, "product_id": wp["id"], "created_at": t,
            })
            log_event("wishlist_add", product_id=wp["id"])
        except mysql.connector.Error:
            pass  # duplicate wishlist entry, skip

    log_event("logout" if not is_guest else "page_view")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    print(f"Seeding {'SMALL debug' if args.small else 'FULL'} dataset...")
    category_ids = seed_categories()
    print(f"  categories: {len(category_ids)} top-level")

    users = seed_users(N_USERS)
    print(f"  users: {len(users)}")

    products = seed_products(N_PRODUCTS, category_ids)
    print(f"  products: {len(products)}")

    n_guest_sessions = max(1, N_SESSIONS // 4)
    n_user_sessions = N_SESSIONS - n_guest_sessions

    for _ in range(n_user_sessions):
        user = random.choice(users)
        try:
            create_session_and_order(user, products)
        except Exception as e:
            conn.rollback()
            print(f"  [skipped a user session due to error: {e}]")

    for _ in range(n_guest_sessions):
        try:
            create_session_and_order(None, products)
        except Exception as e:
            conn.rollback()
            print(f"  [skipped a guest session due to error: {e}]")

    print(f"  sessions generated: {N_SESSIONS} ({n_user_sessions} user, {n_guest_sessions} guest)")
    print("Done.")


if __name__ == "__main__":
    main()
    cur.close()
    conn.close()
