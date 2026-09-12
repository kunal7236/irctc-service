"""
NTES — India's National Train Enquiry System.

This talks to the backend of Indian Railways' own NTES Android app:

    POST https://enquiry.indianrail.gov.in/crisns/AppServAnd
    {"jsonIn": "<MD5(payload + sckey)>#<hex(base64(AES-CBC(payload)))>"}

Both directions are AES-128-CBC with a fixed key and IV. No key, no signup, no
captcha on any of the endpoints used here.

Why this and not a reseller: RailYatri, confirmtkt and the paid APIs are all
relaying NTES. RailYatri's payload literally reports `data_from: "ntes"`. This
is the source, so it is first-hand, complete, and free of anyone else's
trimming.

Deliberately NOT implemented: the PNR lookup on indianrail.gov.in, which is
captcha-gated. `irctc.pnr` already covers PNR through an ungated endpoint.

Field names are terse (`CPOS`, `LDEL`, `ISA`) and a couple are traps — `DF` is
zero on every row and is NOT the delay. The real delays are `DARR`/`DDEP` per
stop and `LDEL` at the top level. All of this is verified against captured
payloads in tests/fixtures.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Any

URL = "https://enquiry.indianrail.gov.in/crisns/AppServAnd"
SERVICE = "service=TrainRunningMob&subService="
USER_AGENT = "Dalvik/2.1.0 (Linux; Android 11)"

_KEY = b"8EA4DB2CC1EB3DC5"
_IV = b"7DC5EB3BB4DB6EA8"
_SCKEY = "645fbc1e56e23365f2f3c204ae0899f6"


# --------------------------------------------------------------------------
# wire format
# --------------------------------------------------------------------------

def _aes(mode_encrypt: bool):
    # Imported lazily so the pure parsers below stay stdlib-only.
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    c = Cipher(algorithms.AES(_KEY), modes.CBC(_IV))
    return c.encryptor() if mode_encrypt else c.decryptor()


def encrypt(payload: str) -> str:
    """Build the `jsonIn` value: checksum, then AES -> base64 -> hex -> upper."""
    from cryptography.hazmat.primitives import padding

    checksum = hashlib.md5((payload + _SCKEY).encode()).hexdigest().upper()
    padder = padding.PKCS7(128).padder()
    data = padder.update(payload.encode()) + padder.finalize()
    enc = _aes(True)
    body = binascii.hexlify(base64.b64encode(enc.update(data) + enc.finalize()))
    return f"{checksum}#{body.decode().upper()}"


def decrypt(blob: str) -> Any:
    """Reverse of `encrypt` for the response body."""
    import json

    from cryptography.hazmat.primitives import padding

    if "#" in blob:
        blob = blob.split("#", 1)[1]
    ciphertext = base64.b64decode(binascii.unhexlify(blob).decode())
    dec = _aes(False)
    raw = dec.update(ciphertext) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return json.loads((unpadder.update(raw) + unpadder.finalize()).decode())


# --------------------------------------------------------------------------
# payload builders — names verified against the NTES app, not guessed
# --------------------------------------------------------------------------

def p_live_status(train: str, start_date: str) -> str:
    """start_date is the date the train STARTED, formatted DD-Mon-YYYY."""
    return f"{SERVICE}ShowFullRunJson&trainNo={train}&startDate={start_date}"


def p_instances(train: str) -> str:
    return f"{SERVICE}GetTrainInstance&trainNo={train}"


def p_station_board(station: str, hours: int = 2) -> str:
    return f"{SERVICE}TrainsAtStationJson&jStation={station}&nHr={hours}&jToStation="


def p_exceptions(train: str) -> str:
    return f"{SERVICE}TrainExcpInfo&trainNo={train}"


def p_schedule(train: str) -> str:
    return f"{SERVICE}GetTrainSchedule&trainNo={train}&startDate="


def p_search(query: str) -> str:
    return f"{SERVICE}FindTrainJson&trainNo={query}"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _time(v) -> str | None:
    """NTES marks the endpoints with words, not times.

    The origin's arrival reads "Source" and the terminus' departure reads
    "Destination" — the same trick erail plays with "First"/"Last". Passing
    them through as times is how you end up rendering "Source" in a clock
    column.
    """
    t = _s(v)
    if not t or t.lower() in ("source", "destination"):
        return None
    return t


def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _delay_minutes(value) -> int | None:
    """NTES writes delays as `HH:MM`, or the literal `On Time`."""
    v = _s(value)
    if not v:
        return None
    if v.lower().replace(" ", "") in ("ontime", "righttime"):
        return 0
    m = re.fullmatch(r"(\d{1,3}):(\d{2})", v)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _coaches(position: str, classes: str) -> list[dict[str, str | None]]:
    """`ENG-LPR-B1-B2...` zipped with `ENG-LPR-3A-3A...`.

    Unlike erail's static rake, this is the composition for THIS run, and NTES
    reports it separately on arrival and departure (they differ where coaches
    are attached or detached en route).
    """
    pos = [p for p in _s(position).split("-") if p]
    cls = [c for c in _s(classes).split("-") if c]
    out = []
    for i, coach in enumerate(pos):
        c = cls[i] if i < len(cls) else None
        out.append({
            "coach": coach,
            "class": c,
            "is_engine": coach.upper() in ("ENG", "EN"),
        })
    return out


def _alert(payload: dict) -> str | None:
    """NTES answers HTTP 200 with an AlertMsg rather than an error status."""
    for key in ("AlertMsg", "alertMsg"):
        msg = _s(payload.get(key))
        if msg:
            return msg
    return None


# --------------------------------------------------------------------------
# parsers — pure, no network, no crypto
# --------------------------------------------------------------------------

def parse_live_status(payload: dict) -> dict[str, Any]:
    """`ShowFullRunJson` -> clean dict.

    Position is derived from the ISA/ISD flags: the train is at the last stop
    whose ISD is true. NTES also ships a ready-made human sentence in `CPOS`
    and `LUPDFULL`, which is usually the best thing to quote verbatim.
    """
    payload = payload or {}
    alert = _alert(payload)
    stns = payload.get("STNS") or []
    if alert and not stns:
        return {"error": alert, "stops": []}

    stops = []
    for i, s in enumerate(stns):
        arr_delay = _delay_minutes(s.get("DARR"))
        dep_delay = _delay_minutes(s.get("DDEP"))
        departed = _s(s.get("ISD")).lower() == "true"
        arrived = _s(s.get("ISA")).lower() == "true"
        stops.append({
            "index": i,
            "code": _s(s.get("SC")),
            "name": _s(s.get("SN")),
            "name_hindi": _s(s.get("SHN")) or None,
            "distance_km": _int(s.get("DIST")),
            "platform": _s(s.get("PF")) or None,
            "stop_number": _int(s.get("Sr")),
            # Scheduled vs expected/actual. Both carry a date, so overnight
            # runs are unambiguous without any day arithmetic.
            "scheduled_arrival": _time(s.get("STA")),
            "scheduled_departure": _time(s.get("STD")),
            "expected_arrival": _time(s.get("ETA")),
            "expected_departure": _time(s.get("ETD")),
            "arrival_delay_minutes": arr_delay,
            "departure_delay_minutes": dep_delay,
            "arrival_delay": _s(s.get("DARR")) or None,
            "departure_delay": _s(s.get("DDEP")) or None,
            "has_arrived": arrived,
            "has_departed": departed,
            "is_reversal": bool(_int(s.get("reversalNumber"))),
            "is_origin": _s(s.get("STA")).lower() == "source",
            "is_destination": _s(s.get("STD")).lower() == "destination",
            "coaches_on_arrival": _coaches(s.get("arrivalCoachPosition"), s.get("arrivalCoachClass")),
            "coaches_on_departure": _coaches(s.get("departureCoachPosition"), s.get("departureCoachClass")),
            "pwd_coach_arrival": _s(s.get("arrPWDCoachPosition")) or None,
            "pwd_coach_departure": _s(s.get("depPWDCoachPosition")) or None,
            # Non-halt stations between this stop and the next.
            "passes_through": [
                {
                    "code": _s(w.get("SC")),
                    "name": _s(w.get("SN")) or None,
                    "scheduled_arrival": _s(w.get("STA")) or None,
                    "distance_km": _int(w.get("DIST")),
                }
                for w in (s.get("WTTSTNS") or [])
            ],
        })

    departed_stops = [s for s in stops if s["has_departed"]]
    current_index = departed_stops[-1]["index"] if departed_stops else None
    upcoming = [s for s in stops if not s["has_arrived"]]

    delay = _int(payload.get("LDEL"))
    started = bool(departed_stops)

    return {
        "error": None,
        "train_number": _s(payload.get("TN")) or None,
        "train_name": _s(payload.get("TNM")) or None,
        "train_name_hindi": _s(payload.get("TNH")) or None,
        "start_date": _s(payload.get("STD")) or None,

        "origin": {"code": _s(payload.get("SRC")), "name": _s(payload.get("SRCN"))},
        "destination": {"code": _s(payload.get("DSTN")), "name": _s(payload.get("DSTNN"))},

        # NTES writes these sentences itself; quoting them beats re-deriving.
        "position": _s(payload.get("CPOS")) or None,
        "position_detail": _s(payload.get("LUPDFULL")) or None,
        "last_update": _s(payload.get("LTIME")) or None,

        "delay_minutes": delay,
        "is_on_time": delay == 0 if delay is not None else None,
        "has_started": started,
        "has_reached_destination": _s(payload.get("isArrDSTN")).lower() == "true",

        "last_station": {
            "code": _s(payload.get("LSTN")) or None,
            "name": _s(payload.get("LSTNN")) or None,
        },
        # The very next station, which is often a non-halt cabin or yard.
        "next_station": {
            "code": _s(payload.get("NSTN")) or None,
            "name": _s(payload.get("NSTNN")) or None,
        },
        # The next station the train actually stops at.
        "next_stop": {
            "code": _s(payload.get("NPSTN")) or None,
            "name": _s(payload.get("NPSTNN")) or None,
        },
        "last_event": "departed" if _s(payload.get("LEVNT")).upper() == "DR" else "arrived",

        "distance_covered_km": _int(payload.get("LDSRC")),
        "total_distance_km": _int(payload.get("TTLDIST")),

        "current_stop_index": current_index,
        "stops_completed": len(departed_stops),
        "stops_remaining": len(upcoming),
        "stop_count": len(stops),
        "stops": stops,

        "raw": {
            "schedule_type": _s(payload.get("SchType")) or None,
            "last_update_short": _s(payload.get("LUPDT")) or None,
            "coach_position_available": _s(payload.get("coachPositionFlag")).lower() == "true",
        },
    }


def parse_instances(payload: dict) -> dict[str, Any]:
    """`GetTrainInstance` -> which dated runs exist and which are moving.

    This is what removes the today-or-yesterday guesswork: an overnight train
    running right now started yesterday, and only this call knows that.
    """
    payload = payload or {}
    runs = []
    for r in payload.get("vInstanceList") or []:
        status = _s(r.get("trainStatus"))
        runs.append({
            "start_date": _s(r.get("startDate")),
            "is_running": status == "1",
            "status_code": status,
            "position": _s(r.get("trainPosition")) or None,
            "exception": _s(r.get("excpMsg")) or None,
        })
    running = [r for r in runs if r["is_running"]]
    return {
        "error": _alert(payload) if not runs else None,
        "train_number": _s(payload.get("TrainNo")) or None,
        "train_name": _s(payload.get("TrainName")) or None,
        "origin": {"code": _s(payload.get("Src")), "name": _s(payload.get("SrcName"))},
        "destination": {"code": _s(payload.get("Dstn")), "name": _s(payload.get("DstnName"))},
        "runs": runs,
        "running_now": running,
        # Most recent live run; the one a "where is my train" question means.
        "active_start_date": running[0]["start_date"] if running else None,
    }


def parse_station_board(payload: dict) -> dict[str, Any]:
    """`TrainsAtStationJson` -> trains due at a station in the next N hours."""
    payload = payload or {}
    rows = payload.get("TrainsAtStation") or []
    alert = _alert(payload)
    if alert and not rows:
        return {"error": alert, "trains": []}

    trains = []
    for t in rows:
        trains.append({
            "train_number": _s(t.get("TrainNumber")),
            "train_name": _s(t.get("TrainName")),
            "train_type": _s(t.get("TrainTypeDesc")) or None,
            "origin": {"code": _s(t.get("Source")), "name": _s(t.get("SourceName"))},
            "destination": {"code": _s(t.get("Destination")), "name": _s(t.get("DestinationName"))},
            "start_date": _s(t.get("StartDate")) or None,
            "platform": _s(t.get("Platform")) or None,
            "scheduled_arrival": _time(t.get("STA")),
            "scheduled_departure": _time(t.get("STD")),
            "expected_arrival": _time(t.get("ETA")),
            "expected_departure": _time(t.get("ETD")),
            "arrival_delay_minutes": _delay_minutes(t.get("DelayArr")),
            "departure_delay_minutes": _delay_minutes(t.get("DelayDep")),
            "has_arrived": _s(t.get("ArrFlag")) == "1",
            "has_departed": _s(t.get("DepFlag")) == "1",
            "is_cancelled": _s(t.get("Cancel")) not in ("", "0"),
            "is_diverted": _s(t.get("Diverted")) not in ("", "0"),
            "source_changed": _s(t.get("SrcChange")) not in ("", "0"),
            "destination_changed": _s(t.get("DstnChange")) not in ("", "0"),
            "coaches": _coaches(t.get("departureCoachPosition"), t.get("departureCoachClass")),
        })

    return {
        "error": None,
        "station": {"code": _s(payload.get("Station")), "name": _s(payload.get("StationName"))},
        "window_hours": _int(payload.get("NextHr")),
        "count": len(trains),
        "trains": trains,
    }


def parse_exceptions(payload: dict) -> dict[str, Any]:
    """`TrainExcpInfo` -> cancellations, diversions, reschedules.

    "No Exceptional Details found" arrives as an AlertMsg and means the train
    is running normally — that is a clean result, not a failure.
    """
    payload = payload or {}
    alert = _alert(payload)
    normal = bool(alert) and "no exceptional" in alert.lower()
    return {
        "error": None if (normal or not alert) else alert,
        "train_number": _s(payload.get("TrainNumber")) or None,
        "train_name": _s(payload.get("TrainName")) or None,
        "origin": {"code": _s(payload.get("Source")), "name": _s(payload.get("SourceName"))},
        "destination": {"code": _s(payload.get("Destination")), "name": _s(payload.get("DestinationName"))},
        "days_of_run": _s(payload.get("DaysOfRun")) or None,
        "has_exceptions": not normal and not alert,
        "message": alert,
    }


def parse_schedule(payload: dict) -> dict[str, Any]:
    """`GetTrainSchedule` -> official schedule metadata."""
    payload = payload or {}
    return {
        "error": _alert(payload) or None,
        "train_number": _s(payload.get("TrainNumber")) or None,
        "train_name": _s(payload.get("TrainName")) or None,
        "train_type": _s(payload.get("TrainTypeDesc")) or None,
        "origin": {"code": _s(payload.get("Source")), "name": _s(payload.get("SourceName"))},
        "destination": {"code": _s(payload.get("Destination")), "name": _s(payload.get("DestinationName"))},
        "travel_time": _s(payload.get("TravelTime")) or None,
        "days_of_run": _s(payload.get("DaysOfRun")) or None,
        "classes": [c for c in _s(payload.get("ClassOfTravel")).split(",") if c],
        "valid_from": _s(payload.get("ValidFrom")) or None,
        "available_start_dates": list(payload.get("vStartDateList") or []),
    }


def parse_search(payload: dict) -> dict[str, Any]:
    """`FindTrainJson` -> trains matching a name or partial number."""
    payload = payload or {}
    trains = [
        {
            "train_number": _s(t.get("TrainNumber")),
            "train_name": _s(t.get("TrainName")),
            "train_type": _s(t.get("Type")) or None,
            "origin": {"code": _s(t.get("Source")), "name": _s(t.get("SourceName"))},
            "destination": {"code": _s(t.get("Destination")), "name": _s(t.get("DestinationName"))},
        }
        for t in (payload.get("Trains") or [])
    ]
    return {"error": None if trains else _alert(payload), "count": len(trains), "trains": trains}
