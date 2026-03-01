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

def parse_yyyy_mm_dd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def parse_dd_mm_yyyy(s: str) -> date:
    return datetime.strptime(s, "%d/%m/%Y").date()

def parse_any_date(s: str) -> date:
    s = (s or "").strip()
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
# PATCH 1: Owners formatting helpers
# =========================
def euro_comma(x: float) -> str:
    s = f"{float(x):.2f}"
    return s.replace(".", ",")

def fmt_day_ddmmyyyy(d: date) -> str:
    return d.strftime("%d/%m/%Y")

# =========================
# PATCH 1.5: Spanish normalization helpers (SAFE)
# =========================
def normalize_spanish_full_report(text: str) -> str:
    """
    Converts common Spanish labels to the English schema used by parse_full_report_block().
    Content/values remain unchanged. This keeps parsing deterministic.
    """
    if not text:
        return text

    out = text

    # Normalize label words (case-insensitive). Keep it conservative.
    replacements = {
        # Day
        r"^\s*d[ií]a\s*:": "Day:",

        # Totals
        r"^\s*ventas?\s*totales?\s*(del\s*d[ií]a)?\s*:": "Total Sales Day:",
        r"^\s*total\s*ventas\s*:": "Total Sales Day:",
        r"^\s*total\s*del\s*d[ií]a\s*:": "Total Sales Day:",

        # Payments
        r"^\s*tarjeta\s*:": "Visa:",
        r"^\s*visa\s*:": "Visa:",
        r"^\s*efectivo\s*:": "Cash:",
        r"^\s*cash\s*:": "Cash:",
        r"^\s*propinas?\s*:": "Tips:",
        r"^\s*tips\s*:": "Tips:",

        # Services
        r"^\s*comida\s*:": "Lunch:",
        r"^\s*almuerzo\s*:": "Lunch:",
        r"^\s*lunch\s*:": "Lunch:",
        r"^\s*cena\s*:": "Dinner:",
        r"^\s*dinner\s*:": "Dinner:",

        # Pax
        r"^\s*personas?\s*:": "Pax:",
        r"^\s*comensales\s*:": "Pax:",
        r"^\s*pax\s*:": "Pax:",

        # Walk-ins
        r"^\s*sin\s*reserva\s*:": "Walk in:",
        r"^\s*walk[-\s]?ins?\s*:": "Walk in:",
        r"^\s*walk\s*in\s*:": "Walk in:",

        # No-shows
        r"^\s*ausentes?\s*:": "No show:",
        r"^\s*no\s*show\s*:": "No show:",
        r"^\s*no[-\s]?shows?\s*:": "No show:",
        r"^\s*no\s*asistieron\s*:": "No show:",
    }

    for pat, rep in replacements.items():
        out = re.sub(pat, rep, out, flags=re.IGNORECASE | re.MULTILINE)

    return out

def normalize_notes_report(text: str) -> str:
    """
    Normalizes Spanish headings to English headings for better consistency.
    Keeps the manager's content as-is.
    """
    if not text:
        return text

    out = text

    subs = {
        r"^\s*incidentes\s*:": "Incidents:",
        r"^\s*incidencias\s*:": "Incidents:",

        r"^\s*personal\s*:": "Staff:",
        r"^\s*equipo\s*:": "Staff:",
        r"^\s*staff\s*:": "Staff:",

        r"^\s*agotad[oa]s?\s*:": "Sold out:",
        r"^\s*agotado\s*:": "Sold out:",
        r"^\s*sold\s*out\s*:": "Sold out:",

        r"^\s*quejas\s*:": "Complaints:",
        r"^\s*reclamaciones\s*:": "Complaints:",
        r"^\s*complaints\s*:": "Complaints:",
    }

    for pat, rep in subs.items():
        out = re.sub(pat, rep, out, flags=re.IGNORECASE | re.MULTILINE)

    return out

def looks_like_notes_report(text: str) -> bool:
    """
    Conservative detector to avoid false triggers.
    Requires at least 3 of the 4 sections (Incidents/Staff/Sold out/Complaints) with ':' present.
    """
    t = (text or "").lower()

    def has_any(prefixes: list[str]) -> bool:
        return any(p in t for p in prefixes)

    hits = 0
    if has_any(["incidents:", "incidentes:", "incidencias:"]):
        hits += 1
    if has_any(["staff:", "personal:", "equipo:"]):
        hits += 1
    if has_any(["sold out:", "agotad", "agotado:"]):
        hits += 1
    if has_any(["complaints:", "quejas:", "reclamaciones:"]):
        hits += 1

    return hits >= 3

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
# FULL DAILY PARSING
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

    day_str = find_line(["Day"])
    if not day_str:
        raise ValueError("Missing Day")
    day_ = parse_any_date(day_str)

    total_sales = _num(find_line(["Total Sales Day", "Total Sales"]) or "")
    visa = _num(find_line(["Visa"]) or "0")
    cash = _num(find_line(["Cash"]) or "0")
    tips = _num(find_line(["Tips"]) or "0")

    def parse_section(section_name: str) -> tuple[float, int, int, int]:
        lines = [ln.strip() for ln in t.splitlines()]
        idx = None
        for i, ln in enumerate(lines):
            if ln.lower().startswith(section_name.lower() + ":"):
                idx = i
                break
        if idx is None:
            raise ValueError(f"Missing {section_name} section")

        sales_val = _num(lines[idx].split(":", 1)[1].strip())

        pax = walkins = noshows = None
        for j in range(idx + 1, min(idx + 10, len(lines))):
            ln = lines[j].strip()
            if not ln:
                continue
            low = ln.lower()
            if low.startswith("pax"):
                pax = _int(ln.split(":", 1)[1])
            elif low.startswith("walk in") or low.startswith("walk-in") or low.startswith("walkin"):
                walkins = _int(ln.split(":", 1)[1])
            elif low.startswith("no show") or low.startswith("no-show") or low.startswith("noshow"):
                noshows = _int(ln.split(":", 1)[1])
            if low.startswith("dinner:") or low.startswith("lunch:"):
                break

        if pax is None or walkins is None or noshows is None:
            raise ValueError(f"Incomplete {section_name} fields (need Pax, Walk in, No show)")
        return float(sales_val), int(pax), int(walkins), int(noshows)

    lunch_sales, lunch_pax, lunch_walkins, lunch_noshows = parse_section("Lunch")
    dinner_sales, dinner_pax, dinner_walkins, dinner_noshows = parse_section("Dinner")

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

# =========================
# SALES COMMANDS
# =========================
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
    if not chat or not user or not update.message:
        return

    # PATCH: If user sends "/report" with text in the SAME message, save it immediately.
    raw = (update.message.text or "").strip()
    # Handles: "/report blah blah" OR "/report\nblah blah"
    tail = raw[len("/report"):].strip() if raw.lower().startswith("/report") else ""
    if tail:
        day_ = business_day_today()
        insert_note_entry(day_, chat.id, user.id, normalize_notes_report(tail))
        await update.message.reply_text(f"Saved 📝 Notes for business day {day_.isoformat()}.")
        return

    day_ = business_day_today()
    set_mode(context.application, REPORT_MODE_KEY, chat.id, user.id, {"on": True, "day": day_.isoformat()})
    await update.message.reply_text(
        f"✅ Report mode ON.\nNow send the notes as your NEXT message.\nBusiness day: {day_.isoformat()}\n\nTo cancel: /cancelreport"
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
    counter = Counter()
    for _, txt in rows:
        t = (txt or "").lower()
        if "sold out" in t or "agotad" in t:
            counter.update(tokenize(txt))
    top = counter.most_common(12)
    if not top:
        await update.message.reply_text("No 'sold out' items detected yet for that period.")
        return
    await update.message.reply_text("🍽️ Sold-out signals:\n" + "\n".join(f"{w}: {c}" for w, c in top))

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
    counter = Counter()
    for _, txt in rows:
        t = (txt or "").lower()
        if "complaint" in t or "queja" in t:
            counter.update(tokenize(txt))
    top = counter.most_common(12)
    if not top:
        await update.message.reply_text("No complaint keywords detected yet for that period.")
        return
    await update.message.reply_text("⚠️ Complaint signals:\n" + "\n".join(f"{w}: {c}" for w, c in top))

# =========================
# FULL DAILY: /setfull + GUIDED MODE
# =========================
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

    # =========================
    # PATCH A: Auto-detect NOTES report (no /report required)
    # Only in OPS_ADMIN or MANAGER_INPUT chats
    # =========================
    if role in (ROLE_OPS_ADMIN, ROLE_MANAGER_INPUT):
        if looks_like_notes_report(msg_text):
            day_ = business_day_today()
            normalized = normalize_notes_report(msg_text)
            insert_note_entry(day_, chat.id, user.id, normalized)
            await update.message.reply_text(f"Saved 📝 Notes for business day {day_.isoformat()}.")
            return

    # =========================
    # PATCH B: Auto-detect FULL daily report (no /setfull required)
    # Supports Spanish labels safely by normalizing them first.
    # Only in OPS_ADMIN or MANAGER_INPUT chats
    # =========================
    if role in (ROLE_OPS_ADMIN, ROLE_MANAGER_INPUT):
        normalized_full = normalize_spanish_full_report(msg_text)
        low = normalized_full.lower()
        if ("day:" in low) and ("total sales" in low) and ("lunch" in low) and ("dinner" in low):
            try:
                d = parse_full_report_block(normalized_full)
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

    # Guided full flow
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

    # Paste full report flow (explicit /setfull mode)
    fm = get_mode(context.application, FULL_MODE_KEY, chat.id, user.id)
    if fm and fm.get("on"):
        try:
            d = parse_full_report_block(normalize_spanish_full_report(msg_text))
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

    # Notes capture (explicit /report mode)
    rm = get_mode(context.application, REPORT_MODE_KEY, chat.id, user.id)
    if rm and rm.get("on"):
        day_str = rm.get("day")
        day_ = parse_yyyy_mm_dd(day_str) if day_str else business_day_today()
        insert_note_entry(day_, chat.id, user.id, normalize_notes_report(msg_text))
        clear_mode(context.application, REPORT_MODE_KEY, chat.id, user.id)
        await update.message.reply_text(f"Saved 📝 Notes for business day {day_.isoformat()}.")
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
# SCHEDULED POSTS
# =========================
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
# PATCH 3: Owners daily post formatted exactly like your template
# =========================
async def send_daily_post_to_owners(context: ContextTypes.DEFAULT_TYPE):
    chats = owners_silent_chat_ids()
    if not chats:
        return

    report_day = previous_business_day(now_local())
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

        msg = (
            f"📌 Norah Daily Post\n"
            f"Day: {fmt_day_ddmmyyyy(report_day)}\n"
            f"Total Sales Day: {euro_comma(total_sales)}\n\n"
            f"Visa: {euro_comma(visa)}\n"
            f"Cash: {euro_comma(cash)}\n"
            f"Tips: {euro_comma(tips)}\n\n"
            f"Lunch: {euro_comma(lunch_sales)}\n"
            f"Pax: {int(lunch_pax)}\n"
            f"Average pax: {euro_comma(lunch_avg)}\n"
            f"Walk in: {int(lunch_walkins)}\n"
            f"No show: {int(lunch_noshows)}\n\n"
            f"Dinner: {euro_comma(dinner_sales)}\n"
            f"Pax: {int(dinner_pax)}\n"
            f"Average pax: {euro_comma(dinner_avg)}\n"
            f"Walk in: {int(dinner_walkins)}\n"
            f"No show: {int(dinner_noshows)}\n\n"
            f"📝 Notes:\n{notes_block}"
        )
    else:
        msg = (
            f"📌 Norah Daily Post\n"
            f"Day: {fmt_day_ddmmyyyy(report_day)}\n"
            f"Total Sales Day: —\n\n"
            f"Visa: —\n"
            f"Cash: —\n"
            f"Tips: —\n\n"
            f"Lunch: —\n"
            f"Pax: —\n"
            f"Average pax: —\n"
            f"Walk in: —\n"
            f"No show: —\n\n"
            f"Dinner: —\n"
            f"Pax: —\n"
            f"Average pax: —\n"
            f"Walk in: —\n"
            f"No show: —\n\n"
            f"📝 Notes:\n{notes_block}"
        )

    for chat_id in chats:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            print(f"Daily post send failed for chat {chat_id}: {e}")

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

    # Full daily
    app.add_handler(CommandHandler("setfull", setfull))
    app.add_handler(CommandHandler("setfullguided", setfullguided))
    app.add_handler(CommandHandler("confirmfull", confirmfull))
    app.add_handler(CommandHandler("cancelfull", cancelfull))

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
