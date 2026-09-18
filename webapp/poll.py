"""One poll, then exit. The cron schedule is the loop.

Everything here is orchestration: the engine decides what is buyable, this
module decides what is new, tells Telegram, and writes the two documents
the page and the next run depend on.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
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
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default
    return data if isinstance(data, dict) else default


def _write(path: pathlib.Path, document: dict):
    """Write via a temp file and os.replace.

    A truncated state.json reads as missing, and read_json treats missing
    as empty - which would re-alert every seat on the next poll. An atomic
    swap means a reader sees either the old file or the new one.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def run_once(cfg, data_dir, now=None, monitor_factory=None, send=None) -> dict:
    """Poll once; write status.json and state.json; return the status.

    Never raises. The workflow's job is to publish a document every run, and
    a run that failed is exactly when the page most needs to say so.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    data_dir = pathlib.Path(data_dir)
    monitor_factory = monitor_factory or monitor.Monitor
    send = send or alerts.send

    status_path, state_path = data_dir / "status.json", data_dir / "state.json"
    previous, stored = None, {}
    try:
        # These two reads sit above the poll's own try. read_json absorbs the
        # corruptions it knows about, but anything it does not would escape
        # run_once, fail the Poll step, skip Publish, and leave the page
        # showing a document from before the failure with nothing to say why.
        previous = read_json(status_path)
        stored = read_json(state_path, {}) or {}
    except Exception as exc:
        monitor.log(f"reading the previous documents failed: {exc}")
        previous, stored = None, {}

    state = monitor.AlertState(stored.get("alerted") or [],
                               stored.get("event") or "")
    last_heartbeat = stored.get("last_heartbeat") or ""
    try:
        failures = int(stored.get("failures") or 0)
    except (TypeError, ValueError):
        failures = 0
    escalated = bool(stored.get("escalated"))

    try:
        mon = monitor_factory(cfg)
        try:
            status_json = mon.refresh_config()
        except monitor.EventOver as exc:
            try:
                mon._event_over(exc)
            except SystemExit as bail:
                # Correct for the desktop app; fatal for an unattended cron
                # job whose one job is to publish a document saying what
                # went wrong.
                raise RuntimeError(f"event is over and pinned: {bail}") from None
            status_json = mon.refresh_config()
        buyable, counts = mon.check(status_json)

        state = state.for_event(mon.api.event_url)
        fresh = state.new_among(buyable)
        alertable = monitor.below_price(fresh, cfg.get("alert_below_price"))

        status = report.build_status(
            fixture=mon.fixture, shop=mon.shop, url=mon.api.event_url,
            buyable=buyable, counts=counts, alerted=len(alertable),
            generated_at=now)

        delivered = None
        if alertable:
            # Telegram is the only channel now, so whether it actually got
            # through is part of the status, not just a log line.
            delivered = bool(send(cfg, alerts.alert_text(status, alertable)))
            if not delivered:
                # A seat nobody was told about must stay eligible to alert
                # again. Erring towards a duplicate message beats erring
                # towards silence.
                state.alerted -= {s.status_id for s in alertable}
        status["alert_delivered"] = delivered
    except Exception as exc:
        monitor.log(f"poll failed: {exc}")
        # new_among() may already have marked seats as alerted before the
        # failure. Keeping that would silence them forever, which is the exact
        # bug the delivery-failure un-marking exists to prevent.
        state = monitor.AlertState(stored.get("alerted") or [],
                                   stored.get("event") or "")
        status = report.build_error_status(previous, now, str(exc))

    if status.get("error"):
        failures += 1
    else:
        failures, escalated = 0, False

    if failures >= alerts.FAILURES_BEFORE_ESCALATION and not escalated:
        # The desktop build toasted after five consecutive failures. Hosted,
        # there is nobody at the screen, so this is the only thing between a
        # blind monitor and the next daily heartbeat.
        try:
            escalated = bool(send(cfg,
                                  alerts.failure_escalation_text(status, failures)))
        except Exception as exc:
            monitor.log(f"failure escalation failed: {exc}")

    today = now.date().isoformat()
    try:
        if alerts.heartbeat_due(last_heartbeat, today):
            # Only burn the day's liveness signal if it actually went out.
            if send(cfg, alerts.heartbeat_text(status)):
                last_heartbeat = today
    except Exception as exc:
        monitor.log(f"heartbeat failed: {exc}")

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        _write(status_path, status)
        _write(state_path, {"event": state.event, "alerted": sorted(state.alerted),
                            "updated": now.isoformat(),
                            "last_heartbeat": last_heartbeat,
                            "failures": failures, "escalated": escalated})
    except Exception as exc:
        monitor.log(f"publishing failed: {exc}")
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
