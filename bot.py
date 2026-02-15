import os
from datetime import date
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import psycopg2

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL")

# --- Database connection ---
def get_conn():
    return psycopg2.connect(DATABASE_URL)

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

# --- Create table if not exists ---
def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_stats (
        day DATE PRIMARY KEY,
        sales REAL,
        covers INTEGER
    );
    """)
    conn.commit()
    cur.close()
    conn.close()

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
        "/setdaily 2500 120  → saves today's numbers\n"
        "/daily → show today's report"
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

    conn = get_conn()
    cur = conn.cursor()

    today = date.today()

    cur.execute("""
        INSERT INTO daily_stats (day, sales, covers)
        VALUES (%s, %s, %s)
        ON CONFLICT (day)
        DO UPDATE SET sales = EXCLUDED.sales, covers = EXCLUDED.covers;
    """, (today, sales, covers))

    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(f"Saved: €{sales} | Covers: {covers}")

# --- /daily ---
async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return

    conn = get_conn()
    cur = conn.cursor()

    today = date.today()

    cur.execute("SELECT sales, covers FROM daily_stats WHERE day=%s;", (today,))
    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        await update.message.reply_text("No data for today yet.")
        return

    sales, covers = row
    avg = sales / covers if covers else 0

    await update.message.reply_text(
        f"📊 Norah Daily Report\n\n"
        f"Sales: €{sales:.2f}\n"
        f"Covers: {covers}\n"
        f"Avg ticket: €{avg:.2f}"
    )

# --- Main ---
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setdaily", setdaily))
    app.add_handler(CommandHandler("daily", daily))

    print("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
