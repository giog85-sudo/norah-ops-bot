import os
from datetime import date
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import psycopg

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# --- Security (allowed users) ---
ALLOWED_USER_IDS = set()
_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
if _raw:
    for x in _raw.split(","):
        x = x.strip()
        if x.isdigit():
            ALLOWED_USER_IDS.add(int(x))

def is_allowed(update: Update):
    if not ALLOWED_USER_IDS:
        return True
    return update.effective_user and update.effective_user.id in ALLOWED_USER_IDS

async def guard(update: Update):
    if not is_allowed(update):
        await update.message.reply_text("Not authorized.")
        return False
    return True

# --- Database connection ---
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    return psycopg.connect(DATABASE_URL)

# --- Create table if not exists ---
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
        conn.commit()

# --- /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text(
        "👋 Norah Ops is online.\n\n"
        "Commands:\n"
        "/setdaily SALES COVERS\n"
        "/daily\n"
        "/help"
    )

# --- /help ---
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text(
        "Usage:\n"
        "/setdaily 2450 118\n"
        "/daily"
    )

# --- /setdaily ---
async def setdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return

    try:
        sales = float(context.args[0])
        covers = int(context.args[1])
    except:
        await update.message.reply_text("Usage: /setdaily SALES COVERS\nExample: /setdaily 2450 118")
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

    await update.message.reply_text(f"Saved ✅  Sales: €{sales} | Covers: {covers}")

# --- /daily ---
async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return

    today = date.today()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sales, covers FROM daily_stats WHERE day=%s;", (today,))
            row = cur.fetchone()

    if not row:
        await update.message.reply_text("No data for today yet. Use: /setdaily 2450 118")
        return

    sales, covers = row
    avg = (sales / covers) if covers else 0

    await update.message.reply_text(
        f"📊 Norah Daily Report\n\n"
        f"Sales: €{sales:.2f}\n"
        f"Covers: {covers}\n"
        f"Avg ticket: €{avg:.2f}"
    )

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setdaily", setdaily))
    app.add_handler(CommandHandler("daily", daily))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
