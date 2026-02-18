import os
import re
import json
import collections
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import psycopg


# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

TIMEZONE = (os.getenv("TIMEZONE", "Europe/Madrid") or "Europe/Madrid").strip()
CUTOFF_HOUR = int((os.getenv("CUTOFF_HOUR", "11") or "11").strip())

DEFAULT_WRITERS = "446855702"  # Gio
_raw_writers = (os.getenv("WRITER_USER_IDS", DEFAULT_WRITERS) or DEFAULT_WRITERS).strip()
WRITER_USER_IDS = {int(x.strip()) for x in _raw_writers.split(",") if x.strip().isdigit()}

# Weekly digest schedule
AUTO_WEEKLY_DIGEST = (os.getenv("AUTO_WEEKLY_DIGEST", "1").strip() == "1")
DIGEST_WEEKDAY = int(os.getenv("DIGEST_WEEKDAY", "0"))  # Monday=0
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "10"))
DIGEST_MINUTE = int(os.getenv("DIGEST_MINUTE", "0"))

# Alerts schedule (optional)
AUTO_ALERTS = (os.getenv("AUTO_ALERTS", "0").strip() == "1")
ALERTS_HOUR = int(os.getenv("ALERTS_HOUR", "11"))
ALERTS_MINUTE = int(os.getenv("ALERTS_MINUTE", "15"))
ALERTS_LOOKBACK_DAYS = int(os.getenv("ALERTS_LOOKBACK_DAYS", "7"))

# Translation settings (LibreTranslate)
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
    # Remove first line that contains /report...
    return "\n".join(lines[1:]).strip()


# =========================
# TRANSLATION (ES -> EN)
# =========================
_translate_cache: dict[str, str] = {}

def translate_es_to_en(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    if t in _translate_cache:
        return _translate_cache[t]
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
            translated = (out.get("translatedText") or "").strip() or t
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
    "the","for","but","not","too","about","when","what","which","would","could","should"
}
STOPWORDS_ES = {
    "esto","esta","este","estos","estas","para","pero","porque","como","cuando","donde","quien",
    "que","con","sin","muy","mas","menos","sobre","entre","hasta","desde","tambien","solo",
    "una","uno","unos","unas","del","de","la","el","los","las","y","o","en","por","se","su",
    "sus","al","un","lo","ya","le","les","me","mi","mis","tu","tus","nos","os","si","no",
    "hay","fue","eran","era","son","estaba","estaban","esta","estoy","estan",
    "mesa","mesas","cliente","clientes"
}
ALWAYS_IGNORE = {
    "incidents","incident","incidencias","incidencia",
    "staff","personal","problemas","problema",
    "sold","soldout","agotados","agotado","platos",
    "complaints","quejas","reclamaciones",
    "test","testing"
}

WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", re.UNICODE)

SECTION_HEADERS = {
    "incidents": ["incidents", "incidencias", "incidencia"],
    "staff": ["staff", "personal", "problemas de personal", "equipo"],
    "soldout": ["sold out", "soldout", "platos agotados", "agotados", "agotado"],
    "complaints": ["complaints", "quejas", "quejas de clientes", "reclamaciones"],
}

def tokenize(text: str) -> list[str]:
    text = (text or "").strip().lower()
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
    text = (report_text or "").strip()
    if not text:
        return {"incidents":"", "staff":"", "soldout":"", "complaints":""}

    variants = []
    for group in SECTION_HEADERS.values():
        variants.extend(group)
    variants = sorted(set(variants), key=lambda x: len(x), reverse=True)

    header_pattern = r"(?im)^\s*(" + "|".join(re.escape(v) for v in variants) + r")\s*:\s*$"
    lines = text.splitlines()
    indices = [i for i, line in enumerate(lines) if re.match(header_pattern, line.strip())]

    if not indices:
        return {"incidents": text, "staff":"", "soldout":"", "complaints":""}

    def canonical(h: str) -> str:
        h = h.strip().lower().rstrip(":").strip()
        for key, group in SECTION_HEADERS.items():
            if any(h == g.lower() for g in group):
                return key
        for key, group in SECTION_HEADERS.items():
            for g in group:
                if g.lower() in h:
                    return key
        return "incidents"

    sections = {"incidents":"", "staff":"", "soldout":"", "complaints":""}

    for idx_pos, start_i in enumerate(indices):
        header_line = lines[start_i].strip()
        key = canonical(header_line)
        end_i = indices[idx_pos + 1] if idx_pos + 1 < len(indices) else len(lines)
        body = "\n".join(lines[start_i + 1:end_i]).strip()
        if sections.get(key):
            sections[key] += "\n" + body
        else:
            sections[key] = body

    return sections

def count_phrases(tokens: list[str]) -> tuple[collections.Counter, collections.Counter]:
    uni = collections.Counter(tokens)
    bi = collections.Counter()
    for a, b in zip(tokens, tokens[1:]):
        if a == b:
            continue
        bi[f"{a} {b}"] += 1
    return bi, uni

def top_items(counter: collections.Counter, n: int = 6, min_count: int = 1) -> list[tuple[str,int]]:
    return [(k,v) for k,v in counter.most_common(n) if v >= min_count]

def build_section_counters(start: date, end: date):
    reports = get_reports_in_range(start, end)
    sec_bigram = {k: collections.Counter() for k in ("incidents","staff","soldout","complaints")}
    sec_unigram = {k: collections.Counter() for k in ("incidents","staff","soldout","complaints")}

    for _, text in reports:
        sections = extract_sections(text)
        for key, content in sections.items():
            toks = tokenize(content)
            bi, uni = count_phrases(toks)
            sec_bigram[key].update(bi)
            sec_unigram[key].update(uni)

    return reports, sec_bigram, sec_unigram

def pick_best(sec_bi: collections.Counter, sec_uni: collections.Counter, want: int = 7) -> list[tuple[str,int]]:
    out = []
    out.extend(top_items(sec_bi, n=want, min_count=1))
    if len(out) < 5:
        existing = set(k for k,_ in out)
        for k,v in sec_uni.most_common(30):
            if k in existing:
                continue
            out.append((k,v))
            if len(out) >= want:
                break
    return out[:want]

def format_top_bilingual(items: list[tuple[str,int]], max_lines: int = 7) -> str:
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


# =========================
# REPORT PENDING STATE (two-step report)
# =========================
# key: (chat_id, user_id) -> {"day": date, "ts": datetime_utc}
PENDING_REPORTS: dict[tuple[int, int], dict] = {}
PENDING_TTL_MINUTES = 30

def _pending_cleanup():
    now = datetime.utcnow()
    dead = []
    for k, v in PENDING_REPORTS.items():
        ts = v.get("ts")
        if not ts or (now - ts).total_seconds() > PENDING_TTL_MINUTES * 60:
            dead.append(k)
    for k in dead:
        PENDING_REPORTS.pop(k, None)

def _set_pending(chat_id: int, user_id: int, day: date):
    _pending_cleanup()
    PENDING_REPORTS[(chat_id, user_id)] = {"day": day, "ts": datetime.utcnow()}

def _pop_pending(chat_id: int, user_id: int) -> date | None:
    _pending_cleanup()
    v = PENDING_REPORTS.pop((chat_id, user_id), None)
    if not v:
        return None
    return v.get("day")


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
        "/report  (send /report, then send notes as next message)\n"
        "/report  YYYY-MM-DD  (optional forced date)\n"
        "/cancelreport\n"
        "/reportdaily\n"
        "/reportday YYYY-MM-DD\n\n"
        "Notes analytics:\n"
        "/noteslast 30 (or 6M / 1Y)\n"
        "/notestrends 30\n"
        "/alerts 7\n"
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
        "Notes (recommended):\n"
        "1) send /report\n"
        "2) paste full note template as the next message\n\n"
        "Force date:\n"
        "/report 2026-02-17\n\n"
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
            "Usage:\n/setdaily SALES COVERS\n/setdaily YYYY-MM-DD SALES COVERS"
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
        f"Saved ✅\nBusiness day: {day.isoformat()}\nSales: €{sales:.2f}\nCovers: {covers}"
    )

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

    try:
        day = parse_ymd(context.args[0])
        sales = float(context.args[1])
        covers = int(context.args[2])
    except Exception:
        await update.effective_message.reply_text("Usage: /edit YYYY-MM-DD SALES COVERS")
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

    await update.effective_message.reply_text("Edited ✅")

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    d = business_day_from_now()
    text = render_day_report(d)
    await update.effective_message.reply_text(text or f"No data for business day {d.isoformat()} yet.")

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
        await update.effective_message.reply_text("Usage: /range YYYY-MM-DD YYYY-MM-DD")
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
        f"🏆 Best Day\nDay: {d.isoformat()}\nSales: €{sales:.2f}\nCovers: {covers}\nAvg ticket: €{avg:.2f}"
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
        f"🧊 Worst Day\nDay: {d.isoformat()}\nSales: €{sales:.2f}\nCovers: {covers}\nAvg ticket: €{avg:.2f}"
    )


# =========================
# NOTES COMMANDS (FIXED)
# =========================
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not is_writer(update):
        await update.effective_message.reply_text("Not authorized to submit notes.")
        return

    forced_day = None
    if context.args:
        try:
            forced_day = parse_ymd(context.args[0])
        except Exception:
            forced_day = None

    day = forced_day if forced_day else business_day_from_now()

    # If notes are included in the same message: save immediately
    msg_text = (update.effective_message.text or "").strip()
    body = extract_report_body(msg_text)
    if body:
        save_daily_report(day, body)
        await update.effective_message.reply_text(f"Saved 📝 Notes for business day: {day.isoformat()}")
        return

    # Otherwise: arm pending mode and ask for next message
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    _set_pending(chat_id, user_id, day)

    await update.effective_message.reply_text(
        f"✅ Report mode ON.\n"
        f"Now send the notes as your NEXT message (or reply to this message).\n"
        f"Business day: {day.isoformat()}\n\n"
        f"To cancel: /cancelreport"
    )

async def cancelreport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    if not update.effective_chat or not update.effective_user:
        return
    _pop_pending(update.effective_chat.id, update.effective_user.id)
    await update.effective_message.reply_text("Cancelled ✅ Report mode OFF.")

async def reportdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    day = business_day_from_now()
    text = fetch_daily_report(day)
    await update.effective_message.reply_text(
        text or f"No notes saved for business day {day.isoformat()} yet.\nUse /report to submit notes."
    )

async def reportday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    try:
        day = parse_ymd(context.args[0])
    except Exception:
        await update.effective_message.reply_text("Usage: /reportday YYYY-MM-DD")
        return
    text = fetch_daily_report(day)
    await update.effective_message.reply_text(text or "No notes for that day.")

async def capture_pending_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the NEXT normal message after /report and saves it as notes."""
    if not await guard(update): return
    if not is_writer(update):
        return
    if not update.effective_chat or not update.effective_user:
        return

    # Ignore messages that start with a command
    txt = (update.effective_message.text or "").strip()
    if not txt or txt.startswith("/"):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # If the user replied to the bot's "Report mode ON" message, we also accept it,
    # but simplest is: if pending exists, always use it.
    day = _pop_pending(chat_id, user_id)
    if not day:
        return

    save_daily_report(day, txt)
    await update.effective_message.reply_text(f"Saved 📝 Notes for business day: {day.isoformat()}")


# =========================
# ANALYTICS COMMANDS
# =========================
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

    reports, sec_bi, sec_uni = build_section_counters(start, end)
    if not reports:
        await update.effective_message.reply_text("No reports yet for that period.")
        return

    inc = pick_best(sec_bi["incidents"], sec_uni["incidents"])
    stf = pick_best(sec_bi["staff"], sec_uni["staff"])
    sol = pick_best(sec_bi["soldout"], sec_uni["soldout"])
    cmp = pick_best(sec_bi["complaints"], sec_uni["complaints"])

    await update.effective_message.reply_text(
        f"📊 Norah Notes Trends (last {token.upper()})\n"
        f"Period: {start.isoformat()} → {end.isoformat()}\n\n"
        f"🛠 Incidents:\n{format_top_bilingual(inc)}\n\n"
        f"👥 Staff:\n{format_top_bilingual(stf)}\n\n"
        f"🍽 Sold out:\n{format_top_bilingual(sol)}\n\n"
        f"⚠️ Complaints:\n{format_top_bilingual(cmp)}"
    )

async def notestrends(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    n = int(context.args[0]) if context.args else 30
    if n < 3 or n > 365:
        await update.effective_message.reply_text("Usage: /notestrends 30 (3..365)")
        return

    end = business_day_from_now()
    last_start = end - timedelta(days=n - 1)
    prev_end = last_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=n - 1)

    last_reports, last_bi, last_uni = build_section_counters(last_start, end)
    prev_reports, prev_bi, prev_uni = build_section_counters(prev_start, prev_end)

    if not last_reports:
        await update.effective_message.reply_text("No reports yet for the latest period.")
        return

    def delta_list(section: str) -> list[tuple[str,int,int]]:
        A = last_bi[section] if sum(last_bi[section].values()) else last_uni[section]
        B = prev_bi[section] if sum(prev_bi[section].values()) else prev_uni[section]
        keys = set(list(A.keys())[:100] + list(B.keys())[:100])
        deltas = []
        for k in keys:
            a = A.get(k, 0)
            b = B.get(k, 0)
            if a == 0 and b == 0:
                continue
            deltas.append((k, a, b))
        deltas.sort(key=lambda x: (x[1] - x[2], x[1]), reverse=True)
        return deltas[:6]

    def format_delta(items):
        if not items:
            return "—"
        lines = []
        for k,a,b in items:
            diff = a - b
            if diff <= 0:
                continue
            en = translate_es_to_en(k)
            label = f"{k} → {en}" if en.strip().lower() != k.strip().lower() else k
            lines.append(f"• {label}: {a} (prev {b}, +{diff})")
        return "\n".join(lines) if lines else "—"

    out = (
        f"📈 Norah Notes Trend Change (last {n} vs previous {n})\n"
        f"Latest: {last_start.isoformat()} → {end.isoformat()}\n"
        f"Previous: {prev_start.isoformat()} → {prev_end.isoformat()}\n\n"
        f"⚠️ Complaints increasing:\n{format_delta(delta_list('complaints'))}\n\n"
        f"🍽 Sold-out increasing:\n{format_delta(delta_list('soldout'))}\n\n"
        f"👥 Staff issues increasing:\n{format_delta(delta_list('staff'))}\n\n"
        f"🛠 Incidents increasing:\n{format_delta(delta_list('incidents'))}"
    )
    await update.effective_message.reply_text(out)

async def alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    n = int(context.args[0]) if context.args else ALERTS_LOOKBACK_DAYS
    n = max(3, min(60, n))

    end = business_day_from_now()
    last_start = end - timedelta(days=n - 1)
    prev_end = last_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=n - 1)

    last_reports, last_bi, last_uni = build_section_counters(last_start, end)
    prev_reports, prev_bi, prev_uni = build_section_counters(prev_start, prev_end)

    if not last_reports:
        await update.effective_message.reply_text("No reports yet.")
        return

    def top_spikes(section: str) -> list[str]:
        A = last_bi[section] if sum(last_bi[section].values()) else last_uni[section]
        B = prev_bi[section] if sum(prev_bi[section].values()) else prev_uni[section]
        spikes = []
        for k,a in A.most_common(30):
            b = B.get(k, 0)
            if a >= 2 and a >= b + 2:
                en = translate_es_to_en(k)
                label = f"{k} → {en}" if en.strip().lower() != k.strip().lower() else k
                spikes.append(f"• {label}: {a} (prev {b})")
            if len(spikes) >= 5:
                break
        return spikes

    comp = top_spikes("complaints")
    sold = top_spikes("soldout")
    staff = top_spikes("staff")
    inc = top_spikes("incidents")

    msg = (
        f"🚨 Norah Alerts (last {n} days)\n"
        f"Period: {last_start.isoformat()} → {end.isoformat()}\n\n"
        f"⚠️ Complaints spikes:\n" + ("\n".join(comp) if comp else "—") + "\n\n"
        f"🍽 Sold-out spikes:\n" + ("\n".join(sold) if sold else "—") + "\n\n"
        f"👥 Staff spikes:\n" + ("\n".join(staff) if staff else "—") + "\n\n"
        f"🛠 Incidents spikes:\n" + ("\n".join(inc) if inc else "—")
    )
    await update.effective_message.reply_text(msg)

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

    matches = [d.isoformat() for d, text in rows if keyword in (text or "").lower()]
    await update.effective_message.reply_text(
        ("🔎 Found on:\n" + "\n".join(matches[:40])) if matches else "No matches found."
    )

async def soldout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    token = context.args[0] if context.args else "30"
    end = business_day_from_now()
    start = parse_period_to_start(token, end)
    if not start:
        await update.effective_message.reply_text("Usage: /soldout 30 (or 6M/1Y)")
        return
    reports, sec_bi, sec_uni = build_section_counters(start, end)
    if not reports:
        await update.effective_message.reply_text("No reports yet.")
        return
    top = pick_best(sec_bi["soldout"], sec_uni["soldout"], want=8)
    await update.effective_message.reply_text(
        f"🍽 Sold-out trends (last {token.upper()}):\n{format_top_bilingual(top, max_lines=8)}"
    )

async def complaints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    token = context.args[0] if context.args else "30"
    end = business_day_from_now()
    start = parse_period_to_start(token, end)
    if not start:
        await update.effective_message.reply_text("Usage: /complaints 30 (or 6M/1Y)")
        return
    reports, sec_bi, sec_uni = build_section_counters(start, end)
    if not reports:
        await update.effective_message.reply_text("No reports yet.")
        return
    top = pick_best(sec_bi["complaints"], sec_uni["complaints"], want=8)
    await update.effective_message.reply_text(
        f"⚠️ Complaint trends (last {token.upper()}):\n{format_top_bilingual(top, max_lines=8)}"
    )


# =========================
# AUTO MESSAGES (Weekly digest + optional alerts)
# =========================
async def send_to_owners(context: ContextTypes.DEFAULT_TYPE, text: str):
    owners = parse_chat_ids(get_setting("OWNERS_CHAT_IDS"))
    if not owners:
        return
    for cid in owners:
        try:
            await context.bot.send_message(chat_id=cid, text=text)
        except Exception:
            pass

async def weekly_digest_job(context: ContextTypes.DEFAULT_TYPE):
    end = business_day_from_now()
    start7 = end - timedelta(days=6)

    numeric = render_range_report("Weekly Digest — Numbers (last 7 days)", start7, end)

    reports, sec_bi, sec_uni = build_section_counters(start7, end)
    if reports:
        inc = pick_best(sec_bi["incidents"], sec_uni["incidents"])
        stf = pick_best(sec_bi["staff"], sec_uni["staff"])
        sol = pick_best(sec_bi["soldout"], sec_uni["soldout"])
        cmp = pick_best(sec_bi["complaints"], sec_uni["complaints"])

        notes = (
            f"📝 Weekly Digest — Notes (last 7 days)\n"
            f"Period: {start7.isoformat()} → {end.isoformat()}\n\n"
            f"🛠 Incidents:\n{format_top_bilingual(inc)}\n\n"
            f"👥 Staff:\n{format_top_bilingual(stf)}\n\n"
            f"🍽 Sold out:\n{format_top_bilingual(sol)}\n\n"
            f"⚠️ Complaints:\n{format_top_bilingual(cmp)}"
        )
    else:
        notes = "📝 Weekly Digest — Notes\nNo notes yet for last 7 days."

    await send_to_owners(context, "📬 NORAH — WEEKLY DIGEST\n\n" + numeric)
    await send_to_owners(context, notes)

async def daily_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    n = ALERTS_LOOKBACK_DAYS
    end = business_day_from_now()
    last_start = end - timedelta(days=n - 1)
    prev_end = last_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=n - 1)

    last_reports, last_bi, last_uni = build_section_counters(last_start, end)
    prev_reports, prev_bi, prev_uni = build_section_counters(prev_start, prev_end)

    if not last_reports:
        return

    def top_spikes(section: str) -> list[str]:
        A = last_bi[section] if sum(last_bi[section].values()) else last_uni[section]
        B = prev_bi[section] if sum(prev_bi[section].values()) else prev_uni[section]
        spikes = []
        for k,a in A.most_common(30):
            b = B.get(k, 0)
            if a >= 2 and a >= b + 2:
                en = translate_es_to_en(k)
                label = f"{k} → {en}" if en.strip().lower() != k.strip().lower() else k
                spikes.append(f"• {label}: {a} (prev {b})")
            if len(spikes) >= 4:
                break
        return spikes

    comp = top_spikes("complaints")
    sold = top_spikes("soldout")
    staff = top_spikes("staff")
    inc = top_spikes("incidents")

    msg = (
        f"🚨 NORAH — AUTO ALERTS (last {n} days)\n"
        f"Period: {last_start.isoformat()} → {end.isoformat()}\n\n"
        f"⚠️ Complaints spikes:\n" + ("\n".join(comp) if comp else "—") + "\n\n"
        f"🍽 Sold-out spikes:\n" + ("\n".join(sold) if sold else "—") + "\n\n"
        f"👥 Staff spikes:\n" + ("\n".join(staff) if staff else "—") + "\n\n"
        f"🛠 Incidents spikes:\n" + ("\n".join(inc) if inc else "—")
    )
    await send_to_owners(context, msg)


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
    app.add_handler(CommandHandler("notestrends", notestrends))
    app.add_handler(CommandHandler("alerts", alerts))
    app.add_handler(CommandHandler("findnote", findnote))
    app.add_handler(CommandHandler("soldout", soldout))
    app.add_handler(CommandHandler("complaints", complaints))

    # IMPORTANT: must be after /report command handler
    # Captures the next normal text message after /report
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), capture_pending_notes))

    # Scheduled jobs
    tz = ZoneInfo(TIMEZONE)

    if AUTO_WEEKLY_DIGEST:
        app.job_queue.run_daily(
            weekly_digest_job,
            time=dtime(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, tzinfo=tz),
            days=(DIGEST_WEEKDAY,),
            name="weekly_digest",
        )

    if AUTO_ALERTS:
        app.job_queue.run_daily(
            daily_alerts_job,
            time=dtime(hour=ALERTS_HOUR, minute=ALERTS_MINUTE, tzinfo=tz),
            days=(0, 1, 2, 3, 4, 5, 6),
            name="daily_alerts",
        )

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
