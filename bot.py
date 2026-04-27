import json
import os
import re
import threading
import time as time_mod
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo
from collections import Counter

import psycopg
from flask import Flask, jsonify, request, send_file, make_response, redirect
from flask_cors import CORS
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
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

TZ_NAME = (os.getenv("TZ_NAME") or os.getenv("TIMEZONE") or "Europe/Madrid").strip() or "Europe/Madrid"
CUTOFF_HOUR = int((os.getenv("CUTOFF_HOUR", "11").strip() or "11"))
WEEKLY_DIGEST_HOUR = int((os.getenv("WEEKLY_DIGEST_HOUR", "12").strip() or "12"))

DAILY_POST_HOUR = int((os.getenv("DAILY_POST_HOUR", "11").strip() or "11"))
DAILY_POST_MINUTE = int((os.getenv("DAILY_POST_MINUTE", "5").strip() or "5"))

ALERT_NOSHOW_MULTIPLIER        = float((os.getenv("ALERT_NOSHOW_MULTIPLIER",        "2.0").strip() or "2.0"))
ALERT_SERVICE_IMBALANCE_PCT    = float((os.getenv("ALERT_SERVICE_IMBALANCE_PCT",    "65").strip()  or "65"))
ALERT_REVENUE_VS_COVERS_DROP_PCT = float((os.getenv("ALERT_REVENUE_VS_COVERS_DROP_PCT", "20").strip() or "20"))
ALERT_TIPS_DROP_PCT            = float((os.getenv("ALERT_TIPS_DROP_PCT",            "30").strip()  or "30"))
ALERT_TICKET_EROSION_DAYS      = int((os.getenv("ALERT_TICKET_EROSION_DAYS",        "3").strip()   or "3"))
ALERT_STRONG_DAY_MISS_PCT      = float((os.getenv("ALERT_STRONG_DAY_MISS_PCT",      "25").strip()  or "25"))
ALERT_WEEK_PACE_PCT            = float((os.getenv("ALERT_WEEK_PACE_PCT",            "25").strip()  or "25"))
ALERT_POSITIVE_REVENUE_PCT     = float((os.getenv("ALERT_POSITIVE_REVENUE_PCT",     "15").strip()  or "15"))
ALERT_POSITIVE_COVERS_PCT      = float((os.getenv("ALERT_POSITIVE_COVERS_PCT",      "10").strip()  or "10"))
ALERT_TOP_PERCENTILE           = float((os.getenv("ALERT_TOP_PERCENTILE",           "10").strip()  or "10"))
ALERT_EVENING_HOUR             = int((os.getenv("ALERT_EVENING_HOUR",               "21").strip()  or "21"))
ALERT_LUNCH_TICKET_MIN = float((os.getenv("ALERT_LUNCH_TICKET_MIN", "35").strip() or "35"))
DASHBOARD_API_KEY  = os.getenv("DASHBOARD_API_KEY",  "").strip()
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()

AGORA_URL      = os.getenv("AGORA_URL",      "").strip()
AGORA_USER     = os.getenv("AGORA_USER",     "").strip()
AGORA_PASSWORD = os.getenv("AGORA_PASSWORD", "").strip()

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

try:
    import agora_integration as _agora_mod
    # Propagate env vars so the module picks up bot.py's config
    _agora_mod.AGORA_URL      = AGORA_URL
    _agora_mod.AGORA_USER     = AGORA_USER
    _agora_mod.AGORA_PASSWORD = AGORA_PASSWORD
    _AGORA_AVAILABLE = True
except Exception:
    _AGORA_AVAILABLE = False

COVERMANAGER_API_KEY    = os.getenv("COVERMANAGER_API_KEY",    "").strip()
COVERMANAGER_RESTAURANT = os.getenv("COVERMANAGER_RESTAURANT", "Restaurante-Norah").strip()

try:
    import covermanager_integration as _cm_mod
    _cm_mod.COVERMANAGER_API_KEY    = COVERMANAGER_API_KEY or _cm_mod.COVERMANAGER_API_KEY
    _cm_mod.COVERMANAGER_RESTAURANT = COVERMANAGER_RESTAURANT
    _CM_AVAILABLE = True
except Exception:
    _CM_AVAILABLE = False

def _try_agora(day_: date):
    """Fetch Agora POS sales for a date. Returns DailySales or None on any error."""
    if not _AGORA_AVAILABLE or not AGORA_USER or not AGORA_PASSWORD:
        return None
    try:
        return _agora_mod.get_daily_sales(day_)
    except Exception as e:
        print(f"Agora fetch failed for {day_}: {e}")
        return None


def _try_cm_walkins_noshows(day_: date) -> dict:
    """
    Fetch walk-ins and no-shows from CoverManager for a single date.
    Returns a dict with keys: lunch_walkins, dinner_walkins, lunch_noshows, dinner_noshows.
    Falls back to zeros on any error or if CM is unavailable.
    """
    zeros = {"lunch_walkins": 0, "dinner_walkins": 0, "lunch_noshows": 0, "dinner_noshows": 0}
    if not _CM_AVAILABLE:
        return zeros
    try:
        res = _cm_mod.get_daily_reservations(day_)
        if not res:
            return zeros

        _LUNCH  = {"comida", "almuerzo", "mediodía", "mediodia"}
        _DINNER = {"cena", "noche", "tarde"}

        lunch_walkins = dinner_walkins = lunch_noshows = dinner_noshows = 0
        for r in res.reservations:
            status = int(r.get("status", 0))
            shift  = (r.get("meal_shift") or "").strip().lower()
            prov   = (r.get("provenance") or "").strip().lower()
            is_lunch  = any(w in shift for w in _LUNCH)
            is_dinner = any(w in shift for w in _DINNER)

            if prov == "walk in":
                if is_lunch:
                    lunch_walkins += 1
                elif is_dinner:
                    dinner_walkins += 1

            if status == -2:  # STATUS_NOSHOW
                if is_lunch:
                    lunch_noshows += 1
                elif is_dinner:
                    dinner_noshows += 1

        return {
            "lunch_walkins":  lunch_walkins,
            "dinner_walkins": dinner_walkins,
            "lunch_noshows":  lunch_noshows,
            "dinner_noshows": dinner_noshows,
        }
    except Exception as e:
        print(f"CM walkins/noshows fetch failed for {day_}: {e}")
        return zeros


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
    tl = (text or "").lower()
    found = []
    for canonical, aliases in NOTE_TAGS.items():
        if any(alias in tl for alias in aliases):
            found.append(canonical)
    return found

def extract_tag_content(text: str, tag: str) -> str:
    tl = text.lower()
    aliases = NOTE_TAGS.get(tag, [])
    for alias in aliases:
        idx = tl.find(alias)
        if idx != -1:
            return text[idx + len(alias):].strip()
    return text.strip()

def notes_have_any_tag(rows: list[tuple]) -> bool:
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

def get_all_historical_sales() -> list[float]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT total_sales FROM full_daily_stats "
                "WHERE total_sales IS NOT NULL ORDER BY total_sales ASC;"
            )
            rows = cur.fetchall()
    return [float(r[0]) for r in rows]

def get_all_historical_covers() -> list[int]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(lunch_pax, 0) + COALESCE(dinner_pax, 0)
                FROM full_daily_stats
                ORDER BY 1 ASC;
                """
            )
            rows = cur.fetchall()
    return [int(r[0]) for r in rows]

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

    day_str = find_line(["Day", "Día", "Dia", "Fecha"])
    if not day_str:
        raise ValueError("Missing Day")
    day_ = parse_any_date(day_str)

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

            if any(low.startswith(x.lower() + ":") for x in ["dinner", "cena", "lunch", "almuerzo", "comida"]):
                break

            if low.startswith("average pax") or low.startswith("avg pax") or low.startswith("avg ticket") or low.startswith("average ticket") or low.startswith("media pax") or low.startswith("ticket medio"):
                continue

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
# NOTES: auto-detect manager report blocks
# =========================
NOTES_HINTS = [
    "incidents", "incident", "staff", "sold out", "sold-out", "complaints",
    "incidencias", "incidencia", "personal", "agotado", "agotados", "quejas", "queja",
]

def extract_day_from_notes(text: str) -> date | None:
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
    if hits >= 2:
        return True
    if hits >= 1 and ("\n" in t):
        return True
    return False

# =========================
# AI AGENT (OWNERS_REQUESTS)
# =========================

AGENT_TOOLS = [
    {
        "name": "get_today",
        "description": "Get today's full sales and operational data (current business day).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_yesterday",
        "description": "Get yesterday's full sales and operational data (previous business day).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_specific_day",
        "description": "Get full sales and operational data for a specific date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"}
            },
            "required": ["date"],
        },
    },
    {
        "name": "get_period_summary",
        "description": "Get aggregated sales and operational summary for a date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_week_comparison",
        "description": "Compare this week's performance vs the same period last week.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_month_comparison",
        "description": "Compare this month's performance vs the same period last month.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_weekend_comparison",
        "description": "Compare last weekend (Fri+Sat) vs the previous weekend.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_weekday_history",
        "description": "Get historical data for a specific day of the week (e.g., all recent Tuesdays). Use this to answer questions like 'how do our Fridays compare' or 'what's our typical Tuesday like'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "weekday": {
                    "type": "integer",
                    "description": "ISO weekday: 1=Monday, 2=Tuesday, 3=Wednesday, 4=Thursday, 5=Friday, 6=Saturday, 7=Sunday",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of past occurrences to return (default 6, max 12)",
                },
            },
            "required": ["weekday"],
        },
    },
    {
        "name": "get_notes",
        "description": "Get manager operational notes for a date range. Notes may contain incidents, complaints, sold-out items, staff issues, and other operational observations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "End date YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_reservations",
        "description": (
            "Get live reservation data from CoverManager for a date range. "
            "Use this for any question about upcoming or recent reservations, covers, "
            "no-shows, large groups, or booking counts by service (lunch/dinner). "
            "Returns per-day breakdown: total covers, lunch/dinner covers, confirmed, "
            "no-shows, cancelled, and any large groups (6+ pax) with their time and shift."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_date":   {"type": "string", "description": "End date YYYY-MM-DD"},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_booking_sources",
        "description": (
            "Analyse where bookings come from (Google, own website, Instagram, walk-in, etc.) "
            "for a date range. Use this for questions about booking source trends, channel "
            "performance, whether Google or the website is performing better, Instagram growth, etc. "
            "Default period is last 30 days. Can group results by week or month for trend analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_date":   {"type": "string", "description": "End date YYYY-MM-DD"},
                "group_by":   {
                    "type": "string",
                    "description": "Granularity for trend breakdown: 'total' (default), 'week', or 'month'",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_guest_intelligence",
        "description": (
            "Analyse guest behaviour and loyalty from CoverManager reservation history. "
            "Use this for questions about loyal/frequent guests, no-show offenders, "
            "dinner-only regulars, lapsed guests, or large-group regulars. "
            "Default period is last 6 months. "
            "query options: 'top_guests' (most frequent visitors), "
            "'noshows' (guests with repeated no-shows), "
            "'dinner_only' (guests who never visit for lunch), "
            "'lunch_only' (guests who never visit for dinner), "
            "'lapsed' (regulars who haven't visited recently), "
            "'large_groups' (guests who consistently book 6+ pax)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD"},
                "end_date":   {"type": "string", "description": "End date YYYY-MM-DD"},
                "query":      {
                    "type": "string",
                    "description": "Type of analysis: top_guests | noshows | dinner_only | lunch_only | lapsed | large_groups",
                },
                "top_n":      {"type": "integer", "description": "How many results to return (default 10)"},
            },
            "required": ["start_date", "end_date", "query"],
        },
    },
]


def _agent_row_to_dict(row, day_: date, label: str = "") -> dict:
    (total_sales, visa, cash, tips,
     lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
     dinner_sales, dinner_pax, dinner_walkins, dinner_noshows) = row
    covers = int((lunch_pax or 0) + (dinner_pax or 0))
    lunch_pax = int(lunch_pax or 0)
    dinner_pax = int(dinner_pax or 0)
    return {
        "date": day_.isoformat(),
        **({"label": label} if label else {}),
        "total_sales": float(total_sales or 0),
        "visa": float(visa or 0),
        "cash": float(cash or 0),
        "tips": float(tips or 0),
        "covers": covers,
        "avg_ticket": (float(total_sales or 0) / covers) if covers else 0.0,
        "lunch_sales": float(lunch_sales or 0),
        "lunch_pax": lunch_pax,
        "lunch_walkins": int(lunch_walkins or 0),
        "lunch_noshows": int(lunch_noshows or 0),
        "lunch_avg": (float(lunch_sales or 0) / lunch_pax) if lunch_pax else 0.0,
        "dinner_sales": float(dinner_sales or 0),
        "dinner_pax": dinner_pax,
        "dinner_walkins": int(dinner_walkins or 0),
        "dinner_noshows": int(dinner_noshows or 0),
        "dinner_avg": (float(dinner_sales or 0) / dinner_pax) if dinner_pax else 0.0,
    }


def _exec_get_today() -> dict:
    day_ = business_day_today()
    row = get_full_day(day_)
    if not row:
        return {"error": f"No data for today ({day_.isoformat()}) yet."}
    return _agent_row_to_dict(row, day_, "today")


def _exec_get_yesterday() -> dict:
    day_ = previous_business_day()
    row = get_full_day(day_)
    if not row:
        return {"error": f"No data for yesterday ({day_.isoformat()}) yet."}
    return _agent_row_to_dict(row, day_, "yesterday")


def _exec_get_specific_day(date_str: str) -> dict:
    try:
        day_ = parse_yyyy_mm_dd(date_str)
    except Exception:
        return {"error": f"Invalid date: {date_str}. Use YYYY-MM-DD."}
    row = get_full_day(day_)
    if not row:
        return {"error": f"No data for {date_str}."}
    return _agent_row_to_dict(row, day_)


def _exec_get_period_summary(start_date: str, end_date: str) -> dict:
    try:
        start = parse_yyyy_mm_dd(start_date)
        end = parse_yyyy_mm_dd(end_date)
    except Exception:
        return {"error": "Invalid date format. Use YYYY-MM-DD."}
    p = Period(start, end)
    agg = sum_full_in_period(p)
    covers = agg["lunch_pax"] + agg["dinner_pax"]
    return {
        "start_date": start_date,
        "end_date": end_date,
        "days_with_data": agg["full_days"],
        "total_sales": agg["total_sales"],
        "tips": agg["tips"],
        "covers": covers,
        "avg_ticket": (agg["total_sales"] / covers) if covers else 0.0,
        "lunch_sales": agg["lunch_sales"],
        "lunch_pax": agg["lunch_pax"],
        "lunch_avg": (agg["lunch_sales"] / agg["lunch_pax"]) if agg["lunch_pax"] else 0.0,
        "lunch_noshows": agg["lunch_noshows"],
        "dinner_sales": agg["dinner_sales"],
        "dinner_pax": agg["dinner_pax"],
        "dinner_avg": (agg["dinner_sales"] / agg["dinner_pax"]) if agg["dinner_pax"] else 0.0,
        "dinner_noshows": agg["dinner_noshows"],
        "total_noshows": agg["lunch_noshows"] + agg["dinner_noshows"],
    }


def _exec_get_week_comparison() -> dict:
    today = business_day_today()
    this_mon = _last_monday(today)
    last_mon = this_mon - timedelta(days=7)
    last_equiv = today - timedelta(days=7)
    a = _sum_period_rows(get_full_days_in_period(Period(this_mon, today)))
    b = _sum_period_rows(get_full_days_in_period(Period(last_mon, last_equiv)))
    return {
        "this_week": {"start": this_mon.isoformat(), "end": today.isoformat(), **a},
        "last_week": {"start": last_mon.isoformat(), "end": last_equiv.isoformat(), **b},
    }


def _exec_get_month_comparison() -> dict:
    today = business_day_today()
    this_start = date(today.year, today.month, 1)
    last_start = add_months(this_start, -1)
    last_equiv = add_months(today, -1)
    a = _sum_period_rows(get_full_days_in_period(Period(this_start, today)))
    b = _sum_period_rows(get_full_days_in_period(Period(last_start, last_equiv)))
    return {
        "this_month": {"start": this_start.isoformat(), "end": today.isoformat(), **a},
        "last_month": {"start": last_start.isoformat(), "end": last_equiv.isoformat(), **b},
    }


def _exec_get_weekend_comparison() -> dict:
    today = business_day_today()
    days_since_sat = (today.weekday() - 5) % 7
    last_sat = today - timedelta(days=days_since_sat)
    last_fri = last_sat - timedelta(days=1)
    prev_sat = last_sat - timedelta(days=7)
    prev_fri = prev_sat - timedelta(days=1)
    a = _sum_period_rows(list(get_full_days_for_dates([last_fri, last_sat]).values()))
    b = _sum_period_rows(list(get_full_days_for_dates([prev_fri, prev_sat]).values()))
    return {
        "last_weekend": {"fri": last_fri.isoformat(), "sat": last_sat.isoformat(), **a},
        "prev_weekend": {"fri": prev_fri.isoformat(), "sat": prev_sat.isoformat(), **b},
    }


def _exec_get_weekday_history(weekday: int, limit: int = 6) -> dict:
    limit = min(max(1, limit), 12)
    today = business_day_today()
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_name = day_names[weekday - 1] if 1 <= weekday <= 7 else "Unknown"
    rows = get_full_days_for_weekday(weekday, today, limit)
    return {"weekday": weekday, "weekday_name": day_name, "entries": rows}


def _exec_get_notes(start_date: str, end_date: str) -> dict:
    try:
        start = parse_yyyy_mm_dd(start_date)
        end = parse_yyyy_mm_dd(end_date)
    except Exception:
        return {"error": "Invalid date format. Use YYYY-MM-DD."}
    rows = notes_in_period(Period(start, end))
    entries = [
        {"date": d.isoformat(), "tags": extract_note_tags(txt), "text": txt[:600]}
        for d, txt in rows[-20:]
    ]
    return {"start_date": start_date, "end_date": end_date, "total_notes": len(rows), "entries": entries}


def _exec_get_reservations(start_date: str, end_date: str) -> dict:
    if not _CM_AVAILABLE:
        return {"error": "CoverManager integration not available."}
    try:
        start = parse_yyyy_mm_dd(start_date)
        end   = parse_yyyy_mm_dd(end_date)
    except Exception:
        return {"error": "Invalid date format. Use YYYY-MM-DD."}
    try:
        rows = _cm_mod.get_reservations_range(start, end)
    except RuntimeError as e:
        return {"error": str(e)}
    if not rows:
        return {"start_date": start_date, "end_date": end_date, "days": [], "note": "No reservations found for this period."}
    return {"start_date": start_date, "end_date": end_date, "days": rows}


def _last_record_date(records: list):
    """Return the latest date found in a list of CoverManager record dicts, or None."""
    dates = [r.get("date", "") for r in records if r.get("date")]
    if not dates:
        return None
    try:
        return date.fromisoformat(max(dates))
    except Exception:
        return None


def _monthly_chunks(start, end):
    """Yield (chunk_start, chunk_end) date pairs, month by month."""
    import calendar
    current = start
    while current <= end:
        # Last day of current month
        last_day = calendar.monthrange(current.year, current.month)[1]
        chunk_end = min(date(current.year, current.month, last_day), end)
        yield current, chunk_end
        # Advance to first day of next month
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def _fetch_cm_records_chunked(start, end, timeout_sec: float = 45):
    """
    Fetch raw CoverManager records for start..end using monthly chunks.
    Returns (records, partial, covered_through) where:
      - records: list of raw dicts collected so far
      - partial: True if we hit the timeout before finishing
      - covered_through: last chunk_end that completed (date), or None
    """
    import time as _time
    deadline = _time.monotonic() + timeout_sec
    all_records = []
    covered_through = None

    num_months = (end.year - start.year) * 12 + (end.month - start.month) + 1
    use_chunks = num_months > 2

    if not use_chunks:
        records = _cm_mod.get_raw_records(start, end)
        return records, False, end

    for chunk_start, chunk_end in _monthly_chunks(start, end):
        if _time.monotonic() >= deadline:
            # Use actual last record date, not chunk boundary
            actual_last = _last_record_date(all_records) or covered_through
            return all_records, True, actual_last
        batch = _cm_mod.get_raw_records(chunk_start, chunk_end)
        all_records.extend(batch)
        covered_through = chunk_end

    return all_records, False, end


def _classify_channel(r: dict) -> str:
    origin = (r.get("origin") or "").strip().lower()
    prov   = (r.get("provenance") or "").strip().lower()
    if prov == "walk in":         return "Walk-in"
    if "instagram" in origin:     return "Instagram"
    if origin == "google":        return "Google"
    if prov == "moduloweb":       return "Own website"
    if prov == "app-movil":       return "Mobile app"
    if prov == "waitinglist":     return "Waiting list"
    if prov == "software":        return "Staff/software"
    if prov == "terceros":        return "Third-party"
    return "Other"


def _exec_get_booking_sources(start_date: str, end_date: str, group_by: str = "total") -> dict:
    if not _CM_AVAILABLE:
        return {"error": "CoverManager integration not available."}
    try:
        start = parse_yyyy_mm_dd(start_date)
        end   = parse_yyyy_mm_dd(end_date)
    except Exception:
        return {"error": "Invalid date format. Use YYYY-MM-DD."}
    try:
        records, partial, covered_through = _fetch_cm_records_chunked(start, end)
    except Exception as e:
        return {"error": str(e)}

    if not records:
        return {"start_date": start_date, "end_date": end_date, "note": "No reservations found.", "totals": {}}

    partial_note = (f" (partial data through {covered_through})" if partial and covered_through else "")

    if group_by in ("week", "month"):
        from collections import defaultdict as _dd
        buckets = _dd(lambda: _dd(int))
        for r in records:
            d = r.get("date", "")
            if not d:
                continue
            try:
                rd = date.fromisoformat(d)
                if group_by == "week":
                    mon = rd - timedelta(days=rd.weekday())
                    key = mon.isoformat()
                else:
                    key = d[:7]  # YYYY-MM
            except Exception:
                continue
            buckets[key][_classify_channel(r)] += 1

        breakdown = []
        for period_key in sorted(buckets.keys()):
            counts = dict(buckets[period_key])
            total  = sum(counts.values())
            breakdown.append({
                "period": period_key,
                "total":  total,
                "channels": {ch: {"count": cnt, "pct": round(cnt / total * 100, 1)}
                             for ch, cnt in sorted(counts.items(), key=lambda x: -x[1])},
            })
        result = {"start_date": start_date, "end_date": end_date,
                  "group_by": group_by, "periods": breakdown}
        if partial_note:
            result["note"] = f"Showing partial results{partial_note}"
        return result
    else:
        from collections import Counter as _Ctr
        counts = _Ctr(_classify_channel(r) for r in records)
        total  = sum(counts.values())
        result = {
            "start_date": start_date, "end_date": end_date,
            "total_bookings": total,
            "channels": {ch: {"count": cnt, "pct": round(cnt / total * 100, 1)}
                         for ch, cnt in counts.most_common()},
        }
        if partial_note:
            result["note"] = f"Showing partial results{partial_note}"
        return result


def _exec_get_guest_intelligence(start_date: str, end_date: str,
                                  query: str, top_n: int = 10) -> dict:
    if not _CM_AVAILABLE:
        return {"error": "CoverManager integration not available."}
    try:
        start = parse_yyyy_mm_dd(start_date)
        end   = parse_yyyy_mm_dd(end_date)
    except Exception:
        return {"error": "Invalid date format. Use YYYY-MM-DD."}
    try:
        records, partial, covered_through = _fetch_cm_records_chunked(start, end)
    except Exception as e:
        return {"error": str(e)}

    if not records:
        return {"error": "No reservation data found for this period."}

    partial_note = (f"Showing partial results through {covered_through}" if partial and covered_through else None)

    # Status codes: "1","2","3","5" = active visit; "-2" = noshow; "-3","-5","-1" = cancel
    ACTIVE  = {"1", "2", "3", "5"}
    NOSHOW  = {"-2"}

    from collections import defaultdict as _dd
    guests = _dd(lambda: {"name": "", "visits": 0, "noshows": 0,
                           "pax_total": 0, "lunch": 0, "dinner": 0, "dates": []})

    for r in records:
        cid    = r.get("id_client") or r.get("id_reserv", "?")
        status = str(r.get("status", "0"))
        pax    = int(r.get("for", 0) or 0)
        shift  = (r.get("meal_shift") or "").lower()
        d      = r.get("date", "")
        name   = ((r.get("first_name", "") + " " + r.get("last_name", "")).strip()
                  or r.get("user_name", "—"))

        g = guests[cid]
        if not g["name"] and name not in ("—", ""):
            g["name"] = name

        if status in ACTIVE:
            g["visits"]    += 1
            g["pax_total"] += pax
            g["dates"].append(d)
            if any(w in shift for w in ("comida", "almuerzo")):
                g["lunch"]  += 1
            elif any(w in shift for w in ("cena", "noche")):
                g["dinner"] += 1
        elif status in NOSHOW:
            g["noshows"] += 1

    today_str = date.today().isoformat()

    if query == "top_guests":
        ranked = sorted(guests.values(), key=lambda g: -g["visits"])
        result = []
        for g in ranked[:top_n]:
            if g["visits"] == 0:
                continue
            last = max(g["dates"]) if g["dates"] else "—"
            avg  = round(g["pax_total"] / g["visits"], 1)
            pref = ("Lunch" if g["lunch"] > g["dinner"] else
                    "Dinner" if g["dinner"] > g["lunch"] else "Both")
            result.append({"name": g["name"], "visits": g["visits"],
                           "last_visit": last, "avg_pax": avg, "preferred_shift": pref,
                           "lunch_visits": g["lunch"], "dinner_visits": g["dinner"]})
        ret = {"query": query, "start_date": start_date, "end_date": end_date,
               "top_guests": result}
        if partial_note: ret["note"] = partial_note
        return ret

    elif query == "noshows":
        offenders = sorted(
            [g for g in guests.values() if g["noshows"] >= 2],
            key=lambda g: -g["noshows"]
        )
        result = [{"name": g["name"], "noshows": g["noshows"],
                   "actual_visits": g["visits"]}
                  for g in offenders]
        ret = {"query": query, "start_date": start_date, "end_date": end_date,
               "noshow_offenders": result,
               "total_with_2plus_noshows": len(result)}
        if partial_note: ret["note"] = partial_note
        return ret

    elif query == "dinner_only":
        dinner_only = sorted(
            [g for g in guests.values() if g["dinner"] >= 2 and g["lunch"] == 0],
            key=lambda g: -g["dinner"]
        )[:top_n]
        result = []
        for g in dinner_only:
            last = max(g["dates"]) if g["dates"] else "—"
            avg  = round(g["pax_total"] / g["visits"], 1) if g["visits"] else 0
            result.append({"name": g["name"], "dinner_visits": g["dinner"],
                           "avg_pax": avg, "last_visit": last})
        ret = {"query": query, "start_date": start_date, "end_date": end_date,
               "dinner_only_guests": result}
        if partial_note: ret["note"] = partial_note
        return ret

    elif query == "lunch_only":
        lunch_only = sorted(
            [g for g in guests.values() if g["lunch"] >= 2 and g["dinner"] == 0],
            key=lambda g: -g["lunch"]
        )[:top_n]
        result = []
        for g in lunch_only:
            last = max(g["dates"]) if g["dates"] else "—"
            avg  = round(g["pax_total"] / g["visits"], 1) if g["visits"] else 0
            result.append({"name": g["name"], "lunch_visits": g["lunch"],
                           "avg_pax": avg, "last_visit": last})
        ret = {"query": query, "start_date": start_date, "end_date": end_date,
               "lunch_only_guests": result}
        if partial_note: ret["note"] = partial_note
        return ret

    elif query == "lapsed":
        # Regulars (2+ visits) whose last visit was 60+ days ago
        cutoff = (date.today() - timedelta(days=60)).isoformat()
        lapsed = sorted(
            [g for g in guests.values()
             if g["visits"] >= 2 and g["dates"] and max(g["dates"]) < cutoff],
            key=lambda g: max(g["dates"])
        )[:top_n]
        result = []
        for g in lapsed:
            last = max(g["dates"])
            avg  = round(g["pax_total"] / g["visits"], 1) if g["visits"] else 0
            result.append({"name": g["name"], "visits": g["visits"],
                           "last_visit": last, "avg_pax": avg})
        ret = {"query": query, "cutoff_days": 60,
               "start_date": start_date, "end_date": end_date,
               "lapsed_guests": result}
        if partial_note: ret["note"] = partial_note
        return ret

    elif query == "large_groups":
        large = sorted(
            [g for g in guests.values()
             if g["visits"] >= 2 and g["pax_total"] / g["visits"] >= 5],
            key=lambda g: -(g["pax_total"] / g["visits"])
        )[:top_n]
        result = []
        for g in large:
            last = max(g["dates"]) if g["dates"] else "—"
            avg  = round(g["pax_total"] / g["visits"], 1)
            result.append({"name": g["name"], "visits": g["visits"],
                           "avg_pax": avg, "last_visit": last})
        ret = {"query": query, "start_date": start_date, "end_date": end_date,
               "large_group_guests": result}
        if partial_note: ret["note"] = partial_note
        return ret

    else:
        return {"error": f"Unknown query type: {query}. Use: top_guests, noshows, dinner_only, lunch_only, lapsed, large_groups"}


def execute_agent_tool(tool_name: str, tool_input: dict) -> str:
    try:
        if tool_name == "get_today":
            result = _exec_get_today()
        elif tool_name == "get_yesterday":
            result = _exec_get_yesterday()
        elif tool_name == "get_specific_day":
            result = _exec_get_specific_day(tool_input.get("date", ""))
        elif tool_name == "get_period_summary":
            result = _exec_get_period_summary(
                tool_input.get("start_date", ""), tool_input.get("end_date", "")
            )
        elif tool_name == "get_week_comparison":
            result = _exec_get_week_comparison()
        elif tool_name == "get_month_comparison":
            result = _exec_get_month_comparison()
        elif tool_name == "get_weekend_comparison":
            result = _exec_get_weekend_comparison()
        elif tool_name == "get_weekday_history":
            result = _exec_get_weekday_history(
                int(tool_input.get("weekday", 1)), int(tool_input.get("limit", 6))
            )
        elif tool_name == "get_notes":
            result = _exec_get_notes(
                tool_input.get("start_date", ""), tool_input.get("end_date", "")
            )
        elif tool_name == "get_reservations":
            result = _exec_get_reservations(
                tool_input.get("start_date", ""), tool_input.get("end_date", "")
            )
        elif tool_name == "get_booking_sources":
            result = _exec_get_booking_sources(
                tool_input.get("start_date", ""), tool_input.get("end_date", ""),
                tool_input.get("group_by", "total"),
            )
        elif tool_name == "get_guest_intelligence":
            result = _exec_get_guest_intelligence(
                tool_input.get("start_date", ""), tool_input.get("end_date", ""),
                tool_input.get("query", "top_guests"),
                int(tool_input.get("top_n", 10)),
            )
        else:
            result = {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        result = {"error": str(e)}
    return json.dumps(result, default=str)


def _build_agent_system_prompt() -> str:
    # Use actual wall-clock date for calendar/weekday context, not business_day_today()
    # (business_day_today returns yesterday before CUTOFF_HOUR, which breaks weekday arithmetic)
    now        = now_local()
    cal_today  = now.date()
    biz_today  = business_day_today()

    # Build explicit Mon–Sun date map for the current ISO week so the model never
    # has to calculate weekday offsets itself
    monday = cal_today - timedelta(days=cal_today.weekday())  # weekday() 0=Mon
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    week_lines = "  ".join(
        f"{day_names[i]}: {(monday + timedelta(days=i)).isoformat()}"
        + (" ← today" if (monday + timedelta(days=i)) == cal_today else "")
        for i in range(7)
    )

    return (
        f"You are Norah Ops, the analytics assistant for Norah, a restaurant in Madrid, Spain.\n\n"
        f"Current date and time: {cal_today.isoformat()} ({cal_today.strftime('%A, %d %B %Y')}, {now.strftime('%H:%M')} Madrid time).\n"
        f"Current business day (for historical sales data): {biz_today.isoformat()}.\n\n"
        f"This week's dates for quick reference:\n{week_lines}\n\n"
        "You have access to the restaurant's operational database with:\n"
        "- Daily sales (total, visa, cash, tips)\n"
        "- Covers split by lunch and dinner\n"
        "- Average ticket (overall, lunch, dinner)\n"
        "- Walk-ins and no-shows per service\n"
        "- Manager operational notes (incidents, complaints, sold-out items, staff issues)\n"
        "- Live reservation data from CoverManager (upcoming and past bookings, covers, no-shows, large groups)\n"
        "- Booking source analytics (Google, own website, Instagram, walk-in, etc.) via CoverManager\n"
        "- Guest intelligence: loyal guests, no-show offenders, dinner-only regulars, lapsed guests\n\n"
        "Tool selection guide:\n"
        "- Upcoming/recent reservations, covers, large groups → get_reservations\n"
        "- Where bookings come from, channel trends, Google vs website → get_booking_sources\n"
        "- Loyal guests, frequent visitors, no-shows, dinner-only, lapsed → get_guest_intelligence\n\n"
        "Be concise and analytical. Format currency as €X.XX. Always mention which date(s) the data refers to.\n"
        "If data is missing for a requested period, say so clearly.\n\n"
        "IMPORTANT: Detect the language of the user's message and always respond in that same language "
        "(English, Spanish, or Russian). If unclear, default to English."
    )


async def handle_agent_query(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if not ANTHROPIC_API_KEY:
        await update.message.reply_text("⚠️ AI agent not configured (missing ANTHROPIC_API_KEY).")
        return

    import anthropic as _anthropic
    client = _anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    messages = [{"role": "user", "content": text}]

    _HEAVY_TOOLS = {"get_booking_sources", "get_guest_intelligence"}
    _fetching_sent = False

    try:
        for _ in range(6):  # max iterations to prevent runaway loops
            response = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                system=_build_agent_system_prompt(),
                tools=AGENT_TOOLS,
                messages=messages,
            )

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            if response.stop_reason == "end_turn" or not tool_uses:
                final_text = "\n".join(b.text for b in text_blocks).strip()
                if final_text:
                    await update.message.reply_text(final_text)
                return

            # Warn user before heavy CoverManager fetches (only once per query)
            if not _fetching_sent and any(tu.name in _HEAVY_TOOLS for tu in tool_uses):
                await update.message.reply_text("⏳ Fetching data from CoverManager…")
                _fetching_sent = True

            # Execute all tool calls and continue the loop
            messages.append({"role": "assistant", "content": response.content})
            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": execute_agent_tool(tu.name, tu.input),
                }
                for tu in tool_uses
            ]
            messages.append({"role": "user", "content": tool_results})

        await update.message.reply_text("⚠️ Could not complete the request. Please try rephrasing.")

    except Exception as e:
        print(f"Agent error: {e}")
        await update.message.reply_text("⚠️ Something went wrong. Please try again.")


# =========================
# ANOMALY ALERT SYSTEM
# =========================

def _top_pct_threshold(sorted_asc: list[float], top_pct: float) -> float:
    """Value at the boundary of the top `top_pct`% of a sorted-ascending list."""
    if not sorted_asc:
        return float("inf")
    idx = max(0, int(len(sorted_asc) * (1.0 - top_pct / 100.0)))
    return sorted_asc[min(idx, len(sorted_asc) - 1)]


async def send_evening_alerts(context: ContextTypes.DEFAULT_TYPE):
    chats = owners_silent_chat_ids()
    if not chats:
        return

    yesterday = previous_business_day(now_local())
    row = get_full_day(yesterday)
    if not row:
        return  # No full report posted yet — skip silently

    (total_sales, visa, cash, tips,
     lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
     dinner_sales, dinner_pax, dinner_walkins, dinner_noshows) = row

    total_sales    = float(total_sales or 0)
    lunch_sales    = float(lunch_sales or 0)
    dinner_sales   = float(dinner_sales or 0)
    lunch_pax      = int(lunch_pax or 0)
    dinner_pax     = int(dinner_pax or 0)
    lunch_noshows  = int(lunch_noshows or 0)
    dinner_noshows = int(dinner_noshows or 0)
    tips           = float(tips or 0)
    covers         = lunch_pax + dinner_pax
    lunch_avg      = (lunch_sales  / lunch_pax)  if lunch_pax  else 0.0
    dinner_avg     = (dinner_sales / dinner_pax) if dinner_pax else 0.0
    tips_pct       = (tips / total_sales * 100)  if total_sales else 0.0

    weekday  = yesterday.isoweekday()  # 1=Mon … 7=Sun
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_name  = day_names[weekday - 1]

    # Same-weekday history: [yesterday, prev1, prev2, prev3, prev4] ordered DESC
    same_wd_rows = get_full_days_for_weekday(weekday, yesterday, 5)
    prev_wd_rows  = same_wd_rows[1:]  # Exclude yesterday; up to 4 previous same-weekday records

    def _avg(vals: list) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    def _pct(value: float, ref: float) -> float:
        return (value - ref) / ref * 100.0 if ref else 0.0

    def _fmt(pct: float) -> str:
        return f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"

    alerts: list[str] = []

    # ── NEGATIVE: OPERATIONAL PROBLEMS 🔴 ────────────────────────────────────

    if len(prev_wd_rows) >= 2:
        avg_sales_wd    = _avg([r["total_sales"]    for r in prev_wd_rows])
        avg_covers_wd   = _avg([r["covers"]         for r in prev_wd_rows])
        avg_dinner_ns   = _avg([r["dinner_noshows"] for r in prev_wd_rows])
        avg_tips_pct_wd = _avg([
            (r["tips"] / r["total_sales"] * 100) if r["total_sales"] else 0.0
            for r in prev_wd_rows
        ])

        # 1. Dinner no-shows ≥ multiplier × same-weekday average
        if avg_dinner_ns > 0 and dinner_noshows >= avg_dinner_ns * ALERT_NOSHOW_MULTIPLIER:
            pct = _pct(dinner_noshows, avg_dinner_ns)
            alerts.append(
                f"🔴 Dinner no-shows: {dinner_noshows} vs {day_name} avg {avg_dinner_ns:.1f} "
                f"({_fmt(pct)}) — possible confirmation process failure"
            )

        # 2. Service imbalance: one service ≥ threshold % of daily revenue
        if total_sales > 0:
            lunch_share  = lunch_sales  / total_sales * 100
            dinner_share = dinner_sales / total_sales * 100
            if lunch_share >= ALERT_SERVICE_IMBALANCE_PCT:
                alerts.append(
                    f"🔴 Service imbalance: Lunch was {lunch_share:.0f}% of revenue "
                    f"(€{lunch_sales:.0f} lunch vs €{dinner_sales:.0f} dinner) — unusually quiet dinner"
                )
            elif dinner_share >= ALERT_SERVICE_IMBALANCE_PCT:
                alerts.append(
                    f"🔴 Service imbalance: Dinner was {dinner_share:.0f}% of revenue "
                    f"(€{dinner_sales:.0f} dinner vs €{lunch_sales:.0f} lunch) — unusually quiet lunch"
                )

        # 3. Covers normal (≥85% of avg) but revenue dropped ≥ threshold %
        if avg_covers_wd > 0 and avg_sales_wd > 0:
            covers_ratio = covers / avg_covers_wd
            revenue_pct  = _pct(total_sales, avg_sales_wd)
            if covers_ratio >= 0.85 and revenue_pct <= -ALERT_REVENUE_VS_COVERS_DROP_PCT:
                alerts.append(
                    f"📊 Revenue −{abs(revenue_pct):.0f}% vs {day_name} avg despite normal covers "
                    f"(€{total_sales:.0f} vs avg €{avg_sales_wd:.0f} | {covers} covers vs avg {avg_covers_wd:.0f}) "
                    f"— possible over-discounting or comps"
                )

        # 4. Tips % dropped ≥ threshold % vs same-weekday average
        if avg_tips_pct_wd > 0:
            tips_drop = _pct(tips_pct, avg_tips_pct_wd)
            if tips_drop <= -ALERT_TIPS_DROP_PCT:
                alerts.append(
                    f"🔴 Tips dropped: {tips_pct:.1f}% of revenue vs {day_name} avg {avg_tips_pct_wd:.1f}% "
                    f"({_fmt(tips_drop)}) — service quality signal"
                )

    # 5. Consecutive avg-ticket erosion over N same weekdays (lunch OR dinner)
    n_eros = ALERT_TICKET_EROSION_DAYS
    if len(same_wd_rows) >= n_eros:
        erosion_rows = same_wd_rows[:n_eros]  # Newest N same-weekday records, DESC
        l_avgs = [r["lunch_avg"]  for r in erosion_rows]
        d_avgs = [r["dinner_avg"] for r in erosion_rows]

        # DESC order → avgs[i] < avgs[i+1] means each weekday occurrence is lower than the one before it
        if all(v > 0 for v in l_avgs) and all(l_avgs[i] < l_avgs[i + 1] for i in range(n_eros - 1)):
            trend = " → ".join(f"€{v:.2f}" for v in reversed(l_avgs))
            alerts.append(
                f"📊 Lunch avg ticket declining {n_eros} consecutive {day_name}s: "
                f"{trend} — erosion trend"
            )
        if all(v > 0 for v in d_avgs) and all(d_avgs[i] < d_avgs[i + 1] for i in range(n_eros - 1)):
            trend = " → ".join(f"€{v:.2f}" for v in reversed(d_avgs))
            alerts.append(
                f"📊 Dinner avg ticket declining {n_eros} consecutive {day_name}s: "
                f"{trend} — erosion trend"
            )

        # 5b. Lunch avg ticket below absolute minimum threshold
        if lunch_pax > 0 and lunch_avg < ALERT_LUNCH_TICKET_MIN:
            alerts.append(
                f"🔴 Lunch avg ticket low: €{lunch_avg:.2f} vs minimum €{ALERT_LUNCH_TICKET_MIN:.0f} "
                f"({lunch_pax} pax, €{lunch_sales:.0f} sales) — check pricing or discounts"
            )

    # ── NEGATIVE: FINANCIAL HEALTH 📊 ────────────────────────────────────────

    # 6. Friday/Saturday: revenue ≥ threshold % below typical same-weekday average
    if weekday in (5, 6) and len(prev_wd_rows) >= 2:
        avg_strong = _avg([r["total_sales"] for r in prev_wd_rows])
        if avg_strong > 0:
            miss_pct = _pct(total_sales, avg_strong)
            if miss_pct <= -ALERT_STRONG_DAY_MISS_PCT:
                alerts.append(
                    f"📊 {day_name} underperformed: €{total_sales:.0f} vs {day_name} avg €{avg_strong:.0f} "
                    f"({_fmt(miss_pct)}) — missed high-demand day"
                )

    # 7. Monday only: this Monday ≥ threshold % below last Monday (week-pace warning)
    if weekday == 1 and len(same_wd_rows) >= 2:
        last_mon_sales = same_wd_rows[1]["total_sales"]
        if last_mon_sales > 0:
            pace_pct = _pct(total_sales, last_mon_sales)
            if pace_pct <= -ALERT_WEEK_PACE_PCT:
                alerts.append(
                    f"📊 Week-pace warning: Monday €{total_sales:.0f} vs last Monday "
                    f"€{last_mon_sales:.0f} ({_fmt(pace_pct)}, {fmt_day_ddmmyyyy(same_wd_rows[1]['day'])}) "
                    f"— early signal for a slow week"
                )

    # ── POSITIVE ALERTS ✅ ────────────────────────────────────────────────────

    # 8. Revenue in top ALERT_TOP_PERCENTILE % of all recorded days
    all_sales_hist = get_all_historical_sales()
    if len(all_sales_hist) >= 10:
        rev_thr = _top_pct_threshold(all_sales_hist, ALERT_TOP_PERCENTILE)
        if total_sales >= rev_thr:
            alerts.append(
                f"✅ Revenue €{total_sales:.0f} is in the top {ALERT_TOP_PERCENTILE:.0f}% of all recorded days "
                f"(threshold ≥ €{rev_thr:.0f})"
            )

    # 9. Covers in top ALERT_POSITIVE_COVERS_PCT % of all recorded days
    all_covers_hist = get_all_historical_covers()
    if len(all_covers_hist) >= 10:
        cov_thr = _top_pct_threshold([float(c) for c in all_covers_hist], ALERT_POSITIVE_COVERS_PCT)
        if covers >= cov_thr:
            alerts.append(
                f"✅ Covers {covers} are in the top {ALERT_POSITIVE_COVERS_PCT:.0f}% of all recorded days "
                f"(threshold ≥ {int(cov_thr)})"
            )

    # 10. Dinner turnaround: yesterday bounced ≥ threshold % above prev-3 same-weekday dinner avg
    if len(same_wd_rows) >= 4:
        prev3 = same_wd_rows[1:4]
        prev3_dinner_avgs = [r["dinner_avg"] for r in prev3 if r["dinner_avg"] > 0]
        if prev3_dinner_avgs:
            prev3_avg  = _avg(prev3_dinner_avgs)
            bounce_pct = _pct(dinner_avg, prev3_avg)
            if bounce_pct >= ALERT_POSITIVE_REVENUE_PCT:
                alerts.append(
                    f"✅ Dinner turnaround: avg ticket €{dinner_avg:.2f} vs prev-3 {day_name} avg "
                    f"€{prev3_avg:.2f} ({_fmt(bounce_pct)}) — bounce-back worth noting"
                )

    # ── SEND ─────────────────────────────────────────────────────────────────

    if not alerts:
        return

    msg = f"🔔 Norah Evening Alerts — {fmt_day_ddmmyyyy(yesterday)}\n\n" + "\n\n".join(alerts)
    for chat_id in chats:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            print(f"Evening alert send failed for chat {chat_id}: {e}")


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
    agent_status = "✅ configured" if ANTHROPIC_API_KEY else "❌ missing ANTHROPIC_API_KEY"

    msg = (
        "🏓 PONG — Norah Ops Health Check\n\n"
        f"Bot: ✅ running\n"
        f"DB: {'✅ OK' if db_ok else '❌ FAIL'}\n"
        f"AI Agent: {agent_status}\n"
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
    if not await guard_admin(update, reply_in_private_only=False):
        return
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

    tagged_texts = [(d, txt) for d, txt in rows if "SOLD OUT" in extract_note_tags(txt)]
    if tagged_texts:
        counter = Counter()
        for _, txt in tagged_texts:
            content = extract_tag_content(txt, "SOLD OUT")
            counter.update(tokenize(content))
        top = counter.most_common(12)
        source = f"({len(tagged_texts)} tagged notes)"
    else:
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

    tagged_texts = [(d, txt) for d, txt in rows if "COMPLAINT" in extract_note_tags(txt)]
    if tagged_texts:
        counter = Counter()
        for _, txt in tagged_texts:
            content = extract_tag_content(txt, "COMPLAINT")
            counter.update(tokenize(content))
        top = counter.most_common(12)
        source = f"({len(tagged_texts)} tagged notes)"
    else:
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
    for d, txt in tagged[-10:]:
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
    weekday = today.isoweekday()
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
    days_since_sat = (today.weekday() - 5) % 7
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
        wd = r["day"].weekday()
        buckets[wd].append(r)

    lines = [f"📅 Weekday Mix (last {n_weeks} weeks)\n"]
    for wd in range(6):
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
    for wd in range(6):
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
        # No manual entry — fill from Agora POS (sales analytics + Z report)
        # and CoverManager (walk-ins / no-shows).
        agora = _try_agora(report_day)
        if agora:
            cm = _try_cm_walkins_noshows(report_day)
            upsert_full_day(
                report_day,
                agora.total_net, agora.visa, agora.cash, agora.tips,
                agora.lunch_net, agora.lunch_covers, cm["lunch_walkins"], cm["lunch_noshows"],
                agora.dinner_net, agora.dinner_covers, cm["dinner_walkins"], cm["dinner_noshows"],
            )
            upsert_daily(report_day, agora.total_net, agora.total_covers)
            visa_str = euro_comma(agora.visa) if agora.visa else "—"
            cash_str = euro_comma(agora.cash) if agora.cash else "—"
            tips_str = euro_comma(agora.tips) if agora.tips else "—"
            msg = (
                f"📌 Norah Daily Post\n"
                f"Day: {fmt_day_ddmmyyyy(report_day)}\n"
                f"Total Sales Day: {euro_comma(agora.total_net)} *(Agora POS)*\n"
                f"Total Covers: {agora.total_covers}  |  Avg Ticket: {euro_comma(agora.avg_ticket)}\n\n"
                f"Visa: {visa_str}\n"
                f"Cash: {cash_str}\n"
                f"Tips: {tips_str}\n\n"
                f"Lunch: {euro_comma(agora.lunch_net)}\n"
                f"Pax: {agora.lunch_covers}\n"
                f"Avg Ticket: {euro_comma(agora.lunch_avg_ticket)}\n"
                f"Walk in: {cm['lunch_walkins']}\n"
                f"No show: {cm['lunch_noshows']}\n\n"
                f"Dinner: {euro_comma(agora.dinner_net)}\n"
                f"Pax: {agora.dinner_covers}\n"
                f"Avg Ticket: {euro_comma(agora.dinner_avg_ticket)}\n"
                f"Walk in: {cm['dinner_walkins']}\n"
                f"No show: {cm['dinner_noshows']}\n\n"
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
    if report_day.weekday() == 6:   # Sunday — Norah is closed, skip silently
        print(f"[daily_post] skipping Sunday {report_day.isoformat()}")
        return
    msg = build_owners_post_for_day(report_day)
    for chat_id in chats:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            print(f"Daily post send failed for chat {chat_id}: {e}")

def _booking_sources_block(from_date: date, to_date: date) -> str:
    """
    Fetch CoverManager reservations for the week and return a formatted
    booking-source breakdown string. Returns "" silently on any error.
    """
    if not _CM_AVAILABLE:
        return ""
    try:
        rows = _cm_mod.get_reservations_range(from_date, to_date)
    except Exception as e:
        print(f"CoverManager source fetch failed: {e}")
        return ""

    if not rows:
        return ""

    # Aggregate across all day records — raw reservations are inside each day's aggregation,
    # but get_reservations_range doesn't expose raw records. Re-fetch raw via the module.
    try:
        import urllib.request as _ur, json as _json
        from collections import Counter as _Counter

        base = _cm_mod.COVERMANAGER_BASE
        key  = _cm_mod.COVERMANAGER_API_KEY
        rest = _cm_mod.COVERMANAGER_RESTAURANT
        from_str = from_date.isoformat()
        to_str   = to_date.isoformat()

        all_records = []
        page = 0
        while True:
            url = f"{base}/restaurant/get_reservs/{key}/{rest}/{from_str}/{to_str}/{page}"
            req = _ur.Request(url)
            req.add_header("Accept", "application/json")
            with _ur.urlopen(req, timeout=15) as r:
                data = _json.loads(r.read().decode())
            batch = data.get("reservs", [])
            all_records.extend(batch)
            if len(batch) < 1000:
                break
            page += 1

        if not all_records:
            return ""

        # Classify each record into a human-readable channel
        channels = _Counter()
        for r in all_records:
            origin = (r.get("origin") or "").strip().lower()
            prov   = (r.get("provenance") or "").strip().lower()

            if prov == "walk in":
                channels["Walk-in"] += 1
            elif "instagram" in origin:
                channels["Instagram"] += 1
            elif origin == "google":
                channels["Google"] += 1
            elif prov == "moduloweb":
                channels["Own website"] += 1
            elif prov == "app-movil":
                channels["Mobile app"] += 1
            elif prov == "waitinglist":
                channels["Waiting list"] += 1
            elif prov == "software":
                channels["Staff/software"] += 1
            elif prov == "terceros":
                channels["Third-party"] += 1
            else:
                channels["Other"] += 1

        total = sum(channels.values())
        lines = [f"\n📍 Booking Sources (this week, {total} total)"]
        for ch, cnt in sorted(channels.items(), key=lambda x: -x[1]):
            pct = cnt / total * 100
            lines.append(f"{ch}: {cnt} ({pct:.0f}%)")
        return "\n".join(lines)

    except Exception as e:
        print(f"CoverManager source aggregation failed: {e}")
        return ""


async def send_weekly_digest(context: ContextTypes.DEFAULT_TYPE):
    chats = owners_silent_chat_ids()
    if not chats:
        return

    # Job fires on Monday — last week = Mon to Sun
    today = datetime.now(TZ).date()
    last_monday = today - timedelta(days=7)
    last_sunday = today - timedelta(days=1)
    prev_monday = today - timedelta(days=14)
    prev_sunday = today - timedelta(days=8)

    p_this = Period(start=last_monday, end=last_sunday)
    p_prev = Period(start=prev_monday, end=prev_sunday)

    agg = sum_full_in_period(p_this)
    agg_prev = sum_full_in_period(p_prev)

    def _diff(new, old):
        if old == 0:
            return ""
        pct = (new - old) / old * 100.0
        arrow = "▲" if pct >= 0 else "▼"
        return f" {arrow} {abs(pct):.1f}%"

    def _fmt_week(d):
        return d.strftime("%-d %b")

    week_label = f"{_fmt_week(last_monday)} – {_fmt_week(last_sunday)} {last_sunday.year}"
    prev_label = f"{_fmt_week(prev_monday)} – {_fmt_week(prev_sunday)}"

    total_sales = agg["total_sales"]
    prev_sales = agg_prev["total_sales"]
    lunch_sales = agg["lunch_sales"]
    lunch_pax = agg["lunch_pax"]
    dinner_sales = agg["dinner_sales"]
    dinner_pax = agg["dinner_pax"]
    total_pax = lunch_pax + dinner_pax
    lunch_avg = (lunch_sales / lunch_pax) if lunch_pax else 0.0
    dinner_avg = (dinner_sales / dinner_pax) if dinner_pax else 0.0
    tips = agg["tips"]
    tips_pct = (tips / total_sales * 100.0) if total_sales else 0.0

    prev_lunch_sales = agg_prev["lunch_sales"]
    prev_lunch_pax = agg_prev["lunch_pax"]
    prev_dinner_sales = agg_prev["dinner_sales"]
    prev_dinner_pax = agg_prev["dinner_pax"]
    prev_total_pax = prev_lunch_pax + prev_dinner_pax
    prev_lunch_avg = (prev_lunch_sales / prev_lunch_pax) if prev_lunch_pax else 0.0
    prev_dinner_avg = (prev_dinner_sales / prev_dinner_pax) if prev_dinner_pax else 0.0
    prev_tips = agg_prev["tips"]
    prev_tips_pct = (prev_tips / prev_sales * 100.0) if prev_sales else 0.0

    walkins = agg["lunch_walkins"] + agg["dinner_walkins"]
    noshows = agg["lunch_noshows"] + agg["dinner_noshows"]
    prev_walkins = agg_prev["lunch_walkins"] + agg_prev["dinner_walkins"]
    prev_noshows = agg_prev["lunch_noshows"] + agg_prev["dinner_noshows"]

    msg = (
        f"🗓️ Norah Weekly Digest\n"
        f"Week: {week_label}\n"
        f"vs prev week: {prev_label}\n"
        f"\n📊 Revenue\n"
        f"Total: €{total_sales:.0f}{_diff(total_sales, prev_sales)}"
        f"  (prev: €{prev_sales:.0f})\n"
        f"Covers: {total_pax}{_diff(total_pax, prev_total_pax)}"
        f"  (prev: {prev_total_pax})\n"
        f"\n🥗 Lunch\n"
        f"Sales: €{lunch_sales:.0f}{_diff(lunch_sales, prev_lunch_sales)}"
        f"  (prev: €{prev_lunch_sales:.0f})\n"
        f"Covers: {lunch_pax}{_diff(lunch_pax, prev_lunch_pax)}"
        f"  (prev: {prev_lunch_pax})\n"
        f"Avg ticket: €{lunch_avg:.2f}{_diff(lunch_avg, prev_lunch_avg)}"
        f"  (prev: €{prev_lunch_avg:.2f})\n"
        f"\n🌙 Dinner\n"
        f"Sales: €{dinner_sales:.0f}{_diff(dinner_sales, prev_dinner_sales)}"
        f"  (prev: €{prev_dinner_sales:.0f})\n"
        f"Covers: {dinner_pax}{_diff(dinner_pax, prev_dinner_pax)}"
        f"  (prev: {prev_dinner_pax})\n"
        f"Avg ticket: €{dinner_avg:.2f}{_diff(dinner_avg, prev_dinner_avg)}"
        f"  (prev: €{prev_dinner_avg:.2f})\n"
        f"\n💶 Tips\n"
        f"Total: €{tips:.0f} ({tips_pct:.1f}% of sales){_diff(tips, prev_tips)}"
        f"  (prev: €{prev_tips:.0f}, {prev_tips_pct:.1f}%)\n"
        f"\n🚶 Walk-ins / No-shows\n"
        f"Walk-ins: {walkins}{_diff(walkins, prev_walkins)}"
        f"  (prev: {prev_walkins})\n"
        f"No-shows: {noshows}{_diff(noshows, prev_noshows)}"
        f"  (prev: {prev_noshows})"
    )

    sources_block = _booking_sources_block(last_monday, last_sunday)
    if sources_block:
        msg += sources_block

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

    if d.weekday() == 6:
        await update.message.reply_text(f"⚠️ {d.isoformat()} is a Sunday — Norah is closed, no post to send.")
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

    # ---------------------------------------------------------
    # AI Agent — plain text queries in OWNERS_REQUESTS
    # ---------------------------------------------------------
    if role == ROLE_OWNERS_REQUESTS and not user.is_bot:
        await handle_agent_query(update, context, msg_text)
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
# FLASK API
# =========================
flask_app = Flask(__name__)
CORS(flask_app)

@flask_app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    return response

@flask_app.route('/api/stats/daily', methods=['OPTIONS'])
def daily_options():
    return '', 204

@flask_app.route('/api/stats/weekly', methods=['OPTIONS'])
def weekly_options():
    return '', 204


_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Norah · Login</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Inter',sans-serif;background:#f5f5f0;display:flex;align-items:center;justify-content:center;min-height:100vh}}
    .card{{background:#fff;border:1px solid #e8e8e2;border-radius:14px;padding:40px 36px;width:320px;box-shadow:0 2px 12px rgba(0,0,0,0.07);text-align:center}}
    .brand{{font-size:1rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px}}
    .sub{{font-size:0.75rem;color:#9ca3af;margin-bottom:28px}}
    input{{width:100%;border:1px solid #e8e8e2;border-radius:8px;padding:10px 14px;font-size:0.9rem;font-family:inherit;outline:none;margin-bottom:12px;background:#f9f9f6}}
    input:focus{{border-color:#a0a0f0}}
    button{{width:100%;background:#1a1a2e;color:#fff;border:none;border-radius:8px;padding:11px;font-size:0.9rem;font-family:inherit;font-weight:600;cursor:pointer}}
    .error{{color:#b91c1c;font-size:0.78rem;margin-bottom:10px}}
  </style>
</head>
<body>
  <div class="card">
    <div class="brand">Norah</div>
    <div class="sub">Operations Dashboard</div>
    {error}
    <form method="POST" action="/login">
      <input type="password" name="password" placeholder="Password" autofocus />
      <button type="submit">Continue</button>
    </form>
  </div>
</body>
</html>"""


@flask_app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        pwd = request.form.get('password', '')
        if DASHBOARD_PASSWORD and pwd == DASHBOARD_PASSWORD:
            resp = make_response(redirect('/dashboard'))
            resp.set_cookie('dash_auth', pwd, httponly=True, samesite='Lax')
            return resp
        return _LOGIN_PAGE.format(error='<p class="error">Incorrect password</p>'), 401
    return _LOGIN_PAGE.format(error='')


@flask_app.route('/dashboard')
def serve_dashboard():
    if DASHBOARD_PASSWORD:
        if request.cookies.get('dash_auth') != DASHBOARD_PASSWORD:
            return redirect('/login')
    return send_file('dashboard.html')


def _api_check_auth():
    if not DASHBOARD_API_KEY:
        return False
    return request.headers.get("Authorization", "") == f"Bearer {DASHBOARD_API_KEY}"


@flask_app.route("/api/stats/daily")
def api_stats_daily():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    from_str = request.args.get("from")
    to_str = request.args.get("to")
    if not from_str or not to_str:
        return jsonify({"error": "from and to query params required (YYYY-MM-DD)"}), 400

    try:
        from_date = date.fromisoformat(from_str)
        to_date = date.fromisoformat(to_str)
    except ValueError:
        return jsonify({"error": "Invalid date format, use YYYY-MM-DD"}), 400

    if to_date < from_date:
        return jsonify({"error": "to must be >= from"}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day,
                       total_sales,
                       lunch_pax + dinner_pax AS total_covers,
                       lunch_sales,
                       dinner_sales,
                       lunch_pax,
                       dinner_pax,
                       COALESCE(tips, 0) AS tips,
                       COALESCE(lunch_noshows, 0) AS lunch_noshows,
                       COALESCE(dinner_noshows, 0) AS dinner_noshows
                FROM full_daily_stats
                WHERE day BETWEEN %s AND %s
                ORDER BY day
                """,
                (from_date, to_date),
            )
            rows = cur.fetchall()

    result = []
    for row in rows:
        day, total_sales, total_covers, lunch_sales, dinner_sales, lunch_covers, dinner_covers, tips, lunch_noshows, dinner_noshows = row
        total_covers = int(total_covers or 0)
        total_sales = float(total_sales or 0)
        lc = int(lunch_covers or 0)
        dc = int(dinner_covers or 0)
        ls = float(lunch_sales or 0)
        ds = float(dinner_sales or 0)
        avg_ticket = round(total_sales / total_covers, 2) if total_covers else 0.0
        lunch_avg_ticket = round(ls / lc, 2) if lc else 0.0
        dinner_avg_ticket = round(ds / dc, 2) if dc else 0.0
        result.append({
            "date": day.isoformat(),
            "total_sales": round(total_sales, 2),
            "total_covers": total_covers,
            "avg_ticket": avg_ticket,
            "lunch_sales": round(ls, 2),
            "dinner_sales": round(ds, 2),
            "lunch_covers": lc,
            "dinner_covers": dc,
            "lunch_avg_ticket": lunch_avg_ticket,
            "dinner_avg_ticket": dinner_avg_ticket,
            "tips": round(float(tips or 0), 2),
            "lunch_noshows": int(lunch_noshows or 0),
            "dinner_noshows": int(dinner_noshows or 0),
        })

    return jsonify(result)


@flask_app.route("/api/stats/weekly")
def api_stats_weekly():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        weeks = int(request.args.get("weeks", 8))
        if not 1 <= weeks <= 52:
            raise ValueError()
    except ValueError:
        return jsonify({"error": "weeks must be an integer between 1 and 52"}), 400

    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    range_start = last_monday - timedelta(weeks=weeks - 1)
    range_end = last_monday + timedelta(days=6)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT day,
                       total_sales,
                       lunch_pax + dinner_pax AS covers
                FROM full_daily_stats
                WHERE day BETWEEN %s AND %s
                ORDER BY day
                """,
                (range_start, range_end),
            )
            rows = cur.fetchall()

    buckets = {}
    for day, total_sales, covers in rows:
        monday = day - timedelta(days=day.weekday())
        if monday not in buckets:
            buckets[monday] = {"total_sales": 0.0, "total_covers": 0}
        buckets[monday]["total_sales"] += float(total_sales or 0)
        buckets[monday]["total_covers"] += int(covers or 0)

    result = []
    for i in range(weeks):
        week_start = last_monday - timedelta(weeks=weeks - 1 - i)
        week_end = week_start + timedelta(days=6)
        b = buckets.get(week_start, {"total_sales": 0.0, "total_covers": 0})
        total_sales = round(b["total_sales"], 2)
        total_covers = b["total_covers"]
        avg_ticket = round(total_sales / total_covers, 2) if total_covers else 0.0
        result.append({
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "total_sales": total_sales,
            "total_covers": total_covers,
            "avg_ticket": avg_ticket,
        })

    return jsonify(result)


@flask_app.route('/api/booking-sources', methods=['OPTIONS'])
def booking_sources_options():
    return '', 204


@flask_app.route("/api/booking-sources")
def api_booking_sources():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    if not _CM_AVAILABLE:
        print("[booking-sources] CoverManager not available (_CM_AVAILABLE=False)", flush=True)
        return jsonify({"error": "CoverManager not available"}), 503

    today = date.today()
    days_since_monday = today.weekday()
    this_monday = today - timedelta(days=days_since_monday)
    trend_from = this_monday - timedelta(weeks=11)   # 12 weeks total
    pie_from   = today - timedelta(days=29)          # last 30 days

    print(f"[booking-sources] fetching {trend_from} → {today}", flush=True)

    try:
        # Fetch using chunked helper (3 months > 2-month threshold → monthly chunks)
        all_records, partial, covered_through = _fetch_cm_records_chunked(trend_from, today, timeout_sec=55)
        print(f"[booking-sources] got {len(all_records)} records, partial={partial}, through={covered_through}", flush=True)
    except Exception as e:
        print(f"[booking-sources] fetch error: {e}", flush=True)
        return jsonify({"error": f"CoverManager fetch failed: {e}"}), 500

    if not all_records:
        return jsonify({"error": "No reservation data returned from CoverManager"}), 500

    pie_from_str = pie_from.isoformat()
    # Filter Sundays (restaurant closed — any Sunday entries are noise)
    def _is_sunday(r):
        d_str = r.get("date", "")
        if not d_str:
            return False
        try:
            return date.fromisoformat(d_str).weekday() == 6
        except Exception:
            return False

    all_records_no_sun = [r for r in all_records if not _is_sunday(r)]
    pie_records = [r for r in all_records_no_sun if (r.get("date") or "") >= pie_from_str]

    from collections import Counter, defaultdict as _dd

    # ── Pie: last 30 days ────────────────────────────────────────────────────
    pie_counts = Counter(_classify_channel(r) for r in pie_records)
    pie_total  = sum(pie_counts.values())

    # ── Trends: last 30 days, one point per day (Sundays already removed) ────
    _MON_ABBR = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    day_buckets = _dd(lambda: _dd(int))
    for r in pie_records:   # pie_records = last 30 days, no Sundays
        d_str = r.get("date", "")
        if d_str:
            day_buckets[d_str][_classify_channel(r)] += 1

    # Build ordered date list (last 30 days, no Sundays)
    trend_dates = []
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        if d.weekday() != 6:            # skip Sundays
            trend_dates.append(d.isoformat())

    date_labels = [
        f"{date.fromisoformat(d).day} {_MON_ABBR[date.fromisoformat(d).month - 1]}"
        for d in trend_dates
    ]

    _TREND_CHANNELS = ["Google", "Own website", "Instagram", "Walk-in", "Mobile app", "Staff/software"]
    series = {}
    for ch in _TREND_CHANNELS:
        vals = [day_buckets.get(d, {}).get(ch, 0) for d in trend_dates]
        if any(v > 0 for v in vals):
            series[ch] = vals

    result = {
        "pie": {
            "total":    pie_total,
            "period":   {"from": pie_from_str, "to": today.isoformat()},
            "channels": {
                ch: {"count": cnt, "pct": round(cnt / pie_total * 100, 1) if pie_total else 0}
                for ch, cnt in pie_counts.most_common()
            },
        },
        "trends": {
            "dates":       trend_dates,
            "date_labels": date_labels,
            "series":      series,
        },
    }
    if partial:
        result["note"] = f"Partial data through {covered_through}"
    print(f"[booking-sources] returning pie total={pie_total}, trend dates={len(trend_dates)}, series={list(series.keys())}", flush=True)
    return jsonify(result)


@flask_app.route("/test-agora")
def test_agora():
    import urllib.request, urllib.error, gzip as _gzip
    target = AGORA_URL
    result = {"target": target, "reachable": False}
    try:
        req = urllib.request.Request(target + "/", method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            if raw[:2] == b'\x1f\x8b':
                raw = _gzip.decompress(raw)
            result["reachable"] = True
            result["status"] = r.status
            result["body_preview"] = raw.decode("utf-8", errors="replace")[:300]
    except urllib.error.HTTPError as e:
        result["reachable"] = True   # got a response — port is open
        result["status"] = e.code
        result["body_preview"] = e.read().decode("utf-8", errors="replace")[:300]
    except Exception as e:
        result["error"] = str(e)
    return jsonify(result)


@flask_app.route("/run-pipeline")
def run_pipeline():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-23")
    try:
        ds = _agora_mod.get_daily_sales(date_str, save_to_db=False)
        if ds is None:
            return jsonify({"error": f"No sales data for {date_str}"}), 404
        return jsonify({
            "date":              ds.date,
            "total_sales":       ds.total_net,
            "visa":              ds.visa,
            "cash":              ds.cash,
            "lunch_sales":       ds.lunch_net,
            "lunch_pax":         ds.lunch_covers,
            "lunch_avg_ticket":  ds.lunch_avg_ticket,
            "dinner_sales":      ds.dinner_net,
            "dinner_pax":        ds.dinner_covers,
            "dinner_avg_ticket": ds.dinner_avg_ticket,
            "total_covers":      ds.total_covers,
            "avg_ticket":        ds.avg_ticket,
            "raw_line_items":    ds.raw_items,
            "waiters":           ds.waiters,
            "families":          ds.families,
            "top_products":      ds.top_products,
            "line_items":        ds.line_items,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/test-payment-methods")
def test_payment_methods():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-20")
    try:
        result = _agora_mod.get_payment_methods(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/test-salecenter")
def test_salecenter():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-23")
    try:
        result = _agora_mod.get_salecenter_sales_file(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/test-tips-byuser")
def test_tips_byuser():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-23")
    try:
        result = _agora_mod.get_tips_by_user(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/test-zreport")
def test_zreport():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-23")
    try:
        result = _agora_mod.get_pos_closeouts(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/test-remaining")
def test_remaining():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-23")
    try:
        result = _agora_mod.get_remaining_reports(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/test-covers")
def test_covers():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-23")
    try:
        result = _agora_mod.get_covers_report(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/test-closure2")
def test_closure2():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-20")
    try:
        result = _agora_mod.get_closure_report2(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/test-cashregister")
def test_cashregister():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-20")
    try:
        result = _agora_mod.get_cash_register_report(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/test-closure")
def test_closure():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-20")
    try:
        result = _agora_mod.get_closure_report(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/run-sales-probe")
def run_sales_probe_endpoint():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-18")
    try:
        import agora_sales_probe as _sp
        result = _sp.run_sales_probe(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/run-deep-probe")
def run_deep_probe_endpoint():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", "2026-04-18")
    try:
        import agora_deep_probe as _dp
        result = _dp.run_deep_probe(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@flask_app.route("/run-probe")
def run_probe_endpoint():
    if not _api_check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    date_str = request.args.get("date", str(date.today()))
    try:
        import agora_probe as _probe
        result = _probe.run_probe(date_str)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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

    # Analytics commands
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
            days=(1,),
            name="weekly_digest_monday",
        )
        app.job_queue.run_daily(
            send_daily_post_to_owners,
            time=time(hour=DAILY_POST_HOUR, minute=DAILY_POST_MINUTE, tzinfo=TZ),
            name="daily_post_to_owners",
        )
        app.job_queue.run_daily(
            send_evening_alerts,
            time=time(hour=ALERT_EVENING_HOUR, minute=0, tzinfo=TZ),
            name="evening_alerts",
        )

    flask_thread = threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080))),
        daemon=True,
    )
    flask_thread.start()

    while True:
        try:
            print("Waiting 20s before polling...")
            time_mod.sleep(20)
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
            break
        except Exception as e:
            print(f"Bot crashed, restarting in 30s: {e}")
            time_mod.sleep(30)

if __name__ == "__main__":
    main()
