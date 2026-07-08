# wedding-bot

Watches the Aabenraa civil-wedding booking site and, the instant a bookable slot
appears, **sends you a Telegram message and (optionally) rings your phone** so
you can open the browser and book it yourself. It does not book anything
automatically. The phone call is what wakes you at 5 AM; the Telegram message
carries the tappable booking link.

New ceremony slots are released around **05:00 Danish time**, ~60 days ahead, on
weekdays (Mon–Thu are ceremony days). The bot polls every minute in a window
around that drop and stops for the day once it has notified you.

## How it works

- `bot/wedding_bot.py` — a single, dependency-free Python 3 script (standard
  library only). Each run it:
  1. Opens a fresh session on the booking flow (`StartReservation` →
     `TimeSelection`) — a fresh cookie jar per run avoids the site's
     "multiple tabs" error.
  2. Parses every date section and detects which have bookable time slots.
  3. If any date is available, sends a Telegram alert with a link to the booking
     page and (if Twilio is configured) places a phone call that reads out a
     spoken alert, then **latches** so it won't poll or re-alert again that day.
  4. Diffs against `~/state/state.json` for de-duplication, counts consecutive
     failures, and warns once (via Telegram) if the site scrape keeps failing.
- Deployed to your OVH server by Ansible as a dedicated non-sudo `weddingbot`
  user, driven by **systemd user timers** (no cron, survives reboots via linger).

Watched ceremony type: **"without own witnesses"** (the `buttonId` from your
link). Change `weddingbot_start_url` in `ansible/inventory/group_vars/all.yml`
to watch a different type.

## One-time setup

### 1. Create a Telegram bot and get your chat id

1. In Telegram, message [@BotFather](https://t.me/BotFather), send
   `/newbot`, follow the prompts, and copy the **bot token** it gives you
   (looks like `1234567890:AAE...`).
2. Open a chat with your new bot and send it any message (e.g. `hi`) — this is
   required before it can message you.
3. Get your **chat id**:
   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" \
     | grep -o '"chat":{"id":[0-9-]*' | head -1
   ```
   The number after `"id":` is your `telegram_chat_id`.

### 1b. (Optional) Set up the phone call via Twilio

A phone call is far more likely to wake you at 5 AM than a notification. To
enable it:

1. Create a [Twilio](https://www.twilio.com/try-twilio) account and buy a phone
   number with **Voice** capability (~$1–2/month).
2. From the Twilio Console dashboard, copy your **Account SID** and **Auth
   Token**.
3. Note your Twilio number (the caller) and the number to ring (your mobile),
   both in E.164 format, e.g. `+4520123456`.
4. On the receiving phone, add the Twilio number as a contact and put it on your
   **emergency-bypass / allow list** so it rings through Do Not Disturb.

You'll put these four values into `dev.yml` in the next step. Leave them blank to
run Telegram-only.

### 2. Fill in server + secrets

```bash
cp ansible/inventory/host_vars/dev.yml.example ansible/inventory/host_vars/dev.yml
```
Edit `dev.yml` (gitignored) with:
- `ansible_host` / `ansible_ssh_private_key_file` — the **same values you use in
  the `ovhcloud-server-setup` repo**. Ansible connects as `ansible-clispi` on
  port 2222 (the hardened admin identity); this playbook does not touch SSH,
  firewall, or any other baseline hardening.
- `telegram_bot_token` and `telegram_chat_id` from step 1.
- (optional) `twilio_account_sid`, `twilio_auth_token`, `twilio_from`, `call_to`
  from step 1b — or leave blank for Telegram-only.

### 3. Deploy

```bash
cd ansible
ansible-galaxy collection install -r requirements.yml
ansible-playbook playbooks/site.yml
```

On success you'll receive a **"✅ Wedding bot deployed"** Telegram message.

## Operating it

Everything runs as the `weddingbot` user on the server. To inspect:

```bash
# as an admin, become the bot user with a user-systemd session:
sudo machinectl shell weddingbot@   # or: sudo -u weddingbot XDG_RUNTIME_DIR=/run/user/$(id -u weddingbot) bash

systemctl --user list-timers            # see next burst/heartbeat fire times
systemctl --user status weddingbot-burst.timer
journalctl --user -u weddingbot.service # per-run logs
cat ~/state/weddingbot.log              # bot's own one-line-per-run log
cat ~/state/state.json                  # last check, availability, latch
```

Send yourself a test / status message any time:
```bash
python3 ~/bin/wedding_bot.py --test-notify   # "deployed OK" style message
python3 ~/bin/wedding_bot.py --test-call      # place a real test phone call (Twilio)
python3 ~/bin/wedding_bot.py --heartbeat      # last-check + current availability
```
(These read your token/number from `~/.config/weddingbot.env`; run them with
that env loaded, e.g. `set -a; . ~/.config/weddingbot.env; set +a` first.)

### Schedule

- **Burst** (`weddingbot-burst.timer`): every minute, Mon–Thu, across the two
  possible UTC placements of 04:50–05:10 Copenhagen (DST-safe — see the comment
  in `weddingbot-burst.timer.j2`). Latches after the first alert each day.
- **Heartbeat** (`weddingbot-heartbeat.timer`): Monday 08:00, so a silent bot
  can be distinguished from a dead one. Toggle with
  `weddingbot_heartbeat_enabled`.
- **Watch** (`weddingbot-watch.timer`): optional every-10-min daytime poll to
  catch cancellations. Off by default — set `weddingbot_watch_enabled: true`.

Tuning knobs live in `ansible/inventory/group_vars/all.yml`.

## Development

```bash
cd bot
python3 -m unittest          # parser + poll/notify logic tests (no network)

# live poll against the real site (no Telegram configured → just logs):
STATE_FILE=/tmp/s.json LOG_FILE=/tmp/s.log python3 wedding_bot.py

# simulate availability against a fixture:
FETCH_FILE=fixtures/has_slots.html STATE_FILE=/tmp/s.json python3 wedding_bot.py
```

Fixtures in `bot/fixtures/` include the real "no slots" page captured live, plus
synthetic "slots available" and error pages.
