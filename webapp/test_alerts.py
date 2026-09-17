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

    def test_send_survives_a_sleep_that_throws(self):
        """send()'s no-raise contract protects the caller's pending disk write."""
        def bad_sleep(_):
            raise RuntimeError("clock broke")
        self.assertFalse(alerts.send({}, "hi", attempts=2,
                                     sender=Sender([False, False]), sleep=bad_sleep))

    def test_zero_attempts_sends_nothing_and_reports_failure(self):
        s = Sender([True])
        self.assertFalse(alerts.send({}, "hi", attempts=0, sender=s,
                                     sleep=lambda _: None))
        self.assertEqual(s.sent, [])


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

    def test_escapes_html_so_telegram_cannot_reject_the_message(self):
        """Team names come from a third-party feed; & and < must not break HTML."""
        doc = status_doc([seat("s1")])
        doc["fixture"]["name"] = 'Ajax & <b>PSV</b>'
        t = alerts.alert_text(doc, [seat("s1")])
        self.assertIn("Ajax &amp; &lt;b&gt;PSV&lt;/b&gt;", t)
        self.assertEqual(t.count("<b>"), 1)
        self.assertEqual(t.count("</b>"), 1)

    def test_a_pathological_fixture_name_still_yields_valid_bounded_html(self):
        doc = status_doc([seat("s1")])
        doc["fixture"]["name"] = "x" * 10000
        t = alerts.alert_text(doc, [seat("s1")])
        self.assertLess(len(t), 4096)
        self.assertTrue(t.count("<b>") == t.count("</b>") == 1)

    def test_heartbeat_escapes_an_error_string(self):
        doc = status_doc([])
        doc["error"] = "boom & <crash>"
        self.assertIn("&amp;", alerts.heartbeat_text(doc))


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
