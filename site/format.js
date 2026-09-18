// Pure formatting and safety helpers, kept out of app.js so they can be
// tested: app.js runs on import and cannot be exercised by node --test.

export function safeHref(url) {
  // fixture.url comes from a third-party ticket feed and is the only
  // feed-sourced value that reaches a live href rather than textContent.
  // A javascript: URL here would run in this page's origin on tap.
  // Falsy input (missing/empty) is rejected up front: new URL('', base)
  // does not throw, it resolves to the page's own base href, which would
  // otherwise slip past the scheme check below.
  if (!url) return null;
  try {
    const parsed = new URL(String(url), location.href);
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
