// Pure formatting and safety helpers, kept out of app.js so they can be
// tested: app.js runs on import and cannot be exercised by node --test.

export function safeHref(url) {
  // fixture.url comes from a third-party ticket feed and is the only
  // feed-sourced value that reaches a live href rather than textContent.
  // A javascript: URL here would run in this page's origin on tap.
  //
  // Parsed with NO base, so anything without its own scheme throws and is
  // rejected. Resolving against the page turned '//evil.test/x' into a CTA
  // pointing at an arbitrary host, and whitespace into a CTA that merely
  // reloaded the monitor - at the moment the user is racing for a
  // one-per-customer ticket.
  if (!url) return null;
  try {
    const parsed = new URL(String(url));
    return (parsed.protocol === 'https:' || parsed.protocol === 'http:')
      ? parsed.href : null;
  } catch {
    return null;
  }
}

export function ago(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) {
    return 'an unknown time ago';
  }
  if (seconds < 90) return `${Math.max(0, Math.round(seconds))}s ago`;
  const mins = Math.round(seconds / 60);
  if (mins < 90) return `${mins} min ago`;
  return `${Math.round(mins / 60)} h ago`;
}

export function liveSummary(status, ageSeconds, lastSuccessSeconds) {
  // What a screen reader hears. Kept pure and tested because it is the
  // only channel by which a non-sighted user learns the monitor died.
  if (!status) return 'The monitor could not be reached.';
  if (status.error) {
    // The counts are still on screen but they are not current, and a
    // screen-reader user has no badge colour to tell them so.
    const since = typeof lastSuccessSeconds === 'number'
      ? ` The figures shown were last confirmed ${ago(lastSuccessSeconds)}.` : '';
    return `The last check failed: ${status.error}. Checked ${ago(ageSeconds)}.${since}`;
  }
  const n = status.buyable ?? 0;
  return `${n} seat${n === 1 ? '' : 's'} buyable, checked ${ago(ageSeconds)}.`;
}

// Verified live on 2026-09-18: raw.githubusercontent.com had a poisoned
// CDN edge-cache entry for this one path (persistent 503s from the browser,
// while curl and every other path on the same branch returned 200). The
// GitHub contents API is the fallback for exactly that failure mode - it
// goes through a different edge and is unaffected - but GitHub caps it at
// 60 requests/hour per viewer IP, so it must not become the steady-state
// source. jsDelivr also 200s but caches branch refs for up to 12 hours,
// useless for a 5-minute poll cadence, and is intentionally not listed.
export function dataSources(repo) {
  return [
    {
      name: 'raw',
      url: `https://raw.githubusercontent.com/${repo}/data/status.json`,
      headers: {},
    },
    {
      // `Accept: application/vnd.github.raw` makes this endpoint return the
      // raw file body directly, instead of a JSON envelope with the content
      // base64-encoded inside it.
      name: 'contents-api',
      url: `https://api.github.com/repos/${repo}/contents/status.json?ref=data`,
      headers: { Accept: 'application/vnd.github.raw' },
    },
  ];
}

const REFRESH_DELAYS = {
  raw: 60000,
  'contents-api': 120000,
};

export function refreshDelay(sourceName) {
  // Unknown or absent names (e.g. before any fetch has ever succeeded) get
  // the primary's cadence, not the slower one - there is no evidence yet
  // that we are leaning on the rate-limited fallback, and defaulting to the
  // long delay would just make the page feel unresponsive for no reason.
  return REFRESH_DELAYS[sourceName] ?? 60000;
}

export function kickoff(value) {
  // Returns a formatted local date, or null when the value is absent or
  // unparseable - new Date('nonsense') does not throw, it renders the
  // literal text "Invalid Date" to the user.
  if (!value) return null;
  const when = new Date(value);
  if (!Number.isFinite(when.getTime())) return null;
  return when.toLocaleString(undefined, {
    weekday: 'short', day: 'numeric', month: 'short',
    hour: '2-digit', minute: '2-digit',
  });
}
