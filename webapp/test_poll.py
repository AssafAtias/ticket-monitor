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
import unittest.mock

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

    def test_no_temp_files_are_left_behind(self):
        self.run_once()
        self.assertEqual(list(self.dir.glob("*.tmp")), [])


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

    def test_a_failed_delivery_leaves_the_seat_eligible_to_alert_again(self):
        """A seat nobody was told about must not be recorded as announced."""
        FakeMonitor.seats_to_return = [seat("a")]
        self.sent = Recorder(ok=False)
        self.run_once()
        self.assertEqual(self.read("state.json")["alerted"], [])
        self.sent = Recorder(ok=True)
        self.run_once(now=NOW + datetime.timedelta(minutes=5))
        self.assertTrue(any("AVAILABLE" in m for m in self.sent.messages))

    def test_a_price_suppressed_seat_stays_marked_even_though_nothing_was_sent(self):
        """Suppression is deliberate; only delivery FAILURE should un-mark."""
        FakeMonitor.seats_to_return = [seat("a", price=155)]
        self.run_once(cfg={"alert_below_price": 100})
        self.assertEqual(self.read("state.json")["alerted"], ["a"])


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

    def test_a_pinned_finished_event_is_published_as_an_error_not_a_crash(self):
        """_event_over raises SystemExit, which is not an Exception."""
        class Pinned(FakeMonitor):
            def refresh_config(self, status_json=None):
                raise monitor.EventOver("the game was played")
            def _event_over(self, exc):
                raise SystemExit("event is over - set event_url to auto")
        status = poll.run_once({"alert_below_price": None}, self.dir, now=NOW,
                               monitor_factory=Pinned, send=self.sent)
        self.assertIn("over", status["error"])
        self.assertTrue((self.dir / "status.json").exists())

    def test_a_publishing_failure_does_not_raise(self):
        FakeMonitor.seats_to_return = [seat("a")]
        with unittest.mock.patch.object(poll, "_write",
                                        side_effect=OSError("disk full")):
            self.assertIsInstance(self.run_once(), dict)

    def test_a_heartbeat_failure_does_not_prevent_publication(self):
        def boom(cfg, text, **kw):
            raise RuntimeError("telegram exploded")
        poll.run_once({"alert_below_price": None}, self.dir, now=NOW,
                      monitor_factory=FakeMonitor, send=boom)
        self.assertTrue((self.dir / "status.json").exists())

    def test_a_status_file_of_the_wrong_shape_is_ignored(self):
        (self.dir / "status.json").write_text("[1, 2, 3]", encoding="utf-8")
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


class TestMidPollFailureDoesNotConsumeAlerts(Base):
    """new_among() marks seats the moment it is asked what is new.

    Anything that raises between that call and a successful send is caught
    by run_once's except Exception, and the state written afterwards would
    still carry those ids - so the seats would be recorded as announced
    without a message ever being sent, and never alerted again.
    """

    def test_an_exception_after_new_among_leaves_the_seats_unalerted(self):
        FakeMonitor.seats_to_return = [seat("a"), seat("b")]
        with unittest.mock.patch.object(poll.report, "build_status",
                                        side_effect=RuntimeError("kaboom")):
            status = self.run_once()
        self.assertIn("kaboom", status["error"])
        self.assertEqual(self.read("state.json")["alerted"], [])

    def test_the_next_poll_still_alerts_those_seats(self):
        FakeMonitor.seats_to_return = [seat("a")]
        with unittest.mock.patch.object(poll.report, "build_status",
                                        side_effect=RuntimeError("kaboom")):
            self.run_once()
        self.sent.messages.clear()
        self.run_once(now=NOW + datetime.timedelta(minutes=5))
        self.assertTrue(any("AVAILABLE" in m for m in self.sent.messages))

    def test_seats_announced_before_the_failure_are_not_re_announced(self):
        """Restoring state must restore what was on disk, not wipe it."""
        FakeMonitor.seats_to_return = [seat("a")]
        self.run_once()
        self.sent.messages.clear()
        FakeMonitor.seats_to_return = [seat("a"), seat("b")]
        with unittest.mock.patch.object(poll.report, "build_status",
                                        side_effect=RuntimeError("kaboom")):
            self.run_once(now=NOW + datetime.timedelta(minutes=5))
        self.assertEqual(self.read("state.json")["alerted"], ["a"])


class TestUnreadableStateFile(Base):
    """run_once promises never to raise, and the reads happen before its try.

    A single non-UTF-8 byte in state.json used to escape as a
    UnicodeDecodeError, fail the Poll step, skip Publish, and leave the page
    with nothing - the one outcome the whole error path exists to prevent.
    """

    def test_invalid_utf8_in_state_is_treated_as_no_state(self):
        (self.dir / "state.json").write_bytes(bytes([0xff, 0xfe, 0x00]) + b"rubbish")
        status = self.run_once()
        self.assertIsInstance(status, dict)
        self.assertTrue((self.dir / "status.json").exists())

    def test_invalid_utf8_in_status_does_not_stop_the_run(self):
        (self.dir / "status.json").write_bytes(bytes([0xff, 0xfe]) + b"junk")
        self.assertIsInstance(self.run_once(), dict)

    def test_read_json_returns_the_default_for_undecodable_bytes(self):
        path = self.dir / "weird.json"
        path.write_bytes(bytes([0xff, 0xfe, 0x00]) + b"rubbish")
        self.assertEqual(poll.read_json(path, {}), {})

    def test_an_unexpected_read_failure_still_publishes(self):
        """read_json absorbs what it knows about; the guard covers the rest."""
        with unittest.mock.patch.object(poll, "read_json",
                                        side_effect=RuntimeError("bad disk")):
            status = self.run_once()
        self.assertIsInstance(status, dict)
        self.assertTrue((self.dir / "status.json").exists())


class TestHeartbeatDelivery(Base):
    def test_a_failed_heartbeat_is_not_recorded_as_sent(self):
        """It is the day's only liveness signal; a failed send must not
        consume it."""
        self.sent = Recorder(ok=False)
        self.run_once()
        self.assertEqual(self.read("state.json")["last_heartbeat"], "")

    def test_it_is_retried_on_the_next_poll_the_same_day(self):
        self.sent = Recorder(ok=False)
        self.run_once()
        self.sent = Recorder(ok=True)
        self.run_once(now=NOW + datetime.timedelta(minutes=5))
        self.assertTrue(any("alive" in m.lower() for m in self.sent.messages))
        self.assertEqual(self.read("state.json")["last_heartbeat"], "2026-09-17")


class TestFailureEscalation(Base):
    """The hosted stand-in for the desktop build's five-failure toast."""

    ESCALATION = "polls in a row"

    def escalations(self):
        return [m for m in self.sent.messages if self.ESCALATION in m]

    def fail_polls(self, count, start=1):
        FakeMonitor.raise_on_refresh = RuntimeError("upstream down")
        for i in range(start, start + count):
            self.run_once(now=NOW + datetime.timedelta(minutes=5 * i))

    def test_failures_are_counted_in_state(self):
        self.fail_polls(3)
        self.assertEqual(self.read("state.json")["failures"], 3)

    def test_nothing_is_escalated_before_five(self):
        self.fail_polls(4)
        self.assertEqual(self.escalations(), [])

    def test_the_fifth_consecutive_failure_escalates_once(self):
        self.fail_polls(5)
        self.assertEqual(len(self.escalations()), 1)
        self.assertIn("upstream down", self.escalations()[0])

    def test_a_long_outage_does_not_send_hundreds_of_messages(self):
        self.fail_polls(20)
        self.assertEqual(len(self.escalations()), 1)

    def test_a_successful_poll_resets_the_counter_and_re_arms_it(self):
        self.fail_polls(5)
        FakeMonitor.raise_on_refresh = None
        self.run_once(now=NOW + datetime.timedelta(hours=1))
        self.assertEqual(self.read("state.json")["failures"], 0)
        self.assertIs(self.read("state.json")["escalated"], False)
        self.sent.messages.clear()
        self.fail_polls(5, start=20)
        self.assertEqual(len(self.escalations()), 1)

    def test_an_undelivered_escalation_is_not_recorded_as_sent(self):
        """Same rule as the heartbeat: a failed send must not burn the signal."""
        self.sent = Recorder(ok=False)
        self.fail_polls(5)
        self.assertIs(self.read("state.json")["escalated"], False)
        self.sent = Recorder(ok=True)
        self.fail_polls(1, start=6)
        self.assertEqual(len(self.escalations()), 1)

    def test_a_corrupt_failure_count_is_treated_as_zero(self):
        (self.dir / "state.json").write_text('{"failures": "lots"}',
                                             encoding="utf-8")
        self.fail_polls(1)
        self.assertEqual(self.read("state.json")["failures"], 1)

    def test_an_escalation_that_raises_does_not_stop_publication(self):
        def boom(cfg, text, **kw):
            if TestFailureEscalation.ESCALATION in text:
                raise RuntimeError("telegram exploded")
            return True
        FakeMonitor.raise_on_refresh = RuntimeError("upstream down")
        for i in range(1, 6):
            poll.run_once({"alert_below_price": None}, self.dir,
                          now=NOW + datetime.timedelta(minutes=5 * i),
                          monitor_factory=FakeMonitor, send=boom)
        self.assertTrue((self.dir / "status.json").exists())


class TestLastSuccessAt(Base):
    def test_a_good_poll_stamps_it_with_now(self):
        self.run_once()
        self.assertEqual(self.read("status.json")["last_success_at"],
                         NOW.isoformat())

    def test_a_failed_poll_carries_it_forward_unchanged(self):
        """The counts on the page are as old as this, not as old as
        generated_at."""
        self.run_once()
        later = NOW + datetime.timedelta(hours=6)
        FakeMonitor.raise_on_refresh = RuntimeError("boom")
        self.run_once(now=later)
        doc = self.read("status.json")
        self.assertEqual(doc["generated_at"], later.isoformat())
        self.assertEqual(doc["last_success_at"], NOW.isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
