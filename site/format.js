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
