"""
Parsers for erail's two `~`-delimited endpoints.

Pure stdlib, no network, no I/O — every function here takes a response body and
returns plain dicts, so they can be unit-tested against captured payloads.

Both `getTrains.aspx?Station_From=..&Station_To=..` and
`getTrains.aspx?TrainNo=..` return the SAME 72-field record layout; only the
set of returned records differs. `parse_trains()` handles both.
"""

from __future__ import annotations

import html
import re
from typing import Any

from .constants import CLASSES, CLASS_NAMES, DAYS, QUOTAS, R, T


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _get(parts: list[str], idx: int) -> str:
    return parts[idx].strip() if 0 <= idx < len(parts) else ""


def _int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _time(value: str) -> str | None:
    """erail writes times as `05.00`. Normalise to `05:00`.

    Returns None for the sentinels erail uses at a train's endpoints
    ("First" at the origin's arrival, "Last" at the terminus' departure).
    """
    value = (value or "").strip()
    if not value or value.lower() in ("first", "last"):
        return None
    return value.replace(".", ":")


def _duration_minutes(value: str) -> int | None:
    """`17.40` means 17h40m, not 17.4 hours."""
    value = (value or "").strip()
    m = re.fullmatch(r"(\d+)\.(\d{1,2})", value)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _run_days(mask: str) -> dict[str, Any]:
    """`1111100` -> the days the train runs. Index 0 is Monday."""
    mask = (mask or "").strip()
    if len(mask) != 7:
        return {"mask": mask or None, "days": [], "runs_daily": False}
    days = [DAYS[i] for i, c in enumerate(mask) if c == "1"]
    return {"mask": mask, "days": days, "runs_daily": len(days) == 7}


def _hindi(value: str) -> str | None:
    """Route rows carry the Hindi name as HTML numeric entities."""
    value = (value or "").strip()
    return html.unescape(value) or None if value else None


# --------------------------------------------------------------------------
# field 41 — fares, and field 62 — per-quota seat counts
# --------------------------------------------------------------------------

def _parse_fare_block(raw: str) -> dict[str, Any]:
    """`SUPERFAST:1330:<g0>:<g1>:...:<g14>` — one group per class in CLASSES.

    Each group is six comma-separated numbers. erail's public bundle never
    consumes them, so their individual meanings are unconfirmed; the first
    value consistently matches the headline fare for that class. They are
    exposed verbatim rather than guessed at.
    """
    parts = (raw or "").split(":")
    if len(parts) < 3:
        return {"train_type": None, "distance_km": None, "by_class": {}}

    groups = parts[2:]
    by_class: dict[str, list[int | None]] = {}
    for idx, group in enumerate(groups):
        if idx >= len(CLASSES):
            break
        if not group.strip(", "):
            continue
        by_class[CLASSES[idx]] = [_int(v) for v in group.split(",")]

    return {
        "train_type": parts[0] or None,
        "distance_km": _int(parts[1]),
        "by_class": by_class,
    }


def _parse_seat_block(raw: str) -> dict[str, dict[str, int]]:
    """`2A:65:80:30:20:4:::2::3:|3A:...` — seats per quota, per class.

    The ten colon slots after the class code map positionally onto QUOTAS
    (GN TQ PT LD SS FT HP YU DP PH), per erail's own `d_Quota`.
    """
    out: dict[str, dict[str, int]] = {}
    for chunk in (raw or "").split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        bits = chunk.split(":")
        cls, values = bits[0], bits[1:]
        if not cls:
            continue
        quotas = {}
        for i, quota in enumerate(QUOTAS):
            n = _int(values[i]) if i < len(values) else None
            if n:
                quotas[quota] = n
        out[cls] = quotas
    return out


def _parse_coaches(raw: str) -> list[dict[str, str | None]]:
    """`,,En:LPR,LPR,LPR:B,B1,3A:...` — the physical rake, engine first.

    Each `:`-separated segment is `prefix,coachId,class`. The leading `,,En`
    marks the locomotive. This is a fixed structure, not something that needs
    heuristic sniffing.
    """
    coaches = []
    for seg in (raw or "").split(":"):
        seg = seg.strip()
        if not seg:
            continue
        cols = [c.strip() for c in seg.split(",")]
        while len(cols) < 3:
            cols.append("")
        prefix, coach_id, cls = cols[0], cols[1], cols[2]
        label = coach_id or cls or prefix
        if not label:
            continue
        is_engine = label.upper() == "EN"
        # "En" occupies the class column for the locomotive segment; it is a
        # marker, not a travel class.
        cls_code = "" if is_engine else cls.upper()
        # The third column is a travel class for passenger coaches, but a type
        # marker for the rest: LPR (loco pilot rear / brake van), SLRD (sleeper
        # cum luggage), PC (pantry car), VP (parcel van), GN (general).
        coaches.append({
            "coach": label.upper(),
            "class": cls_code or None,
            "class_name": CLASS_NAMES.get(cls_code),
            "is_engine": is_engine,
            "is_passenger_class": cls_code in CLASS_NAMES,
        })
    return coaches


def _classes_from_mask(mask: str) -> list[str]:
    return [CLASSES[i] for i, c in enumerate(mask or "") if c == "1" and i < len(CLASSES)]


# --------------------------------------------------------------------------
# getTrains.aspx
# --------------------------------------------------------------------------

def parse_train_record(record: str) -> dict[str, Any] | None:
    """One `^`-delimited record from getTrains.aspx -> a clean dict."""
    parts = record.split("~")
    if len(parts) < 63 or not _get(parts, T["No"]):
        return None

    mask = _get(parts, T["Classes"])
    seats = _parse_seat_block(_get(parts, T["MaxSeats"]))
    fares = _parse_fare_block(_get(parts, T["Fare"]))

    # Three sources disagree on rare records (9 of 155 captured). Prefer the
    # seat block, fall back to the mask, and surface the disagreement rather
    # than silently picking one.
    from_mask = set(_classes_from_mask(mask))
    from_seats = set(seats)
    from_fares = set(fares["by_class"])
    classes = sorted(from_seats or from_mask or from_fares,
                     key=lambda c: CLASSES.index(c) if c in CLASSES else 99)

    travel_raw = _get(parts, T["Travel"])

    return {
        "train_number": _get(parts, T["No"]),
        "train_name": _get(parts, T["Name"]),
        "train_long_name": _get(parts, T["LongName"]) or None,
        "train_id": _get(parts, T["TrainID"]) or None,   # needed by the route call
        "train_type": _get(parts, T["TrainType"]) or None,
        "train_type_label": _get(parts, T["TrainType2"]) or None,

        "origin": {
            "code": _get(parts, T["StnFirst"]),
            "name": _get(parts, T["StnFirstName"]),
            "departure": _time(_get(parts, T["DepartureFirstStn"])),
        },
        "destination": {
            "code": _get(parts, T["StnLast"]),
            "name": _get(parts, T["StnLastName"]),
            "arrival": _time(_get(parts, T["ArrivalLastStn"])),
        },

        # The searched leg. For a TrainNo query this equals origin/destination;
        # for a station-pair query it may be a NEARBY station, not the one
        # asked for — surfacing both codes is what stops an agent claiming a
        # train leaves New Delhi when it actually leaves Nizamuddin.
        "journey": {
            "from_code": _get(parts, T["StnFrom"]),
            "from_name": _get(parts, T["StnFromName"]),
            "to_code": _get(parts, T["StnTo"]),
            "to_name": _get(parts, T["StnToName"]),
            "departure": _time(_get(parts, T["Departure"])),
            "arrival": _time(_get(parts, T["Arrival"])),
            "duration": travel_raw.replace(".", ":") or None,
            "duration_minutes": _duration_minutes(travel_raw),
            "from_halt_minutes": _int(_get(parts, T["StnFromHalt"])),
            "to_halt_minutes": _int(_get(parts, T["StnToHalt"])),
            "from_stop_number": _int(_get(parts, T["StnFromSNo"])),
            "to_stop_number": _int(_get(parts, T["StnToSNo"])),
            "from_platform": _get(parts, T["PFFrom"]) or None,
            "to_platform": _get(parts, T["PFTo"]) or None,
            "arrival_at_from": _time(_get(parts, T["Arrival_AtFrom"])),
            "departure_at_to": _time(_get(parts, T["DepartureAtTo"])),
        },

        "runs_on": _run_days(_get(parts, T["RunDays"])),
        "runs_on_from_origin": _run_days(_get(parts, T["RunDaysFirstStn"])),

        "classes": classes,
        "class_names": [CLASS_NAMES.get(c, c) for c in classes],
        "class_sources_disagree": bool(from_seats and from_mask and from_seats != from_mask),

        "seats_by_class": seats,
        "fares": fares,
        "coaches": _parse_coaches(_get(parts, T["Rakes"])),

        "distance_km": _int(_get(parts, T["Distance"])),
        "avg_speed_kmph": _int(_get(parts, T["AvSpeed"])),
        "has_pantry": _get(parts, T["Pantry"]) == "1",
        "is_lhb_rake": _get(parts, T["LHB"]) == "1",
        "is_duronto": _get(parts, T["IsDuronto"]) == "1",
        "is_garib_rath": _get(parts, T["IsGaribRath"]) == "1",
        "is_special": _get(parts, T["IsSpecial"]) == "1",
        "advance_reservation_days": _int(_get(parts, T["ARP"])),
        "zone": _get(parts, T["OwningRailway"]) or None,
        "division": _get(parts, T["Division"]) or None,
        "gauge": _get(parts, T["Guage"]) or None,
        "rake_shared_with": [t for t in _get(parts, T["RakeShare"]).split(",") if t],
        "train_number_alias": _get(parts, T["TrainNoAlias"]) or None,
        "schedule_valid_from": _get(parts, T["DateFrom"]) or None,
        "schedule_valid_to": _get(parts, T["DateTo"]) or None,
        "note": _get(parts, T["Note"]) or None,

        # Undecoded by erail's own public bundle. Kept verbatim so nothing is
        # lost; strip before handing a payload to a model.
        "raw": {
            "flags": _get(parts, T["Flags"]) or None,
            "class_mask": mask or None,
            "status": _get(parts, T["Status"]) or None,
            "route_id": _get(parts, T["RouteID"]) or None,
            "reverse": _get(parts, T["Reverse"]) or None,
            "data_source": _get(parts, T["DataSource"]) or None,
        },
    }


def parse_trains(body: str) -> list[dict[str, Any]]:
    """Full getTrains.aspx body -> list of clean train dicts."""
    segments = (body or "").split("^")[1:]
    out = []
    for seg in segments:
        rec = parse_train_record(seg)
        if rec:
            out.append(rec)
    return out


def filter_by_day(trains: list[dict], weekday_index: int) -> list[dict]:
    """Keep trains that run on a given weekday. 0 = Monday, matching `date.weekday()`.

    erail returns every train on the route regardless of date, so this filter
    is the caller's job. It matches on the boarding station's running-day mask,
    which is the one that matters for a passenger joining there.
    """
    out = []
    for t in trains:
        mask = (t.get("runs_on") or {}).get("mask") or ""
        if len(mask) == 7 and mask[weekday_index] == "1":
            out.append(t)
    return out


# --------------------------------------------------------------------------
# data.aspx?Action=TRAINROUTE
# --------------------------------------------------------------------------

def parse_route(body: str) -> dict[str, Any]:
    """TRAINROUTE response -> stops plus the per-segment seat matrix.

    The body is two sections joined at the first `^`:
      1. `#`-separated origin-destination pairs, each with per-class counts
      2. `^`-separated station rows, 18 `~`-delimited fields each
    """
    body = (body or "").strip()
    if not body:
        return {"stops": [], "segments": []}

    head, _, tail = body.partition("^")

    # -- section 1: seat matrix, e.g. MMCT-NDLS-1A|10:2A|65|20|80:3A|182|55|242
    segments = []
    for chunk in head.split("#"):
        chunk = chunk.strip()
        if not chunk:
            continue
        classes_raw = chunk.split(":")
        first = classes_raw[0]
        m = re.match(r"^([A-Z0-9]+)-([A-Z0-9]+)-(.*)$", first)
        if not m:
            continue
        from_code, to_code, first_class = m.group(1), m.group(2), m.group(3)
        by_class = {}
        for piece in [first_class] + classes_raw[1:]:
            bits = piece.split("|")
            if len(bits) < 2 or not bits[0]:
                continue
            by_class[bits[0]] = [_int(v) for v in bits[1:]]
        segments.append({"from_code": from_code, "to_code": to_code, "seats_by_class": by_class})

    # -- section 2: station rows
    stops = []
    for row in tail.split("^"):
        row = row.strip()
        if not row:
            continue
        p = row.split("~")
        if len(p) < 16 or not _get(p, R["Code"]):
            continue
        stops.append({
            "stop_number": _int(_get(p, R["SNo"])),
            "code": _get(p, R["Code"]),
            "name": _get(p, R["Name"]),
            "name_hindi": _hindi(_get(p, R["NameHindi"])),
            "arrival": _time(_get(p, R["Arrival"])),
            "departure": _time(_get(p, R["Departure"])),
            "is_origin": _get(p, R["Arrival"]).lower() == "first",
            "is_destination": _get(p, R["Departure"]).lower() == "last",
            "halt_minutes": _int(_get(p, R["HaltMinutes"])),
            "distance_km": _int(_get(p, R["DistanceKm"])),
            "day": _int(_get(p, R["Day"])),
            # Elapsed minutes from the train's origin. These make next-day
            # arrivals exact instead of inferred from wall-clock times.
            "minutes_from_origin_arrival": _int(_get(p, R["MinutesToArrival"])),
            "minutes_from_origin_departure": _int(_get(p, R["MinutesToDeparture"])),
            "zone": _get(p, R["Zone"]) or None,
            "division": _get(p, R["Division"]) or None,
            "quota_type": _get(p, R["QuotaType"]) or None,   # PQ / RL
            "latitude": _float(_get(p, R["Latitude"])),
            "longitude": _float(_get(p, R["Longitude"])),
        })

    return {"stops": stops, "segments": segments}


def departure_from_station(route: dict, station_code: str) -> str | None:
    """Scheduled departure from one stop, matched on CODE.

    The PNR payload carries `boardingPoint` as a code (e.g. `SCL`), which
    matches a route stop exactly — so there is no need to fuzzy-match station
    names. Falls back to the arrival time at a terminating stop.
    """
    code = (station_code or "").strip().upper()
    if not code:
        return None
    for stop in route.get("stops", []):
        if stop["code"].upper() == code:
            return stop.get("departure") or stop.get("arrival")
    return None
