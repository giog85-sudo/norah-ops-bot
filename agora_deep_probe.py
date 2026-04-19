"""
Agora IGT bus deep probe — calls 6 specific endpoints and returns their
full raw JSON response so we can inspect the exact data structure.

Can be run standalone:
    python3 agora_deep_probe.py [YYYY-MM-DD]

Or imported and called as:
    from agora_deep_probe import run_deep_probe
    results = run_deep_probe("2026-04-18")
"""

import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.request

AGORA_URL        = os.getenv("AGORA_URL",      "").rstrip("/")
AGORA_USER       = os.getenv("AGORA_USER",     "").strip()
AGORA_PASSWORD   = os.getenv("AGORA_PASSWORD", "").strip()
AGORA_MACHINE_ID = "582a8d9b-9fba-eae6-75c4-a4658936424f"

_TARGETS = [
    ("payment_methods",   "IGT.POS.Bus.Reporting.Messages.GetPaymentMethodsReportRequest"),
    ("waiter_sales",      "IGT.POS.Bus.Reporting.Messages.GetWaiterSalesReportRequest"),
    ("tips",              "IGT.POS.Bus.Reporting.Messages.GetTipsReportRequest"),
    ("table_turnover",    "IGT.POS.Bus.Reporting.Messages.GetTableTurnoverReportRequest"),
    ("product_mix",       "IGT.POS.Bus.Reporting.Messages.GetProductMixReportRequest"),
    ("discounts",         "IGT.POS.Bus.Reporting.Messages.GetDiscountReportRequest"),
]


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _post(endpoint, body, cookie=""):
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(AGORA_URL + endpoint, data=payload, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            if raw[:2] == b'\x1f\x8b':
                raw = gzip.decompress(raw)
            return r.status, r.getheader("Set-Cookie") or "", raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, "", e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, "", str(e)


# ── Login ─────────────────────────────────────────────────────────────────────

def _login():
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
        "UserName":  AGORA_USER,
        "UserPassword": AGORA_PASSWORD,
        "RememberMe": False,
        "DefaultLanguageCode": "",
        "RestorePreviousSession": False,
        "NotificationToken": "",
    }
    status, set_cookie, text = _post("/auth/", {"CLRType": msg["CLRType"], "Message": msg})
    if status != 200:
        raise RuntimeError(f"Login failed: HTTP {status} — {text[:300]}")
    match = re.search(r"auth-token=([^;]+)", set_cookie)
    if not match:
        raise RuntimeError(f"Login OK but no auth-token in Set-Cookie: {set_cookie}")
    session = json.loads(text)["Message"]["Session"]
    return match.group(1), session


# ── Request body ──────────────────────────────────────────────────────────────

def _build_body(clr_type, session, date_str):
    return {
        "CLRType": clr_type,
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
            "UserId":  session["UserId"],
            "UserName": session["UserName"],
        },
        "PosGroupsIds":        [1],
        "TimeFrameGroupId":    1,
        "IncludeDeliveryNotes": False,
        "From": f"{date_str}T00:00:00.000",
        "To":   f"{date_str}T23:59:59.000",
    }


# ── Public API ────────────────────────────────────────────────────────────────

def run_deep_probe(date_str):
    """
    Login to Agora and fetch the full raw JSON response for each of the
    6 target endpoints for date_str.

    Returns a dict:
    {
        "agora_url":    str,
        "date":         str,
        "logged_in_as": str,
        "endpoints": {
            "payment_methods": {"http": 200, "parsed": {...}},
            ...
        }
    }
    """
    if not AGORA_URL:
        raise RuntimeError("AGORA_URL env var is not set")
    if not AGORA_USER or not AGORA_PASSWORD:
        raise RuntimeError("AGORA_USER and AGORA_PASSWORD env vars must be set")

    auth_token, session = _login()

    endpoints = {}
    for key, clr_type in _TARGETS:
        body = _build_body(clr_type, session, date_str)
        status, _, text = _post(
            "/bus/",
            {"CLRType": clr_type, "Message": body},
            cookie=f"auth-token={auth_token}",
        )
        if status == 200:
            try:
                parsed = json.loads(text)
            except Exception as e:
                parsed = {"_parse_error": str(e), "_raw": text[:2000]}
        else:
            parsed = {"_raw": text[:2000]}

        endpoints[key] = {
            "clr_type": clr_type,
            "http":     status,
            "parsed":   parsed,
        }

    return {
        "agora_url":    AGORA_URL,
        "date":         date_str,
        "logged_in_as": session.get("UserName"),
        "endpoints":    endpoints,
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else "2026-04-18"
    try:
        out = run_deep_probe(date_arg)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print(f"Target : {out['agora_url']}")
    print(f"Date   : {out['date']}")
    print(f"User   : {out['logged_in_as']}")
    print("=" * 72)
    for key, ep in out["endpoints"].items():
        print(f"\n{'='*72}")
        print(f"  {key}  (HTTP {ep['http']})")
        print(f"  CLRType: {ep['clr_type']}")
        print(json.dumps(ep["parsed"], indent=2, ensure_ascii=False))
