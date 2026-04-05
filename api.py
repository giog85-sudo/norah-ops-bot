import os
from datetime import date, timedelta

import psycopg
from flask import Flask, jsonify, request
from flask_cors import CORS

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY", "").strip()

app = Flask(__name__)
CORS(app)


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    return psycopg.connect(DATABASE_URL)


def _check_auth():
    if not DASHBOARD_API_KEY:
        return False
    return request.headers.get("Authorization", "") == f"Bearer {DASHBOARD_API_KEY}"


# =============================================================================
# GET /api/stats/daily?from=YYYY-MM-DD&to=YYYY-MM-DD
# =============================================================================
@app.route("/api/stats/daily")
def stats_daily():
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    from_str = request.args.get("from")
    to_str = request.args.get("to")
    if not from_str or not to_str:
        return jsonify({"error": "from and to query params required (YYYY-MM-DD)"}), 400

    try:
        from_date = date.fromisoformat(from_str)
        to_date = date.fromisoformat(to_str)
    except ValueError:
        return jsonify({"error": "Invalid date format, use YYYY-MM-DD"}), 400

    if to_date < from_date:
        return jsonify({"error": "to must be >= from"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day,
                       total_sales,
                       lunch_pax + dinner_pax AS total_covers,
                       lunch_sales,
                       dinner_sales,
                       lunch_pax,
                       dinner_pax
                FROM full_daily_stats
                WHERE day BETWEEN %s AND %s
                ORDER BY day
                """,
                (from_date, to_date),
            )
            rows = cur.fetchall()

    result = []
    for row in rows:
        day, total_sales, total_covers, lunch_sales, dinner_sales, lunch_covers, dinner_covers = row
        total_covers = int(total_covers or 0)
        total_sales = float(total_sales or 0)
        avg_ticket = round(total_sales / total_covers, 2) if total_covers else 0.0
        result.append({
            "date": day.isoformat(),
            "total_sales": round(total_sales, 2),
            "total_covers": total_covers,
            "avg_ticket": avg_ticket,
            "lunch_sales": round(float(lunch_sales or 0), 2),
            "dinner_sales": round(float(dinner_sales or 0), 2),
            "lunch_covers": int(lunch_covers or 0),
            "dinner_covers": int(dinner_covers or 0),
        })

    return jsonify(result)


# =============================================================================
# GET /api/stats/weekly?weeks=N  (default 8, max 52)
# =============================================================================
@app.route("/api/stats/weekly")
def stats_weekly():
    if not _check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        weeks = int(request.args.get("weeks", 8))
        if not 1 <= weeks <= 52:
            raise ValueError()
    except ValueError:
        return jsonify({"error": "weeks must be an integer between 1 and 52"}), 400

    today = date.today()
    # Last completed Mon–Sun week
    last_monday = today - timedelta(days=today.weekday() + 7)
    range_start = last_monday - timedelta(weeks=weeks - 1)
    range_end = last_monday + timedelta(days=6)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day,
                       total_sales,
                       lunch_pax + dinner_pax AS covers
                FROM full_daily_stats
                WHERE day BETWEEN %s AND %s
                ORDER BY day
                """,
                (range_start, range_end),
            )
            rows = cur.fetchall()

    # Group rows into Mon–Sun buckets
    buckets = {}
    for day, total_sales, covers in rows:
        monday = day - timedelta(days=day.weekday())
        if monday not in buckets:
            buckets[monday] = {"total_sales": 0.0, "total_covers": 0}
        buckets[monday]["total_sales"] += float(total_sales or 0)
        buckets[monday]["total_covers"] += int(covers or 0)

    result = []
    for i in range(weeks):
        week_start = last_monday - timedelta(weeks=weeks - 1 - i)
        week_end = week_start + timedelta(days=6)
        b = buckets.get(week_start, {"total_sales": 0.0, "total_covers": 0})
        total_sales = round(b["total_sales"], 2)
        total_covers = b["total_covers"]
        avg_ticket = round(total_sales / total_covers, 2) if total_covers else 0.0
        result.append({
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "total_sales": total_sales,
            "total_covers": total_covers,
            "avg_ticket": avg_ticket,
        })

    return jsonify(result)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
