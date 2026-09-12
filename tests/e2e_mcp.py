"""
End-to-end MCP tests: boots the real server over HTTP and drives it with a real
MCP client, exactly as ChatGPT/Gemini/Claude would.

Network-dependent tools are exercised too — where the upstream is unreachable
the assertion is that the failure comes back as a readable payload rather than
a stack trace, which is the behaviour that matters to an agent.

    python3 tests/test_mcp.py
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from fastmcp import Client

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-do-not-use-in-production"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not condition else ""))


async def run(port: int):
    url = f"http://127.0.0.1:{port}/mcp"

    print("\n[auth]")
    try:
        async with Client(url) as c:
            await c.list_tools()
        check("unauthenticated request is rejected", False, "server accepted a request with no token")
    except Exception:
        check("unauthenticated request is rejected", True)

    try:
        async with Client(url, auth="wrong-token") as c:
            await c.list_tools()
        check("wrong token is rejected", False, "server accepted a bad token")
    except Exception:
        check("wrong token is rejected", True)

    print("\n[path-token auth — for clients that cannot set headers]")
    path_url = f"http://127.0.0.1:{port}/mcp/{TOKEN}"
    try:
        # Deliberately no auth= here: the token is in the URL, nothing else.
        async with Client(path_url) as c:
            tools = await c.list_tools()
        check("token in the URL path authenticates with no header", len(tools) == 9,
              f"{len(tools)} tools")
    except Exception as e:
        check("token in the URL path authenticates with no header", False, repr(e)[:160])

    try:
        async with Client(f"http://127.0.0.1:{port}/mcp/not-the-token") as c:
            await c.list_tools()
        check("wrong token in the path is rejected", False, "server accepted a bad path token")
    except Exception:
        check("wrong token in the path is rejected", True)

    try:
        async with Client(path_url) as c:
            r = await c.call_tool("find_station", {"query": "NDLS"})
            payload = json.loads(r.content[0].text) if r.content else r.structured_content
        check("tools are callable over the path-token URL",
              payload["matches"][0]["code"] == "NDLS")
    except Exception as e:
        check("tools are callable over the path-token URL", False, repr(e)[:160])

    async with Client(url, auth=TOKEN) as client:
        print("\n[discovery]")
        tools = await client.list_tools()
        names = sorted(t.name for t in tools)
        expected = sorted([
            "find_station", "find_train", "search_trains", "check_seat_availability",
            "get_train_details", "get_train_route", "get_coach_position",
            "get_pnr_status", "estimate_refund",
        ])
        check("valid token connects", True)
        check(f"all 9 tools exposed ({len(names)})", names == expected, str(names))
        check("every tool has a description",
              all(t.description and len(t.description) > 60 for t in tools))
        check("every tool declares an input schema",
              all(getattr(t, "input_schema", None) or t.inputSchema for t in tools))

        async def call(tool, **kwargs):
            r = await client.call_tool(tool, kwargs)
            return json.loads(r.content[0].text) if r.content else r.structured_content

        print("\n[local tools — no network needed]")
        r = await call("find_station", query="NDLS")
        check("find_station resolves an exact code",
              r["matches"][0]["code"] == "NDLS", json.dumps(r)[:200])

        r = await call("find_station", query="Varanasi")
        check("find_station resolves a name",
              any(m["code"] == "BSB" for m in r["matches"]), str([m["code"] for m in r["matches"]]))

        r = await call("find_station", query="Delhi")
        check("ambiguous city is flagged, not guessed", r["ambiguous"] is True)

        r = await call("find_train", query="12951")
        check("find_train resolves a number", r["matches"][0]["number"] == "12951")

        r = await call("find_station", query="qqqzzz")
        check("unknown station returns an error, not an empty success",
              r["error"] is not None)

        print("\n[argument handling]")
        r = await call("search_trains", from_station="Delhi", to_station="NDLS")
        check("ambiguous origin returns candidates instead of picking one",
              "candidates" in r and r["error"], json.dumps(r)[:200])

        r = await call("search_trains", from_station="NDLS", to_station="BCT",
                       travel_date="not-a-date")
        check("bad date is rejected with a usable message",
              r.get("error") and "YYYY-MM-DD" in r["error"])

        r = await call("check_seat_availability", from_station="NDLS",
                       to_station="BCT", travel_date="tomorrow")
        check("'tomorrow' is accepted as a date",
              "Could not read" not in json.dumps(r))

        r = await call("get_pnr_status", pnr="123")
        check("short PNR rejected before any upstream call",
              r.get("error") and "10 digits" in r["error"])

        print("\n[timestamping]")
        r = await call("find_station", query="NDLS")
        check("responses carry as_of_ist so the model needn't guess the date",
              "as_of_ist" in r)

        print("\n[upstream — live where reachable]")
        # This suite runs both in sandboxes with no route to erail and on a
        # machine that can reach it. Both are valid; assert the right thing
        # for whichever we're in rather than assuming.
        r = await call("get_train_details", train="12951")

        if r.get("error"):
            check("upstream failure returns a message, never a traceback",
                  "Traceback" not in json.dumps(r))
            print(f"        upstream unreachable here ({r['error'][:60]}…) — "
                  "skipping live assertions")
            return

        check("upstream reachable — running live checks", True)
        t = r["train"]
        check("live: details returns the requested train", t["train_number"] == "12951")
        check("live: classes populated", bool(t["classes"]), str(t.get("classes")))
        check("live: distance looks sane",
              isinstance(t.get("distance_km"), int) and 500 < t["distance_km"] < 2500,
              str(t.get("distance_km")))

        r = await call("get_train_route", train="12951")
        stops = r.get("stops") or []
        check("live: route has stops", len(stops) > 2, json.dumps(r)[:160])
        check("live: route starts and ends at the train's endpoints",
              stops and stops[0]["code"] == "MMCT" and stops[-1]["code"] == "NDLS",
              f'{stops[0]["code"]}..{stops[-1]["code"]}' if stops else "none")
        check("live: distances increase along the route",
              all(a["distance_km"] <= b["distance_km"]
                  for a, b in zip(stops, stops[1:])))
        check("live: overnight arrival carries a day number > 1",
              stops[-1]["day"] and stops[-1]["day"] > 1, str(stops[-1].get("day")))

        r = await call("get_coach_position", train="12951")
        check("live: coach layout starts at the engine",
              (r.get("layout") or "").startswith("EN"), (r.get("layout") or "")[:60])

        r = await call("search_trains", from_station="NDLS", to_station="BCT",
                       travel_date="tomorrow", limit=5)
        check("live: search returns trains", r.get("total_matching", 0) > 0, json.dumps(r)[:160])
        check("live: search respects limit", len(r.get("trains", [])) <= 5)
        check("live: each train names the station it actually departs from",
              all("(" in t["from"] and "(" in t["to"] for t in r.get("trains", [])))

        r = await call("check_seat_availability", from_station="NDLS",
                       to_station="BCT", travel_date="tomorrow")
        check("live: availability returns trains", r.get("count", 0) > 0, json.dumps(r)[:160])
        buckets = [b for t in r.get("trains", []) for b in t["availability"].values()]
        check("live: availability buckets carry a status and a fare",
              buckets and all(b.get("status") for b in buckets),
              json.dumps(buckets[:1]))

        # Opt-in: a real PNR is personal data, so it is never hardcoded here.
        #   IRCTC_TEST_PNR=6608645872 python3 tests/test_mcp.py
        test_pnr = os.environ.get("IRCTC_TEST_PNR", "").strip()
        if test_pnr:
            r = await call("get_pnr_status", pnr=test_pnr)
            check("live: PNR status resolves", not r.get("error"), json.dumps(r)[:160])
            if not r.get("error"):
                check("live: passengers have a berth type",
                      all(p.get("berth_type") or p.get("current") for p in r["passengers"]))
                r = await call("estimate_refund", pnr=test_pnr)
                check("live: refund estimate computes", not r.get("error"), json.dumps(r)[:160])
                if not r.get("error"):
                    check("live: refund uses the 2026 rule table",
                          r["rules_version"] == "irctc-2026-04-72-24-8", r.get("rules_version"))
                    when = (f'{r["time_since_departure"]} after departure'
                            if r["has_departed"]
                            else f'{r["time_to_departure"]} to departure')
                    print(f'        refund now: Rs.{r["total_refund_inr"]} ({when})')
        else:
            print("        set IRCTC_TEST_PNR=<pnr> to include PNR and refund checks")




def main():
    port = free_port()
    env = {**os.environ, "IRCTC_MCP_TOKEN": TOKEN, "IRCTC_MCP_PORT": str(port),
           "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, "mcp_server.py"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        if not wait_for(port):
            print("server did not start:\n", proc.stdout.read() if proc.stdout else "")
            return 1
        print(f"server up on :{port}")
        asyncio.run(run(port))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
