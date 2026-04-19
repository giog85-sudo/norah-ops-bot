"""
Agora POS client — standalone exploration script.
Connects to Agora at BASE_URL, logs in, and fetches sales analytics.

Discovered format:
  - Login:  POST /auth/  with {CLRType, Message: {flat LoginRequest fields}}
  - Sales:  POST /bus/   with {CLRType, Message: {flat SalesAnalyticsRequest}}
  - Auth:   'auth-token' cookie returned by /auth/, sent on subsequent requests
  - Responses are gzip-compressed when data is present

Run: python3 agora_client.py
"""

import json
import re
import gzip
import urllib.request
import urllib.error
from datetime import date, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL       = "http://88.1.26.209:8984"
AGORA_USERNAME = "Angie"
AGORA_PASSWORD = "1543"
MACHINE_ID     = "582a8d9b-9fba-eae6-75c4-a4658936424f"

# Date to query — change as needed
QUERY_DATE = str(date.today())


# =============================================================================
# HTTP helper
# =============================================================================

def post_json(url, body, cookie=None):
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
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
        body_text = e.read().decode("utf-8", errors="replace")
        return e.code, "", body_text


# =============================================================================
# Login
# =============================================================================

def login():
    msg = {
        "CLRType": "IGT.POS.Bus.Security.Messages.LoginRequest",
        "IsBlocking": False,
        "OutOfBandMessages": [],
        "Sender": {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.5.6",
            "LanguageCode": "es",
            "MachineId": MACHINE_ID,
            "MachineName": "Web Device",
            "MachineType": 4,
            "PosId": None,
            "PosName": "",
            "UserId": None,
            "UserName": "",
        },
        "UserName": AGORA_USERNAME,
        "UserPassword": AGORA_PASSWORD,
        "RememberMe": False,
        "DefaultLanguageCode": "",
        "RestorePreviousSession": False,
        "NotificationToken": "",
    }
    status, set_cookie, text = post_json(BASE_URL + "/auth/", {"CLRType": msg["CLRType"], "Message": msg})
    if status != 200:
        print(f"Login failed — HTTP {status}")
        print(f"Set-Cookie: {set_cookie!r}")
        print(f"Response body:\n{text}")
        raise RuntimeError(f"Login failed: HTTP {status}")

    token_match = re.search(r"auth-token=([^;]+)", set_cookie)
    if not token_match:
        raise RuntimeError("Login succeeded but no auth-token cookie in response")

    auth_token = token_match.group(1)
    session = json.loads(text)["Message"]["Session"]
    print(f"Logged in as {session['UserName']} (UserId={session['UserId']})")
    return auth_token, session


# =============================================================================
# Sales analytics request
# =============================================================================

def get_sales(auth_token, session, from_date, to_date):
    msg = {
        "CLRType": "IGT.POS.Bus.Reporting.Messages.GetSalesAnalyticsReportRequest",
        "IsBlocking": True,
        "OutOfBandMessages": [],
        "Sender": {
            "ApplicationName": "AgoraWebAdmin",
            "ApplicationVersion": "8.5.6",
            "LanguageCode": "es",
            "MachineId": MACHINE_ID,
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
    status, _, text = post_json(
        BASE_URL + "/bus/",
        {"CLRType": msg["CLRType"], "Message": msg},
        cookie=f"auth-token={auth_token}",
    )
    if status != 200:
        raise RuntimeError(f"Sales request failed: HTTP {status} — {text[:200]}")
    if not text.strip():
        return []

    data = json.loads(text)
    return data.get("Message", {}).get("Report", {}).get("Sales", [])


# =============================================================================
# Summarise line items into daily totals
# =============================================================================

def summarise(rows):
    totals = {
        "total_net": 0.0,
        "total_gross": 0.0,
        "lunch_net": 0.0,
        "dinner_net": 0.0,
        "lunch_tickets": set(),
        "dinner_tickets": set(),
        "by_family": {},
        "by_category": {},
        "by_timeframe": {},
    }
    for r in rows:
        net   = float(r.get("Net",   0) or 0)
        gross = float(r.get("Gross", 0) or 0)
        tf    = (r.get("TimeFrame") or "").strip()
        doc   = r.get("DocumentNumber") or ""
        fam   = r.get("Family") or "—"
        cat   = r.get("Categories") or "—"

        totals["total_net"]   += net
        totals["total_gross"] += gross

        # Lunch / dinner split (Agora uses Spanish: Mediodía = lunch, Noche = dinner)
        if "Mediodía" in tf or "Mediodia" in tf or "mediodia" in tf.lower():
            totals["lunch_net"] += net
            totals["lunch_tickets"].add(doc)
        elif "Noche" in tf or "noche" in tf.lower() or "Cena" in tf:
            totals["dinner_net"] += net
            totals["dinner_tickets"].add(doc)

        # By time frame
        totals["by_timeframe"][tf] = totals["by_timeframe"].get(tf, 0.0) + net

        # By family
        totals["by_family"][fam] = totals["by_family"].get(fam, 0.0) + net

        # By category
        totals["by_category"][cat] = totals["by_category"].get(cat, 0.0) + net

    return totals


# =============================================================================
# Main
# =============================================================================

def main():
    print(f"Querying Agora POS at {BASE_URL}")
    print(f"Date: {QUERY_DATE}\n")

    auth_token, session = login()
    rows = get_sales(auth_token, session, QUERY_DATE, QUERY_DATE)

    if not rows:
        print(f"\nNo sales data found for {QUERY_DATE}.")
        return

    print(f"\n{len(rows)} line items returned\n")

    t = summarise(rows)

    lunch_covers  = len(t["lunch_tickets"])
    dinner_covers = len(t["dinner_tickets"])
    total_tickets = lunch_covers + dinner_covers

    print(f"{'='*50}")
    print(f"DAILY SUMMARY — {QUERY_DATE}")
    print(f"{'='*50}")
    print(f"Total revenue (inc VAT):  €{t['total_net']:.2f}")
    print(f"Total revenue (ex VAT):   €{t['total_gross']:.2f}")
    print(f"Tickets (covers proxy):   {total_tickets}  (lunch {lunch_covers}, dinner {dinner_covers})")
    print(f"Avg ticket (inc VAT):     €{t['total_net']/total_tickets:.2f}" if total_tickets else "Avg ticket: —")
    print(f"\nLunch revenue:  €{t['lunch_net']:.2f} ({lunch_covers} tickets)")
    print(f"Dinner revenue: €{t['dinner_net']:.2f} ({dinner_covers} tickets)")

    print(f"\nBy time frame:")
    for tf, v in sorted(t["by_timeframe"].items(), key=lambda x: -x[1]):
        print(f"  {tf:<20} €{v:>8.2f}")

    print(f"\nTop 10 families by revenue:")
    for fam, v in sorted(t["by_family"].items(), key=lambda x: -x[1])[:10]:
        print(f"  {fam:<25} €{v:>8.2f}")

    print(f"\nTop 15 categories by revenue:")
    for cat, v in sorted(t["by_category"].items(), key=lambda x: -x[1])[:15]:
        print(f"  {cat:<25} €{v:>8.2f}")

    print(f"\nSample rows (first 5):")
    for r in rows[:5]:
        print(f"  {r.get('TimeFrame',''):<12} {r.get('Product',''):<35} "
              f"x{r.get('Quantity','')}  €{r.get('Net','')}")


if __name__ == "__main__":
    main()
