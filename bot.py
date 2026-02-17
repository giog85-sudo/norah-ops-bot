import os
import re
import collections
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import psycopg

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TIMEZONE = os.getenv("TIMEZONE", "Europe/Madrid")
CUTOFF_HOUR = int(os.getenv("CUTOFF_HOUR", "11"))

WRITER_USER_IDS = set()
_raw = os.getenv("WRITER_USER_IDS", "446855702")
for x in _raw.split(","):
    if x.strip().isdigit():
        WRITER_USER_IDS.add(int(x.strip()))

# =========================
# SECURITY
# =========================
def is_writer(update: Update):
    return update.effective_user and update.effective_user.id in WRITER_USER_IDS

async def guard(update: Update):
    return True

# =========================
# DB
# =========================
def get_conn():
    return psycopg.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats(
                day DATE PRIMARY KEY,
                sales DOUBLE PRECISION,
                covers INTEGER
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_reports(
                day DATE PRIMARY KEY,
                report_text TEXT
            );
            """)
        conn.commit()

# =========================
# TIME
# =========================
def now_local():
    return datetime.now(ZoneInfo(TIMEZONE))

def business_day():
    n = now_local()
    if n.hour < CUTOFF_HOUR:
        return (n.date() - timedelta(days=1))
    return n.date()

# =========================
# DAILY SALES
# =========================
async def setdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_writer(update):
        await update.message.reply_text("Not authorized")
        return

    try:
        if len(context.args) == 2:
            day = business_day()
            sales = float(context.args[0])
            covers = int(context.args[1])
        else:
            day = datetime.strptime(context.args[0], "%Y-%m-%d").date()
            sales = float(context.args[1])
            covers = int(context.args[2])
    except:
        await update.message.reply_text("Usage: /setdaily SALES COVERS or /setdaily YYYY-MM-DD SALES COVERS")
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO daily_stats(day,sales,covers)
            VALUES(%s,%s,%s)
            ON CONFLICT(day) DO UPDATE SET sales=EXCLUDED.sales,covers=EXCLUDED.covers;
            """,(day,sales,covers))
        conn.commit()

    await update.message.reply_text(f"Saved ✅ Business day: {day}\nSales €{sales}\nCovers {covers}")

async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_writer(update):
        return
    day = datetime.strptime(context.args[0], "%Y-%m-%d").date()
    sales = float(context.args[1])
    covers = int(context.args[2])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO daily_stats(day,sales,covers)
            VALUES(%s,%s,%s)
            ON CONFLICT(day) DO UPDATE SET sales=EXCLUDED.sales,covers=EXCLUDED.covers;
            """,(day,sales,covers))
        conn.commit()
    await update.message.reply_text("Edited")

# =========================
# REPORT STORAGE
# =========================
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.split("\n",1)
    if len(text)<2:
        await update.message.reply_text("Write notes under /report")
        return
    notes = text[1]
    day = business_day()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO daily_reports(day,report_text)
            VALUES(%s,%s)
            ON CONFLICT(day) DO UPDATE SET report_text=EXCLUDED.report_text;
            """,(day,notes))
        conn.commit()

    await update.message.reply_text(f"Notes saved for {day}")

async def reportday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day=datetime.strptime(context.args[0],"%Y-%m-%d").date()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT report_text FROM daily_reports WHERE day=%s",(day,))
            r=cur.fetchone()
    if not r:
        await update.message.reply_text("No report")
    else:
        await update.message.reply_text(r[0])

async def reportdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    day=business_day()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT report_text FROM daily_reports WHERE day=%s",(day,))
            r=cur.fetchone()
    if not r:
        await update.message.reply_text("No report")
    else:
        await update.message.reply_text(r[0])

# =========================
# TREND ANALYSIS
# =========================
def tokenize(text):
    words=re.findall(r"[a-zA-Z]+",text.lower())
    return [w for w in words if len(w)>3]

async def noteslast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days=int(context.args[0]) if context.args else 30
    start=business_day()-timedelta(days=days)
    end=business_day()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT report_text FROM daily_reports WHERE day BETWEEN %s AND %s",(start,end))
            rows=cur.fetchall()

    counter=collections.Counter()
    for r in rows:
        counter.update(tokenize(r[0]))

    if not counter:
        await update.message.reply_text("No reports yet")
        return

    msg="\n".join([f"{w}: {c}" for w,c in counter.most_common(10)])
    await update.message.reply_text("📊 Notes trends:\n"+msg)

async def findnote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword=context.args[0].lower()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT day,report_text FROM daily_reports ORDER BY day DESC LIMIT 120")
            rows=cur.fetchall()

    matches=[str(d) for d,t in rows if keyword in t.lower()]
    await update.message.reply_text("\n".join(matches) if matches else "No matches")

async def soldout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT report_text FROM daily_reports")
            rows=cur.fetchall()

    counter=collections.Counter()
    for r in rows:
        m=re.search(r"Sold out:(.*)",r[0],re.I)
        if m:
            counter.update(tokenize(m.group(1)))

    await update.message.reply_text("\n".join([f"{w}: {c}" for w,c in counter.most_common(10)]) or "No data")

async def complaints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT report_text FROM daily_reports")
            rows=cur.fetchall()

    counter=collections.Counter()
    for r in rows:
        m=re.search(r"Complaints:(.*)",r[0],re.I)
        if m:
            counter.update(tokenize(m.group(1)))

    await update.message.reply_text("\n".join([f"{w}: {c}" for w,c in counter.most_common(10)]) or "No data")

# =========================
# MAIN
# =========================
def main():
    init_db()
    app=Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("setdaily",setdaily))
    app.add_handler(CommandHandler("edit",edit))
    app.add_handler(CommandHandler("report",report))
    app.add_handler(CommandHandler("reportday",reportday))
    app.add_handler(CommandHandler("reportdaily",reportdaily))
    app.add_handler(CommandHandler("noteslast",noteslast))
    app.add_handler(CommandHandler("findnote",findnote))
    app.add_handler(CommandHandler("soldout",soldout))
    app.add_handler(CommandHandler("complaints",complaints))

    app.run_polling()

if __name__=="__main__":
    main()
