import os
from datetime import date, datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import psycopg

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# --- Read permissions (who can use the bot at all) ---
# If ALLOWED_USER_IDS is empty -> allow everyone (same as before)
ALLOWED_USER_IDS = set()
_raw_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
if _raw_allowed:
    for x in _raw_allowed.split(","):
        x = x.strip()
        if x.isdigit():
            ALLOWED_USER_IDS.add(int(x))

# --- Write permissions (who can run /setdaily) ---
# For now you are the only writer; later add GM's Telegram ID here (comma-separated).
# Default to your ID if env var not set.
DEFAULT_WRITERS = "446855702"
_raw_writers = os.getenv("WRITER_USER_IDS", DEFAULT_WRITERS).strip()
WRITER_USER_IDS = set()
if _raw_writers:
    for x in _raw_writers.split(","):
        x = x.strip()
        if x.isdigit():
            WRITER_USER_IDS.add(int(x))

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

# --- Database connection ---
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    return psycopg.connect(DATABASE_URL)

# --- Create tables if not exists ---
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

# --- Settings helpers ---
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

# --- Report helpers ---
def fetch_daily(day: date):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sales, covers FROM daily_stats WHERE day=%s;", (day,))
            return cur.fetchone()

def render_daily_report(day: date) -> str | None:
    row = fetch_daily(day)
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

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

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

# --- Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text(
        "👋 Norah Ops is online.\n\n"
        "Commands:\n"
        "/setdaily SALES COVERS   (writers only)\n"
        "/daily\n"
        "/month\n"
        "/lastmonth\n"
        "/last N\n"
        "/range YYYY-MM-DD YYYY-MM-DD\n"
        "/setowners   (run in owners dashboard once)\n"
        "/help"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text(
        "Usage:\n"
        "/setdaily 2450 118\n"
        "/daily\n"
        "/month\n"
        "/lastmonth\n"
        "/last 7\n"
        "/range 2026-01-01 2026-01-31\n"
        "/setowners"
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

    try:
        sales = float(context.args[0])
        covers = int(context.args[1])
    except Exception:
        await update.effective_message.reply_text(
            "Usage: /setdaily SALES COVERS\nExample: /setdaily 2450 118"
        )
        return

    today = date.today()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daily_stats (day, sales, covers)
                VALUES (%s, %s, %s)
                ON CONFLICT (day)
                DO UPDATE SET sales = EXCLUDED.sales, covers = EXCLUDED.covers;
            """, (today, sales, covers))
        conn.commit()

    await update.effective_message.reply_text(f"Saved ✅  Sales: €{sales} | Covers: {covers}")

    # Auto-broadcast to owners dashboard(s)
    owners = parse_chat_ids(get_setting("OWNERS_CHAT_IDS"))
    text = render_daily_report(today)
    if owners and text:
        for cid in owners:
            try:
                await context.bot.send_message(chat_id=cid, text=text)
            except Exception:
                pass

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    today = date.today()
    text = render_daily_report(today)
    if not text:
        await update.effective_message.reply_text("No data for today yet. Use: /setdaily 2450 118")
        return
    await update.effective_message.reply_text(text)

async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    today = date.today()
    start = today.replace(day=1)
    text = render_range_report("Norah Month-to-Date", start, today)
    await update.effective_message.reply_text(text)

async def lastmonth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    today = date.today()
    first_this = today.replace(day=1)
    # last day of previous month:
    last_prev = first_this.fromordinal(first_this.toordinal() - 1)
    start_prev = last_prev.replace(day=1)
    text = render_range_report("Norah Last Month", start_prev, last_prev)
    await update.effective_message.reply_text(text)

async def last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        n = int(context.args[0])
        if n <= 0 or n > 366:
            raise ValueError
    except Exception:
        await update.effective_message.reply_text("Usage: /last N  (example: /last 7)")
        return

    today = date.today()
    start = today.fromordinal(today.toordinal() - (n - 1))
    text = render_range_report(f"Norah Last {n} Days", start, today)
    await update.effective_message.reply_text(text)

async def range_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        start = parse_ymd(context.args[0])
        end = parse_ymd(context.args[1])
        if end < start:
            raise ValueError
    except Exception:
        await update.effective_message.reply_text(
            "Usage: /range YYYY-MM-DD YYYY-MM-DD\nExample: /range 2026-01-01 2026-01-31"
        )
        return

    text = render_range_report("Norah Custom Range", start, end)
    await update.effective_message.reply_text(text)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setowners", setowners))
    app.add_handler(CommandHandler("setdaily", setdaily))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("lastmonth", lastmonth))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("range", range_cmd))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
