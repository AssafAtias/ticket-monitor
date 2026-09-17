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
MAX_NAME_CHARS = 120
MAX_URL_CHARS = 300


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

    Fixture names and seat descriptions come from a third-party feed. An
    unescaped & or < makes Telegram reject the whole message, and send()
    would then retry the same broken text and give up silently.
    """
    return html.escape(str(value if value is not None else ""), quote=False)


def alert_text(status: dict, seats) -> str:
    """The message body for newly buyable seats.

    Every interpolated value is escaped and length-bounded, so the result
    is valid HTML under Telegram's limit by construction rather than by a
    final slice that could cut a tag in half.
    """
    fixture = status.get("fixture") or {}
    count = len(seats)
    noun = "TICKET" if count == 1 else "TICKETS"
    name = _esc(fixture.get("name", ""))[:MAX_NAME_CHARS]
    lines = [f"<b>{count} {noun} AVAILABLE - {name}</b>"]
    for s in seats[:SEATS_IN_ALERT]:
        lines.append(f"• {_esc(s.describe())}")
    if count > SEATS_IN_ALERT:
        lines.append(f"• ...and {count - SEATS_IN_ALERT} more")
    lines.append("")
    lines.append(f"Max {_esc(status.get('max_per_order', 0))} per customer - go now:")
    lines.append(_esc(fixture.get("url", ""))[:MAX_URL_CHARS])
    return "\n".join(lines)


def heartbeat_text(status: dict) -> str:
    """The daily 'still alive' message.

    A page nobody is looking at cannot warn anybody, and a workflow that
    GitHub disabled for inactivity stops without telling you. This is the
    only thing that makes that silence audible.
    """
    fixture = status.get("fixture") or {}
    if status.get("error"):
        return (f"Ticket monitor is alive but the last poll FAILED:\n"
                f"{_esc(status['error'])}")
    return (f"Ticket monitor alive. Watching {_esc(fixture.get('name', 'nothing'))}"
            f" - {status.get('buyable', 0)} seat(s) buyable.")


def heartbeat_due(last_sent, today: str) -> bool:
    """True once per calendar day. Both arguments are YYYY-MM-DD strings."""
    return (last_sent or "") != today
