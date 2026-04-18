"""
Agora POS integration module.

Provides get_daily_sales(date) for fetching live sales data from the Agora POS.
Not wired into the bot yet — import and call once port forwarding is set up
so Railway can reach the Agora server.

Required env vars:
    AGORA_URL       e.g. http://192.168.1.10:8984  (or public URL after port forward)
    AGORA_USER      e.g. Angie
    AGORA_PASSWORD  e.g. 1543
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
AGORA_MACHINE_ID = "582a8d9b-9fba-eae6-75c4-a4658936424f"

# TimeFrame strings Agora uses for lunch and dinner (Spanish)
_LUNCH_FRAMES  = {"mediodía", "mediodia", "almuerzo", "comida"}
_DINNER_FRAMES = {"noche", "cena", "tarde"}


# =============================================================================
# Return type
# =============================================================================

@dataclass
class DailySales:
    date: str                          # "YYYY-MM-DD"
    total_net: float                   # total revenue inc VAT
    total_gross: float                 # total revenue ex VAT
    lunch_net: float                   # lunch revenue inc VAT
    dinner_net: float                  # dinner revenue inc VAT
    lunch_covers: int                  # unique lunch tickets (proxy for covers)
    dinner_covers: int                 # unique dinner tickets
    total_covers: int                  # lunch + dinner covers
    avg_ticket: float                  # total_net / total_covers (0 if no covers)
    lunch_avg_ticket: float
    dinner_avg_ticket: float
    top_products: list = field(default_factory=list)   # list of {product, net, qty}
    line_items: list  = field(default_factory=list)    # raw rows (full detail)
    raw_items: int = 0                                 # total line items before aggregation


# =============================================================================
# Internal HTTP helper
# =============================================================================

def _post(endpoint: str, body: dict, cookie: str = "") -> tuple[int, str, str]:
    """POST JSON to endpoint. Returns (status, set_cookie_header, response_text)."""
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(AGORA_URL + endpoint, data=payload, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            if raw[:2] == b'\x1f\x8b':        # gzip magic bytes
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
    """Login to Agora. Returns (auth_token, session_dict)."""
    if not AGORA_USER or not AGORA_PASSWORD:
        raise RuntimeError("AGORA_USER and AGORA_PASSWORD env vars must be set")

    msg = {
        "CLRType": "IGT.POS.Bus.Security.Messages.LoginRequest",
        "IsBlocking": False,
        "OutOfBandMessages": [],
        "Sender": {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.4.2",
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
# Fetch raw sales line items for a date range
# =============================================================================

def _fetch_sales_rows(auth_token: str, session: dict, from_date: str, to_date: str) -> list[dict]:
    """Fetch raw product-level sales rows from Agora for the given date range."""
    msg = {
        "CLRType": "IGT.POS.Bus.Reporting.Messages.GetSalesAnalyticsReportRequest",
        "IsBlocking": True,
        "OutOfBandMessages": [],
        "Sender": {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.4.2",
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
# Aggregate raw rows into DailySales
# =============================================================================

def _aggregate(query_date: str, rows: list[dict]) -> DailySales:
    total_net   = 0.0
    total_gross = 0.0
    lunch_net   = 0.0
    dinner_net  = 0.0
    lunch_tickets:  set[str] = set()
    dinner_tickets: set[str] = set()
    product_net: dict[str, float] = {}
    product_qty: dict[str, float] = {}

    for r in rows:
        net   = float(r.get("Net",   0) or 0)
        gross = float(r.get("Gross", 0) or 0)
        tf    = (r.get("TimeFrame") or "").strip().lower()
        doc   = r.get("DocumentNumber") or ""
        prod  = r.get("Product") or "—"
        qty   = float(r.get("Quantity", 0) or 0)

        total_net   += net
        total_gross += gross

        if any(w in tf for w in _LUNCH_FRAMES):
            lunch_net += net
            lunch_tickets.add(doc)
        elif any(w in tf for w in _DINNER_FRAMES):
            dinner_net += net
            dinner_tickets.add(doc)

        product_net[prod] = product_net.get(prod, 0.0) + net
        product_qty[prod] = product_qty.get(prod, 0.0) + qty

    lunch_covers  = len(lunch_tickets)
    dinner_covers = len(dinner_tickets)
    total_covers  = lunch_covers + dinner_covers

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
        dinner_net=round(dinner_net, 2),
        lunch_covers=lunch_covers,
        dinner_covers=dinner_covers,
        total_covers=total_covers,
        avg_ticket=round(total_net / total_covers, 2) if total_covers else 0.0,
        lunch_avg_ticket=round(lunch_net / lunch_covers, 2) if lunch_covers else 0.0,
        dinner_avg_ticket=round(dinner_net / dinner_covers, 2) if dinner_covers else 0.0,
        top_products=top_products,
        line_items=rows,
        raw_items=len(rows),
    )


# =============================================================================
# Public API
# =============================================================================

def get_daily_sales(query_date) -> Optional[DailySales]:
    """
    Fetch and aggregate sales data from Agora POS for a single date.

    Args:
        query_date: a date object or "YYYY-MM-DD" string

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

    return _aggregate(date_str, rows)


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
    print(f"Lunch:          €{result.lunch_net:.2f}  ({result.lunch_covers} tickets, avg €{result.lunch_avg_ticket:.2f})")
    print(f"Dinner:         €{result.dinner_net:.2f}  ({result.dinner_covers} tickets, avg €{result.dinner_avg_ticket:.2f})")
    print(f"Line items:     {result.raw_items}")
    print(f"\nTop 10 products:")
    for i, p in enumerate(result.top_products, 1):
        print(f"  {i:>2}. {p['product']:<35}  €{p['net']:>8.2f}  (qty {p['qty']})")
