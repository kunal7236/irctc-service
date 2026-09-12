"""
Cancellation refund estimate.

There is no refund API — this is pure computation over a normalized PNR plus
the train's schedule (for the exact departure time from the boarding station).

The rule table is deliberately data-driven — IRCTC slabs have changed before,
most recently in April 2026. Swapping RULES is one edit and requires no change
to the logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .constants import (
    CLERKAGE_CHARGE,
    DEFAULT_MIN_CANCELLATION_CHARGE,
    MIN_CANCELLATION_CHARGE,
)

# Slabs for a CONFIRMED ticket, evaluated top-down on hours-until-departure.
# `pct` is the share of fare deducted; the deduction is never less than the
# per-class flat minimum. `pct=1.0` means no refund.
#
# Current rules, effective 1 April 2026: the penalty-free window moved from 48
# to 72 hours and the no-refund cliff from 4 to 8 hours. The per-class flat
# charges were not changed by that revision, and Tatkal, RAC and waitlist
# handling is unchanged. The previous structure was 48 / 12 / 4.
RULES = [
    {"min_hours": 72.0, "pct": 0.0, "label": "More than 72h before departure — flat charge only"},
    {"min_hours": 24.0, "pct": 0.25, "label": "72h to 24h before departure — 25% of fare"},
    {"min_hours": 8.0, "pct": 0.50, "label": "24h to 8h before departure — 50% of fare"},
    {"min_hours": float("-inf"), "pct": 1.00, "label": "Under 8h before departure — no refund"},
]

RULES_VERSION = "irctc-2026-04-72-24-8"


def _slab(hours: float) -> dict:
    for rule in RULES:
        if hours >= rule["min_hours"]:
            return rule
    return RULES[-1]


def _humanise(seconds: float) -> str:
    """Duration as prose. Callers handle sign; this always describes a length."""
    seconds = abs(seconds)
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    bits = []
    if days:
        bits.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        bits.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes and not days:
        bits.append(f"{minutes} min")
    return ", ".join(bits) or "less than a minute"


def estimate(
    pnr: dict[str, Any],
    scheduled_departure: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Estimate the refund for a normalized PNR.

    Args:
        pnr: output of `irctc.pnr.parse_status`.
        scheduled_departure: `HH:MM` departure from the BOARDING station, from
            `irctc.erail.departure_from_station(route, pnr['journey']['boarding_code'])`.
        now: injectable for testing.

    Returns a dict with a per-passenger breakdown. Every deduction states which
    rule produced it, so the answer is auditable rather than a bare number.
    """
    now = now or datetime.now()

    if pnr.get("error"):
        return {"error": pnr["error"], "passengers": []}

    passengers = pnr.get("passengers") or []
    fare_each = pnr.get("fare_per_passenger")
    if not passengers or not fare_each:
        return {"error": "Cannot estimate: missing fare or passenger data.", "passengers": []}

    date_str = (pnr.get("journey") or {}).get("date")
    if not date_str or not scheduled_departure:
        return {"error": "Cannot estimate: missing journey date or scheduled departure time.",
                "passengers": []}

    try:
        departs_at = datetime.strptime(
            f"{date_str.replace('/', '-')} {scheduled_departure}", "%d-%m-%Y %H:%M"
        )
    except ValueError as exc:
        return {"error": f"Cannot parse departure datetime: {exc}", "passengers": []}

    seconds_to_departure = (departs_at - now).total_seconds()
    hours_to_departure = seconds_to_departure / 3600.0

    # Class CODE, not the prose name — the prose name never matches a lookup key.
    cls = (pnr.get("travel_class") or "").upper()
    flat_minimum = MIN_CANCELLATION_CHARGE.get(cls, DEFAULT_MIN_CANCELLATION_CHARGE)
    is_tatkal = bool(pnr.get("is_tatkal"))

    breakdown = []
    total = 0.0
    for p in passengers:
        if is_tatkal and p["is_confirmed"]:
            deduction, rule = fare_each, "Confirmed Tatkal — no refund"
        elif p["is_waitlisted"] or p["is_rac"]:
            deduction, rule = float(CLERKAGE_CHARGE), f"RAC/waitlisted — flat ₹{CLERKAGE_CHARGE} clerkage"
        else:
            slab = _slab(hours_to_departure)
            deduction = fare_each if slab["pct"] >= 1.0 else max(flat_minimum, slab["pct"] * fare_each)
            rule = slab["label"]
            if slab["pct"] == 0.0:
                rule += f" (₹{flat_minimum} for {cls})"

        deduction = min(deduction, fare_each)
        refund = round(fare_each - deduction, 2)
        total += refund
        breakdown.append({
            "passenger": p["number"],
            "status": p["current_status"],
            "fare": fare_each,
            "deduction": round(deduction, 2),
            "refund": refund,
            "rule": rule,
        })

    return {
        "error": None,
        "pnr": pnr.get("pnr"),
        "travel_class": cls,
        "quota": pnr.get("quota_name"),
        "scheduled_departure": departs_at.isoformat(),
        "hours_to_departure": round(hours_to_departure, 2),
        # Exactly one of these is set. `time_to_departure` names a duration, so
        # it must not carry "train has departed" — anything formatting it into
        # a sentence would produce "... to departure" nonsense.
        "time_to_departure": None if seconds_to_departure < 0 else _humanise(seconds_to_departure),
        "time_since_departure": _humanise(seconds_to_departure) if seconds_to_departure < 0 else None,
        "has_departed": seconds_to_departure < 0,
        "flat_minimum_charge": flat_minimum,
        "total_fare": pnr.get("ticket_fare"),
        "total_refund": round(total, 2),
        "passengers": breakdown,
        "rules_version": RULES_VERSION,
        "disclaimer": (
            "Estimate only. Excludes GST on the cancellation charge, IRCTC "
            "convenience fees and any agent charges. Final amount is determined "
            "by IRCTC."
        ),
    }
