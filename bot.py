import os
import re
import json
import collections
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import psycopg


# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

TIMEZONE = (os.getenv("TIMEZONE", "Europe/Madrid") or "Europe/Madrid").strip()
CUTOFF_HOUR = int((os.getenv("CUTOFF_HOUR", "11") or "11").strip())

# Writers: who can set/edit numbers (and usually also submit notes)
DEFAULT_WRITERS = "446855702"  # your Telegram id (Gio) as fallback
_raw_writers = (os.getenv("WRITER_USER_IDS", DEFAULT_WRITERS) or DEFAULT_WRITERS).strip()
WRITER_USER_IDS = set()
for x in _raw_writers.split(","):
    x = x.strip()
    if x.isdigit():
        WRITER_USER_IDS.add(int(x))

# Optional translation settings (LibreTranslate)
TRANSLATE_URL = (os.getenv("TRANSLATE_URL", "https://libretranslate.de/translate") or "").strip()
TRANSLATE_FROM = (os.getenv("TRANSLATE_FROM", "es") or "es").strip()
TRANSLATE_TO = (os.getenv("TRANSLATE_TO", "en") or "en").strip()
TRANSLATE_API_KEY = (os.getenv("TRANSLATE_API_KEY", "") or "").strip()


# =========================
# SECURITY
# =========================
def is_writer(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in WRITER_USER_IDS)

async def guard(update: Update) -> bool:
    # Keep open for now; you can restrict later.
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_reports (
                    day DATE PRIMARY KEY,
                    report_text TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
        conn.commit()


# =========================
# SETTINGS (owners destinations)
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
    n = now_local()
    d = n.date()
    if n.hour < CUTOFF_HOUR:
        return d - timedelta(days=1)
    return d

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    if m == 12:
        next_month = date(y + 1, 1, 1)
    else:
        next_month = date(y, m + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(y, m, min(d.day, last_day))

def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + years)

def parse_period_to_start(token: str, end: date) -> date | None:
    s = token.strip().upper()
    m = re.fullmatch(r"(\d+)([MY])?", s)
    if not m:
        return None
    num = int(m.group(1))
    unit = m.group(2) or "D"
    if num <= 0 or num > 5000:
        return None
    if unit == "D":
        return end - timedelta(days=num - 1)
    if unit == "M":
        return add_months(end, -num)
    if unit == "Y":
        return add_years(end, -num)
    return None


# =========================
# NUMERIC REPORT HELPERS
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


# =========================
# QUALITATIVE REPORT HELPERS
# =========================
def save_daily_report(day: date, text: str) -> None:
    text = text.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daily_reports (day, report_text)
                VALUES (%s, %s)
                ON CONFLICT (day)
                DO UPDATE SET report_text = EXCLUDED.report_text,
                              updated_at = NOW();
            """, (day, text))
        conn.commit()

def fetch_daily_report(day: date) -> str | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT report_text FROM daily_reports WHERE day=%s;", (day,))
            row = cur.fetchone()
    return row[0] if row else None

def get_reports_in_range(start: date, end: date):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT day, report_text
                FROM daily_reports
                WHERE day BETWEEN %s AND %s
                ORDER BY day;
            """, (start, end))
            return cur.fetchall()

def extract_report_body(full_message: str) -> str:
    if not full_message:
        return ""
    lines = full_message.splitlines()
    if not lines:
        return ""
    return "\n".join(lines[1:]).strip()


# =========================
# TRANSLATION (ES -> EN)
# =========================
_translate_cache: dict[str, str] = {}

def translate_es_to_en(text: str) -> str:
    """Best-effort: returns English translation, or original text if translation fails."""
    t = text.strip()
    if not t:
        return t
    if t in _translate_cache:
        return _translate_cache[t]

    # If no URL configured, just return original.
    if not TRANSLATE_URL:
        _translate_cache[t] = t
        return t

    payload = {
        "q": t,
        "source": TRANSLATE_FROM,
        "target": TRANSLATE_TO,
        "format": "text",
    }
    if TRANSLATE_API_KEY:
        payload["api_key"] = TRANSLATE_API_KEY

    data = json.dumps(payload).encode("utf-8")
    req = Request(
        TRANSLATE_URL,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )

    try:
        with urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            out = json.loads(raw)
            translated = (out.get("translatedText") or "").strip()
            if not translated:
                translated = t
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        translated = t

    _translate_cache[t] = translated
    return translated


# =========================
# NLP / TREND ENGINE
# =========================
STOPWORDS_EN = {
    "this","that","with","from","your","have","just","very","into","over","after","before",
    "then","than","they","them","there","here","also","only","been","were","was","are","and",
    "the","for","but","not","too","about","when","what","which","would","could","should",
    "table","guest","guests","said","says","music","loud"  # we do NOT include these normally,
}
# NOTE: we won't hard-block "music/loud"; left here as example. We'll use a real list below.

STOPWORDS_ES = {
    "esto","esta","este","estos","estas","para","pero","porque","como","cuando","donde","quien",
    "que","con","sin","muy","mas","menos","sobre","entre","hasta","desde","tambien","solo",
    "una","uno","unos","unas","del","de","la","el","los","las","y","o","en","por","se","su",
    "sus","al","un","lo","ya","le","les","me","mi","mis","tu","tus","nos","os","si","no",
    "hay","fue","eran","era","son","estaba","estaban","esta","estoy","estan",
    "mesa","mesas","cliente","clientes"
}

# Words we ALWAYS ignore because they are template headers / noise
ALWAYS_IGNORE = {
    "incidents","incident","incidencias",
    "staff","personal","problemas","problema",
    "sold","soldout","agotados","agotado","platos",
    "complaints","quejas",
    "test","testing"
}

WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", re.UNICODE)

# Section header variants (ES + EN)
SECTION_HEADERS = {
    "incidents": ["incidents", "incidencias", "incidencia"],
    "staff": ["staff", "personal", "problemas de personal", "equipo"],
    "soldout": ["sold out", "soldout", "platos agotados", "agotados", "agotado"],
    "complaints": ["complaints", "quejas", "quejas de clientes", "reclamaciones"],
}

def normalize_text(s: str) -> str:
    return (s or "").strip()

def tokenize(text: str) -> list[str]:
    text = normalize_text(text).lower()
    words = WORD_RE.findall(text)
    out = []
    for w in words:
        w = w.lower()
        if len(w) <= 3:
            continue
        if w in ALWAYS_IGNORE:
            continue
        if w in STOPWORDS_EN or w in STOPWORDS_ES:
            continue
        out.append(w)
    return out

def extract_sections(report_text: str) -> dict[str, str]:
    """
    Extract sections from freeform report text.
    Works even if some headings are missing.
    """
    text = normalize_text(report_text)
    if not text:
        return {"incidents":"", "staff":"", "soldout":"", "complaints":""}

    # Build a header regex for all known variants
    # Example: (Incidents|Incidencias|Staff|Personal|Platos agotados|Quejas...)
    variants = []
    for group in SECTION_HEADERS.values():
        variants.extend(group)

    # Sort longer first to match "problemas de personal" before "personal"
    variants = sorted(set(variants), key=lambda x: len(x), reverse=True)
    header_pattern = r"(?im)^\s*(" + "|".join(re.escape(v) for v in variants) + r")\s*:\s*$"

    # Find headings and slice blocks
    lines = text.splitlines()
    indices = []
    for i, line in enumerate(lines):
        if re.match(header_pattern, line.strip()):
            indices.append(i)

    # If no headings, put everything into incidents (fallback)
    if not indices:
        return {"incidents": text, "staff":"", "soldout":"", "complaints":""}

    # Map heading -> canonical key
    def canonical(h: str) -> str:
        h = h.strip().lower()
        for key, group in SECTION_HEADERS.items():
            if any(h == g.lower() for g in group):
                return key
        # try contains match
        for key, group in SECTION_HEADERS.items():
            for g in group:
                if g.lower() in h:
                    return key
        return "incidents"

    sections = {"incidents":"", "staff":"", "soldout":"", "complaints":""}
    for idx_pos, start_i in enumerate(indices):
        header_line = lines[start_i].strip().rstrip(":").strip()
        key = canonical(header_line.replace(":", ""))

        end_i = indices[idx_pos + 1] if idx_pos + 1 < len(indices) else len(lines)
        body = "\n".join(lines[start_i + 1:end_i]).strip()
        # append if repeated headings
        if sections.get(key):
            sections[key] += "\n" + body
        else:
            sections[key] = body

    return sections

def count_phrases(tokens: list[str]) -> tuple[collections.Counter, collections.Counter]:
    """
    Returns (bigrams, unigrams)
    """
    uni = collections.Counter(tokens)
    bi = collections.Counter()
    for a, b in zip(tokens, tokens[1:]):
        if a == b:
            continue
        bi[f"{a} {b}"] += 1
    return bi, uni

def top_items(counter: collections.Counter, n: int = 5, min_count: int = 1) -> list[tuple[str,int]]:
    return [(k,v) for k,v in counter.most_common(n) if v >= min_count]


# =========================
# TELEGRAM COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text(
        "👋 Norah Ops is online.\n\n"
        "Numbers:\n"
        "/setdaily SALES COVERS\n"
        "/setdaily YYYY-MM-DD SALES COVERS\n"
        "/edit YYYY-MM-DD SALES COVERS\n"
        "/daily\n"
        "/month\n"
        "/last 7 | /last 6M | /last 1Y\n"
        "/range YYYY-MM-DD YYYY-MM-DD\n"
        "/bestday\n"
        "/worstday\n\n"
        "Notes:\n"
        "/report  (paste sections under command)\n"
        "/reportdaily\n"
        "/reportday YYYY-MM-DD\n\n"
        "Notes analytics:\n"
        "/noteslast 30  (or 6M / 1Y)\n"
        "/findnote keyword\n"
        "/soldout 30\n"
        "/complaints 30\n\n"
        "Setup:\n"
        "/setowners"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.effective_message.reply_text(
        "Examples:\n"
        "/setdaily 2450 118\n"
        "/setdaily 2026-02-16 2450 118\n"
        "/edit 2026-02-16 2450 118\n"
        "/last 6M\n"
        "/range 2026-03-15 2026-04-07\n"
        "/bestday\n"
        "/worstday\n\n"
        "Notes:\n"
        "/report\n"
        "Incidents:\n...\n\nStaff:\n...\n\nSold out:\n...\n\nComplaints:\n...\n\n"
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

    # Auto-broadcast numeric daily report to owners dashboards
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
        f"Edited ✅\nDay: {day.isoformat()}\nSales: €{sales:.2f}\nCovers: {covers}"
    )

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    d = business_day_from_now()
    text = render_day_report(d)
    if not text:
        await update.effective_message.reply_text(
            f"No data for business day {d.isoformat()} yet.\nUse: /setdaily SALES COVERS"
        )
        return
    await update.effective_message.reply_text(text)

async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    today = now_local().date()
    start = today.replace(day=1)
    await update.effective_message.reply_text(render_range_report("Norah Month-to-Date", start, today))

async def last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.effective_message.reply_text("Usage: /last 7  OR  /last 6M  OR  /last 1Y")
        return
    token = context.args[0]
    end = now_local().date()
    start = parse_period_to_start(token, end)
    if not start:
        await update.effective_message.reply_text("Usage: /last 7  OR  /last 6M  OR  /last 1Y")
        return
    await update.effective_message.reply_text(render_range_report(f"Norah Last {token.upper()}", start, end))

async def range_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        start = parse_ymd(context.args[0])
        end = parse_ymd(context.args[1])
        if end < start:
            raise ValueError
    except Exception:
        await update.effective_message.reply_text("Usage: /range YYYY-MM-DD YYYY-MM-DD\nExample: /range 2026-03-15 2026-04-07")
        return
    await update.effective_message.reply_text(render_range_report("Norah Custom Range", start, end))

async def bestday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    row = fetch_best_worst(best=True)
    if not row:
        await update.effective_message.reply_text("No data yet.")
        return
    d, sales, covers = row
    avg = (sales / covers) if covers else 0
    await update.effective_message.reply_text(
        f"🏆 Norah Best Day\n\nDay: {d.isoformat()}\nSales: €{sales:.2f}\nCovers: {covers}\nAvg ticket: €{avg:.2f}"
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
        f"🧊 Norah Worst Day\n\nDay: {d.isoformat()}\nSales: €{sales:.2f}\nCovers: {covers}\nAvg ticket: €{avg:.2f}"
    )


# =========================
# NOTES COMMANDS
# =========================
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return

    msg_text = update.effective_message.text or ""
    args = context.args

    forced_day = None
    if args:
        try:
            forced_day = parse_ymd(args[0])
        except Exception:
            forced_day = None

    day = forced_day if forced_day else business_day_from_now()
    body = extract_report_body(msg_text)

    if not body:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/report\n"
            "Incidents:\n...\n\n"
            "Staff:\n...\n\n"
            "Sold out:\n...\n\n"
            "Complaints:\n...\n\n"
            "Or force date:\n/report YYYY-MM-DD\n(then paste the text under the command line)"
        )
        return

    save_daily_report(day, body)

    now = now_local()
    calendar_today = now.date()
    if forced_day:
        note = "(date forced)"
    else:
        if day != calendar_today:
            note = f"(before {CUTOFF_HOUR:02d}:00 cutoff — recorded as previous business day)"
        else:
            note = "(recorded as today’s business day)"

    await update.effective_message.reply_text(
        f"Saved 📝\nBusiness day: {day.isoformat()} {note}\nNotes saved."
    )

async def reportdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    day = business_day_from_now()
    text = fetch_daily_report(day)
    if not text:
        await update.effective_message.reply_text(
            f"No notes saved for business day {day.isoformat()} yet.\nUse /report to submit notes."
        )
        return
    await update.effective_message.reply_text(f"📝 Norah Daily Notes ({day.isoformat()})\n\n{text.strip()}")

async def reportday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        day = parse_ymd(context.args[0])
    except Exception:
        await update.effective_message.reply_text("Usage: /reportday YYYY-MM-DD\nExample: /reportday 2026-02-17")
        return

    text = fetch_daily_report(day)
    if not text:
        await update.effective_message.reply_text(f"No notes saved for {day.isoformat()} yet.")
        return
    await update.effective_message.reply_text(f"📝 Norah Daily Notes ({day.isoformat()})\n\n{text.strip()}")


# =========================
# NOTES ANALYTICS (Bilingual)
# =========================
def format_top_bilingual(items: list[tuple[str,int]], max_lines: int = 5) -> str:
    if not items:
        return "—"
    lines = []
    for phrase, cnt in items[:max_lines]:
        en = translate_es_to_en(phrase)
        if en.strip().lower() == phrase.strip().lower():
            lines.append(f"• {phrase} ({cnt})")
        else:
            lines.append(f"• {phrase} → {en} ({cnt})")
    return "\n".join(lines)

async def noteslast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.effective_message.reply_text("Usage: /noteslast 30  (or 6M / 1Y)")
        return

    token = context.args[0]
    end = business_day_from_now()
    start = parse_period_to_start(token, end)
    if not start:
        await update.effective_message.reply_text("Usage: /noteslast 30  (or 6M / 1Y)")
        return

    reports = get_reports_in_range(start, end)
    if not reports:
        await update.effective_message.reply_text("No reports yet for that period.")
        return

    # Counters per section
    sec_bigram = {k: collections.Counter() for k in ("incidents","staff","soldout","complaints")}
    sec_unigram = {k: collections.Counter() for k in ("incidents","staff","soldout","complaints")}

    for _, text in reports:
        sections = extract_sections(text)
        for key, content in sections.items():
            toks = tokenize(content)
            bi, uni = count_phrases(toks)
            sec_bigram[key].update(bi)
            sec_unigram[key].update(uni)

    # Prefer bigrams, then fill with unigrams if needed
    def best(counter_bi: collections.Counter, counter_uni: collections.Counter) -> list[tuple[str,int]]:
        out = []
        out.extend(top_items(counter_bi, n=7, min_count=1))
        if len(out) < 5:
            # add unigrams not already in out
            existing = set(k for k,_ in out)
            for k,v in counter_uni.most_common(10):
                if k in existing:
                    continue
                out.append((k,v))
                if len(out) >= 7:
                    break
        return out[:7]

    inc = best(sec_bigram["incidents"], sec_unigram["incidents"])
    stf = best(sec_bigram["staff"], sec_unigram["staff"])
    sol = best(sec_bigram["soldout"], sec_unigram["soldout"])
    cmp = best(sec_bigram["complaints"], sec_unigram["complaints"])

    await update.effective_message.reply_text(
        f"📊 Norah Notes Trends (last {token.upper()})\n"
        f"Period: {start.isoformat()} → {end.isoformat()}\n\n"
        f"🛠 Incidents:\n{format_top_bilingual(inc)}\n\n"
        f"👥 Staff:\n{format_top_bilingual(stf)}\n\n"
        f"🍽 Sold out:\n{format_top_bilingual(sol)}\n\n"
        f"⚠️ Complaints:\n{format_top_bilingual(cmp)}"
    )

async def findnote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not context.args:
        await update.effective_message.reply_text("Usage: /findnote keyword")
        return
    keyword = context.args[0].lower()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT day, report_text
                FROM daily_reports
                ORDER BY day DESC
                LIMIT 365;
            """)
            rows = cur.fetchall()

    matches = []
    for d, text in rows:
        if keyword in (text or "").lower():
            matches.append(d.isoformat())

    if not matches:
        await update.effective_message.reply_text("No matches found.")
        return

    await update.effective_message.reply_text(
        f"🔎 Keyword '{keyword}' found on:\n" + "\n".join(matches[:40])
    )

async def soldout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    token = context.args[0] if context.args else "30"
    end = business_day_from_now()
    start = parse_period_to_start(token, end)
    if not start:
        await update.effective_message.reply_text("Usage: /soldout 30  (or 6M / 1Y)")
        return

    reports = get_reports_in_range(start, end)
    if not reports:
        await update.effective_message.reply_text("No reports yet for that period.")
        return

    counter_bi = collections.Counter()
    counter_uni = collections.Counter()

    for _, text in reports:
        sections = extract_sections(text)
        toks = tokenize(sections.get("soldout",""))
        bi, uni = count_phrases(toks)
        counter_bi.update(bi)
        counter_uni.update(uni)

    top = top_items(counter_bi, n=8, min_count=1)
    if len(top) < 5:
        existing = set(k for k,_ in top)
        for k,v in counter_uni.most_common(15):
            if k not in existing:
                top.append((k,v))
            if len(top) >= 8:
                break

    await update.effective_message.reply_text(
        f"🍽 Sold-out trends (last {token.upper()}):\n{format_top_bilingual(top, max_lines=8)}"
    )

async def complaints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    token = context.args[0] if context.args else "30"
    end = business_day_from_now()
    start = parse_period_to_start(token, end)
    if not start:
        await update.effective_message.reply_text("Usage: /complaints 30  (or 6M / 1Y)")
        return

    reports = get_reports_in_range(start, end)
    if not reports:
        await update.effective_message.reply_text("No reports yet for that period.")
        return

    counter_bi = collections.Counter()
    counter_uni = collections.Counter()

    for _, text in reports:
        sections = extract_sections(text)
        toks = tokenize(sections.get("complaints",""))
        bi, uni = count_phrases(toks)
        counter_bi.update(bi)
        counter_uni.update(uni)

    top = top_items(counter_bi, n=8, min_count=1)
    if len(top) < 5:
        existing = set(k for k,_ in top)
        for k,v in counter_uni.most_common(15):
            if k not in existing:
                top.append((k,v))
            if len(top) >= 8:
                break

    await update.effective_message.reply_text(
        f"⚠️ Complaint trends (last {token.upper()}):\n{format_top_bilingual(top, max_lines=8)}"
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
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("range", range_cmd))
    app.add_handler(CommandHandler("bestday", bestday))
    app.add_handler(CommandHandler("worstday", worstday))

    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("reportdaily", reportdaily))
    app.add_handler(CommandHandler("reportday", reportday))

    app.add_handler(CommandHandler("noteslast", noteslast))
    app.add_handler(CommandHandler("findnote", findnote))
    app.add_handler(CommandHandler("soldout", soldout))
    app.add_handler(CommandHandler("complaints", complaints))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
