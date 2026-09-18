"""Regression tests for the availability engine, run against real captured payloads.

The headline test is test_today_snapshot_has_zero_buyable: the raw status feed
reports 521 free seats, but none of them are purchasable from this shop. Any
implementation that just counts "free" in the JSON fails there.
"""
import copy
import datetime
import json
import os
import pathlib
import shutil
import tempfile
import unittest
import unittest.mock

import monitor

FIX = pathlib.Path(__file__).parent / "fixtures"
# Frozen inside the sale window, so contingent blockedUntil dates behave as captured.
NOW = datetime.datetime(2026, 9, 8, 10, 40, tzinfo=datetime.timezone.utc)


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.status = load("status.json")
        cls.map = load("map.json")
        cls.contingents = load("contingents.json")
        cls.next = load("next.json")
        cls.html = (FIX / "event.html").read_text(encoding="utf-8")
        cls.shop = monitor.parse_shop(cls.next)
        cls.seats = monitor.build_seat_index(cls.map)

    def buyable(self, status=None, shop=None, contingents=None):
        return monitor.compute_buyable(
            status if status is not None else self.status,
            self.seats,
            contingents if contingents is not None else self.contingents,
            shop or self.shop,
            now=NOW,
        )

    def free_one(self, sid, status=None):
        """Return a copy of the status feed with sid absent everywhere (= free)."""
        st = copy.deepcopy(status if status is not None else self.status)
        st["status"].pop(sid, None)
        for child in st["childMaps"].values():
            child.pop(sid, None)
        return st

    def pick(self, seating_ref, unblocked=True):
        blocked = monitor.blocked_seat_ids(self.contingents, set(), NOW)
        for sid, s in self.seats.items():
            if s.category_id != seating_ref:
                continue
            if unblocked and sid in blocked:
                continue
            return sid
        self.fail("no seat found for category " + seating_ref)


class TestShopParsing(Base):
    def test_extracts_next_data_from_html(self):
        self.assertEqual(monitor.extract_next_data(self.html), self.next)

    def test_reads_event_identity(self):
        self.assertEqual(self.shop.seating_event_id, "6a70951c1d39e111df667729")
        self.assertEqual(self.shop.revision_id, "66d564e48f04e5f01300926e")
        self.assertIn("מכבי", self.shop.event_name)

    def test_finds_27_sellable_categories(self):
        self.assertEqual(len(self.shop.sellable), 27)

    def test_sellable_carries_name_and_price(self):
        name, price = self.shop.sellable["nDTAudn8Mta3TA"]
        self.assertEqual(name, "319-322")
        self.assertEqual(price, 200)

    def test_categories_without_active_tickets_are_not_sellable(self):
        # Gate 4 has 230 free seats but no active ticket - the whole trap.
        self.assertNotIn("4WZXj7z6g1Cnbu", self.shop.sellable)   # gate 4
        self.assertNotIn("wZIq0OogXl8mLK", self.shop.sellable)   # MEDIA
        self.assertNotIn("gvquj4GZVaNxbJ", self.shop.sellable)   # VIP A/B

    def test_purchase_limits_are_captured(self):
        self.assertEqual(self.shop.max_per_order, 1)
        self.assertEqual(self.shop.sale_status, "onSale")


class TestSeatIndex(Base):
    def test_indexes_every_seat_in_the_map(self):
        self.assertEqual(len(self.seats), 29941)

    def test_seat_carries_full_location(self):
        # Keyed by statusId (what the status feed uses), not the seat's own _id.
        s = self.seats["O9PHqIeljnawEO"]
        self.assertEqual((s.section, s.seat, s.gate), ("330", "288", "8"))
        self.assertEqual(s.category_id, "FVYRDXsRN0Hucj")

    def test_seat_id_is_not_used_as_the_index_key(self):
        self.assertNotIn("XdJRCSz2ZPxaI9", self.seats)   # that seat's _id

    def test_index_covers_the_live_status_feed(self):
        missing = [k for k in self.status["status"] if k not in self.seats]
        self.assertEqual(missing, [], "every status key must resolve to a seat")


class TestStatusResolution(Base):
    def setUp(self):
        self.united = monitor.united_map(self.status, self.shop.seating_event_id)
        self.override = self.status["override"]

    def resolve(self, sid):
        return monitor.resolve_seat_status(self.united, self.override, sid)

    def test_unknown_seat_defaults_to_free(self):
        self.assertEqual(self.resolve("nosuchseatid"), "free")

    def test_booked_in_parent_wins(self):
        self.assertEqual(self.resolve("uebmthzOMFiv6s"), "booked")

    def test_booked_in_child_wins_over_absent_parent(self):
        child = self.status["childMaps"]["6a292156116a0419bac7987c"]
        sid = next(k for k, v in child.items()
                   if v == "booked" and k not in self.status["status"])
        self.assertEqual(self.resolve(sid), "booked")

    def test_most_restrictive_across_maps_wins(self):
        united = {"a": {"S": "free"}, "b": {"S": "booked"}}
        self.assertEqual(monitor.resolve_seat_status(united, {}, "S"), "booked")

    def test_override_layer_takes_precedence_over_map(self):
        united = {"evt": {"S": "booked"}}
        override = {"evt.S": "free"}
        self.assertEqual(monitor.resolve_seat_status(united, override, "S"), "free")


class TestContingents(Base):
    def test_blocks_7402_seats_when_shop_allows_none(self):
        blocked = monitor.blocked_seat_ids(self.contingents, allowed=set(), now=NOW)
        self.assertEqual(len(blocked), 7402)

    def test_allowed_contingent_does_not_contribute_blocks(self):
        cont = {"e": {"c1": {"_id": "c1", "objects": ["S1", "S2"]},
                      "c2": {"_id": "c2", "objects": ["S3"]}}}
        self.assertEqual(monitor.blocked_seat_ids(cont, {"c1"}, NOW), {"S3"})
        self.assertEqual(monitor.blocked_seat_ids(cont, {"c1", "c2"}, NOW), set())

    def test_seat_stays_blocked_while_any_other_contingent_holds_it(self):
        """1,619 of the 7,402 held seats sit in more than one contingent."""
        cont = {"e": {"c1": {"_id": "c1", "objects": ["S"]},
                      "c2": {"_id": "c2", "objects": ["S"]}}}
        self.assertEqual(monitor.blocked_seat_ids(cont, {"c1"}, NOW), {"S"})

    def test_allowing_contingents_shrinks_the_real_blocked_set(self):
        all_ids = {str(cid) for d in self.contingents.values() for cid in d}
        self.assertEqual(
            monitor.blocked_seat_ids(self.contingents, all_ids, NOW), set())

    def test_expired_block_is_ignored(self):
        cont = {"e": {"c1": {"_id": "c1", "objects": ["S"],
                             "blockedUntil": "2026-01-01T00:00:00.000Z"}}}
        self.assertEqual(monitor.blocked_seat_ids(cont, set(), NOW), set())

    def test_general_admission_contingent_is_ignored(self):
        cont = {"e": {"c1": {"_id": "c1", "objects": ["S"], "generalAdmission": True}}}
        self.assertEqual(monitor.blocked_seat_ids(cont, set(), NOW), set())


class TestBuyable(Base):
    def test_today_snapshot_has_zero_buyable(self):
        """The whole point: 521 seats read as free, none are purchasable."""
        raw_free = sum(1 for v in self.status["status"].values() if v == "free")
        self.assertGreater(raw_free, 0, "sanity: the feed does contain free seats")
        self.assertEqual(self.buyable(), [])

    def test_breakdown_matches_captured_reality(self):
        b = monitor.breakdown(self.status, self.seats, self.contingents,
                              self.shop, now=NOW)
        self.assertEqual(b["booked"], 23229)
        self.assertEqual(b["blocked_by_contingent"], 6159)
        self.assertEqual(b["free_not_on_sale"], 521)
        self.assertEqual(b["reserved"], 32)
        self.assertEqual(b["buyable"], 0)
        self.assertEqual(sum(b.values()), 29941)

    def test_freed_sellable_seat_is_reported_with_location_and_price(self):
        sid = self.pick("nDTAudn8Mta3TA")          # 319-322, 200 ILS
        hits = self.buyable(self.free_one(sid))
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.status_id, sid)
        self.assertEqual(hit.price, 200)
        self.assertEqual(hit.category, "319-322")
        self.assertEqual(hit.section, self.seats[sid].section)
        self.assertEqual(hit.row, self.seats[sid].row)
        self.assertEqual(hit.seat, self.seats[sid].seat)

    def test_freed_media_seat_is_ignored(self):
        sid = self.pick("wZIq0OogXl8mLK")          # MEDIA, no active ticket
        self.assertEqual(self.buyable(self.free_one(sid)), [])

    def test_freed_but_contingent_blocked_seat_is_ignored(self):
        blocked = monitor.blocked_seat_ids(self.contingents, set(), NOW)
        sid = next(s for s in blocked if s in self.seats
                   and self.seats[s].category_id in self.shop.sellable)
        self.assertEqual(self.buyable(self.free_one(sid)), [])

    def test_reserved_seats_are_not_buyable(self):
        reserved = [k for k, v in self.status["status"].items() if v == "reserved"]
        self.assertTrue(reserved, "sanity: snapshot has reserved seats")
        self.assertEqual(self.buyable(), [])

    def test_results_are_ranked_cheapest_first(self):
        st = self.status
        for ref in ("KBXBlvjLLqW792", "nDTAudn8Mta3TA", "EKyyG0RZoPr7gc"):
            st = self.free_one(self.pick(ref), st)
        prices = [h.price for h in self.buyable(st)]
        self.assertEqual(prices, sorted(prices))
        self.assertEqual(prices, [200, 325, 900])

    def test_activating_a_category_releases_its_free_seats(self):
        """A release can arrive as a category going on sale, not a status flip."""
        shop = self.shop.with_extra_sellable("4WZXj7z6g1Cnbu", "gate 4", 180)
        hits = self.buyable(shop=shop)
        self.assertEqual(len(hits), 230)
        self.assertTrue(all(h.category == "gate 4" for h in hits))

    def test_releasing_all_contingents_exposes_seats(self):
        self.assertGreater(len(self.buyable(contingents={})), 0)


class TestPriceFilter(Base):
    def mk(self, *prices):
        return [monitor.Seat(f"s{i}", "320", "1", str(i), "7", "cat", p)
                for i, p in enumerate(prices)]

    def test_keeps_only_seats_strictly_below_threshold(self):
        got = monitor.below_price(self.mk(180, 200, 210), 200)
        self.assertEqual([s.price for s in got], [180])

    def test_threshold_is_exclusive_so_200_is_dropped(self):
        self.assertEqual(monitor.below_price(self.mk(200), 200), [])

    def test_none_threshold_disables_the_filter(self):
        got = monitor.below_price(self.mk(180, 900), None)
        self.assertEqual([s.price for s in got], [180, 900])

    def test_only_the_180_tiers_qualify_under_200(self):
        """Guards the config the monitor actually runs with."""
        qualifying = sorted({p for _, p in self.shop.sellable.values() if p < 200})
        self.assertEqual(qualifying, [180])
        names = sorted(n for n, p in self.shop.sellable.values() if p < 200)
        self.assertEqual(len(names), 4)

    def test_filter_does_not_change_the_breakdown(self):
        """Pricier seats must still be counted and recorded, just not alerted."""
        sid = self.pick("KBXBlvjLLqW792")            # 900 ILS
        status = self.free_one(sid)
        buyable = self.buyable(status)
        self.assertEqual(len(buyable), 1)
        self.assertEqual(monitor.below_price(buyable, 200), [])
        counts = monitor.breakdown(status, self.seats, self.contingents,
                                   self.shop, now=NOW)
        self.assertEqual(counts["buyable"], 1)


class TestAlertState(Base):
    def seat(self, sid, price):
        return monitor.Seat(sid, "319-322", "5", "12", "7", "319-322", price)

    def test_only_new_seats_are_alerted(self):
        state = monitor.AlertState(set())
        a, b = self.seat("s1", 200), self.seat("s2", 230)
        self.assertEqual([s.status_id for s in state.new_among([a])], ["s1"])
        self.assertEqual(state.new_among([a]), [])
        self.assertEqual([s.status_id for s in state.new_among([a, b])], ["s2"])

    def test_seat_can_alert_again_after_it_disappears(self):
        state = monitor.AlertState(set())
        a = self.seat("s1", 200)
        state.new_among([a])
        state.new_among([])                      # taken by someone else
        self.assertEqual([s.status_id for s in state.new_among([a])], ["s1"])



# --------------------------------------------------------- fixture discovery

# The ticket office lists Maccabi TA's upcoming events as JSON in the category
# page. Frozen two days before the Netanya game, with the previous fixture
# already past, so rollover is exercised rather than assumed.
FIXTURES_NOW = datetime.datetime(2026, 9, 17, 18, 0, tzinfo=datetime.timezone.utc)


class TestFixtureDiscovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feed = load("upcoming.json")

    def test_extracts_only_real_fixtures(self):
        names = [f.name for f in monitor.extract_fixtures(self.feed)]
        self.assertIn('מכבי נתניה - מכבי ת"א', names)
        # Season membership and a refund voucher share the feed and are not games.
        self.assertNotIn("FOREVER 26/27", names)
        for name in names:
            self.assertNotIn("החזר דרבי", name)

    def test_fixture_carries_what_the_monitor_needs(self):
        f = monitor.pick_next_fixture(monitor.extract_fixtures(self.feed), FIXTURES_NOW)
        self.assertEqual(f.url, "https://tickets.leaan.net/event/--02j286")
        self.assertEqual(f.start.astimezone(datetime.timezone.utc).isoformat(),
                         "2026-09-19T17:00:00+00:00")
        self.assertEqual(f.venue, "אצטדיון נתניה")

    def test_picks_the_soonest_upcoming_game(self):
        f = monitor.pick_next_fixture(monitor.extract_fixtures(self.feed), FIXTURES_NOW)
        self.assertEqual(f.name, 'מכבי נתניה - מכבי ת"א')

    def test_rolls_over_once_the_game_is_over(self):
        """The bug this fixes: a finished game must never stay selected."""
        fixtures = monitor.extract_fixtures(self.feed)
        after = datetime.datetime(2026, 9, 20, 6, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(monitor.pick_next_fixture(fixtures, after).url,
                         "https://tickets.leaan.net/event/--co3j4w")

    def test_kickoff_does_not_roll_over_mid_game(self):
        """Tickets are still worth alerting on right up to the whistle."""
        fixtures = monitor.extract_fixtures(self.feed)
        kickoff = datetime.datetime(2026, 9, 19, 17, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(monitor.pick_next_fixture(fixtures, kickoff).url,
                         "https://tickets.leaan.net/event/--02j286")

    def test_no_upcoming_fixture_is_an_error_not_a_stale_pick(self):
        fixtures = monitor.extract_fixtures(self.feed)
        far = datetime.datetime(2027, 6, 1, tzinfo=datetime.timezone.utc)
        with self.assertRaises(monitor.NoFixture):
            monitor.pick_next_fixture(fixtures, far)

    def test_layout_change_is_reported_loudly(self):
        with self.assertRaises(ValueError):
            monitor.extract_fixtures({"props": {"pageProps": {}}})


class TestStateFollowsTheEvent(unittest.TestCase):
    def seat(self, sid):
        return monitor.Seat(sid, "F", "1", "1", "F", "E+", 155)

    def test_state_is_kept_for_the_same_event(self):
        state = monitor.AlertState({"s1"}, event="A")
        self.assertIs(state.for_event("A"), state)
        self.assertEqual(state.for_event("A").new_among([self.seat("s1")]), [])

    def test_state_is_dropped_when_the_game_changes(self):
        """Seat ids from last week's map must not mute this week's alerts."""
        state = monitor.AlertState({"s1"}, event="A").for_event("B")
        self.assertEqual(state.event, "B")
        self.assertEqual([s.status_id for s in state.new_among([self.seat("s1")])],
                         ["s1"])



class TestSiblingEvents(Base):
    """Events sharing a seat map are listed nowhere, but the feed leaks them.

    Miss one and its seats read as free: on the captured Bloomfield map the
    sibling alone accounts for 21,356 seats the parent leaves unmarked.
    """
    PARENT = "6a70951c1d39e111df667729"
    SIBLING = "6a292156116a0419bac7987c"

    def test_finds_sibling_from_the_override_layer(self):
        bare = {"status": {}, "override": {f"{self.SIBLING}.seat1": "booked"}}
        self.assertEqual(monitor.sibling_event_ids(bare, self.PARENT), {self.SIBLING})

    def test_finds_sibling_already_returned_as_a_child_map(self):
        self.assertIn(self.SIBLING,
                      monitor.sibling_event_ids(self.status, self.PARENT))

    def test_parent_is_never_reported_as_its_own_sibling(self):
        feed = {"status": {}, "override": {f"{self.PARENT}.seat1": "booked"},
                "childMaps": {self.PARENT: {}}}
        self.assertEqual(monitor.sibling_event_ids(feed, self.PARENT), set())

    def test_no_siblings_is_empty_not_an_error(self):
        self.assertEqual(monitor.sibling_event_ids({}, self.PARENT), set())

    def test_sibling_seats_are_not_buyable(self):
        """The whole point: a seat the sibling booked must never be alerted."""
        sid = self.pick("bTrTBqIXpYZaPS")
        freed = self.free_one(sid)
        freed["childMaps"][self.SIBLING][sid] = "booked"
        self.assertNotIn(sid, [s.status_id for s in self.buyable(freed)])


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


class TestTruncatedPageRetry(unittest.TestCase):
    """The ticket office intermittently returns a short read.

    gzip.decompress yields the partial body without raising, so the only
    symptom is __NEXT_DATA__ with no closing </script>. Retrying is what
    distinguishes that from a genuine layout change.
    """

    GOOD = ('<html><script id="__NEXT_DATA__" type="application/json">'
            '{"props": {"ok": true}}</script></html>')
    # A real truncation: the tag opens, the body is cut, nothing closes.
    TRUNCATED = ('<html><script id="__NEXT_DATA__" type="application/json">'
                 '{"props": {"ok": tr')

    def fetcher(self, *pages):
        self.calls = []
        pages = list(pages)

        def fetch(url, timeout=45):
            self.calls.append(url)
            return pages[len(self.calls) - 1].encode("utf-8")
        return fetch

    def test_a_complete_page_is_parsed_without_retrying(self):
        data = monitor.fetch_next_data("u", fetcher=self.fetcher(self.GOOD),
                                       sleep=lambda _: None)
        self.assertEqual(data["props"]["ok"], True)
        self.assertEqual(len(self.calls), 1)

    def test_a_short_read_is_retried_and_then_succeeds(self):
        data = monitor.fetch_next_data(
            "u", fetcher=self.fetcher(self.TRUNCATED, self.TRUNCATED, self.GOOD),
            sleep=lambda _: None)
        self.assertEqual(data["props"]["ok"], True)
        self.assertEqual(len(self.calls), 3)

    def test_it_gives_up_after_the_attempt_budget(self):
        with self.assertRaises(RuntimeError):
            monitor.fetch_next_data(
                "u", attempts=2,
                fetcher=self.fetcher(self.TRUNCATED, self.TRUNCATED),
                sleep=lambda _: None)
        self.assertEqual(len(self.calls), 2)

    def test_the_error_blames_a_short_read_not_a_layout_change(self):
        """The old message sent a reader hunting for a redesign that never
        happened."""
        with self.assertRaises(RuntimeError) as caught:
            monitor.fetch_next_data(
                "u", attempts=1, label="fixture listing",
                fetcher=self.fetcher(self.TRUNCATED), sleep=lambda _: None)
        message = str(caught.exception).lower()
        self.assertIn("incomplete", message)
        self.assertIn("fixture listing", message)

    def test_it_backs_off_between_attempts(self):
        waits = []
        with self.assertRaises(RuntimeError):
            monitor.fetch_next_data(
                "u", attempts=3,
                fetcher=self.fetcher(self.TRUNCATED, self.TRUNCATED,
                                     self.TRUNCATED),
                sleep=waits.append)
        self.assertEqual(waits, [1, 2])

    def test_a_network_error_is_also_retried(self):
        """A short read and a dropped connection deserve the same treatment."""
        calls = []

        def flaky(url, timeout=45):
            calls.append(url)
            if len(calls) < 3:
                raise OSError("connection reset")
            return self.GOOD.encode("utf-8")

        data = monitor.fetch_next_data("u", fetcher=flaky, sleep=lambda _: None)
        self.assertEqual(data["props"]["ok"], True)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
