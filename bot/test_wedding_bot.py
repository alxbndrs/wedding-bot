#!/usr/bin/env python3
"""Unit tests for the TimeSelection parser and the poll/notify decision logic.

Run: cd bot && python3 -m unittest
No network and no Telegram calls happen — sends are monkeypatched.
"""

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

    def test_empty_page_raises_scrape_error(self):
        with self.assertRaises(wedding_bot.ScrapeError):
            wedding_bot.parse_dates("<html><body>nothing here</body></html>")


class PollLogicTest(unittest.TestCase):
    def setUp(self):
        self._sent = []
        self._orig_send = wedding_bot.send_telegram
        self._orig_fetch = wedding_bot.fetch_timeselection
        wedding_bot.send_telegram = self._fake_send

    def tearDown(self):
        wedding_bot.send_telegram = self._orig_send
        wedding_bot.fetch_timeselection = self._orig_fetch

    def _fake_send(self, text):
        self._sent.append(text)
        return True

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
        # Second run the same day must NOT poll or re-alert (stop-once-notified).
        state = wedding_bot.run_poll(state)
        self.assertEqual(len(self._sent), 1)

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


if __name__ == "__main__":
    unittest.main()
