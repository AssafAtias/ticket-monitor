"""Monitor vivenu-backed ticket sales for genuinely purchasable seats.

Availability cannot be read off the status feed alone. A seat is only
purchasable when all four of these hold:

  1. no event in the united map (parent + children, override layer first)
     marks it as anything other than "free";
  2. it is not held by a contingent this shop is not allowed to sell from;
  3. its category has at least one *active* ticket type;
  4. that ticket type has a price (used to rank alerts cheapest-first).

Rules 1 and 2 are ports of vivenu's own seatselector.js (resolveSeatStatus /
initBlockedSeatsByStatusId). Rule 3 is the one that matters most in practice:
for the Maccabi-Hapoel snapshot this was written against, 521 seats read as
"free" in the feed while 0 were actually buyable, because every one of them sat
in a press / VIP / platinum / accessible category with no ticket on sale.

Run:  python monitor.py            (loop)
      python monitor.py --once     (single check, prints a breakdown)
      python monitor.py --test-alert
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import gzip
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
HISTORY_PATH = HERE / "history.jsonl"
CACHE_DIR = HERE / ".cache"

# The ticket office's Maccabi TA page: the listing the monitor follows so it
# moves on to next week's game by itself.
DISCOVERY_URL = ("https://www.leaan.co.il/category/"
                 "%D7%A1%D7%A4%D7%95%D7%A8%D7%98/%D7%9B%D7%93%D7%95%D7%A8%D7%92%D7%9C/"
                 "%D7%9E%D7%9B%D7%91%D7%99-%D7%AA%D7%9C-%D7%90%D7%91%D7%99%D7%91")

FREE = "free"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- data model


@dataclasses.dataclass(frozen=True)
class SeatInfo:
    """A seat's fixed position, from the seat map."""
    section: str
    row: str
    seat: str
    gate: str
    category_id: str


@dataclasses.dataclass(frozen=True)
class Seat:
    """A purchasable seat, ready to report."""
    status_id: str
    section: str
    row: str
    seat: str
    gate: str
    category: str
    price: float

    def describe(self) -> str:
        return (f"{self.price:g} ILS | block {self.section} row {self.row} "
                f"seat {self.seat} | gate {self.gate} | {self.category}")


@dataclasses.dataclass(frozen=True)
class Shop:
    """Everything the shop page tells us about what is on sale."""
    event_id: str
    seating_event_id: str
    revision_id: str
    event_name: str
    event_start: str
    sale_status: str
    max_per_order: int
    allowed_contingents: frozenset
    sellable: dict          # seating category ref -> (name, price)

    def with_extra_sellable(self, ref, name, price) -> "Shop":
        merged = dict(self.sellable)
        merged[ref] = (name, price)
        return dataclasses.replace(self, sellable=merged)


class AlertState:
    """Remembers which seats we have already shouted about, and for which game.

    Seat ids are only meaningful within one seat map, so the event they were
    recorded against is part of the state: carrying them across a fixture
    rollover would silently mute the first alert of the new game.
    """

    def __init__(self, alerted, event=""):
        self.alerted = set(alerted)
        self.event = event

    def for_event(self, event):
        """This state if it belongs to `event`, an empty one otherwise."""
        if self.event == event:
            return self
        return AlertState((), event)

    def new_among(self, seats):
        current = {s.status_id for s in seats}
        fresh = [s for s in seats if s.status_id not in self.alerted]
        # Forget seats that are gone, so they can alert again if they come back.
        self.alerted = current
        return fresh


# ------------------------------------------------------------------ parsing

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def extract_next_data(html: str) -> dict:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ValueError("__NEXT_DATA__ not found - the shop page layout changed")
    return json.loads(m.group(1))


def parse_shop(next_data: dict) -> Shop:
    props = next_data["props"]["pageProps"]
    event, shop = props["event"], props["shop"]
    seating = event.get("seating") or {}

    tickets_by_cat: dict = {}
    for t in shop.get("tickets", []):
        if t.get("active"):
            tickets_by_cat.setdefault(t.get("categoryRef"), []).append(t)

    sellable = {}
    for cat in shop.get("categories", []):
        ref = cat.get("seatingReference")
        active = tickets_by_cat.get(cat.get("ref")) or []
        if ref and active:
            sellable[ref] = (cat.get("name") or ref,
                             min(t.get("price", 0) for t in active))

    return Shop(
        event_id=event["_id"],
        seating_event_id=seating.get("eventId", ""),
        revision_id=seating.get("revisionId", ""),
        event_name=event.get("name", ""),
        event_start=event.get("start", ""),
        sale_status=shop.get("saleStatus", ""),
        max_per_order=shop.get("maxAmountPerOrder") or 0,
        allowed_contingents=frozenset(str(c) for c in (shop.get("contingents") or [])),
        sellable=sellable,
    )


class NoFixture(Exception):
    """The listing has no game left to watch."""


class EventOver(Exception):
    """The event being watched has already been played."""


@dataclasses.dataclass(frozen=True)
class Fixture:
    """One upcoming game, as the ticket office lists it."""
    name: str
    url: str
    start: datetime.datetime
    end: datetime.datetime
    venue: str
    starting_price: float
    max_per_order: int

    def describe(self) -> str:
        local = self.start.astimezone()
        return (f"{self.name} | {local:%a %d %b %H:%M}"
                + (f" | {self.venue}" if self.venue else ""))


def _epoch(value, fallback=None):
    if not value:
        return fallback
    return datetime.datetime.fromtimestamp(int(value), datetime.timezone.utc)


# Season memberships and refund vouchers sit in the same feed as the games.
# A fixture is the only entry named after two sides, "<home> - <away>".
_FIXTURE_NAME_RE = re.compile(r"\S.*\s-\s.*\S")


def extract_fixtures(next_data: dict) -> list:
    """Upcoming games, from the ticket office's own listing feed.

    Raises ValueError rather than returning [] if the feed's shape changed:
    an empty list is indistinguishable from "season over" at the call site,
    and silently watching nothing is the failure mode this monitor exists to
    avoid.
    """
    node = next_data
    for key in ("props", "pageProps", "initialState", "pageData",
                "upcoming_matches", "matches"):
        if not isinstance(node, dict) or key not in node:
            raise ValueError(
                "fixture listing not found at props.pageProps.initialState"
                ".pageData.upcoming_matches.matches - the page layout changed")
        node = node[key]

    fixtures = []
    for entry in node or []:
        url = entry.get("redirect_url")
        name = (entry.get("event_name") or entry.get("name") or "").strip()
        if not url or entry.get("subscription") or not _FIXTURE_NAME_RE.fullmatch(name):
            continue
        start = _epoch(entry.get("event_start"))
        if start is None:
            continue
        fixtures.append(Fixture(
            name=name,
            url=url,
            start=start,
            end=_epoch(entry.get("event_end"), start + datetime.timedelta(hours=3)),
            venue=(entry.get("location") or {}).get("name", ""),
            starting_price=entry.get("starting_price") or 0,
            max_per_order=entry.get("max_amount_per_order") or 0,
        ))
    return fixtures


def pick_next_fixture(fixtures, now=None):
    """The game to watch: the soonest one that has not finished yet.

    Selection runs off the final whistle, not kick-off, so a seat released
    during the match is still alerted on.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    upcoming = sorted((f for f in fixtures if f.end > now), key=lambda f: f.start)
    if not upcoming:
        raise NoFixture("no upcoming game in the listing")
    return upcoming[0]


def build_seat_index(map_json: dict) -> dict:
    """statusId -> SeatInfo, for every seat on every layer."""
    index = {}
    for layer in map_json["seatMap"].get("layers", []):
        for section in layer.get("sections", []):
            for group in section.get("groups", []):
                for row in group.get("rows", []):
                    for s in row.get("seats", []):
                        index[s["statusId"]] = SeatInfo(
                            section=s.get("sectionName") or group.get("sectionName")
                            or section.get("name") or "?",
                            row=row.get("name") or "?",
                            seat=s.get("seatName") or "?",
                            gate=s.get("gate") or row.get("gate")
                            or group.get("gate") or section.get("gate") or "?",
                            category_id=s.get("categoryId") or "",
                        )
    return index


# ----------------------------------------------------------- availability


def united_map(status_json: dict, parent_event_id: str) -> dict:
    """Parent status map plus every child event's map, keyed by event id."""
    united = {parent_event_id: status_json.get("status") or {}}
    united.update(status_json.get("childMaps") or {})
    return united


def sibling_event_ids(status_json: dict, parent_event_id: str) -> set:
    """Other events sharing this seat map, as the status feed reveals them.

    Nothing lists them - not the shop page, not the listing - but the override
    layer is keyed "<eventId>.<statusId>" and names them even when no children
    were requested. Asking for their maps is what keeps their bookings visible:
    on a Bloomfield home game the season-ticket sibling alone marks 21,356
    seats that the parent map leaves unmarked, i.e. reading as free.
    """
    ids = set(status_json.get("childMaps") or {})
    for key in status_json.get("override") or {}:
        ids.add(key.split(".", 1)[0])
    ids.discard(parent_event_id)
    return ids


def resolve_seat_status(united: dict, override: dict, status_id: str) -> str:
    """Port of vivenu's resolveSeatStatus: most restrictive entry wins.

    A seat absent from every map is free. The override layer, keyed
    "<eventId>.<statusId>", shadows that event's map entry.
    """
    result = FREE
    for event_id, seat_map in united.items():
        if result != FREE:
            break
        value = override.get(f"{event_id}.{status_id}") or (seat_map or {}).get(status_id)
        if value and value != FREE:
            result = value
    return result


def _parse_iso(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def blocked_seat_ids(contingents: dict, allowed, now=None) -> set:
    """Seats held by contingents this shop may not sell from.

    Mirrors initBlockedSeatsByStatusId: general-admission contingents and ones
    whose block has already expired do not restrict seat selection.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    allowed = {str(a) for a in allowed}
    blocked = set()
    for by_event in (contingents or {}).values():
        for cid, cont in (by_event or {}).items():
            if cont.get("generalAdmission"):
                continue
            blocked_until = cont.get("blockedUntil")
            if blocked_until and _parse_iso(blocked_until) <= now:
                continue
            if str(cont.get("_id", cid)) in allowed:
                continue
            blocked.update(cont.get("objects") or [])
    return blocked


def compute_buyable(status_json, seats, contingents, shop, now=None):
    """Seats that can actually be bought right now, cheapest first."""
    united = united_map(status_json, shop.seating_event_id)
    override = status_json.get("override") or {}
    blocked = blocked_seat_ids(contingents, shop.allowed_contingents, now)

    hits = []
    for status_id, info in seats.items():
        if info.category_id not in shop.sellable:
            continue
        if status_id in blocked:
            continue
        if resolve_seat_status(united, override, status_id) != FREE:
            continue
        name, price = shop.sellable[info.category_id]
        hits.append(Seat(status_id, info.section, info.row, info.seat,
                         info.gate, name, price))

    hits.sort(key=lambda s: (s.price, s.section, s.row, s.seat))
    return hits


def below_price(seats, threshold):
    """Seats cheap enough to alert on. threshold None means no price filter.

    Applied at alert time, not inside compute_buyable, so the breakdown counts
    and history stay complete - a pricier seat is still recorded and logged,
    just not shouted about.
    """
    if threshold is None:
        return list(seats)
    return [s for s in seats if s.price < threshold]


def breakdown(status_json, seats, contingents, shop, now=None) -> dict:
    """Classify every seat in the map. Counts sum to the map's seat total."""
    united = united_map(status_json, shop.seating_event_id)
    override = status_json.get("override") or {}
    blocked = blocked_seat_ids(contingents, shop.allowed_contingents, now)

    counts = {"buyable": 0, "free_not_on_sale": 0, "blocked_by_contingent": 0,
              "booked": 0, "reserved": 0, "notforsale": 0, "other": 0}
    for status_id, info in seats.items():
        status = resolve_seat_status(united, override, status_id)
        if status != FREE:
            key = status if status in counts else "other"
            counts[key] += 1
        elif status_id in blocked:
            counts["blocked_by_contingent"] += 1
        elif info.category_id in shop.sellable:
            counts["buyable"] += 1
        else:
            counts["free_not_on_sale"] += 1
    return counts


# -------------------------------------------------------------- http layer


def fetch(url: str, timeout=45) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw


def fetch_json(url: str, timeout=45) -> dict:
    return json.loads(fetch(url, timeout).decode("utf-8"))


def fetch_with_retry(url, attempts=3, timeout=45, label=""):
    last = None
    for i in range(attempts):
        try:
            return fetch_json(url, timeout)
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"{label or url} failed after {attempts} attempts: {last}")


def fetch_next_data(url, attempts=3, label="", fetcher=None, sleep=None) -> dict:
    """Fetch an HTML page and parse its __NEXT_DATA__, retrying short reads.

    The ticket office intermittently returns a truncated body - observed at
    2,033,592 bytes with no closing </html> where the complete page is
    2,209,538 - and gzip.decompress returns that partial content without
    raising. The only symptom is a __NEXT_DATA__ script with no closing
    </script>, which extract_next_data reports as a layout change.

    Retrying is what separates the two: a short read succeeds on the next
    attempt, a real layout change fails every time. The final error says so,
    rather than sending the reader hunting for a redesign that never happened.
    """
    fetcher = fetcher or fetch
    sleep = sleep or time.sleep
    last = None
    for attempt in range(attempts):
        try:
            return extract_next_data(fetcher(url).decode("utf-8", "replace"))
        except (ValueError, OSError) as exc:
            last = exc
            if attempt < attempts - 1:
                sleep(2 ** attempt)
    raise RuntimeError(
        f"{label or url}: page still incomplete after {attempts} attempts "
        f"({last}). A short read looks identical to a layout change; this "
        f"failed every time, so the layout may genuinely have changed.")


class Api:
    def __init__(self, cfg):
        self.cfg = cfg
        self.seating_base = cfg.get("seating_base", "https://seatmap.vivenu.com")
        self.child_ids = list(cfg.get("child_event_ids") or [])
        # Pinned for the whole run, or None to follow the schedule. In auto
        # mode event_url is what discovery last resolved to.
        self.pinned = pinned_event_url(cfg)
        self.event_url = self.pinned

    @property
    def auto(self) -> bool:
        return self.pinned is None

    def _children_qs(self):
        return ("?childEventIds=" + ",".join(self.child_ids)) if self.child_ids else ""

    def discover(self) -> Fixture:
        """The next Maccabi TA game, from the ticket office's listing page."""
        url = self.cfg.get("discovery_url") or DISCOVERY_URL
        return pick_next_fixture(
            extract_fixtures(fetch_next_data(url, label="fixture listing")))

    def shop_page(self) -> dict:
        return fetch_next_data(self.event_url, label="shop page")

    def status(self, seating_event_id) -> dict:
        url = f"{self.seating_base}/api/public/event/{seating_event_id}/status{self._children_qs()}"
        return fetch_with_retry(url, label="status")

    def contingents(self, seating_event_id) -> dict:
        url = (f"{self.seating_base}/api/public/event/{seating_event_id}"
               f"/contingents{self._children_qs()}")
        return fetch_with_retry(url, label="contingents")

    def seat_map(self, seating_event_id, revision_updated_at) -> dict:
        """Seat geometry. Big and static, so cached on disk per revision."""
        CACHE_DIR.mkdir(exist_ok=True)
        key = re.sub(r"[^0-9A-Za-z]", "", revision_updated_at or "nocache")
        cached = CACHE_DIR / f"map-{seating_event_id}-{key}.json"
        if cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))
        qs = urllib.parse.urlencode({"c": revision_updated_at or "", "shrink": "true"})
        url = f"{self.seating_base}/api/public/event/{seating_event_id}/map?{qs}"
        data = fetch_with_retry(url, timeout=120, label="map")
        cached.write_text(json.dumps(data), encoding="utf-8")
        return data


# ------------------------------------------------------------ notifications


def _powershell(script: str):
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-Command", script],
                       timeout=30, capture_output=True)
    except Exception as exc:                                  # never kill the loop
        log(f"toast failed: {exc}")


def _powershell_detached(script: str, tag: str):
    """Fire a script and don't wait. Used for the popup, which stays on screen
    until dismissed and so must never block the poll loop."""
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        path = CACHE_DIR / f"{tag}.ps1"
        path.write_text(script, encoding="utf-8-sig")
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: detached leaves powershell with
        # no console and it exits immediately with code 0, taking the window with
        # it and reporting nothing.
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", str(path)],
                         creationflags=0x08000000)
    except Exception as exc:
        log(f"popup failed: {exc}")


# Windows only renders toasts for a registered AppUserModelID. PowerShell's own
# AUMID ships on every Windows install, so borrowing it avoids Show() succeeding
# while nothing ever appears on screen.
POWERSHELL_AUMID = ("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
                    "\\WindowsPowerShell\\v1.0\\powershell.exe")


def toast(title: str, lines, loud=True):
    """Windows toast via WinRT, with no third-party dependency."""
    body = "\n".join(lines)[:600]
    esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;"))
    # A looping <audio> is only valid on a duration="long" toast. Without it
    # Windows rejects the payload while Show() still reports success, so the
    # notification never appears - which is exactly what happened first time.
    audio = ('<audio src="ms-winsoundevent:Notification.Looping.Alarm2" loop="true"/>'
             if loud else '<audio src="ms-winsoundevent:Notification.Default"/>')
    attrs = 'scenario="urgent" duration="long"' if loud else ''
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@"
<toast {attrs}>
  <visual><binding template="ToastGeneric">
    <text>{esc(title)}</text><text>{esc(body)}</text>
  </binding></visual>
  {audio}
</toast>
"@)
$t = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{POWERSHELL_AUMID}").Show($t)
"""
    _powershell(script)
    if loud:
        try:
            import winsound
            for _ in range(3):
                winsound.MessageBeep(winsound.MB_ICONHAND)
                time.sleep(0.35)
        except Exception:
            pass


def popup(title: str, lines, url=""):
    """An always-on-top window that survives Do Not Disturb and stays until
    dismissed. The toast is a convenience; this is the alert that must not fail.
    """
    body = "\n".join(lines)[:900]
    q = lambda s: s.replace("'", "''")
    script = f"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$f = New-Object System.Windows.Forms.Form
$f.Text = 'Ticket Monitor'
$f.Size = New-Object System.Drawing.Size(620, 380)
$f.StartPosition = 'CenterScreen'
$f.TopMost = $true
$f.BackColor = [System.Drawing.Color]::FromArgb(20, 22, 28)

$h = New-Object System.Windows.Forms.Label
$h.Text = '{q(title)}'
$h.Font = New-Object System.Drawing.Font('Segoe UI', 15, [System.Drawing.FontStyle]::Bold)
$h.ForeColor = [System.Drawing.Color]::FromArgb(120, 230, 140)
$h.Location = New-Object System.Drawing.Point(20, 18)
$h.Size = New-Object System.Drawing.Size(570, 60)
$f.Controls.Add($h)

$b = New-Object System.Windows.Forms.TextBox
$b.Multiline = $true
$b.ReadOnly = $true
$b.Text = '{q(body)}'.Replace("`n", "`r`n")
$b.Font = New-Object System.Drawing.Font('Consolas', 10)
$b.BackColor = [System.Drawing.Color]::FromArgb(28, 31, 38)
$b.ForeColor = [System.Drawing.Color]::White
$b.BorderStyle = 'None'
$b.Location = New-Object System.Drawing.Point(20, 84)
$b.Size = New-Object System.Drawing.Size(565, 175)
$b.ScrollBars = 'Vertical'
# Without this the read-only box takes focus and shows every line highlighted.
$b.TabStop = $false
$f.Controls.Add($b)

# Buttons must set BackColor explicitly: BackColor is an ambient property in
# WinForms, so without this they inherit the form's near-black background and
# render as dark-on-dark.
$open = New-Object System.Windows.Forms.Button
$open.Text = 'Open the shop'
$open.Location = New-Object System.Drawing.Point(20, 285)
$open.Size = New-Object System.Drawing.Size(150, 40)
$open.FlatStyle = 'Flat'
$open.BackColor = [System.Drawing.Color]::FromArgb(56, 190, 90)
$open.ForeColor = [System.Drawing.Color]::FromArgb(10, 30, 15)
$open.Font = New-Object System.Drawing.Font('Segoe UI', 10, [System.Drawing.FontStyle]::Bold)
$open.FlatAppearance.BorderSize = 0
$open.Cursor = [System.Windows.Forms.Cursors]::Hand
$open.Add_Click({{ Start-Process '{q(url)}'; $f.Close() }})
if ('{q(url)}') {{ $f.Controls.Add($open) }}

$ok = New-Object System.Windows.Forms.Button
$ok.Text = 'Dismiss'
$ok.Location = New-Object System.Drawing.Point(185, 285)
$ok.Size = New-Object System.Drawing.Size(110, 40)
$ok.FlatStyle = 'Flat'
$ok.BackColor = [System.Drawing.Color]::FromArgb(232, 234, 238)
$ok.ForeColor = [System.Drawing.Color]::FromArgb(24, 26, 32)
$ok.Font = New-Object System.Drawing.Font('Segoe UI', 10)
$ok.FlatAppearance.BorderSize = 0
$ok.Cursor = [System.Windows.Forms.Cursors]::Hand
$ok.Add_Click({{ $f.Close() }})
$f.Controls.Add($ok)

$f.Add_Shown({{
  $f.Activate()
  $b.SelectionLength = 0
  if ($f.Controls.Contains($open)) {{ $open.Focus() }} else {{ $ok.Focus() }}
}})
[System.Windows.Forms.Application]::Run($f)
"""
    _powershell_detached(script, "alert-popup")


def telegram(cfg, text: str):
    tg = cfg.get("telegram") or {}
    token, chat_id = tg.get("bot_token"), tg.get("chat_id")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read()).get("ok", False)
    except Exception as exc:
        log(f"telegram failed: {exc}")
        return False


def announce(cfg, seats, shop, test=False, url=""):
    top = seats[:8]
    lines = [s.describe() for s in top]
    if len(seats) > len(top):
        lines.append(f"...and {len(seats) - len(top)} more")
    if test:
        # A test that looks like a real alert sends you chasing a seat that does
        # not exist. Make the difference impossible to miss.
        lines = ["THIS IS A TEST - the seat below is fake, do not go looking.",
                 ""] + lines

    # In auto mode config.json holds "auto", not a link - the shop URL comes
    # from whatever the monitor actually resolved to.
    url = url or pinned_event_url(cfg) or ""

    title = (f"{len(seats)} TICKET{'S' if len(seats) != 1 else ''} AVAILABLE"
             f" - {shop.event_name}")
    if test:
        title = "[TEST - NOT A REAL TICKET] " + title
    toast(title, lines, loud=True)
    if cfg.get("popup", True):
        popup(title, lines + ["", f"Max {shop.max_per_order} per customer - go now."],
              url)

    body = "\n".join(f"• {line}" for line in lines)
    configured = bool((cfg.get("telegram") or {}).get("bot_token"))
    sent = telegram(cfg, (f"<b>{title}</b>\n{body}\n\n"
                          f"Max {shop.max_per_order} per customer - go now:\n"
                          f"{url}"))
    if configured and not sent:
        log("!! telegram delivery FAILED - desktop alert only")

    log("=" * 62)
    log(f"*** {title} ***")
    for line in lines:
        log("    " + line)
    log(f"    {url}")
    log("=" * 62)


# ------------------------------------------------------------------ runtime


def log(msg: str):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{stamp}] {msg}", flush=True)
    except UnicodeEncodeError:
        print(f"[{stamp}] {msg.encode('ascii', 'replace').decode()}", flush=True)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"missing config: {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return _apply_env_secrets(cfg)


def _apply_env_secrets(cfg: dict) -> dict:
    """Overlay credentials from the environment onto the config file.

    The repo is public, so config.json is committed without secrets and the
    workflow injects them. Both halves must be present: a token without a chat
    id cannot send, and pretending otherwise turns a misconfiguration into a
    silent non-delivery.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        cfg = dict(cfg)
        cfg["telegram"] = {"bot_token": token, "chat_id": chat_id}
    return cfg


def pinned_event_url(cfg):
    """The event_url to stick to, or None to follow the schedule.

    "auto" (or nothing at all) means discover the next game every refresh.
    A real URL pins the monitor to that one event - useful for a cup game or a
    one-off sale the league listing does not carry.
    """
    url = (cfg.get("event_url") or "auto").strip()
    return None if url.lower() == "auto" else url


def load_state() -> AlertState:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return AlertState(data.get("alerted") or [], data.get("event") or "")
        except (json.JSONDecodeError, OSError):
            pass
    return AlertState(set())


def save_state(state: AlertState):
    STATE_PATH.write_text(json.dumps({
        "event": state.event,
        "alerted": sorted(state.alerted),
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }, indent=1), encoding="utf-8")


def record(entry: dict):
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Monitor:
    """Owns the refresh cadence: status every poll, shop config periodically."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.api = Api(cfg)
        self.shop = None
        self.seats = None
        self.contingents = None
        self.config_age = None
        self.fixture = None

    def resolve_event(self):
        """Point the API at the game to watch, following the schedule if asked.

        Runs on every config refresh, so the monitor rolls over to next week's
        game on its own rather than polling a finished one forever.
        """
        if not self.api.auto:
            return False
        fixture = self.api.discover()
        if fixture.url == self.api.event_url:
            return False
        first = self.fixture is None
        self.fixture = fixture
        self.api.event_url = fixture.url
        log(("watching" if first else "!! NEW GAME - now watching")
            + f": {fixture.describe()}")
        log(f"   {fixture.url}")
        return not first

    def refresh_config(self, status_json=None):
        """Re-read what is on sale. Catches releases that are not status flips:
        a category gaining an active ticket, or a contingent being released."""
        rolled = self.resolve_event()
        if rolled:
            # Different seat map: nothing carried over from the old game.
            self.shop = None
            self.api.child_ids = list(self.cfg.get("child_event_ids") or [])
        next_data = self.api.shop_page()
        shop = parse_shop(next_data)
        if shop.sale_status == "past":
            # The symptom that started this: a finished event polls forever and
            # reports last week's teams. Never fail quietly here.
            raise EventOver(f"{shop.event_name} ({shop.event_start}) is over")
        if status_json is None:
            status_json = self.api.status(shop.seating_event_id)
        status_json = self.adopt_siblings(status_json, shop.seating_event_id)
        revision = (status_json.get("revision") or {}).get("updatedAt", "")

        previous = self.shop
        self.shop = shop
        self.seats = build_seat_index(self.api.seat_map(shop.seating_event_id, revision))
        self.contingents = self.api.contingents(shop.seating_event_id)
        self.config_age = time.monotonic()

        if previous is not None:
            gained = set(shop.sellable) - set(previous.sellable)
            lost = set(previous.sellable) - set(shop.sellable)
            for ref in gained:
                log(f"!! category went ON SALE: {shop.sellable[ref][0]}")
            for ref in lost:
                log(f"   category left sale: {previous.sellable[ref][0]}")
            if shop.sale_status != previous.sale_status:
                log(f"!! saleStatus {previous.sale_status} -> {shop.sale_status}")
        return status_json

    def adopt_siblings(self, status_json, seating_event_id):
        """Start asking for any sibling event's map the feed has revealed.

        Re-fetched immediately rather than next poll: until the sibling's map
        is included its seats read as free, which is exactly the false alert
        this monitor is built to avoid.
        """
        found = sibling_event_ids(status_json, seating_event_id)
        new = found - set(self.api.child_ids)
        if not new:
            return status_json
        self.api.child_ids = sorted(set(self.api.child_ids) | found)
        log(f"   sharing this seat map: {', '.join(sorted(new))}")
        return self.api.status(seating_event_id)

    def _event_over(self, exc):
        """A finished event is the one thing this monitor must never sit on."""
        log(f"!! {exc}")
        if not self.api.auto:
            toast("Ticket monitor stopped",
                  ["The event in config.json has already been played.",
                   'Set "event_url": "auto" to follow the schedule.'], loud=False)
            raise SystemExit(
                'event is over - set "event_url": "auto" in config.json to '
                "follow the schedule, or point it at the next game")
        log("   re-reading the listing for the next game")
        self.api.event_url = None
        self.fixture = None
        self.shop = None
        self.api.child_ids = list(self.cfg.get("child_event_ids") or [])

    def check(self, status_json):
        buyable = compute_buyable(status_json, self.seats, self.contingents, self.shop)
        counts = breakdown(status_json, self.seats, self.contingents, self.shop)
        return buyable, counts

    def run(self):
        interval = self.cfg.get("poll_seconds", 60)
        config_every = self.cfg.get("refresh_config_seconds", 300)

        try:
            status_json = self.refresh_config()
        except EventOver as exc:
            self._event_over(exc)
            status_json = self.refresh_config()
        state = load_state().for_event(self.api.event_url)
        log(f"confirmed: {self.shop.event_name}  ({self.shop.event_start})")
        log(f"sale: {self.shop.sale_status} | max per customer: {self.shop.max_per_order}")
        log(f"{len(self.seats)} seats mapped | {len(self.shop.sellable)} categories on sale")
        log(f"polling every {interval}s, re-reading what is on sale every {config_every}s")
        threshold = self.cfg.get("alert_below_price")
        if threshold is None:
            log("alerting on any price")
        else:
            cheap = sorted({f"{p:g}" for _, p in self.shop.sellable.values()
                            if p < threshold}, key=float)
            log(f"alerting ONLY under {threshold:g} ILS "
                f"(qualifying tiers: {', '.join(cheap) or 'NONE'})")
        if not (self.cfg.get("telegram") or {}).get("bot_token"):
            log("telegram not configured - desktop alerts only")

        failures = 0
        first = True
        while True:
            try:
                if not first:
                    if time.monotonic() - self.config_age >= config_every:
                        status_json = self.refresh_config()
                        # A rollover invalidates last game's seat ids.
                        state = state.for_event(self.api.event_url)
                    else:
                        status_json = self.api.status(self.shop.seating_event_id)
                first = False

                buyable, counts = self.check(status_json)
                failures = 0

                fresh = state.new_among(buyable)
                save_state(state)
                threshold = self.cfg.get("alert_below_price")
                alertable = below_price(fresh, threshold)
                too_pricey = [s for s in fresh if s not in alertable]
                record({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "counts": counts, "new": len(fresh),
                        "alerted": len(alertable), "over_threshold": len(too_pricey),
                        "seats": [dataclasses.asdict(s) for s in buyable[:40]]})

                if too_pricey:
                    # Never let a found seat vanish silently just because of the filter.
                    log(f"   {len(too_pricey)} seat(s) found but over the "
                        f"{threshold:g} ILS alert threshold - not alerting:")
                    for s in too_pricey[:5]:
                        log("     " + s.describe())

                if alertable:
                    announce(self.cfg, alertable, self.shop,
                             url=self.api.event_url)
                else:
                    log(f"no tickets  (buyable {counts['buyable']}, "
                        f"booked {counts['booked']}, "
                        f"blocked {counts['blocked_by_contingent']}, "
                        f"free-not-on-sale {counts['free_not_on_sale']})")

            except KeyboardInterrupt:
                raise
            except EventOver as exc:
                self._event_over(exc)
                self.config_age = time.monotonic() - config_every
            except NoFixture as exc:
                log(f"!! {exc} - looking again next poll")
                self.config_age = time.monotonic() - config_every
            except Exception as exc:
                failures += 1
                log(f"poll failed ({failures}): {exc}")
                if failures == 5:
                    toast("Ticket monitor is failing",
                          [f"{failures} polls in a row failed.", str(exc)[:200]],
                          loud=False)
                if failures >= 5:
                    try:
                        status_json = self.refresh_config()
                        first = False
                    except Exception as reset_exc:
                        log(f"config refresh also failed: {reset_exc}")

            time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description="Monitor purchasable tickets.")
    ap.add_argument("--once", action="store_true",
                    help="check once, print the breakdown, exit")
    ap.add_argument("--test-alert", action="store_true",
                    help="fire a sample toast + telegram message and exit")
    args = ap.parse_args()

    cfg = load_config()

    if args.test_alert:
        sample = [Seat("demo", "FAKE", "0", "0", "0", "TEST DATA", 200)]
        label = cfg.get("label") or "Test event"
        url = pinned_event_url(cfg) or ""
        try:
            fixture = Api(cfg).discover()
            label, url = fixture.name, fixture.url
        except Exception as exc:                 # discovery is a nicety here
            log(f"(could not read the listing for a label: {exc})")
        shop = Shop("", "", "", label, "", "onSale", 1, frozenset(), {})
        announce(cfg, sample, shop, test=True, url=url)
        log("TEST alert sent (clearly labelled - no real ticket implied)")
        return

    mon = Monitor(cfg)
    if args.once:
        try:
            status_json = mon.refresh_config()
        except EventOver as exc:
            mon._event_over(exc)
            status_json = mon.refresh_config()
        buyable, counts = mon.check(status_json)
        log(f"{mon.shop.event_name}  ({mon.shop.event_start})")
        log(f"sale: {mon.shop.sale_status} | max per customer: {mon.shop.max_per_order}")
        for key, value in counts.items():
            log(f"  {key:22} {value}")
        if buyable:
            log(f"BUYABLE NOW ({len(buyable)}), cheapest first:")
            for s in buyable[:40]:
                log("  " + s.describe())
        else:
            log("nothing purchasable right now")
        return

    try:
        mon.run()
    except KeyboardInterrupt:
        log("stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
