#!/usr/bin/env python3
"""Aabenraa wedding-slot watcher.

Polls the frontdesksuite booking flow, and sends a Telegram message the moment a
bookable time slot appears so a human can open the browser and book it. Uses only
the Python standard library so it runs on a bare server with no pip install.

Config comes from environment variables (see weddingbot.env.j2). Run with no args
for a normal poll; see --help for the maintenance flags.
"""

from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# --- Configuration (from environment) ---------------------------------------

# The StartReservation URL establishes flow state and redirects to TimeSelection.
# Defaults target the "without own witnesses" ceremony type from the shared link.
START_URL = os.environ.get(
    "START_URL",
    "https://reservation.frontdesksuite.com/aabenraavielse/vielse/ReserveTime/"
    "StartReservation?pageId=b373305a-e1ef-4f58-8e27-fbfbf65b417a"
    "&buttonId=25803dc3-fdee-4af6-bc51-2c62de114ceb&culture=en&uiCulture=en",
)
# Public entry page — handed to the human in the alert so they can book manually.
BOOKING_URL = os.environ.get(
    "BOOKING_URL",
    "https://reservation.frontdesksuite.com/aabenraavielse/vielse?culture=en",
)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.environ.get(
    "STATE_FILE", os.path.expanduser("~/state/state.json")
)
LOG_FILE = os.environ.get("LOG_FILE", os.path.expanduser("~/state/weddingbot.log"))
LOG_MAX_BYTES = int(os.environ.get("LOG_MAX_BYTES", str(512 * 1024)))

# Re-alert if a date is still open this many minutes after the last alert.
RENOTIFY_MINUTES = int(os.environ.get("RENOTIFY_MINUTES", "30"))
# Send a single "bot is failing" warning after this many consecutive failures.
FAILURE_ALERT_THRESHOLD = int(os.environ.get("FAILURE_ALERT_THRESHOLD", "10"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))

# Test hook: read HTML from a local file instead of the network.
FETCH_FILE = os.environ.get("FETCH_FILE", "")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

NO_SLOTS_MARKER = "No more available time slots"


# --- HTML parsing -----------------------------------------------------------


class _TimeSelectionParser(HTMLParser):
    """Extract each date section and whether it has bookable slots.

    The page renders one ``<div class="date ...">`` per date. Inside it, a
    ``<span class="... header-text">`` holds the date text, and an unavailable
    date carries a ``warning-message`` block with NO_SLOTS_MARKER. An available
    date instead exposes clickable time entries (anchors / elements wired to the
    ``selectTime(...)`` JS handler), and no warning marker.
    """

    def __init__(self) -> None:
        super().__init__()
        self.dates: list[dict] = []
        self._depth = 0  # nesting depth inside the current date div
        self._in_date = False
        self._capture_header = False
        self._cur_name: list[str] = []
        self._cur_text: list[str] = []  # all text inside the current date div
        self._found_select_time = False

    def handle_starttag(self, tag, attrs):
        attrd = dict(attrs)
        classes = attrd.get("class", "").split()
        if tag == "div" and "date" in classes and not self._in_date:
            self._in_date = True
            self._depth = 1
            self._capture_header = False
            self._cur_name = []
            self._cur_text = []
            self._found_select_time = False
            return
        if not self._in_date:
            return
        if tag == "div":
            self._depth += 1
        if "header-text" in classes:
            self._capture_header = True
        # An available slot is a control wired to selectTime(...).
        onclick = attrd.get("onclick", "")
        if "selectTime" in onclick:
            self._found_select_time = True

    def handle_endtag(self, tag):
        if not self._in_date:
            return
        if self._capture_header and tag == "span":
            self._capture_header = False
        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self._close_date()

    def handle_data(self, data):
        if not self._in_date:
            return
        if self._capture_header:
            self._cur_name.append(data)
        self._cur_text.append(data)

    def _close_date(self):
        name = _clean(" ".join(self._cur_name))
        body = " ".join(self._cur_text)
        has_marker = NO_SLOTS_MARKER.lower() in body.lower()
        available = self._found_select_time or not has_marker
        if name:
            self.dates.append({"name": name, "available": available})
        self._in_date = False


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class ScrapeError(Exception):
    """Raised when the page is not a recognizable TimeSelection page."""


def parse_dates(page_html: str) -> list[dict]:
    """Return ``[{"name": str, "available": bool}, ...]`` or raise ScrapeError."""
    # Guard against the error / concurrent-tab / queue pages that carry no dates.
    if 'class="date' not in page_html and "date-list" not in page_html:
        snippet = _clean(re.sub(r"<[^>]+>", " ", page_html))[:200]
        raise ScrapeError(f"no date sections found; page said: {snippet!r}")
    parser = _TimeSelectionParser()
    parser.feed(page_html)
    if not parser.dates:
        raise ScrapeError("date-list present but no date entries parsed")
    return parser.dates


# --- Network ----------------------------------------------------------------


def fetch_timeselection() -> str:
    """Fetch the TimeSelection HTML with a fresh cookie jar (or a local file)."""
    if FETCH_FILE:
        with open(FETCH_FILE, "r", encoding="utf-8") as fh:
            return fh.read()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(START_URL, headers={"User-Agent": USER_AGENT})
    with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


# --- Telegram ---------------------------------------------------------------


def send_telegram(text: str) -> bool:
    """Send an HTML-formatted Telegram message. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("telegram not configured (missing token/chat id); message dropped")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }
    ).encode()
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            if not body.get("ok"):
                log(f"telegram API error: {body}")
                return False
            return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"telegram send failed: {exc}")
        return False


# --- State ------------------------------------------------------------------


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}"
    print(line, file=sys.stderr)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            # Keep the tail so the log never grows without bound.
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
                tail = fh.readlines()[-500:]
            with open(LOG_FILE, "w", encoding="utf-8") as fh:
                fh.writelines(tail)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _today() -> str:
    return time.strftime("%Y-%m-%d")


# --- Core logic -------------------------------------------------------------


def build_alert(available: list[dict]) -> str:
    names = [d["name"] for d in available]
    lines = "\n".join(f"• {html.escape(n)}" for n in names)
    return (
        "🔔 <b>Wedding slot(s) available!</b>\n\n"
        f"{lines}\n\n"
        f'👉 <a href="{html.escape(BOOKING_URL)}">Open the booking page and book now</a>'
    )


def run_poll(state: dict) -> dict:
    """One poll cycle. Mutates and returns ``state``."""
    now = time.time()
    today = _today()

    # Reset the per-day "already notified" latch when a new day starts. Each
    # morning's slot drop is a fresh event, so also clear the renotify history
    # so the first hit of the new day always alerts.
    if state.get("notified_day") != today:
        state["notified_day"] = today
        state["notified_today"] = False
        state["last_alert"] = {}

    # Stop-once-notified: during a burst window we don't re-poll after a hit.
    if state.get("notified_today"):
        log("already notified today; skipping poll")
        return state

    try:
        page = fetch_timeselection()
        dates = parse_dates(page)
    except (ScrapeError, urllib.error.URLError, OSError) as exc:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        log(f"scrape failed ({state['consecutive_failures']}): {exc}")
        if state["consecutive_failures"] == FAILURE_ALERT_THRESHOLD:
            send_telegram(
                "⚠️ <b>Wedding bot is failing.</b>\n"
                f"{state['consecutive_failures']} consecutive errors.\n"
                f"Last error: <code>{html.escape(str(exc))[:300]}</code>"
            )
        return state

    # Success: clear the failure counter.
    if state.get("consecutive_failures"):
        log(f"recovered after {state['consecutive_failures']} failures")
    state["consecutive_failures"] = 0

    available = [d for d in dates if d["available"]]
    avail_names = sorted(d["name"] for d in available)
    log(
        f"checked {len(dates)} dates; "
        f"{len(available)} available: {avail_names or '[]'}"
    )

    last_alert = state.get("last_alert", {})  # name -> epoch seconds
    stale = now - RENOTIFY_MINUTES * 60
    to_alert = [
        d for d in available if last_alert.get(d["name"], 0) < stale
    ]

    if to_alert:
        if send_telegram(build_alert(available)):
            state["notified_today"] = True
            for d in available:
                last_alert[d["name"]] = now
            log(f"ALERT sent for {[d['name'] for d in available]}")
        else:
            log("alert NOT sent (telegram failure); will retry next run")
    state["last_alert"] = {
        name: ts for name, ts in last_alert.items() if name in avail_names
    }
    state["last_available"] = avail_names
    state["last_check"] = now
    return state


# --- Entry points -----------------------------------------------------------


def cmd_test_notify() -> int:
    ok = send_telegram(
        "✅ <b>Wedding bot deployed.</b> Notifications are working — "
        "you'll get a message here the moment a slot opens."
    )
    return 0 if ok else 1


def cmd_heartbeat(state: dict) -> int:
    last = state.get("last_check")
    when = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(last))
        if last
        else "never"
    )
    avail = state.get("last_available") or []
    fails = state.get("consecutive_failures", 0)
    ok = send_telegram(
        "💓 <b>Wedding bot heartbeat.</b>\n"
        f"Last check: {when}\n"
        f"Currently available: {', '.join(avail) if avail else 'none'}\n"
        f"Consecutive failures: {fails}"
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--test-notify",
        action="store_true",
        help="send a one-off 'deployed OK' message and exit",
    )
    ap.add_argument(
        "--heartbeat",
        action="store_true",
        help="send a status summary and exit (does not poll)",
    )
    args = ap.parse_args(argv)

    if args.test_notify:
        return cmd_test_notify()

    state = load_state()
    if args.heartbeat:
        return cmd_heartbeat(state)

    state = run_poll(state)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
