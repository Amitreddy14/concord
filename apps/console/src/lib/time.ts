/**
 * Time, as a reviewer needs to read it.
 *
 * The API sends `expires_in_seconds` alongside each pending approval rather
 * than only `expires_at`, so a countdown does not depend on the reviewer's
 * clock agreeing with the server's. This module formats what the server
 * computed; it does not compute deadlines itself.
 */

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** "4h 12m", "38m", "under a minute" — enough to triage a queue by. */
export function formatDuration(seconds: number): string {
  if (seconds <= 0) {
    return 'expired';
  }
  if (seconds < MINUTE) {
    return 'under a minute';
  }

  const days = Math.floor(seconds / DAY);
  const hours = Math.floor((seconds % DAY) / HOUR);
  const minutes = Math.floor((seconds % HOUR) / MINUTE);

  if (days > 0) {
    return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  }
  if (hours > 0) {
    return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  }
  return `${minutes}m`;
}

/**
 * How much attention a row's remaining time deserves.
 *
 * Three bands rather than a gradient: a reviewer scanning a queue is sorting
 * into "now", "today" and "later", and a continuous colour ramp gives them a
 * decision to make where a label would have made it for them.
 */
export type Urgency = 'expired' | 'urgent' | 'soon' | 'normal';

export const URGENT_SECONDS = 2 * HOUR;
export const SOON_SECONDS = 8 * HOUR;

export function urgencyOf(secondsRemaining: number): Urgency {
  if (secondsRemaining <= 0) {
    return 'expired';
  }
  if (secondsRemaining <= URGENT_SECONDS) {
    return 'urgent';
  }
  if (secondsRemaining <= SOON_SECONDS) {
    return 'soon';
  }
  return 'normal';
}

/** An absolute timestamp, in UTC, for the audit trail rather than the queue. */
export function formatTimestamp(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  return `${parsed.toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}
