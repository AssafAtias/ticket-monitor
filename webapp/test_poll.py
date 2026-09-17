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
