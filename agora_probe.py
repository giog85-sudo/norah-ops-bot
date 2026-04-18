"""
Agora IGT bus probe — tries candidate CLRType message names and reports
which ones return data, so we know what endpoints are available.

Can be run standalone:
    python3 agora_probe.py [YYYY-MM-DD]

Or imported and called as:
    from agora_probe import run_probe
    results = run_probe("2026-04-16")
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


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _post(endpoint, body, cookie=""):
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


# ── Probe ─────────────────────────────────────────────────────────────────────

_BASE_SENDER = {
    "ApplicationName": "AgoraWebAdmin",
    "ApplicationVersion": "8.4.2",
    "LanguageCode": "es",
    "MachineId": AGORA_MACHINE_ID,
    "MachineName": "Web Device",
    "MachineType": 4,
}

_CANDIDATES = [
    # label, CLRType
    ("Sales analytics (known working)",
     "IGT.POS.Bus.Reporting.Messages.GetSalesAnalyticsReportRequest"),
    ("Payment methods breakdown",
     "IGT.POS.Bus.Reporting.Messages.GetPaymentMethodsReportRequest"),
    ("Cash register / Z-report",
     "IGT.POS.Bus.Reporting.Messages.GetCashRegisterReportRequest"),
    ("Closure report",
     "IGT.POS.Bus.Reporting.Messages.GetClosureReportRequest"),
    ("Daily totals",
     "IGT.POS.Bus.Reporting.Messages.GetDailyTotalsReportRequest"),
    ("Waiter sales report",
     "IGT.POS.Bus.Reporting.Messages.GetWaiterSalesReportRequest"),
    ("Staff report",
     "IGT.POS.Bus.Reporting.Messages.GetStaffReportRequest"),
    ("Employee report",
     "IGT.POS.Bus.Reporting.Messages.GetEmployeeReportRequest"),
    ("Product mix report",
     "IGT.POS.Bus.Reporting.Messages.GetProductMixReportRequest"),
    ("Product sales report",
     "IGT.POS.Bus.Reporting.Messages.GetProductSalesReportRequest"),
    ("Category sales report",
     "IGT.POS.Bus.Reporting.Messages.GetCategorySalesReportRequest"),
    ("Family sales report",
     "IGT.POS.Bus.Reporting.Messages.GetFamilySalesReportRequest"),
    ("Table report",
     "IGT.POS.Bus.Reporting.Messages.GetTableReportRequest"),
    ("Table turnover report",
     "IGT.POS.Bus.Reporting.Messages.GetTableTurnoverReportRequest"),
    ("Covers report",
     "IGT.POS.Bus.Reporting.Messages.GetCoversReportRequest"),
    ("Tips report",
     "IGT.POS.Bus.Reporting.Messages.GetTipsReportRequest"),
    ("Gratuity report",
     "IGT.POS.Bus.Reporting.Messages.GetGratuityReportRequest"),
    ("Discount report",
     "IGT.POS.Bus.Reporting.Messages.GetDiscountReportRequest"),
    ("Void report",
     "IGT.POS.Bus.Reporting.Messages.GetVoidReportRequest"),
    ("Invitations report",
     "IGT.POS.Bus.Reporting.Messages.GetInvitationsReportRequest"),
    ("Ticket detail report",
     "IGT.POS.Bus.Reporting.Messages.GetTicketDetailReportRequest"),
    ("Invoice report",
     "IGT.POS.Bus.Reporting.Messages.GetInvoiceReportRequest"),
    ("Shift report",
     "IGT.POS.Bus.Reporting.Messages.GetShiftReportRequest"),
    ("Shift summary",
     "IGT.POS.Bus.Reporting.Messages.GetShiftSummaryReportRequest"),
    ("Reservation report",
     "IGT.POS.Bus.Reporting.Messages.GetReservationReportRequest"),
]


def _build_body(clr_type, session, date_str):
    return {
        "CLRType": clr_type,
        "IsBlocking": True,
        "OutOfBandMessages": [],
        "Sender": {
            **_BASE_SENDER,
            "PosId":   0,
            "PosName": "",
            "UserId":  session["UserId"],
            "UserName": session["UserName"],
        },
        "PosGroupsIds":       [1],
        "TimeFrameGroupId":   1,
        "IncludeDeliveryNotes": False,
        "From": f"{date_str}T00:00:00.000",
        "To":   f"{date_str}T23:59:59.000",
    }


def _analyse(text):
    """Return a summary dict describing what came back in the Message."""
    try:
        d = json.loads(text)
    except Exception:
        return {"status": "non_json", "preview": text[:200]}

    msg = d.get("Message", {})

    # Collect any non-empty lists / notable scalars
    data_fields = {}
    for k, v in msg.items():
        if isinstance(v, list) and v:
            data_fields[k] = len(v)
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, list) and v2:
                    data_fields[f"{k}.{k2}"] = len(v2)
                elif isinstance(v2, (int, float)) and v2 not in (0, 0.0, None):
                    data_fields[f"{k}.{k2}"] = v2

    err = d.get("Error") or msg.get("ErrorMessage") or msg.get("Error")
    if err:
        return {"status": "error", "error": str(err)[:200]}
    if data_fields:
        return {"status": "data", "fields": data_fields}
    if not msg:
        return {"status": "empty"}
    return {"status": "ok_no_data", "msg_keys": list(msg.keys())[:10]}


def run_probe(date_str):
    """
    Login to Agora and probe all candidate CLRTypes for date_str.
    Returns a list of result dicts, one per candidate.
    Raises RuntimeError if login fails or AGORA_URL is not set.
    """
    if not AGORA_URL:
        raise RuntimeError("AGORA_URL env var is not set")
    if not AGORA_USER or not AGORA_PASSWORD:
        raise RuntimeError("AGORA_USER and AGORA_PASSWORD env vars must be set")

    auth_token, session = _login()

    results = []
    for label, clr_type in _CANDIDATES:
        body = _build_body(clr_type, session, date_str)
        status, _, text = _post(
            "/bus/",
            {"CLRType": clr_type, "Message": body},
            cookie=f"auth-token={auth_token}",
        )
        analysis = _analyse(text) if status == 200 else {"status": f"http_{status}"}
        results.append({
            "label":    label,
            "clr_type": clr_type,
            "http":     status,
            "result":   analysis,
        })

    return {
        "agora_url":   AGORA_URL,
        "date":        date_str,
        "logged_in_as": session.get("UserName"),
        "candidates":  results,
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else "2026-04-16"
    try:
        out = run_probe(date_arg)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print(f"Target : {out['agora_url']}")
    print(f"Date   : {out['date']}")
    print(f"User   : {out['logged_in_as']}")
    print("=" * 72)
    for r in out["candidates"]:
        res = r["result"]
        if res["status"] == "data":
            icon = "✓"
            detail = "DATA: " + ", ".join(f"{k}[{v}]" if isinstance(v, int) else f"{k}={v}"
                                          for k, v in res["fields"].items())
        elif res["status"] == "error":
            icon = "✗"
            detail = "error: " + res["error"]
        elif res["status"].startswith("http_"):
            icon = "✗"
            detail = res["status"]
        else:
            icon = "·"
            detail = res["status"]
        print(f"  {icon}  {r['label']}")
        print(f"       → {detail}")
    print()
