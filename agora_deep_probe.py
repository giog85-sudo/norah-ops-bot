"""
Agora IGT bus deep probe — fires multiple body variations for 6 specific
endpoints and returns the full raw response for each, so we can see
which parameter combination the server actually accepts.

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
    ("payment_methods",  "IGT.POS.Bus.Reporting.Messages.GetPaymentMethodsReportRequest"),
    ("waiter_sales",     "IGT.POS.Bus.Reporting.Messages.GetWaiterSalesReportRequest"),
    ("tips",             "IGT.POS.Bus.Reporting.Messages.GetTipsReportRequest"),
    ("table_turnover",   "IGT.POS.Bus.Reporting.Messages.GetTableTurnoverReportRequest"),
    ("product_mix",      "IGT.POS.Bus.Reporting.Messages.GetProductMixReportRequest"),
    ("discounts",        "IGT.POS.Bus.Reporting.Messages.GetDiscountReportRequest"),
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


# ── Base sender ───────────────────────────────────────────────────────────────

def _sender(session):
    return {
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
    }


# ── Body variations ───────────────────────────────────────────────────────────
#
# The working GetSalesAnalyticsReportRequest uses:
#   PosGroupsIds=[1], TimeFrameGroupId=1, IncludeDeliveryNotes=False
#
# We try multiple combinations to find what each endpoint needs.

def _variations(clr_type, session, date_str):
    """
    Yield (label, post_body) tuples — each is a different attempt at calling
    this CLRType.  We stop after the first success in run_deep_probe().
    """
    s = _sender(session)
    frm = f"{date_str}T00:00:00.000"
    to  = f"{date_str}T23:59:59.000"

    # ── V1: exact mirror of the working GetSalesAnalyticsReportRequest ────────
    msg_v1 = {
        "CLRType":             clr_type,
        "IsBlocking":          True,
        "OutOfBandMessages":   [],
        "Sender":              s,
        "PosGroupsIds":        [1],
        "TimeFrameGroupId":    1,
        "IncludeDeliveryNotes": False,
        "From": frm,
        "To":   to,
    }
    yield "v1_exact_mirror", {"CLRType": clr_type, "Message": msg_v1}

    # ── V2: PosGroupsIds=[0] (pos-level instead of group-level) ──────────────
    msg_v2 = {**msg_v1, "PosGroupsIds": [0]}
    yield "v2_pos_group_0", {"CLRType": clr_type, "Message": msg_v2}

    # ── V3: PosGroupsIds=[] (empty — let server use all) ─────────────────────
    msg_v3 = {**msg_v1, "PosGroupsIds": []}
    yield "v3_pos_groups_empty", {"CLRType": clr_type, "Message": msg_v3}

    # ── V4: PosGroupsIds=None ─────────────────────────────────────────────────
    msg_v4 = {**msg_v1, "PosGroupsIds": None}
    yield "v4_pos_groups_null", {"CLRType": clr_type, "Message": msg_v4}

    # ── V5: TimeFrameGroupId=0 ────────────────────────────────────────────────
    msg_v5 = {**msg_v1, "TimeFrameGroupId": 0}
    yield "v5_timeframe_0", {"CLRType": clr_type, "Message": msg_v5}

    # ── V6: TimeFrameGroupId=2 ────────────────────────────────────────────────
    msg_v6 = {**msg_v1, "TimeFrameGroupId": 2}
    yield "v6_timeframe_2", {"CLRType": clr_type, "Message": msg_v6}

    # ── V7: minimal body — only Sender + dates, no group/timeframe fields ─────
    msg_v7 = {
        "CLRType":           clr_type,
        "IsBlocking":        True,
        "OutOfBandMessages": [],
        "Sender":            s,
        "From": frm,
        "To":   to,
    }
    yield "v7_minimal", {"CLRType": clr_type, "Message": msg_v7}

    # ── V8: send the message *unwrapped* — body IS the message (no outer wrapper)
    yield "v8_unwrapped", msg_v1

    # ── V9: IsBlocking=False ──────────────────────────────────────────────────
    msg_v9 = {**msg_v1, "IsBlocking": False}
    yield "v9_nonblocking", {"CLRType": clr_type, "Message": msg_v9}

    # ── V10: PosGroupsIds=[1,2] ───────────────────────────────────────────────
    msg_v10 = {**msg_v1, "PosGroupsIds": [1, 2]}
    yield "v10_pos_groups_1_2", {"CLRType": clr_type, "Message": msg_v10}

    # ── V11: date range as plain YYYY-MM-DD (no time component) ──────────────
    msg_v11 = {**msg_v1, "From": date_str, "To": date_str}
    yield "v11_date_only", {"CLRType": clr_type, "Message": msg_v11}

    # ── V12: PosId=1 in Sender (instead of 0) ─────────────────────────────────
    s2 = {**s, "PosId": 1}
    msg_v12 = {**msg_v1, "Sender": s2}
    yield "v12_posid_1", {"CLRType": clr_type, "Message": msg_v12}


# ── Public API ────────────────────────────────────────────────────────────────

def _is_success(http_status, text):
    """Return True if the response looks like real data (not an error message)."""
    if http_status != 200:
        return False
    try:
        d = json.loads(text)
    except Exception:
        return False
    err = d.get("Error") or d.get("Message", {}).get("ErrorMessage") or d.get("Message", {}).get("Error")
    return not err


def run_deep_probe(date_str):
    """
    Login to Agora and try multiple body variations for each of the 6 target
    endpoints.  Stops at the first successful variation for each endpoint.

    Returns:
    {
        "agora_url":    str,
        "date":         str,
        "logged_in_as": str,
        "endpoints": {
            "payment_methods": {
                "clr_type": str,
                "winner": "v1_exact_mirror" | null,
                "attempts": [
                    {"label": str, "http": int, "success": bool, "parsed": {...}},
                    ...
                ]
            },
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
        attempts = []
        winner = None

        for label, post_body in _variations(clr_type, session, date_str):
            status, _, text = _post(
                "/bus/",
                post_body,
                cookie=f"auth-token={auth_token}",
            )
            success = _is_success(status, text)
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = {"_raw": text[:3000]}

            attempts.append({
                "label":   label,
                "http":    status,
                "success": success,
                "parsed":  parsed,
            })

            if success:
                winner = label
                break   # stop trying variations once we get clean data

        endpoints[key] = {
            "clr_type": clr_type,
            "winner":   winner,
            "attempts": attempts,
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

    for key, ep in out["endpoints"].items():
        print(f"\n{'='*72}")
        print(f"  {key}")
        print(f"  CLRType : {ep['clr_type']}")
        print(f"  Winner  : {ep['winner'] or 'NONE — all variations failed'}")
        for att in ep["attempts"]:
            icon = "✓" if att["success"] else "✗"
            print(f"\n  {icon} [{att['label']}]  HTTP {att['http']}")
            print(json.dumps(att["parsed"], indent=4, ensure_ascii=False)[:1000])
            if att["success"]:
                break
