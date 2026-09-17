"""Telegram delivery.

Hosting removed the always-on-top window that used to be the alert that
must not fail, so Telegram is now the only channel. It is treated
accordingly: retried, and never allowed to raise into the poller.
"""
from __future__ import annotations

import time

import monitor

MAX_TELEGRAM_CHARS = 4096
SEATS_IN_ALERT = 8


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
            sleep(2 ** attempt)
    return False


def alert_text(status: dict, seats) -> str:
    """The message body for newly buyable seats."""
    fixture = status.get("fixture") or {}
    count = len(seats)
    noun = "TICKET" if count == 1 else "TICKETS"
    lines = [f"<b>{count} {noun} AVAILABLE - {fixture.get('name', '')}</b>"]
    for s in seats[:SEATS_IN_ALERT]:
        lines.append(f"• {s.describe()}")
    if count > SEATS_IN_ALERT:
        lines.append(f"• ...and {count - SEATS_IN_ALERT} more")
    lines.append("")
    lines.append(f"Max {status.get('max_per_order', 0)} per customer - go now:")
    lines.append(fixture.get("url", ""))
    text = "\n".join(lines)
    return text[:MAX_TELEGRAM_CHARS]


def heartbeat_text(status: dict) -> str:
    """The daily 'still alive' message.

    A page nobody is looking at cannot warn anybody, and a workflow that
    GitHub disabled for inactivity stops without telling you. This is the
    only thing that makes that silence audible.
    """
    fixture = status.get("fixture") or {}
    if status.get("error"):
        return (f"Ticket monitor is alive but the last poll FAILED:\n"
                f"{status['error']}")
    return (f"Ticket monitor alive. Watching {fixture.get('name', 'nothing')}"
            f" - {status.get('buyable', 0)} seat(s) buyable.")


def heartbeat_due(last_sent, today: str) -> bool:
    """True once per calendar day. Both arguments are YYYY-MM-DD strings."""
    return (last_sent or "") != today
