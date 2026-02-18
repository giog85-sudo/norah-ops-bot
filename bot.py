import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo
from collections import Counter

import psycopg
from telegram import Update
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

TZ_NAME = os.getenv("TZ_NAME", "Europe/Madrid").strip() or "Europe/Madrid"
CUTOFF_HOUR = int(os.getenv("CUTOFF_HOUR", "11").strip() or "11")  # business day cutoff next day
WEEKLY_DIGEST_HOUR = int(os.getenv("WEEKLY_DIGEST_HOUR", "9").strip() or "9")  # Monday digest hour

# Allowed users
ALLOWED_USER_IDS = set()
_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
if _raw:
    for x in _raw.split(","):
        x = x.strip()
        if x.isdigit():
            ALLOWED_USER_IDS.add(int(x))

TZ = ZoneInfo(TZ_NAME)

# For report-mode capture:
# key = f"{chat_id}:{user_id}" -> True/False
REPORT_MODE_KEY = "report_mode_map"


# =========================
# SECURITY
# =========================
def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def guard(update: Update) -> bool:
    if not is_allowed(update):
        if update.message:
            await update.message.reply_text("Not authorized.")
        return False
    return True


# =========================
# DATABASE
# =========================
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL")
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Daily sales/covers
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_stats (
                    day DATE PRIMARY KEY,
                    sales DOUBLE PRECISION,
                    covers INTEGER,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )
            # Notes (we store entries, so multiple per day possible)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS notes_entries (
                    id SERIAL PRIMARY KEY,
                    day DATE NOT NULL,
                    chat_id BIGINT,
                    user_id BIGINT,
                    text TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_notes_entries_day ON notes_entries(day);")

            # Settings (owners chats, etc.)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
        conn.commit()


def set_setting(key: str, value: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
                """,
                (key, value),
            )
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key=%s;", (key,))
            row = cur.fetchone()
    return row[0] if row and row[0] is not None else default


def parse_chat_ids(s: str) -> list[int]:
    out: list[int] = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except:
            continue
    return out


def add_owner_chat(chat_id: int):
    current = parse_chat_ids(get_setting("OWNERS_CHAT_IDS", ""))
    if chat_id not in current:
        current.append(chat_id)
    set_setting("OWNERS_CHAT_IDS", ",".join(str(x) for x in current))


def owners_chat_ids() -> list[int]:
    return parse_chat_ids(get_setting("OWNERS_CHAT_IDS", ""))


# =========================
# DATE / PERIOD HELPERS
# =========================
def now_local() -> datetime:
    return datetime.now(TZ)


def business_day_for(ts: datetime) -> date:
    """
    Business day definition:
    - If local time is before CUTOFF_HOUR, business day is previous calendar day
    - Else business day is today
    """
    if ts.hour < CUTOFF_HOUR:
        return (ts.date() - timedelta(days=1))
    return ts.date()


def business_day_today() -> date:
    return business_day_for(now_local())


def parse_yyyy_mm_dd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def add_months(d: date, months: int) -> date:
    """
    Calendar-month subtraction/addition without external libs.
    """
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    # clamp day to last day of target month
    # find last day of month: go to first of next month minus 1 day
    if m == 12:
        next_first = date(y + 1, 1, 1)
    else:
        next_first = date(y, m + 1, 1)
    last_day = (next_first - timedelta(days=1)).day
    return date(y, m, min(d.day, last_day))


@dataclass
class Period:
    start: date
    end: date  # inclusive


def parse_period_arg(arg: str) -> int | tuple[str, int]:
    """
    Returns either:
      - int days
      - ("M", months) or ("Y", years)
    """
    a = (arg or "").strip().upper()
    if a.isdigit():
        return int(a)
    m = re.fullmatch(r"(\d+)([MY])", a)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        return (unit, n)
    raise ValueError("Invalid period")


def period_ending_today(arg: str) -> Period:
    end = business_day_today()
    spec = parse_period_arg(arg)
    if isinstance(spec, int):
        start = end - timedelta(days=spec - 1) if spec > 0 else end
        return Period(start=start, end=end)

    unit, n = spec
    if unit == "M":
        start = add_months(end, -n) + timedelta(days=1)
    else:  # "Y"
        start = date(end.year - n, end.month, end.day) + timedelta(days=1)
    return Period(start=start, end=end)


def daterange_days(p: Period) -> int:
    return (p.end - p.start).days + 1


# =========================
# TEXT CLEANING FOR NOTES ANALYTICS
# =========================
STOPWORDS = set(
    """
a an the and or but if then else for to of in on at by with without from as is are was were be been being
i you he she it we they me him her us them my your his their our this that these those
de la el los las y o pero si entonces para a en con sin por del al es son fue fueron ser estar
test
""".split()
)

def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    # keep letters, numbers; replace punctuation with space
    text = re.sub(r"[^a-z0-9áéíóúñüç]+", " ", text)
    words = [w.strip() for w in text.split() if w.strip()]
    return [w for w in words if w not in STOPWORDS and len(w) >= 3]


# =========================
# CORE QUERIES
# =========================
def upsert_daily(day_: date, sales: float, covers: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO daily_stats (day, sales, covers)
                VALUES (%s, %s, %s)
                ON CONFLICT (day)
                DO UPDATE SET sales = EXCLUDED.sales, covers = EXCLUDED.covers;
                """,
                (day_, sales, covers),
            )
        conn.commit()


def get_daily(day_: date):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sales, covers FROM daily_stats WHERE day=%s;", (day_,))
            row = cur.fetchone()
    return row


def sum_daily(p: Period):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(sales),0), COALESCE(SUM(covers),0), COUNT(*)
                FROM daily_stats
                WHERE day BETWEEN %s AND %s;
                """,
                (p.start, p.end),
            )
            row = cur.fetchone()
    total_sales, total_covers, days_with_data = row
    return float(total_sales), int(total_covers), int(days_with_data)


def best_or_worst_day(p: Period, worst: bool = False):
    order = "ASC" if worst else "DESC"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT day, sales, covers
                FROM daily_stats
                WHERE day BETWEEN %s AND %s AND sales IS NOT NULL
                ORDER BY sales {order}
                LIMIT 1;
                """,
                (p.start, p.end),
            )
            row = cur.fetchone()
    return row


def insert_note_entry(day_: date, chat_id: int, user_id: int, text: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notes_entries (day, chat_id, user_id, text)
                VALUES (%s, %s, %s, %s);
                """,
                (day_, chat_id, user_id, text),
            )
        conn.commit()


def notes_for_day(day_: date) -> list[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT text FROM notes_entries WHERE day=%s ORDER BY created_at ASC;",
                (day_,),
            )
            rows = cur.fetchall()
    return [r[0] for r in rows]


def notes_in_period(p: Period) -> list[tuple[date, str]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day, text
                FROM notes_entries
                WHERE day BETWEEN %s AND %s
                ORDER BY day ASC, created_at ASC;
                """,
                (p.start, p.end),
            )
            rows = cur.fetchall()
    return [(r[0], r[1]) for r in rows]


# =========================
# REPORT MODE STATE
# =========================
def _report_map(app: Application) -> dict[str, dict]:
    m = app.bot_data.get(REPORT_MODE_KEY)
    if not isinstance(m, dict):
        m = {}
        app.bot_data[REPORT_MODE_KEY] = m
    return m


def set_report_mode(app: Application, chat_id: int, user_id: int, day_: date | None):
    key = f"{chat_id}:{user_id}"
    _report_map(app)[key] = {
        "on": True,
        "day": day_.isoformat() if day_ else None,
        "ts": now_local().isoformat(),
    }


def clear_report_mode(app: Application, chat_id: int, user_id: int):
    key = f"{chat_id}:{user_id}"
    _report_map(app).pop(key, None)


def get_report_mode(app: Application, chat_id: int, user_id: int):
    key = f"{chat_id}:{user_id}"
    return _report_map(app).get(key)


# =========================
# COMMANDS
# =========================
HELP_TEXT = (
    "📌 Norah Ops commands\n\n"
    "Sales:\n"
    "/setdaily SALES COVERS  (uses business day)\n"
    "/edit YYYY-MM-DD SALES COVERS\n"
    "/daily\n"
    "/month\n"
    "/last 7 | /last 6M | /last 1Y\n"
    "/range YYYY-MM-DD YYYY-MM-DD\n"
    "/bestday\n"
    "/worstday\n\n"
    "Notes:\n"
    "/report  (then send notes as next message)\n"
    "/cancelreport\n"
    "/reportdaily\n"
    "/reportday YYYY-MM-DD\n\n"
    "Notes analytics:\n"
    "/noteslast 30 (or 6M / 1Y)\n"
    "/findnote keyword\n"
    "/soldout 30\n"
    "/complaints 30\n\n"
    "Setup:\n"
    "/setowners  (run in Owners chat once)\n"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text("👋 Norah Ops is online.\n\n" + HELP_TEXT)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text(HELP_TEXT)

async def setowners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    chat = update.effective_chat
    if not chat:
        return
    add_owner_chat(chat.id)
    await update.message.reply_text(f"✅ Owners chat registered: {chat.id}")

# --- SALES ---
async def setdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setdaily SALES COVERS\nExample: /setdaily 2450 118")
        return

    try:
        sales = float(context.args[0])
        covers = int(context.args[1])
    except:
        await update.message.reply_text("Usage: /setdaily SALES COVERS\nExample: /setdaily 2450 118")
        return

    day_ = business_day_today()
    upsert_daily(day_, sales, covers)
    await update.message.reply_text(f"Saved ✅  Day: {day_.isoformat()} | Sales: €{sales:.2f} | Covers: {covers}")

async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /edit YYYY-MM-DD SALES COVERS")
        return
    try:
        day_ = parse_yyyy_mm_dd(context.args[0])
        sales = float(context.args[1])
        covers = int(context.args[2])
    except:
        await update.message.reply_text("Usage: /edit YYYY-MM-DD SALES COVERS")
        return
    upsert_daily(day_, sales, covers)
    await update.message.reply_text(f"Edited ✅  Day: {day_.isoformat()} | Sales: €{sales:.2f} | Covers: {covers}")

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    day_ = business_day_today()
    row = get_daily(day_)
    if not row:
        await update.message.reply_text(f"No data for business day {day_.isoformat()} yet. Use: /setdaily 2450 118")
        return
    sales, covers = row
    sales = float(sales or 0)
    covers = int(covers or 0)
    avg = (sales / covers) if covers else 0.0
    await update.message.reply_text(
        f"📊 Norah Daily Report\n\n"
        f"Business day: {day_.isoformat()}\n"
        f"Sales: €{sales:.2f}\n"
        f"Covers: {covers}\n"
        f"Avg ticket: €{avg:.2f}"
    )

async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    end = business_day_today()
    start = date(end.year, end.month, 1)
    p = Period(start=start, end=end)
    total_sales, total_covers, days_with_data = sum_daily(p)
    avg_ticket = (total_sales / total_covers) if total_covers else 0.0
    await update.message.reply_text(
        f"📈 Norah Month-to-Date\n"
        f"Period: {p.start.isoformat()} → {p.end.isoformat()} ({daterange_days(p)} day(s))\n\n"
        f"Days with data: {days_with_data}\n"
        f"Total sales: €{total_sales:.2f}\n"
        f"Total covers: {total_covers}\n"
        f"Avg ticket: €{avg_ticket:.2f}"
    )

async def last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.message.reply_text("Usage: /last 7   OR   /last 6M   OR   /last 1Y")
        return
    try:
        p = period_ending_today(context.args[0])
    except:
        await update.message.reply_text("Usage: /last 7   OR   /last 6M   OR   /last 1Y")
        return
    total_sales, total_covers, days_with_data = sum_daily(p)
    avg_ticket = (total_sales / total_covers) if total_covers else 0.0
    await update.message.reply_text(
        f"📊 Norah Summary\n"
        f"Period: {p.start.isoformat()} → {p.end.isoformat()} ({daterange_days(p)} day(s))\n\n"
        f"Days with data: {days_with_data}\n"
        f"Total sales: €{total_sales:.2f}\n"
        f"Total covers: {total_covers}\n"
        f"Avg ticket: €{avg_ticket:.2f}"
    )

async def range_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /range YYYY-MM-DD YYYY-MM-DD")
        return
    try:
        start = parse_yyyy_mm_dd(context.args[0])
        end = parse_yyyy_mm_dd(context.args[1])
        if end < start:
            raise ValueError()
    except:
        await update.message.reply_text("Usage: /range YYYY-MM-DD YYYY-MM-DD")
        return
    p = Period(start=start, end=end)
    total_sales, total_covers, days_with_data = sum_daily(p)
    avg_ticket = (total_sales / total_covers) if total_covers else 0.0
    await update.message.reply_text(
        f"📊 Norah Range Report\n"
        f"Period: {p.start.isoformat()} → {p.end.isoformat()} ({daterange_days(p)} day(s))\n\n"
        f"Days with data: {days_with_data}\n"
        f"Total sales: €{total_sales:.2f}\n"
        f"Total covers: {total_covers}\n"
        f"Avg ticket: €{avg_ticket:.2f}"
    )

async def bestday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    # default: last 30 business days
    p = period_ending_today("30")
    row = best_or_worst_day(p, worst=False)
    if not row:
        await update.message.reply_text("No sales data found yet.")
        return
    d, sales, covers = row
    avg = (float(sales) / int(covers)) if covers else 0.0
    await update.message.reply_text(
        f"🏆 Best day (last 30)\n"
        f"Day: {d}\nSales: €{float(sales):.2f}\nCovers: {int(covers)}\nAvg ticket: €{avg:.2f}"
    )

async def worstday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    p = period_ending_today("30")
    row = best_or_worst_day(p, worst=True)
    if not row:
        await update.message.reply_text("No sales data found yet.")
        return
    d, sales, covers = row
    avg = (float(sales) / int(covers)) if covers else 0.0
    await update.message.reply_text(
        f"🧯 Worst day (last 30)\n"
        f"Day: {d}\nSales: €{float(sales):.2f}\nCovers: {int(covers)}\nAvg ticket: €{avg:.2f}"
    )

# --- NOTES ---
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    day_ = business_day_today()
    set_report_mode(context.application, chat.id, user.id, day_)
    await update.message.reply_text(
        f"✅ Report mode ON.\n"
        f"Now send the notes as your NEXT message (or reply to this message).\n"
        f"Business day: {day_.isoformat()}\n\n"
        f"To cancel: /cancelreport"
    )

async def cancelreport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    clear_report_mode(context.application, chat.id, user.id)
    await update.message.reply_text("❎ Report mode cancelled.")

async def reportdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    day_ = business_day_today()
    texts = notes_for_day(day_)
    if not texts:
        await update.message.reply_text(
            f"No notes saved for business day {day_.isoformat()} yet.\nUse /report to submit notes."
        )
        return
    joined = "\n\n— — —\n\n".join(texts)
    await update.message.reply_text(
        f"📝 Notes for business day {day_.isoformat()}:\n\n{joined}"
    )

async def reportday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.message.reply_text("Usage: /reportday YYYY-MM-DD")
        return
    try:
        day_ = parse_yyyy_mm_dd(context.args[0])
    except:
        await update.message.reply_text("Usage: /reportday YYYY-MM-DD")
        return
    texts = notes_for_day(day_)
    if not texts:
        await update.message.reply_text(f"No notes saved for {day_.isoformat()}.")
        return
    joined = "\n\n— — —\n\n".join(texts)
    await update.message.reply_text(f"📝 Notes for {day_.isoformat()}:\n\n{joined}")

# --- NOTES ANALYTICS ---
async def noteslast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.message.reply_text("Usage: /noteslast 30   (or 6M / 1Y)")
        return
    try:
        p = period_ending_today(context.args[0])
    except:
        await update.message.reply_text("Usage: /noteslast 30   (or 6M / 1Y)")
        return

    rows = notes_in_period(p)
    if not rows:
        await update.message.reply_text("No notes found for that period yet.")
        return

    counter = Counter()
    for _, txt in rows:
        counter.update(tokenize(txt))

    top = counter.most_common(12)
    lines = [f"{w}: {c}" for w, c in top] if top else ["(no keywords yet)"]

    await update.message.reply_text(
        "📊 Notes trends:\n" + "\n".join(lines)
    )

async def findnote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.message.reply_text("Usage: /findnote keyword")
        return
    keyword = " ".join(context.args).strip().lower()
    if not keyword:
        await update.message.reply_text("Usage: /findnote keyword")
        return

    # Search last 365 days by default
    p = period_ending_today("1Y")
    rows = notes_in_period(p)
    matches: list[date] = []
    for d, txt in rows:
        if keyword in (txt or "").lower():
            matches.append(d)

    if not matches:
        await update.message.reply_text(f"No notes found containing: {keyword}")
        return

    uniq = []
    for d in matches:
        if d not in uniq:
            uniq.append(d)

    show = uniq[-10:]  # last 10 match dates
    await update.message.reply_text(
        f"🔎 Matches for '{keyword}':\n" + "\n".join(d.isoformat() for d in show)
    )

async def soldout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.message.reply_text("Usage: /soldout 30")
        return
    try:
        p = period_ending_today(context.args[0])
    except:
        await update.message.reply_text("Usage: /soldout 30")
        return

    rows = notes_in_period(p)
    if not rows:
        await update.message.reply_text("No notes found for that period yet.")
        return

    counter = Counter()
    for _, txt in rows:
        t = (txt or "").lower()
        if "sold out" in t or "agotad" in t:
            counter.update(tokenize(txt))

    top = counter.most_common(12)
    if not top:
        await update.message.reply_text("No 'sold out' items detected yet for that period.")
        return

    await update.message.reply_text(
        "🍽️ Sold-out signals:\n" + "\n".join(f"{w}: {c}" for w, c in top)
    )

async def complaints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.message.reply_text("Usage: /complaints 30")
        return
    try:
        p = period_ending_today(context.args[0])
    except:
        await update.message.reply_text("Usage: /complaints 30")
        return

    rows = notes_in_period(p)
    if not rows:
        await update.message.reply_text("No notes found for that period yet.")
        return

    counter = Counter()
    for _, txt in rows:
        t = (txt or "").lower()
        if "complaint" in t or "queja" in t:
            counter.update(tokenize(txt))

    top = counter.most_common(12)
    if not top:
        await update.message.reply_text("No complaint keywords detected yet for that period.")
        return

    await update.message.reply_text(
        "⚠️ Complaint signals:\n" + "\n".join(f"{w}: {c}" for w, c in top)
    )

# =========================
# TEXT HANDLER (THIS FIXES YOUR PROBLEM)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    This is the missing piece: after /report, ANY next text message from that user in that chat
    will be saved as notes (no need to reply).
    """
    if not update.message:
        return
    if not await guard(update):
        return

    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    msg_text = (update.message.text or "").strip()
    if not msg_text:
        return

    # If user is in report mode, consume this message as notes
    rm = get_report_mode(context.application, chat.id, user.id)
    if rm and rm.get("on"):
        # Determine business day saved (stored when /report was issued)
        day_str = rm.get("day")
        day_ = parse_yyyy_mm_dd(day_str) if day_str else business_day_today()

        insert_note_entry(day_, chat.id, user.id, msg_text)
        clear_report_mode(context.application, chat.id, user.id)

        await update.message.reply_text(f"Saved 📝 Notes for business day {day_.isoformat()}.")
        return

    # Otherwise: ignore normal chatter (owners chat may be read-only anyway)
    # You can optionally respond with help, but better to stay silent in groups.


# =========================
# WEEKLY DIGEST
# =========================
async def send_weekly_digest(context: ContextTypes.DEFAULT_TYPE):
    chats = owners_chat_ids()
    if not chats:
        return  # owners chat not registered yet

    # last 7 business days ending today
    p7 = period_ending_today("7")
    total_sales_7, total_covers_7, days_data_7 = sum_daily(p7)
    avg_ticket_7 = (total_sales_7 / total_covers_7) if total_covers_7 else 0.0

    # previous 7 for delta
    prev_end = p7.start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)
    pprev = Period(prev_start, prev_end)
    total_sales_prev, total_covers_prev, _ = sum_daily(pprev)
    avg_ticket_prev = (total_sales_prev / total_covers_prev) if total_covers_prev else 0.0

    def pct(a, b):
        if b == 0:
            return None
        return (a - b) / b * 100.0

    sales_delta = pct(total_sales_7, total_sales_prev)
    ticket_delta = pct(avg_ticket_7, avg_ticket_prev)

    # best/worst in last 7
    best = best_or_worst_day(p7, worst=False)
    worst = best_or_worst_day(p7, worst=True)

    # notes trends last 7
    rows = notes_in_period(p7)
    counter = Counter()
    for _, txt in rows:
        counter.update(tokenize(txt))
    top_words = counter.most_common(8)
    top_words_str = ", ".join(f"{w}({c})" for w, c in top_words) if top_words else "—"

    # Basic alerts
    alerts = []
    if sales_delta is not None and sales_delta <= -10:
        alerts.append(f"Sales down {sales_delta:.0f}% vs previous 7 days")
    if ticket_delta is not None and ticket_delta <= -10:
        alerts.append(f"Avg ticket down {ticket_delta:.0f}% vs previous 7 days")
    if any("music" in (t or "").lower() or "ruido" in (t or "").lower() for _, t in rows):
        alerts.append("Noise/music mentioned in notes")

    alerts_str = "• " + "\n• ".join(alerts) if alerts else "None ✅"

    best_str = f"{best[0]} €{float(best[1]):.0f}" if best else "—"
    worst_str = f"{worst[0]} €{float(worst[1]):.0f}" if worst else "—"

    msg = (
        f"🗓️ Norah Weekly Digest\n"
        f"Period: {p7.start.isoformat()} → {p7.end.isoformat()}\n\n"
        f"Sales: €{total_sales_7:.2f}\n"
        f"Covers: {total_covers_7}\n"
        f"Avg ticket: €{avg_ticket_7:.2f}\n"
    )
    if sales_delta is not None:
        msg += f"Sales vs prev 7d: {sales_delta:+.0f}%\n"
    if ticket_delta is not None:
        msg += f"Avg ticket vs prev 7d: {ticket_delta:+.0f}%\n"

    msg += (
        f"\nBest day: {best_str}\n"
        f"Worst day: {worst_str}\n"
        f"\nTop note keywords: {top_words_str}\n"
        f"\nAlerts:\n{alerts_str}"
    )

    for chat_id in chats:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            # don't crash digest if one chat fails
            print(f"Weekly digest send failed for chat {chat_id}: {e}")


# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CommandHandler("setowners", setowners))

    app.add_handler(CommandHandler("setdaily", setdaily))
    app.add_handler(CommandHandler("edit", edit))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("range", range_cmd))
    app.add_handler(CommandHandler("bestday", bestday))
    app.add_handler(CommandHandler("worstday", worstday))

    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("cancelreport", cancelreport))
    app.add_handler(CommandHandler("reportdaily", reportdaily))
    app.add_handler(CommandHandler("reportday", reportday))

    app.add_handler(CommandHandler("noteslast", noteslast))
    app.add_handler(CommandHandler("findnote", findnote))
    app.add_handler(CommandHandler("soldout", soldout))
    app.add_handler(CommandHandler("complaints", complaints))

    # Text handler (THIS is what fixes your report capture)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Weekly digest: Monday at WEEKLY_DIGEST_HOUR (Madrid time)
    if app.job_queue is not None:
        app.job_queue.run_daily(
            send_weekly_digest,
            time=time(hour=WEEKLY_DIGEST_HOUR, minute=0, tzinfo=TZ),
            days=(0,),  # Monday
            name="weekly_digest_monday",
        )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
