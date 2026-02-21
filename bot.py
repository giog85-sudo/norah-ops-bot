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

# Backwards-compatible timezone env names:
TZ_NAME = (os.getenv("TZ_NAME") or os.getenv("TIMEZONE") or "Europe/Madrid").strip() or "Europe/Madrid"
CUTOFF_HOUR = int((os.getenv("CUTOFF_HOUR", "11").strip() or "11"))  # business day cutoff next day
WEEKLY_DIGEST_HOUR = int((os.getenv("WEEKLY_DIGEST_HOUR", "9").strip() or "9"))  # Monday digest hour

# Daily auto-post to owners "silent" group:
DAILY_POST_HOUR = int((os.getenv("DAILY_POST_HOUR", "11").strip() or "11"))
DAILY_POST_MINUTE = int((os.getenv("DAILY_POST_MINUTE", "5").strip() or "5"))

# Access mode:
# OPEN -> anyone can use commands
# RESTRICTED -> only users in ALLOWED_USER_IDS can use admin/setup commands
ACCESS_MODE = (os.getenv("ACCESS_MODE", "RESTRICTED").strip().upper() or "RESTRICTED")
ACCESS_MODE = "OPEN" if ACCESS_MODE == "OPEN" else "RESTRICTED"

# Allowed users (comma-separated user IDs)
ALLOWED_USER_IDS = set()
_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
if _raw:
    for x in _raw.split(","):
        x = x.strip()
        if x.isdigit():
            ALLOWED_USER_IDS.add(int(x))

TZ = ZoneInfo(TZ_NAME)

# For report-mode capture:
# key = f"{chat_id}:{user_id}" -> dict state
REPORT_MODE_KEY = "report_mode_map"

# For full-daily mode capture:
FULL_MODE_KEY = "full_mode_map"

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
    # OPEN means everyone can run admin/setup commands
    if ACCESS_MODE == "OPEN":
        return True
    # RESTRICTED means only ALLOWED_USER_IDS
    if not ALLOWED_USER_IDS:
        # Safety fallback: if not set, treat as OPEN to avoid locking yourself out
        return True
    uid = user_id(update)
    return bool(uid and uid in ALLOWED_USER_IDS)


async def guard_admin(update: Update, *, reply_in_private_only: bool = True) -> bool:
    """
    For admin/setup commands: if unauthorized, optionally reply (prefer only in private chat to avoid spam).
    """
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
            # Daily sales/covers (base table)
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

            # --- MIGRATION: extend daily_stats for "full daily report" fields ---
            # Totals
            cur.execute("ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS visa DOUBLE PRECISION;")
            cur.execute("ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS cash DOUBLE PRECISION;")
            cur.execute("ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS tips DOUBLE PRECISION;")

            # Lunch
            cur.execute("ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS lunch_sales DOUBLE PRECISION;")
            cur.execute("ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS lunch_pax INTEGER;")
            cur.execute("ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS lunch_walkins INTEGER;")
            cur.execute("ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS lunch_noshows INTEGER;")

            # Dinner
            cur.execute("ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS dinner_sales DOUBLE PRECISION;")
            cur.execute("ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS dinner_pax INTEGER;")
            cur.execute("ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS dinner_walkins INTEGER;")
            cur.execute("ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS dinner_noshows INTEGER;")

            # Notes (multiple entries per day possible)
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

            # Settings (legacy owners chats + misc)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

            # Chat roles
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


# ---- Legacy owners chat ids (kept) ----
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


# ---- Chat roles helpers ----
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
    # Prefer role-based; fallback to legacy
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


def previous_business_day(ts: datetime | None = None) -> date:
    ts = ts or now_local()
    return business_day_for(ts) - timedelta(days=1)


def parse_yyyy_mm_dd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


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
    end: date  # inclusive


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
    text = re.sub(r"[^a-z0-9áéíóúñüç]+", " ", text)
    words = [w.strip() for w in text.split() if w.strip()]
    return [w for w in words if w not in STOPWORDS and len(w) >= 3]


# =========================
# NUMBER / PARSING HELPERS
# =========================
def parse_num(s: str) -> float:
    """
    Parses numbers like:
    - 7199,50
    - 6.400,30
    - 6400.30
    - 799
    """
    if s is None:
        raise ValueError("missing number")
    t = str(s).strip()
    t = t.replace("€", "").replace(" ", "")
    # If both '.' and ',' present: assume '.' thousands and ',' decimals (EU format)
    if "," in t and "." in t:
        t = t.replace(".", "")
        t = t.replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    return float(t)


def parse_int(s: str) -> int:
    if s is None:
        raise ValueError("missing int")
    t = str(s).strip()
    t = t.replace(" ", "")
    return int(re.sub(r"[^\d\-]+", "", t))


def parse_day_any(s: str) -> date:
    t = (s or "").strip()
    # yyyy-mm-dd
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return parse_yyyy_mm_dd(t)
    # dd/mm/yyyy
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", t):
        return datetime.strptime(t, "%d/%m/%Y").date()
    raise ValueError("Invalid day format")


def parse_full_report_block(text: str) -> dict:
    """
    Parses the "full daily report" block:

    Day: 24/01/2026
    Total Sales Day:7199,50
    Visa:6400,3
    Cash:799,2
    Tips: 103,60

    Lunch:2341,30
    Pax: 50
    Average pax:46,82
    Walk in:3
    No show: 7

    Dinner:4858,20
    Pax: 106
    Average pax:45,83
    Walk in:2
    No show:4
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip()]

    out: dict = {
        "day": None,
        "total_sales": None,
        "visa": None,
        "cash": None,
        "tips": None,
        "lunch_sales": None,
        "lunch_pax": None,
        "lunch_walkins": None,
        "lunch_noshows": None,
        "dinner_sales": None,
        "dinner_pax": None,
        "dinner_walkins": None,
        "dinner_noshows": None,
    }

    section: str | None = None  # None / "lunch" / "dinner"

    for raw in lines:
        ln = re.sub(r"\s*:\s*", ":", raw)
        low = ln.lower()

        m = re.match(r"^(day|fecha):(.+)$", low)
        if m:
            out["day"] = parse_day_any(m.group(2).strip())
            continue

        m = re.match(r"^(total sales day|total sales|sales day|ventas dia|ventas día|total ventas):(.+)$", low)
        if m:
            out["total_sales"] = parse_num(m.group(2))
            continue

        m = re.match(r"^(visa|card|tarjeta):(.+)$", low)
        if m:
            out["visa"] = parse_num(m.group(2))
            continue

        m = re.match(r"^(cash|efectivo):(.+)$", low)
        if m:
            out["cash"] = parse_num(m.group(2))
            continue

        m = re.match(r"^(tips|propinas|tip):(.+)$", low)
        if m:
            out["tips"] = parse_num(m.group(2))
            continue

        m = re.match(r"^(lunch|almuerzo|comida):(.+)$", low)
        if m:
            section = "lunch"
            out["lunch_sales"] = parse_num(m.group(2))
            continue

        m = re.match(r"^(dinner|cena):(.+)$", low)
        if m:
            section = "dinner"
            out["dinner_sales"] = parse_num(m.group(2))
            continue

        m = re.match(r"^(pax|covers|guests|comensales):(.+)$", low)
        if m and section in ("lunch", "dinner"):
            out[f"{section}_pax"] = parse_int(m.group(2))
            continue

        # ignore "Average pax" lines (we compute)
        if low.startswith("average") or low.startswith("avg"):
            continue

        m = re.match(r"^(walk in|walk-in|walkins|walk-ins|sin reserva):(.+)$", low)
        if m and section in ("lunch", "dinner"):
            out[f"{section}_walkins"] = parse_int(m.group(2))
            continue

        m = re.match(r"^(no show|no-show|noshows|no-shows|no asist|no se present):(.+)$", low)
        if m and section in ("lunch", "dinner"):
            out[f"{section}_noshows"] = parse_int(m.group(2))
            continue

    if not out["day"]:
        raise ValueError("Missing Day")

    if out["total_sales"] is None:
        if out["lunch_sales"] is not None or out["dinner_sales"] is not None:
            out["total_sales"] = float(out["lunch_sales"] or 0) + float(out["dinner_sales"] or 0)
        else:
            raise ValueError("Missing total sales")

    return out


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


def upsert_daily_full(
    day_: date,
    *,
    total_sales: float,
    visa: float | None,
    cash: float | None,
    tips: float | None,
    lunch_sales: float | None,
    lunch_pax: int | None,
    lunch_walkins: int | None,
    lunch_noshows: int | None,
    dinner_sales: float | None,
    dinner_pax: int | None,
    dinner_walkins: int | None,
    dinner_noshows: int | None,
):
    covers_total = int((lunch_pax or 0) + (dinner_pax or 0))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO daily_stats (
                    day, sales, covers,
                    visa, cash, tips,
                    lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
                    dinner_sales, dinner_pax, dinner_walkins, dinner_noshows
                )
                VALUES (
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (day)
                DO UPDATE SET
                    sales = EXCLUDED.sales,
                    covers = EXCLUDED.covers,
                    visa = EXCLUDED.visa,
                    cash = EXCLUDED.cash,
                    tips = EXCLUDED.tips,
                    lunch_sales = EXCLUDED.lunch_sales,
                    lunch_pax = EXCLUDED.lunch_pax,
                    lunch_walkins = EXCLUDED.lunch_walkins,
                    lunch_noshows = EXCLUDED.lunch_noshows,
                    dinner_sales = EXCLUDED.dinner_sales,
                    dinner_pax = EXCLUDED.dinner_pax,
                    dinner_walkins = EXCLUDED.dinner_walkins,
                    dinner_noshows = EXCLUDED.dinner_noshows;
                """,
                (
                    day_,
                    float(total_sales),
                    covers_total if covers_total > 0 else None,
                    visa,
                    cash,
                    tips,
                    lunch_sales,
                    lunch_pax,
                    lunch_walkins,
                    lunch_noshows,
                    dinner_sales,
                    dinner_pax,
                    dinner_walkins,
                    dinner_noshows,
                ),
            )
        conn.commit()


def get_daily(day_: date):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT sales, covers FROM daily_stats WHERE day=%s;", (day_,))
            row = cur.fetchone()
    return row


def get_daily_full(day_: date):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    sales, covers,
                    visa, cash, tips,
                    lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
                    dinner_sales, dinner_pax, dinner_walkins, dinner_noshows
                FROM daily_stats
                WHERE day=%s;
                """,
                (day_,),
            )
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


def sum_full_fields(p: Period):
    """
    Sums extra fields for periods.
    Returns a count of days that have ANY full-field data.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(tips),0),
                    COALESCE(SUM(lunch_sales),0),
                    COALESCE(SUM(dinner_sales),0),
                    COALESCE(SUM(lunch_pax),0),
                    COALESCE(SUM(dinner_pax),0),
                    COALESCE(SUM(lunch_walkins),0),
                    COALESCE(SUM(dinner_walkins),0),
                    COALESCE(SUM(lunch_noshows),0),
                    COALESCE(SUM(dinner_noshows),0),
                    COUNT(*) FILTER (
                        WHERE tips IS NOT NULL
                           OR lunch_sales IS NOT NULL OR dinner_sales IS NOT NULL
                           OR lunch_pax IS NOT NULL OR dinner_pax IS NOT NULL
                           OR lunch_walkins IS NOT NULL OR dinner_walkins IS NOT NULL
                           OR lunch_noshows IS NOT NULL OR dinner_noshows IS NOT NULL
                           OR visa IS NOT NULL OR cash IS NOT NULL
                    )
                FROM daily_stats
                WHERE day BETWEEN %s AND %s;
                """,
                (p.start, p.end),
            )
            row = cur.fetchone()

    (
        tips_sum,
        lunch_sales_sum,
        dinner_sales_sum,
        lunch_pax_sum,
        dinner_pax_sum,
        lunch_walkins_sum,
        dinner_walkins_sum,
        lunch_noshows_sum,
        dinner_noshows_sum,
        full_days,
    ) = row

    return {
        "tips_sum": float(tips_sum),
        "lunch_sales_sum": float(lunch_sales_sum),
        "dinner_sales_sum": float(dinner_sales_sum),
        "lunch_pax_sum": int(lunch_pax_sum),
        "dinner_pax_sum": int(dinner_pax_sum),
        "lunch_walkins_sum": int(lunch_walkins_sum),
        "dinner_walkins_sum": int(dinner_walkins_sum),
        "lunch_noshows_sum": int(lunch_noshows_sum),
        "dinner_noshows_sum": int(dinner_noshows_sum),
        "full_days": int(full_days),
    }


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
# FULL MODE STATE
# =========================
def _full_map(app: Application) -> dict[str, dict]:
    m = app.bot_data.get(FULL_MODE_KEY)
    if not isinstance(m, dict):
        m = {}
        app.bot_data[FULL_MODE_KEY] = m
    return m


def set_full_mode(app: Application, chat_id: int, user_id: int, day_: date | None):
    key = f"{chat_id}:{user_id}"
    _full_map(app)[key] = {
        "on": True,
        "day": day_.isoformat() if day_ else None,
        "ts": now_local().isoformat(),
    }


def clear_full_mode(app: Application, chat_id: int, user_id: int):
    key = f"{chat_id}:{user_id}"
    _full_map(app).pop(key, None)


def get_full_mode(app: Application, chat_id: int, user_id: int):
    key = f"{chat_id}:{user_id}"
    return _full_map(app).get(key)


# =========================
# PERMISSION BY CHAT ROLE
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


# =========================
# RENDER HELPERS
# =========================
def fmt_eur(x: float | None) -> str:
    if x is None:
        return "—"
    try:
        return f"€{float(x):.2f}"
    except:
        return "—"


def fmt_int(x: int | None) -> str:
    if x is None:
        return "—"
    try:
        return str(int(x))
    except:
        return "—"


def safe_div(a: float, b: float) -> float:
    return (a / b) if b else 0.0


def render_day_report(day_: date, row_full, notes_texts: list[str], *, title: str) -> str:
    if not row_full:
        sales_line = "Sales: —\nCovers: —\nAvg ticket: —"
        notes_block = "No notes submitted." if not notes_texts else "\n\n— — —\n\n".join(notes_texts)
        return (
            f"{title}\n"
            f"Business day: {day_.isoformat()}\n\n"
            f"{sales_line}\n\n"
            f"📝 Notes:\n{notes_block}"
        )

    (
        sales, covers,
        visa, cash, tips,
        lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
        dinner_sales, dinner_pax, dinner_walkins, dinner_noshows
    ) = row_full

    sales = float(sales or 0)
    covers = int(covers or 0)
    avg_total = safe_div(sales, covers)

    has_full = any(
        x is not None for x in (
            visa, cash, tips,
            lunch_sales, lunch_pax, lunch_walkins, lunch_noshows,
            dinner_sales, dinner_pax, dinner_walkins, dinner_noshows
        )
    )

    notes_block = "No notes submitted." if not notes_texts else "\n\n— — —\n\n".join(notes_texts)

    if not has_full:
        return (
            f"{title}\n"
            f"Business day: {day_.isoformat()}\n\n"
            f"Sales: {fmt_eur(sales)}\n"
            f"Covers: {covers}\n"
            f"Avg ticket: {fmt_eur(avg_total)}\n\n"
            f"📝 Notes:\n{notes_block}"
        )

    lp = int(lunch_pax or 0)
    dp = int(dinner_pax or 0)
    pax_total = lp + dp
    l_avg = safe_div(float(lunch_sales or 0), lp) if lp else 0.0
    d_avg = safe_div(float(dinner_sales or 0), dp) if dp else 0.0

    walkins_total = int(lunch_walkins or 0) + int(dinner_walkins or 0)
    noshows_total = int(lunch_noshows or 0) + int(dinner_noshows or 0)

    lines = [
        f"{title}",
        f"Business day: {day_.isoformat()}",
        "",
        f"Revenue: {fmt_eur(sales)}",
        f"Visa: {fmt_eur(visa)}",
        f"Cash: {fmt_eur(cash)}",
        f"Tips: {fmt_eur(tips)}",
        "",
        "🍽 LUNCH",
        f"Sales: {fmt_eur(lunch_sales)}",
        f"Guests: {fmt_int(lunch_pax)}",
        f"Avg ticket: {fmt_eur(l_avg)}",
        f"Walk-ins: {fmt_int(lunch_walkins)}",
        f"No-shows: {fmt_int(lunch_noshows)}",
        "",
        "🌙 DINNER",
        f"Sales: {fmt_eur(dinner_sales)}",
        f"Guests: {fmt_int(dinner_pax)}",
        f"Avg ticket: {fmt_eur(d_avg)}",
        f"Walk-ins: {fmt_int(dinner_walkins)}",
        f"No-shows: {fmt_int(dinner_noshows)}",
        "",
        "Totals",
        f"Guests: {pax_total if pax_total else covers}",
        f"Avg ticket: {fmt_eur(avg_total)}",
        f"Walk-ins: {walkins_total}",
        f"No-shows: {noshows_total}",
        "",
        f"📝 Notes:\n{notes_block}",
    ]
    return "\n".join(lines)


# =========================
# COMMANDS
# =========================
HELP_TEXT = (
    "📌 Norah Ops commands\n\n"
    "Sales:\n"
    "/setdaily SALES COVERS  (uses business day)\n"
    "/setfull  (paste full daily report as next message)\n"
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
    "Setup (ADMIN):\n"
    "/setowners  (legacy: run in Owners Silent once)\n"
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


# ---- Chat role setup ----
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

    # Keep legacy owners list in sync
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


# ---- Legacy owners setup ----
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
        await update.message.reply_text("Owners chats: NONE. Run /setowners (legacy) or /setchatrole OWNERS_SILENT.")
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


# --- DEBUG ---
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


# --- SALES ---
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


async def setfull(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setfull  (then paste the full report as the NEXT message)
    Optional override:
      /setfull YYYY-MM-DD
    """
    if not allow_sales_cmd(update):
        return

    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    day_override: date | None = None
    if context.args:
        try:
            day_override = parse_day_any(context.args[0])
        except:
            await update.message.reply_text("Usage: /setfull  (or /setfull YYYY-MM-DD)\nThen paste the full report as the next message.")
            return

    set_full_mode(context.application, chat.id, user.id, day_override)
    await update.message.reply_text(
        "✅ Full daily mode ON.\n"
        "Now paste the full daily report as your NEXT message.\n\n"
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
        "No show: 4\n\n"
        "To cancel: /cancelreport"
    )


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
    row = get_daily_full(day_)
    if not row:
        await update.message.reply_text(f"No data for business day {day_.isoformat()} yet. Use: /setdaily 2450 118  OR  /setfull")
        return
    msg = render_day_report(day_, row, notes_for_day(day_), title="📊 Norah Daily Report")
    await update.message.reply_text(msg)


def _append_full_analytics_block(msg: str, p: Period, total_sales: float, days_with_data: int) -> str:
    extras = sum_full_fields(p)
    full_days = extras["full_days"]
    if full_days <= 0:
        return msg

    walkins_total = extras["lunch_walkins_sum"] + extras["dinner_walkins_sum"]
    noshows_total = extras["lunch_noshows_sum"] + extras["dinner_noshows_sum"]
    pax_total = extras["lunch_pax_sum"] + extras["dinner_pax_sum"]
    tips_total = extras["tips_sum"]

    lunch_avg = safe_div(extras["lunch_sales_sum"], extras["lunch_pax_sum"])
    dinner_avg = safe_div(extras["dinner_sales_sum"], extras["dinner_pax_sum"])

    denom_days = full_days if full_days else days_with_data
    avg_walkins_per_day = safe_div(walkins_total, denom_days)
    walkins_rate = safe_div(walkins_total, pax_total)
    avg_tips_per_day = safe_div(tips_total, denom_days)
    tip_per_cover = safe_div(tips_total, pax_total)
    tips_rate = safe_div(tips_total, total_sales)

    msg += (
        f"\n\n🍽 Service split (weighted)\n"
        f"Lunch avg ticket: €{lunch_avg:.2f}\n"
        f"Dinner avg ticket: €{dinner_avg:.2f}\n"
        f"\n💶 Tips\n"
        f"Total tips: €{tips_total:.2f}\n"
        f"Avg tips/day: €{avg_tips_per_day:.2f}\n"
        f"Tip/cover: €{tip_per_cover:.2f}\n"
        f"Tips % of sales: {tips_rate*100:.1f}%\n"
        f"\n🚶 Walk-ins / No-shows\n"
        f"Total walk-ins: {walkins_total}\n"
        f"Avg walk-ins/day: {avg_walkins_per_day:.2f}\n"
        f"Walk-ins rate: {walkins_rate*100:.1f}%\n"
        f"Total no-shows: {noshows_total}\n"
    )
    return msg


async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_sales_cmd(update):
        return
    end = business_day_today()
    start = date(end.year, end.month, 1)
    p = Period(start=start, end=end)
    total_sales, total_covers, days_with_data = sum_daily(p)
    avg_ticket = safe_div(total_sales, total_covers)

    msg = (
        f"📈 Norah Month-to-Date\n"
        f"Period: {p.start.isoformat()} → {p.end.isoformat()} ({daterange_days(p)} day(s))\n\n"
        f"Days with data: {days_with_data}\n"
        f"Total sales: €{total_sales:.2f}\n"
        f"Total covers: {total_covers}\n"
        f"Avg ticket: €{avg_ticket:.2f}"
    )

    msg = _append_full_analytics_block(msg, p, total_sales, days_with_data)
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
    avg_ticket = safe_div(total_sales, total_covers)

    msg = (
        f"📊 Norah Summary\n"
        f"Period: {p.start.isoformat()} → {p.end.isoformat()} ({daterange_days(p)} day(s))\n\n"
        f"Days with data: {days_with_data}\n"
        f"Total sales: €{total_sales:.2f}\n"
        f"Total covers: {total_covers}\n"
        f"Avg ticket: €{avg_ticket:.2f}"
    )

    msg = _append_full_analytics_block(msg, p, total_sales, days_with_data)
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
    avg_ticket = safe_div(total_sales, total_covers)

    msg = (
        f"📊 Norah Range Report\n"
        f"Period: {p.start.isoformat()} → {p.end.isoformat()} ({daterange_days(p)} day(s))\n\n"
        f"Days with data: {days_with_data}\n"
        f"Total sales: €{total_sales:.2f}\n"
        f"Total covers: {total_covers}\n"
        f"Avg ticket: €{avg_ticket:.2f}"
    )

    msg = _append_full_analytics_block(msg, p, total_sales, days_with_data)
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
    avg = safe_div(float(sales), int(covers or 0))
    await update.message.reply_text(
        f"🏆 Best day (last 30)\n"
        f"Day: {d}\nSales: €{float(sales):.2f}\nCovers: {int(covers)}\nAvg ticket: €{avg:.2f}"
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
    avg = safe_div(float(sales), int(covers or 0))
    await update.message.reply_text(
        f"🧯 Worst day (last 30)\n"
        f"Day: {d}\nSales: €{float(sales):.2f}\nCovers: {int(covers)}\nAvg ticket: €{avg:.2f}"
    )


# --- NOTES ---
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_notes_cmd(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    day_ = business_day_today()
    set_report_mode(context.application, chat.id, user.id, day_)
    await update.message.reply_text(
        f"✅ Report mode ON.\n"
        f"Now send the notes as your NEXT message.\n"
        f"Business day: {day_.isoformat()}\n\n"
        f"To cancel: /cancelreport"
    )


async def cancelreport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_notes_cmd(update):
        return
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    clear_report_mode(context.application, chat.id, user.id)
    clear_full_mode(context.application, chat.id, user.id)
    await update.message.reply_text("❎ Mode cancelled.")


async def reportdaily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_notes_cmd(update):
        return
    day_ = business_day_today()
    texts = notes_for_day(day_)
    if not texts:
        await update.message.reply_text(
            f"No notes saved for business day {day_.isoformat()} yet.\nUse /report to submit notes."
        )
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


# --- NOTES ANALYTICS ---
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
# TEXT HANDLER (captures full after /setfull AND notes after /report)
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

    # 1) FULL mode capture
    fm = get_full_mode(context.application, chat.id, user.id)
    if fm and fm.get("on"):
        if not allow_sales_cmd(update):
            clear_full_mode(context.application, chat.id, user.id)
            return

        try:
            parsed = parse_full_report_block(msg_text)

            day_override = fm.get("day")
            day_ = parse_yyyy_mm_dd(day_override) if day_override else parsed["day"]

            total_sales = float(parsed["total_sales"] or 0)

            upsert_daily_full(
                day_,
                total_sales=total_sales,
                visa=parsed["visa"],
                cash=parsed["cash"],
                tips=parsed["tips"],
                lunch_sales=parsed["lunch_sales"],
                lunch_pax=parsed["lunch_pax"],
                lunch_walkins=parsed["lunch_walkins"],
                lunch_noshows=parsed["lunch_noshows"],
                dinner_sales=parsed["dinner_sales"],
                dinner_pax=parsed["dinner_pax"],
                dinner_walkins=parsed["dinner_walkins"],
                dinner_noshows=parsed["dinner_noshows"],
            )

            clear_full_mode(context.application, chat.id, user.id)

            lp = int(parsed["lunch_pax"] or 0)
            dp = int(parsed["dinner_pax"] or 0)
            pax_total = lp + dp
            avg_total = safe_div(total_sales, pax_total) if pax_total else 0.0

            walkins_total = int(parsed["lunch_walkins"] or 0) + int(parsed["dinner_walkins"] or 0)
            noshows_total = int(parsed["lunch_noshows"] or 0) + int(parsed["dinner_noshows"] or 0)

            await update.message.reply_text(
                "Saved ✅ Full daily report\n"
                f"Day: {day_.isoformat()}\n"
                f"Revenue: €{total_sales:.2f}\n"
                f"Pax total: {pax_total if pax_total else '—'}\n"
                f"Avg ticket total: €{avg_total:.2f}\n"
                f"Walk-ins total: {walkins_total}\n"
                f"No-shows total: {noshows_total}\n"
                f"Tips: {fmt_eur(parsed['tips'])}"
            )
        except Exception as e:
            await update.message.reply_text(
                "❌ Could not parse that full report.\n"
                "Please keep the same format and include at least:\n"
                "- Day: dd/mm/yyyy\n"
                "- Total Sales Day: ...\n"
                "- Lunch: ... and Dinner: ... with Pax\n\n"
                f"Error: {str(e)[:120]}"
            )
        return

    # 2) NOTES report-mode capture
    rm = get_report_mode(context.application, chat.id, user.id)
    if rm and rm.get("on"):
        if not allow_notes_cmd(update):
            clear_report_mode(context.application, chat.id, user.id)
            return

        day_str = rm.get("day")
        day_ = parse_yyyy_mm_dd(day_str) if day_str else business_day_today()

        insert_note_entry(day_, chat.id, user.id, msg_text)
        clear_report_mode(context.application, chat.id, user.id)

        await update.message.reply_text(f"Saved 📝 Notes for business day {day_.isoformat()}.")
        return

    # 3) Keep OWNERS_SILENT clean
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
    avg_ticket_7 = safe_div(total_sales_7, total_covers_7)

    prev_end = p7.start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)
    pprev = Period(prev_start, prev_end)
    total_sales_prev, total_covers_prev, _ = sum_daily(pprev)
    avg_ticket_prev = safe_div(total_sales_prev, total_covers_prev)

    def pct(a, b):
        if b == 0:
            return None
        return (a - b) / b * 100.0

    sales_delta = pct(total_sales_7, total_sales_prev)
    ticket_delta = pct(avg_ticket_7, avg_ticket_prev)

    best = best_or_worst_day(p7, worst=False)
    worst = best_or_worst_day(p7, worst=True)

    rows = notes_in_period(p7)
    counter = Counter()
    for _, txt in rows:
        counter.update(tokenize(txt))
    top_words = counter.most_common(8)
    top_words_str = ", ".join(f"{w}({c})" for w, c in top_words) if top_words else "—"

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
            print(f"Weekly digest send failed for chat {chat_id}: {e}")


async def send_daily_post_to_owners(context: ContextTypes.DEFAULT_TYPE):
    chats = owners_silent_chat_ids()
    if not chats:
        return

    report_day = previous_business_day(now_local())
    row_full = get_daily_full(report_day)
    notes_texts = notes_for_day(report_day)

    msg = render_day_report(report_day, row_full, notes_texts, title="📌 Norah Daily Post")
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

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # Setup
    app.add_handler(CommandHandler("setchatrole", setchatrole_cmd))
    app.add_handler(CommandHandler("chats", chats_cmd))
    app.add_handler(CommandHandler("setowners", setowners))
    app.add_handler(CommandHandler("ownerslist", ownerslist))
    app.add_handler(CommandHandler("removeowners", removeowners))

    # Debug
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("ping", ping))

    # Sales
    app.add_handler(CommandHandler("setdaily", setdaily))
    app.add_handler(CommandHandler("setfull", setfull))
    app.add_handler(CommandHandler("edit", edit))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("last", last))
    app.add_handler(CommandHandler("range", range_cmd))
    app.add_handler(CommandHandler("bestday", bestday))
    app.add_handler(CommandHandler("worstday", worstday))

    # Notes
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("cancelreport", cancelreport))
    app.add_handler(CommandHandler("reportdaily", reportdaily))
    app.add_handler(CommandHandler("reportday", reportday))

    # Notes analytics
    app.add_handler(CommandHandler("noteslast", noteslast))
    app.add_handler(CommandHandler("findnote", findnote))
    app.add_handler(CommandHandler("soldout", soldout))
    app.add_handler(CommandHandler("complaints", complaints))

    # Text handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Schedules
    if app.job_queue is not None:
        app.job_queue.run_daily(
            send_weekly_digest,
            time=time(hour=WEEKLY_DIGEST_HOUR, minute=0, tzinfo=TZ),
            days=(0,),  # Monday
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
