// Cron is scheduled every 5 minutes but GitHub delays runs under load, so
// "late" is expected and only "down" is alarming.
export const LATE_AFTER_SECONDS = 600;    // 10 min
export const DOWN_AFTER_SECONDS = 1800;   // 30 min

export function freshnessFor(ageSeconds, hasError) {
  // A failed poll's own timestamp is fresh; the numbers it carries are not.
  // Never let an errored document read as 'ok', or a six-hour upstream
  // outage renders as a healthy monitor with a green badge and a full
  // tier table - silence mistakable for "no tickets yet".
  const level = freshness(ageSeconds);
  if (!hasError) return level;
  return level === 'ok' ? 'late' : level;
}

export function freshness(ageSeconds) {
  if (typeof ageSeconds !== 'number' || Number.isNaN(ageSeconds)) return 'down';
  if (ageSeconds < LATE_AFTER_SECONDS) return 'ok';
  if (ageSeconds < DOWN_AFTER_SECONDS) return 'late';
  return 'down';
}
