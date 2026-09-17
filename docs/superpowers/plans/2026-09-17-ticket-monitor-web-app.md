# Ticket Monitor Web App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Host the existing ticket monitor on free infrastructure so it runs without a PC, alerts to Telegram, and shows live status on a phone-friendly page.

**Architecture:** A GitHub Actions cron job runs the existing Python engine once every 5 minutes, sends Telegram alerts for newly buyable seats, and force-pushes `status.json` + `state.json` to an orphan `data` branch. A static page on GitHub Pages fetches `status.json` from `raw.githubusercontent.com` (CORS-enabled) and renders one card. The engine in `monitor.py` is not modified except for reading secrets from the environment.

**Tech Stack:** Python 3.12 standard library only (no third-party packages — the project has no `requirements.txt` and must keep it that way), vanilla HTML/CSS/JS with no build step, GitHub Actions, GitHub Pages, `node --test` for the one piece of JS logic worth testing.

**Spec:** `docs/superpowers/specs/2026-09-17-ticket-monitor-web-app-design.md`

## Global Constraints

- **Python standard library only.** No new dependencies, no `requirements.txt`. The engine already runs on stdlib alone; keep it so.
- **The 51 existing tests in `test_monitor.py` must keep passing, unmodified.** If a change to `monitor.py` would require editing an existing test, the change is wrong — stop and reconsider.
- **The engine is off-limits.** Do not modify `parse_shop`, `build_seat_index`, `united_map`, `resolve_seat_status`, `blocked_seat_ids`, `compute_buyable`, `breakdown`, `sibling_event_ids`, `extract_fixtures`, `pick_next_fixture`, or the `Monitor` class. Task 1 modifies `load_config` only.
- **Never call `monitor.announce()` from the web app.** It shells out to PowerShell for the Windows toast and always-on-top popup. On a Linux runner `_powershell` catches the failure and logs `toast failed: ...` twice per alert — harmless but noisy, and it gives no control over retries. The poller calls `monitor.telegram()` directly.
- **Secrets come from the environment only.** `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. They must never be written to `status.json` or `state.json`, and never committed.
- **Never write a credential literal into a command, a test, or a doc** — not even a fragment, and not even to grep for it. Scan by pattern. An earlier draft of this plan embedded token fragments in a `grep` example and committed them; that is the failure this rule exists to prevent.
- **All new tests run offline.** No network access in any test. Use fixtures and injected fakes.
- **Times are UTC and ISO-8601 with an offset**, produced by `datetime.datetime.now(datetime.timezone.utc).isoformat()`.

---

### Task 1: Read Telegram credentials from the environment

The workflow runs on a public repo, so `config.json` gets committed and must not contain secrets. `load_config` learns to overlay environment variables on top of the file.

**Files:**
- Modify: `monitor.py` (the `load_config` function, around line 707)
- Modify: `.gitignore` (remove the `config.json` line)
- Modify: `config.json` (remove the `telegram` block)
- Test: `test_monitor.py` (append a new test class)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `monitor.load_config() -> dict` — unchanged signature. When `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in the environment they replace `cfg["telegram"]["bot_token"]` and `cfg["telegram"]["chat_id"]`. Every later task calls `load_config()` and passes the resulting `cfg` to `monitor.telegram(cfg, text)`.

- [ ] **Step 1: Write the failing test**

Append to `test_monitor.py`, before the `if __name__ == "__main__":` block:

```python
class TestConfigSecrets(unittest.TestCase):
    """The repo is public, so credentials come from the environment."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "config.json"
        patcher = unittest.mock.patch.object(monitor, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, cfg):
        self.path.write_text(json.dumps(cfg), encoding="utf-8")

    def test_env_supplies_credentials_the_file_does_not_have(self):
        self.write({"event_url": "auto"})
        with unittest.mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok",
                                                   "TELEGRAM_CHAT_ID": "42"}):
            cfg = monitor.load_config()
        self.assertEqual(cfg["telegram"]["bot_token"], "tok")
        self.assertEqual(cfg["telegram"]["chat_id"], "42")

    def test_env_wins_over_the_file(self):
        self.write({"telegram": {"bot_token": "stale", "chat_id": "old"}})
        with unittest.mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fresh",
                                                   "TELEGRAM_CHAT_ID": "new"}):
            cfg = monitor.load_config()
        self.assertEqual(cfg["telegram"]["bot_token"], "fresh")
        self.assertEqual(cfg["telegram"]["chat_id"], "new")

    def test_file_still_works_with_no_env(self):
        """Local use must not require exporting variables."""
        self.write({"telegram": {"bot_token": "local", "chat_id": "7"}})
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            cfg = monitor.load_config()
        self.assertEqual(cfg["telegram"]["bot_token"], "local")

    def test_absent_everywhere_is_not_an_error(self):
        """Telegram is optional; everything else must still work without it."""
        self.write({"event_url": "auto"})
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            cfg = monitor.load_config()
        self.assertFalse((cfg.get("telegram") or {}).get("bot_token"))
        self.assertFalse(monitor.telegram(cfg, "should not send"))

    def test_partial_env_is_ignored_rather_than_half_applied(self):
        """A token with no chat id cannot send; do not pretend it is configured."""
        self.write({"event_url": "auto"})
        with unittest.mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"},
                                      clear=True):
            cfg = monitor.load_config()
        self.assertFalse((cfg.get("telegram") or {}).get("bot_token"))
```

Add these imports to the top of `test_monitor.py` alongside the existing ones:

```python
import os
import shutil
import tempfile
import unittest.mock
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_monitor.TestConfigSecrets -v`
Expected: FAIL — `test_env_supplies_credentials_the_file_does_not_have` fails with `KeyError: 'telegram'`.

- [ ] **Step 3: Write minimal implementation**

In `monitor.py`, replace `load_config`:

```python
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"missing config: {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return _apply_env_secrets(cfg)


def _apply_env_secrets(cfg: dict) -> dict:
    """Overlay credentials from the environment onto the config file.

    The repo is public, so config.json is committed without secrets and the
    workflow injects them. Both halves must be present: a token without a chat
    id cannot send, and pretending otherwise turns a misconfiguration into a
    silent non-delivery.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        cfg = dict(cfg)
        cfg["telegram"] = {"bot_token": token, "chat_id": chat_id}
    return cfg
```

Add `import os` to the import block at the top of `monitor.py` (it is not currently imported).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_monitor -v`
Expected: PASS, 56 tests (51 existing + 5 new). The 51 existing tests must be unmodified.

- [ ] **Step 5: Commit config changes to the repo**

Remove the `telegram` block from `config.json` so the committed copy is secret-free:

```json
{
  "label": "next Maccabi TA game (discovered automatically)",
  "event_url": "auto",
  "child_event_ids": [],
  "seating_base": "https://seatmap.vivenu.com",
  "poll_seconds": 60,
  "refresh_config_seconds": 300,
  "alert_below_price": 800,
  "popup": true
}
```

Remove the `config.json` line from `.gitignore`. Leave `state.json`, `history.jsonl`, `.cache/`, `__pycache__/` and `*.pyc` ignored.

**Before committing, verify no credential is being staged.**

Scan by pattern, never by literal: writing the actual token into a command is
how it ends up committed in the first place. A Telegram bot token is digits,
a colon, then 35 URL-safe base64 characters.

```bash
git add -A
if git diff --cached | grep -nE "[0-9]{8,10}:[A-Za-z0-9_-]{30,}"; then
  echo "CREDENTIAL IN STAGED DIFF - do not commit"; exit 1
else
  echo "staged diff is clean"
fi
```

Expected: `staged diff is clean`. If it reports a credential, stop and report
BLOCKED — do not commit.

- [ ] **Step 6: Commit**

```bash
git add monitor.py test_monitor.py config.json .gitignore
git commit -m "Read Telegram credentials from the environment

The repo is going public, so config.json is committed without secrets
and the workflow injects them. Both halves must be present: a token
with no chat id cannot send, and treating that as configured would
turn a misconfiguration into silent non-delivery."
```

---

### Task 2: Serialise a poll result to the status contract

Pure functions, no I/O, no network. This is the contract the page depends on, so it is worth testing hard and in isolation.

**Files:**
- Create: `webapp/__init__.py` (empty)
- Create: `webapp/report.py`
- Test: `webapp/test_report.py`

**Interfaces:**
- Consumes: `monitor.Shop`, `monitor.Seat`, `monitor.Fixture` (all already defined in `monitor.py`).
- Produces:
  - `webapp.report.tiers(shop, buyable) -> list[dict]` — one row per on-sale category, keys `category` (str), `price` (float), `available` (int), sorted cheapest first.
  - `webapp.report.cheapest(buyable, limit=10) -> list[dict]` — keys `price`, `section`, `row`, `seat`, `gate`, `category`.
  - `webapp.report.build_status(*, fixture, shop, url, buyable, counts, alerted, generated_at, alert_delivered=None) -> dict` — the full `status.json` document. Keyword-only. `fixture` may be `None` (the monitor only sets it in auto mode), which is why `url` is passed separately — `mon.api.event_url` is correct in both auto and pinned mode. `alert_delivered` is `None` when nothing needed sending, `True`/`False` otherwise.
  - `webapp.report.build_error_status(previous, generated_at, error) -> dict` — a status document carrying `error`, reusing the previous document's fixture and counts.

- [ ] **Step 1: Write the failing test**

Create `webapp/test_report.py`:

```python
"""Tests for the poller-to-page data contract.

The page is the only consumer, and it is deployed separately from the
poller, so this contract is the one thing that must not drift silently.
"""
import datetime
import unittest

import monitor
from webapp import report

NOW = datetime.datetime(2026, 9, 17, 18, 40, tzinfo=datetime.timezone.utc)

FIXTURE = monitor.Fixture(
    name='מכבי נתניה - מכבי ת"א',
    url="https://tickets.leaan.net/event/--02j286",
    start=datetime.datetime(2026, 9, 19, 17, 0, tzinfo=datetime.timezone.utc),
    end=datetime.datetime(2026, 9, 19, 19, 0, tzinfo=datetime.timezone.utc),
    venue="אצטדיון נתניה",
    starting_price=75,
    max_per_order=1,
)

SHOP = monitor.Shop(
    event_id="e1", seating_event_id="se1", revision_id="r1",
    event_name='מכבי נתניה - מכבי ת"א', event_start="2026-09-19T17:00:00.000Z",
    sale_status="onSale", max_per_order=1, allowed_contingents=frozenset(),
    sellable={"catA": ("אי פלוס", 155), "catB": ("מזרחי עליון", 75)},
)

COUNTS = {"buyable": 2, "free_not_on_sale": 6409, "blocked_by_contingent": 771,
          "booked": 5823, "reserved": 40, "notforsale": 0, "other": 0}


def seat(sid, price, category, section="F"):
    return monitor.Seat(sid, section, "1", "1", "F", category, price)


class TestTiers(unittest.TestCase):
    def test_counts_available_seats_per_category(self):
        buyable = [seat("s1", 155, "אי פלוס"), seat("s2", 155, "אי פלוס")]
        rows = report.tiers(SHOP, buyable)
        by_name = {r["category"]: r for r in rows}
        self.assertEqual(by_name["אי פלוס"]["available"], 2)

    def test_on_sale_category_with_no_seats_is_shown_as_zero(self):
        """A sold-out tier must stay visible, not vanish from the page."""
        rows = report.tiers(SHOP, [seat("s1", 155, "אי פלוס")])
        by_name = {r["category"]: r for r in rows}
        self.assertIn("מזרחי עליון", by_name)
        self.assertEqual(by_name["מזרחי עליון"]["available"], 0)

    def test_cheapest_tier_first(self):
        rows = report.tiers(SHOP, [])
        self.assertEqual([r["price"] for r in rows], [75, 155])

    def test_no_sellable_categories_gives_an_empty_list(self):
        empty = monitor.dataclasses.replace(SHOP, sellable={})
        self.assertEqual(report.tiers(empty, []), [])


class TestCheapest(unittest.TestCase):
    def test_carries_the_full_seat_location(self):
        row = report.cheapest([seat("s1", 155, "אי פלוס", section="F")])[0]
        self.assertEqual(row, {"price": 155, "section": "F", "row": "1",
                               "seat": "1", "gate": "F", "category": "אי פלוס"})

    def test_caps_the_list(self):
        many = [seat(f"s{i}", 155, "אי פלוס") for i in range(50)]
        self.assertEqual(len(report.cheapest(many, limit=10)), 10)

    def test_preserves_engine_order(self):
        """compute_buyable already sorts cheapest-first; do not re-sort."""
        given = [seat("a", 75, "מזרחי עליון"), seat("b", 155, "אי פלוס")]
        self.assertEqual([r["price"] for r in report.cheapest(given)], [75, 155])


class TestBuildStatus(unittest.TestCase):
    def status(self, buyable=None, alerted=0, fixture=FIXTURE,
               alert_delivered=None):
        return report.build_status(
            fixture=fixture, shop=SHOP, url=FIXTURE.url,
            buyable=buyable if buyable is not None else [seat("s1", 155, "אי פלוס")],
            counts=COUNTS, alerted=alerted, generated_at=NOW,
            alert_delivered=alert_delivered)

    def test_documents_the_fixture(self):
        f = self.status()["fixture"]
        self.assertEqual(f["url"], "https://tickets.leaan.net/event/--02j286")
        self.assertEqual(f["venue"], "אצטדיון נתניה")
        self.assertEqual(f["start"], "2026-09-19T17:00:00+00:00")

    def test_falls_back_to_the_shop_when_no_fixture_was_discovered(self):
        """In pinned mode the monitor never sets .fixture; the page still needs
        a name and a working link."""
        f = self.status(fixture=None)["fixture"]
        self.assertEqual(f["name"], SHOP.event_name)
        self.assertEqual(f["url"], FIXTURE.url)
        self.assertEqual(f["start"], SHOP.event_start)

    def test_alert_delivery_is_null_when_nothing_needed_sending(self):
        self.assertIsNone(self.status()["alert_delivered"])

    def test_a_failed_telegram_delivery_is_recorded(self):
        """Telegram is the only channel; a silent non-delivery is the worst
        outcome available, so the page has to be able to say so."""
        self.assertIs(self.status(alerted=3, alert_delivered=False)
                      ["alert_delivered"], False)

    def test_generated_at_is_iso_with_offset(self):
        self.assertEqual(self.status()["generated_at"], "2026-09-17T18:40:00+00:00")

    def test_carries_sale_state_and_purchase_limit(self):
        s = self.status()
        self.assertEqual(s["sale_status"], "onSale")
        self.assertEqual(s["max_per_order"], 1)

    def test_buyable_count_matches_the_seat_list(self):
        s = self.status(buyable=[seat("a", 155, "אי פלוס"),
                                 seat("b", 155, "אי פלוס")])
        self.assertEqual(s["buyable"], 2)

    def test_error_is_null_on_a_good_poll(self):
        self.assertIsNone(self.status()["error"])

    def test_no_secret_can_reach_the_page(self):
        """status.json is world-readable; assert the shape cannot carry one."""
        import json as _json
        blob = _json.dumps(self.status(), ensure_ascii=False)
        for word in ("token", "bot_token", "chat_id", "telegram"):
            self.assertNotIn(word, blob.lower())


class TestBuildErrorStatus(unittest.TestCase):
    def prev(self, buyable=(), alerted=0):
        return report.build_status(fixture=FIXTURE, shop=SHOP, url=FIXTURE.url,
                                   buyable=list(buyable), counts=COUNTS,
                                   alerted=alerted, generated_at=NOW)

    def test_records_the_error(self):
        later = NOW + datetime.timedelta(minutes=5)
        s = report.build_error_status(self.prev(), later, "status feed timed out")
        self.assertEqual(s["error"], "status feed timed out")

    def test_keeps_the_previous_fixture_and_counts(self):
        """A failed poll must not erase what we last knew."""
        prev = self.prev(buyable=[seat("s1", 155, "אי פלוס")], alerted=1)
        s = report.build_error_status(prev, NOW, "boom")
        self.assertEqual(s["fixture"], prev["fixture"])
        self.assertEqual(s["counts"], prev["counts"])
        self.assertEqual(s["buyable"], prev["buyable"])

    def test_advances_generated_at_so_staleness_stays_truthful(self):
        """The page measures age from generated_at; a failure is still a check."""
        later = NOW + datetime.timedelta(minutes=5)
        s = report.build_error_status(self.prev(), later, "boom")
        self.assertEqual(s["generated_at"], later.isoformat())

    def test_works_with_no_previous_document_at_all(self):
        """First ever run can fail before anything was written."""
        s = report.build_error_status(None, NOW, "boom")
        self.assertEqual(s["error"], "boom")
        self.assertIsNone(s["fixture"])
        self.assertEqual(s["buyable"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest webapp.test_report -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'webapp'`.

- [ ] **Step 3: Write minimal implementation**

Create `webapp/__init__.py` as an empty file.

Create `webapp/report.py`:

```python
"""Turn one poll into the JSON document the page reads.

Pure functions only. The page is deployed separately from the poller, so
this module is the contract between them and the only place its shape is
decided.
"""
from __future__ import annotations

import collections


def tiers(shop, buyable) -> list:
    """Availability per on-sale category, cheapest first.

    A category that is on sale but currently has nothing available is
    reported with available=0 rather than omitted: "sold out" and "not on
    sale" mean different things to someone deciding whether to keep waiting.
    """
    available = collections.Counter(s.category for s in buyable)
    rows = {}
    for name, price in shop.sellable.values():
        rows[(name, price)] = {"category": name, "price": price,
                               "available": available.get(name, 0)}
    return sorted(rows.values(), key=lambda r: (r["price"], r["category"]))


def cheapest(buyable, limit: int = 10) -> list:
    """The first `limit` seats, in the order the engine ranked them.

    compute_buyable already sorts cheapest-first; re-sorting here would risk
    disagreeing with the alert text about which seat is the best one.
    """
    return [{"price": s.price, "section": s.section, "row": s.row,
             "seat": s.seat, "gate": s.gate, "category": s.category}
            for s in buyable[:limit]]


def build_status(*, fixture, shop, url, buyable, counts, alerted,
                 generated_at, alert_delivered=None) -> dict:
    """The full status document for a successful poll.

    `fixture` may be None: the monitor only discovers one in auto mode, and a
    pinned event has none. `url` is passed separately because
    `Api.event_url` is correct in both modes, and the page's only call to
    action depends on it.
    """
    return {
        "generated_at": generated_at.isoformat(),
        "fixture": {
            "name": fixture.name if fixture else shop.event_name,
            "url": url,
            "start": fixture.start.isoformat() if fixture else shop.event_start,
            "venue": fixture.venue if fixture else "",
        },
        "sale_status": shop.sale_status,
        "max_per_order": shop.max_per_order,
        "buyable": len(buyable),
        "counts": dict(counts),
        "tiers": tiers(shop, buyable),
        "cheapest": cheapest(buyable),
        "alerted": alerted,
        "alert_delivered": alert_delivered,
        "error": None,
    }


def build_error_status(previous, generated_at, error: str) -> dict:
    """A status document for a poll that failed.

    Everything we last knew is carried forward and `error` is set, so the
    page shows a failure rather than stale success. generated_at still
    advances, because a failed check is a check: staleness measures when we
    last tried, not when we last succeeded.
    """
    base = dict(previous or {})
    base.setdefault("fixture", None)
    base.setdefault("sale_status", "")
    base.setdefault("max_per_order", 0)
    base.setdefault("buyable", 0)
    base.setdefault("counts", {})
    base.setdefault("tiers", [])
    base.setdefault("cheapest", [])
    base.setdefault("alerted", 0)
    base.setdefault("alert_delivered", None)
    base["generated_at"] = generated_at.isoformat()
    base["error"] = error
    return base
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest webapp.test_report -v`
Expected: PASS, 20 tests.

Run: `python -m unittest test_monitor webapp.test_report`
Expected: PASS, 76 tests total (56 from Task 1 + 20).

- [ ] **Step 5: Commit**

```bash
git add webapp/__init__.py webapp/report.py webapp/test_report.py
git commit -m "Serialise a poll result to the page's status contract

Pure functions, no I/O. A sold-out tier stays visible with available=0
because 'sold out' and 'not on sale' mean different things to someone
deciding whether to keep waiting. A failed poll carries the last known
fixture and counts forward with error set, and still advances
generated_at: staleness measures when we last tried."
```

---

### Task 3: Telegram delivery with retry and a daily heartbeat

**Files:**
- Create: `webapp/alerts.py`
- Test: `webapp/test_alerts.py`

**Interfaces:**
- Consumes: `monitor.telegram(cfg, text) -> bool` as the default sender.
- Produces:
  - `webapp.alerts.send(cfg, text, attempts=3, sender=None, sleep=None) -> bool`
  - `webapp.alerts.alert_text(status, seats) -> str` — `status` is a `build_status` dict, `seats` a list of `monitor.Seat`.
  - `webapp.alerts.heartbeat_text(status) -> str`
  - `webapp.alerts.heartbeat_due(last_sent, today) -> bool` — both arguments are `YYYY-MM-DD` strings; `last_sent` may be `None` or `""`.

- [ ] **Step 1: Write the failing test**

Create `webapp/test_alerts.py`:

```python
"""Tests for alert delivery.

Telegram is the only channel now that the monitor is hosted, so a delivery
that fails quietly is the worst outcome available.
"""
import datetime
import unittest

import monitor
from webapp import alerts, report

NOW = datetime.datetime(2026, 9, 17, 18, 40, tzinfo=datetime.timezone.utc)

FIXTURE = monitor.Fixture(
    name='מכבי נתניה - מכבי ת"א',
    url="https://tickets.leaan.net/event/--02j286",
    start=datetime.datetime(2026, 9, 19, 17, 0, tzinfo=datetime.timezone.utc),
    end=datetime.datetime(2026, 9, 19, 19, 0, tzinfo=datetime.timezone.utc),
    venue="אצטדיון נתניה", starting_price=75, max_per_order=1)

SHOP = monitor.Shop(
    event_id="e1", seating_event_id="se1", revision_id="r1",
    event_name='מכבי נתניה - מכבי ת"א', event_start="2026-09-19T17:00:00.000Z",
    sale_status="onSale", max_per_order=1, allowed_contingents=frozenset(),
    sellable={"catA": ("אי פלוס", 155)})

COUNTS = {"buyable": 1, "free_not_on_sale": 0, "blocked_by_contingent": 0,
          "booked": 0, "reserved": 0, "notforsale": 0, "other": 0}


def seat(sid, price=155, category="אי פלוס"):
    return monitor.Seat(sid, "F", "1", "1", "F", category, price)


def status_doc(buyable):
    return report.build_status(fixture=FIXTURE, shop=SHOP, url=FIXTURE.url,
                               buyable=buyable, counts=COUNTS,
                               alerted=len(buyable), generated_at=NOW)


class Sender:
    """Records calls and returns a scripted sequence of outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.sent = []

    def __call__(self, cfg, text):
        self.sent.append(text)
        return self.outcomes.pop(0) if self.outcomes else False


class TestSend(unittest.TestCase):
    def test_returns_true_on_first_success_without_retrying(self):
        s = Sender([True])
        self.assertTrue(alerts.send({}, "hi", sender=s, sleep=lambda _: None))
        self.assertEqual(len(s.sent), 1)

    def test_retries_until_it_succeeds(self):
        s = Sender([False, False, True])
        self.assertTrue(alerts.send({}, "hi", sender=s, sleep=lambda _: None))
        self.assertEqual(len(s.sent), 3)

    def test_gives_up_after_the_attempt_budget(self):
        s = Sender([False, False, False])
        self.assertFalse(alerts.send({}, "hi", attempts=3, sender=s,
                                     sleep=lambda _: None))
        self.assertEqual(len(s.sent), 3)

    def test_backs_off_between_attempts(self):
        waits = []
        alerts.send({}, "hi", attempts=3, sender=Sender([False, False, True]),
                    sleep=waits.append)
        self.assertEqual(waits, [1, 2])

    def test_does_not_sleep_after_the_final_attempt(self):
        waits = []
        alerts.send({}, "hi", attempts=2, sender=Sender([False, False]),
                    sleep=waits.append)
        self.assertEqual(waits, [1])

    def test_an_exception_from_the_sender_is_a_failed_attempt_not_a_crash(self):
        def boom(cfg, text):
            raise RuntimeError("network down")
        self.assertFalse(alerts.send({}, "hi", attempts=2, sender=boom,
                                     sleep=lambda _: None))


class TestAlertText(unittest.TestCase):
    def text(self, n=3):
        seats = [seat(f"s{i}") for i in range(n)]
        return alerts.alert_text(status_doc(seats), seats)

    def test_leads_with_the_count_and_the_game(self):
        t = self.text()
        self.assertIn("3", t)
        self.assertIn('מכבי נתניה - מכבי ת"א', t)

    def test_carries_the_shop_link(self):
        self.assertIn("https://tickets.leaan.net/event/--02j286", self.text())

    def test_states_the_purchase_limit(self):
        self.assertIn("1", self.text())

    def test_lists_seats_but_not_all_of_them(self):
        t = self.text(n=400)
        self.assertLess(len(t), 4096, "Telegram rejects messages over 4096 chars")
        self.assertIn("more", t.lower())

    def test_singular_for_one_seat(self):
        self.assertNotIn("1 TICKETS", self.text(n=1))


class TestHeartbeat(unittest.TestCase):
    def test_due_when_never_sent(self):
        self.assertTrue(alerts.heartbeat_due(None, "2026-09-17"))
        self.assertTrue(alerts.heartbeat_due("", "2026-09-17"))

    def test_not_due_twice_in_one_day(self):
        self.assertFalse(alerts.heartbeat_due("2026-09-17", "2026-09-17"))

    def test_due_the_next_day(self):
        self.assertTrue(alerts.heartbeat_due("2026-09-17", "2026-09-18"))

    def test_text_says_what_is_being_watched_and_what_is_available(self):
        t = alerts.heartbeat_text(status_doc([seat("s1")]))
        self.assertIn('מכבי נתניה - מכבי ת"א', t)
        self.assertIn("1", t)

    def test_text_handles_a_failed_poll(self):
        doc = report.build_error_status(status_doc([]), NOW, "feed timed out")
        self.assertIn("feed timed out", alerts.heartbeat_text(doc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest webapp.test_alerts -v`
Expected: FAIL with `ImportError: cannot import name 'alerts' from 'webapp'`.

- [ ] **Step 3: Write minimal implementation**

Create `webapp/alerts.py`:

```python
"""Telegram delivery.

Hosting removed the always-on-top window that used to be the alert that
must not fail, so Telegram is now the only channel. It is treated
accordingly: retried, and never allowed to raise into the poller.
"""
from __future__ import annotations

import time

import monitor

MAX_TELEGRAM_CHARS = 4096
SEATS_IN_ALERT = 8


def send(cfg, text: str, attempts: int = 3, sender=None, sleep=None) -> bool:
    """Send, retrying with a short backoff. Never raises.

    A sender that throws counts as a failed attempt rather than an error:
    the poll that produced this alert has already succeeded and its result
    still needs writing.
    """
    sender = sender or monitor.telegram
    sleep = sleep or time.sleep
    for attempt in range(attempts):
        try:
            if sender(cfg, text):
                return True
        except Exception as exc:
            monitor.log(f"telegram attempt {attempt + 1} raised: {exc}")
        if attempt < attempts - 1:
            sleep(2 ** attempt)
    return False


def alert_text(status: dict, seats) -> str:
    """The message body for newly buyable seats."""
    fixture = status.get("fixture") or {}
    count = len(seats)
    noun = "TICKET" if count == 1 else "TICKETS"
    lines = [f"<b>{count} {noun} AVAILABLE - {fixture.get('name', '')}</b>"]
    for s in seats[:SEATS_IN_ALERT]:
        lines.append(f"• {s.describe()}")
    if count > SEATS_IN_ALERT:
        lines.append(f"• ...and {count - SEATS_IN_ALERT} more")
    lines.append("")
    lines.append(f"Max {status.get('max_per_order', 0)} per customer - go now:")
    lines.append(fixture.get("url", ""))
    text = "\n".join(lines)
    return text[:MAX_TELEGRAM_CHARS]


def heartbeat_text(status: dict) -> str:
    """The daily 'still alive' message.

    A page nobody is looking at cannot warn anybody, and a workflow that
    GitHub disabled for inactivity stops without telling you. This is the
    only thing that makes that silence audible.
    """
    fixture = status.get("fixture") or {}
    if status.get("error"):
        return (f"Ticket monitor is alive but the last poll FAILED:\n"
                f"{status['error']}")
    return (f"Ticket monitor alive. Watching {fixture.get('name', 'nothing')}"
            f" - {status.get('buyable', 0)} seat(s) buyable.")


def heartbeat_due(last_sent, today: str) -> bool:
    """True once per calendar day. Both arguments are YYYY-MM-DD strings."""
    return (last_sent or "") != today
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest webapp.test_alerts -v`
Expected: PASS, 16 tests.

- [ ] **Step 5: Commit**

```bash
git add webapp/alerts.py webapp/test_alerts.py
git commit -m "Telegram delivery with retry and a daily heartbeat

Hosting removed the always-on-top window that used to be the alert
that must not fail, so Telegram is now the only channel. A sender that
throws counts as a failed attempt rather than an error - the poll has
already succeeded and its result still needs writing."
```

---

### Task 4: The one-shot poller

Orchestration: read prior state, poll, alert, write both documents. The cron is the loop, so there is no loop here.

**Files:**
- Create: `webapp/poll.py`
- Test: `webapp/test_poll.py`

**Interfaces:**
- Consumes: `webapp.report.build_status`, `webapp.report.build_error_status`, `webapp.alerts.send`, `webapp.alerts.alert_text`, `webapp.alerts.heartbeat_text`, `webapp.alerts.heartbeat_due`, `monitor.Monitor`, `monitor.AlertState`, `monitor.below_price`, `monitor.EventOver`.
- Produces:
  - `webapp.poll.run_once(cfg, data_dir, now=None, monitor_factory=None, send=None) -> dict` — writes `<data_dir>/status.json` and `<data_dir>/state.json`, returns the status dict.
  - `webapp.poll.read_json(path, default=None) -> dict | None`
  - `python -m webapp.poll --data-dir DIR` as the CLI the workflow calls.

- [ ] **Step 1: Write the failing test**

Create `webapp/test_poll.py`:

```python
"""Tests for the one-shot poller.

No network: a fake Monitor stands in for the engine, which is already
covered by its own 51 tests.
"""
import datetime
import json
import pathlib
import shutil
import tempfile
import unittest

import monitor
from webapp import poll

NOW = datetime.datetime(2026, 9, 17, 18, 40, tzinfo=datetime.timezone.utc)

FIXTURE = monitor.Fixture(
    name='מכבי נתניה - מכבי ת"א',
    url="https://tickets.leaan.net/event/--02j286",
    start=datetime.datetime(2026, 9, 19, 17, 0, tzinfo=datetime.timezone.utc),
    end=datetime.datetime(2026, 9, 19, 19, 0, tzinfo=datetime.timezone.utc),
    venue="אצטדיון נתניה", starting_price=75, max_per_order=1)

OTHER_FIXTURE = monitor.dataclasses.replace(
    FIXTURE, name='מכבי ת"א - בני סכנין',
    url="https://tickets.leaan.net/event/--co3j4w")

SHOP = monitor.Shop(
    event_id="e1", seating_event_id="se1", revision_id="r1",
    event_name='מכבי נתניה - מכבי ת"א', event_start="2026-09-19T17:00:00.000Z",
    sale_status="onSale", max_per_order=1, allowed_contingents=frozenset(),
    sellable={"catA": ("אי פלוס", 155)})

COUNTS = {"buyable": 0, "free_not_on_sale": 0, "blocked_by_contingent": 0,
          "booked": 0, "reserved": 0, "notforsale": 0, "other": 0}


def seat(sid, price=155):
    return monitor.Seat(sid, "F", "1", "1", "F", "אי פלוס", price)


class FakeMonitor:
    """Stands in for monitor.Monitor. Never touches the network."""
    seats_to_return = []
    fixture_to_return = FIXTURE
    raise_on_refresh = None

    def __init__(self, cfg):
        self.cfg = cfg
        self.shop = SHOP
        self.fixture = type(self).fixture_to_return

        class _Api:
            event_url = type(self).fixture_to_return.url
        self.api = _Api()

    def refresh_config(self, status_json=None):
        if type(self).raise_on_refresh:
            raise type(self).raise_on_refresh
        return {"status": {}}

    def check(self, status_json):
        seats = list(type(self).seats_to_return)
        counts = dict(COUNTS, buyable=len(seats))
        return seats, counts


class Recorder:
    def __init__(self, ok=True):
        self.ok = ok
        self.messages = []

    def __call__(self, cfg, text, **kw):
        self.messages.append(text)
        return self.ok


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        FakeMonitor.seats_to_return = []
        FakeMonitor.fixture_to_return = FIXTURE
        FakeMonitor.raise_on_refresh = None
        self.sent = Recorder()

    def run_once(self, cfg=None, now=NOW):
        return poll.run_once(cfg or {"alert_below_price": None},
                             self.dir, now=now,
                             monitor_factory=FakeMonitor, send=self.sent)

    def read(self, name):
        return json.loads((self.dir / name).read_text(encoding="utf-8"))


class TestWritesDocuments(Base):
    def test_writes_status_and_state(self):
        self.run_once()
        self.assertTrue((self.dir / "status.json").exists())
        self.assertTrue((self.dir / "state.json").exists())

    def test_status_reflects_the_poll(self):
        FakeMonitor.seats_to_return = [seat("a"), seat("b")]
        self.run_once()
        self.assertEqual(self.read("status.json")["buyable"], 2)

    def test_creates_the_data_directory_if_absent(self):
        nested = self.dir / "nope" / "deeper"
        poll.run_once({"alert_below_price": None}, nested, now=NOW,
                      monitor_factory=FakeMonitor, send=self.sent)
        self.assertTrue((nested / "status.json").exists())


class TestAlerting(Base):
    def test_alerts_on_newly_buyable_seats(self):
        FakeMonitor.seats_to_return = [seat("a")]
        self.run_once()
        self.assertTrue(any("AVAILABLE" in m for m in self.sent.messages))

    def test_does_not_alert_twice_for_the_same_seats(self):
        FakeMonitor.seats_to_return = [seat("a")]
        self.run_once()
        self.sent.messages.clear()
        self.run_once(now=NOW + datetime.timedelta(minutes=5))
        self.assertEqual([m for m in self.sent.messages if "AVAILABLE" in m], [])

    def test_alerts_again_for_a_seat_that_came_back(self):
        FakeMonitor.seats_to_return = [seat("a")]
        self.run_once()
        FakeMonitor.seats_to_return = []
        self.run_once(now=NOW + datetime.timedelta(minutes=5))
        self.sent.messages.clear()
        FakeMonitor.seats_to_return = [seat("a")]
        self.run_once(now=NOW + datetime.timedelta(minutes=10))
        self.assertTrue(any("AVAILABLE" in m for m in self.sent.messages))

    def test_no_seats_sends_nothing(self):
        self.run_once()
        self.assertEqual([m for m in self.sent.messages if "AVAILABLE" in m], [])

    def test_price_filter_suppresses_the_alert_but_not_the_count(self):
        FakeMonitor.seats_to_return = [seat("a", price=155)]
        self.run_once(cfg={"alert_below_price": 100})
        self.assertEqual([m for m in self.sent.messages if "AVAILABLE" in m], [])
        self.assertEqual(self.read("status.json")["buyable"], 1)

    def test_records_how_many_were_alerted(self):
        FakeMonitor.seats_to_return = [seat("a"), seat("b")]
        self.run_once()
        self.assertEqual(self.read("status.json")["alerted"], 2)

    def test_records_that_the_alert_was_delivered(self):
        FakeMonitor.seats_to_return = [seat("a")]
        self.run_once()
        self.assertIs(self.read("status.json")["alert_delivered"], True)

    def test_records_a_delivery_failure(self):
        """Telegram is the only channel; the page must be able to say it
        did not get through."""
        FakeMonitor.seats_to_return = [seat("a")]
        self.sent = Recorder(ok=False)
        self.run_once()
        self.assertIs(self.read("status.json")["alert_delivered"], False)

    def test_delivery_flag_is_null_when_there_was_nothing_to_send(self):
        self.run_once()
        self.assertIsNone(self.read("status.json")["alert_delivered"])


class TestFixtureRollover(Base):
    def test_state_is_dropped_and_seats_realert_on_a_new_fixture(self):
        FakeMonitor.seats_to_return = [seat("a")]
        self.run_once()
        self.sent.messages.clear()
        FakeMonitor.fixture_to_return = OTHER_FIXTURE
        self.run_once(now=NOW + datetime.timedelta(days=1))
        self.assertTrue(any("AVAILABLE" in m for m in self.sent.messages))
        self.assertEqual(self.read("state.json")["event"], OTHER_FIXTURE.url)


class TestFailure(Base):
    def test_a_failed_poll_writes_the_error(self):
        FakeMonitor.raise_on_refresh = RuntimeError("status feed timed out")
        self.run_once()
        self.assertIn("timed out", self.read("status.json")["error"])

    def test_a_failed_poll_preserves_the_last_known_counts(self):
        FakeMonitor.seats_to_return = [seat("a")]
        self.run_once()
        FakeMonitor.raise_on_refresh = RuntimeError("boom")
        self.run_once(now=NOW + datetime.timedelta(minutes=5))
        self.assertEqual(self.read("status.json")["buyable"], 1)

    def test_a_failed_poll_still_advances_generated_at(self):
        self.run_once()
        later = NOW + datetime.timedelta(minutes=5)
        FakeMonitor.raise_on_refresh = RuntimeError("boom")
        self.run_once(now=later)
        self.assertEqual(self.read("status.json")["generated_at"],
                         later.isoformat())

    def test_a_failed_poll_does_not_destroy_alert_state(self):
        """Losing state would re-alert every seat on the next good poll."""
        FakeMonitor.seats_to_return = [seat("a")]
        self.run_once()
        FakeMonitor.raise_on_refresh = RuntimeError("boom")
        self.run_once(now=NOW + datetime.timedelta(minutes=5))
        self.assertEqual(self.read("state.json")["alerted"], ["a"])

    def test_run_once_does_not_raise(self):
        """The workflow must still publish a document when the poll fails."""
        FakeMonitor.raise_on_refresh = RuntimeError("boom")
        self.assertIsInstance(self.run_once(), dict)


class TestHeartbeat(Base):
    def test_sends_once_a_day(self):
        self.run_once()
        self.assertTrue(any("alive" in m.lower() for m in self.sent.messages))

    def test_not_sent_again_the_same_day(self):
        self.run_once()
        self.sent.messages.clear()
        self.run_once(now=NOW + datetime.timedelta(minutes=5))
        self.assertEqual([m for m in self.sent.messages if "alive" in m.lower()], [])

    def test_sent_again_the_next_day(self):
        self.run_once()
        self.sent.messages.clear()
        self.run_once(now=NOW + datetime.timedelta(days=1))
        self.assertTrue(any("alive" in m.lower() for m in self.sent.messages))

    def test_recorded_in_state(self):
        self.run_once()
        self.assertEqual(self.read("state.json")["last_heartbeat"], "2026-09-17")


class TestNoSecretsLeak(Base):
    def test_credentials_never_reach_either_document(self):
        cfg = {"alert_below_price": None,
               "telegram": {"bot_token": "SECRET-TOKEN", "chat_id": "99"}}
        FakeMonitor.seats_to_return = [seat("a")]
        self.run_once(cfg=cfg)
        for name in ("status.json", "state.json"):
            blob = (self.dir / name).read_text(encoding="utf-8")
            self.assertNotIn("SECRET-TOKEN", blob)
            self.assertNotIn("99", blob.replace('"alerted": 1', ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest webapp.test_poll -v`
Expected: FAIL with `ImportError: cannot import name 'poll' from 'webapp'`.

- [ ] **Step 3: Write minimal implementation**

Create `webapp/poll.py`:

```python
"""One poll, then exit. The cron schedule is the loop.

Everything here is orchestration: the engine decides what is buyable, this
module decides what is new, tells Telegram, and writes the two documents
the page and the next run depend on.
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

import monitor
from webapp import alerts, report


def read_json(path: pathlib.Path, default=None):
    """Read a JSON file, treating absent or corrupt as `default`.

    A corrupt state file must not stop the run: the cost is re-alerting
    seats we already announced, which is far better than not running.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: pathlib.Path, document: dict):
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def run_once(cfg, data_dir, now=None, monitor_factory=None, send=None) -> dict:
    """Poll once; write status.json and state.json; return the status.

    Never raises. The workflow's job is to publish a document every run, and
    a run that failed is exactly when the page most needs to say so.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    data_dir = pathlib.Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    monitor_factory = monitor_factory or monitor.Monitor
    send = send or alerts.send

    status_path, state_path = data_dir / "status.json", data_dir / "state.json"
    previous = read_json(status_path)
    stored = read_json(state_path, {}) or {}
    state = monitor.AlertState(stored.get("alerted") or [],
                               stored.get("event") or "")
    last_heartbeat = stored.get("last_heartbeat") or ""

    try:
        mon = monitor_factory(cfg)
        try:
            status_json = mon.refresh_config()
        except monitor.EventOver as exc:
            mon._event_over(exc)
            status_json = mon.refresh_config()
        buyable, counts = mon.check(status_json)

        state = state.for_event(mon.api.event_url)
        fresh = state.new_among(buyable)
        alertable = monitor.below_price(fresh, cfg.get("alert_below_price"))

        status = report.build_status(
            fixture=mon.fixture, shop=mon.shop, url=mon.api.event_url,
            buyable=buyable, counts=counts, alerted=len(alertable),
            generated_at=now)

        if alertable:
            # Telegram is the only channel now, so whether it actually got
            # through is part of the status, not just a log line.
            status["alert_delivered"] = bool(
                send(cfg, alerts.alert_text(status, alertable)))
    except Exception as exc:
        monitor.log(f"poll failed: {exc}")
        status = report.build_error_status(previous, now, str(exc))

    today = now.date().isoformat()
    if alerts.heartbeat_due(last_heartbeat, today):
        send(cfg, alerts.heartbeat_text(status))
        last_heartbeat = today

    _write(status_path, status)
    _write(state_path, {"event": state.event, "alerted": sorted(state.alerted),
                        "updated": now.isoformat(),
                        "last_heartbeat": last_heartbeat})
    return status


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run one poll and publish it.")
    ap.add_argument("--data-dir", default="data",
                    help="where status.json and state.json are written")
    args = ap.parse_args(argv)

    status = run_once(monitor.load_config(), args.data_dir)
    if status.get("error"):
        monitor.log(f"published a FAILED poll: {status['error']}")
    else:
        monitor.log(f"published: {status['buyable']} buyable, "
                    f"{status['alerted']} alerted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest webapp.test_poll -v`
Expected: PASS, 25 tests.

Run: `python -m unittest test_monitor webapp.test_report webapp.test_alerts webapp.test_poll`
Expected: PASS, 117 tests total (56 + 20 + 16 + 25).

- [ ] **Step 5: Verify it works against the live site once**

Run: `python -m webapp.poll --data-dir /tmp/tm && cat /tmp/tm/status.json`
Expected: a status document naming the next fixture with a plausible `buyable` count. With no Telegram credentials in the environment, `monitor.telegram` returns `False` and `alerts.send` gives up after three attempts — that is correct behaviour, not a failure of the run.

- [ ] **Step 6: Commit**

```bash
git add webapp/poll.py webapp/test_poll.py
git commit -m "One-shot poller: alert, then publish both documents

The cron schedule is the loop, so there is no loop here. run_once never
raises: the workflow's job is to publish a document every run, and a run
that failed is exactly when the page most needs to say so. A failed poll
keeps the alert state, because losing it would re-alert every seat on
the next good poll."
```

---

### Task 5: The page

**Files:**
- Create: `site/index.html`
- Create: `site/style.css`
- Create: `site/freshness.js`
- Create: `site/app.js`
- Test: `site/freshness.test.js`

**Interfaces:**
- Consumes: `status.json` as defined in Task 2.
- Produces: `freshness(ageSeconds) -> "ok" | "late" | "down"`, exported from `site/freshness.js` as an ES module and imported by both `app.js` and the test.

- [ ] **Step 1: Write the failing test**

Create `site/freshness.test.js`:

```js
// The staleness thresholds are the page's only real logic, and they are
// what stops a dead monitor from looking like a quiet one.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { freshness } from './freshness.js';

test('a recent check is normal', () => {
  assert.equal(freshness(0), 'ok');
  assert.equal(freshness(120), 'ok');
  assert.equal(freshness(599), 'ok');
});

test('past ten minutes the cron is running late', () => {
  assert.equal(freshness(600), 'late');
  assert.equal(freshness(1799), 'late');
});

test('past thirty minutes the monitor may be down', () => {
  assert.equal(freshness(1800), 'down');
  assert.equal(freshness(86400), 'down');
});

test('a clock skewed into the future is not treated as stale', () => {
  assert.equal(freshness(-30), 'ok');
});

test('an unusable age is treated as down, never as fresh', () => {
  // Erring towards "ok" here would hide a broken monitor behind a green badge.
  assert.equal(freshness(NaN), 'down');
  assert.equal(freshness(null), 'down');
  assert.equal(freshness(undefined), 'down');
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test "site/**/*.test.js"`
Expected: FAIL — cannot find module `./freshness.js`.

- [ ] **Step 3: Write minimal implementation**

Create `site/freshness.js`:

```js
// Cron is scheduled every 5 minutes but GitHub delays runs under load, so
// "late" is expected and only "down" is alarming.
export const LATE_AFTER_SECONDS = 600;    // 10 min
export const DOWN_AFTER_SECONDS = 1800;   // 30 min

export function freshness(ageSeconds) {
  if (typeof ageSeconds !== 'number' || Number.isNaN(ageSeconds)) return 'down';
  if (ageSeconds < LATE_AFTER_SECONDS) return 'ok';
  if (ageSeconds < DOWN_AFTER_SECONDS) return 'late';
  return 'down';
}
```

Create `site/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ticket Monitor</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<main class="card" id="card" aria-live="polite">
  <p class="loading" id="loading">Loading…</p>
</main>
<script type="module" src="app.js"></script>
</body>
</html>
```

Create `site/style.css`:

```css
:root {
  --bg: #0f1115; --card: #171a21; --line: #272b35;
  --text: #e8eaed; --dim: #9aa0aa;
  --ok: #3fb950; --late: #d29922; --down: #f85149; --accent: #1f6feb;
}
@media (prefers-color-scheme: light) {
  :root { --bg: #f6f7f9; --card: #fff; --line: #e3e5e9;
          --text: #1a1d23; --dim: #6b7280; }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px; background: var(--bg); color: var(--text);
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  display: flex; justify-content: center;
}
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 14px;
  padding: 20px; width: 100%; max-width: 460px;
}
h1 { font-size: 20px; margin: 0 0 4px; }
.when { color: var(--dim); font-size: 14px; margin: 0 0 18px; }
.count { font-size: 46px; font-weight: 700; text-align: center; margin: 14px 0 2px; }
.count-label { text-align: center; color: var(--dim); margin: 0 0 18px; }
table { width: 100%; border-collapse: collapse; margin-bottom: 18px; }
td { padding: 7px 0; border-top: 1px solid var(--line); }
td.num { text-align: right; color: var(--dim); }
td.has { text-align: right; color: var(--ok); font-weight: 600; }
.cta {
  display: block; text-align: center; padding: 13px; border-radius: 10px;
  background: var(--accent); color: #fff; text-decoration: none; font-weight: 600;
}
.cta[aria-disabled="true"] { background: var(--line); color: var(--dim); }
.note { color: var(--dim); font-size: 13px; text-align: center; margin: 10px 0 0; }
.status { margin: 16px 0 0; font-size: 13px; text-align: center; }
.status.ok { color: var(--dim); }
.status.late { color: var(--late); }
.status.down { color: var(--down); font-weight: 600; }
.error {
  margin: 14px 0 0; padding: 10px; border-radius: 8px;
  background: rgba(248, 81, 73, .12); color: var(--down); font-size: 13px;
}
```

Create `site/app.js`:

```js
import { freshness } from './freshness.js';

// The page is served from GitHub Pages but the data lives on the `data`
// branch, fetched straight from raw.githubusercontent.com (which sends
// Access-Control-Allow-Origin: *). That keeps Pages from redeploying every
// five minutes just because a number changed.
const DATA_URL =
  'https://raw.githubusercontent.com/AssafAtias/ticket-monitor/data/status.json';
const REFRESH_MS = 30000;

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

function ago(seconds) {
  if (seconds < 90) return `${Math.max(0, Math.round(seconds))}s ago`;
  const mins = Math.round(seconds / 60);
  if (mins < 90) return `${mins} min ago`;
  return `${Math.round(mins / 60)} h ago`;
}

const STATUS_TEXT = {
  ok: age => `checked ${ago(age)}`,
  late: age => `checked ${ago(age)} — the scheduler is running late`,
  down: age => `LAST CHECKED ${ago(age)} — THE MONITOR MAY BE DOWN`,
};

function render(status, ageSeconds) {
  const card = document.getElementById('card');
  card.replaceChildren();

  const fixture = status.fixture || {};
  card.append(el('h1', null, fixture.name || 'No fixture'));

  if (fixture.start) {
    const kickoff = new Date(fixture.start);
    const when = kickoff.toLocaleString(undefined, {
      weekday: 'short', day: 'numeric', month: 'short',
      hour: '2-digit', minute: '2-digit',
    });
    card.append(el('p', 'when', fixture.venue ? `${when} · ${fixture.venue}` : when));
  }

  card.append(el('p', 'count', String(status.buyable ?? 0)));
  card.append(el('p', 'count-label',
    status.buyable === 1 ? 'seat buyable' : 'seats buyable'));

  if (status.tiers && status.tiers.length) {
    const table = el('table');
    for (const tier of status.tiers) {
      const tr = el('tr');
      tr.append(el('td', null, tier.category));
      tr.append(el('td', 'num', `${tier.price} ₪`));
      tr.append(el('td', tier.available ? 'has' : 'num',
        tier.available ? `${tier.available}` : 'sold out'));
      table.append(tr);
    }
    card.append(table);
  }

  if (fixture.url) {
    const cta = el('a', 'cta', 'Open the shop →');
    cta.href = fixture.url;
    cta.rel = 'noopener';
    cta.target = '_blank';
    card.append(cta);
  }

  if (status.max_per_order) {
    card.append(el('p', 'note',
      `max ${status.max_per_order} per customer · sign in before it fires`));
  }

  if (status.error) {
    card.append(el('p', 'error', `Last poll failed: ${status.error}`));
  }

  if (status.alert_delivered === false) {
    // Telegram is the only channel. If it failed, the seats in this card may
    // never have been announced anywhere.
    card.append(el('p', 'error',
      'The Telegram alert for these seats FAILED to send.'));
  }

  const level = freshness(ageSeconds);
  card.append(el('p', `status ${level}`, STATUS_TEXT[level](ageSeconds)));
  document.title = `${status.buyable ?? 0} · Ticket Monitor`;
}

function renderUnreachable(message) {
  const card = document.getElementById('card');
  card.replaceChildren();
  card.append(el('h1', null, 'Cannot reach the monitor'));
  card.append(el('p', 'error', message));
  card.append(el('p', 'status down',
    'This page could not load status.json. The monitor itself may still be running.'));
}

async function tick() {
  try {
    // Cache-bust: raw.githubusercontent.com caches for about five minutes.
    const resp = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const status = await resp.json();
    const age = (Date.now() - new Date(status.generated_at).getTime()) / 1000;
    render(status, age);
  } catch (err) {
    renderUnreachable(String(err.message || err));
  }
}

tick();
setInterval(tick, REFRESH_MS);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test "site/**/*.test.js"`
Expected: PASS, 5 tests.

- [ ] **Step 5: Check the page renders against a real document**

Run:

```bash
python -m webapp.poll --data-dir site
python -m http.server 8000 --directory site
```

Open `http://localhost:8000`. The card shows the fixture and a count. It will
also show "Cannot reach the monitor" for the remote URL until the `data`
branch exists in Task 6 — that is expected at this stage. Delete the local
copies afterwards so they are not committed:

```bash
rm -f site/status.json site/state.json
```

- [ ] **Step 6: Commit**

```bash
git add site/
git commit -m "The page: one card, honest about staleness

Freshness is computed from the document's own generated_at, never the
browser's idea of when it loaded. An unusable age reads as 'down'
rather than 'ok', because erring towards green would hide a broken
monitor behind a reassuring badge."
```

---

### Task 6: The poller workflow and the data branch

**Files:**
- Create: `.github/workflows/monitor.yml`

**Interfaces:**
- Consumes: `python -m webapp.poll --data-dir data` from Task 4.
- Produces: an orphan `data` branch holding `status.json` and `state.json`, force-pushed each run, readable at `https://raw.githubusercontent.com/AssafAtias/ticket-monitor/data/status.json`.

- [ ] **Step 1: Make the repository public and add the secrets**

Actions minutes are unlimited on public repos and metered on private ones, so this must happen before the schedule is enabled or the first 2,000 minutes will be consumed and then billed.

```bash
gh repo edit AssafAtias/ticket-monitor --visibility public --accept-visibility-change-consequences
gh secret set TELEGRAM_BOT_TOKEN --repo AssafAtias/ticket-monitor
gh secret set TELEGRAM_CHAT_ID  --repo AssafAtias/ticket-monitor
```

Take both values from the local `config.json` — which by now is the only place they exist, and is no longer committed with them.

Verify: `gh repo view AssafAtias/ticket-monitor --json visibility` reports `PUBLIC`, and `gh secret list --repo AssafAtias/ticket-monitor` lists both names.

- [ ] **Step 2: Write the workflow**

Create `.github/workflows/monitor.yml`:

```yaml
name: monitor

on:
  schedule:
    # Every 5 minutes is the GitHub minimum. Runs are frequently delayed
    # under load; the page reports true age, so lateness is visible.
    - cron: '*/5 * * * *'
  workflow_dispatch:

# Only one poll at a time. Overlapping runs would race on the data branch
# and could re-alert seats that the other run already announced.
concurrency:
  group: monitor
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  poll:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      # The seat map is ~4.6MB and changes only when the venue's revision
      # changes. Restoring it keeps us from re-downloading it every 5 minutes.
      - uses: actions/cache/restore@v4
        id: mapcache
        with:
          path: .cache
          key: seatmap-
          restore-keys: seatmap-

      - name: Fetch the previous state
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          mkdir -p data
          # Read state over git, not raw.githubusercontent.com: raw is CDN
          # cached for ~5 minutes, and a stale state file would re-alert
          # seats we already announced.
          if git fetch --depth=1 origin data 2>/dev/null; then
            git show FETCH_HEAD:state.json > data/state.json || echo '{}' > data/state.json
            git show FETCH_HEAD:status.json > data/status.json || true
          else
            echo "no data branch yet - first run"
            echo '{}' > data/state.json
          fi

      - name: Poll
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          PYTHONIOENCODING: utf-8
        run: python -m webapp.poll --data-dir data

      - name: Publish to the data branch
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          # A fresh orphan commit force-pushed each run: the branch always
          # holds exactly one commit, so 8,640 pushes a month never grow
          # the repository.
          work="$(mktemp -d)"
          cp data/status.json data/state.json "$work/"
          cd "$work"
          git init -q -b data
          git add -A
          git -c user.name='ticket-monitor' \
              -c user.email='actions@github.com' \
              commit -q -m "data: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
          git push -q --force \
            "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" \
            data

      - name: Compute the seat-map cache key
        id: mapkey
        run: |
          # The cached filename encodes seating event id and revision, so the
          # key is stable for a fixture and changes when the venue's map does.
          fingerprint="$(ls .cache 2>/dev/null | sort | sha256sum | cut -c1-16)"
          echo "key=seatmap-${fingerprint}" >> "$GITHUB_OUTPUT"

      - uses: actions/cache/save@v4
        if: steps.mapkey.outputs.key != 'seatmap-'
        continue-on-error: true
        with:
          path: .cache
          key: ${{ steps.mapkey.outputs.key }}
```

- [ ] **Step 3: Commit and push**

```bash
git add .github/workflows/monitor.yml
git commit -m "Poller workflow: cron every 5 minutes, data on an orphan branch

State is read over git rather than raw.githubusercontent.com, which is
CDN cached for about five minutes - a stale state file would re-alert
seats already announced. The data branch is force-pushed as a single
orphan commit so 8,640 pushes a month never grow the repository."
git push origin main
```

- [ ] **Step 4: Run it once by hand and verify**

```bash
gh workflow run monitor.yml --repo AssafAtias/ticket-monitor
sleep 60
gh run list --workflow=monitor.yml --repo AssafAtias/ticket-monitor --limit 1
```

Expected: the run concludes `success`. Then:

```bash
curl -sS "https://raw.githubusercontent.com/AssafAtias/ticket-monitor/data/status.json" | head -30
```

Expected: the status document, naming the next fixture. A Telegram heartbeat
should also arrive on the first run.

If the run failed, read the log with
`gh run view --log-failed --repo AssafAtias/ticket-monitor` before changing
anything — do not guess at the cause.

- [ ] **Step 5: Confirm the second run does not re-alert**

```bash
gh workflow run monitor.yml --repo AssafAtias/ticket-monitor
```

Expected: no second "TICKETS AVAILABLE" message for seats already announced,
and no second heartbeat the same day. This proves the state round-trip through
the `data` branch works, which is the one thing local tests cannot cover.

---

### Task 7: Publish the page

**Files:**
- Create: `.github/workflows/pages.yml`

**Interfaces:**
- Consumes: `site/` from Task 5, the `data` branch from Task 6.
- Produces: the live page at `https://assafatias.github.io/ticket-monitor/`.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/pages.yml`:

```yaml
name: pages

# Deploys only when the site itself changes. Data updates go to the `data`
# branch and are fetched by the browser, so a number changing never
# redeploys the site - which also keeps us clear of Pages' soft limit of
# 10 builds per hour.
on:
  push:
    branches: [main]
    paths: ['site/**', '.github/workflows/pages.yml']
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: site
      - id: deployment
        uses: actions/deploy-pages@v4
```

- [ ] **Step 2: Enable Pages with Actions as the source**

```bash
gh api -X POST repos/AssafAtias/ticket-monitor/pages \
  -f "build_type=workflow" || \
gh api -X PUT repos/AssafAtias/ticket-monitor/pages \
  -f "build_type=workflow"
```

Verify: `gh api repos/AssafAtias/ticket-monitor/pages --jq '.build_type, .html_url'`
Expected: `workflow` and `https://assafatias.github.io/ticket-monitor/`.

- [ ] **Step 3: Commit, push and deploy**

```bash
git add .github/workflows/pages.yml
git commit -m "Publish the page to GitHub Pages

Deploys only when site/ changes: data updates go to the data branch and
are fetched by the browser, so a number changing never redeploys the
site. That also keeps us clear of Pages' soft limit of 10 builds/hour."
git push origin main
gh run watch --repo AssafAtias/ticket-monitor
```

- [ ] **Step 4: Verify the live page end to end**

```bash
curl -sS -o /dev/null -w "page: %{http_code}\n" https://assafatias.github.io/ticket-monitor/
curl -sS -o /dev/null -w "data: %{http_code}\n" https://raw.githubusercontent.com/AssafAtias/ticket-monitor/data/status.json
```

Expected: both `200`.

Then open `https://assafatias.github.io/ticket-monitor/` on a phone and confirm:
the fixture name and kick-off are shown, the count matches `status.json`, tiers
list with sold-out ones visible, "Open the shop" opens the shop, and the
footer reads `checked N min ago` in grey rather than amber or red.

---

### Task 8: Documentation

**Files:**
- Modify: `README.md`

**Interfaces:** none — documentation only.

- [ ] **Step 1: Rewrite the run and limits sections**

The README currently describes a Windows console program. Add a section
immediately after the title, before "Run it":

````markdown
## Where it runs

Live at **https://assafatias.github.io/ticket-monitor/**.

A GitHub Actions cron job polls every 5 minutes, alerts to Telegram, and
publishes `status.json` to the `data` branch. The page fetches that document
and renders it; it never talks to the ticket shop itself.

```
Actions cron */5  ->  webapp/poll.py  ->  Telegram
                                      ->  data branch (status.json, state.json)
                                                 ^
                      GitHub Pages (site/) ------+  fetched by the browser
```

`run.cmd` still works for running it locally on Windows, with the console
output, toast and always-on-top popup.
````

Replace the existing "Limits" section with:

```markdown
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
  freshness - only, sometimes, late.
```

Update the test line under "Run it":

```markdown
python -m unittest discover -p "test_*.py"   # 117 Python tests
node --test "site/**/*.test.js"              # 5 JS tests
```

- [ ] **Step 2: Verify the documented commands actually work**

Run: `python -m unittest discover -p "test_*.py"`
Expected: PASS, 117 tests. If the count differs, correct the README rather
than the number in this plan.

Run: `node --test "site/**/*.test.js"`
Expected: PASS, 5 tests.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Document the hosted deployment and its honest limits

Records the 5-15 minute latency as a deliberate trade for zero running
cost, not an oversight, and says plainly that an away fixture cannot be
bought unless you are already signed in when the alert arrives."
git push origin main
```

---

## Final verification

- [ ] `python -m unittest discover -p "test_*.py"` — 117 tests pass
- [ ] `node --test "site/**/*.test.js"` — 5 tests pass
- [ ] The 51 original tests in `test_monitor.py` are unmodified: `git diff f58a8ee -- test_monitor.py` shows only additions
- [ ] `git grep -nE "[0-9]{8,10}:[A-Za-z0-9_-]{30,}"` returns nothing — no bot token in any tracked file
- [ ] `gh api repos/AssafAtias/ticket-monitor/pages --jq .html_url` returns the live URL
- [ ] Two consecutive workflow runs produce exactly one alert for a given seat and one heartbeat per day
- [ ] The live page shows the same `buyable` count as `curl`-ing `status.json`
