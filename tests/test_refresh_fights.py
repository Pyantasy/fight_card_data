import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import refresh_fights as rf  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIXTURES / name).read_text())


class ParseTests(unittest.TestCase):
    def test_event_list(self):
        events, has_more = rf.parse_event_list(load("events_list.json"))
        self.assertEqual(len(events), 4)
        self.assertFalse(has_more)

    def test_event_list_missing_field_raises(self):
        with self.assertRaises(rf.ParseError):
            rf.parse_event_list({"data": [{"id": 1, "title": "x"}], "meta": {}})

    def test_selection_skips_cancelled_and_caps(self):
        events, _ = rf.parse_event_list(load("events_list.json"))
        chosen = rf.select_events_for_cards(events, rf.FIXTURE_TODAY, 10)
        self.assertEqual([e["id"] for e in chosen], [9101, 9102, 9104])
        capped = rf.select_events_for_cards(events, rf.FIXTURE_TODAY, 2)
        self.assertEqual([e["id"] for e in capped], [9101, 9102])

    def test_completed_card(self):
        event, fights, skipped = rf.parse_event_detail(load("event_9101.json"), NOW)
        self.assertEqual(event["event_date"], "2026-11-14")  # 01:00Z is the evening before in Las Vegas
        self.assertEqual(event["venue_city"], "Las Vegas")
        self.assertEqual(skipped, 1)  # bout with a TBA corner
        self.assertEqual(len(fights), 4)
        main = fights[0]
        self.assertEqual(main["fighter_1"], "Alex Sample")
        self.assertEqual(main["winner"], "Alex Sample")
        self.assertEqual(main["result_method"], "KO/TKO")
        self.assertEqual(main["result_method_norm"], "ko_tko")
        self.assertEqual(main["result_round"], 3)
        self.assertEqual(main["scheduled_rounds"], 5)
        self.assertEqual(main["main_or_prelim"], "main")
        self.assertEqual(main["weightclass"], "Welterweight")
        self.assertEqual([f["main_or_prelim"] for f in fights], ["main", "main", "prelim", "prelim"])
        draw = fights[3]
        self.assertIsNone(draw["winner"])

    def test_upcoming_card_flags(self):
        event, fights, skipped = rf.parse_event_detail(load("event_9102.json"), NOW)
        self.assertTrue(event["is_ppv"])
        self.assertEqual(skipped, 1)
        title = fights[0]
        self.assertTrue(title["is_championship"])
        self.assertEqual(title["scheduled_rounds"], 5)
        self.assertIsNone(title["result_method"])
        self.assertFalse(fights[1]["is_championship"])

    def test_missing_section_falls_back_on_ordering(self):
        self.assertEqual(rf.section_to_main_or_prelim(None, False, 6), "prelim")
        self.assertEqual(rf.section_to_main_or_prelim(None, False, 3), "main")
        self.assertEqual(rf.section_to_main_or_prelim(None, True, 9), "main")


class BudgetTests(unittest.TestCase):
    def test_cap_enforced(self):
        budget = rf.Budget(max_requests=2)
        budget.spend()
        budget.spend()
        with self.assertRaises(rf.BudgetExceeded):
            budget.spend()

    def test_low_quota_stops(self):
        budget = rf.Budget(max_requests=10, quota_remaining=rf.QUOTA_RESERVE - 1)
        with self.assertRaises(rf.QuotaLow):
            budget.spend()

    def test_collect_counts_requests(self):
        budget = rf.Budget(max_requests=10)
        summary = rf.collect(rf.FixtureClient(budget), budget, rf.FIXTURE_TODAY, 10, 28, NOW)
        self.assertEqual(summary.requests_used, 4)  # 1 list + 3 cards
        self.assertEqual(rf.validate(summary), [])

    def test_cap_limits_cards_fetched(self):
        budget = rf.Budget(max_requests=3)
        summary = rf.collect(rf.FixtureClient(budget), budget, rf.FIXTURE_TODAY, 10, 28, NOW)
        self.assertEqual(summary.requests_used, 3)  # 1 list + the 2 cards the cap allows
        self.assertEqual(len(summary.events), 2)

    def test_low_quota_after_list_call_aborts_before_any_card(self):
        class LowQuotaClient(rf.FixtureClient):
            def get(self, path, params):
                body = super().get(path, params)
                self._budget.quota_remaining = rf.QUOTA_RESERVE - 1
                return body

        budget = rf.Budget(max_requests=10)
        with self.assertRaises(rf.QuotaLow):
            rf.collect(LowQuotaClient(budget), budget, rf.FIXTURE_TODAY, 10, 28, NOW)
        self.assertEqual(budget.used, 1)


class OutputTests(unittest.TestCase):
    def collect_fights(self):
        budget = rf.Budget(max_requests=10)
        summary = rf.collect(rf.FixtureClient(budget), budget, rf.FIXTURE_TODAY, 10, 28, NOW)
        return rf.build_fights(summary)

    def test_row_shape_matches_what_the_app_decodes(self):
        fights = self.collect_fights()
        self.assertEqual(len(fights), 10)
        expected_keys = {
            "id", "date", "promotion", "event", "main_or_prelim", "card_placement",
            "fighter_1", "fighter_2", "weightclass", "rounds", "is_championship",
        }
        for row in fights:
            self.assertEqual(set(row), expected_keys)
            self.assertIsInstance(row["id"], int)
            self.assertRegex(row["date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertIn(row["main_or_prelim"], {"main", "prelim"})
            self.assertIsInstance(row["card_placement"], str)
            self.assertIsInstance(row["rounds"], int)
            self.assertIn(row["is_championship"], {0, 1})
            self.assertEqual(row["promotion"], "UFC")

    def test_main_event_row(self):
        main = self.collect_fights()[0]
        self.assertEqual(main["date"], "2026-11-14")
        self.assertEqual(main["id"], 301)
        self.assertEqual(main["fighter_1"], "Alex Sample")
        self.assertEqual(main["card_placement"], "1")
        self.assertEqual(main["rounds"], 5)
        self.assertEqual(main["weightclass"], "Welterweight")

    def test_sorted_by_date_then_main_card_first(self):
        fights = self.collect_fights()
        self.assertEqual([f["date"] for f in fights], sorted(f["date"] for f in fights))
        for event in {f["event"] for f in fights}:
            kinds = [f["main_or_prelim"] for f in fights if f["event"] == event]
            self.assertEqual(kinds, sorted(kinds, key=lambda k: k != "main"))

    def test_championship_flag(self):
        title = [f for f in self.collect_fights() if f["is_championship"]]
        self.assertEqual(len(title), 1)

    def test_cancelled_bouts_dropped(self):
        budget = rf.Budget(max_requests=10)
        summary = rf.collect(rf.FixtureClient(budget), budget, rf.FIXTURE_TODAY, 10, 28, NOW)
        before = len(rf.build_fights(summary))
        summary.fights[0]["status"] = "cancelled"
        self.assertEqual(len(rf.build_fights(summary)), before - 1)

    def test_missing_rounds_and_weightclass_use_old_defaults(self):
        budget = rf.Budget(max_requests=10)
        summary = rf.collect(rf.FixtureClient(budget), budget, rf.FIXTURE_TODAY, 10, 28, NOW)
        summary.fights[0]["scheduled_rounds"] = None
        summary.fights[0]["weightclass"] = None
        row = rf.to_app_fight(summary.events[0], summary.fights[0])
        self.assertEqual(row["rounds"], 3)
        self.assertEqual(row["weightclass"], "")

    def test_payload_envelope(self):
        fights = self.collect_fights()
        payload = rf.build_payload(fights, NOW)
        self.assertEqual(set(payload), {"status", "count", "fights", "timestamp"})
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["count"], len(fights))

    def test_write_skips_unchanged_fights(self):
        fights = self.collect_fights()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "fights.json"
            self.assertTrue(rf.write_fights_json(path, fights, NOW))
            first = path.read_text()
            later = datetime(2026, 12, 3, 12, 0, tzinfo=timezone.utc)
            self.assertFalse(rf.write_fights_json(path, fights, later))
            self.assertEqual(path.read_text(), first)  # byte-identical, so git sees no change
            changed = [dict(fights[0], fighter_2="Someone Else")] + fights[1:]
            self.assertTrue(rf.write_fights_json(path, changed, later))
            self.assertEqual(json.loads(path.read_text())["timestamp"], "2026-12-03T12:00:00+00:00")


class MainTests(unittest.TestCase):
    def test_low_quota_exits_without_writing(self):
        class LowQuotaClient(rf.FixtureClient):
            def get(self, path, params):
                body = super().get(path, params)
                self._budget.quota_remaining = 3
                return body

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"UFCALENDAR_API_KEY": "test-key-not-real"}
        ), mock.patch.object(rf, "UfcalendarClient", lambda key, budget, raw: LowQuotaClient(budget)):
            out = Path(tmp) / "fights.json"
            self.assertEqual(rf.main(["--output", str(out)]), 3)
            self.assertFalse(out.exists())

    def test_max_requests_is_clamped_to_ten(self):
        with mock.patch.object(rf, "collect", side_effect=rf.BudgetExceeded("x")) as collect, mock.patch.dict(
            "os.environ", {"UFCALENDAR_API_KEY": "test-key-not-real"}
        ):
            rf.main(["--max-requests", "50"])
            self.assertEqual(collect.call_args.args[1].max_requests, 10)

    def test_live_run_writes_file_with_no_secret_in_it(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"UFCALENDAR_API_KEY": "test-key-not-real"}
        ), mock.patch.object(rf, "UfcalendarClient", lambda key, budget, raw: rf.FixtureClient(budget)):
            out = Path(tmp) / "fights.json"
            self.assertEqual(rf.main(["--output", str(out)]), 0)
            self.assertNotIn("test-key-not-real", out.read_text())
            self.assertEqual(json.loads(out.read_text())["count"], 10)


class DryRunTests(unittest.TestCase):
    def test_dry_run_needs_no_network_or_secrets(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network used")
        ):
            self.assertEqual(rf.main(["--dry-run", "--output", "/nonexistent/should-not-be-written.json"]), 0)

    def test_live_mode_without_key_fails_cleanly(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(rf.main([]), 2)


if __name__ == "__main__":
    unittest.main()
