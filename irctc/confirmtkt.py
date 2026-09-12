"""
Normalizer for the confirmtkt journey-search response.

This is the only source in the stack that returns real JSON, and the only one
with live seat availability plus confirmation prediction. Everything here is a
pure transform of a decoded response body.
"""

from __future__ import annotations

from typing import Any

from .constants import CLASS_NAMES, DAYS


def _run_days(mask: str) -> dict[str, Any]:
    mask = (mask or "").strip()
    if len(mask) != 7:
        return {"mask": mask or None, "days": [], "runs_daily": False}
    days = [DAYS[i] for i, c in enumerate(mask) if c == "1"]
    return {"mask": mask, "days": days, "runs_daily": len(days) == 7}


def _num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _availability(entry: dict) -> dict[str, Any]:
    """One class bucket out of availabilityCache."""
    raw_status = entry.get("availability") or ""
    return {
        "status": raw_status or None,                    # e.g. RLWL37/WL28, AVAILABLE-12, REGRET
        "display": entry.get("availabilityDisplayName"),  # e.g. "WL 28"
        "fare": _num(entry.get("fare")),
        "quota": entry.get("quota"),
        "confirm_probability": _num(entry.get("predictionPercentage")),
        "confirm_prediction": entry.get("predictionDisplayName"),
        "confirm_verdict": entry.get("confirmTktStatus"),  # Confirm / Probable / No Chance
        "is_regret": raw_status.upper().startswith("REGRET"),
        "cached_at": entry.get("cacheTime"),
        # confirmtkt reports availability for ITS OWN resolved station pair,
        # which nearby-expansion may have changed.
        "source_code": entry.get("source"),
        "destination_code": entry.get("destination"),
    }


def _train(t: dict) -> dict[str, Any]:
    general = {c: _availability(e) for c, e in (t.get("availabilityCache") or {}).items()}
    tatkal = {c: _availability(e) for c, e in (t.get("availabilityCacheTatkal") or {}).items()}
    other = {c: _availability(e) for c, e in (t.get("availabilityCacheForQuota") or {}).items()}

    classes = list(t.get("avlClassesSorted") or t.get("avlClasses") or [])
    duration = _num(t.get("duration"))

    return {
        "train_number": t.get("trainNumber"),
        "train_name": t.get("trainName"),
        "train_type": t.get("trainType"),          # R = Rajdhani-family, O = ordinary

        # The pair confirmtkt actually resolved to. With enableNearby=true this
        # is frequently NOT what was asked for.
        "journey": {
            "from_code": t.get("fromStnCode"),
            "from_name": t.get("fromStnName"),
            "to_code": t.get("toStnCode"),
            "to_name": t.get("toStnName"),
            "from_city": t.get("fromCityName"),
            "to_city": t.get("toCityName"),
            "departure": t.get("departureTime"),
            "arrival": t.get("arrivalTime"),
            "duration_minutes": duration,
            "duration": f"{duration // 60:02d}:{duration % 60:02d}" if duration else None,
            "distance_km": _num(t.get("distance")),
            "has_departed": bool(t.get("hasDeparted")),
        },

        "origin": {
            "code": t.get("trainOriginStationCode"),
            "name": t.get("trainOriginStationName"),
        },
        "destination": {
            "code": t.get("trainDestinationStationCode"),
            "name": t.get("trainDestinationStationName"),
        },

        "runs_on": _run_days(t.get("runningDays")),
        "classes": classes,
        "class_names": [CLASS_NAMES.get(c, c) for c in classes],
        "allowed_quotas": list(t.get("allowedQuotas") or []),

        "availability": {
            "general": general,
            "tatkal": tatkal,
            "other_quota": other,
        },

        "has_pantry": bool(t.get("hasPantry")),
        "rating": _num(t.get("trainRating")),
    }


def parse_search(payload: dict) -> dict[str, Any]:
    """Full confirmtkt search response -> clean dict.

    The upstream returns HTTP 200 with `data.errorMessage` set on failure, so
    callers must check `error`, not the status code.
    """
    data = (payload or {}).get("data") or {}
    if data.get("errorMessage"):
        return {
            "error": data.get("errorMessage"),
            "error_code": data.get("errorCode"),
            "trains": [],
        }

    trains = [_train(t) for t in (data.get("trainList") or [])]

    # Nearby expansion means the returned legs may not be the pair requested.
    pairs = {(t["journey"]["from_code"], t["journey"]["to_code"]) for t in trains}

    return {
        "error": None,
        "trains": trains,
        "count": len(trains),
        "quotas_available": list(data.get("quotaList") or []),
        "station_pairs_returned": sorted(f"{a}-{b}" for a, b in pairs if a and b),
        "nearby_expanded": len(pairs) > 1,
        "served_from_cache": bool(data.get("fromCache")),
    }
