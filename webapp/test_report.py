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


class TestLastSuccessAt(unittest.TestCase):
    """When the numbers on the page were last true, as opposed to when we
    last tried to check them.

    Without it, a six-hour upstream outage renders as a full tier table under
    a fresh "checked 12s ago": carried-forward counts wearing a current
    timestamp.
    """

    def good(self, generated_at=NOW):
        return report.build_status(fixture=FIXTURE, shop=SHOP, url=FIXTURE.url,
                                   buyable=[seat("s1", 155, "אי פלוס")],
                                   counts=COUNTS, alerted=0,
                                   generated_at=generated_at)

    def test_a_successful_poll_is_current_by_definition(self):
        s = self.good()
        self.assertEqual(s["last_success_at"], s["generated_at"])

    def test_a_failed_poll_carries_it_forward_rather_than_advancing_it(self):
        later = NOW + datetime.timedelta(hours=6)
        s = report.build_error_status(self.good(), later, "boom")
        self.assertEqual(s["generated_at"], later.isoformat())
        self.assertEqual(s["last_success_at"], NOW.isoformat())

    def test_it_survives_a_run_of_consecutive_failures(self):
        """Six hours of failures must not creep the timestamp forward."""
        doc = self.good()
        for minutes in range(5, 365, 5):
            doc = report.build_error_status(
                doc, NOW + datetime.timedelta(minutes=minutes), "still down")
        self.assertEqual(doc["last_success_at"], NOW.isoformat())

    def test_it_is_null_when_nothing_ever_succeeded(self):
        """First ever run fails: there is no moment the counts were true."""
        s = report.build_error_status(None, NOW, "boom")
        self.assertIsNone(s["last_success_at"])

    def test_a_previous_document_written_before_the_field_existed(self):
        """The data branch may hold a status.json from the old contract."""
        legacy = self.good()
        del legacy["last_success_at"]
        s = report.build_error_status(legacy, NOW, "boom")
        self.assertIsNone(s["last_success_at"])

    def test_a_recovery_stamps_it_again(self):
        later = NOW + datetime.timedelta(hours=6)
        self.assertEqual(self.good(generated_at=later)["last_success_at"],
                         later.isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
