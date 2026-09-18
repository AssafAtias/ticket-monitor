import { test } from 'node:test';
import assert from 'node:assert/strict';
import { safeHref, ago, kickoff, liveSummary } from './format.js';

// safeHref runs in a browser where `location` exists; node --test does not
// provide one, so give the module a minimal stand-in.
globalThis.location = { href: 'https://example.test/page' };

test('a normal shop link is allowed through', () => {
  assert.equal(safeHref('https://tickets.leaan.net/event/--02j286'),
               'https://tickets.leaan.net/event/--02j286');
});

test('a javascript: url is rejected', () => {
  // The whole point: this value comes from a third-party feed.
  assert.equal(safeHref('javascript:alert(1)'), null);
});

test('data: and other schemes are rejected', () => {
  assert.equal(safeHref('data:text/html,<script>x</script>'), null);
  assert.equal(safeHref('file:///etc/passwd'), null);
});

test('garbage and missing values are rejected rather than thrown on', () => {
  assert.equal(safeHref(''), null);
  assert.equal(safeHref(null), null);
  assert.equal(safeHref(undefined), null);
});

test('ago formats seconds, minutes and hours', () => {
  assert.equal(ago(5), '5s ago');
  assert.equal(ago(600), '10 min ago');
  assert.equal(ago(7200), '2 h ago');
});

test('ago clamps a skewed clock rather than counting backwards', () => {
  assert.equal(ago(-30), '0s ago');
});

test('ago says so when the age is unusable, instead of printing NaN', () => {
  assert.equal(ago(NaN), 'an unknown time ago');
  assert.equal(ago(null), 'an unknown time ago');
});

test('kickoff parses both formats the poller can emit', () => {
  assert.ok(kickoff('2026-09-19T17:00:00+00:00'));
  assert.ok(kickoff('2026-09-19T17:00:00.000Z'));
});

test('kickoff returns null rather than rendering "Invalid Date"', () => {
  assert.equal(kickoff('not a date'), null);
  assert.equal(kickoff(''), null);
  assert.equal(kickoff(undefined), null);
});

test('liveSummary announces the count and the age', () => {
  const t = liveSummary({ buyable: 3 }, 120);
  assert.match(t, /3 seats buyable/);
  assert.match(t, /2 min ago/);
});

test('liveSummary is singular for one seat', () => {
  assert.match(liveSummary({ buyable: 1 }, 5), /1 seat buyable/);
});

test('liveSummary announces a failed check rather than a seat count', () => {
  // A screen-reader user must not hear "0 seats buyable" when the truth
  // is that the check itself failed.
  const t = liveSummary({ buyable: 0, error: 'feed timed out' }, 60);
  assert.match(t, /last check failed/i);
  assert.match(t, /feed timed out/);
});

test('liveSummary handles no document at all', () => {
  assert.match(liveSummary(null, NaN), /could not be reached/i);
});

test('a protocol-relative url cannot redirect the CTA to another host', () => {
  // The reviewer's probe: this used to resolve to https://evil.test/x and
  // render as a perfectly normal "Open the shop" button.
  assert.equal(safeHref('//evil.test/x'), null);
});

test('a site-relative url is rejected rather than pointed at this page', () => {
  assert.equal(safeHref('/evil'), null);
  assert.equal(safeHref('evil'), null);
  assert.equal(safeHref('../evil'), null);
});

test('whitespace does not become a CTA that reloads the monitor', () => {
  // Worst case of all: the button looks live, and tapping it costs the user
  // the seconds they were racing for.
  assert.equal(safeHref('   '), null);
  assert.equal(safeHref('\n\t'), null);
});

test('a non-string feed value is rejected', () => {
  assert.equal(safeHref(12345), null);
  assert.equal(safeHref({}), null);
  assert.equal(safeHref([]), null);
});

test('safeHref does not consult the page it is running on', () => {
  // Nothing below has its own scheme, so nothing may be accepted, whatever
  // location happens to say.
  const saved = globalThis.location;
  globalThis.location = { href: 'https://assafatias.github.io/ticket-monitor/' };
  assert.equal(safeHref('/evil'), null);
  assert.equal(safeHref('//evil.test/x'), null);
  globalThis.location = saved;
});

test('liveSummary tells a screen reader how old the figures really are', () => {
  // The badge colour is the sighted user's cue; this is the only one a
  // screen-reader user gets.
  const t = liveSummary({ buyable: 393, error: 'feed timed out' }, 12, 21600);
  assert.match(t, /last check failed/i);
  assert.match(t, /last confirmed 6 h ago/);
});

test('liveSummary omits the confirmation clause when there is nothing to say', () => {
  const t = liveSummary({ buyable: 0, error: 'feed timed out' }, 12);
  assert.doesNotMatch(t, /last confirmed/);
});
