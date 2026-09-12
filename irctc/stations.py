"""
Station and train name -> code resolution.

Every upstream endpoint takes codes, but people say "Delhi to Bombay". There is
no API for this — the data ships as JSON alongside the package (lifted from the
Flutter app's assets), and lookup is local and instant.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA = Path(__file__).parent / "data"


def _norm(s: str) -> str:
    """Fold the noise that stops a name matching: junction suffixes, punctuation."""
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(
        r"\b(jn|junction|cantt|cantonment|city|central|terminus|terminal|"
        r"railway station|station|road|rd)\b",
        " ", s,
    )
    return re.sub(r"\s+", " ", s).strip()


@lru_cache(maxsize=1)
def _stations() -> list[dict[str, Any]]:
    raw = json.loads((_DATA / "stations.json").read_text(encoding="utf-8"))
    out = []
    for s in raw.get("stations", []):
        code = (s.get("stnCode") or "").strip().upper()
        name = (s.get("stnName") or "").strip()
        if not code:
            continue
        out.append({
            "code": code,
            "name": name,
            "city": (s.get("stnCity") or "").strip() or None,
            "_n": _norm(name),
            "_c": _norm(s.get("stnCity") or ""),
        })
    return out


@lru_cache(maxsize=1)
def _trains() -> list[dict[str, Any]]:
    raw = json.loads((_DATA / "trains.json").read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else next(
        (v for v in raw.values() if isinstance(v, list)), []
    )
    out = []
    for t in rows:
        if not isinstance(t, dict):
            continue
        # NB: this file's `number` key is a row index (1, 2, 3...), not the
        # train number — `trainno` is the real one. Do not fall back to `number`.
        number = str(t.get("trainno") or t.get("trainNumber") or t.get("trainNo") or "").strip()
        name = str(t.get("trainName") or t.get("name") or "").strip()
        if not number:
            continue
        out.append({
            "number": number,
            "name": name,
            "source": (t.get("source") or "").strip() or None,
            "destination": (t.get("dest") or "").strip() or None,
            "_n": _norm(name),
        })
    return out


def _score(query: str, target: str) -> float:
    """Rank candidates for a partial station or train name.

    Token containment is ranked above raw prefix on purpose. Searching "Delhi"
    with a prefix-first rule surfaces "Delhi Shahdara" and "Delhi Kishangnj"
    above "New Delhi", which is the wrong answer for essentially every real
    query. Comparing whole tokens and penalising extra ones fixes that.
    """
    if not query or not target:
        return 0.0
    if query == target:
        return 1.0

    q_tokens = query.split()
    t_tokens = target.split()
    if q_tokens and set(q_tokens) <= set(t_tokens):
        extra = len(t_tokens) - len(q_tokens)
        return max(0.75, 0.95 - 0.03 * extra)

    if target.startswith(query):
        return 0.74 - min(len(target) - len(query), 40) / 400
    if any(w.startswith(query) for w in t_tokens):
        return 0.6
    if query in target:
        return 0.45
    return 0.0


def resolve_station(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Resolve a station name, city or code to candidate codes, best first.

    Returns a list rather than one answer on purpose: "Delhi" is genuinely
    ambiguous across NDLS, DLI, NZM, DEE and DEC, and an agent should be able
    to say so instead of silently picking one.
    """
    q = (query or "").strip()
    if not q:
        return []

    # A bare code wins outright.
    if re.fullmatch(r"[A-Za-z]{2,5}", q):
        for s in _stations():
            if s["code"] == q.upper():
                return [{"code": s["code"], "name": s["name"], "city": s["city"],
                         "match": "code", "score": 1.0}]

    qn = _norm(q)
    scored = []
    for s in _stations():
        score = max(_score(qn, s["_n"]), _score(qn, s["_c"]) * 0.95)
        if score > 0:
            scored.append((score, s))

    scored.sort(key=lambda x: (-x[0], len(x[1]["name"])))
    return [
        {"code": s["code"], "name": s["name"], "city": s["city"],
         "match": "name", "score": round(score, 3)}
        for score, s in scored[:limit]
    ]


def resolve_train(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Resolve a train name or number to candidates, best first."""
    q = (query or "").strip()
    if not q:
        return []

    if re.fullmatch(r"\d{4,5}", q):
        exact = [t for t in _trains() if t["number"] == q]
        if exact:
            return [{"number": t["number"], "name": t["name"], "source": t["source"],
                     "destination": t["destination"], "match": "number", "score": 1.0}
                    for t in exact[:limit]]

    qn = _norm(q)
    scored = []
    for t in _trains():
        score = _score(qn, t["_n"])
        if t["number"].startswith(q):
            score = max(score, 0.85)
        if score > 0:
            scored.append((score, t))

    scored.sort(key=lambda x: (-x[0], len(x[1]["name"])))
    return [
        {"number": t["number"], "name": t["name"], "source": t["source"],
         "destination": t["destination"], "match": "name", "score": round(score, 3)}
        for score, t in scored[:limit]
    ]


def station_name(code: str) -> str | None:
    code = (code or "").strip().upper()
    for s in _stations():
        if s["code"] == code:
            return s["name"]
    return None
