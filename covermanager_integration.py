"""
CoverManager reservations integration module.

Provides get_daily_reservations(date) for fetching live reservation data.

Required env vars:
    COVERMANAGER_API_KEY     e.g. jByeKYdu3p6DfHYemUBT
    COVERMANAGER_RESTAURANT  e.g. Restaurante-Norah

Endpoint format:
    GET /api/restaurant/get_reservs/{api_key}/{restaurant}/{from}/{to}/
"""

import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional


# ── Config ────────────────────────────────────────────────────────────────────
COVERMANAGER_BASE       = "https://www.covermanager.com/api"
COVERMANAGER_API_KEY    = os.getenv("COVERMANAGER_API_KEY",    "jByeKYdu3p6DfHYemUBT").strip()
COVERMANAGER_RESTAURANT = os.getenv("COVERMANAGER_RESTAURANT", "Restaurante-Norah").strip()

# Reservation status codes returned by CoverManager
STATUS_CONFIRMED      =  1   # booked / confirmed
STATUS_SEATED         =  2   # arrived / seated
STATUS_PRECONFIRMED   =  3   # pre-confirmed via third-party platform (terceros, moduloweb, app-movil)
STATUS_ARRIVED        =  5   # arrived/active — used by this CoverManager installation
STATUS_WALKIN         =  9   # walk-in entered directly by staff into CoverManager
STATUS_CANCELLED      = -2   # cancelled by client
STATUS_NOSHOW         = -3   # no-show (guest did not appear)
STATUS_HARD_CANCELLED = -5   # cancelled by guest or restaurant

# Shift labels (Spanish)
_LUNCH_SHIFTS  = {"comida", "almuerzo", "mediodía", "mediodia"}
_DINNER_SHIFTS = {"cena", "noche", "tarde"}

# Service cutoff: walk-ins entered before this hour belong to the previous day
_OVERNIGHT_CUTOFF = "06:00:00"


def _remap_overnight_walkins(records: list) -> list:
    """
    Walk-in entries created after midnight but before 06:00 belong to the
    previous day's dinner service. Two patterns are handled:

    Case 1 — status=9, date_add == date, time_add < 06:00:
        CoverManager dated the record on the calendar day it was entered (e.g.
        May 16), but the visit belongs to May 15 dinner. Remap date to
        date - 1 day and force meal_shift=Cena.

    Case 2 — status=5, provenance="walk in", date_add == date, time_add < 06:00:
        Staff entered the guest as arrived (status 5) after midnight on the same
        calendar day as the visit. CoverManager dated the record on the day it was
        entered (e.g. May 16) even though the service was the previous night
        (May 15 dinner). Remap date to date - 1 and force meal_shift=Cena.
    """
    out = []
    for r in records:
        status   = int(r.get("status") or 0)
        time_add = r.get("time_add") or "99:99:99"
        date_add = r.get("date_add", "")
        rec_date = r.get("date", "")
        prov     = (r.get("provenance") or "").strip().lower()

        if not rec_date:
            out.append(r)
            continue

        # Case 1: status=9 walk-in entered after midnight on the same calendar
        # day as the visit date — remap date to previous day, force Cena.
        if (status == STATUS_WALKIN
                and date_add == rec_date
                and time_add < _OVERNIGHT_CUTOFF):
            r = dict(r)
            try:
                r["date"] = (date.fromisoformat(rec_date) - timedelta(days=1)).isoformat()
            except ValueError:
                pass
            r["meal_shift"] = "Cena"

        # Case 2: status=5 provenance="walk in" entered after midnight on the
        # same calendar day — same pattern as Case 1, remap date and force Cena.
        elif (status == STATUS_ARRIVED
                and prov == "walk in"
                and date_add == rec_date
                and time_add < _OVERNIGHT_CUTOFF):
            r = dict(r)
            try:
                r["date"] = (date.fromisoformat(rec_date) - timedelta(days=1)).isoformat()
            except ValueError:
                pass
            r["meal_shift"] = "Cena"

        out.append(r)
    return out


# =============================================================================
# Return type
# =============================================================================

@dataclass
class DailyReservations:
    date: str                           # "YYYY-MM-DD"
    total_covers: int                   # pax across all active reservations
    lunch_covers: int                   # pax at lunch (Comida)
    dinner_covers: int                  # pax at dinner (Cena)
    confirmed_count: int                # reservations with status 1 or 2
    lunch_noshows: int                  # no-show pax at lunch (status -3, updated same calendar day)
    dinner_noshows: int                 # no-show pax at dinner (status -3, updated same calendar day)
    cancelled_count: int                # reservations with status -5
    total_reservations: int             # all records returned (any status)
    lunch_reservations: int             # reservation count at lunch
    dinner_reservations: int            # reservation count at dinner
    reservations: list = field(default_factory=list)  # raw records


# =============================================================================
# HTTP helper
# =============================================================================

def _get(url: str) -> tuple:
    """GET url. Returns (status, response_text)."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"CoverManager request failed: {e}") from e


# =============================================================================
# Fetch raw reservations
# =============================================================================

def _fetch_reservations(date_str: str) -> list:
    """Fetch raw reservation records from CoverManager for a single date.

    Also fetches the next calendar day to capture status=9 walk-in entries
    created after midnight (before 06:00) that belong to this business day.
    """
    if not COVERMANAGER_API_KEY:
        raise RuntimeError("COVERMANAGER_API_KEY env var must be set")
    if not COVERMANAGER_RESTAURANT:
        raise RuntimeError("COVERMANAGER_RESTAURANT env var must be set")

    next_day = (date.fromisoformat(date_str) + timedelta(days=1)).isoformat()
    url = (
        f"{COVERMANAGER_BASE}/restaurant/get_reservs"
        f"/{COVERMANAGER_API_KEY}/{COVERMANAGER_RESTAURANT}"
        f"/{date_str}/{next_day}/"
    )

    status, text = _get(url)

    if status != 200:
        raise RuntimeError(f"CoverManager returned HTTP {status}: {text[:300]}")

    data = json.loads(text)
    if data.get("resp") != 1:
        error = data.get("error", "unknown error")
        raise RuntimeError(f"CoverManager API error: {error}")

    records = _remap_overnight_walkins(data.get("reservs", []))
    return [r for r in records if r.get("date", "") == date_str]


# =============================================================================
# Aggregate raw records into DailyReservations
# =============================================================================

def _aggregate(date_str: str, records: list) -> DailyReservations:
    total_covers       = 0
    lunch_covers       = 0
    dinner_covers      = 0
    confirmed_count    = 0
    lunch_noshows      = 0
    dinner_noshows     = 0
    cancelled_count    = 0
    lunch_reservations = 0
    dinner_reservations = 0

    for r in records:
        status = int(r.get("status") or 0)
        pax    = int(r.get("for", 0) or 0)
        shift  = (r.get("meal_shift") or "").strip().lower()

        # Count covers for confirmed, pre-confirmed (3), seated, arrived (5), or walk-in (9)
        is_active = status in (STATUS_CONFIRMED, STATUS_PRECONFIRMED, STATUS_SEATED, STATUS_ARRIVED, STATUS_WALKIN)

        if is_active:
            total_covers += pax
            if any(w in shift for w in _LUNCH_SHIFTS):
                lunch_covers += pax
                lunch_reservations += 1
            elif any(w in shift for w in _DINNER_SHIFTS):
                dinner_covers += pax
                dinner_reservations += 1

        if is_active:
            confirmed_count += 1
        elif status == STATUS_NOSHOW:
            # Count as a true no-show when last_update_status is on the same
            # calendar day AND at or after 12:00. The 12:00 floor excludes
            # suspiciously early morning updates (e.g. 08:45 for a 15:15 table)
            # while capturing afternoon/evening same-day cancellations.
            last_upd_raw  = (r.get("last_update_status") or "")
            last_upd_date = last_upd_raw[:10]
            last_upd_time = last_upd_raw[11:16]   # HH:MM
            if last_upd_date == date_str and last_upd_time >= "12:00":
                if any(w in shift for w in _LUNCH_SHIFTS):
                    lunch_noshows += pax
                elif any(w in shift for w in _DINNER_SHIFTS):
                    dinner_noshows += pax
        elif status == STATUS_CANCELLED:
            cancelled_count += 1

    return DailyReservations(
        date=date_str,
        total_covers=total_covers,
        lunch_covers=lunch_covers,
        dinner_covers=dinner_covers,
        confirmed_count=confirmed_count,
        lunch_noshows=lunch_noshows,
        dinner_noshows=dinner_noshows,
        cancelled_count=cancelled_count,
        total_reservations=len(records),
        lunch_reservations=lunch_reservations,
        dinner_reservations=dinner_reservations,
        reservations=records,
    )


# =============================================================================
# Public API
# =============================================================================

def get_reservations_range(from_date, to_date) -> list:
    """
    Fetch reservations for a date range and return a list of per-day aggregates.

    Args:
        from_date: date object or "YYYY-MM-DD" string (start, inclusive)
        to_date:   date object or "YYYY-MM-DD" string (end, inclusive)

    Returns:
        List of dicts — one entry per day that has reservation data, sorted by date.
        Each dict is suitable for passing to Claude as JSON.

    Raises:
        RuntimeError if CoverManager is unreachable or credentials are wrong.
    """
    from_str = from_date.isoformat() if isinstance(from_date, date) else str(from_date)
    to_str   = to_date.isoformat()   if isinstance(to_date, date)   else str(to_date)

    if not COVERMANAGER_API_KEY:
        raise RuntimeError("COVERMANAGER_API_KEY env var must be set")

    # Fetch one extra day to capture overnight walk-ins (status 9, time_add < 06:00)
    fetch_to = (date.fromisoformat(to_str) + timedelta(days=1)).isoformat()
    url = (
        f"{COVERMANAGER_BASE}/restaurant/get_reservs"
        f"/{COVERMANAGER_API_KEY}/{COVERMANAGER_RESTAURANT}"
        f"/{from_str}/{fetch_to}/"
    )

    status, text = _get(url)
    if status != 200:
        raise RuntimeError(f"CoverManager returned HTTP {status}: {text[:300]}")

    data = json.loads(text)
    if data.get("resp") != 1:
        error = data.get("error", "unknown error")
        raise RuntimeError(f"CoverManager API error: {error}")

    all_records = _remap_overnight_walkins(data.get("reservs", []))

    # Group records by date, filtering to the originally requested range
    by_day: dict = {}
    for r in all_records:
        d = r.get("date", "")
        if d and from_str <= d <= to_str:
            by_day.setdefault(d, []).append(r)

    results = []
    for day_str in sorted(by_day.keys()):
        agg = _aggregate(day_str, by_day[day_str])

        # Build large-group list (pax >= 6) without PII
        large_groups = []
        for r in by_day[day_str]:
            pax = int(r.get("for", 0) or 0)
            st  = int(r.get("status") or 0)
            if pax >= 6 and st in (STATUS_CONFIRMED, STATUS_SEATED):
                large_groups.append({
                    "time":  r.get("time", ""),
                    "pax":   pax,
                    "shift": r.get("meal_shift", ""),
                    "zone":  r.get("name_zone", ""),
                    "table": r.get("table_names", ""),
                })

        results.append({
            "date":               day_str,
            "total_covers":       agg.total_covers,
            "lunch_covers":       agg.lunch_covers,
            "dinner_covers":      agg.dinner_covers,
            "lunch_reservations": agg.lunch_reservations,
            "dinner_reservations":agg.dinner_reservations,
            "confirmed":          agg.confirmed_count,
            "lunch_noshows":      agg.lunch_noshows,
            "dinner_noshows":     agg.dinner_noshows,
            "cancelled":          agg.cancelled_count,
            "total_reservations": agg.total_reservations,
            "large_groups":       large_groups,
        })

    return results


def get_raw_records(from_date, to_date) -> list:
    """
    Fetch all raw reservation records for a date range, handling pagination.

    Returns the full list of individual reservation dicts as returned by
    CoverManager (one dict per booking). Suitable for client-level analytics.

    Raises:
        RuntimeError if CoverManager is unreachable or credentials are wrong.
    """
    if not COVERMANAGER_API_KEY:
        raise RuntimeError("COVERMANAGER_API_KEY env var must be set")

    from_str = from_date.isoformat() if isinstance(from_date, date) else str(from_date)
    to_str   = to_date.isoformat()   if isinstance(to_date, date)   else str(to_date)

    # Fetch one extra day to capture overnight walk-ins (status 9, time_add < 06:00)
    fetch_to = (date.fromisoformat(to_str) + timedelta(days=1)).isoformat()

    all_records = []
    page = 0
    while True:
        url = (
            f"{COVERMANAGER_BASE}/restaurant/get_reservs"
            f"/{COVERMANAGER_API_KEY}/{COVERMANAGER_RESTAURANT}"
            f"/{from_str}/{fetch_to}/{page}"
        )
        status, text = _get(url)
        if status != 200:
            raise RuntimeError(f"CoverManager returned HTTP {status}: {text[:200]}")
        data = json.loads(text)
        if data.get("resp") != 1:
            raise RuntimeError(f"CoverManager API error: {data.get('error', 'unknown')}")
        batch = data.get("reservs", [])
        all_records.extend(batch)
        if len(batch) < 1000:
            break
        page += 1

    all_records = _remap_overnight_walkins(all_records)
    return [r for r in all_records if from_str <= (r.get("date") or "") <= to_str]


def get_daily_reservations(query_date) -> Optional[DailyReservations]:
    """
    Fetch and aggregate reservation data from CoverManager for a single date.

    Args:
        query_date: a date object or "YYYY-MM-DD" string

    Returns:
        DailyReservations dataclass, or None if no reservations exist.

    Raises:
        RuntimeError if CoverManager is unreachable or credentials are wrong.
    """
    if isinstance(query_date, date):
        date_str = query_date.isoformat()
    else:
        date_str = str(query_date)

    records = _fetch_reservations(date_str)

    if not records:
        return None

    return _aggregate(date_str, records)


# =============================================================================
# Quick test — run directly to verify connectivity
# =============================================================================

if __name__ == "__main__":
    import sys

    test_date = sys.argv[1] if len(sys.argv) > 1 else str(date.today())
    print(f"Testing CoverManager integration for {test_date} ...\n")

    try:
        result = get_daily_reservations(test_date)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if result is None:
        print("No reservations for that date.")
        sys.exit(0)

    print(f"Date:                 {result.date}")
    print(f"Total covers:         {result.total_covers}  (lunch {result.lunch_covers}, dinner {result.dinner_covers})")
    print(f"Total reservations:   {result.total_reservations}")
    print(f"  Confirmed/seated:   {result.confirmed_count}")
    print(f"  No-shows (same day): lunch {result.lunch_noshows}, dinner {result.dinner_noshows}")
    print(f"  Cancelled:          {result.cancelled_count}")
    print(f"Lunch reservations:   {result.lunch_reservations}  ({result.lunch_covers} pax)")
    print(f"Dinner reservations:  {result.dinner_reservations}  ({result.dinner_covers} pax)")
