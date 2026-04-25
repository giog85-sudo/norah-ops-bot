"""
Agora POS integration module.

Provides get_daily_sales(date) for fetching live sales data from the Agora POS.

Required env vars:
    AGORA_URL       e.g. http://192.168.1.10:8984  (or public URL after port forward)
    AGORA_USER      e.g. Angie
    AGORA_PASSWORD  e.g. 1543
    DATABASE_URL    PostgreSQL connection string (for auto-save to full_daily_stats)
"""

import json
import re
import gzip
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# ── Config from environment ───────────────────────────────────────────────────
AGORA_URL      = os.getenv("AGORA_URL",      "").rstrip("/")
AGORA_USER     = os.getenv("AGORA_USER",     "").strip()
AGORA_PASSWORD = os.getenv("AGORA_PASSWORD", "").strip()
DATABASE_URL   = os.getenv("DATABASE_URL",   "")
AGORA_MACHINE_ID = "582a8d9b-9fba-eae6-75c4-a4658936424f"

# TimeFrame values confirmed from live data:
#   Mediodía, Tarde  → lunch
#   Noche            → dinner
_LUNCH_FRAMES  = {"mediodía", "mediodia", "tarde", "almuerzo", "comida"}
_DINNER_FRAMES = {"noche", "cena"}


# =============================================================================
# Return type
# =============================================================================

@dataclass
class DailySales:
    date: str                       # "YYYY-MM-DD"

    # ── Revenue totals ────────────────────────────────────────────────────────
    total_net: float                # total revenue inc VAT
    total_gross: float              # total revenue ex VAT

    # ── Lunch service ─────────────────────────────────────────────────────────
    lunch_net: float                # lunch revenue inc VAT
    lunch_covers: int               # unique lunch tickets (covers proxy)
    lunch_avg_ticket: float

    # ── Dinner service ────────────────────────────────────────────────────────
    dinner_net: float               # dinner revenue inc VAT
    dinner_covers: int              # unique dinner tickets
    dinner_avg_ticket: float

    # ── Combined ──────────────────────────────────────────────────────────────
    total_covers: int               # lunch + dinner
    avg_ticket: float               # total_net / total_covers

    # ── Per-waiter breakdown ──────────────────────────────────────────────────
    # Each entry: {name, net, tickets, avg_ticket}
    waiters: list = field(default_factory=list)

    # ── Per-family breakdown ──────────────────────────────────────────────────
    # Each entry: {family, net, pct}  — sorted by net desc
    families: list = field(default_factory=list)

    # ── Top products ──────────────────────────────────────────────────────────
    # Each entry: {product, net, qty}  — top 10 by net
    top_products: list = field(default_factory=list)

    # ── Discounts ─────────────────────────────────────────────────────────────
    discounts_total: float = 0.0    # sum of all discount amounts
    discounts_count: int   = 0      # number of line items with a discount

    # ── Raw data ──────────────────────────────────────────────────────────────
    line_items: list = field(default_factory=list)
    raw_items: int   = 0


# =============================================================================
# Internal HTTP helper
# =============================================================================

def _post(endpoint: str, body: dict, cookie: str = "") -> tuple[int, str, str]:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(AGORA_URL + endpoint, data=payload, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            if raw[:2] == b'\x1f\x8b':
                raw = gzip.decompress(raw)
            return r.status, r.getheader("Set-Cookie") or "", raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, "", e.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"Agora request to {endpoint} failed: {e}") from e


# =============================================================================
# Login → auth-token cookie + session
# =============================================================================

def _login() -> tuple[str, dict]:
    if not AGORA_USER or not AGORA_PASSWORD:
        raise RuntimeError("AGORA_USER and AGORA_PASSWORD env vars must be set")

    msg = {
        "CLRType": "IGT.POS.Bus.Security.Messages.LoginRequest",
        "IsBlocking": False,
        "OutOfBandMessages": [],
        "Sender": {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.5.6",
            "LanguageCode": "es",
            "MachineId": AGORA_MACHINE_ID,
            "MachineName": "Web Device",
            "MachineType": 4,
            "PosId": None,
            "PosName": "",
            "UserId": None,
            "UserName": "",
        },
        "UserName": AGORA_USER,
        "UserPassword": AGORA_PASSWORD,
        "RememberMe": False,
        "DefaultLanguageCode": "",
        "RestorePreviousSession": False,
        "NotificationToken": "",
    }

    status, set_cookie, text = _post("/auth/", {"CLRType": msg["CLRType"], "Message": msg})
    if status != 200:
        raise RuntimeError(f"Agora login failed: HTTP {status} — {text[:300]}")

    match = re.search(r"auth-token=([^;]+)", set_cookie)
    if not match:
        raise RuntimeError("Login returned 200 but no auth-token cookie found")

    session = json.loads(text)["Message"]["Session"]
    return match.group(1), session


# =============================================================================
# Fetch raw sales line items
# =============================================================================

def _fetch_sales_rows(auth_token: str, session: dict, from_date: str, to_date: str) -> list[dict]:
    msg = {
        "CLRType": "IGT.POS.Bus.Reporting.Messages.GetSalesAnalyticsReportRequest",
        "IsBlocking": True,
        "OutOfBandMessages": [],
        "Sender": {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.5.6",
            "LanguageCode": "es",
            "MachineId": AGORA_MACHINE_ID,
            "MachineName": "Web Device",
            "MachineType": 4,
            "PosId": 0,
            "PosName": "",
            "UserId": session["UserId"],
            "UserName": session["UserName"],
        },
        "PosGroupsIds": [1],
        "TimeFrameGroupId": 1,
        "IncludeDeliveryNotes": False,
        "From": f"{from_date}T00:00:00.000",
        "To":   f"{to_date}T23:59:59.000",
    }

    status, _, text = _post(
        "/bus/",
        {"CLRType": msg["CLRType"], "Message": msg},
        cookie=f"auth-token={auth_token}",
    )

    if status == 401:
        raise RuntimeError("Agora /bus/ returned 401 — auth token may have expired")
    if status != 200:
        raise RuntimeError(f"Agora sales request failed: HTTP {status} — {text[:300]}")
    if not text.strip():
        return []

    return json.loads(text).get("Message", {}).get("Report", {}).get("Sales", [])


# =============================================================================
# Aggregate raw rows → DailySales
# =============================================================================

def _aggregate(query_date: str, rows: list[dict]) -> DailySales:
    total_net   = 0.0
    total_gross = 0.0
    lunch_net   = 0.0
    dinner_net  = 0.0
    # Each DocumentId is a unique check/ticket (one per table turn).
    # There is no per-guest pax field in GetSalesAnalyticsReportRequest rows —
    # Quantity is product units sold, not guest count.  Unique check count is
    # a proxy only.  The real cover count (Comensales) lives in the Z report
    # (GetCashRegisterReportRequest) — wire that in once the endpoint is confirmed.
    lunch_tickets:  set[int] = set()
    dinner_tickets: set[int] = set()

    # waiter: {name → {net, tickets: set[doc]}}
    waiter_net: dict[str, float]    = {}
    waiter_docs: dict[str, set]     = {}

    # family: {name → net}
    family_net: dict[str, float]    = {}

    # products: {name → {net, qty}}
    product_net: dict[str, float]   = {}
    product_qty: dict[str, float]   = {}

    discounts_total = 0.0
    discounts_count = 0

    for r in rows:
        net   = float(r.get("Net",   0) or 0)
        gross = float(r.get("Gross", 0) or 0)
        tf    = (r.get("TimeFrame") or "").strip().lower()
        # DocumentId is the numeric ticket/check ID — use it as the unique
        # cover proxy.  DocumentNumber is a display string ("T/006885") that
        # maps 1-to-1 with DocumentId, but int comparison is cheaper.
        doc   = r.get("DocumentId") or r.get("DocumentNumber") or None
        prod  = r.get("Product") or "—"
        qty   = float(r.get("Quantity", 0) or 0)
        user  = (r.get("User") or "").strip() or "Unknown"
        fam   = (r.get("Family") or "").strip() or "Other"

        # Discount — may be a float, a string amount, or None/empty
        disc_raw = r.get("Discount") or r.get("DiscountAmount") or 0
        try:
            disc = float(disc_raw) if disc_raw not in ("", None) else 0.0
        except (ValueError, TypeError):
            disc = 0.0
        if disc != 0.0:
            discounts_total += abs(disc)
            discounts_count += 1

        total_net   += net
        total_gross += gross

        # ── Service split ──────────────────────────────────────────────────
        if tf in _LUNCH_FRAMES:
            lunch_net += net
            if doc:
                lunch_tickets.add(doc)
        elif tf in _DINNER_FRAMES:
            dinner_net += net
            if doc:
                dinner_tickets.add(doc)

        # ── Per-waiter ─────────────────────────────────────────────────────
        waiter_net[user]  = waiter_net.get(user, 0.0) + net
        if user not in waiter_docs:
            waiter_docs[user] = set()
        if doc:
            waiter_docs[user].add(doc)

        # ── Per-family ─────────────────────────────────────────────────────
        family_net[fam] = family_net.get(fam, 0.0) + net

        # ── Products ───────────────────────────────────────────────────────
        product_net[prod] = product_net.get(prod, 0.0) + net
        product_qty[prod] = product_qty.get(prod, 0.0) + qty

    lunch_covers  = len(lunch_tickets)
    dinner_covers = len(dinner_tickets)
    total_covers  = lunch_covers + dinner_covers

    # ── Waiter list (sorted by net desc) ──────────────────────────────────────
    waiters = []
    for name, wnet in sorted(waiter_net.items(), key=lambda x: -x[1]):
        tix = len(waiter_docs.get(name, set()))
        waiters.append({
            "name":       name,
            "net":        round(wnet, 2),
            "tickets":    tix,
            "avg_ticket": round(wnet / tix, 2) if tix else 0.0,
        })

    # ── Family list (sorted by net desc, with % of total) ────────────────────
    families = []
    for fname, fnet in sorted(family_net.items(), key=lambda x: -x[1]):
        families.append({
            "family": fname,
            "net":    round(fnet, 2),
            "pct":    round(fnet / total_net * 100, 1) if total_net else 0.0,
        })

    # ── Top 10 products ───────────────────────────────────────────────────────
    top_products = sorted(
        [{"product": p, "net": round(v, 2), "qty": round(product_qty[p], 1)}
         for p, v in product_net.items()],
        key=lambda x: -x["net"],
    )[:10]

    return DailySales(
        date=query_date,
        total_net=round(total_net, 2),
        total_gross=round(total_gross, 2),
        lunch_net=round(lunch_net, 2),
        lunch_covers=lunch_covers,
        lunch_avg_ticket=round(lunch_net / lunch_covers, 2) if lunch_covers else 0.0,
        dinner_net=round(dinner_net, 2),
        dinner_covers=dinner_covers,
        dinner_avg_ticket=round(dinner_net / dinner_covers, 2) if dinner_covers else 0.0,
        total_covers=total_covers,
        avg_ticket=round(total_net / total_covers, 2) if total_covers else 0.0,
        waiters=waiters,
        families=families,
        top_products=top_products,
        discounts_total=round(discounts_total, 2),
        discounts_count=discounts_count,
        line_items=rows,
        raw_items=len(rows),
    )


# =============================================================================
# Auto-save to full_daily_stats
# Only writes Agora-sourced columns; never touches visa/cash/tips/walkins/noshows
# which are entered manually by staff.
# =============================================================================

def _save_to_db(ds: DailySales) -> None:
    db_url = DATABASE_URL
    if not db_url:
        return
    try:
        import psycopg
    except ImportError:
        return
    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO full_daily_stats
                        (day, total_sales, lunch_sales, lunch_pax, dinner_sales, dinner_pax)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (day) DO UPDATE SET
                        total_sales  = EXCLUDED.total_sales,
                        lunch_sales  = EXCLUDED.lunch_sales,
                        lunch_pax    = EXCLUDED.lunch_pax,
                        dinner_sales = EXCLUDED.dinner_sales,
                        dinner_pax   = EXCLUDED.dinner_pax
                    """,
                    (
                        ds.date,
                        ds.total_net,
                        ds.lunch_net,
                        ds.lunch_covers,
                        ds.dinner_net,
                        ds.dinner_covers,
                    ),
                )
            conn.commit()
        print(f"[agora] saved {ds.date} to full_daily_stats (total={ds.total_net})")
    except Exception as e:
        print(f"[agora] DB save failed for {ds.date}: {e}")


# =============================================================================
# Public API
# =============================================================================

def get_daily_sales(query_date, save_to_db: bool = True) -> Optional[DailySales]:
    """
    Fetch and aggregate sales data from Agora POS for a single date.

    Args:
        query_date:  a date object or "YYYY-MM-DD" string
        save_to_db:  if True (default), auto-save to full_daily_stats

    Returns:
        DailySales dataclass, or None if no data exists for that date.

    Raises:
        RuntimeError if Agora is unreachable or credentials are wrong.
    """
    if isinstance(query_date, date):
        date_str = query_date.isoformat()
    else:
        date_str = str(query_date)

    auth_token, session = _login()
    rows = _fetch_sales_rows(auth_token, session, date_str, date_str)

    if not rows:
        return None

    ds = _aggregate(date_str, rows)

    if save_to_db:
        _save_to_db(ds)

    return ds


# =============================================================================
# Payment methods — raw response probe
# =============================================================================

def get_payment_methods(query_date) -> dict:
    """
    Call GetPaymentMethodsReportRequest for query_date and return the full
    raw parsed JSON response so we can inspect the data structure.

    Args:
        query_date: a date object or "YYYY-MM-DD" string

    Returns:
        dict — the full parsed JSON from Agora (Message + any Error fields).

    Raises:
        RuntimeError if login fails or Agora is unreachable.
    """
    if not AGORA_URL:
        raise RuntimeError("AGORA_URL env var is not set")
    if not AGORA_USER or not AGORA_PASSWORD:
        raise RuntimeError("AGORA_USER and AGORA_PASSWORD env vars must be set")

    if isinstance(query_date, date):
        date_str = query_date.isoformat()
    else:
        date_str = str(query_date)

    auth_token, session = _login()

    clr = "IGT.POS.Bus.Reporting.Messages.GetPaymentMethodsReportRequest"
    msg = {
        "CLRType": clr,
        "IsBlocking": True,
        "OutOfBandMessages": [],
        "Sender": {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.5.6",
            "LanguageCode": "es",
            "MachineId": AGORA_MACHINE_ID,
            "MachineName": "Web Device",
            "MachineType": 4,
            "PosId": 0,
            "PosName": "",
            "UserId": session["UserId"],
            "UserName": session["UserName"],
        },
        "PosGroupsIds": [1],
        "TimeFrameGroupId": 1,
        "IncludeDeliveryNotes": False,
        "From": f"{date_str}T00:00:00.000",
        "To":   f"{date_str}T23:59:59.000",
    }

    status, _, text = _post(
        "/bus/",
        {"CLRType": clr, "Message": msg},
        cookie=f"auth-token={auth_token}",
    )

    if status == 401:
        raise RuntimeError("Agora /bus/ returned 401 — auth token may have expired")

    try:
        return {"http_status": status, "body": json.loads(text)}
    except Exception:
        return {"http_status": status, "body": text[:5000]}


# =============================================================================
# Cash register (Z report) — raw response probe
# =============================================================================

def get_cash_register_report(query_date) -> dict:
    """
    Try multiple variations of GetCashRegisterReportRequest for query_date.
    The Z report is per-terminal (POS "TPV") and is expected to contain
    Comensales (real cover count), payment method totals, and till summary.

    Returns a dict:
    {
        "date": str,
        "attempts": [
            {"label": str, "http_status": int, "body": parsed_json_or_str},
            ...
        ]
    }
    """
    if not AGORA_URL:
        raise RuntimeError("AGORA_URL env var is not set")
    if not AGORA_USER or not AGORA_PASSWORD:
        raise RuntimeError("AGORA_USER and AGORA_PASSWORD env vars must be set")

    if isinstance(query_date, date):
        date_str = query_date.isoformat()
    else:
        date_str = str(query_date)

    auth_token, session = _login()

    clr = "IGT.POS.Bus.Reporting.Messages.GetCashRegisterReportRequest"
    frm = f"{date_str}T00:00:00.000"
    to  = f"{date_str}T23:59:59.000"

    def _sender(**overrides):
        base = {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.5.6",
            "LanguageCode": "es",
            "MachineId": AGORA_MACHINE_ID,
            "MachineName": "Web Device",
            "MachineType": 4,
            "PosId": 0,
            "PosName": "",
            "UserId": session["UserId"],
            "UserName": session["UserName"],
        }
        base.update(overrides)
        return base

    def _msg(**extra):
        base = {
            "CLRType": clr,
            "IsBlocking": True,
            "OutOfBandMessages": [],
            "Sender": _sender(),
            "PosGroupsIds": [1],
            "TimeFrameGroupId": 1,
            "IncludeDeliveryNotes": False,
            "From": frm,
            "To":   to,
        }
        base.update(extra)
        return base

    variations = [
        # v1: exact mirror of working GetSalesAnalyticsReportRequest
        ("v1_baseline",
         _msg()),

        # v2: PosId=1 in Sender (TPV terminal id guess)
        ("v2_sender_posid_1",
         {**_msg(), "Sender": _sender(PosId=1)}),

        # v3: PosName="TPV" in Sender
        ("v3_sender_posname_tpv",
         {**_msg(), "Sender": _sender(PosName="TPV")}),

        # v4: top-level PosId field
        ("v4_toplevel_posid_1",
         _msg(PosId=1)),

        # v5: top-level PosName="TPV"
        ("v5_toplevel_posname_tpv",
         _msg(PosName="TPV")),

        # v6: top-level PosIds list (plural)
        ("v6_posids_list",
         _msg(PosIds=[1])),

        # v7: PosGroupsIds=[] (all groups)
        ("v7_pos_groups_empty",
         _msg(PosGroupsIds=[])),

        # v8: PosGroupsIds=None
        ("v8_pos_groups_null",
         _msg(PosGroupsIds=None)),

        # v9: TimeFrameGroupId=0
        ("v9_timeframe_0",
         _msg(TimeFrameGroupId=0)),

        # v10: minimal body — no group/timeframe fields
        ("v10_minimal",
         {
             "CLRType": clr,
             "IsBlocking": True,
             "OutOfBandMessages": [],
             "Sender": _sender(),
             "From": frm,
             "To":   to,
         }),

        # v11: CashRegisterId=1
        ("v11_cashregister_id_1",
         _msg(CashRegisterId=1)),

        # v12: CashRegisterIds=[1]
        ("v12_cashregister_ids",
         _msg(CashRegisterIds=[1])),
    ]

    attempts = []
    for label, body in variations:
        status, _, text = _post(
            "/bus/",
            {"CLRType": clr, "Message": body},
            cookie=f"auth-token={auth_token}",
        )
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"_raw": text[:5000]}

        err = (parsed.get("Error")
               or parsed.get("Message", {}).get("ErrorMessage")
               or parsed.get("Message", {}).get("Error"))

        attempts.append({
            "label":      label,
            "http_status": status,
            "error":      str(err)[:300] if err else None,
            "body":       parsed,
        })

        # Stop on first clean success
        if status == 200 and not err:
            break

    return {"date": date_str, "attempts": attempts}


# =============================================================================
# Z report (POS close-outs) probe
# =============================================================================

def get_pos_closeouts(query_date) -> dict:
    """
    Call GetPosCloseOutsRequest (the real Z report endpoint, discovered via
    network interception) and return the full raw JSON response.

    Tries multiple variations:
    - With date range (IsBlocking False and True)
    - Without dates at all
    - With PosGroupsIds variations

    Args:
        query_date: a date object or "YYYY-MM-DD" string

    Returns:
        dict with keys: date, attempts (label, http_status, error, body)
    """
    if not AGORA_URL:
        raise RuntimeError("AGORA_URL env var is not set")
    if not AGORA_USER or not AGORA_PASSWORD:
        raise RuntimeError("AGORA_USER and AGORA_PASSWORD env vars must be set")

    if isinstance(query_date, date):
        date_str = query_date.isoformat()
    else:
        date_str = str(query_date)

    auth_token, session = _login()

    CLR = "IGT.POS.Bus.Reporting.Messages.GetPosCloseOutsRequest"
    frm = f"{date_str}T00:00:00.000"
    to  = f"{date_str}T23:59:59.000"

    def _sender(pos_id=0):
        return {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.5.6",
            "LanguageCode": "es",
            "MachineId": AGORA_MACHINE_ID,
            "MachineName": "Web Device",
            "MachineType": 4,
            "PosId": pos_id,
            "PosName": "",
            "UserId": session["UserId"],
            "UserName": session["UserName"],
        }

    import datetime as _dt
    d0 = date.fromisoformat(date_str)
    d_prev = d0 - _dt.timedelta(days=1)

    # MachineId captured from Angie's browser traffic
    _CLOUD_MACHINE_ID = "c60c7180-c208-2554-1614-7bac32ddc4ed"

    def _sender_exact(pos_id=0):
        """Sender block matching exactly what Angie's browser sends."""
        return {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.5.6",
            "LanguageCode": "es",
            "MachineId": _CLOUD_MACHINE_ID,
            "MachineName": "Web Device",
            "MachineType": 4,
            "PosId": pos_id,
            "PosName": "",
            "UserId": 31,
            "UserName": "Angie",
        }

    def _base(sender_fn=None, **extra):
        msg = {
            "CLRType": CLR,
            "IsBlocking": False,
            "OutOfBandMessages": [],
            "Sender": (sender_fn or _sender_exact)(),
            # NOTE: correct field name is PosGroupIds (singular Group), not PosGroupsIds
            "PosGroupIds": [1],
        }
        msg.update(extra)
        return msg

    # Date strings in the format from the captured payload
    from_close = f"{d_prev.isoformat()}T00:00:00.000"
    to_close   = f"{d0.isoformat()}T00:00:00.000"

    variations = [
        # v1: EXACT payload from Angie's browser — this is the one that should work
        ("v1_exact_browser_payload",
         _base(
             FromCloseDate=from_close,
             ToCloseDate=to_close,
         )),

        # v2: same but ToCloseDate = end of day
        ("v2_to_end_of_day",
         _base(
             FromCloseDate=from_close,
             ToCloseDate=f"{d0.isoformat()}T23:59:59.000",
         )),

        # v3: widen range — last 7 days
        ("v3_last_7_days",
         _base(
             FromCloseDate=f"{(d0 - _dt.timedelta(days=7)).isoformat()}T00:00:00.000",
             ToCloseDate=f"{d0.isoformat()}T23:59:59.000",
         )),

        # v4: no date filter — return all records
        ("v4_no_dates",
         _base()),

        # v5: exact browser payload but with original MachineId (in case cloud vs local differs)
        ("v5_original_machine_id",
         {
             "CLRType": CLR,
             "IsBlocking": False,
             "OutOfBandMessages": [],
             "Sender": _sender(pos_id=0),
             "PosGroupIds": [1],
             "FromCloseDate": from_close,
             "ToCloseDate": to_close,
         }),

        # v6: IsBlocking=True with exact structure
        ("v6_blocking",
         {**_base(FromCloseDate=from_close, ToCloseDate=to_close), "IsBlocking": True}),
    ]

    attempts = []
    for label, body in variations:
        status, _, text = _post(
            "/bus/",
            {"CLRType": CLR, "Message": body},
            cookie=f"auth-token={auth_token}",
        )
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"_raw": text[:5000]}

        err = (parsed.get("Error")
               or parsed.get("Message", {}).get("ErrorMessage")
               or parsed.get("Message", {}).get("Error"))

        closeouts = parsed.get("Message", {}).get("PosCloseOuts")
        attempts.append({
            "label":          label,
            "http_status":    status,
            "error":          str(err)[:300] if err else None,
            "closeouts_count": len(closeouts) if isinstance(closeouts, list) else None,
            "body":           parsed,
        })

    return {"date": date_str, "attempts": attempts}


# =============================================================================
# Closure / DailyTotals extended probe
# =============================================================================

def get_closure_report2(query_date) -> dict:
    """
    Try GetClosureReportRequest and GetDailyTotalsReportRequest with many
    parameter variations — ClosureId, SessionId, no dates, etc.
    Returns all attempts with full raw responses.
    """
    if not AGORA_URL:
        raise RuntimeError("AGORA_URL env var is not set")
    if not AGORA_USER or not AGORA_PASSWORD:
        raise RuntimeError("AGORA_USER and AGORA_PASSWORD env vars must be set")

    if isinstance(query_date, date):
        date_str = query_date.isoformat()
    else:
        date_str = str(query_date)

    auth_token, session = _login()

    frm = f"{date_str}T00:00:00.000"
    to  = f"{date_str}T23:59:59.000"

    def _sender(pos_id=0, pos_name=""):
        return {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.5.6",
            "LanguageCode": "es",
            "MachineId": AGORA_MACHINE_ID,
            "MachineName": "Web Device",
            "MachineType": 4,
            "PosId": pos_id,
            "PosName": pos_name,
            "UserId": session["UserId"],
            "UserName": session["UserName"],
        }

    def _base(clr, **extra):
        msg = {
            "CLRType": clr,
            "IsBlocking": True,
            "OutOfBandMessages": [],
            "Sender": _sender(),
            "PosGroupsIds": [1],
            "TimeFrameGroupId": 1,
            "IncludeDeliveryNotes": False,
            "From": frm,
            "To":   to,
        }
        msg.update(extra)
        return msg

    CLR_CLOSURE = "IGT.POS.Bus.Reporting.Messages.GetClosureReportRequest"
    CLR_DAILY   = "IGT.POS.Bus.Reporting.Messages.GetDailyTotalsReportRequest"

    variations = [
        # ── GetClosureReportRequest ──────────────────────────────────────────
        ("closure_baseline",
         CLR_CLOSURE, _base(CLR_CLOSURE)),

        ("closure_id_1",
         CLR_CLOSURE, _base(CLR_CLOSURE, ClosureId=1)),

        ("closure_id_0",
         CLR_CLOSURE, _base(CLR_CLOSURE, ClosureId=0)),

        ("closure_id_null",
         CLR_CLOSURE, _base(CLR_CLOSURE, ClosureId=None)),

        ("closure_session_id_1",
         CLR_CLOSURE, _base(CLR_CLOSURE, SessionId=1)),

        ("closure_session_id_0",
         CLR_CLOSURE, _base(CLR_CLOSURE, SessionId=0)),

        ("closure_no_dates",
         CLR_CLOSURE, {
             "CLRType": CLR_CLOSURE,
             "IsBlocking": True,
             "OutOfBandMessages": [],
             "Sender": _sender(),
             "PosGroupsIds": [1],
             "TimeFrameGroupId": 1,
         }),

        ("closure_pos_groups_empty",
         CLR_CLOSURE, _base(CLR_CLOSURE, PosGroupsIds=[])),

        ("closure_timeframe_0",
         CLR_CLOSURE, _base(CLR_CLOSURE, TimeFrameGroupId=0)),

        ("closure_minimal_no_groups",
         CLR_CLOSURE, {
             "CLRType": CLR_CLOSURE,
             "IsBlocking": True,
             "OutOfBandMessages": [],
             "Sender": _sender(),
             "From": frm,
             "To":   to,
         }),

        ("closure_with_session_and_id",
         CLR_CLOSURE, _base(CLR_CLOSURE, ClosureId=1, SessionId=1)),

        ("closure_posid_1_in_sender",
         CLR_CLOSURE, {**_base(CLR_CLOSURE), "Sender": _sender(pos_id=1)}),

        # ── GetDailyTotalsReportRequest ──────────────────────────────────────
        ("dailytotals_baseline",
         CLR_DAILY, _base(CLR_DAILY)),

        ("dailytotals_timeframe_0",
         CLR_DAILY, _base(CLR_DAILY, TimeFrameGroupId=0)),

        ("dailytotals_pos_groups_empty",
         CLR_DAILY, _base(CLR_DAILY, PosGroupsIds=[])),

        ("dailytotals_no_dates",
         CLR_DAILY, {
             "CLRType": CLR_DAILY,
             "IsBlocking": True,
             "OutOfBandMessages": [],
             "Sender": _sender(),
             "PosGroupsIds": [1],
             "TimeFrameGroupId": 1,
         }),

        ("dailytotals_minimal",
         CLR_DAILY, {
             "CLRType": CLR_DAILY,
             "IsBlocking": True,
             "OutOfBandMessages": [],
             "Sender": _sender(),
             "From": frm,
             "To":   to,
         }),

        ("dailytotals_session_id_1",
         CLR_DAILY, _base(CLR_DAILY, SessionId=1)),

        ("dailytotals_posid_1_in_sender",
         CLR_DAILY, {**_base(CLR_DAILY), "Sender": _sender(pos_id=1)}),

        ("dailytotals_pos_groups_null",
         CLR_DAILY, _base(CLR_DAILY, PosGroupsIds=None)),
    ]

    attempts = []
    for label, clr, body in variations:
        status, _, text = _post(
            "/bus/",
            {"CLRType": clr, "Message": body},
            cookie=f"auth-token={auth_token}",
        )
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"_raw": text[:5000]}

        err = (parsed.get("Error")
               or parsed.get("Message", {}).get("ErrorMessage")
               or parsed.get("Message", {}).get("Error"))

        attempts.append({
            "label":       label,
            "clr":         clr,
            "http_status": status,
            "error":       str(err)[:300] if err else None,
            "body":        parsed,
        })

    return {"date": date_str, "attempts": attempts}


# =============================================================================
# Closure report — raw response probe
# =============================================================================

def get_closure_report(query_date) -> dict:
    """
    Call GetClosureReportRequest for query_date and return the full raw
    parsed JSON response so we can inspect what fields it contains.

    Args:
        query_date: a date object or "YYYY-MM-DD" string

    Returns:
        dict with keys: http_status, body (full parsed JSON or raw text).

    Raises:
        RuntimeError if login fails or Agora is unreachable.
    """
    if not AGORA_URL:
        raise RuntimeError("AGORA_URL env var is not set")
    if not AGORA_USER or not AGORA_PASSWORD:
        raise RuntimeError("AGORA_USER and AGORA_PASSWORD env vars must be set")

    if isinstance(query_date, date):
        date_str = query_date.isoformat()
    else:
        date_str = str(query_date)

    auth_token, session = _login()

    clr = "IGT.POS.Bus.Reporting.Messages.GetClosureReportRequest"
    msg = {
        "CLRType": clr,
        "IsBlocking": True,
        "OutOfBandMessages": [],
        "Sender": {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.5.6",
            "LanguageCode": "es",
            "MachineId": AGORA_MACHINE_ID,
            "MachineName": "Web Device",
            "MachineType": 4,
            "PosId": 0,
            "PosName": "",
            "UserId": session["UserId"],
            "UserName": session["UserName"],
        },
        "PosGroupsIds": [1],
        "TimeFrameGroupId": 1,
        "IncludeDeliveryNotes": False,
        "From": f"{date_str}T00:00:00.000",
        "To":   f"{date_str}T23:59:59.000",
    }

    status, _, text = _post(
        "/bus/",
        {"CLRType": clr, "Message": msg},
        cookie=f"auth-token={auth_token}",
    )

    if status == 401:
        raise RuntimeError("Agora /bus/ returned 401 — auth token may have expired")

    try:
        return {"http_status": status, "body": json.loads(text)}
    except Exception:
        return {"http_status": status, "body": text[:5000]}


# =============================================================================
# Quick test — run directly to verify connectivity
# =============================================================================

if __name__ == "__main__":
    import sys

    test_date = sys.argv[1] if len(sys.argv) > 1 else str(date.today())
    print(f"Testing Agora integration for {test_date} ...\n")

    try:
        result = get_daily_sales(test_date)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if result is None:
        print("No data for that date.")
        sys.exit(0)

    print(f"Date:           {result.date}")
    print(f"Total revenue:  €{result.total_net:.2f}  (ex-VAT €{result.total_gross:.2f})")
    print(f"Total covers:   {result.total_covers}  (lunch {result.lunch_covers}, dinner {result.dinner_covers})")
    print(f"Avg ticket:     €{result.avg_ticket:.2f}")
    print(f"Lunch:          €{result.lunch_net:.2f}  ({result.lunch_covers} covers, avg €{result.lunch_avg_ticket:.2f})")
    print(f"Dinner:         €{result.dinner_net:.2f}  ({result.dinner_covers} covers, avg €{result.dinner_avg_ticket:.2f})")
    print(f"Discounts:      €{result.discounts_total:.2f}  ({result.discounts_count} items)")
    print(f"Line items:     {result.raw_items}")

    print(f"\nWaiters ({len(result.waiters)}):")
    for w in result.waiters:
        print(f"  {w['name']:<20}  €{w['net']:>8.2f}  {w['tickets']} tickets  avg €{w['avg_ticket']:.2f}")

    print(f"\nFamilies:")
    for f in result.families:
        print(f"  {f['family']:<25}  €{f['net']:>8.2f}  ({f['pct']}%)")

    print(f"\nTop 10 products:")
    for i, p in enumerate(result.top_products, 1):
        print(f"  {i:>2}. {p['product']:<35}  €{p['net']:>8.2f}  (qty {p['qty']})")
