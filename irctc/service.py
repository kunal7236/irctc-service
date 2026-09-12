"""
The service layer: fetch upstream, normalize, return clean JSON.

These are the functions the MCP server calls. Each returns a plain dict that is
already meaningful on its own — no post-processing needed by the caller.

Sources are kept separate on purpose. `search_trains` (erail) and
`check_availability` (confirmtkt) both answer "what runs between A and B" but
they are independent, independently testable, and one failing does not take the
other down.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import requests

from . import cache, confirmtkt, erail, ntes, pnr as pnr_mod, refund as refund_mod, stations

ERAIL_BASE = "https://erail.in"
CONFIRMTKT_BASE = "https://cttrainsapi.confirmtkt.com"
PNR_URL = "https://railways.easemytrip.com/Train/PnrchkStatus"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
TIMEOUT = 20

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


class UpstreamError(RuntimeError):
    """An upstream returned something unusable."""


def _get_text(url: str) -> str:
    resp = _session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    # erail serves Latin-1-ish bytes that are really UTF-8; decode explicitly
    # or Hindi station names arrive mangled.
    return resp.content.decode("utf-8", errors="replace")


def _get_json(url: str) -> dict:
    resp = _session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------
# 1. trains between stations (erail)
# --------------------------------------------------------------------------

def search_trains(
    from_code: str,
    to_code: str,
    on_date: date | None = None,
) -> dict[str, Any]:
    """Trains running between two stations, optionally filtered to one date.

    erail ignores the date entirely and returns every train on the route, so
    the day filter is applied here against each train's running-day mask.
    """
    from_code = (from_code or "").strip().upper()
    to_code = (to_code or "").strip().upper()

    def produce():
        url = (f"{ERAIL_BASE}/rail/getTrains.aspx?Station_From={from_code}"
               f"&Station_To={to_code}&DataSource=0&Language=0&Cache=true")
        return erail.parse_trains(_get_text(url))

    trains = cache.cached("trains_between", f"{from_code}-{to_code}", produce)

    filtered = trains
    if on_date:
        filtered = erail.filter_by_day(trains, on_date.weekday())

    # erail expands to nearby stations; make that visible instead of implicit.
    pairs = sorted({f"{t['journey']['from_code']}-{t['journey']['to_code']}" for t in filtered})

    return {
        "error": None,
        "query": {
            "from_code": from_code,
            "to_code": to_code,
            "date": on_date.isoformat() if on_date else None,
            "day": on_date.strftime("%A") if on_date else None,
        },
        "count": len(filtered),
        "count_before_day_filter": len(trains),
        "station_pairs_returned": pairs,
        "nearby_expanded": len(pairs) > 1,
        "trains": filtered,
    }


# --------------------------------------------------------------------------
# 2. train details (erail)
# --------------------------------------------------------------------------

def train_details(train_number: str) -> dict[str, Any]:
    train_number = (train_number or "").strip()

    def produce():
        url = (f"{ERAIL_BASE}/rail/getTrains.aspx?TrainNo={train_number}"
               f"&DataSource=0&Language=0&Cache=true")
        return erail.parse_trains(_get_text(url))

    trains = cache.cached("train_details", train_number, produce)
    if not trains:
        return {"error": f"No train found for number {train_number}.", "train": None}
    return {"error": None, "train": trains[0]}


# --------------------------------------------------------------------------
# 3. train route (erail) — needs the internal trainId from #2
# --------------------------------------------------------------------------

def train_route(train_number: str) -> dict[str, Any]:
    """Full stopping pattern. Two upstream calls: details for the id, then route."""
    details = train_details(train_number)
    if details["error"]:
        return {"error": details["error"], "stops": []}

    train = details["train"]
    train_id = train.get("train_id")
    if not train_id:
        return {"error": f"Could not resolve erail trainId for {train_number}.", "stops": []}

    def produce():
        url = (f"{ERAIL_BASE}/data.aspx?Action=TRAINROUTE&Password=2012"
               f"&Data1={train_id}&Data2=0&Cache=true")
        return erail.parse_route(_get_text(url))

    route = cache.cached("train_route", str(train_id), produce)
    if not route.get("stops"):
        return {"error": "Route response contained no stops.", "stops": []}

    return {
        "error": None,
        "train_number": train["train_number"],
        "train_name": train["train_name"],
        "train_id": train_id,
        "total_distance_km": train.get("distance_km"),
        "stop_count": len(route["stops"]),
        "stops": route["stops"],
        "segment_seats": route["segments"],
    }


# --------------------------------------------------------------------------
# 4. coach composition (erail, from the details record)
# --------------------------------------------------------------------------

def coach_position(train_number: str) -> dict[str, Any]:
    details = train_details(train_number)
    if details["error"]:
        return {"error": details["error"], "coaches": []}

    train = details["train"]
    coaches = train.get("coaches") or []
    if not coaches:
        return {"error": f"No coach composition published for {train_number}.", "coaches": []}

    return {
        "error": None,
        "train_number": train["train_number"],
        "train_name": train["train_name"],
        "coach_count": len(coaches),
        "coaches": coaches,
        "layout": " - ".join(c["coach"] for c in coaches),
        "is_lhb_rake": train.get("is_lhb_rake"),
        "has_pantry": train.get("has_pantry"),
        "note": ("Sequence is normally engine-to-rear and is subject to "
                 "last-minute operational changes."),
    }


# --------------------------------------------------------------------------
# 5. seat availability and fares (confirmtkt) — never cached
# --------------------------------------------------------------------------

def check_availability(
    from_code: str,
    to_code: str,
    on_date: date,
    quota: str | None = None,
) -> dict[str, Any]:
    from_code = (from_code or "").strip().upper()
    to_code = (to_code or "").strip().upper()
    doj = on_date.strftime("%d-%m-%Y")   # confirmtkt wants DD-MM-YYYY

    url = (f"{CONFIRMTKT_BASE}/api/v1/trains/search"
           f"?sourceStationCode={from_code}&destinationStationCode={to_code}"
           f"&dateOfJourney={doj}&addAvailabilityCache=true"
           f"&enableNearby=true&enableTG=true")
    if quota:
        url += f"&quota={quota.upper()}"

    result = confirmtkt.parse_search(_get_json(url))
    result["query"] = {
        "from_code": from_code,
        "to_code": to_code,
        "date": on_date.isoformat(),
        "quota": (quota or "GN").upper(),
    }
    return result


# --------------------------------------------------------------------------
# 6. PNR status and refund — never cached
# --------------------------------------------------------------------------

def pnr_status(pnr: str) -> dict[str, Any]:
    pnr = (pnr or "").strip()
    if not pnr_mod.is_valid_pnr(pnr):
        return {"error": "A PNR must be exactly 10 digits.", "pnr": pnr, "passengers": []}

    resp = _session.post(
        PNR_URL,
        json={"pnrNumber": pnr_mod.encrypt_pnr(pnr)},
        headers={"Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    # Upstream answers 200 even on failure; the error lives in the body.
    return pnr_mod.parse_status(resp.json())


def refund_estimate(pnr: str, now: datetime | None = None) -> dict[str, Any]:
    """Estimate a cancellation refund. No upstream API — pure calculation."""
    status = pnr_status(pnr)
    if status.get("error"):
        return {"error": status["error"], "passengers": []}

    train_number = status.get("train_number")
    boarding = (status.get("journey") or {}).get("boarding_code")
    if not train_number or not boarding:
        return {"error": "PNR is missing the train number or boarding point.", "passengers": []}

    route = train_route(train_number)
    if route.get("error"):
        return {"error": f"Could not load schedule for {train_number}: {route['error']}",
                "passengers": []}

    departure = erail.departure_from_station(route, boarding)
    if not departure:
        return {"error": f"Boarding station {boarding} not found in the route of {train_number}.",
                "passengers": []}

    result = refund_mod.estimate(status, departure, now=now)
    result["train_number"] = train_number
    result["boarding_station"] = boarding
    return result


# --------------------------------------------------------------------------
# 7 & 8. local resolvers — no upstream at all
# --------------------------------------------------------------------------

def find_station(query: str, limit: int = 5) -> dict[str, Any]:
    matches = stations.resolve_station(query, limit=limit)
    # Two candidates tied at the top is the MOST ambiguous case, not the least
    # — "Delhi" matches both DLI ("Delhi") and DEC ("Delhi Cantt") exactly once
    # junction/cantt suffixes are folded. A top score of 1.0 is not on its own
    # evidence of a single answer.
    ambiguous = len(matches) > 1 and (
        matches[0]["score"] < 1.0 or matches[1]["score"] == matches[0]["score"]
    )
    return {
        "error": None if matches else f"No station matched '{query}'.",
        "query": query,
        "count": len(matches),
        "ambiguous": ambiguous,
        "matches": matches,
    }


def find_train(query: str, limit: int = 5) -> dict[str, Any]:
    matches = stations.resolve_train(query, limit=limit)
    return {
        "error": None if matches else f"No train matched '{query}'.",
        "query": query,
        "count": len(matches),
        "matches": matches,
    }


# --------------------------------------------------------------------------
# 9-12. NTES — live running status and station board. Never cached.
# --------------------------------------------------------------------------

def _ntes(payload: str) -> dict:
    """POST an encrypted payload to NTES and decrypt the reply."""
    resp = _session.post(
        ntes.URL,
        json={"jsonIn": ntes.encrypt(payload)},
        headers={"Content-Type": "application/json", "User-Agent": ntes.USER_AGENT},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    if not resp.text.strip():
        raise UpstreamError("NTES returned an empty response.")
    body = resp.json()
    if "jsonIn" not in body:
        return body
    return ntes.decrypt(body["jsonIn"])


def train_instances(train_number: str) -> dict[str, Any]:
    """Which dated runs of a train exist, and which are moving right now."""
    return ntes.parse_instances(_ntes(ntes.p_instances((train_number or "").strip())))


def live_status(train_number: str, start_date: date | None = None) -> dict[str, Any]:
    """Live running status.

    NTES keys a run by the date it STARTED, not today. A train running now may
    have left yesterday or the day before, so unless the caller pins a date we
    ask which instances are live and use that. Guessing "today" is the single
    most common way to get an empty timeline back.
    """
    train_number = (train_number or "").strip()

    if start_date is not None:
        chosen = start_date.strftime("%d-%b-%Y")
        instances = None
    else:
        instances = train_instances(train_number)
        chosen = instances.get("active_start_date")
        if not chosen:
            runs = instances.get("runs") or []
            return {
                "error": f"Train {train_number} is not currently running.",
                "train_number": train_number,
                "upcoming_runs": [r["start_date"] for r in runs if not r["is_running"]][:5],
                "hint": "Pass an explicit start_date to inspect a specific run.",
                "stops": [],
            }

    result = ntes.parse_live_status(_ntes(ntes.p_live_status(train_number, chosen)))
    result["queried_start_date"] = chosen
    if instances:
        result["other_runs"] = [r["start_date"] for r in instances["runs"] if not r["is_running"]][:5]
    return result


def station_board(station_code: str, hours: int = 2) -> dict[str, Any]:
    """Trains due at a station within the next N hours (NTES allows 2, 4 or 8)."""
    station_code = (station_code or "").strip().upper()
    hours = hours if hours in (2, 4, 8) else 2
    return ntes.parse_station_board(_ntes(ntes.p_station_board(station_code, hours)))


def train_exceptions(train_number: str) -> dict[str, Any]:
    """Cancellations, diversions and reschedules for a train."""
    return ntes.parse_exceptions(_ntes(ntes.p_exceptions((train_number or "").strip())))
