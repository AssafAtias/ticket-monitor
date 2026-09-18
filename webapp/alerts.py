"""Telegram delivery.

Hosting removed the always-on-top window that used to be the alert that
must not fail, so Telegram is now the only channel. It is treated
accordingly: retried, and never allowed to raise into the poller.
"""
from __future__ import annotations

import html
import time

import monitor

MAX_TELEGRAM_CHARS = 4096
SEATS_IN_ALERT = 8
MAX_NAME_CHARS = 80
MAX_URL_CHARS = 200
MAX_SEAT_CHARS = 100
MAX_COUNT_CHARS = 10
MAX_ERROR_CHARS = 300
FAILURES_BEFORE_ESCALATION = 5


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
            try:
                sleep(2 ** attempt)
            except Exception as exc:
                monitor.log(f"backoff sleep raised: {exc}")
    return False


def _esc(value) -> str:
    """Escape text for Telegram's parse_mode=HTML.

    Always called on an ALREADY-TRUNCATED value: escaping only grows text,
    so truncating first guarantees a slice can never cut an entity such as
    &amp; in half.
    """
    return html.escape(str(value if value is not None else ""), quote=False)


def alert_text(status: dict, seats) -> str:
    """The message body for newly buyable seats.

    Telegram rejects malformed HTML and anything over 4096 characters, and
    send() would retry the identical rejected text and give up silently. So
    every interpolated value is sliced first, then escaped. With the seat
    list empty, head (~432 chars: <b>{N TICKETS AVAILABLE - 80-char name})
    plus tail (~1077 chars: blank line, "Max {10-char count} per customer",
    200-char URL) = ~1509 chars, comfortably under 4096. Each of those three
    values is counted at 5x its slice, because "&" escapes to "&amp;" and a
    feed value may be nothing but ampersands - including max_per_order, which
    an earlier version of this arithmetic counted unescaped. Seat lines are
    dropped whole from the tail until the message fits. A seat line is
    expendable; the link is not.
    """
    fixture = status.get("fixture") or {}
    count = len(seats)
    noun = "TICKET" if count == 1 else "TICKETS"

    name = _esc(str(fixture.get("name") or "")[:MAX_NAME_CHARS])
    head = f"<b>{count} {noun} AVAILABLE - {name}</b>"
    tail = ["",
            f"Max {_esc(str(status.get('max_per_order', 0))[:MAX_COUNT_CHARS])} per customer - go now:",
            _esc(str(fixture.get("url") or "")[:MAX_URL_CHARS])]


    shown = list(seats[:SEATS_IN_ALERT])
    while True:
        body = [f"• {_esc(str(s.describe())[:MAX_SEAT_CHARS])}" for s in shown]
        if count > len(shown):
            body.append(f"• ...and {count - len(shown)} more")
        text = "\n".join([head] + body + tail)
        if len(text) <= MAX_TELEGRAM_CHARS or not shown:
            return text
        shown.pop()


def heartbeat_text(status: dict) -> str:
    """The daily 'still alive' message.

    A page nobody is looking at cannot warn anybody, and a workflow that
    GitHub disabled for inactivity stops without telling you. This is the
    only thing that makes that silence audible.
    """
    fixture = status.get("fixture") or {}
    if status.get("error"):
        return (f"Ticket monitor is alive but the last poll FAILED:\n"
                f"{_esc(str(status['error'])[:300])}")
    return (f"Ticket monitor alive. Watching {_esc(str(fixture.get('name', 'nothing'))[:MAX_NAME_CHARS])}"
            f" - {status.get('buyable', 0)} seat(s) buyable.")


def failure_escalation_text(status: dict, failures) -> str:
    """The message sent once a run of polls has all failed.

    The desktop build toasted after five consecutive failures; hosted, there
    is no screen to toast at, so without this a blind monitor says nothing
    until the next daily heartbeat - up to 24 hours of looking healthy while
    seeing nothing. The error is sliced then escaped, like every other
    interpolated value, because it can carry a third-party URL or response
    body.
    """
    error = _esc(str(status.get("error") or "unknown")[:MAX_ERROR_CHARS])
    return (f"Ticket monitor has FAILED {_esc(str(failures)[:MAX_COUNT_CHARS])} "
            f"polls in a row - it is not watching anything right now.\n"
            f"Last error: {error}")


def heartbeat_due(last_sent, today: str) -> bool:
    """True once per calendar day. Both arguments are YYYY-MM-DD strings."""
    return (last_sent or "") != today
