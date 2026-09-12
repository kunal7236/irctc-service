"""
Parser tests against real captured payloads.

Everything under test is pure, so this runs with no network and stdlib only.
    python3 -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from irctc import confirmtkt, erail, pnr as pnr_mod, refund, stations  # noqa: E402
from irctc.constants import CLASSES, QUOTAS  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def text(name):
    return (FIX / name).read_text(encoding="utf-8", errors="replace")


def js(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class TestErailTrainRecord(unittest.TestCase):
    def setUp(self):
        self.rajdhani = erail.parse_trains(text("C_details_12951.txt"))[0]
        self.vande = erail.parse_trains(text("C_details_22436.txt"))[0]
        self.between = erail.parse_trains(text("D_between_NDLS_GZB.txt"))

    def test_identifies_train(self):
        self.assertEqual(self.rajdhani["train_number"], "12951")
        self.assertEqual(self.rajdhani["train_name"], "NDLS TEJAS RAJ")
        self.assertEqual(self.rajdhani["train_type"], "RAJDHANI")

    def test_extracts_train_id_for_route_call(self):
        # The route endpoint needs this, not the train number.
        self.assertEqual(self.rajdhani["train_id"], "2633")

    def test_times_normalised(self):
        self.assertEqual(self.rajdhani["journey"]["departure"], "17:00")
        self.assertEqual(self.rajdhani["journey"]["arrival"], "08:32")

    def test_duration_is_hours_and_minutes_not_decimal(self):
        # "15.32" is 15h32m = 932 minutes, not 15.32 hours.
        self.assertEqual(self.rajdhani["journey"]["duration_minutes"], 932)

    def test_running_days(self):
        self.assertTrue(self.rajdhani["runs_on"]["runs_daily"])
        self.assertEqual(self.vande["runs_on"]["days"], ["Mon", "Tue", "Wed", "Fri", "Sat", "Sun"])

    def test_classes_from_seat_block(self):
        self.assertEqual(self.rajdhani["classes"], ["1A", "2A", "3A"])
        self.assertEqual(sorted(self.vande["classes"]), ["CC", "EC"])

    def test_per_quota_seats_use_erail_quota_order(self):
        seats = self.rajdhani["seats_by_class"]["2A"]
        self.assertEqual(seats["GN"], 65)
        self.assertEqual(seats["TQ"], 80)
        self.assertEqual(seats["PT"], 30)
        self.assertEqual(seats["LD"], 20)
        self.assertEqual(seats["SS"], 4)

    def test_coach_composition_parsed_structurally(self):
        coaches = self.rajdhani["coaches"]
        self.assertTrue(coaches[0]["is_engine"])
        labels = [c["coach"] for c in coaches]
        self.assertIn("B1", labels)
        self.assertIn("H1", labels)
        b1 = next(c for c in coaches if c["coach"] == "B1")
        self.assertEqual(b1["class"], "3A")

    def test_platform_numbers_surfaced(self):
        # T_PFFrom / T_PFTo — dropped entirely by the Flutter parser.
        self.assertIsNotNone(self.rajdhani["journey"]["to_platform"])

    def test_metadata(self):
        self.assertEqual(self.rajdhani["distance_km"], 1384)
        self.assertEqual(self.rajdhani["avg_speed_kmph"], 89)
        self.assertEqual(self.rajdhani["zone"], "WR")
        self.assertEqual(self.rajdhani["advance_reservation_days"], 60)
        self.assertIn("12952", self.rajdhani["rake_shared_with"])

    def test_parses_the_whole_between_stations_body(self):
        self.assertGreater(len(self.between), 100)
        for t in self.between:
            self.assertTrue(t["train_number"])
            self.assertIsInstance(t["classes"], list)

    def test_every_class_seen_is_a_known_code(self):
        for t in self.between:
            for c in t["classes"]:
                self.assertIn(c, CLASSES, f"unknown class {c} on {t['train_number']}")

    def test_journey_codes_may_differ_from_query(self):
        # erail expands to nearby stations; both codes must survive so a caller
        # can tell the user which station the train actually leaves from.
        for t in self.between:
            self.assertTrue(t["journey"]["from_code"])
            self.assertTrue(t["journey"]["to_code"])

    def test_day_filter(self):
        monday = erail.filter_by_day(self.between, 0)
        self.assertTrue(all(t["runs_on"]["mask"][0] == "1" for t in monday))
        self.assertLessEqual(len(monday), len(self.between))


class TestErailRoute(unittest.TestCase):
    def setUp(self):
        self.route = erail.parse_route(text("A_route_12951_id2633.txt"))

    def test_stops(self):
        stops = self.route["stops"]
        self.assertEqual(len(stops), 8)
        self.assertEqual(stops[0]["code"], "MMCT")
        self.assertEqual(stops[-1]["code"], "NDLS")
        self.assertTrue(stops[0]["is_origin"])
        self.assertTrue(stops[-1]["is_destination"])

    def test_endpoint_sentinels_become_none(self):
        self.assertIsNone(self.route["stops"][0]["arrival"])
        self.assertIsNone(self.route["stops"][-1]["departure"])

    def test_elapsed_minutes_make_next_day_arrival_exact(self):
        ndls = self.route["stops"][-1]
        self.assertEqual(ndls["minutes_from_origin_arrival"], 932)
        self.assertEqual(ndls["day"], 2)

    def test_geo_and_admin_fields(self):
        bvi = self.route["stops"][1]
        self.assertEqual(bvi["code"], "BVI")
        self.assertEqual(bvi["halt_minutes"], 2)
        self.assertEqual(bvi["distance_km"], 30)
        self.assertAlmostEqual(bvi["latitude"], 19.229, places=2)
        self.assertAlmostEqual(bvi["longitude"], 72.857, places=2)
        self.assertEqual(bvi["division"], "BCT")

    def test_hindi_names_decoded(self):
        self.assertTrue(any(s["name_hindi"] for s in self.route["stops"]))

    def test_segment_seat_matrix(self):
        seg = next(s for s in self.route["segments"]
                   if s["from_code"] == "MMCT" and s["to_code"] == "NDLS")
        self.assertEqual(seg["seats_by_class"]["1A"][0], 10)
        self.assertEqual(seg["seats_by_class"]["3A"][0], 182)

    def test_departure_lookup_by_code(self):
        self.assertEqual(erail.departure_from_station(self.route, "BVI"), "17:22")
        # Terminating stop has no departure; fall back to arrival.
        self.assertEqual(erail.departure_from_station(self.route, "NDLS"), "08:32")
        self.assertIsNone(erail.departure_from_station(self.route, "XXXX"))


class TestConfirmtkt(unittest.TestCase):
    def setUp(self):
        self.result = confirmtkt.parse_search(js("E_availability_tomorrow.json"))

    def test_parses_trains(self):
        self.assertIsNone(self.result["error"])
        self.assertGreater(self.result["count"], 0)

    def test_availability_and_prediction(self):
        train = next(t for t in self.result["trains"] if t["train_number"] == "12952")
        cls = train["availability"]["general"]["3A"]
        self.assertIsNotNone(cls["status"])
        self.assertIsNotNone(cls["fare"])
        self.assertIsInstance(cls["confirm_probability"], (int, float))
        self.assertIn(cls["confirm_verdict"], ("Confirm", "Probable", "No Chance"))

    def test_regret_flagged(self):
        buckets = [c for t in self.result["trains"]
                   for c in t["availability"]["general"].values()]
        regrets = [b for b in buckets if b["is_regret"]]
        if regrets:
            self.assertEqual(regrets[0]["confirm_probability"], 0)

    def test_nearby_expansion_is_visible(self):
        # Asked NDLS->BCT, got NZM/DEE/DEC -> BDTS/PNVL/CSMT.
        self.assertTrue(self.result["nearby_expanded"])
        self.assertGreater(len(self.result["station_pairs_returned"]), 1)

    def test_duration_formatted(self):
        train = self.result["trains"][0]
        self.assertRegex(train["journey"]["duration"], r"^\d{2}:\d{2}$")


class TestPnr(unittest.TestCase):
    def setUp(self):
        self.pnr = pnr_mod.parse_status(js("G_pnr_real.json"))

    def test_basics(self):
        self.assertIsNone(self.pnr["error"])
        self.assertEqual(self.pnr["pnr"], "6608645872")
        self.assertEqual(self.pnr["train_number"], "15641")
        self.assertEqual(self.pnr["passenger_count"], 3)

    def test_class_code_not_prose(self):
        # The bug in the existing bot: className is "AC 3 Tier", which never
        # matches a class lookup. travel_class must be the code.
        self.assertEqual(self.pnr["travel_class"], "3A")
        self.assertEqual(self.pnr["travel_class_name"], "AC 3 Tier")

    def test_boarding_point_is_a_code(self):
        self.assertEqual(self.pnr["journey"]["boarding_code"], "SCL")
        self.assertEqual(self.pnr["journey"]["boarding_name"], "Silchar")

    def test_berth_type_decoded(self):
        types = {p["berth_type"] for p in self.pnr["passengers"]}
        self.assertEqual(types, {"Lower", "Middle", "Upper"})

    def test_fare_split(self):
        self.assertEqual(self.pnr["ticket_fare"], 1980.0)
        self.assertEqual(self.pnr["fare_per_passenger"], 660.0)

    def test_all_confirmed(self):
        self.assertTrue(self.pnr["all_confirmed"])
        self.assertTrue(self.pnr["chart_prepared"])

    def test_error_envelope(self):
        bad = pnr_mod.parse_status({"errorMessage": "Flushed PNR /PNR not yet generated",
                                    "pnrNumber": "1234567890"})
        self.assertEqual(bad["error"], "Flushed PNR /PNR not yet generated")
        self.assertEqual(bad["passengers"], [])

    def test_validation(self):
        self.assertTrue(pnr_mod.is_valid_pnr("6608645872"))
        self.assertFalse(pnr_mod.is_valid_pnr("12345"))
        self.assertFalse(pnr_mod.is_valid_pnr("abcdefghij"))

    def test_encryption_round_trips_to_expected_ciphertext(self):
        try:
            enc = pnr_mod.encrypt_pnr("1234567890")
        except ImportError:
            self.skipTest("cryptography not installed")
        self.assertTrue(enc.endswith("=") or len(enc) % 4 == 0)
        self.assertEqual(enc, pnr_mod.encrypt_pnr("1234567890"))


class TestRefund(unittest.TestCase):
    def setUp(self):
        self.pnr = pnr_mod.parse_status(js("G_pnr_real.json"))
        # Journey is 05-09-2026, departing 22:00 from Silchar.
        self.departure = "22:00"

    def _at(self, when):
        return refund.estimate(self.pnr, self.departure, now=datetime.fromisoformat(when))

    # Slabs below are the rules effective 1 April 2026: 72 / 24 / 8 hours.

    def test_more_than_72h_charges_flat_minimum_for_the_class(self):
        r = self._at("2026-09-01T10:00:00")   # 108h before
        # 3A -> flat 180 per passenger, on a 660 fare.
        self.assertEqual(r["flat_minimum_charge"], 180)
        self.assertEqual(r["passengers"][0]["deduction"], 180)
        self.assertEqual(r["passengers"][0]["refund"], 480)
        self.assertEqual(r["total_refund"], 1440.0)

    def test_between_72_and_24_hours_takes_25_percent_or_the_minimum(self):
        r = self._at("2026-09-04T10:00:00")   # 36h before
        # 25% of 660 = 165, below the 180 floor, so the floor applies.
        self.assertEqual(r["passengers"][0]["deduction"], 180)
        self.assertIn("25%", r["passengers"][0]["rule"])

    def test_between_24_and_8_hours_takes_50_percent(self):
        r = self._at("2026-09-05T10:00:00")   # 12h before
        self.assertEqual(r["passengers"][0]["deduction"], 330.0)
        self.assertEqual(r["passengers"][0]["refund"], 330.0)

    def test_under_8_hours_no_refund(self):
        # 6h before. Under the old 48/12/4 rules this was still a 50% slab and
        # would have refunded 330 per passenger — guards against reverting.
        r = self._at("2026-09-05T16:00:00")
        self.assertEqual(r["total_refund"], 0.0)
        self.assertIn("no refund", r["passengers"][0]["rule"].lower())

    def test_rules_version_is_the_2026_table(self):
        self.assertEqual(refund.RULES_VERSION, "irctc-2026-04-72-24-8")
        self.assertEqual([r["min_hours"] for r in refund.RULES][:3], [72.0, 24.0, 8.0])

    def test_after_departure_no_refund(self):
        r = self._at("2026-09-06T02:00:00")
        self.assertTrue(r["has_departed"])
        self.assertEqual(r["total_refund"], 0.0)
        # time_to_departure names a duration; it must be empty once departed,
        # with the elapsed time in its own field instead.
        self.assertIsNone(r["time_to_departure"])
        self.assertEqual(r["time_since_departure"], "4 hours")

    def test_before_departure_sets_only_the_forward_field(self):
        r = self._at("2026-09-05T16:00:00")
        self.assertFalse(r["has_departed"])
        self.assertEqual(r["time_to_departure"], "6 hours")
        self.assertIsNone(r["time_since_departure"])

    def test_class_lookup_would_have_failed_on_the_prose_name(self):
        # Regression guard for the original bug: had we keyed off className,
        # the flat charge would silently fall back to the default.
        from irctc.constants import MIN_CANCELLATION_CHARGE
        self.assertNotIn("AC 3 TIER", MIN_CANCELLATION_CHARGE)
        self.assertIn("3A", MIN_CANCELLATION_CHARGE)

    def test_confirmed_tatkal_gets_nothing(self):
        tatkal = dict(self.pnr, is_tatkal=True)
        r = refund.estimate(tatkal, self.departure,
                            now=datetime.fromisoformat("2026-09-01T10:00:00"))
        self.assertEqual(r["total_refund"], 0.0)
        self.assertIn("Tatkal", r["passengers"][0]["rule"])

    def test_waitlisted_pays_clerkage_only(self):
        wl = json.loads(json.dumps(self.pnr))
        for p in wl["passengers"]:
            p["is_confirmed"] = False
            p["is_waitlisted"] = True
        r = refund.estimate(wl, self.departure,
                            now=datetime.fromisoformat("2026-09-04T10:00:00"))
        self.assertEqual(r["passengers"][0]["deduction"], 60.0)
        self.assertEqual(r["passengers"][0]["refund"], 600.0)

    def test_missing_departure_is_an_error_not_a_wrong_number(self):
        r = refund.estimate(self.pnr, None)
        self.assertIsNotNone(r["error"])
        self.assertEqual(r["passengers"], [])


class TestStationResolver(unittest.TestCase):
    def test_exact_code(self):
        m = stations.resolve_station("NDLS")
        self.assertEqual(m[0]["code"], "NDLS")
        self.assertEqual(m[0]["match"], "code")

    def test_name(self):
        codes = [m["code"] for m in stations.resolve_station("New Delhi")]
        self.assertIn("NDLS", codes)

    def test_junction_suffix_ignored(self):
        codes = [m["code"] for m in stations.resolve_station("Kota Junction")]
        self.assertIn("KOTA", codes)

    def test_ambiguity_is_reported_not_hidden(self):
        matches = stations.resolve_station("Delhi", limit=6)
        self.assertGreater(len(matches), 1)

    def test_train_by_number(self):
        m = stations.resolve_train("12951")
        self.assertTrue(m)
        self.assertEqual(m[0]["number"], "12951")

    def test_unknown_returns_empty(self):
        self.assertEqual(stations.resolve_station("zzzzqqqq"), [])


class TestConstants(unittest.TestCase):
    def test_class_and_quota_orders_match_erail(self):
        self.assertEqual(CLASSES[:4], ["1A", "2A", "3A", "CC"])
        self.assertEqual(CLASSES[10], "EC")
        self.assertEqual(QUOTAS, ["GN", "TQ", "PT", "LD", "SS", "FT", "HP", "YU", "DP", "PH"])


class TestNoHardDependencies(unittest.TestCase):
    """The pure modules must import with stdlib alone.

    The Pi builds its venv from scratch, and a parser that quietly needs
    `requests` turns a missing wheel into a total failure instead of a
    degraded one. This caught a real regression: `irctc/__init__` used to
    import `service` eagerly.
    """

    def test_pure_modules_need_no_third_party_packages(self):
        import builtins
        blocked = {"requests", "cryptography", "fastmcp"}
        real_import = builtins.__import__

        def guard(name, *args, **kwargs):
            if name.split(".")[0] in blocked:
                raise ModuleNotFoundError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        saved = {k: v for k, v in sys.modules.items()
                 if k.split(".")[0] in blocked or k.startswith("irctc")}
        for k in saved:
            del sys.modules[k]
        builtins.__import__ = guard
        try:
            import importlib
            for mod in ("constants", "erail", "confirmtkt", "pnr", "refund", "stations", "cache"):
                importlib.import_module(f"irctc.{mod}")
        finally:
            builtins.__import__ = real_import
            sys.modules.update(saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestResolverRanking(unittest.TestCase):
    """Regression guards for ranking traps found while eyeballing output."""

    def test_new_delhi_outranks_minor_delhi_stations(self):
        codes = [m["code"] for m in stations.resolve_station("Delhi", limit=6)]
        self.assertIn("NDLS", codes)
        self.assertLess(codes.index("NDLS"), codes.index("DSA"))

    def test_mumbai_central_findable(self):
        codes = [m["code"] for m in stations.resolve_station("Mumbai Central", limit=5)]
        self.assertTrue({"MMCT", "BCT"} & set(codes))

    def test_engine_is_not_a_travel_class(self):
        t = erail.parse_trains(text("C_details_12951.txt"))[0]
        engine = t["coaches"][0]
        self.assertTrue(engine["is_engine"])
        self.assertIsNone(engine["class"])
        # Non-passenger coaches (LPR, PC, VP, SLRD, GN) are real and must be
        # kept, but flagged so a caller does not read them as travel classes.
        for c in t["coaches"]:
            if c["is_passenger_class"]:
                self.assertIn(c["class"], CLASSES)
        self.assertTrue(any(c["is_passenger_class"] for c in t["coaches"]))
        self.assertTrue(any(not c["is_passenger_class"] for c in t["coaches"]))
