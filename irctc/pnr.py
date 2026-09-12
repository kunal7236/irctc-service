"""
PNR status: request encryption and response normalization.

The upstream expects the PNR AES-CBC encrypted (key and IV are both the ASCII
string `8080808080808080`, PKCS7-padded, base64-encoded) and always answers
HTTP 200 — failures arrive as a populated `errorMessage` on an otherwise empty
envelope, so status codes tell you nothing.
"""

from __future__ import annotations

import re
from base64 import b64encode
from typing import Any

from .constants import CLASS_NAMES

_KEY_IV = b"8080808080808080"

BERTH_NAMES = {
    "LB": "Lower", "MB": "Middle", "UB": "Upper",
    "SL": "Side Lower", "SU": "Side Upper", "SM": "Side Middle",
    "WS": "Window Side", "CB": "Cabin", "CP": "Coupe",
}


def encrypt_pnr(pnr: str) -> str:
    """AES-128-CBC + PKCS7 + base64, as the upstream expects."""
    # Imported lazily so the pure normalizers stay importable without pycryptography.
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    data = padder.update(pnr.encode()) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(_KEY_IV), modes.CBC(_KEY_IV), backend=default_backend()
    ).encryptor()
    return b64encode(encryptor.update(data) + encryptor.finalize()).decode()


def is_valid_pnr(pnr: str) -> bool:
    return bool(re.fullmatch(r"\d{10}", (pnr or "").strip()))


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _passenger(p: dict, index: int) -> dict[str, Any]:
    current = " ".join((p.get("currentStatus") or "").split())
    booked = (p.get("bookingStatus") or "").strip()
    berth_code = (p.get("bookingBerthCode") or "").strip().upper()
    upper = current.upper()

    return {
        "number": int(p.get("passengerSerialNumber") or index + 1),
        "booking_status": booked or None,
        "current_status": current or None,
        "is_confirmed": "CNF" in upper or "CONFIRM" in upper,
        "is_rac": "RAC" in upper,
        # Covers plain WL plus every pool prefix: GNWL, RLWL, PQWL, RSWL, TQWL.
        "is_waitlisted": "WL" in upper,
        "is_cancelled": "CAN" in upper,
        "coach": p.get("currentCoachId") or p.get("bookingCoachId") or None,
        "berth_number": p.get("currentBerthNo") or p.get("bookingBerthNo") or None,
        # The part passengers actually care about, and which the existing
        # Telegram formatter drops.
        "berth_code": berth_code or None,
        "berth_type": BERTH_NAMES.get(berth_code),
        "booked_coach": p.get("bookingCoachId") or None,
        "booked_berth_number": p.get("bookingBerthNo") or None,
        "nationality": p.get("passengerNationality") or None,
    }


def parse_status(payload: dict) -> dict[str, Any]:
    """Raw upstream PNR payload -> clean dict.

    Note this free endpoint returns no passenger names, ages or genders — those
    fields exist in the schema but come back empty or "NA".
    """
    payload = payload or {}
    error = payload.get("errorMessage")
    if error:
        return {"error": error, "pnr": payload.get("pnrNumber"), "passengers": []}

    passengers = [_passenger(p, i) for i, p in enumerate(payload.get("passengerList") or [])]
    fare = _num(payload.get("ticketFare"))
    journey_class = (payload.get("journeyClass") or "").strip().upper()

    return {
        "error": None,
        "pnr": payload.get("pnrNumber"),
        "train_number": payload.get("trainNumber") or None,
        "train_name": payload.get("trainName") or None,

        "journey": {
            # `boardingPoint` is a CODE (e.g. SCL) — it joins directly onto an
            # erail route stop, which is what the refund calculation needs.
            "boarding_code": payload.get("boardingPoint") or None,
            "boarding_name": payload.get("BrdPointName") or None,
            "from_code": payload.get("sourceStation") or None,
            "from_name": payload.get("SrcStnName") or None,
            "to_code": payload.get("destinationStation") or None,
            "to_name": payload.get("DestStnName") or None,
            "reservation_upto_code": payload.get("reservationUpto") or None,
            "reservation_upto_name": payload.get("reservationUptoName") or None,
            "date": payload.get("dateOfJourney") or None,   # DD-MM-YYYY
        },

        # `journeyClass` is the CODE ("3A"); `className` is prose ("AC 3 Tier").
        # Anything doing a lookup by class must use the code.
        "travel_class": journey_class or None,
        "travel_class_name": payload.get("className") or CLASS_NAMES.get(journey_class),
        "quota": payload.get("quota") or None,
        "quota_name": payload.get("quotaName") or None,
        "is_tatkal": "TATKAL" in (payload.get("quotaName") or "").upper(),

        "chart_status": payload.get("chartStatus") or None,
        "chart_prepared": "PREPARED" in (payload.get("chartStatus") or "").upper()
                          and "NOT" not in (payload.get("chartStatus") or "").upper(),

        "ticket_fare": fare,
        "booking_fare": _num(payload.get("bookingFare")),
        "passenger_count": len(passengers),
        "fare_per_passenger": round(fare / len(passengers), 2) if fare and passengers else None,
        "overall_booking_status": payload.get("bookingStatus") or None,
        "all_confirmed": bool(passengers) and all(p["is_confirmed"] for p in passengers),

        "passengers": passengers,
    }
