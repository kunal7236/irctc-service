"""
irctc — a clean service layer over the IRCTC-adjacent endpoints.

Turns four upstreams that speak `~`-delimited text, nested JSON and AES-wrapped
POST bodies into one set of plain, meaningful dicts.

Layout:
    constants.py   field maps, taken from erail's own client bundle
    erail.py       pure parsers for the two `~`-delimited endpoints
    confirmtkt.py  pure normalizer for the availability search
    pnr.py         PNR encryption + response normalizer
    refund.py      IRCTC cancellation ladder (no API — pure calculation)
    stations.py    local station/train name -> code resolution
    cache.py       TTL cache: schedule cached, availability never
    service.py     fetch + normalize; this is what the MCP server calls

Everything except `service.py` is pure and importable without network access,
so the parsers can be tested against captured payloads.
"""

# `service` is imported lazily. It needs `requests`, and the parsers do not —
# keeping the import deferred means the pure modules (erail, confirmtkt, pnr,
# refund, stations, constants) and their tests run with stdlib alone.
_LAZY = {
    "search_trains", "train_details", "train_route", "coach_position",
    "check_availability", "pnr_status", "refund_estimate",
    "find_station", "find_train",
    "live_status", "station_board", "train_instances", "train_exceptions",
}

__all__ = sorted(_LAZY)
__version__ = "0.1.0"


def __getattr__(name: str):
    if name in _LAZY:
        from . import service
        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | _LAZY)
