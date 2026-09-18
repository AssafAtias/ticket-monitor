# Ticket Monitor

Watches a vivenu-backed ticket shop and alerts the moment a **genuinely
purchasable** seat appears. It follows Maccabi TA's schedule by itself: every
config refresh it re-reads the ticket office's fixture list and locks onto the
next game that has not been played yet.

## Where it runs

Once GitHub Pages is switched on for this repository the page will be served
at **https://assafatias.github.io/ticket-monitor/**. It is not enabled yet, so
that URL does not resolve today.

A GitHub Actions cron job polls every 5 minutes, alerts to Telegram, and
publishes `status.json` and `state.json` to the `data` branch. The page fetches that document
and renders it; it never talks to the ticket shop itself.

```
Actions cron */5  ->  webapp/poll.py  ->  Telegram
                                      ->  data branch (status.json, state.json)
                                                 ^
                      GitHub Pages (site/) ------+  fetched by the browser
```

`run.cmd` still works for running it locally on Windows, with the console
output, toast and always-on-top popup.

## Run it

```
run.cmd
```

Leave the window open; Ctrl+C stops it. It restarts itself if it ever crashes.

```
python monitor.py --once         # one check, print the breakdown, exit
python monitor.py --test-alert   # verify the toast + Telegram path
python -m unittest discover -p "test_*.py"   # 187 Python tests
node --test "site/**/*.test.js"              # 30 JS tests (keep the quotes)
```

## Which game it watches

`"event_url": "auto"` (the default) means: read the ticket office's Maccabi TA
listing, keep every entry named after two sides — season memberships and refund
vouchers share that feed and are not games — and take the soonest one whose
**final whistle** is still ahead. Selection runs off the end of the match, not
kick-off, so a seat released at 20:40 still alerts.

This is not a convenience. The first version of this monitor had the event
hard-coded, the game was played, and it went on polling that finished event for
three days, reporting the previous fixture's teams and never saying a word.
Two things prevent a repeat:

- the fixture is re-resolved on every config refresh, so a rollover happens on
  its own, and `state.json` records which game its seat ids belong to so last
  week's ids cannot mute this week's first alert;
- `saleStatus: "past"` is now a hard error rather than a state to poll in. In
  auto mode it re-reads the listing; if you pinned an event by hand it stops
  and says so, loudly.

Put a real shop URL in `event_url` to pin one event — a cup tie, or a sale the
league listing does not carry. You then own the rollover.

### Sibling events

Events sharing a seat map are the other thing that cannot be hard-coded, and
nothing lists them: not the shop page, not the fixture feed. The status feed's
`override` layer is keyed `<eventId>.<statusId>` and names them anyway, even
when no children were requested — so the monitor harvests them from there and
re-fetches with their maps included.

Getting this wrong is not subtle. On the Bloomfield home game against Sakhnin
the season-ticket sibling marks seats the parent map leaves unmarked:

| | buyable seats reported |
|---|---|
| sibling's map included | **1,405** |
| sibling missed | 22,559 |

## Why this is not a one-liner

The obvious approach — poll the status feed, count `"free"` — is wrong. At the
time of writing the feed reports **521 free seats while 0 are purchasable**.
Every one of those 521 sits in a press / VIP / platinum / accessible category
with no ticket on sale. A naive monitor would alert every 60 seconds forever
and every alert would be junk.

A seat is only really buyable when all four hold:

1. **No event marks it taken.** Statuses arrive as a *united map*: the parent
   seating event plus every child event sharing the seat map, with an override
   layer keyed `<eventId>.<statusId>` taking precedence. Most restrictive wins;
   a seat absent from every map is free. (Port of vivenu's `resolveSeatStatus`.)
2. **No contingent holds it.** 109 contingents currently reserve ~7,400 seats
   this shop cannot sell. General-admission contingents and expired
   `blockedUntil` holds do not count. 1,619 seats sit in more than one
   contingent, so a seat stays blocked while *any* non-allowed one holds it.
   (Port of `initBlockedSeatsByStatusId`.)
3. **Its category has an active ticket type.** 27 of the 43 categories do.
4. It resolves to a price, used to rank alerts cheapest-first.

## Data sources

| Endpoint | Role | Cadence |
|---|---|---|
| the ticket office's Maccabi TA listing (`__NEXT_DATA__`) | which game to watch | every 5 min |
| `/api/public/event/{id}/status?childEventIds=…` | live seat statuses | every 60s |
| `/api/public/event/{id}/contingents?childEventIds=…` | which seats are held back | every 5 min |
| the shop page's `__NEXT_DATA__` | categories, active tickets, prices, limits | every 5 min |
| `/api/public/event/{id}/map?c={rev}&shrink=true` | seat ID → block/row/seat/gate | once, cached in `.cache/` |

The cadences above are the **desktop loop's**: one long-lived process with a
60-second status poll inside a 5-minute config refresh. The hosted poller has
no loop. Each cron run is a cold start that fetches all of it once - listing,
status, contingents, shop page, and the seat map unless the Actions cache still
holds it - and then exits. Its effective cadence is the cron schedule, every
5 minutes, for everything.

**The 5-minute config refresh is not incidental.** A release may not be a seat
flipping to free — it can be a whole category gaining an active ticket (gate 4
alone holds 230 free seats waiting on exactly that), or a contingent being
released. Polling `status` alone would miss both. When a category goes on sale
the monitor logs `!! category went ON SALE`.

## Alerts

### Hosted (GitHub Actions)

**Telegram is the only channel.** There is no screen to toast at and no window
to raise, so a Telegram message that does not arrive is a seat nobody was told
about. Three things follow from that:

- a failed delivery un-marks the seats, so they stay eligible to alert again on
  the next poll - a duplicate message is a much smaller cost than silence;
- the page publishes whether the alert was delivered, and says so if it was not;
- a daily heartbeat and a five-consecutive-failure escalation (below) are what
  make a dead monitor audible.

### Local (`run.cmd`)

The desktop build keeps all three channels: the always-on-top window, the
Windows toast, and Telegram if the environment supplies credentials.

- **An always-on-top window** that stays until dismissed, with an "Open the
  shop" button. This is the alert that must not fail: it survives Do Not
  Disturb and does not depend on notification settings.
- **Windows toast** with an urgent looping alarm, plus three system beeps.

Two silent-failure traps were hit getting those working, both worth knowing if
this is ever ported:

- A toast whose `<audio loop="true">` is not paired with `duration="long"` is
  rejected by Windows while `Show()` still reports success — nothing appears.
- Launching the popup with `DETACHED_PROCESS` leaves PowerShell without a
  console, so it exits immediately with code 0 and the window never renders.
  `CREATE_NO_WINDOW` (`0x08000000`) is the flag that works.

Both were verified by enumerating visible desktop windows via Win32
`EnumWindows` + `IsWindowVisible`, rather than trusting the API's return value.

- **Telegram**, if the environment supplies credentials. Locally it is optional;
  hosted it is the whole alerting system.
- **Console + `history.jsonl`**, one line per poll. Local only: the hosted
  poller never writes `history.jsonl`, and the file is gitignored. What the
  hosted run leaves behind is `status.json` and `state.json` on the `data`
  branch.

Alerts fire only for **newly** buyable seats (`state.json`). A seat someone else
takes is forgotten, so it can alert again if it comes back. The state records
the game it belongs to and is dropped on rollover, because seat ids only mean
anything within one seat map.

If five polls fail in a row, silence should never be mistaken for "no tickets
yet":

- **locally**, you get a (quiet) toast;
- **hosted**, there is no toast, so the fifth consecutive failure sends one
  Telegram message naming the last error. One message, not one per poll: the
  flag that suppresses repeats is set only when the message is actually
  delivered, and it resets on the first successful poll.

The page will not hide it either. A document carrying an error can never show
the fresh green badge, however recently the failed poll ran, and the counts it
carries forward are labelled with when they were last confirmed.

## Telegram setup

> **`config.json` is committed to this repository and must never contain
> credentials.** The environment variables below take precedence and are the
> only place the hosted deployment supplies them — but the config loader
> overlays them onto whatever `config.json` holds, so a token left in that
> committed file would still be read and used. That is exactly why it must
> never be there. An earlier draft of this README said to put them in
> `config.json`; following it published a live bot token and cost a rotation.

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id`.
3. Provide them as environment variables:

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | the token from BotFather |
   | `TELEGRAM_CHAT_ID` | your `message.chat.id` |

   Locally, set them in the shell that runs `run.cmd`. Hosted, add them as
   **repository secrets** (Settings → Secrets and variables → Actions); the
   workflow passes them to the poll step and nowhere else.

Both halves must be present. A token with no chat id cannot send, and the
config loader ignores a half-configured pair rather than pretending Telegram is
working. Neither value is ever written to `status.json` or `state.json`.

## Config

| Key | Meaning |
|---|---|
| `event_url` | `"auto"` to follow the schedule, or a shop URL to pin one event |
| `discovery_url` | the fixture listing to read; defaults to the Maccabi TA page |
| `child_event_ids` | extra sibling events to always ask for — normally `[]`, since they are discovered per event |
| `poll_seconds` | status poll interval (60) |
| `refresh_config_seconds` | on-sale/contingent re-read interval (300) |
| `alert_below_price` | alert only **strictly below** this price; `null` = any price |
| `popup` | show the always-on-top window (true) |

### Price filter

Alert only on seats **strictly below** this price; `null` means any price. It
is currently `800`, which covers every tier of every fixture — the away game at
Netanya sells at 75 (מזרחי) and 155 (אי פלוס), home games start at 90.

Because the monitor now moves between venues, the threshold is a ceiling rather
than a tier selector: on a Bloomfield home game `200` would alert only on the
180 ILS tiers (gate 10, gate 11, מזרחי עליון 428-431, שער 7 משפחות 419-422) and
stay silent on the 200 ILS blocks. Check the tiers of the current fixture — the
startup log prints the qualifying ones — before tightening it.

The filter applies at alert time only. A pricier seat is still counted in the
breakdown, still written to `history.jsonl`, and still printed to the console as
`N seat(s) found but over the … threshold` — so it can never disappear
unnoticed, it just doesn't wake you up.

## Limits

- **5-15 minutes of latency.** GitHub's cron minimum is 5 minutes and runs are
  frequently delayed under load. For a drop capped at one ticket per customer
  this may lose the seat. Hosting the poller on a $2/month always-on machine
  would bring it back to 60 seconds; that trade was made deliberately to keep
  the running cost at zero.
- **Max 1 ticket per customer, per order, per transaction.** When it fires, move.
- Away fixtures can only be bought by signed-in users. The monitor sees the
  sale without signing in, so **sign in before the alert arrives**.
- Scheduled workflows are disabled automatically after 60 days without
  repository activity. The daily Telegram heartbeat is how you would notice.
- The page reports the true age of its data, so it is never misleading about
  freshness — only, sometimes, late.
