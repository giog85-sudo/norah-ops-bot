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
    # the best available proxy for covers from this endpoint.
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
