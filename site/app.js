import { freshnessFor } from './freshness.js';
import { safeHref, ago, kickoff, liveSummary, dataSources, refreshDelay } from './format.js';

// The page is served from GitHub Pages but the data lives on the `data`
// branch. Primary source is raw.githubusercontent.com (Access-Control-
// Allow-Origin: *, no rate limit); that keeps Pages from redeploying every
// five minutes just because a number changed. It has a CDN edge that can
// serve a poisoned cache entry for one path indefinitely (verified live:
// persistent 503s from the browser for this exact URL while curl and every
// other path on the same branch returned 200), so a second source - the
// GitHub contents API - is tried if the first fails. See dataSources() in
// format.js for why, and for the ordering.
const SOURCES = dataSources('AssafAtias/ticket-monitor');

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

// A failed poll still has a fresh generated_at, so the normal wording would
// read "checked 12s ago" over numbers nobody has confirmed for hours.
const ERROR_STATUS_TEXT = {
  late: age => `TRIED ${ago(age)} — that check FAILED`,
  down: age => `TRIED ${ago(age)} — THE MONITOR MAY BE DOWN`,
};

function secondsSince(iso) {
  const stamp = new Date(iso ?? '').getTime();
  return Number.isFinite(stamp) ? (Date.now() - stamp) / 1000 : null;
}

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

  if (status.error) {
    // Above the count, not below it: on a phone the error box used to sit
    // under a 46px number and a full tier table, off the bottom of the
    // screen, while the page read as healthy.
    card.append(el('p', 'error',
      `Last poll FAILED: ${status.error}. The figures below are the last ` +
      'ones we could confirm, not current ones.'));
  }

  if (status.alert_delivered === false) {
    // Telegram is the only channel. If it failed, the seats in this card may
    // never have been announced anywhere - the worst outcome available, so
    // it goes at the top too.
    card.append(el('p', 'error',
      'The Telegram alert for these seats FAILED to send.'));
  }

  card.append(el('p', 'count', String(status.buyable ?? 0)));
  card.append(el('p', 'count-label',
    status.buyable === 1 ? 'seat buyable' : 'seats buyable'));

  const confirmedAge = secondsSince(status.last_success_at);
  if (status.error) {
    card.append(el('p', 'stale', confirmedAge === null
      ? 'never confirmed'
      : `last confirmed ${ago(confirmedAge)}`));
  }

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

  const level = freshnessFor(ageSeconds, Boolean(status.error));
  const text = (status.error && ERROR_STATUS_TEXT[level]) || STATUS_TEXT[level];
  card.append(el('p', `status ${level}`, text(ageSeconds)));
  document.title = status.error
    ? '! · Ticket Monitor' : `${status.buyable ?? 0} · Ticket Monitor`;

  announce(liveSummary(status, ageSeconds,
                       confirmedAge === null ? undefined : confirmedAge));
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
// Which source last produced a document. Feeds refreshDelay() so that once
// we've fallen back to the rate-limited contents API, we poll it less often
// - a viewer leaving the page open for an hour during a raw outage must not
// burn through the 60-requests-per-hour budget. Starts unset, which
// refreshDelay() treats the same as the primary.
let lastSourceName;

function scheduleNext() {
  setTimeout(tick, refreshDelay(lastSourceName));
}

async function tick() {
  let status;
  const attempts = [];
  for (const source of SOURCES) {
    try {
      // No cache-buster here: raw.githubusercontent.com's CDN was verified
      // live to normalise the query string away and serve the same 503
      // either way, so `?t=${Date.now()}` did nothing but look reassuring.
      // Do not re-add it - use a real second source instead (below).
      const resp = await fetch(source.url, { cache: 'no-store', headers: source.headers });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      status = await resp.json();
      lastSourceName = source.name;
      break;
    } catch (err) {
      attempts.push(`${source.name}: ${String((err && err.message) || err)}`);
    }
  }

  if (!status) {
    const message = `all sources failed (${attempts.join('; ')})`;
    // Keep the last reading visible. Its own staleness badge is more
    // honest than discarding real data over one blip.
    if (lastShown) noteRefreshFailure(message);
    else renderUnreachable(message);
    scheduleNext();
    return;
  }

  const stamp = new Date(status.generated_at).getTime();
  // Responses can land out of order; an older document must never
  // replace a newer one already on screen.
  if (Number.isFinite(stamp)) {
    if (stamp < lastShown) { scheduleNext(); return; }
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
  scheduleNext();
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
