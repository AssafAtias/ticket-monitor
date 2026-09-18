import { freshness } from './freshness.js';
import { safeHref, ago, kickoff, liveSummary } from './format.js';

// The page is served from GitHub Pages but the data lives on the `data`
// branch, fetched straight from raw.githubusercontent.com (which sends
// Access-Control-Allow-Origin: *). That keeps Pages from redeploying every
// five minutes just because a number changed.
const DATA_URL =
  'https://raw.githubusercontent.com/AssafAtias/ticket-monitor/data/status.json';
const REFRESH_MS = 30000;

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const STATUS_TEXT = {
  ok: age => `checked ${ago(age)}`,
  late: age => `checked ${ago(age)} — the scheduler is running late`,
  down: age => `LAST CHECKED ${ago(age)} — THE MONITOR MAY BE DOWN`,
};

function announce(text) {
  const live = document.getElementById('live');
  if (live && live.textContent !== text) live.textContent = text;
}

function render(status, ageSeconds) {
  const card = document.getElementById('card');
  card.replaceChildren();

  const fixture = status.fixture || {};
  card.append(el('h1', null, fixture.name || 'No fixture'));

  const when = kickoff(fixture.start);
  if (when) {
    card.append(el('p', 'when', fixture.venue ? `${when} · ${fixture.venue}` : when));
  }

  card.append(el('p', 'count', String(status.buyable ?? 0)));
  card.append(el('p', 'count-label',
    status.buyable === 1 ? 'seat buyable' : 'seats buyable'));

  if (status.tiers && status.tiers.length) {
    const table = el('table');
    for (const tier of status.tiers) {
      const tr = el('tr');
      tr.append(el('td', null, tier.category));
      tr.append(el('td', 'num', `${tier.price} ₪`));
      tr.append(el('td', tier.available ? 'has' : 'num',
        tier.available ? `${tier.available}` : 'sold out'));
      table.append(tr);
    }
    card.append(table);
  }

  const href = safeHref(fixture.url);
  if (href) {
    const cta = el('a', 'cta', 'Open the shop →');
    cta.href = href;
    cta.rel = 'noopener';
    cta.target = '_blank';
    card.append(cta);
  } else if (fixture.url) {
    card.append(el('p', 'error',
      `Shop link was rejected as unsafe: ${fixture.url}`));
  }

  if (status.max_per_order) {
    card.append(el('p', 'note',
      `max ${status.max_per_order} per customer · sign in before it fires`));
  }

  if (status.error) {
    card.append(el('p', 'error', `Last poll failed: ${status.error}`));
  }

  if (status.alert_delivered === false) {
    // Telegram is the only channel. If it failed, the seats in this card may
    // never have been announced anywhere.
    card.append(el('p', 'error',
      'The Telegram alert for these seats FAILED to send.'));
  }

  const level = freshness(ageSeconds);
  card.append(el('p', `status ${level}`, STATUS_TEXT[level](ageSeconds)));
  document.title = `${status.buyable ?? 0} · Ticket Monitor`;

  announce(liveSummary(status, ageSeconds));
}

function renderUnreachable(message) {
  const card = document.getElementById('card');
  card.replaceChildren();
  card.append(el('h1', null, 'Cannot reach the monitor'));
  card.append(el('p', 'error', `Could not show the monitor: ${message}`));
  card.append(el('p', 'status down',
    'The monitor itself may still be running.'));
  announce(`The monitor could not be reached: ${message}`);
}

let lastShown = 0;

async function tick() {
  let status;
  try {
    // Cache-bust: raw.githubusercontent.com caches for about five minutes.
    const resp = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    status = await resp.json();
  } catch (err) {
    const message = String((err && err.message) || err);
    // Keep the last reading visible. Its own staleness badge is more
    // honest than discarding real data over one blip.
    if (lastShown) noteRefreshFailure(message);
    else renderUnreachable(message);
    return;
  }

  const stamp = new Date(status.generated_at).getTime();
  // Responses can land out of order; an older document must never
  // replace a newer one already on screen.
  if (Number.isFinite(stamp)) {
    if (stamp < lastShown) return;
    lastShown = stamp;
  }

  try {
    render(status, (Date.now() - stamp) / 1000);
  } catch (err) {
    // A rendering fault is our bug, not the data's. Saying "could not
    // load status.json" would send someone debugging a file that is fine.
    renderUnreachable(
      `loaded the data but could not render it: ${String((err && err.message) || err)}`);
  }
}

function noteRefreshFailure(message) {
  const card = document.getElementById('card');
  let note = document.getElementById('refresh-note');
  if (!note) {
    note = el('p', 'error');
    note.id = 'refresh-note';
    card.append(note);
  }
  note.textContent = `Could not refresh (${message}). Showing the last reading.`;
  announce(`Could not refresh: ${message}. Showing the last reading.`);
}

tick();
setInterval(tick, REFRESH_MS);
