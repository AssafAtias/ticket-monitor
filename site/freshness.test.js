// The staleness thresholds are the page's only real logic, and they are
// what stops a dead monitor from looking like a quiet one.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { freshness, freshnessFor } from './freshness.js';

test('a recent check is normal', () => {
  assert.equal(freshness(0), 'ok');
  assert.equal(freshness(120), 'ok');
  assert.equal(freshness(599), 'ok');
});

test('past ten minutes the cron is running late', () => {
  assert.equal(freshness(600), 'late');
  assert.equal(freshness(1799), 'late');
});

test('past thirty minutes the monitor may be down', () => {
  assert.equal(freshness(1800), 'down');
  assert.equal(freshness(86400), 'down');
});

test('a clock skewed into the future is not treated as stale', () => {
  assert.equal(freshness(-30), 'ok');
});

test('an unusable age is treated as down, never as fresh', () => {
  // Erring towards "ok" here would hide a broken monitor behind a green badge.
  assert.equal(freshness(NaN), 'down');
  assert.equal(freshness(null), 'down');
  assert.equal(freshness(undefined), 'down');
});

test('an errored document never reads as ok, however recent it is', () => {
  // The failed poll's own timestamp is fresh. The numbers it carries are
  // not, and a green badge over six-hour-old counts is the exact way a
  // blind monitor passes for a healthy one.
  assert.equal(freshnessFor(10, true), 'late');
  assert.equal(freshnessFor(0, true), 'late');
  assert.equal(freshnessFor(599, true), 'late');
});

test('an errored document that is also old still reads as down', () => {
  assert.equal(freshnessFor(45 * 60, true), 'down');
  assert.equal(freshnessFor(2700, true), 'down');
});

test('an errored document already late is not downgraded back', () => {
  assert.equal(freshnessFor(900, true), 'late');
});

test('a clean document is graded exactly as before', () => {
  assert.equal(freshnessFor(10, false), 'ok');
  assert.equal(freshnessFor(700, false), 'late');
  assert.equal(freshnessFor(3600, false), 'down');
});

test('an unusable age with an error is still down, not softened to late', () => {
  assert.equal(freshnessFor(NaN, true), 'down');
  assert.equal(freshnessFor(undefined, true), 'down');
});
