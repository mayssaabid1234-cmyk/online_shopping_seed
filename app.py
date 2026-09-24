"""
app.py - minimal Flask API on top of recommender.py

Endpoints:
    GET /api/products/<product_id>/recommendations?n=10
        -> "people who interacted with this also liked..."

    GET /api/users/<user_id>/recommendations?n=10
        -> personalized recommendations for that user

    GET /api/popular?n=10
        -> popularity fallback (cold start / no history)

    GET /api/health
        -> quick check that the API and DB connection are alive

Run:
    pip install flask
    python app.py
Then open, e.g.:
    http://localhost:5000/api/products/1/recommendations
    http://localhost:5000/api/users/1/recommendations
    http://localhost:5000/api/popular
"""

import json

from flask import Flask, jsonify, request

from recommender import (
    recommend_products,
    recommend_for_user,
    popular_products,
    get_connection,
)

app = Flask(__name__, static_folder="static", static_url_path="")


@app.route("/")
def index():
    return app.send_static_file("index.html")


def get_n(default=10, maximum=50):
    """Read ?n=... from the query string, with sane bounds."""
    try:
        n = int(request.args.get("n", default))
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))


@app.route("/api/health")
def health():
    try:
        conn = get_connection()
        conn.close()
        return jsonify({"status": "ok", "database": "connected"})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/api/products/<int:product_id>/recommendations")
def product_recommendations(product_id):
    n = get_n()
    try:
        results = recommend_products(product_id, n=n)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "product_id": product_id,
        "count": len(results),
        "recommendations": results,
    })


@app.route("/api/users/<int:user_id>/recommendations")
def user_recommendations(user_id):
    n = get_n()
    try:
        results = recommend_for_user(user_id, n=n)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "user_id": user_id,
        "count": len(results),
        "recommendations": results,
    })


VALID_EVENT_TYPES = {
    "page_view", "search", "product_view", "add_to_cart", "remove_from_cart",
    "wishlist_add", "wishlist_remove", "checkout_start", "purchase",
    "review", "login", "logout",
}


@app.route("/api/events", methods=["POST"])
def log_event():
    data = request.get_json(silent=True) or {}

    event_type = data.get("event_type")
    if event_type not in VALID_EVENT_TYPES:
        return jsonify({"error": f"invalid event_type: {event_type!r}"}), 400

    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    user_id = data.get("user_id")  # None for guests, matches schema's nullable FK
    product_id = data.get("product_id")
    event_value = data.get("event_value")
    metadata = data.get("metadata") or {}

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO user_events "
            "(user_id, session_id, product_id, event_type, event_value, metadata, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, NOW())",
            (user_id, session_id, product_id, event_type, event_value, json.dumps(metadata)),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"status": "logged"}), 201


@app.route("/api/products")
def list_products():
    n = get_n(default=24, maximum=100)
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT product_id, name, brand, price, rating_avg, rating_count "
            "FROM products WHERE is_active = 1 "
            "ORDER BY product_id LIMIT %s",
            (n,),
        )
        results = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"count": len(results), "products": results})


@app.route("/api/popular")
def popular():
    n = get_n()
    try:
        results = popular_products(n=n)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "count": len(results),
        "products": results,
    })


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found. Try /api/health, /api/popular, "
                              "/api/products/<id>/recommendations, "
                              "/api/users/<id>/recommendations"}), 404


if __name__ == "__main__":
    app.run(debug=True, port=5000)
