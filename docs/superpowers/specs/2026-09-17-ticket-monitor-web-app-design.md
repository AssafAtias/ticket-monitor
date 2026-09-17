# Ticket monitor as a web app — design

**Date:** 2026-09-17
**Status:** approved, ready for implementation planning

## Why

The monitor works but only alerts while it is running on a PC that is awake.
It also stopped being useful silently once: the configured event was played,
and it polled that finished event for three days reporting the previous
fixture's teams. Fixture rollover is now fixed in the engine; this design
fixes the other half — the monitor should run without a PC, and be visible
from a phone.

## Goal

A hosted, always-running watcher for the next Maccabi TA fixture, alerting to
Telegram and showing live status on a phone-friendly page. One watcher, no
accounts, no watch-list CRUD.

## Decisions, and the evidence behind them

| Decision | Why |
|---|---|
| Keep the Python engine as-is | It is correct and covered by 51 tests. Nothing in this design touches seat resolution, contingents, fixture discovery or sibling discovery. |
| Poller = GitHub Actions cron | Free and unlimited on public repos, runs Python unchanged. |
| UI = GitHub Pages | Free on public repos. No second platform or account. |
| Repo becomes public | Actions minutes are unlimited on public repos and metered on private ones. |
| Alerts = Telegram only | Already works, already reaches a locked phone from anywhere. No VAPID keys, no service worker, no iOS Add-to-Home-Screen trap. |

### Rejected: Cloudflare Workers free plan

Measured, not assumed. Cron triggers on the free plan get **10 ms CPU**, the
same as HTTP requests. On the worst realistic case — a Bloomfield home game
with the season-ticket sibling map included — the status feed is 0.61 MB and
`JSON.parse` alone costs **20.6 ms p50 / 36.8 ms max** in V8, before any
logic runs. An optimised full poll (override layer indexed once instead of
building 44k key strings, contingents cached across polls) still measured
17.7 ms p50. Parsing dominates and is inherent to the payload size.

It would have worked for the 0.15 MB away fixture and failed on every home
game, by CPU termination — a silent failure, which is the exact mode this
project exists to avoid.

### Rejected: Actions on a private repo

8,640 runs/month, billed rounded up to the minute, against a 2,000-minute
free quota: ~$53/month. Fly.io at ~$2/month would have been 25x cheaper.

### Accepted cost

5–15 minute alert latency (Actions cron minimum is 5 minutes and runs are
frequently delayed). For a drop capped at 1 ticket per customer this may lose
the seat. Fly.io at ~$2/month would give 60 s. This is the deliberate price of
$0 and is recorded here so the trade is not forgotten.

## Architecture

```
GitHub Actions  (cron */5, public repo = free)
   |
   +- restore seat-map cache        actions/cache, keyed by revision
   +- checkout `data` branch   ---> read state.json
   +- run the engine, one shot
   |     discover fixture -> resolve seats -> contingents -> siblings
   +- new buyable seats?       ---> Telegram   (GitHub Secrets)
   +- force-push `data` branch ---> status.json + state.json

GitHub Pages  (deployed from site/ via actions/deploy-pages,
               only when site files change)
   +- page fetches status.json from raw.githubusercontent.com (CORS: *)
```

Pages is deployed with `actions/upload-pages-artifact` + `actions/deploy-pages`
rather than branch-based publishing, because branch publishing can only serve
the repo root or `/docs`, and `/docs` already holds these specs.

**Why data lives on a branch rather than in the Pages deploy:** publishing
data through Pages would redeploy the site 12x an hour, against a soft limit
of 10 builds/hour. Serving data from `raw.githubusercontent.com` — verified
to send `Access-Control-Allow-Origin: *` — keeps the site deploy static and
decouples data cadence from Pages entirely.

## Components

### `webapp/poll.py` — one shot

Imports `monitor`. Runs a single poll, decides what is new, sends Telegram,
writes both JSON files. No loop, no sleep; the cron is the loop.

Depends on: `monitor`. Used by: the workflow. Testable without network by
injecting a fake `Monitor`.

### `site/` — the page

Static HTML/CSS/JS. Fetches `status.json`, renders one card, re-fetches every
30 s. No build step, no framework.

### `.github/workflows/monitor.yml`

Cron `*/5 * * * *` plus `workflow_dispatch` for manual runs.

## Data contracts

### `status.json` — poller to page

```json
{
  "generated_at": "2026-09-17T18:40:00+00:00",
  "fixture": {
    "name": "מכבי נתניה - מכבי ת\"א",
    "url": "https://tickets.leaan.net/event/--02j286",
    "start": "2026-09-19T17:00:00+00:00",
    "venue": "אצטדיון נתניה"
  },
  "sale_status": "onSale",
  "max_per_order": 1,
  "buyable": 427,
  "counts": { "buyable": 427, "booked": 5823, "blocked_by_contingent": 771,
              "free_not_on_sale": 6409, "reserved": 40,
              "notforsale": 0, "other": 0 },
  "tiers": [ { "category": "אי פלוס", "price": 155, "available": 427 },
             { "category": "מזרחי עליון", "price": 75, "available": 0 } ],
  "cheapest": [ { "price": 155, "section": "F", "row": "1", "seat": "1",
                  "gate": "F", "category": "אי פלוס" } ],
  "alerted": 427,
  "error": null
}
```

`error` carries the exception string when a poll fails, so the page can show
a failure rather than stale success. `generated_at` is the single source of
truth for freshness; the page never trusts its own clock about when data was
produced.

### `state.json` — poller to itself

```json
{ "event": "<shop url>", "alerted": ["<statusId>"],
  "updated": "<iso>", "last_heartbeat": "2026-09-17" }
```

Same per-seat semantics as today: alert only on newly buyable seats, forget
seats that disappear so they can alert again, and drop the whole set when the
fixture changes. On a home game `alerted` reaches ~22k ids (~500 KB); the
branch is force-pushed so history does not grow.

## Config and secrets

`config.json` becomes a committed, public file with the Telegram block
removed. `load_config` gains env-var overrides so the workflow injects
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from GitHub Secrets. Secrets are
never read from the file and never written to either JSON output.

Local use is unchanged: a developer's own credentials come from the same env
vars, so no secret ever needs to sit in a file again.

`.gitignore` changes accordingly: `config.json` stops being ignored, since the
committed copy is now secret-free. `state.json` stays ignored on `main` — the
published copy lives on the `data` branch, which the workflow writes from a
separate worktree so the main-branch ignore rules do not apply to it.

## Failure honesty

The project's existing rule — silence must never be mistakable for "no
tickets yet" — carries over:

- The page renders freshness from `generated_at`: normal under 10 min, amber
  10–30 min ("cron is running late"), red beyond 30 min ("monitor may be
  down"). Red is a visual state, not a hidden console line.
- A failed poll writes `status.json` with `error` set and the previous
  counts left untouched, so the page shows a failure instead of stale success.
- A once-daily Telegram heartbeat, because a page nobody is looking at cannot
  warn anybody. This is also how a workflow auto-disabled for repo inactivity
  (GitHub does this after 60 days) would be noticed.
- Telegram sends retry, and a delivery failure is recorded in `status.json`.

## Testing

The 51 existing tests stay untouched and must keep passing — this design adds
no reason for any of them to change.

New tests in `webapp/test_poll.py`, all offline against fixtures:

- serialisation: a known engine result produces the documented `status.json`
- tier roll-up: per-seat results aggregate to correct per-tier availability
- dedup: a second poll with identical seats alerts nobody
- rollover: a fixture change drops `alerted` and re-alerts
- failure: a raising poll writes `error` and preserves prior counts
- heartbeat: fires once per day, not once per poll
- secrets: Telegram credentials never appear in either JSON output

## Out of scope

Watch-list CRUD, multiple simultaneous events, accounts, web push, SMS/call
escalation, buying automation.

## Known limits

- 5–15 min latency; may lose a contested seat.
- `raw.githubusercontent.com` caches ~5 min, so observed staleness can reach
  ~10 min. The page reports true age from `generated_at`, so it is never
  misleading — only late.
- Scheduled workflows are auto-disabled after 60 days of repo inactivity.
- Away fixtures require being signed in to buy; the monitor sees the sale
  without signing in, so the page states this.
