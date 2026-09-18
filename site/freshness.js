// Cron is scheduled every 5 minutes but GitHub delays runs under load, so
// "late" is expected and only "down" is alarming.
export const LATE_AFTER_SECONDS = 600;    // 10 min
export const DOWN_AFTER_SECONDS = 1800;   // 30 min

export function freshness(ageSeconds) {
  if (typeof ageSeconds !== 'number' || Number.isNaN(ageSeconds)) return 'down';
  if (ageSeconds < LATE_AFTER_SECONDS) return 'ok';
  if (ageSeconds < DOWN_AFTER_SECONDS) return 'late';
  return 'down';
}
