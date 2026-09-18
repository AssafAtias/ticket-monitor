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

    def test_negative_attempts_sends_nothing(self):
        s = Sender([True])
        self.assertFalse(alerts.send({}, "hi", attempts=-1, sender=s,
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

    def test_truncation_never_cuts_an_html_entity_in_half(self):
        """Escaping after slicing is what guarantees this; the reverse order
        leaves a broken &a fragment."""
        doc = status_doc([seat("s1")])
        doc["fixture"]["name"] = "x" * (alerts.MAX_NAME_CHARS - 1) + "&"
        t = alerts.alert_text(doc, [seat("s1")])
        self.assertIn("&amp;", t)
        self.assertNotRegex(t, r"&[a-z]{0,3}(?![a-z]*;)\b(?<!&amp;)")

    def test_a_url_full_of_ampersands_stays_well_formed(self):
        doc = status_doc([seat("s1")])
        doc["fixture"]["url"] = "https://x.test/?" + "&a=1" * 200
        t = alerts.alert_text(doc, [seat("s1")])
        self.assertLess(len(t), alerts.MAX_TELEGRAM_CHARS)

    def test_pathological_seat_data_cannot_blow_the_size_limit(self):
        """Seat fields come from the same third-party feed as everything else."""
        fat = [monitor.Seat(f"s{i}", "F", "1", "1", "F", "c" * 5000, 155)
               for i in range(8)]
        doc = status_doc(fat)
        t = alerts.alert_text(doc, fat)
        self.assertLess(len(t), alerts.MAX_TELEGRAM_CHARS)
        self.assertEqual(t.count("<b>"), t.count("</b>"))

    def test_the_shop_link_survives_even_when_seats_are_dropped(self):
        """Losing the link would make the alert useless exactly when it matters.

        Reaching 4096 takes every bounded value at its maximum AND every
        character escaping to five: one fat field is not enough once each is
        capped on its own. The previous version of this test dropped nothing -
        instrumentation showed zero pops - and its bullet-count assertion was
        satisfied by the unconditional "...and N more" line, which is present
        whether the loop runs or not. This data drops two lines.
        """
        fat = [monitor.Seat(f"s{i}", "&" * 200, "&" * 200, "&" * 200,
                            "&" * 200, "&" * 200, 155) for i in range(20)]
        doc = status_doc(fat)
        doc["max_per_order"] = "&" * 10
        doc["fixture"]["name"] = "&" * 80
        doc["fixture"]["url"] = ("https://tickets.leaan.net/event/--02j286"
                                 + "&" * 200)
        t = alerts.alert_text(doc, fat)

        seat_lines = [line for line in t.splitlines()
                      if line.startswith("• ") and "more" not in line]
        self.assertLess(len(seat_lines), alerts.SEATS_IN_ALERT,
                        "this data is supposed to force the shrink loop to pop")
        self.assertLessEqual(len(t), alerts.MAX_TELEGRAM_CHARS)
        self.assertIn("https://tickets.leaan.net/event/--02j286", t)
        self.assertEqual(t.count("<b>"), t.count("</b>"))

    def test_an_oversized_max_per_order_cannot_blow_the_limit(self):
        """Every interpolated value comes from the same third-party feed, so
        none of them may be trusted to be small - including this one."""
        fat = [monitor.Seat(f"s{i}", "F", "1", "1", "F", "c" * 5000, 155)
               for i in range(8)]
        doc = status_doc(fat)
        doc["max_per_order"] = "9" * 5000
        t = alerts.alert_text(doc, fat)
        self.assertLess(len(t), alerts.MAX_TELEGRAM_CHARS)
        self.assertIn("https://tickets.leaan.net/event/--02j286", t)


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


class TestFailureEscalationText(unittest.TestCase):
    """The hosted replacement for the desktop build's five-failure toast."""

    def doc(self, error="status feed timed out"):
        d = status_doc([])
        d["error"] = error
        return d

    def test_says_how_many_polls_failed_and_why(self):
        t = alerts.failure_escalation_text(self.doc(), 5)
        self.assertIn("5", t)
        self.assertIn("status feed timed out", t)

    def test_says_plainly_that_nothing_is_being_watched(self):
        """The whole point is that the reader must not assume silence means
        no tickets."""
        self.assertIn("not watching", alerts.failure_escalation_text(self.doc(), 5))

    def test_escapes_html_so_telegram_cannot_reject_it(self):
        t = alerts.failure_escalation_text(self.doc("boom & <crash>"), 5)
        self.assertIn("&amp;", t)
        self.assertIn("&lt;crash&gt;", t)
        self.assertNotIn("<crash>", t)

    def test_a_huge_error_string_cannot_blow_the_size_limit(self):
        """Errors can carry a third-party response body."""
        t = alerts.failure_escalation_text(self.doc("&" * 20000), 5)
        self.assertLess(len(t), alerts.MAX_TELEGRAM_CHARS)

    def test_a_missing_error_still_produces_a_usable_message(self):
        d = status_doc([])
        d["error"] = None
        self.assertIn("unknown", alerts.failure_escalation_text(d, 5))

    def test_the_failure_count_is_bounded_too(self):
        t = alerts.failure_escalation_text(self.doc(), "9" * 5000)
        self.assertLess(len(t), alerts.MAX_TELEGRAM_CHARS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
