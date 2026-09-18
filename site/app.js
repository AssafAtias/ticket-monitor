import { freshness } from './freshness.js';

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

function ago(seconds) {
  if (seconds < 90) return `${Math.max(0, Math.round(seconds))}s ago`;
  const mins = Math.round(seconds / 60);
  if (mins < 90) return `${mins} min ago`;
  return `${Math.round(mins / 60)} h ago`;
}

const STATUS_TEXT = {
  ok: age => `checked ${ago(age)}`,
  late: age => `checked ${ago(age)} — the scheduler is running late`,
  down: age => `LAST CHECKED ${ago(age)} — THE MONITOR MAY BE DOWN`,
};

function render(status, ageSeconds) {
  const card = document.getElementById('card');
  card.replaceChildren();

  const fixture = status.fixture || {};
  card.append(el('h1', null, fixture.name || 'No fixture'));

  if (fixture.start) {
    const kickoff = new Date(fixture.start);
    const when = kickoff.toLocaleString(undefined, {
      weekday: 'short', day: 'numeric', month: 'short',
      hour: '2-digit', minute: '2-digit',
    });
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

  if (fixture.url) {
    const cta = el('a', 'cta', 'Open the shop →');
    cta.href = fixture.url;
    cta.rel = 'noopener';
    cta.target = '_blank';
    card.append(cta);
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
}

function renderUnreachable(message) {
  const card = document.getElementById('card');
  card.replaceChildren();
  card.append(el('h1', null, 'Cannot reach the monitor'));
  card.append(el('p', 'error', message));
  card.append(el('p', 'status down',
    'This page could not load status.json. The monitor itself may still be running.'));
}

async function tick() {
  try {
    // Cache-bust: raw.githubusercontent.com caches for about five minutes.
    const resp = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const status = await resp.json();
    const age = (Date.now() - new Date(status.generated_at).getTime()) / 1000;
    render(status, age);
  } catch (err) {
    renderUnreachable(String(err.message || err));
  }
}

tick();
setInterval(tick, REFRESH_MS);
