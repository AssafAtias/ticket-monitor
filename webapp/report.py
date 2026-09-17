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
