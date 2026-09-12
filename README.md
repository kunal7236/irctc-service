# irctc-service

A clean service layer over the four upstreams behind your Flutter app and
Telegram bot. Each function returns plain, meaningful JSON — the MCP server
consumes these directly and does no parsing of its own.

## Why this exists

The upstreams are awkward in different ways: erail returns 72-field
`~`-delimited text with no header, confirmtkt returns deeply nested JSON with
its own station resolution, and the PNR endpoint wants an AES-encrypted body
and answers HTTP 200 on failure. This package absorbs all of that.

## Services

| Function | Upstream | Cached |
|---|---|---|
| `search_trains(from, to, date=None)` | erail `getTrains.aspx` | 1h |
| `train_details(train_number)` | erail `getTrains.aspx` | 6h |
| `train_route(train_number)` | erail `data.aspx` (+details for the id) | 24h |
| `coach_position(train_number)` | erail `getTrains.aspx` | 6h |
| `check_availability(from, to, date, quota=None)` | confirmtkt | never |
| `pnr_status(pnr)` | easemytrip | never |
| `refund_estimate(pnr)` | *no API — pure calculation* | never |
| `find_station(query)` | local `stations.json` | n/a |
| `find_train(query)` | local `trains.json` | n/a |

Schedule data is cached because it changes rarely and erail will rate-limit a
chatty caller. Availability, fares and PNR are never cached — a stale seat
count is the one answer people act on immediately.

## Install

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

`requests` and `cryptography` are needed to reach the upstreams, `fastmcp` for
the MCP server. Every other module is pure stdlib — `irctc/__init__` imports
`service` lazily so the parsers and their tests run with neither installed.
There is a test that enforces this.

## Test

```bash
python3 -m unittest discover -s tests -v
```

57 tests run against real captured payloads in `tests/fixtures/` — no network.

## Where the field maps came from

`constants.py` is not reverse-engineered. The index constants, class order and
quota order are lifted from erail's own client bundle
(`https://erail.in/js5/cmp/eRailMobileHTMLEN.js`), which declares
`T_No=0, T_Name=1, …`, `n_Classes2="1A 2A 3A CC FC SL 2S 3E GN EA EC EV VC VS"`
and `d_Quota="GN TQ PT LD SS FT HP YU DP PH"`. They were then verified against
155 real records.

## Things worth knowing

**Nearby-station expansion.** Both erail and confirmtkt silently widen a query
— ask for NDLS→BCT and you get trains from NZM, DEE and DEC to BDTS, PNVL and
CSMT. Every response carries the *actual* resolved codes plus a
`nearby_expanded` flag. Do not drop those fields: without them an agent will
tell someone their train leaves from New Delhi when it leaves from
Nizamuddin, 8 km away.

**Three disagreeing sources for a train's classes.** The class bitmask (field
21), the seat block (62) and the fare block (41) disagree on 9 of 155 captured
records. The parser prefers the seat block and sets `class_sources_disagree`
rather than silently picking one.

**Undecoded fields are kept, not dropped.** `raw` holds the ones erail's own
public bundle never consumes — `flags` (58), the class bitmask, `status`,
`route_id`. The six sub-values inside each fare group are likewise exposed
verbatim under `fares.by_class` instead of guessed at. Strip `raw` before
handing a payload to a model; keep it in the service layer.

**Refund rules are a data table, not logic.** `refund.RULES` implements the
rules effective 1 April 2026: 72 / 24 / 8 hour slabs, with per-class flat
charges of ₹240 (1A/EC) / 200 (2A/FC) / 180 (3A/3E/CC) / 120 (SL) / 60 (2S) and
₹60 clerkage on RAC and waitlisted tickets. Tatkal, RAC and waitlist handling
was not altered by that revision. The previous structure was 48 / 12 / 4. If
the slabs change again, edit `RULES` and bump `RULES_VERSION`; no logic
changes.

## Two bugs this fixes from the existing Telegram bot

1. `calculate_refund_text` keyed the flat cancellation charge off `className`
   (`"AC 3 Tier"`), which never matched entries like `"3A"` — so every class
   silently fell through to the default. The code is `journeyClass`.
2. `get_departure_time` fuzzy-matched `BrdPointName` against route station
   names with substring matching and `" jn"` stripping. The PNR payload's
   `boardingPoint` is already a station code that joins to a route stop
   exactly.

The bot's slabs (48 / 6 / −2h) and charges (120 / 100 / 90 / 60 / 30) are both
stale — the current rules are 72 / 24 / 8 with charges of 240 / 200 / 180 /
120 / 60.

## MCP server

`mcp_server.py` exposes all nine services as MCP tools over Streamable HTTP,
authenticated with a static token — sent either as an `Authorization: Bearer`
header, or as a URL path segment (`/mcp/<token>`) for clients whose UI has no
header field. See **MCP_SETUP.md** for local
testing on the Mac and deployment to the Pi at `irctcmcp.kunalka.me`.

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env        # set IRCTC_MCP_TOKEN
set -a && . ./.env && set +a
./venv/bin/python mcp_server.py          # http://127.0.0.1:8100/mcp
```

## Layout

```
irctc/
  constants.py   field maps from erail's own bundle
  erail.py       pure parsers for both ~-delimited endpoints
  confirmtkt.py  pure normalizer for the availability search
  pnr.py         PNR encryption + response normalizer
  refund.py      IRCTC cancellation ladder (pure calculation)
  stations.py    local name -> code resolution
  cache.py       TTL cache
  service.py     fetch + normalize; the MCP calls these
  data/          stations.json (8.5k), trains.json (7.5k)
mcp_server.py    9 MCP tools over Streamable HTTP
probe.py         call one tool from the CLI (local or deployed)
tests/
  test_parsers.py  57 unit tests, no network
  e2e_mcp.py       end-to-end run through a real MCP client
  fixtures/        real captured payloads
```
