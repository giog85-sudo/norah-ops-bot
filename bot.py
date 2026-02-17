import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import psycopg


# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Default timezone for business-day logic (Madrid)
TIMEZONE = os.getenv("TIMEZONE", "Europe/Madrid").strip() or "Europe/Madrid"

# Business day cutoff hour (0-23). If report is sent BEFORE this hour, count it as "yesterday".
# You requested 11:00
CUTOFF_HOUR = int(os.getenv("CUTOFF_HOUR", "11").strip() or "11")

# If ALLOWED_USER_IDS is empty -> allow everyone to read/query (same behavior as before)
ALLOWED_USER_IDS = set()
_raw_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
if _raw_allowed:
    for x in _raw_allowed.split(","):
        x = x.strip()
        if x.isdigit():
            ALLOWED_USER_IDS.add(int(x))

# Writers: only these users can run /setdaily and /edit
# Keep default to your ID, can add GM later: "446855702,GM_ID"
DEFAULT_WRITERS = "446855702"
_raw_writers = os.getenv("WRITER_USER_IDS", DEFAULT_WRITERS).strip()
WRITER_USER_IDS = set()
if _raw_writers:
    for x in _raw_writers.split(","):
        x = x.strip()
        if x.isdigit():
            WRITER_USER_IDS.add(int(x))


# =========================
# SECURITY
# =========================
def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return bool(update.effective_user and update.effective_user.id in ALLOWED_USER_IDS)

def is_writer(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in WRITER_USER_IDS)

async def guard(update: Update) -> bool:
    if not is_allowed(update):
        if update.effective_message:
            await update.effective_message.reply_text("Not authorized.")
        return False
    return True


# =========================
# DB
# =========================
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    return psycopg.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    day DATE PRIMARY KEY,
                    sales DOUBLE PRECISION,
                    covers INTEGER
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
        conn.commit()


# =========================
# SETTINGS (owners broadcast chat ids)
# =========================
def get_setting(key: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_settings WHERE key=%s;", (key,))
            row = cur.fetchone()
    return row[0] if row else None

def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value;
            """, (key, value))
        conn.commit()

def parse_chat_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    out = []
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.append(int(x))
        except ValueError:
            pass
    return out


# =========================
# DATE / BUSINESS DAY LOGIC
# =========================
def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))

def business_day_from_now() -> date:
    """
    If current local time is before cutoff hour, treat as previous business day.
    Example with CUTOFF_HOUR=11:
      00:30 -> yesterday
      10:59 -> yesterday
      11:01 -> today
    """
    n = now_local()
    d = n.date()
    if n.hour < CUTOFF_HOUR:
        return d - timedelta(days=1)
    return d

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def add_months(d: date, months: int) -> date:
    """
    Add (or subtract) months from a date, keeping day-of-month where possible.
    If day doesn't exist (e.g. 31st), clamp to last day of target month.
    """
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    # clamp day
    # find last day of target month:
    if m == 12:
        next_month = date(y + 1, 1, 1)
    else:
        next_month = date(y, m + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    day = min(d.day, last_day)
    return date(y, m, day)

def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        # Feb 29 -> Feb 28 on non-leap years
        return d.replace(month=2, day=28, year=d.year + years)


# =========================
# REPORT HELPERS
# =========================
def fetch_day(day: date):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sales, covers FROM daily_stats WHERE day=%s;", (day,))
            return cur.fetchone()

def render_day_report(day: date) -> str | None:
    row = fetch_day(day)
    if not row:
        return None
    sales, covers = row
    avg = (sales / covers) if covers else 0
    return (
        f"📊 Norah Daily Report ({day.isoformat()})\n\n"
        f"Sales: €{sales:.2f}\n"
        f"Covers: {covers}\n"
        f"Avg ticket: €{avg:.2f}"
    )

def summarize_range(start: date, end: date) -> tuple[float, int, int]:
    """Return (sales_sum, covers_sum, day_count) for inclusive date range."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(SUM(sales), 0) AS sales_sum,
                    COALESCE(SUM(covers), 0) AS covers_sum,
                    COUNT(*) AS day_count
                FROM daily_stats
                WHERE day BETWEEN %s AND %s;
            """, (start, end))
            sales_sum, covers_sum, day_count = cur.fetchone()
    return float(sales_sum), int(covers_sum), int(day_count)

def render_range_report(title: str, start: date, end: date) -> str:
    sales_sum, covers_sum, day_count = summarize_range(start, end)
    avg = (sales_sum / covers_sum) if covers_sum else 0
    return (
        f"📈 {title}\n"
        f"Period: {start.isoformat()} → {end.isoformat()} ({day_count} day(s))\n\n"
        f"Total sales: €{sales_sum:.2f}\n"
        f"Total covers: {covers_sum}\n"
        f"Avg ticket: €{avg:.2f}"
    )

def fetch_best_worst(best: bool = True):
    order = "DESC" if best else "ASC"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT day, sales, covers
                FROM daily_stats
                WHERE sales IS NOT NULL
                ORDER BY sales {order}
                LIMIT 1;
            """)
            return cur.fetchone()

def parse_period_to_start(n_str: str, end: date) -> date | None:
    """
    Accepts:
      "7"   -> last 7 days
      "6M"  -> last 6 months
      "1Y"  -> last 1 year
    """
    s = n_str.strip().upper()
    m = re.fullmatch(r"(\d+)([MY])?", s)
    if not m:
        return None
    num = int(m.group(1))
    unit = m.group(2) or "D"

    if num <= 0 or num > 5000:
        return None

    if unit == "D":
        # inclusive: last N days ending at 'end'
        return end - timedelta(days=num - 1)
    if unit == "M":
        # start is same day N months ago (inclusive)
        return add_months(end, -num)
    if unit == "Y":
        return add_years(end, -num)
    return None


# =========================
# TELEGRAM COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text(
        "👋 Norah Ops is online.\n\n"
        "Key commands:\n"
        "/setdaily SALES COVERS\n"
        "/setdaily YYYY-MM-DD SALES COVERS\n"
        "/daily  (business day)\n"
        "/today  (calendar day)\n"
        "/month\n"
        "/lastmonth\n"
        "/last 7 | /last 6M | /last 1Y\n"
        "/last7\n"
        "/range YYYY-MM-DD YYYY-MM-DD\n"
        "/bestday\n"
        "/worstday\n"
        "/edit YYYY-MM-DD SALES COVERS\n"
        "/setowners  (run once in locked owners group)\n"
        "/help"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text(
        "Examples:\n"
        "/setdaily 4820 114\n"
        "/setdaily 2026-02-16 4820 114\n"
        "/daily\n"
        "/today\n"
        "/month\n"
        "/lastmonth\n"
        "/last 7\n"
        "/last 6M\n"
        "/last 1Y\n"
        "/range 2026-03-15 2026-04-07\n"
        "/bestday\n"
        "/worstday\n"
        "/edit 2026-02-16 4820 114\n"
        "\nNotes:\n"
        f"Business-day cutoff: {CUTOFF_HOUR:02d}:00 ({TIMEZONE})"
    )

async def setowners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    chat_id = update.effective_chat.id
    current = parse_chat_ids(get_setting("OWNERS_CHAT_IDS"))
    if chat_id not in current:
        current.append(chat_id)
        set_setting("OWNERS_CHAT_IDS", ",".join(str(x) for x in current))
    await update.effective_message.reply_text(
        f"✅ Owners destination saved.\nChat ID: {chat_id}\nTotal destinations: {len(current)}"
    )

async def setdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not is_writer(update):
        await update.effective_message.reply_text("Not authorized to set daily stats.")
        return

    # Support:
    # /setdaily SALES COVERS
    # /setdaily YYYY-MM-DD SALES COVERS
    args = context.args
    try:
        if len(args) == 2:
            sales = float(args[0])
            covers = int(args[1])
            day = business_day_from_now()
        elif len(args) == 3:
            day = parse_ymd(args[0])
            sales = float(args[1])
            covers = int(args[2])
        else:
            raise ValueError
    except Exception:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/setdaily SALES COVERS\n"
            "/setdaily YYYY-MM-DD SALES COVERS\n"
            "Example: /setdaily 2450 118\n"
            "Example: /setdaily 2026-02-16 2450 118"
        )
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daily_stats (day, sales, covers)
                VALUES (%s, %s, %s)
                ON CONFLICT (day)
                DO UPDATE SET sales = EXCLUDED.sales, covers = EXCLUDED.covers;
            """, (day, sales, covers))
        conn.commit()

    # Detect if cutoff rule moved the day
now = now_local()
calendar_today = now.date()

if day != calendar_today:
    note = f"(before {CUTOFF_HOUR:02d}:00 cutoff — recorded as previous business day)"
else:
    note = "(recorded as today’s business day)"

await update.effective_message.reply_text(
    f"Saved ✅\n"
    f"Business day: {day.isoformat()} {note}\n"
    f"Sales: €{sales:.2f}\n"
    f"Covers: {covers}"
)

    # Auto-broadcast to owners dashboard(s)
    owners = parse_chat_ids(get_setting("OWNERS_CHAT_IDS"))
    text = render_day_report(day)
    if owners and text:
        for cid in owners:
            try:
                await context.bot.send_message(chat_id=cid, text=text)
            except Exception:
                pass

async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not is_writer(update):
        await update.effective_message.reply_text("Not authorized to edit daily stats.")
        return

    args = context.args
    try:
        day = parse_ymd(args[0])
        sales = float(args[1])
        covers = int(args[2])
    except Exception:
        await update.effective_message.reply_text(
            "Usage: /edit YYYY-MM-DD SALES COVERS\nExample: /edit 2026-02-16 2450 118"
        )
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daily_stats (day, sales, covers)
                VALUES (%s, %s, %s)
                ON CONFLICT (day)
                DO UPDATE SET sales = EXCLUDED.sales, covers = EXCLUDED.covers;
            """, (day, sales, covers))
        conn.commit()

    await update.effective_message.reply_text(
        f"Edited ✅ Day: {day.isoformat()} | Sales: €{sales:.2f} | Covers: {covers}"
    )

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Business-day daily report (uses cutoff)."""
    if not await guard(update): return
    day = business_day_from_now()
    text = render_day_report(day)
    if not text:
        await update.effective_message.reply_text(
            f"No data for business day {day.isoformat()} yet.\n"
            "Use: /setdaily SALES COVERS"
        )
        return
    await update.effective_message.reply_text(text)

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Calendar-day report (pure date.today in local tz)."""
    if not await guard(update): return
    d = now_local().date()
    text = render_day_report(d)
    if not text:
        await update.effective_message.reply_text(
            f"No data for calendar day {d.isoformat()} yet."
        )
        return
    await update.effective_message.reply_text(text)

async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    today = now_local().date()
    start = today.replace(day=1)
    await update.effective_message.reply_text(
        render_range_report("Norah Month-to-Date", start, today)
    )

async def lastmonth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    today = now_local().date()
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    start_prev = last_prev.replace(day=1)
    await update.effective_message.reply_text(
        render_range_report("Norah Last Month", start_prev, last_prev)
    )

async def last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        token = context.args[0]
    except Exception:
        await update.effective_message.reply_text("Usage: /last 7  OR  /last 6M  OR  /last 1Y")
        return

    end = now_local().date()
    start = parse_period_to_start(token, end)
    if not start:
        await update.effective_message.reply_text("Usage: /last 7  OR  /last 6M  OR  /last 1Y")
        return

    await update.effective_message.reply_text(
        render_range_report(f"Norah Last {token.upper()}", start, end)
    )

async def last7(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    end = now_local().date()
    start = end - timedelta(days=6)
    await update.effective_message.reply_text(
        render_range_report("Norah Last 7 Days", start, end)
    )

async def range_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        start = parse_ymd(context.args[0])
        end = parse_ymd(context.args[1])
        if end < start:
            raise ValueError
    except Exception:
        await update.effective_message.reply_text(
            "Usage: /range YYYY-MM-DD YYYY-MM-DD\nExample: /range 2026-03-15 2026-04-07"
        )
        return

    await update.effective_message.reply_text(
        render_range_report("Norah Custom Range", start, end)
    )

async def bestday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    row = fetch_best_worst(best=True)
    if not row:
        await update.effective_message.reply_text("No data yet.")
        return
    d, sales, covers = row
    avg = (sales / covers) if covers else 0
    await update.effective_message.reply_text(
        f"🏆 Norah Best Day\n\n"
        f"Day: {d.isoformat()}\n"
        f"Sales: €{sales:.2f}\n"
        f"Covers: {covers}\n"
        f"Avg ticket: €{avg:.2f}"
    )

async def worstday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    row = fetch_best_worst(best=False)
    if not row:
        await update.effective_message.reply_text("No data yet.")
        return
    d, sales, covers = row
    avg = (sales / covers) if covers else 0
    await update.effective_message.reply_text(
        f"🧊 Norah Worst Day\n\n"
        f"Day: {d.isoformat()}\n"
        f"Sales: €{sales:.2f}\n"
        f"Covers: {covers}\n"
        f"Avg ticket: €{avg:.2f}"
    )


# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setowners", setowners))
    app.add_handler(CommandHandler("setdaily", setdaily))
    app.add_handler(CommandHandler("edit", edit))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("lastmonth", lastmonth))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("last7", last7))
    app.add_handler(CommandHandler("range", range_cmd))
    app.add_handler(CommandHandler("bestday", bestday))
    app.add_handler(CommandHandler("worstday", worstday))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
