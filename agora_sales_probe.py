"""
Agora GetSalesAnalyticsReportRequest deep probe.

Since other report CLRTypes aren't activated, this probes every variation
of the one working endpoint to see the full raw response and discover
whether payment, waiter, tips, or other fields are buried inside it.

Can be run standalone:
    python3 agora_sales_probe.py [YYYY-MM-DD]

Or imported:
    from agora_sales_probe import run_sales_probe
    results = run_sales_probe("2026-04-18")
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

CLR = "IGT.POS.Bus.Reporting.Messages.GetSalesAnalyticsReportRequest"


# ── HTTP / login helpers ──────────────────────────────────────────────────────

def _post(endpoint, body, cookie=""):
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(AGORA_URL + endpoint, data=payload, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            if raw[:2] == b'\x1f\x8b':
                raw = gzip.decompress(raw)
            return r.status, r.getheader("Set-Cookie") or "", raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, "", e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, "", str(e)


def _login():
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
        raise RuntimeError(f"No auth-token in Set-Cookie: {set_cookie}")
    session = json.loads(text)["Message"]["Session"]
    return match.group(1), session


def _sender(session, pos_id=0):
    return {
        "ApplicationName": "AgoraWebAdmin",
        "ApplicationVersion": "8.5.6",
        "LanguageCode": "es",
        "MachineId": AGORA_MACHINE_ID,
        "MachineName": "Web Device",
        "MachineType": 4,
        "PosId": pos_id,
        "PosName": "",
        "UserId":  session["UserId"],
        "UserName": session["UserName"],
    }


# ── Variations ────────────────────────────────────────────────────────────────

def _build_variations(session, date_str):
    """
    Yield (label, post_body) for every variation of GetSalesAnalyticsReportRequest.
    We always return the full raw parsed JSON — no filtering.
    """
    frm = f"{date_str}T00:00:00.000"
    to  = f"{date_str}T23:59:59.000"
    s   = _sender(session)

    def _msg(**kwargs):
        base = {
            "CLRType":             CLR,
            "IsBlocking":          True,
            "OutOfBandMessages":   [],
            "Sender":              s,
            "PosGroupsIds":        [1],
            "TimeFrameGroupId":    1,
            "IncludeDeliveryNotes": False,
            "From": frm,
            "To":   to,
        }
        base.update(kwargs)
        return base

    # ── 1. Exact working config (baseline) ────────────────────────────────────
    yield "baseline_working", {"CLRType": CLR, "Message": _msg()}

    # ── 2. IncludeDeliveryNotes=True ──────────────────────────────────────────
    yield "include_delivery_notes", {"CLRType": CLR, "Message": _msg(IncludeDeliveryNotes=True)}

    # ── 3. TimeFrameGroupId=0 (all timeframes / no split) ────────────────────
    yield "timeframe_0", {"CLRType": CLR, "Message": _msg(TimeFrameGroupId=0)}

    # ── 4. TimeFrameGroupId=2 ─────────────────────────────────────────────────
    yield "timeframe_2", {"CLRType": CLR, "Message": _msg(TimeFrameGroupId=2)}

    # ── 5. PosGroupsIds=[] (no group filter — all groups) ────────────────────
    yield "pos_groups_empty", {"CLRType": CLR, "Message": _msg(PosGroupsIds=[])}

    # ── 6. PosGroupsIds=[0] ───────────────────────────────────────────────────
    yield "pos_groups_0", {"CLRType": CLR, "Message": _msg(PosGroupsIds=[0])}

    # ── 7. PosGroupsIds=[1,2] ─────────────────────────────────────────────────
    yield "pos_groups_1_2", {"CLRType": CLR, "Message": _msg(PosGroupsIds=[1, 2])}

    # ── 8. Extra field: GroupByWaiter=True ────────────────────────────────────
    yield "group_by_waiter", {"CLRType": CLR, "Message": _msg(GroupByWaiter=True)}

    # ── 9. Extra field: GroupByPaymentMethod=True ─────────────────────────────
    yield "group_by_payment", {"CLRType": CLR, "Message": _msg(GroupByPaymentMethod=True)}

    # ── 10. Extra field: IncludeTips=True ─────────────────────────────────────
    yield "include_tips", {"CLRType": CLR, "Message": _msg(IncludeTips=True)}

    # ── 11. Extra field: IncludeDiscounts=True ────────────────────────────────
    yield "include_discounts", {"CLRType": CLR, "Message": _msg(IncludeDiscounts=True)}

    # ── 12. Extra field: IncludeVoids=True ────────────────────────────────────
    yield "include_voids", {"CLRType": CLR, "Message": _msg(IncludeVoids=True)}

    # ── 13. Extra field: ExpandPaymentMethods=True ────────────────────────────
    yield "expand_payment_methods", {"CLRType": CLR, "Message": _msg(ExpandPaymentMethods=True)}

    # ── 14. GroupBy="Waiter" (string enum guess) ──────────────────────────────
    yield "groupby_waiter_str", {"CLRType": CLR, "Message": _msg(GroupBy="Waiter")}

    # ── 15. GroupBy="PaymentMethod" ───────────────────────────────────────────
    yield "groupby_payment_str", {"CLRType": CLR, "Message": _msg(GroupBy="PaymentMethod")}

    # ── 16. ReportType="Full" ─────────────────────────────────────────────────
    yield "report_type_full", {"CLRType": CLR, "Message": _msg(ReportType="Full")}

    # ── 17. ReportType=0 ──────────────────────────────────────────────────────
    yield "report_type_0", {"CLRType": CLR, "Message": _msg(ReportType=0)}

    # ── 18. Full week (Mon–Sun of the date's week) ────────────────────────────
    import datetime
    d = datetime.date.fromisoformat(date_str)
    week_mon = d - datetime.timedelta(days=d.weekday())
    week_sun = week_mon + datetime.timedelta(days=6)
    yield "full_week", {"CLRType": CLR, "Message": _msg(
        From=f"{week_mon.isoformat()}T00:00:00.000",
        To=f"{week_sun.isoformat()}T23:59:59.000",
    )}

    # ── 19. Last 7 days ────────────────────────────────────────────────────────
    seven_ago = d - datetime.timedelta(days=6)
    yield "last_7_days", {"CLRType": CLR, "Message": _msg(
        From=f"{seven_ago.isoformat()}T00:00:00.000",
        To=f"{d.isoformat()}T23:59:59.000",
    )}

    # ── 20. SplitByTimeFrame=True ─────────────────────────────────────────────
    yield "split_by_timeframe", {"CLRType": CLR, "Message": _msg(SplitByTimeFrame=True)}

    # ── 21. All extra guessed fields combined ─────────────────────────────────
    yield "all_extras", {"CLRType": CLR, "Message": _msg(
        IncludeDeliveryNotes=True,
        GroupByWaiter=True,
        GroupByPaymentMethod=True,
        IncludeTips=True,
        IncludeDiscounts=True,
        IncludeVoids=True,
        SplitByTimeFrame=True,
    )}


# ── Analysis helper ───────────────────────────────────────────────────────────

def _summarise_keys(parsed):
    """Walk the parsed JSON and return a flat map of key-paths → preview."""
    result = {}

    def _walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _walk(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, list):
            result[path] = f"list[{len(obj)}]"
            if obj and isinstance(obj[0], dict):
                _walk(obj[0], f"{path}[0]")
        else:
            result[path] = repr(obj)[:120]

    _walk(parsed)
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def run_sales_probe(date_str):
    if not AGORA_URL:
        raise RuntimeError("AGORA_URL env var is not set")
    if not AGORA_USER or not AGORA_PASSWORD:
        raise RuntimeError("AGORA_USER and AGORA_PASSWORD env vars must be set")

    auth_token, session = _login()

    attempts = []
    for label, post_body in _build_variations(session, date_str):
        status, _, text = _post(
            "/bus/",
            post_body,
            cookie=f"auth-token={auth_token}",
        )
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"_raw": text[:3000]}

        # Check for error
        err = (parsed.get("Error")
               or parsed.get("Message", {}).get("ErrorMessage")
               or parsed.get("Message", {}).get("Error"))

        attempts.append({
            "label":    label,
            "http":     status,
            "error":    str(err)[:300] if err else None,
            # Full parsed response always included
            "parsed":   parsed,
            # Flat key map makes it easy to spot new fields without reading the whole blob
            "key_map":  _summarise_keys(parsed),
        })

    return {
        "agora_url":    AGORA_URL,
        "date":         date_str,
        "logged_in_as": session.get("UserName"),
        "attempts":     attempts,
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else "2026-04-18"
    try:
        out = run_sales_probe(date_arg)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print(f"Target : {out['agora_url']}")
    print(f"Date   : {out['date']}")
    print(f"User   : {out['logged_in_as']}")

    for att in out["attempts"]:
        print(f"\n{'='*72}")
        icon = "✗" if att["error"] else "✓"
        print(f"  {icon}  [{att['label']}]  HTTP {att['http']}")
        if att["error"]:
            print(f"     error: {att['error']}")
        else:
            print("     key_map:")
            for k, v in att["key_map"].items():
                print(f"       {k}: {v}")
