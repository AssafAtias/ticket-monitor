// The staleness thresholds are the page's only real logic, and they are
// what stops a dead monitor from looking like a quiet one.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { freshness } from './freshness.js';

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
