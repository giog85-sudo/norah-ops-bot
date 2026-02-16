import os
from datetime import date
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import psycopg

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# --- Security (allowed users) ---
# If ALLOWED_USER_IDS is empty -> allow everyone (same behavior as your current bot)
ALLOWED_USER_IDS = set()
_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
if _raw:
    for x in _raw.split(","):
        x = x.strip()
        if x.isdigit():
            ALLOWED_USER_IDS.add(int(x))

def is_allowed(update: Update) -> bool:
    # Channels don't have a normal "effective_user" like private chats/groups.
    # If you restrict users, we still allow commands in channels/groups by only allowing admins to call /setowners
    # but for simplicity: if ALLOWED_USER_IDS is set and we can't identify user -> deny.
    if not ALLOWED_USER_IDS:
        return True
    return bool(update.effective_user and update.effective_user.id in ALLOWED_USER_IDS)

async def guard(update: Update) -> bool:
    if not is_allowed(update):
        # effective_message works for message + channel_post
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

# --- /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text(
        "👋 Norah Ops is online.\n\n"
        "Commands:\n"
        "/setdaily SALES COVERS\n"
        "/daily\n"
        "/setowners   (run in owners chat once)\n"
        "/help"
    )

# --- /help ---
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text(
        "Usage:\n"
        "/setdaily 2450 118\n"
        "/daily\n"
        "/setowners   (run in owners chat/group/channel once)"
    )

# --- /setowners ---
# Run this ONCE inside the Owners room (group or channel).
# It stores that chat_id as a broadcast destination.
async def setowners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return

    chat_id = update.effective_chat.id  # works for groups + channels
    current = parse_chat_ids(get_setting("OWNERS_CHAT_IDS"))

    if chat_id not in current:
        current.append(chat_id)
        set_setting("OWNERS_CHAT_IDS", ",".join(str(x) for x in current))

    await update.effective_message.reply_text(
        f"✅ Owners destination saved.\n"
        f"Chat ID: {chat_id}\n"
        f"Total destinations: {len(current)}"
    )

# --- /setdaily ---
async def setdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return

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

    # Auto-broadcast to owners (if configured)
    owners = parse_chat_ids(get_setting("OWNERS_CHAT_IDS"))
    text = render_daily_report(today)
    if owners and text:
        for cid in owners:
            try:
                await context.bot.send_message(chat_id=cid, text=text)
            except Exception:
                # Don't crash if one destination is invalid
                pass

# --- /daily ---
async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return

    today = date.today()
    text = render_daily_report(today)

    if not text:
        await update.effective_message.reply_text("No data for today yet. Use: /setdaily 2450 118")
        return

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

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
