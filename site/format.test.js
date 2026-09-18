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
