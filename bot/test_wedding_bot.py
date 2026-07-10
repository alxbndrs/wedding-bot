#!/usr/bin/env python3
"""Unit tests for the TimeSelection parser and the poll/notify decision logic.

Run: cd bot && python3 -m unittest
No network and no Telegram calls happen — sends are monkeypatched.
"""

import datetime
import os
import tempfile
import unittest

import wedding_bot

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return fh.read()


class ParseDatesTest(unittest.TestCase):
    def test_real_no_slots_page_all_unavailable(self):
        dates = wedding_bot.parse_dates(_fixture("no_slots.html"))
        # The captured live page listed 16 dates, every one fully booked.
        self.assertEqual(len(dates), 16)
        self.assertTrue(all(not d["available"] for d in dates))
        self.assertEqual(dates[0]["name"], "Wednesday July 15ᵗʰ, 2026")

    def test_has_slots_page_detects_available_date(self):
        dates = wedding_bot.parse_dates(_fixture("has_slots.html"))
        self.assertEqual(len(dates), 3)
        available = [d for d in dates if d["available"]]
        self.assertEqual(len(available), 1)
        self.assertEqual(available[0]["name"], "Monday August 17ᵗʰ, 2026")
        # The two dates with the warning block stay unavailable.
        self.assertEqual(
            sorted(d["name"] for d in dates if not d["available"]),
            ["Tuesday August 18ᵗʰ, 2026", "Wednesday August 19ᵗʰ, 2026"],
        )

    def test_error_page_raises_scrape_error(self):
        with self.assertRaises(wedding_bot.ScrapeError):
            wedding_bot.parse_dates(_fixture("error_page.html"))

    def test_parse_date_name_handles_superscript_ordinals(self):
        cases = {
            "Wednesday July 15ᵗʰ, 2026": datetime.date(2026, 7, 15),
            "Tuesday July 21ˢᵗ, 2026": datetime.date(2026, 7, 21),
            "Thursday July 23ʳᵈ, 2026": datetime.date(2026, 7, 23),
            "Tuesday September 1ˢᵗ, 2026": datetime.date(2026, 9, 1),
            "Wednesday September 2ⁿᵈ, 2026": datetime.date(2026, 9, 2),
            "Monday August 17ᵗʰ, 2026": datetime.date(2026, 8, 17),
        }
        for name, expected in cases.items():
            self.assertEqual(wedding_bot.parse_date_name(name), expected, name)

    def test_parse_date_name_returns_none_when_unparseable(self):
        self.assertIsNone(wedding_bot.parse_date_name("Coming soon"))
        self.assertIsNone(wedding_bot.parse_date_name(""))

    def test_empty_page_raises_scrape_error(self):
        with self.assertRaises(wedding_bot.ScrapeError):
            wedding_bot.parse_dates("<html><body>nothing here</body></html>")


class PollLogicTest(unittest.TestCase):
    def setUp(self):
        self._sent = []
        self._calls = 0
        self._orig_send = wedding_bot.send_telegram
        self._orig_call = wedding_bot.place_call
        self._orig_fetch = wedding_bot.fetch_timeselection
        wedding_bot.send_telegram = self._fake_send
        wedding_bot.place_call = self._fake_call

    def tearDown(self):
        wedding_bot.send_telegram = self._orig_send
        wedding_bot.place_call = self._orig_call
        wedding_bot.fetch_timeselection = self._orig_fetch

    # Default doubles: telegram succeeds, call is "not configured" (no-op).
    def _fake_send(self, text):
        self._sent.append(text)
        return True

    def _fake_call(self):
        self._calls += 1
        return False

    def _fetch(self, name):
        def _f():
            return _fixture(name)
        return _f

    def test_no_slots_sends_nothing(self):
        wedding_bot.fetch_timeselection = self._fetch("no_slots.html")
        state = wedding_bot.run_poll({})
        self.assertEqual(self._sent, [])
        self.assertFalse(state.get("notified_today"))
        self.assertEqual(state["last_available"], [])
        self.assertEqual(state["consecutive_failures"], 0)

    def test_available_sends_one_alert_and_latches(self):
        wedding_bot.fetch_timeselection = self._fetch("has_slots.html")
        state = wedding_bot.run_poll({})
        self.assertEqual(len(self._sent), 1)
        self.assertIn("Monday August 17", self._sent[0])
        self.assertTrue(state["notified_today"])
        # Both channels are attempted on a hit.
        self.assertEqual(self._calls, 1)
        # Second run the same day must NOT poll or re-alert (stop-once-notified).
        state = wedding_bot.run_poll(state)
        self.assertEqual(len(self._sent), 1)
        self.assertEqual(self._calls, 1)

    def test_latches_when_only_call_succeeds(self):
        # Telegram fails, phone call succeeds -> still counts as delivered.
        wedding_bot.send_telegram = lambda text: False
        wedding_bot.place_call = lambda: True
        wedding_bot.fetch_timeselection = self._fetch("has_slots.html")
        state = wedding_bot.run_poll({})
        self.assertTrue(state["notified_today"])

    def test_no_delivery_does_not_latch(self):
        # All channels fail -> do NOT latch, so the next run retries.
        wedding_bot.send_telegram = lambda text: False
        wedding_bot.place_call = lambda: False
        wedding_bot.fetch_timeselection = self._fetch("has_slots.html")
        state = wedding_bot.run_poll({})
        self.assertFalse(state["notified_today"])

    def test_new_day_resets_latch(self):
        wedding_bot.fetch_timeselection = self._fetch("has_slots.html")
        state = wedding_bot.run_poll({})
        self.assertEqual(len(self._sent), 1)
        # Simulate the calendar rolling to a different day.
        state["notified_day"] = "1999-01-01"
        state = wedding_bot.run_poll(state)
        self.assertEqual(len(self._sent), 2)

    def test_failure_threshold_alerts_once(self):
        def _boom():
            raise wedding_bot.ScrapeError("boom")

        wedding_bot.fetch_timeselection = _boom
        state = {}
        for _ in range(wedding_bot.FAILURE_ALERT_THRESHOLD - 1):
            state = wedding_bot.run_poll(state)
        self.assertEqual(self._sent, [])  # not yet at threshold
        state = wedding_bot.run_poll(state)  # hits threshold exactly
        self.assertEqual(len(self._sent), 1)
        self.assertIn("failing", self._sent[0])
        # Further failures do not spam.
        state = wedding_bot.run_poll(state)
        self.assertEqual(len(self._sent), 1)

    def test_recovery_clears_failure_counter(self):
        state = {"consecutive_failures": 5, "notified_day": wedding_bot._today()}
        wedding_bot.fetch_timeselection = self._fetch("no_slots.html")
        state = wedding_bot.run_poll(state)
        self.assertEqual(state["consecutive_failures"], 0)

    def test_first_run_learns_calendar_silently(self):
        # The initial poll (deploy) records the visible dates without alerting.
        wedding_bot.fetch_timeselection = self._fetch("no_slots.html")
        state = wedding_bot.run_poll({})
        self.assertEqual(self._sent, [])
        self.assertIn("2026-09-03", state["known_dates"])
        self.assertEqual(state["latest_date"], "2026-09-03")

    def test_new_but_fully_booked_date_is_silent(self):
        # A later date appears but is fully booked ("No more available time
        # slots"). It must be learned silently — NO notification.
        wedding_bot.fetch_timeselection = self._fetch("no_slots.html")
        state = wedding_bot.run_poll({})
        wedding_bot.fetch_timeselection = self._fetch("new_date.html")
        state = wedding_bot.run_poll(state)
        self.assertEqual(self._sent, [])  # unavailable new date does not alert
        self.assertEqual(self._calls, 0)
        self.assertIn("2026-09-07", state["known_dates"])  # learned all the same
        self.assertFalse(state.get("notified_today"))

    def test_new_available_date_alerts_once_and_is_tagged(self):
        # Learn the calendar, then a brand-new date appears that IS bookable.
        # Exactly one alert, the new date is marked 🆕, and it latches for the day.
        wedding_bot.fetch_timeselection = self._fetch("no_slots.html")
        state = wedding_bot.run_poll({})
        wedding_bot.fetch_timeselection = self._fetch("new_available_date.html")
        state = wedding_bot.run_poll(state)
        self.assertEqual(len(self._sent), 1)
        self.assertIn("September 7", self._sent[0])
        self.assertIn("🆕", self._sent[0])
        self.assertNotIn("September 3", self._sent[0])  # booked date not listed
        self.assertTrue(state["notified_today"])
        # Same page next tick: latched, so still a single notification.
        state = wedding_bot.run_poll(state)
        self.assertEqual(len(self._sent), 1)

    def test_only_one_notification_across_repeated_polls(self):
        # The three timer tiers all hit the same state; the latch guarantees a
        # single notification (message + call) no matter how many ticks fire.
        wedding_bot.fetch_timeselection = self._fetch("has_slots.html")
        state = wedding_bot.run_poll({})
        for _ in range(5):
            state = wedding_bot.run_poll(state)
        self.assertEqual(len(self._sent), 1)
        self.assertEqual(self._calls, 1)


if __name__ == "__main__":
    unittest.main()
