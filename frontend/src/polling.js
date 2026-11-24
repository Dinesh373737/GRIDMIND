export const CLOUD_POLL_MS = 1250;
export const LOCAL_POLL_MS = 1100;

export function retryAfterMs(value, now = Date.now()) {
  if (!value) return 0;
  const header = value.trim();
  if (/^\d+$/.test(header)) {
    const milliseconds = Number(header) * 1000;
    return Number.isSafeInteger(milliseconds) ? milliseconds : 0;
  }
  if (!/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), /i.test(header)) return 0;
  const timestamp = Date.parse(header);
  return Number.isFinite(timestamp) ? Math.max(0, timestamp - now) : 0;
}

export function pollDelayMs(attempt, retryAfter = 0) {
  // Bound fallback delay; a valid server deadline can ask us to wait longer.
  const fallback = Math.min(30_000, 2500 * (2 ** Math.min(4, Math.max(0, attempt - 1))));
  return Math.max(fallback, retryAfter);
}
