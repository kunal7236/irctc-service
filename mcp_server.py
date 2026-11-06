"""
IRCTC MCP server.

Exposes the service layer as MCP tools over Streamable HTTP, so ChatGPT, Gemini
and Claude can all connect to the same URL.

Run:
    python3 mcp_server.py
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from starlette.types import ASGIApp

import irctc
from irctc import stations

IST = ZoneInfo("Asia/Kolkata")

mcp = FastMCP(
    name="irctc",
    version="0.1.0",
    instructions=(
        "Indian Railways information: live running status and station "
        "departure boards, train schedules and routes, seat availability with "
        "confirmation predictions, coach layouts, PNR status and cancellation "
        "refund estimates.\n\n"
        "Live data (running status, station boards) comes from NTES, Indian "
        "Railways' own tracking system, and can lag a few minutes — say when "
        "it was last updated rather than implying it is instantaneous.\n\n"
        "Station and train arguments accept either an official code (NDLS, "
        "12951) or a plain name ('New Delhi', 'Mumbai Rajdhani') — the server "
        "resolves names itself. When a name is ambiguous the tool returns the "
        "candidate list instead of guessing; ask the user which one they mean "
        "rather than picking one.\n\n"
        "Dates are YYYY-MM-DD, or the words 'today' or 'tomorrow'. All times "
        "are Indian Standard Time and every response carries `as_of_ist` so "
        "you never have to assume the current date.\n\n"
        "This server is read-only. It cannot book, cancel or modify tickets."
    ),
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(IST)


def _stamp(payload: dict) -> dict:
    payload["as_of_ist"] = _now().strftime("%Y-%m-%d %H:%M")
    return payload


def _parse_date(value: str | None) -> tuple[date | None, str | None]:
    """Accept YYYY-MM-DD, 'today' or 'tomorrow'. Returns (date, error)."""
    if not value:
        return None, None
    v = value.strip().lower()
    today = _now().date()
    if v == "today":
        return today, None
    if v == "tomorrow":
        return today + timedelta(days=1), None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date(), None
    except ValueError:
        return None, f"Could not read '{value}' as a date. Use YYYY-MM-DD, 'today' or 'tomorrow'."


def _resolve_station(value: str, label: str) -> tuple[str | None, dict | None]:
    """Turn a name or code into a single station code.

    Returns (code, error_payload). Ambiguity is an error carrying the
    candidates — the alternative is silently sending someone to Old Delhi when
    they meant New Delhi.
    """
    v = (value or "").strip()
    if not v:
        return None, {"error": f"No {label} station given."}

    if re.fullmatch(r"[A-Za-z]{2,5}", v) and stations.station_name(v):
        return v.upper(), None

    result = irctc.find_station(v, limit=5)
    matches = result["matches"]
    if not matches:
        return None, {"error": f"No station matches '{v}' for {label}.",
                      "hint": "Try the official station code."}
    if result["ambiguous"]:
        return None, {
            "error": f"'{v}' is ambiguous for {label}.",
            "candidates": matches,
            "hint": "Ask the user which station they mean, then call again with its code.",
        }
    return matches[0]["code"], None


def _resolve_train(value: str) -> tuple[str | None, dict | None]:
    v = (value or "").strip()
    if not v:
        return None, {"error": "No train given."}
    if re.fullmatch(r"\d{4,5}", v):
        return v, None

    matches = irctc.find_train(v, limit=5)["matches"]
    if not matches:
        return None, {"error": f"No train matches '{v}'.",
                      "hint": "Try the 5-digit train number."}
    if len(matches) > 1 and matches[0]["score"] < 1.0:
        return None, {
            "error": f"'{v}' matches several trains.",
            "candidates": matches,
            "hint": "Ask the user which train they mean, then call again with its number.",
        }
    return matches[0]["number"], None


def _slim_train(t: dict, verbose: bool) -> dict:
    """Trim a train record for model context.

    A between-stations query can return 150+ trains; handing over every field
    of every one buries the answer and wastes the context window. Seat counts
    per quota, the fare block and `raw` are only useful when specifically
    asked for.
    """
    out = {
        "train_number": t["train_number"],
        "train_name": t["train_name"],
        "type": t["train_type_label"] or t["train_type"],
        "from": f'{t["journey"]["from_name"]} ({t["journey"]["from_code"]})',
        "to": f'{t["journey"]["to_name"]} ({t["journey"]["to_code"]})',
        "departure": t["journey"]["departure"],
        "arrival": t["journey"]["arrival"],
        "duration": t["journey"]["duration"],
        "duration_minutes": t["journey"]["duration_minutes"],
        "distance_km": t["distance_km"],
        "runs_on": t["runs_on"]["days"],
        "classes": t["classes"],
        "has_pantry": t["has_pantry"],
    }
    if t["journey"]["from_platform"]:
        out["departure_platform"] = t["journey"]["from_platform"]
    if t["journey"]["to_platform"]:
        out["arrival_platform"] = t["journey"]["to_platform"]
    if verbose:
        out["class_names"] = t["class_names"]
        out["seats_by_quota"] = t["seats_by_class"]
        out["fares_raw"] = t["fares"]
        out["zone"] = t["zone"]
        out["avg_speed_kmph"] = t["avg_speed_kmph"]
        out["advance_reservation_days"] = t["advance_reservation_days"]
        out["rake_shared_with"] = t["rake_shared_with"]
        out["is_lhb_rake"] = t["is_lhb_rake"]
    return out


def _guard(fn, *args, **kwargs) -> dict:
    """Turn upstream failures into a readable payload instead of a stack trace."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — the agent needs a message, not a traceback
        return {"error": f"Upstream request failed: {type(exc).__name__}: {exc}",
                "hint": "The upstream may be slow or temporarily down. Retrying once is reasonable."}


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

@mcp.tool
def find_station(query: str) -> dict[str, Any]:
    """Look up an Indian Railways station code from a place or station name.

    Use this when the user names a city or station and you need its code, or
    when another tool reports an ambiguous station. Large cities have several
    stations (Delhi has NDLS, DLI, NZM, DEE, DEC) and they are genuinely
    different places — surface the options rather than choosing.

    Args:
        query: A station name, city name, or code. e.g. "Varanasi", "NDLS".
    """
    return _stamp(irctc.find_station(query, limit=8))


@mcp.tool
def find_train(query: str) -> dict[str, Any]:
    """Look up a train number from a train name, or confirm a number is real.

    Use this when the user refers to a train by name ("the Shatabdi to
    Amritsar", "Punjab Mail") and you need its number before calling the
    schedule, route or coach tools. Many trains share a similar name and
    differ only by direction — 12951 and 12952 are the same Rajdhani running
    opposite ways — so check the source and destination on each candidate
    rather than assuming the first is right.

    Args:
        query: A train name or number. e.g. "Mumbai Rajdhani", "12951".
    """
    return _stamp(irctc.find_train(query, limit=8))


@mcp.tool
def search_trains(
    from_station: str,
    to_station: str,
    travel_date: str | None = None,
    limit: int = 20,
    verbose: bool = False,
) -> dict[str, Any]:
    """List trains running between two stations, with timings and classes.

    This is the schedule view — it does NOT include seat availability or live
    fares. For "can I get a ticket" or "how much does it cost", use
    check_seat_availability instead.

    Results may include trains from NEARBY stations: asking for New Delhi can
    return departures from Nizamuddin or Delhi Sarai Rohilla, which are
    different stations several kilometres apart. Each result states the station
    it actually departs from — pass that on to the user rather than repeating
    what they asked for.

    Args:
        from_station: Origin station name or code.
        to_station: Destination station name or code.
        travel_date: YYYY-MM-DD, "today" or "tomorrow". Filters to trains that
            run on that weekday. Omit to see every train on the route.
        limit: Maximum trains to return, ordered by departure time.
        verbose: Include per-quota seat counts, fare block and rake details.
    """
    src, err = _resolve_station(from_station, "origin")
    if err:
        return _stamp(err)
    dst, err = _resolve_station(to_station, "destination")
    if err:
        return _stamp(err)
    when, err = _parse_date(travel_date)
    if err:
        return _stamp({"error": err})

    result = _guard(irctc.search_trains, src, dst, when)
    if result.get("error"):
        return _stamp(result)

    trains = sorted(result["trains"], key=lambda t: t["journey"]["departure"] or "99:99")
    shown = trains[: max(1, limit)]

    return _stamp({
        "error": None,
        "from": src,
        "to": dst,
        "date": when.isoformat() if when else None,
        "day": when.strftime("%A") if when else None,
        "total_matching": len(trains),
        "showing": len(shown),
        "truncated": len(shown) < len(trains),
        "includes_nearby_stations": result["nearby_expanded"],
        "station_pairs_returned": result["station_pairs_returned"],
        "trains": [_slim_train(t, verbose) for t in shown],
    })


@mcp.tool
def check_seat_availability(
    from_station: str,
    to_station: str,
    travel_date: str,
    travel_class: str | None = None,
    quota: str = "GN",
) -> dict[str, Any]:
    """Live seat availability, fares and confirmation chances for a date.

    Use this for "is there space", "how much is a ticket", "will my waitlist
    clear". Returns the current booking status per class (AVAILABLE, RAC, a
    waitlist position, or REGRET), the fare, and a predicted probability of
    a waitlisted ticket confirming.

    Predictions are estimates from historical booking patterns, not guarantees
    — say so when relaying a percentage.

    Args:
        from_station: Origin station name or code.
        to_station: Destination station name or code.
        travel_date: YYYY-MM-DD, "today" or "tomorrow". Required.
        travel_class: Optionally filter to one class, e.g. "3A", "SL", "CC".
        quota: "GN" (general, default), "TQ" (tatkal), "PT" (premium tatkal),
            "LD" (ladies), "SS" (senior citizen).
    """
    src, err = _resolve_station(from_station, "origin")
    if err:
        return _stamp(err)
    dst, err = _resolve_station(to_station, "destination")
    if err:
        return _stamp(err)
    when, err = _parse_date(travel_date)
    if err:
        return _stamp({"error": err})
    if not when:
        return _stamp({"error": "travel_date is required for availability."})

    result = _guard(irctc.check_availability, src, dst, when,
                    quota=None if quota.upper() == "GN" else quota)
    if result.get("error"):
        return _stamp(result)

    wanted = (travel_class or "").strip().upper()
    trains = []
    for t in result["trains"]:
        buckets = t["availability"]["general"] | t["availability"]["tatkal"] | t["availability"]["other_quota"]
        if wanted:
            buckets = {k: v for k, v in buckets.items() if k == wanted}
            if not buckets:
                continue
        trains.append({
            "train_number": t["train_number"],
            "train_name": t["train_name"],
            "from": f'{t["journey"]["from_name"]} ({t["journey"]["from_code"]})',
            "to": f'{t["journey"]["to_name"]} ({t["journey"]["to_code"]})',
            "departure": t["journey"]["departure"],
            "arrival": t["journey"]["arrival"],
            "duration": t["journey"]["duration"],
            "has_departed": t["journey"]["has_departed"],
            "availability": {
                cls: {
                    "status": b["display"] or b["status"],
                    "fare_inr": b["fare"],
                    "confirm_chance": b["confirm_prediction"],
                    "confirm_probability_pct": b["confirm_probability"],
                    "verdict": b["confirm_verdict"],
                }
                for cls, b in buckets.items()
            },
        })

    return _stamp({
        "error": None,
        "from": src,
        "to": dst,
        "date": when.isoformat(),
        "quota": quota.upper(),
        "travel_class": wanted or "all",
        "count": len(trains),
        "includes_nearby_stations": result["nearby_expanded"],
        "trains": trains,
        "note": "Confirmation chances are predictions from historical data, not guarantees.",
    })


@mcp.tool
def get_train_details(train: str) -> dict[str, Any]:
    """Everything known about one train, without needing a route or a date.

    Covers where it starts and ends, departure and arrival times, total
    duration and distance, which days it runs, the classes it carries, average
    speed, zone, pantry car, whether it is an LHB rake, how far in advance
    booking opens, and the seats allotted to each quota.

    Use this for "tell me about train 12951" or "which classes does the
    Rajdhani have". For the stopping pattern use get_train_route; for whether
    seats are actually free on a date use check_seat_availability.

    Args:
        train: Train number or name. e.g. "12951", "Mumbai Rajdhani".
    """
    num, err = _resolve_train(train)
    if err:
        return _stamp(err)
    result = _guard(irctc.train_details, num)
    if result.get("error"):
        return _stamp(result)
    return _stamp({"error": None, "train": _slim_train(result["train"], verbose=True)})


@mcp.tool
def get_train_route(train: str) -> dict[str, Any]:
    """Every stop a train makes, with arrival, departure, halt and distance.

    Use this for "where does this train stop", "what time does it reach X", or
    "how long is the halt at Y". Each stop carries a day number, so an arrival
    after midnight is unambiguous.

    Args:
        train: Train number or name.
    """
    num, err = _resolve_train(train)
    if err:
        return _stamp(err)
    result = _guard(irctc.train_route, num)
    if result.get("error"):
        return _stamp(result)

    return _stamp({
        "error": None,
        "train_number": result["train_number"],
        "train_name": result["train_name"],
        "total_distance_km": result["total_distance_km"],
        "stop_count": result["stop_count"],
        "stops": [
            {
                "n": s["stop_number"],
                "code": s["code"],
                "station": s["name"],
                "arrival": s["arrival"],
                "departure": s["departure"],
                "halt_minutes": s["halt_minutes"],
                "distance_km": s["distance_km"],
                "day": s["day"],
            }
            for s in result["stops"]
        ],
    })


@mcp.tool
def get_coach_position(train: str) -> dict[str, Any]:
    """The physical order of coaches on a train, engine to rear.

    Use this for "where will my coach be on the platform". The sequence is the
    booked rake and can change at short notice, so present it as indicative.

    Args:
        train: Train number or name.
    """
    num, err = _resolve_train(train)
    if err:
        return _stamp(err)
    result = _guard(irctc.coach_position, num)
    if result.get("error"):
        return _stamp(result)
    return _stamp({
        "error": None,
        "train_number": result["train_number"],
        "train_name": result["train_name"],
        "layout": result["layout"],
        "coach_count": result["coach_count"],
        "is_lhb_rake": result["is_lhb_rake"],
        "has_pantry": result["has_pantry"],
        "coaches": [
            {"coach": c["coach"], "class": c["class"], "class_name": c["class_name"]}
            for c in result["coaches"]
        ],
        "note": result["note"],
    })


@mcp.tool
def get_pnr_status(pnr: str) -> dict[str, Any]:
    """Current booking status for a 10-digit PNR.

    Returns per-passenger booking and current status, coach and berth, plus
    whether the chart is prepared. Berth type (Lower/Middle/Upper) is included
    — it is usually the detail people actually want.

    Args:
        pnr: The 10-digit PNR number.
    """
    result = _guard(irctc.pnr_status, pnr)
    if result.get("error"):
        return _stamp(result)
    j = result["journey"]
    return _stamp({
        "error": None,
        "pnr": result["pnr"],
        "train": f'{result["train_name"]} ({result["train_number"]})',
        "date_of_journey": j["date"],
        "boarding": f'{j["boarding_name"]} ({j["boarding_code"]})',
        "destination": f'{j["reservation_upto_name"]} ({j["reservation_upto_code"]})',
        "travel_class": f'{result["travel_class"]} — {result["travel_class_name"]}',
        "quota": result["quota_name"],
        "chart_status": result["chart_status"],
        "total_fare_inr": result["ticket_fare"],
        "passenger_count": result["passenger_count"],
        "all_confirmed": result["all_confirmed"],
        "passengers": [
            {
                "n": p["number"],
                "booked_as": p["booking_status"],
                "current": p["current_status"],
                "coach": p["coach"],
                "berth": p["berth_number"],
                "berth_type": p["berth_type"],
            }
            for p in result["passengers"]
        ],
    })


@mcp.tool
def estimate_refund(pnr: str) -> dict[str, Any]:
    """Estimate the refund if a ticket were cancelled right now.

    Applies the Indian Railways cancellation rules in force since 1 April 2026
    (flat charge over 72 hours, 25% from 72 to 24 hours, 50% from 24 to 8
    hours, nothing under 8 hours), with per-class minimums and the flat
    clerkage on RAC and waitlisted passengers.

    The figure is an estimate: it excludes GST on the cancellation charge,
    IRCTC convenience fees and agent charges. Always relay that caveat, and
    relay the per-passenger rule so the user can see how it was reached.

    This tool cannot cancel anything — it only calculates.

    Args:
        pnr: The 10-digit PNR number.
    """
    result = _guard(irctc.refund_estimate, pnr)
    if result.get("error"):
        return _stamp(result)
    return _stamp({
        "error": None,
        "pnr": result["pnr"],
        "train_number": result.get("train_number"),
        "boarding_station": result.get("boarding_station"),
        "scheduled_departure": result["scheduled_departure"],
        "time_to_departure": result["time_to_departure"],
        "time_since_departure": result["time_since_departure"],
        "has_departed": result["has_departed"],
        "travel_class": result["travel_class"],
        "total_fare_inr": result["total_fare"],
        "total_refund_inr": result["total_refund"],
        "passengers": result["passengers"],
        "rules_version": result["rules_version"],
        "disclaimer": result["disclaimer"],
    })


@mcp.tool
def get_live_status(train: str, start_date: str | None = None) -> dict[str, Any]:
    """Where a train is right now, and how late it is running.

    Use this for "where is my train", "is it delayed", "when will it reach X".
    Data comes from NTES, Indian Railways' own live-running system — the same
    source every third-party tracker resells.

    Returns the current position, delay in minutes, distance covered, and every
    stop with its scheduled and expected times, platform, and per-stop delay.
    Stops the train has already left are marked, so "how far along is it" is
    answerable directly.

    You normally do NOT need start_date. NTES identifies a run by the day it
    STARTED, and a long-distance train running tonight may have departed
    yesterday or the day before — the server resolves that automatically.
    Only pass a date to inspect one specific run.

    The `position` field is a ready-made sentence from NTES and is usually the
    best thing to quote. Delays change minute to minute; say when it was last
    updated rather than implying it is instantaneous.

    Args:
        train: Train number or name.
        start_date: Optional YYYY-MM-DD, the date that run STARTED.
    """
    num, err = _resolve_train(train)
    if err:
        return _stamp(err)

    when = None
    if start_date:
        when, derr = _parse_date(start_date)
        if derr:
            return _stamp({"error": derr})

    result = _guard(irctc.live_status, num, when)
    if result.get("error"):
        return _stamp(result)

    return _stamp({
        "error": None,
        "train": f'{result["train_name"]} ({result["train_number"]})',
        "run_started": result.get("queried_start_date"),
        "position": result["position"],
        "detail": result["position_detail"],
        "last_updated": result["last_update"],
        "delay_minutes": result["delay_minutes"],
        "on_time": result["is_on_time"],
        "distance_covered_km": result["distance_covered_km"],
        "total_distance_km": result["total_distance_km"],
        "stops_completed": f'{result["stops_completed"]} of {result["stop_count"]}',
        "next_stop": result["next_stop"],
        "reached_destination": result["has_reached_destination"],
        "stops": [
            {
                "code": s["code"],
                "station": s["name"],
                "platform": s["platform"],
                "scheduled_arrival": s["scheduled_arrival"],
                "expected_arrival": s["expected_arrival"],
                "scheduled_departure": s["scheduled_departure"],
                "expected_departure": s["expected_departure"],
                "delay_minutes": s["arrival_delay_minutes"],
                "departed": s["has_departed"],
                "distance_km": s["distance_km"],
            }
            for s in result["stops"]
        ],
        "note": "Live positions come from NTES and can lag by a few minutes.",
    })


@mcp.tool
def get_station_board(station: str, hours: int = 2) -> dict[str, Any]:
    """Trains arriving at or leaving a station in the next few hours.

    Use this for "what's coming into <station>", "which trains leave from here
    soon", or "which platform". Returns each train with its platform, expected
    times, live delay, and whether it is cancelled or diverted.

    This is a live departure board, not a timetable — it only covers the next
    2, 4 or 8 hours. For scheduled services on a future date use search_trains.

    Args:
        station: Station name or code.
        hours: How far ahead to look. NTES supports 2, 4 or 8 only.
    """
    code, err = _resolve_station(station, "station")
    if err:
        return _stamp(err)
    if hours not in (2, 4, 8):
        hours = 2

    result = _guard(irctc.station_board, code, hours)
    if result.get("error"):
        return _stamp(result)

    return _stamp({
        "error": None,
        "station": result["station"],
        "window_hours": result["window_hours"],
        "count": result["count"],
        "trains": [
            {
                "train": f'{t["train_name"]} ({t["train_number"]})',
                "from": t["origin"]["name"],
                "to": t["destination"]["name"],
                "platform": t["platform"],
                "scheduled_arrival": t["scheduled_arrival"],
                "expected_arrival": t["expected_arrival"],
                "scheduled_departure": t["scheduled_departure"],
                "expected_departure": t["expected_departure"],
                "arrival_delay_minutes": t["arrival_delay_minutes"],
                "cancelled": t["is_cancelled"],
                "diverted": t["is_diverted"],
            }
            for t in result["trains"]
        ],
    })


# --------------------------------------------------------------------------

def build_app(allowed_hosts: list[str] | None = None) -> ASGIApp:
    return mcp.http_app(path="/mcp", allowed_hosts=allowed_hosts)


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("IRCTC_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("IRCTC_MCP_PORT", "8100"))
    # Behind Cloudflare Tunnel the Host header is the public name, so it has to
    # be allowed explicitly or the request is rejected before reaching a tool.
    allowed = [h.strip() for h in os.environ.get(
        "IRCTC_MCP_ALLOWED_HOSTS", "127.0.0.1,localhost"
    ).split(",") if h.strip()]

    print(f"irctc MCP")
    print(f"  url          : http://{host}:{port}/mcp")
    print(f"  allowed hosts: {allowed}")

    uvicorn.run(build_app(allowed_hosts=allowed), host=host, port=port, log_level="info")
