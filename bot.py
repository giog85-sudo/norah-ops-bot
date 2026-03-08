import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo
from collections import Counter

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

ACCESS_MODE = (os.getenv("ACCESS_MODE", "RESTRICTED").strip().upper() or "RESTRICTED")
ACCESS_MODE = "OPEN" if ACCESS_MODE == "OPEN" else "RESTRICTED"

ALLOWED_USER_IDS = set()
_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
if _raw:
    for x in _raw.split(","):
        x = x.strip()
        if x.isdigit():
            ALLOWED_USER_IDS.add(int(x))

TZ = ZoneInfo(TZ_NAME)

REPORT_MODE_KEY = "report_mode_map"
FULL_MODE_KEY = "full_mode_map"
GUIDED_FULL_KEY = "guided_full_map"

# =========================
# CHAT ROLES
# =========================
ROLE_OPS_ADMIN = "OPS_ADMIN"
ROLE_OWNERS_SILENT = "OWNERS_SILENT"
ROLE_MANAGER_INPUT = "MANAGER_INPUT"
ROLE_OWNERS_REQUESTS = "OWNERS_REQUESTS"
VALID_CHAT_ROLES = {ROLE_OPS_ADMIN, ROLE_OWNERS_SILENT, ROLE_MANAGER_INPUT, ROLE_OWNERS_REQUESTS}

# =========================
# SECURITY / AUTH
# =========================
def user_id(update: Update) -> int | None:
    u = update.effective_user
    return u.id if u else None

def chat_type(update: Update) -> str | None:
    c = update.effective_chat
    return c.type if c else None

def is_admin(update: Update) -> bool:
    if ACCESS_MODE == "OPEN":
        return True
    if not ALLOWED_USER_IDS:
        return True
    uid = user_id(update)
    return bool(uid and uid in ALLOWED_USER_IDS)

async def guard_admin(update: Update, *, reply_in_private_only: bool = True) -> bool:
    if is_admin(update):
        return True
    ctype = chat_type(update)
    if reply_in_private_only and ctype in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False
    if update.message:
        await update.message.reply_text("Not authorized.")
    return False

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

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS full_daily_stats (
                    day DATE PRIMARY KEY,
                    total_sales DOUBLE PRECISION,
                    visa DOUBLE PRECISION,
                    cash DOUBLE PRECISION,
                    tips DOUBLE PRECISION,

                    lunch_sales DOUBLE PRECISION,
                    lunch_pax INTEGER,
                    lunch_walkins INTEGER,
                    lunch_noshows INTEGER,

                    dinner_sales DOUBLE PRECISION,
                    dinner_pax INTEGER,
                    dinner_walkins INTEGER,
                    dinner_noshows INTEGER,

                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_full_daily_stats_day ON full_daily_stats(day);")

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

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_roles (
                    chat_id BIGINT PRIMARY KEY,
                    role TEXT NOT NULL,
                    chat_type TEXT,
                    title TEXT,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_roles_role ON chat_roles(role);")
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

def owners_chat_ids_legacy() -> list[int]:
    return parse_chat_ids(get_setting("OWNERS_CHAT_IDS", ""))

def set_owners_chat_ids_legacy(ids: list[int]):
    seen = set()
    uniq = []
    for x in ids:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    set_setting("OWNERS_CHAT_IDS", ",".join(str(x) for x in uniq))

def add_owner_chat_legacy(chat_id: int):
    current = owners_chat_ids_legacy()
    if chat_id not in current:
        current.append(chat_id)
    set_owners_chat_ids_legacy(current)

def remove_owner_chat_legacy(chat_id: int):
    current = [x for x in owners_chat_ids_legacy() if x != chat_id]
    set_owners_chat_ids_legacy(current)

def set_chat_role(chat_id: int, role: str, *, ctype: str | None = None, title: str | None = None):
    role = (role or "").strip().upper()
    if role not in VALID_CHAT_ROLES:
        raise ValueError("Invalid chat role")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO chat_roles (chat_id, role, chat_type, title, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (chat_id) DO UPDATE
                SET role = EXCLUDED.role,
                    chat_type = EXCLUDED.chat_type,
                    title = EXCLUDED.title,
                    updated_at = NOW();
                """,
                (chat_id, role, ctype, title),
            )
        conn.commit()

def get_chat_role(chat_id: int) -> str | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM chat_roles WHERE chat_id=%s;", (chat_id,))
            row = cur.fetchone()
    return row[0] if row else None

def chats_with_role(role: str) -> list[int]:
    role = (role or "").strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id FROM chat_roles WHERE role=%s ORDER BY chat_id;", (role,))
            rows = cur.fetchall()
    return [int(r[0]) for r in rows] if rows else []

def owners_silent_chat_ids() -> list[int]:
    ids = chats_with_role(ROLE_OWNERS_SILENT)
    return ids if ids else owners_chat_ids_legacy()

def list_all_chats() -> list[tuple[int, str, str | None, str | None]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, role, chat_type, title FROM chat_roles ORDER BY role, chat_id;")
            rows = cur.fetchall()
    return [(int(r[0]), r[1], r[2], r[3]) for r in rows] if rows else []

# =========================
# DATE / PERIOD HELPERS
# =========================
def now_local() -> datetime:
    return datetime.now(TZ)

def business_day_for(ts: datetime) -> date:
    if ts.hour < CUTOFF_HOUR:
        return (ts.date() - timedelta(days=1))
    return ts.date()

def business_day_today() -> date:
    return business_day_for(now_local())

def previous_business_day(ts: datetime | None = None) -> date:
    ts = ts or now_local()
    return business_day_for(ts) - timedelta(days=1)

def normalize_date_separators(s: str) -> str:
    # Convert common Unicode dashes to ASCII hyphen-minus
    return (s or "").strip().replace("–", "-").replace("—", "-").replace("−", "-")

def parse_yyyy_mm_dd(s: str) -> date:
    s = normalize_date_separators(s)
    return datetime.strptime(s, "%Y-%m-%d").date()

def parse_dd_mm_yyyy(s: str) -> date:
    s = normalize_date_separators(s)
    return datetime.strptime(s, "%d/%m/%Y").date()

def parse_any_date(s: str) -> date:
    s = normalize_date_separators(s)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return parse_yyyy_mm_dd(s)
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", s):
        return parse_dd_mm_yyyy(s)
    raise ValueError("Invalid date format")

def add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    if m == 12:
        next_first = date(y + 1, 1, 1)
    else:
        next_first = date(y, m + 1, 1)
    last_day = (next_first - timedelta(days=1)).day
    return date(y, m, min(d.day, last_day))

@dataclass
class Period:
    start: date
    end: date

def parse_period_arg(arg: str) -> int | tuple[str, int]:
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
    else:
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
    text = re.sub(r"[^a-z0-9áéíóúñüç]+", " ", text)
    words = [w.strip() for w in text.split() if w.strip()]
    return [w for w in words if w not in STOPWORDS and len(w) >= 3]

# =========================
# NOTE TAG SYSTEM
# =========================
# Supported tags and their aliases (all matched case-insensitively)
NOTE_TAGS = {
    "SOLD OUT":    ["[sold out]", "[soldout]", "[agotado]", "[sin existencias]"],
    "COMPLAINT":   ["[complaint]", "[complaints]", "[queja]", "[quejas]", "[reclamacion]"],
    "STAFF":       ["[staff]", "[personal]", "[equipo]"],
    "MAINTENANCE": ["[maintenance]", "[mantenimiento]", "[technical]", "[tecnico]"],
    "INCIDENT":    ["[incident]", "[incidente]", "[problema]"],
}

TAG_EMOJIS = {
    "SOLD OUT":    "🍽️",
    "COMPLAINT":   "⚠️",
    "STAFF":       "👥",
    "MAINTENANCE": "🔧",
    "INCIDENT":    "🚨",
}

def extract_note_tags(text: str) -> list[str]:
    """Return list of canonical tag names found in the note text."""
    tl = (text or "").lower()
    found = []
    for canonical, aliases in NOTE_TAGS.items():
        if any(alias in tl for alias in aliases):
            found.append(canonical)
    return found

def extract_tag_content(text: str, tag: str) -> str:
    """Return text that follows the tag marker, stripping the tag itself."""
    tl = text.lower()
    aliases = NOTE_TAGS.get(tag, [])
    for alias in aliases:
        idx = tl.find(alias)
        if idx != -1:
            return text[idx + len(alias):].strip()
    return text.strip()

def notes_have_any_tag(rows: list[tuple]) -> bool:
    """Return True if any note in rows contains a structured tag."""
    return any(extract_note_tags(txt) for _, txt in rows)
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

# ---- FULL DAILY QUERIES ----
def upsert_full_day(
    day_: date,
    total_sales: float,
    visa: float,
    cash: float,
    tips: float,
    lunch_sales: float,
    lunch_pax: int,
    lunch_walkins: int,
    lunch_noshows: int,
    dinner_sales: float,
    dinner_pax: int,
    dinner_walkins: int,
    dinner_noshows: int,
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO full_daily_stats (
                    day, total_sales, visa, cash, tips,
                    lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
                    dinner_sales, dinner_pax, dinner_walkins, dinner_noshows
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (day) DO UPDATE SET
                    total_sales=EXCLUDED.total_sales,
                    visa=EXCLUDED.visa,
                    cash=EXCLUDED.cash,
                    tips=EXCLUDED.tips,
                    lunch_sales=EXCLUDED.lunch_sales,
                    lunch_pax=EXCLUDED.lunch_pax,
                    lunch_walkins=EXCLUDED.lunch_walkins,
                    lunch_noshows=EXCLUDED.lunch_noshows,
                    dinner_sales=EXCLUDED.dinner_sales,
                    dinner_pax=EXCLUDED.dinner_pax,
                    dinner_walkins=EXCLUDED.dinner_walkins,
                    dinner_noshows=EXCLUDED.dinner_noshows;
                """,
                (
                    day_, total_sales, visa, cash, tips,
                    lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
                    dinner_sales, dinner_pax, dinner_walkins, dinner_noshows
                ),
            )
        conn.commit()

def get_full_day(day_: date):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT total_sales, visa, cash, tips,
                       lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
                       dinner_sales, dinner_pax, dinner_walkins, dinner_noshows
                FROM full_daily_stats
                WHERE day=%s;
                """,
                (day_,),
            )
            row = cur.fetchone()
    return row

def sum_full_in_period(p: Period):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) AS full_days,
                    COALESCE(SUM(total_sales),0),
                    COALESCE(SUM(tips),0),
                    COALESCE(SUM(lunch_sales),0),
                    COALESCE(SUM(lunch_pax),0),
                    COALESCE(SUM(lunch_walkins),0),
                    COALESCE(SUM(lunch_noshows),0),
                    COALESCE(SUM(dinner_sales),0),
                    COALESCE(SUM(dinner_pax),0),
                    COALESCE(SUM(dinner_walkins),0),
                    COALESCE(SUM(dinner_noshows),0)
                FROM full_daily_stats
                WHERE day BETWEEN %s AND %s;
                """,
                (p.start, p.end),
            )
            row = cur.fetchone()
    (
        full_days,
        total_sales, tips,
        lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
        dinner_sales, dinner_pax, dinner_walkins, dinner_noshows,
    ) = row
    return {
        "full_days": int(full_days),
        "total_sales": float(total_sales),
        "tips": float(tips),
        "lunch_sales": float(lunch_sales),
        "lunch_pax": int(lunch_pax),
        "lunch_walkins": int(lunch_walkins),
        "lunch_noshows": int(lunch_noshows),
        "dinner_sales": float(dinner_sales),
        "dinner_pax": int(dinner_pax),
        "dinner_walkins": int(dinner_walkins),
        "dinner_noshows": int(dinner_noshows),
    }

# =========================
# NEW ANALYTICS DB HELPERS
# =========================

def get_full_days_for_weekday(weekday: int, before_or_on: date, limit: int) -> list[dict]:
    """Return up to `limit` full_daily_stats rows for the given ISO weekday (Mon=1..Sun=7),
    ordered most recent first, on or before `before_or_on`."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day, total_sales,
                       lunch_sales, lunch_pax, lunch_noshows,
                       dinner_sales, dinner_pax, dinner_noshows,
                       tips,
                       (COALESCE(lunch_pax,0) + COALESCE(dinner_pax,0)) AS covers
                FROM full_daily_stats
                WHERE EXTRACT(ISODOW FROM day) = %s AND day <= %s
                ORDER BY day DESC
                LIMIT %s;
                """,
                (weekday, before_or_on, limit),
            )
            rows = cur.fetchall()
    result = []
    for r in rows:
        covers = int(r[9] or 0)
        sales = float(r[1] or 0)
        lunch_pax = int(r[3] or 0)
        dinner_pax = int(r[6] or 0)
        lunch_sales = float(r[2] or 0)
        dinner_sales = float(r[5] or 0)
        result.append({
            "day": r[0],
            "total_sales": sales,
            "lunch_sales": lunch_sales,
            "lunch_pax": lunch_pax,
            "lunch_noshows": int(r[4] or 0),
            "dinner_sales": dinner_sales,
            "dinner_pax": dinner_pax,
            "dinner_noshows": int(r[7] or 0),
            "tips": float(r[8] or 0),
            "covers": covers,
            "avg_ticket": (sales / covers) if covers else 0.0,
            "lunch_avg": (lunch_sales / lunch_pax) if lunch_pax else 0.0,
            "dinner_avg": (dinner_sales / dinner_pax) if dinner_pax else 0.0,
        })
    return result

def get_full_days_in_period(p: Period) -> list[dict]:
    """Return all full_daily_stats rows in period ordered by day ASC."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day, total_sales,
                       lunch_sales, lunch_pax, lunch_noshows,
                       dinner_sales, dinner_pax, dinner_noshows,
                       tips,
                       (COALESCE(lunch_pax,0) + COALESCE(dinner_pax,0)) AS covers
                FROM full_daily_stats
                WHERE day BETWEEN %s AND %s
                ORDER BY day ASC;
                """,
                (p.start, p.end),
            )
            rows = cur.fetchall()
    result = []
    for r in rows:
        covers = int(r[9] or 0)
        sales = float(r[1] or 0)
        lunch_pax = int(r[3] or 0)
        dinner_pax = int(r[6] or 0)
        lunch_sales = float(r[2] or 0)
        dinner_sales = float(r[5] or 0)
        result.append({
            "day": r[0],
            "total_sales": sales,
            "lunch_sales": lunch_sales,
            "lunch_pax": lunch_pax,
            "lunch_noshows": int(r[4] or 0),
            "dinner_sales": dinner_sales,
            "dinner_pax": dinner_pax,
            "dinner_noshows": int(r[7] or 0),
            "tips": float(r[8] or 0),
            "covers": covers,
            "avg_ticket": (sales / covers) if covers else 0.0,
            "lunch_avg": (lunch_sales / lunch_pax) if lunch_pax else 0.0,
            "dinner_avg": (dinner_sales / dinner_pax) if dinner_pax else 0.0,
        })
    return result

def get_full_days_for_dates(dates: list[date]) -> dict:
    """Return full_daily_stats rows keyed by date for the given list of dates."""
    if not dates:
        return {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day, total_sales,
                       lunch_sales, lunch_pax, lunch_noshows,
                       dinner_sales, dinner_pax, dinner_noshows,
                       tips,
                       (COALESCE(lunch_pax,0) + COALESCE(dinner_pax,0)) AS covers
                FROM full_daily_stats
                WHERE day = ANY(%s);
                """,
                (dates,),
            )
            rows = cur.fetchall()
    result = {}
    for r in rows:
        covers = int(r[9] or 0)
        sales = float(r[1] or 0)
        lunch_pax = int(r[3] or 0)
        dinner_pax = int(r[6] or 0)
        lunch_sales = float(r[2] or 0)
        dinner_sales = float(r[5] or 0)
        result[r[0]] = {
            "day": r[0],
            "total_sales": sales,
            "lunch_sales": lunch_sales,
            "lunch_pax": lunch_pax,
            "lunch_noshows": int(r[4] or 0),
            "dinner_sales": dinner_sales,
            "dinner_pax": dinner_pax,
            "dinner_noshows": int(r[7] or 0),
            "tips": float(r[8] or 0),
            "covers": covers,
            "avg_ticket": (sales / covers) if covers else 0.0,
            "lunch_avg": (lunch_sales / lunch_pax) if lunch_pax else 0.0,
            "dinner_avg": (dinner_sales / dinner_pax) if dinner_pax else 0.0,
        }
    return result

# =========================
# Owners formatting helpers
# =========================
def euro_comma(x: float) -> str:
    s = f"{float(x):.2f}"
    return s.replace(".", ",")

def fmt_day_ddmmyyyy(d: date) -> str:
    return d.strftime("%d/%m/%Y")

# =========================
# STATE MAP HELPERS
# =========================
def _map_get(app: Application, key: str) -> dict[str, dict]:
    m = app.bot_data.get(key)
    if not isinstance(m, dict):
        m = {}
        app.bot_data[key] = m
    return m

def set_mode(app: Application, keyname: str, chat_id: int, user_id: int, payload: dict):
    k = f"{chat_id}:{user_id}"
    _map_get(app, keyname)[k] = payload

def get_mode(app: Application, keyname: str, chat_id: int, user_id: int):
    k = f"{chat_id}:{user_id}"
    return _map_get(app, keyname).get(k)

def clear_mode(app: Application, keyname: str, chat_id: int, user_id: int):
    k = f"{chat_id}:{user_id}"
    _map_get(app, keyname).pop(k, None)

# =========================
# PERMISSIONS BY ROLE
# =========================
def current_chat_role(update: Update) -> str | None:
    c = update.effective_chat
    return get_chat_role(c.id) if c else None

def allow_sales_cmd(update: Update) -> bool:
    role = current_chat_role(update)
    if role in (ROLE_OPS_ADMIN, ROLE_MANAGER_INPUT, ROLE_OWNERS_REQUESTS):
        return True
    return is_admin(update)

def allow_notes_cmd(update: Update) -> bool:
    role = current_chat_role(update)
    if role in (ROLE_OPS_ADMIN, ROLE_MANAGER_INPUT):
        return True
    return is_admin(update)

def allow_full_cmd(update: Update) -> bool:
    role = current_chat_role(update)
    if role in (ROLE_OPS_ADMIN, ROLE_MANAGER_INPUT):
        return True
    return is_admin(update)

# =========================
# FULL DAILY PARSING (English + Spanish labels)
# =========================
def _num(s: str) -> float:
    s = (s or "").strip()
    s = s.replace("€", "").replace(" ", "")
    if re.fullmatch(r"[\d\.]+,\d+", s):
        s = s.replace(".", "")
    s = s.replace(",", ".")
    return float(s)

def _int(s: str) -> int:
    s = (s or "").strip()
    s = re.sub(r"[^\d\-]", "", s)
    return int(s)

FULL_EXAMPLE = (
    "Example:\n"
    "Day: 24/01/2026\n"
    "Total Sales Day: 7199,50\n"
    "Visa: 6400,30\n"
    "Cash: 799,20\n"
    "Tips: 103,60\n\n"
    "Lunch: 2341,30\n"
    "Pax: 50\n"
    "Walk in: 3\n"
    "No show: 7\n\n"
    "Dinner: 4858,20\n"
    "Pax: 106\n"
    "Walk in: 2\n"
    "No show: 4\n"
)

def parse_full_report_block(text: str) -> dict:
    t = (text or "").strip()
    if not t:
        raise ValueError("Empty")

    def find_line(prefixes: list[str]) -> str | None:
        for line in t.splitlines():
            raw = line.strip()
            for pfx in prefixes:
                if raw.lower().startswith(pfx.lower()):
                    return raw.split(":", 1)[1].strip() if ":" in raw else raw[len(pfx):].strip()
        return None

    # Day (English / Spanish)
    day_str = find_line(["Day", "Día", "Dia", "Fecha"])
    if not day_str:
        raise ValueError("Missing Day")
    day_ = parse_any_date(day_str)

    # Totals
    total_sales = _num(find_line(["Total Sales Day", "Total Sales", "Ventas Totales Día", "Ventas Totales", "Ventas"]) or "")
    visa = _num(find_line(["Visa", "Tarjeta", "Card"]) or "0")
    cash = _num(find_line(["Cash", "Efectivo"]) or "0")
    tips = _num(find_line(["Tips", "Propinas"]) or "0")

    def parse_section(section_names: list[str]) -> tuple[float, int, int, int]:
        lines = [ln.strip() for ln in t.splitlines()]
        idx = None
        matched_name = None
        for i, ln in enumerate(lines):
            low = ln.lower()
            for nm in section_names:
                if low.startswith(nm.lower() + ":"):
                    idx = i
                    matched_name = nm
                    break
            if idx is not None:
                break
        if idx is None:
            raise ValueError(f"Missing section {section_names[0]}")

        sales_val = _num(lines[idx].split(":", 1)[1].strip())

        pax = walkins = noshows = None
        for j in range(idx + 1, min(idx + 12, len(lines))):
            ln = lines[j].strip()
            if not ln:
                continue
            low = ln.lower()

            # stop if next section begins
            if any(low.startswith(x.lower() + ":") for x in ["dinner", "cena", "lunch", "almuerzo", "comida"]):
                break

            if low.startswith("average pax") or low.startswith("avg pax") or low.startswith("avg ticket") or low.startswith("average ticket") or low.startswith("media pax") or low.startswith("ticket medio"):
                continue  # bot calculates this — skip if GM still includes it

            if low.startswith("pax") or low.startswith("personas"):
                pax = _int(ln.split(":", 1)[1])
            elif low.startswith("walk in") or low.startswith("walk-in") or low.startswith("walkin") or low.startswith("sin reserva") or low.startswith("sin-reserva"):
                walkins = _int(ln.split(":", 1)[1])
            elif low.startswith("no show") or low.startswith("no-show") or low.startswith("noshow") or low.startswith("no se presentó") or low.startswith("no se presento"):
                noshows = _int(ln.split(":", 1)[1])

        if pax is None or walkins is None or noshows is None:
            raise ValueError(f"Incomplete section {matched_name or section_names[0]} (need Pax/Personas, Walk-in/Sin reserva, No-show/No se presentó)")
        return float(sales_val), int(pax), int(walkins), int(noshows)

    lunch_sales, lunch_pax, lunch_walkins, lunch_noshows = parse_section(["Lunch", "Almuerzo", "Comida"])
    dinner_sales, dinner_pax, dinner_walkins, dinner_noshows = parse_section(["Dinner", "Cena"])

    return {
        "day": day_,
        "total_sales": float(total_sales),
        "visa": float(visa),
        "cash": float(cash),
        "tips": float(tips),
        "lunch_sales": float(lunch_sales),
        "lunch_pax": int(lunch_pax),
        "lunch_walkins": int(lunch_walkins),
        "lunch_noshows": int(lunch_noshows),
        "dinner_sales": float(dinner_sales),
        "dinner_pax": int(dinner_pax),
        "dinner_walkins": int(dinner_walkins),
        "dinner_noshows": int(dinner_noshows),
    }

# =========================
# NOTES: auto-detect manager report blocks (English + Spanish)
# =========================
NOTES_HINTS = [
    "incidents", "incident", "staff", "sold out", "sold-out", "complaints",
    "incidencias", "incidencia", "personal", "agotado", "agotados", "quejas", "queja",
]

def extract_day_from_notes(text: str) -> date | None:
    # Optional header: "Day: 26/02/2026" or "Fecha: 2026-02-26"
    for line in (text or "").splitlines()[:6]:
        raw = line.strip()
        if ":" not in raw:
            continue
        k, v = raw.split(":", 1)
        k = k.strip().lower()
        v = v.strip()
        if k in ("day", "día", "dia", "fecha"):
            try:
                return parse_any_date(v)
            except:
                return None
    return None

def looks_like_notes_report(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 12:
        return False
    low = t.lower()
    hits = sum(1 for h in NOTES_HINTS if h in low)
    # require at least 2 hits OR one hit + multi-line structure
    if hits >= 2:
        return True
    if hits >= 1 and ("\n" in t):
        return True
    return False

# =========================
# HELP TEXT
# =========================
HELP_TEXT = (
    "📌 Norah Ops commands\n\n"
    "Sales:\n"
    "/setdaily SALES COVERS  (business day)\n"
    "/edit YYYY-MM-DD SALES COVERS\n"
    "/daily\n"
    "/month\n"
    "/last 7 | /last 6M | /last 1Y\n"
    "/range YYYY-MM-DD YYYY-MM-DD\n"
    "/bestday\n"
    "/worstday\n\n"
    "Full daily (Manager input):\n"
    "/setfull (paste full report next message)\n"
    "/setfullguided (guided Q&A, always lunch+dinner)\n"
    "/confirmfull\n"
    "/cancelfull\n\n"
    "Notes:\n"
    "/report (send notes as next message)\n"
    "/cancelreport\n"
    "/reportdaily\n"
    "/reportday YYYY-MM-DD\n\n"
    "Owners repost (ADMIN):\n"
    "/postday YYYY-MM-DD  (or DD/MM/YYYY)\n\n"
    "Notes analytics:\n"
    "/noteslast 30 (or 6M / 1Y)\n"
    "/findnote keyword\n"
    "/soldout 30\n"
    "/complaints 30\n\n"
    "Setup (ADMIN):\n"
    "/setowners\n"
    "/ownerslist\n"
    "/removeowners\n"
    "/setchatrole OPS_ADMIN | OWNERS_SILENT | MANAGER_INPUT | OWNERS_REQUESTS\n"
    "/chats\n\n"
    "Debug:\n"
    "/ping\n"
    "/whoami\n"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Norah Ops is online.\n\n" + HELP_TEXT)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

# =========================
# CHAT ROLE SETUP
# =========================
async def setchatrole_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    if not context.args:
        await update.message.reply_text("Usage: /setchatrole OPS_ADMIN | OWNERS_SILENT | MANAGER_INPUT | OWNERS_REQUESTS")
        return
    role = context.args[0].strip().upper()
    if role not in VALID_CHAT_ROLES:
        await update.message.reply_text("Invalid role. Use: OPS_ADMIN | OWNERS_SILENT | MANAGER_INPUT | OWNERS_REQUESTS")
        return

    title = getattr(chat, "title", None)
    set_chat_role(chat.id, role, ctype=chat.type, title=title)

    if role == ROLE_OWNERS_SILENT:
        add_owner_chat_legacy(chat.id)

    await update.message.reply_text(f"✅ Chat role set: {role}\nChat ID: {chat.id}")

async def chats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    rows = list_all_chats()
    if not rows:
        await update.message.reply_text("No chat roles set yet. Use /setchatrole in each chat once.")
        return
    lines = []
    for cid, role, ctype, title in rows:
        lines.append(f"{role} | {cid} | {ctype or '-'} | {title or '-'}")
    await update.message.reply_text("Chats:\n" + "\n".join(lines))

async def setowners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    add_owner_chat_legacy(chat.id)
    title = getattr(chat, "title", None)
    set_chat_role(chat.id, ROLE_OWNERS_SILENT, ctype=chat.type, title=title)
    await update.message.reply_text(f"✅ Owners chat registered: {chat.id}")

async def ownerslist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    ids = owners_silent_chat_ids()
    if not ids:
        await update.message.reply_text("Owners chats: NONE. Run /setowners or /setchatrole OWNERS_SILENT.")
        return
    await update.message.reply_text("Owners chats:\n" + "\n".join(str(x) for x in ids))

async def removeowners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    remove_owner_chat_legacy(chat.id)
    await update.message.reply_text(f"🗑️ Removed this chat from owners list: {chat.id}")

# =========================
# DEBUG
# =========================
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    role = get_chat_role(chat.id) or "-"
    await update.message.reply_text(
        f"👤 User ID: {user.id}\n"
        f"💬 Chat ID: {chat.id}\n"
        f"🗣️ Chat type: {chat.type}\n"
        f"🏷️ Chat role: {role}\n"
        f"🔐 Admin: {'YES' if is_admin(update) else 'NO'}"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    db_ok = False
    db_err = ""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
        db_ok = True
    except Exception as e:
        db_ok = False
        db_err = str(e)[:180]

    now = now_local()
    bday = business_day_today()
    prev_bday = previous_business_day(now)
    owners = owners_silent_chat_ids()

    allow_mode = "OPEN" if ACCESS_MODE == "OPEN" else ("OPEN (no ALLOWED_USER_IDS set)" if not ALLOWED_USER_IDS else "RESTRICTED")
    jobq = "YES" if context.application.job_queue is not None else "NO"

    msg = (
        "🏓 PONG — Norah Ops Health Check\n\n"
        f"Bot: ✅ running\n"
        f"DB: {'✅ OK' if db_ok else '❌ FAIL'}\n"
    )
    if not db_ok:
        msg += f"DB error: {db_err}\n"

    msg += (
        f"\nTime: {now.strftime('%Y-%m-%d %H:%M')} ({TZ_NAME})\n"
        f"Cutoff hour: {CUTOFF_HOUR}:00\n"
        f"Business day now: {bday.isoformat()}\n"
        f"Previous business day: {prev_bday.isoformat()}\n"
        f"\nOwners silent chats: {', '.join(str(x) for x in owners) if owners else 'NONE'}\n"
        f"Access mode: {allow_mode}\n"
        f"JobQueue: {jobq}\n"
        f"\nThis chat id: {chat.id}\n"
        f"Your user id: {user.id}"
    )

    await update.message.reply_text(msg)

async def resetdb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only — wipe all operational data, keep structure intact."""
    if not await guard_admin(update, reply_in_private_only=False):
        return
    # Require confirmation argument to prevent accidents
    if not context.args or context.args[0] != "CONFIRM":
        await update.message.reply_text(
            "⚠️ This will delete ALL data (sales, notes, stats).\n\n"
            "To confirm, send:\n/resetdb CONFIRM"
        )
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE full_daily_stats;")
            cur.execute("TRUNCATE TABLE daily_stats;")
            cur.execute("TRUNCATE TABLE notes_entries;")
        conn.commit()
    await update.message.reply_text("✅ Database wiped. All sales and notes data deleted. Ready for real data.")

async def deleteday_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only — delete all data for a specific business day."""
    if not await guard_admin(update, reply_in_private_only=False):
        return
    if not context.args:
        await update.message.reply_text("Usage: /deleteday YYYY-MM-DD")
        return
    try:
        day_ = parse_yyyy_mm_dd(context.args[0])
    except:
        await update.message.reply_text("Usage: /deleteday YYYY-MM-DD\nExample: /deleteday 2026-02-27")
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM full_daily_stats WHERE day = %s;", (day_,))
            deleted_full = cur.rowcount
            cur.execute("DELETE FROM daily_stats WHERE day = %s;", (day_,))
            deleted_daily = cur.rowcount
            cur.execute("DELETE FROM notes_entries WHERE day = %s;", (day_,))
            deleted_notes = cur.rowcount
        conn.commit()
    await update.message.reply_text(
        f"🗑️ Deleted data for {day_.isoformat()}:\n"
        f"  Full stats: {deleted_full} row(s)\n"
        f"  Daily stats: {deleted_daily} row(s)\n"
        f"  Notes: {deleted_notes} row(s)"
    )


def _append_full_analytics_block(p: Period) -> str:
    agg = sum_full_in_period(p)
    full_days = agg["full_days"]
    if full_days <= 0:
        return ""

    lunch_avg = (agg["lunch_sales"] / agg["lunch_pax"]) if agg["lunch_pax"] else 0.0
    dinner_avg = (agg["dinner_sales"] / agg["dinner_pax"]) if agg["dinner_pax"] else 0.0

    covers_full = agg["lunch_pax"] + agg["dinner_pax"]
    tips_pct = (agg["tips"] / agg["total_sales"] * 100.0) if agg["total_sales"] else 0.0
    tip_per_cover = (agg["tips"] / covers_full) if covers_full else 0.0
    avg_tips_day = (agg["tips"] / full_days) if full_days else 0.0

    walkins_total = agg["lunch_walkins"] + agg["dinner_walkins"]
    noshows_total = agg["lunch_noshows"] + agg["dinner_noshows"]
    walkins_rate = (walkins_total / covers_full * 100.0) if covers_full else 0.0
    avg_walkins_day = (walkins_total / full_days) if full_days else 0.0
    avg_noshows_day = (noshows_total / full_days) if full_days else 0.0

    return (
        "\n\n🍽️ Service split (weighted)\n"
        f"Lunch avg ticket: €{lunch_avg:.2f}\n"
        f"Dinner avg ticket: €{dinner_avg:.2f}\n"
        "\n💶 Tips\n"
        f"Total tips: €{agg['tips']:.2f}\n"
        f"Avg tips/day: €{avg_tips_day:.2f}\n"
        f"Tip/cover: €{tip_per_cover:.2f}\n"
        f"Tips % of sales: {tips_pct:.1f}%\n"
        "\n🚶 Walk-ins / No-shows\n"
        f"Total walk-ins: {walkins_total}\n"
        f"Avg walk-ins/day: {avg_walkins_day:.2f}\n"
        f"Walk-ins rate: {walkins_rate:.1f}%\n"
        f"Total no-shows: {noshows_total}\n"
        f"Avg no-shows/day: {avg_noshows_day:.2f}"
    )

async def setdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
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
    if not allow_sales_cmd(update):
        return
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
    if not allow_sales_cmd(update):
        return
    day_ = business_day_today()
    row = get_daily(day_)
    if not row:
        await update.message.reply_text(f"No data for business day {day_.isoformat()} yet. Use: /setdaily 2450 118")
        return
    sales, covers = row
    sales = float(sales or 0)
    covers = int(covers or 0)
    avg = (sales / covers) if covers else 0.0
    p = Period(day_, day_)
    msg = (
        f"📊 Norah Daily Report\n\n"
        f"Business day: {day_.isoformat()}\n"
        f"Sales: €{sales:.2f}\n"
        f"Covers: {covers}\n"
        f"Avg ticket: €{avg:.2f}"
    )
    msg += _append_full_analytics_block(p)
    await update.message.reply_text(msg)

async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    end = business_day_today()
    start = date(end.year, end.month, 1)
    p = Period(start=start, end=end)
    total_sales, total_covers, days_with_data = sum_daily(p)
    avg_ticket = (total_sales / total_covers) if total_covers else 0.0
    msg = (
        f"📈 Norah Month-to-Date\n"
        f"Period: {p.start.isoformat()} → {p.end.isoformat()} ({daterange_days(p)} day(s))\n\n"
        f"Days with data: {days_with_data}\n"
        f"Total sales: €{total_sales:.2f}\n"
        f"Total covers: {total_covers}\n"
        f"Avg ticket: €{avg_ticket:.2f}"
    )
    msg += _append_full_analytics_block(p)
    await update.message.reply_text(msg)

async def last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
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
    msg = (
        f"📊 Norah Summary\n"
        f"Period: {p.start.isoformat()} → {p.end.isoformat()} ({daterange_days(p)} day(s))\n\n"
        f"Days with data: {days_with_data}\n"
        f"Total sales: €{total_sales:.2f}\n"
        f"Total covers: {total_covers}\n"
        f"Avg ticket: €{avg_ticket:.2f}"
    )
    msg += _append_full_analytics_block(p)
    await update.message.reply_text(msg)

async def range_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
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
    msg = (
        f"📊 Norah Range Report\n"
        f"Period: {p.start.isoformat()} → {p.end.isoformat()} ({daterange_days(p)} day(s))\n\n"
        f"Days with data: {days_with_data}\n"
        f"Total sales: €{total_sales:.2f}\n"
        f"Total covers: {total_covers}\n"
        f"Avg ticket: €{avg_ticket:.2f}"
    )
    msg += _append_full_analytics_block(p)
    await update.message.reply_text(msg)

async def bestday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    p = period_ending_today("30")
    row = best_or_worst_day(p, worst=False)
    if not row:
        await update.message.reply_text("No sales data found yet.")
        return
    d, sales, covers = row
    avg = (float(sales) / int(covers)) if covers else 0.0
    await update.message.reply_text(
        f"🏆 Best day (last 30)\nDay: {d}\nSales: €{float(sales):.2f}\nCovers: {int(covers)}\nAvg ticket: €{avg:.2f}"
    )

async def worstday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    p = period_ending_today("30")
    row = best_or_worst_day(p, worst=True)
    if not row:
        await update.message.reply_text("No sales data found yet.")
        return
    d, sales, covers = row
    avg = (float(sales) / int(covers)) if covers else 0.0
    await update.message.reply_text(
        f"🧯 Worst day (last 30)\nDay: {d}\nSales: €{float(sales):.2f}\nCovers: {int(covers)}\nAvg ticket: €{avg:.2f}"
    )

# =========================
# NOTES COMMANDS
# =========================
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_notes_cmd(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    day_ = business_day_today()
    set_mode(context.application, REPORT_MODE_KEY, chat.id, user.id, {"on": True, "day": day_.isoformat()})
    await update.message.reply_text(
        f"✅ Report mode ON. Send your notes as the next message.\n"
        f"Business day: {day_.isoformat()}\n\n"
        f"📌 Use tags to categorize notes:\n"
        f"  [SOLD OUT] item name\n"
        f"  [COMPLAINT] description\n"
        f"  [STAFF] description\n"
        f"  [MAINTENANCE] description\n"
        f"  [INCIDENT] description\n\n"
        f"You can include multiple tags in one message.\n"
        f"To cancel: /cancelreport"
    )

async def cancelreport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_notes_cmd(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    clear_mode(context.application, REPORT_MODE_KEY, chat.id, user.id)
    await update.message.reply_text("❎ Report mode cancelled.")

async def reportdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_notes_cmd(update):
        return
    day_ = business_day_today()
    texts = notes_for_day(day_)
    if not texts:
        await update.message.reply_text(f"No notes saved for business day {day_.isoformat()} yet.\nUse /report to submit notes.")
        return
    joined = "\n\n— — —\n\n".join(texts)
    await update.message.reply_text(f"📝 Notes for business day {day_.isoformat()}:\n\n{joined}")

async def reportday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_notes_cmd(update):
        return
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

# Notes analytics
async def noteslast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
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
    await update.message.reply_text("📊 Notes trends:\n" + "\n".join(lines))

async def findnote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /findnote keyword")
        return
    keyword = " ".join(context.args).strip().lower()
    if not keyword:
        await update.message.reply_text("Usage: /findnote keyword")
        return
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
    show = uniq[-10:]
    await update.message.reply_text(f"🔎 Matches for '{keyword}':\n" + "\n".join(d.isoformat() for d in show))

async def soldout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
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

    # Tag-based extraction (preferred)
    tagged_texts = [(d, txt) for d, txt in rows if "SOLD OUT" in extract_note_tags(txt)]
    if tagged_texts:
        counter = Counter()
        for _, txt in tagged_texts:
            content = extract_tag_content(txt, "SOLD OUT")
            counter.update(tokenize(content))
        top = counter.most_common(12)
        source = f"({len(tagged_texts)} tagged notes)"
    else:
        # Fallback: keyword matching
        counter = Counter()
        for _, txt in rows:
            t = (txt or "").lower()
            if "sold out" in t or "agotad" in t:
                counter.update(tokenize(txt))
        top = counter.most_common(12)
        source = "(keyword fallback — consider using [SOLD OUT] tags)"

    if not top:
        await update.message.reply_text("No sold-out items detected for that period.")
        return
    await update.message.reply_text(
        f"🍽️ Sold-out items {source}:\n" + "\n".join(f"{w}: {c}" for w, c in top)
    )

async def complaints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
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

    # Tag-based extraction (preferred)
    tagged_texts = [(d, txt) for d, txt in rows if "COMPLAINT" in extract_note_tags(txt)]
    if tagged_texts:
        counter = Counter()
        for _, txt in tagged_texts:
            content = extract_tag_content(txt, "COMPLAINT")
            counter.update(tokenize(content))
        top = counter.most_common(12)
        source = f"({len(tagged_texts)} tagged notes)"
    else:
        # Fallback: keyword matching
        counter = Counter()
        for _, txt in rows:
            t = (txt or "").lower()
            if "complaint" in t or "queja" in t:
                counter.update(tokenize(txt))
        top = counter.most_common(12)
        source = "(keyword fallback — consider using [COMPLAINT] tags)"

    if not top:
        await update.message.reply_text("No complaint signals detected for that period.")
        return
    await update.message.reply_text(
        f"⚠️ Complaint signals {source}:\n" + "\n".join(f"{w}: {c}" for w, c in top)
    )

async def tagstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a breakdown of all tagged notes over a period."""
    if not allow_sales_cmd(update):
        return
    try:
        p = period_ending_today(context.args[0]) if context.args else period_ending_today("30")
    except:
        await update.message.reply_text("Usage: /tagstats  or  /tagstats 60")
        return
    rows = notes_in_period(p)
    if not rows:
        await update.message.reply_text("No notes found for that period yet.")
        return

    counts: dict[str, int] = {tag: 0 for tag in NOTE_TAGS}
    untagged = 0
    for _, txt in rows:
        found = extract_note_tags(txt)
        if found:
            for tag in found:
                counts[tag] += 1
        else:
            untagged += 1

    total = len(rows)
    tagged_total = sum(counts.values())
    lines = [
        f"🏷️ Tag Summary ({fmt_day_ddmmyyyy(p.start)} → {fmt_day_ddmmyyyy(p.end)})\n",
        f"Total notes: {total}  |  Tagged: {tagged_total}  |  Untagged: {untagged}\n",
    ]
    for tag, count in counts.items():
        if count > 0:
            emoji = TAG_EMOJIS.get(tag, "•")
            lines.append(f"{emoji} [{tag}]: {count}")
    if tagged_total == 0:
        lines.append("No tagged notes yet. Encourage the manager to use tags like [COMPLAINT], [SOLD OUT], etc.")
    await update.message.reply_text("\n".join(lines))

async def staffnotes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent notes tagged with [STAFF]."""
    if not allow_sales_cmd(update):
        return
    try:
        p = period_ending_today(context.args[0]) if context.args else period_ending_today("30")
    except:
        await update.message.reply_text("Usage: /staffnotes  or  /staffnotes 60")
        return
    rows = notes_in_period(p)
    if not rows:
        await update.message.reply_text("No notes found for that period yet.")
        return

    tagged = [(d, txt) for d, txt in rows if "STAFF" in extract_note_tags(txt)]
    if not tagged:
        await update.message.reply_text(
            f"No [STAFF] tagged notes in the last period.\n"
            f"(keyword fallback — consider using [STAFF] tags)\n\n"
            + _keyword_staff_fallback(rows)
        )
        return

    lines = [f"👥 Staff Notes ({len(tagged)} entries)\n"]
    for d, txt in tagged[-10:]:  # show last 10
        content = extract_tag_content(txt, "STAFF")
        lines.append(f"📆 {fmt_day_ddmmyyyy(d)}: {content[:120]}")
    await update.message.reply_text("\n".join(lines))

def _keyword_staff_fallback(rows: list[tuple]) -> str:
    keywords = ["staff", "personal", "sick", "enfermo", "ausente", "absent", "late", "tarde"]
    matches = [(d, txt) for d, txt in rows if any(k in (txt or "").lower() for k in keywords)]
    if not matches:
        return "(no staff-related notes found via keywords either)"
    lines = []
    for d, txt in matches[-5:]:
        lines.append(f"📆 {fmt_day_ddmmyyyy(d)}: {txt[:120]}")
    return "\n".join(lines)



def _fmt_snapshot(day_: date, label: str) -> str:
    """Format a full single-day snapshot for /today and /yesterday."""
    row = get_full_day(day_)
    if not row:
        return f"No data for {label} ({fmt_day_ddmmyyyy(day_)}) yet."
    (total_sales, visa, cash, tips,
     lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
     dinner_sales, dinner_pax, dinner_walkins, dinner_noshows) = row
    total_sales = float(total_sales or 0)
    lunch_pax = int(lunch_pax or 0)
    dinner_pax = int(dinner_pax or 0)
    covers = lunch_pax + dinner_pax
    avg_ticket = (total_sales / covers) if covers else 0.0
    lunch_sales = float(lunch_sales or 0)
    dinner_sales = float(dinner_sales or 0)
    lunch_avg = (lunch_sales / lunch_pax) if lunch_pax else 0.0
    dinner_avg = (dinner_sales / dinner_pax) if dinner_pax else 0.0
    return (
        f"📊 Norah — {label} ({fmt_day_ddmmyyyy(day_)})\n\n"
        f"💰 Sales: €{total_sales:.2f}\n"
        f"   Visa: €{float(visa or 0):.2f}  |  Cash: €{float(cash or 0):.2f}\n"
        f"   Tips: €{float(tips or 0):.2f}\n\n"
        f"👥 Covers: {covers}  |  Avg ticket: €{avg_ticket:.2f}\n\n"
        f"🌞 Lunch: €{lunch_sales:.2f}  |  {lunch_pax} pax  |  Avg €{lunch_avg:.2f}\n"
        f"   Walk-ins: {int(lunch_walkins or 0)}  |  No-shows: {int(lunch_noshows or 0)}\n\n"
        f"🌙 Dinner: €{dinner_sales:.2f}  |  {dinner_pax} pax  |  Avg €{dinner_avg:.2f}\n"
        f"   Walk-ins: {int(dinner_walkins or 0)}  |  No-shows: {int(dinner_noshows or 0)}"
    )

def _sum_period_rows(rows: list[dict]) -> dict:
    """Aggregate a list of daily row dicts into period totals."""
    sales = sum(r["total_sales"] for r in rows)
    covers = sum(r["covers"] for r in rows)
    lunch_sales = sum(r.get("lunch_sales", 0) for r in rows)
    lunch_pax = sum(r.get("lunch_pax", 0) for r in rows)
    dinner_sales = sum(r.get("dinner_sales", 0) for r in rows)
    dinner_pax = sum(r.get("dinner_pax", 0) for r in rows)
    lunch_noshows = sum(r.get("lunch_noshows", 0) for r in rows)
    dinner_noshows = sum(r.get("dinner_noshows", 0) for r in rows)
    tips = sum(r.get("tips", 0) for r in rows)
    total_noshows = lunch_noshows + dinner_noshows
    noshow_rate = (total_noshows / (covers + total_noshows) * 100) if (covers + total_noshows) > 0 else 0.0
    tips_pct = (tips / sales * 100) if sales else 0.0
    return {
        "sales": sales,
        "covers": covers,
        "avg_ticket": (sales / covers) if covers else 0.0,
        "lunch_sales": lunch_sales,
        "lunch_pax": lunch_pax,
        "lunch_avg": (lunch_sales / lunch_pax) if lunch_pax else 0.0,
        "dinner_sales": dinner_sales,
        "dinner_pax": dinner_pax,
        "dinner_avg": (dinner_sales / dinner_pax) if dinner_pax else 0.0,
        "lunch_noshows": lunch_noshows,
        "dinner_noshows": dinner_noshows,
        "total_noshows": total_noshows,
        "noshow_rate": noshow_rate,
        "tips": tips,
        "tips_pct": tips_pct,
        "days": len(rows),
    }

def _pct_delta(va: float, vb: float) -> str:
    if vb == 0:
        return "n/a"
    pct = (va - vb) / vb * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"

def _last_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    await update.message.reply_text(_fmt_snapshot(business_day_today(), "Today"))

async def yesterday_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    await update.message.reply_text(_fmt_snapshot(previous_business_day(), "Yesterday"))

async def dow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    try:
        n = int(context.args[0]) if context.args else 5
        if n < 1 or n > 20:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /dow 5  (compare today with last N same weekdays, 1–20)")
        return
    today = business_day_today()
    weekday = today.isoweekday()  # Mon=1..Sun=7
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_name = day_names[weekday - 1]
    rows = get_full_days_for_weekday(weekday, today, n + 1)
    if not rows:
        await update.message.reply_text(f"No {day_name} data found yet.")
        return
    lines = [f"📅 {day_name} comparison (last {len(rows)})\n"]
    for r in rows:
        tag = "  ← today" if r["day"] == today else ""
        lines.append(
            f"📆 {fmt_day_ddmmyyyy(r['day'])}{tag}\n"
            f"   Sales: €{r['total_sales']:.2f}  |  Covers: {r['covers']}  |  Avg: €{r['avg_ticket']:.2f}"
        )
    await update.message.reply_text("\n".join(lines))

async def weekcompare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    today = business_day_today()
    this_mon = _last_monday(today)
    last_mon = this_mon - timedelta(days=7)
    last_equiv = today - timedelta(days=7)

    rows_this = get_full_days_in_period(Period(this_mon, today))
    rows_last = get_full_days_in_period(Period(last_mon, last_equiv))
    a = _sum_period_rows(rows_this)
    b = _sum_period_rows(rows_last)

    msg = (
        f"📊 Week Comparison\n\n"
        f"This week ({fmt_day_ddmmyyyy(this_mon)} → {fmt_day_ddmmyyyy(today)}):\n"
        f"  Sales: €{a['sales']:.2f}  |  Covers: {a['covers']}\n"
        f"  Avg ticket: €{a['avg_ticket']:.2f}\n"
        f"  🌞 Lunch avg: €{a['lunch_avg']:.2f}  |  🌙 Dinner avg: €{a['dinner_avg']:.2f}\n"
        f"  💶 Tips: €{a['tips']:.2f} ({a['tips_pct']:.1f}% of sales)\n"
        f"  🚫 No-shows: {a['total_noshows']} ({a['noshow_rate']:.1f}%)\n"
        f"  Days w/ data: {a['days']}\n\n"
        f"Last week ({fmt_day_ddmmyyyy(last_mon)} → {fmt_day_ddmmyyyy(last_equiv)}):\n"
        f"  Sales: €{b['sales']:.2f}  |  Covers: {b['covers']}\n"
        f"  Avg ticket: €{b['avg_ticket']:.2f}\n"
        f"  🌞 Lunch avg: €{b['lunch_avg']:.2f}  |  🌙 Dinner avg: €{b['dinner_avg']:.2f}\n"
        f"  💶 Tips: €{b['tips']:.2f} ({b['tips_pct']:.1f}% of sales)\n"
        f"  🚫 No-shows: {b['total_noshows']} ({b['noshow_rate']:.1f}%)\n"
        f"  Days w/ data: {b['days']}\n\n"
        f"📈 vs last week:\n"
        f"  Sales: {_pct_delta(a['sales'], b['sales'])}\n"
        f"  Covers: {_pct_delta(a['covers'], b['covers'])}\n"
        f"  Avg ticket: {_pct_delta(a['avg_ticket'], b['avg_ticket'])}\n"
        f"  Lunch avg: {_pct_delta(a['lunch_avg'], b['lunch_avg'])}\n"
        f"  Dinner avg: {_pct_delta(a['dinner_avg'], b['dinner_avg'])}\n"
        f"  Tips %: {_pct_delta(a['tips_pct'], b['tips_pct'])}\n"
        f"  No-show rate: {_pct_delta(a['noshow_rate'], b['noshow_rate'])}"
    )
    await update.message.reply_text(msg)

async def monthcompare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    today = business_day_today()
    this_start = date(today.year, today.month, 1)

    # Same day number last month
    last_start = add_months(this_start, -1)
    last_equiv = add_months(today, -1)

    rows_this = get_full_days_in_period(Period(this_start, today))
    rows_last = get_full_days_in_period(Period(last_start, last_equiv))
    a = _sum_period_rows(rows_this)
    b = _sum_period_rows(rows_last)

    msg = (
        f"📊 Month Comparison\n\n"
        f"This month ({fmt_day_ddmmyyyy(this_start)} → {fmt_day_ddmmyyyy(today)}):\n"
        f"  Sales: €{a['sales']:.2f}  |  Covers: {a['covers']}\n"
        f"  Avg ticket: €{a['avg_ticket']:.2f}\n"
        f"  🌞 Lunch avg: €{a['lunch_avg']:.2f}  |  🌙 Dinner avg: €{a['dinner_avg']:.2f}\n"
        f"  💶 Tips: €{a['tips']:.2f} ({a['tips_pct']:.1f}% of sales)\n"
        f"  🚫 No-shows: {a['total_noshows']} ({a['noshow_rate']:.1f}%)\n"
        f"  Days w/ data: {a['days']}\n\n"
        f"Last month ({fmt_day_ddmmyyyy(last_start)} → {fmt_day_ddmmyyyy(last_equiv)}):\n"
        f"  Sales: €{b['sales']:.2f}  |  Covers: {b['covers']}\n"
        f"  Avg ticket: €{b['avg_ticket']:.2f}\n"
        f"  🌞 Lunch avg: €{b['lunch_avg']:.2f}  |  🌙 Dinner avg: €{b['dinner_avg']:.2f}\n"
        f"  💶 Tips: €{b['tips']:.2f} ({b['tips_pct']:.1f}% of sales)\n"
        f"  🚫 No-shows: {b['total_noshows']} ({b['noshow_rate']:.1f}%)\n"
        f"  Days w/ data: {b['days']}\n\n"
        f"📈 vs last month:\n"
        f"  Sales: {_pct_delta(a['sales'], b['sales'])}\n"
        f"  Covers: {_pct_delta(a['covers'], b['covers'])}\n"
        f"  Avg ticket: {_pct_delta(a['avg_ticket'], b['avg_ticket'])}\n"
        f"  Lunch avg: {_pct_delta(a['lunch_avg'], b['lunch_avg'])}\n"
        f"  Dinner avg: {_pct_delta(a['dinner_avg'], b['dinner_avg'])}\n"
        f"  Tips %: {_pct_delta(a['tips_pct'], b['tips_pct'])}\n"
        f"  No-show rate: {_pct_delta(a['noshow_rate'], b['noshow_rate'])}"
    )
    await update.message.reply_text(msg)

async def weekendcompare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    today = business_day_today()
    # Find most recent Saturday on or before today
    days_since_sat = (today.weekday() - 5) % 7  # Saturday=5 in weekday()
    last_sat = today - timedelta(days=days_since_sat)
    last_fri = last_sat - timedelta(days=1)
    prev_sat = last_sat - timedelta(days=7)
    prev_fri = prev_sat - timedelta(days=1)

    rows_a = get_full_days_for_dates([last_fri, last_sat])
    rows_b = get_full_days_for_dates([prev_fri, prev_sat])
    a = _sum_period_rows(list(rows_a.values()))
    b = _sum_period_rows(list(rows_b.values()))

    msg = (
        f"📊 Weekend Comparison (Fri + Sat)\n\n"
        f"Last weekend ({fmt_day_ddmmyyyy(last_fri)} – {fmt_day_ddmmyyyy(last_sat)}):\n"
        f"  Sales: €{a['sales']:.2f}  |  Covers: {a['covers']}\n"
        f"  Avg ticket: €{a['avg_ticket']:.2f}\n"
        f"  🌞 Lunch avg: €{a['lunch_avg']:.2f}  |  🌙 Dinner avg: €{a['dinner_avg']:.2f}\n"
        f"  💶 Tips: €{a['tips']:.2f} ({a['tips_pct']:.1f}% of sales)\n"
        f"  🚫 No-shows: {a['total_noshows']} ({a['noshow_rate']:.1f}%)\n"
        f"  Days w/ data: {a['days']}\n\n"
        f"Previous weekend ({fmt_day_ddmmyyyy(prev_fri)} – {fmt_day_ddmmyyyy(prev_sat)}):\n"
        f"  Sales: €{b['sales']:.2f}  |  Covers: {b['covers']}\n"
        f"  Avg ticket: €{b['avg_ticket']:.2f}\n"
        f"  🌞 Lunch avg: €{b['lunch_avg']:.2f}  |  🌙 Dinner avg: €{b['dinner_avg']:.2f}\n"
        f"  💶 Tips: €{b['tips']:.2f} ({b['tips_pct']:.1f}% of sales)\n"
        f"  🚫 No-shows: {b['total_noshows']} ({b['noshow_rate']:.1f}%)\n"
        f"  Days w/ data: {b['days']}\n\n"
        f"📈 vs previous weekend:\n"
        f"  Sales: {_pct_delta(a['sales'], b['sales'])}\n"
        f"  Covers: {_pct_delta(a['covers'], b['covers'])}\n"
        f"  Avg ticket: {_pct_delta(a['avg_ticket'], b['avg_ticket'])}\n"
        f"  Lunch avg: {_pct_delta(a['lunch_avg'], b['lunch_avg'])}\n"
        f"  Dinner avg: {_pct_delta(a['dinner_avg'], b['dinner_avg'])}\n"
        f"  Tips %: {_pct_delta(a['tips_pct'], b['tips_pct'])}\n"
        f"  No-show rate: {_pct_delta(a['noshow_rate'], b['noshow_rate'])}"
    )
    await update.message.reply_text(msg)

async def weekdaymix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    try:
        n_weeks = int(context.args[0]) if context.args else 8
        if n_weeks < 1 or n_weeks > 52:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /weekdaymix  or  /weekdaymix 12  (last N weeks, default 8)")
        return
    today = business_day_today()
    start = today - timedelta(weeks=n_weeks)
    p = Period(start, today)
    rows = get_full_days_in_period(p)
    if not rows:
        await update.message.reply_text(f"No data found in the last {n_weeks} weeks.")
        return

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    buckets: dict[int, list[dict]] = {i: [] for i in range(7)}
    for r in rows:
        wd = r["day"].weekday()  # Mon=0..Sun=6
        buckets[wd].append(r)

    lines = [f"📅 Weekday Mix (last {n_weeks} weeks)\n"]
    for wd in range(6):  # Mon–Sat, skip Sun
        day_rows = buckets[wd]
        if not day_rows:
            lines.append(f"{day_names[wd]}  —  no data")
            continue
        avg_sales = sum(r["total_sales"] for r in day_rows) / len(day_rows)
        avg_covers = sum(r["covers"] for r in day_rows) / len(day_rows)
        avg_ticket = (avg_sales / avg_covers) if avg_covers else 0.0
        total_noshows = sum(r.get("lunch_noshows", 0) + r.get("dinner_noshows", 0) for r in day_rows)
        total_bookings = sum(r["covers"] for r in day_rows) + total_noshows
        noshow_rate = (total_noshows / total_bookings * 100) if total_bookings else 0.0
        lines.append(
            f"{day_names[wd]}  |  Avg sales: €{avg_sales:.0f}  |  "
            f"Avg covers: {avg_covers:.0f}  |  Avg ticket: €{avg_ticket:.2f}  |  "
            f"No-show: {noshow_rate:.1f}%  ({len(day_rows)} days)"
        )
    await update.message.reply_text("\n".join(lines))

async def noshowrate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    try:
        n_weeks = int(context.args[0]) if context.args else 8
        if n_weeks < 1 or n_weeks > 52:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /noshowrate  or  /noshowrate 12  (last N weeks, default 8)")
        return
    today = business_day_today()
    start = today - timedelta(weeks=n_weeks)
    p = Period(start, today)
    rows = get_full_days_in_period(p)
    if not rows:
        await update.message.reply_text(f"No data found in the last {n_weeks} weeks.")
        return

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    buckets: dict[int, list[dict]] = {i: [] for i in range(7)}
    for r in rows:
        wd = r["day"].weekday()
        buckets[wd].append(r)

    lines = [f"🚫 No-Show Rate by Weekday (last {n_weeks} weeks)\n"]
    for wd in range(6):  # Mon–Sat
        day_rows = buckets[wd]
        if not day_rows:
            lines.append(f"{day_names[wd]}  —  no data")
            continue
        lunch_ns = sum(r.get("lunch_noshows", 0) for r in day_rows)
        dinner_ns = sum(r.get("dinner_noshows", 0) for r in day_rows)
        lunch_pax = sum(r.get("lunch_pax", 0) for r in day_rows)
        dinner_pax = sum(r.get("dinner_pax", 0) for r in day_rows)
        total_ns = lunch_ns + dinner_ns
        total_booked = lunch_pax + dinner_pax + total_ns
        overall_rate = (total_ns / total_booked * 100) if total_booked else 0.0
        lunch_rate = (lunch_ns / (lunch_pax + lunch_ns) * 100) if (lunch_pax + lunch_ns) else 0.0
        dinner_rate = (dinner_ns / (dinner_pax + dinner_ns) * 100) if (dinner_pax + dinner_ns) else 0.0
        avg_ns_per_day = total_ns / len(day_rows)
        lines.append(
            f"{day_names[wd]}  |  Overall: {overall_rate:.1f}%  |  "
            f"🌞 Lunch: {lunch_rate:.1f}%  |  🌙 Dinner: {dinner_rate:.1f}%  |  "
            f"Avg {avg_ns_per_day:.1f} no-shows/day  ({len(day_rows)} days)"
        )
    await update.message.reply_text("\n".join(lines))

GUIDED_STEPS = [
    ("day", "Day (DD/MM/YYYY or YYYY-MM-DD)?"),
    ("total_sales", "Total Sales Day?"),
    ("visa", "Visa total?"),
    ("cash", "Cash total?"),
    ("tips", "Tips total?"),
    ("lunch_sales", "Lunch sales?"),
    ("lunch_pax", "Lunch pax?"),
    ("lunch_walkins", "Lunch walk-ins?"),
    ("lunch_noshows", "Lunch no-shows?"),
    ("dinner_sales", "Dinner sales?"),
    ("dinner_pax", "Dinner pax?"),
    ("dinner_walkins", "Dinner walk-ins?"),
    ("dinner_noshows", "Dinner no-shows?"),
]

async def setfull(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_full_cmd(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    clear_mode(context.application, GUIDED_FULL_KEY, chat.id, user.id)
    set_mode(context.application, FULL_MODE_KEY, chat.id, user.id, {"on": True})
    await update.message.reply_text(
        "✅ Full daily mode ON.\nNow paste the full daily report as your NEXT message.\n\n"
        f"{FULL_EXAMPLE}\n"
        "To cancel: /cancelfull"
    )

async def setfullguided(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_full_cmd(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    clear_mode(context.application, FULL_MODE_KEY, chat.id, user.id)
    set_mode(context.application, GUIDED_FULL_KEY, chat.id, user.id, {"on": True, "step": 0, "data": {}, "awaiting_confirm": False})
    await update.message.reply_text(
        "✅ Guided full-day mode ON.\nReply to each question.\nTo cancel: /cancelfull\n\n"
        f"Q1) {GUIDED_STEPS[0][1]}"
    )

async def cancelfull(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_full_cmd(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    clear_mode(context.application, FULL_MODE_KEY, chat.id, user.id)
    clear_mode(context.application, GUIDED_FULL_KEY, chat.id, user.id)
    await update.message.reply_text("❎ Full daily mode cancelled.")

async def confirmfull(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_full_cmd(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    st = get_mode(context.application, GUIDED_FULL_KEY, chat.id, user.id)
    if not st or not st.get("on") or not st.get("awaiting_confirm"):
        await update.message.reply_text("No guided preview to confirm. Use /setfullguided.")
        return
    d = st["data"]
    covers = int(d["lunch_pax"] + d["dinner_pax"])
    upsert_full_day(
        d["day"],
        d["total_sales"], d["visa"], d["cash"], d["tips"],
        d["lunch_sales"], d["lunch_pax"], d["lunch_walkins"], d["lunch_noshows"],
        d["dinner_sales"], d["dinner_pax"], d["dinner_walkins"], d["dinner_noshows"],
    )
    upsert_daily(d["day"], float(d["total_sales"]), covers)
    clear_mode(context.application, GUIDED_FULL_KEY, chat.id, user.id)
    await update.message.reply_text(f"✅ Saved full daily report for {d['day'].isoformat()}.")

# =========================
# Owners post builder + scheduled post
# =========================
def build_owners_post_for_day(report_day: date) -> str:
    full_row = get_full_day(report_day)
    notes_texts = notes_for_day(report_day)

    notes_block = "No notes submitted."
    if notes_texts:
        notes_block = "\n\n— — —\n\n".join(notes_texts)

    if full_row:
        (
            total_sales, visa, cash, tips,
            lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
            dinner_sales, dinner_pax, dinner_walkins, dinner_noshows
        ) = full_row

        lunch_avg = (float(lunch_sales) / int(lunch_pax)) if lunch_pax else 0.0
        dinner_avg = (float(dinner_sales) / int(dinner_pax)) if dinner_pax else 0.0
        total_covers = int(lunch_pax or 0) + int(dinner_pax or 0)
        total_avg = (float(total_sales) / total_covers) if total_covers else 0.0

        msg = (
            f"📌 Norah Daily Post\n"
            f"Day: {fmt_day_ddmmyyyy(report_day)}\n"
            f"Total Sales Day: {euro_comma(total_sales)}\n"
            f"Total Covers: {total_covers}  |  Avg Ticket: {euro_comma(total_avg)}\n\n"
            f"Visa: {euro_comma(visa)}\n"
            f"Cash: {euro_comma(cash)}\n"
            f"Tips: {euro_comma(tips)}\n\n"
            f"Lunch: {euro_comma(lunch_sales)}\n"
            f"Pax: {int(lunch_pax)}\n"
            f"Avg Ticket: {euro_comma(lunch_avg)}\n"
            f"Walk in: {int(lunch_walkins)}\n"
            f"No show: {int(lunch_noshows)}\n\n"
            f"Dinner: {euro_comma(dinner_sales)}\n"
            f"Pax: {int(dinner_pax)}\n"
            f"Avg Ticket: {euro_comma(dinner_avg)}\n"
            f"Walk in: {int(dinner_walkins)}\n"
            f"No show: {int(dinner_noshows)}\n\n"
            f"📝 Notes:\n{notes_block}"
        )
    else:
        msg = (
            f"📌 Norah Daily Post\n"
            f"Day: {fmt_day_ddmmyyyy(report_day)}\n"
            f"Total Sales Day: —\n"
            f"Total Covers: —  |  Avg Ticket: —\n\n"
            f"Visa: —\n"
            f"Cash: —\n"
            f"Tips: —\n\n"
            f"Lunch: —\n"
            f"Pax: —\n"
            f"Avg Ticket: —\n"
            f"Walk in: —\n"
            f"No show: —\n\n"
            f"Dinner: —\n"
            f"Pax: —\n"
            f"Avg Ticket: —\n"
            f"Walk in: —\n"
            f"No show: —\n\n"
            f"📝 Notes:\n{notes_block}"
        )
    return msg

async def send_daily_post_to_owners(context: ContextTypes.DEFAULT_TYPE):
    chats = owners_silent_chat_ids()
    if not chats:
        return
    report_day = previous_business_day(now_local())
    msg = build_owners_post_for_day(report_day)
    for chat_id in chats:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            print(f"Daily post send failed for chat {chat_id}: {e}")

async def send_weekly_digest(context: ContextTypes.DEFAULT_TYPE):
    chats = owners_silent_chat_ids()
    if not chats:
        return
    p7 = period_ending_today("7")
    total_sales_7, total_covers_7, _ = sum_daily(p7)
    avg_ticket_7 = (total_sales_7 / total_covers_7) if total_covers_7 else 0.0
    msg = (
        f"🗓️ Norah Weekly Digest\n"
        f"Period: {p7.start.isoformat()} → {p7.end.isoformat()}\n\n"
        f"Sales: €{total_sales_7:.2f}\n"
        f"Covers: {total_covers_7}\n"
        f"Avg ticket: €{avg_ticket_7:.2f}"
    )
    for chat_id in chats:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            print(f"Weekly digest send failed for chat {chat_id}: {e}")

# =========================
# ADMIN: /postday
# =========================
async def postday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /postday YYYY-MM-DD  (or DD/MM/YYYY)")
        return
    raw = " ".join(context.args).strip()
    try:
        d = parse_any_date(raw)
    except:
        await update.message.reply_text("Usage: /postday YYYY-MM-DD  (or DD/MM/YYYY)")
        return

    chats = owners_silent_chat_ids()
    if not chats:
        await update.message.reply_text("No Owners Silent chats registered. Use /setowners or /setchatrole OWNERS_SILENT.")
        return

    msg = build_owners_post_for_day(d)
    sent = 0
    for chat_id in chats:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
            sent += 1
        except Exception as e:
            print(f"postday send failed for chat {chat_id}: {e}")

    await update.message.reply_text(f"✅ Posted owners report for {d.isoformat()} to {sent} owners chat(s).")

# =========================
# TEXT HANDLER
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    msg_text = (update.message.text or "").strip()
    if not msg_text:
        return

    role = get_chat_role(chat.id)

    # ---------------------------------------------------------
    # Auto-save FULL daily report (no /setfull) in OPS_ADMIN/MANAGER_INPUT
    # ---------------------------------------------------------
    if role in (ROLE_OPS_ADMIN, ROLE_MANAGER_INPUT):
        low = msg_text.lower()
        # allow english + spanish signals
        looks_full = (
            ("day:" in low or "día:" in low or "dia:" in low or "fecha:" in low)
            and ("total sales" in low or "ventas" in low)
            and (("lunch" in low) or ("almuerzo" in low) or ("comida" in low))
            and (("dinner" in low) or ("cena" in low))
        )
        if looks_full:
            try:
                d = parse_full_report_block(msg_text)
                covers = int(d["lunch_pax"] + d["dinner_pax"])
                upsert_full_day(
                    d["day"],
                    d["total_sales"], d["visa"], d["cash"], d["tips"],
                    d["lunch_sales"], d["lunch_pax"], d["lunch_walkins"], d["lunch_noshows"],
                    d["dinner_sales"], d["dinner_pax"], d["dinner_walkins"], d["dinner_noshows"],
                )
                upsert_daily(d["day"], float(d["total_sales"]), covers)
                await update.message.reply_text(f"✅ Saved full daily report for {d['day'].isoformat()}.")
                return
            except:
                await update.message.reply_text(
                    "❌ This looks like a full daily report, but I couldn't parse it.\n\n"
                    "Please paste it in this exact format (English or Spanish labels are OK):\n\n"
                    f"{FULL_EXAMPLE}"
                )
                return

    # ---------------------------------------------------------
    # Guided full flow
    # ---------------------------------------------------------
    st = get_mode(context.application, GUIDED_FULL_KEY, chat.id, user.id)
    if st and st.get("on"):
        if st.get("awaiting_confirm"):
            await update.message.reply_text("Please confirm with /confirmfull or cancel with /cancelfull.")
            return

        step = int(st.get("step", 0))
        field, question = GUIDED_STEPS[step]
        data = st.get("data") or {}

        try:
            if field == "day":
                data[field] = parse_any_date(msg_text)
            elif field in ("total_sales", "visa", "cash", "tips", "lunch_sales", "dinner_sales"):
                data[field] = _num(msg_text)
            else:
                data[field] = _int(msg_text)
        except:
            await update.message.reply_text(f"Couldn't understand '{msg_text}'. Try again.\n\n{question}")
            return

        step += 1
        st["data"] = data
        st["step"] = step

        if step >= len(GUIDED_STEPS):
            covers = int(data["lunch_pax"] + data["dinner_pax"])
            avg_total = (data["total_sales"] / covers) if covers else 0.0
            lunch_avg = (data["lunch_sales"] / data["lunch_pax"]) if data["lunch_pax"] else 0.0
            dinner_avg = (data["dinner_sales"] / data["dinner_pax"]) if data["dinner_pax"] else 0.0
            walkins_total = data["lunch_walkins"] + data["dinner_walkins"]
            noshows_total = data["lunch_noshows"] + data["dinner_noshows"]
            tips_pct = (data["tips"] / data["total_sales"] * 100.0) if data["total_sales"] else 0.0

            st["awaiting_confirm"] = True
            set_mode(context.application, GUIDED_FULL_KEY, chat.id, user.id, st)

            await update.message.reply_text(
                "📌 Full Day Preview\n"
                f"Day: {data['day'].isoformat()}\n\n"
                f"Total sales: €{data['total_sales']:.2f}\n"
                f"Visa: €{data['visa']:.2f}\n"
                f"Cash: €{data['cash']:.2f}\n"
                f"Tips: €{data['tips']:.2f} ({tips_pct:.1f}%)\n\n"
                f"🍽️ Lunch: €{data['lunch_sales']:.2f} | Pax {data['lunch_pax']} | Avg €{lunch_avg:.2f} | Walk-ins {data['lunch_walkins']} | No-shows {data['lunch_noshows']}\n"
                f"🌙 Dinner: €{data['dinner_sales']:.2f} | Pax {data['dinner_pax']} | Avg €{dinner_avg:.2f} | Walk-ins {data['dinner_walkins']} | No-shows {data['dinner_noshows']}\n\n"
                f"Covers total: {covers} | Avg ticket total: €{avg_total:.2f}\n"
                f"Walk-ins total: {walkins_total} | No-shows total: {noshows_total}\n\n"
                "If correct: /confirmfull\nIf not: /cancelfull"
            )
            return

        set_mode(context.application, GUIDED_FULL_KEY, chat.id, user.id, st)
        await update.message.reply_text(f"Q{step+1}) {GUIDED_STEPS[step][1]}")
        return

    # ---------------------------------------------------------
    # Paste full report flow (legacy /setfull)
    # ---------------------------------------------------------
    fm = get_mode(context.application, FULL_MODE_KEY, chat.id, user.id)
    if fm and fm.get("on"):
        try:
            d = parse_full_report_block(msg_text)
        except:
            await update.message.reply_text(
                "❌ I couldn't parse that report. Please paste again in this format:\n\n"
                f"{FULL_EXAMPLE}\n"
                "To cancel: /cancelfull"
            )
            return
        covers = int(d["lunch_pax"] + d["dinner_pax"])
        upsert_full_day(
            d["day"],
            d["total_sales"], d["visa"], d["cash"], d["tips"],
            d["lunch_sales"], d["lunch_pax"], d["lunch_walkins"], d["lunch_noshows"],
            d["dinner_sales"], d["dinner_pax"], d["dinner_walkins"], d["dinner_noshows"],
        )
        upsert_daily(d["day"], float(d["total_sales"]), covers)
        clear_mode(context.application, FULL_MODE_KEY, chat.id, user.id)
        await update.message.reply_text(f"✅ Saved full daily report for {d['day'].isoformat()}.")
        return

    # ---------------------------------------------------------
    # Auto-notes in MANAGER_INPUT: save without /report
    # ---------------------------------------------------------
    if role == ROLE_MANAGER_INPUT and not user.is_bot:
        if looks_like_notes_report(msg_text):
            d = extract_day_from_notes(msg_text) or business_day_today()
            insert_note_entry(d, chat.id, user.id, msg_text)
            detected = extract_note_tags(msg_text)
            tag_line = f"\nTags detected: {', '.join(detected)}" if detected else ""
            await update.message.reply_text(f"Saved 📝 Notes for business day {d.isoformat()}.{tag_line}")
            return

    # ---------------------------------------------------------
    # Notes capture (legacy /report mode)
    # ---------------------------------------------------------
    rm = get_mode(context.application, REPORT_MODE_KEY, chat.id, user.id)
    if rm and rm.get("on"):
        day_str = rm.get("day")
        day_ = parse_yyyy_mm_dd(day_str) if day_str else business_day_today()
        insert_note_entry(day_, chat.id, user.id, msg_text)
        clear_mode(context.application, REPORT_MODE_KEY, chat.id, user.id)
        detected = extract_note_tags(msg_text)
        tag_line = f"\nTags detected: {', '.join(detected)}" if detected else ""
        await update.message.reply_text(f"Saved 📝 Notes for business day {day_.isoformat()}.{tag_line}")
        return

    # Keep owners silent clean
    if get_chat_role(chat.id) == ROLE_OWNERS_SILENT and not user.is_bot:
        try:
            await update.message.reply_text(
                "🧾 This is the silent Owners group.\nPlease post requests in *Norah Owners Requests*.",
                parse_mode="Markdown",
            )
        except:
            pass
        return

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

    app.add_handler(CommandHandler("setchatrole", setchatrole_cmd))
    app.add_handler(CommandHandler("chats", chats_cmd))
    app.add_handler(CommandHandler("setowners", setowners))
    app.add_handler(CommandHandler("ownerslist", ownerslist))
    app.add_handler(CommandHandler("removeowners", removeowners))

    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("resetdb", resetdb_cmd))
    app.add_handler(CommandHandler("deleteday", deleteday_cmd))

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
    app.add_handler(CommandHandler("tagstats", tagstats_cmd))
    app.add_handler(CommandHandler("staffnotes", staffnotes_cmd))

    # Full daily
    app.add_handler(CommandHandler("setfull", setfull))
    app.add_handler(CommandHandler("setfullguided", setfullguided))
    app.add_handler(CommandHandler("confirmfull", confirmfull))
    app.add_handler(CommandHandler("cancelfull", cancelfull))

    # Admin repost
    app.add_handler(CommandHandler("postday", postday))

    # New analytics commands
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("yesterday", yesterday_cmd))
    app.add_handler(CommandHandler("dow", dow_cmd))
    app.add_handler(CommandHandler("weekcompare", weekcompare_cmd))
    app.add_handler(CommandHandler("monthcompare", monthcompare_cmd))
    app.add_handler(CommandHandler("weekendcompare", weekendcompare_cmd))
    app.add_handler(CommandHandler("weekdaymix", weekdaymix_cmd))
    app.add_handler(CommandHandler("noshowrate", noshowrate_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if app.job_queue is not None:
        app.job_queue.run_daily(
            send_weekly_digest,
            time=time(hour=WEEKLY_DIGEST_HOUR, minute=0, tzinfo=TZ),
            days=(0,),
            name="weekly_digest_monday",
        )
        app.job_queue.run_daily(
            send_daily_post_to_owners,
            time=time(hour=DAILY_POST_HOUR, minute=DAILY_POST_MINUTE, tzinfo=TZ),
            name="daily_post_to_owners",
        )

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
