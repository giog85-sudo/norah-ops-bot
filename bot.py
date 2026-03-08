
# =========================
# NORAH OPS BOT — ANALYTICS EXTENDED VERSION
# Generated for deployment (v2)
# =========================

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo
from collections import Counter, defaultdict

import psycopg
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV / SETTINGS
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

TZ_NAME = (os.getenv("TZ_NAME") or os.getenv("TIMEZONE") or "Europe/Madrid").strip() or "Europe/Madrid"
CUTOFF_HOUR = int((os.getenv("CUTOFF_HOUR", "11").strip() or "11"))
WEEKLY_DIGEST_HOUR = int((os.getenv("WEEKLY_DIGEST_HOUR", "9").strip() or "9"))

DAILY_POST_HOUR = int((os.getenv("DAILY_POST_HOUR", "11").strip() or "11"))
DAILY_POST_MINUTE = int((os.getenv("DAILY_POST_MINUTE", "5").strip() or "5"))

TZ = ZoneInfo(TZ_NAME)

# =========================
# DATABASE
# =========================
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    return psycopg.connect(DATABASE_URL)


# =========================
# DATE HELPERS
# =========================
def now_local():
    return datetime.now(TZ)


def business_day_for(ts: datetime):
    if ts.hour < CUTOFF_HOUR:
        return ts.date() - timedelta(days=1)
    return ts.date()


def business_day_today():
    return business_day_for(now_local())


def previous_business_day():
    return business_day_today() - timedelta(days=1)


# =========================
# CORE DB HELPERS
# =========================
def get_day(day):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sales, covers
                FROM daily_stats
                WHERE day=%s
                """,
                (day,),
            )
            return cur.fetchone()


def get_period(start, end):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day, sales, covers
                FROM daily_stats
                WHERE day BETWEEN %s AND %s
                """,
                (start, end),
            )
            return cur.fetchall()


def aggregate(rows):
    sales = sum(r[1] or 0 for r in rows)
    covers = sum(r[2] or 0 for r in rows)
    avg = sales / covers if covers else 0
    return sales, covers, avg


def pct_diff(a, b):
    if b == 0:
        return 0
    return ((a - b) / b) * 100


# =========================
# BASIC SNAPSHOT COMMANDS
# =========================
async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day = business_day_today()
    row = get_day(day)
    if not row:
        await update.message.reply_text("No data yet for today.")
        return

    sales, covers = row
    avg = sales / covers if covers else 0

    msg = (
        f"📊 Today Snapshot\n"
        f"Day: {day}\n\n"
        f"Sales: €{sales:.2f}\n"
        f"Covers: {covers}\n"
        f"Avg ticket: €{avg:.2f}"
    )
    await update.message.reply_text(msg)


async def yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day = previous_business_day()
    row = get_day(day)

    if not row:
        await update.message.reply_text("No data for yesterday.")
        return

    sales, covers = row
    avg = sales / covers if covers else 0

    msg = (
        f"📊 Yesterday Snapshot\n"
        f"Day: {day}\n\n"
        f"Sales: €{sales:.2f}\n"
        f"Covers: {covers}\n"
        f"Avg ticket: €{avg:.2f}"
    )
    await update.message.reply_text(msg)


# =========================
# SAME WEEKDAY COMPARISON
# =========================
async def dow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /dow 5")
        return

    n = int(context.args[0])
    today = business_day_today()
    weekday = today.weekday()

    dates = []
    d = today - timedelta(days=7)

    while len(dates) < n:
        if d.weekday() == weekday:
            dates.append(d)
        d -= timedelta(days=1)

    rows = []
    for d in dates:
        r = get_day(d)
        if r:
            rows.append((d, r[0], r[1]))

    benchmark_sales, benchmark_covers, benchmark_avg = aggregate(rows)

    today_row = get_day(today)
    if not today_row:
        await update.message.reply_text("No data for today.")
        return

    sales, covers = today_row
    avg = sales / covers if covers else 0

    msg = (
        f"📊 Same Weekday Comparison\n"
        f"Today: {today}\n"
        f"Benchmark: last {n} same weekdays\n\n"
        f"Sales: €{sales:.2f} vs €{benchmark_sales/n:.2f} ({pct_diff(sales, benchmark_sales/n):+.1f}%)\n"
        f"Covers: {covers} vs {benchmark_covers/n:.1f}\n"
        f"Avg ticket: €{avg:.2f} vs €{benchmark_avg:.2f}"
    )

    await update.message.reply_text(msg)


# =========================
# WEEK COMPARISON
# =========================
async def weekcompare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = business_day_today()

    start_this = today - timedelta(days=today.weekday())
    start_last = start_this - timedelta(days=7)

    end_last = start_last + (today - start_this)

    rows_this = get_period(start_this, today)
    rows_last = get_period(start_last, end_last)

    s1, c1, a1 = aggregate(rows_this)
    s2, c2, a2 = aggregate(rows_last)

    msg = (
        f"📊 Week Comparison\n"
        f"This week vs last week\n\n"
        f"Sales: €{s1:.2f} vs €{s2:.2f} ({pct_diff(s1,s2):+.1f}%)\n"
        f"Covers: {c1} vs {c2}\n"
        f"Avg ticket: €{a1:.2f} vs €{a2:.2f}"
    )

    await update.message.reply_text(msg)


# =========================
# MONTH COMPARISON
# =========================
async def monthcompare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = business_day_today()

    start_this = date(today.year, today.month, 1)
    start_last = (start_this - timedelta(days=1)).replace(day=1)

    day_count = (today - start_this).days

    end_last = start_last + timedelta(days=day_count)

    rows_this = get_period(start_this, today)
    rows_last = get_period(start_last, end_last)

    s1, c1, a1 = aggregate(rows_this)
    s2, c2, a2 = aggregate(rows_last)

    msg = (
        f"📊 Month Comparison\n"
        f"This month vs same days last month\n\n"
        f"Sales: €{s1:.2f} vs €{s2:.2f} ({pct_diff(s1,s2):+.1f}%)\n"
        f"Covers: {c1} vs {c2}\n"
        f"Avg ticket: €{a1:.2f} vs €{a2:.2f}"
    )

    await update.message.reply_text(msg)


# =========================
# WEEKEND COMPARISON
# Weekend = Friday + Saturday
# =========================
def get_last_weekend():
    today = business_day_today()
    d = today

    while d.weekday() != 4:  # Friday
        d -= timedelta(days=1)

    fri = d
    sat = d + timedelta(days=1)

    return fri, sat


async def weekendcompare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fri, sat = get_last_weekend()

    last_fri = fri - timedelta(days=7)
    last_sat = sat - timedelta(days=7)

    rows_this = []
    rows_last = []

    for d in [fri, sat]:
        r = get_day(d)
        if r:
            rows_this.append((d, r[0], r[1]))

    for d in [last_fri, last_sat]:
        r = get_day(d)
        if r:
            rows_last.append((d, r[0], r[1]))

    s1, c1, a1 = aggregate(rows_this)
    s2, c2, a2 = aggregate(rows_last)

    msg = (
        f"🌙 Weekend Comparison (Fri+Sat)\n\n"
        f"Sales: €{s1:.2f} vs €{s2:.2f} ({pct_diff(s1,s2):+.1f}%)\n"
        f"Covers: {c1} vs {c2}\n"
        f"Avg ticket: €{a1:.2f} vs €{a2:.2f}"
    )

    await update.message.reply_text(msg)


# =========================
# WEEKDAY MIX
# =========================
async def weekdaymix(update: Update, context: ContextTypes.DEFAULT_TYPE):

    weeks = int(context.args[0]) if context.args else 8

    end = business_day_today()
    start = end - timedelta(days=weeks * 7)

    rows = get_period(start, end)

    bucket = defaultdict(list)

    for d, sales, covers in rows:
        bucket[d.weekday()].append((sales, covers))

    lines = ["📅 Weekday Pattern"]
    names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

    for i in range(7):
        items = bucket[i]
        if not items:
            continue

        s = sum(x[0] for x in items)
        c = sum(x[1] for x in items)

        avg_ticket = s/c if c else 0
        avg_sales = s/len(items)

        lines.append(f"{names[i]} — Sales €{avg_sales:.2f} | Avg ticket €{avg_ticket:.2f}")

    await update.message.reply_text("\n".join(lines))


# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("yesterday", yesterday))
    app.add_handler(CommandHandler("dow", dow))
    app.add_handler(CommandHandler("weekcompare", weekcompare))
    app.add_handler(CommandHandler("monthcompare", monthcompare))
    app.add_handler(CommandHandler("weekendcompare", weekendcompare))
    app.add_handler(CommandHandler("weekdaymix", weekdaymix))

    app.run_polling()


if __name__ == "__main__":
    main()
