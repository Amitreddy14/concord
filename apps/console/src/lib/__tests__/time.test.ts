import { describe, expect, it } from 'vitest';

import { formatDuration, formatTimestamp, SOON_SECONDS, URGENT_SECONDS, urgencyOf } from '../time';

describe('formatDuration', () => {
  it('reads as a reviewer triaging a queue', () => {
    expect(formatDuration(47 * 3600 + 59 * 60)).toBe('1d 23h');
    expect(formatDuration(4 * 3600 + 12 * 60)).toBe('4h 12m');
    expect(formatDuration(38 * 60)).toBe('38m');
    expect(formatDuration(30)).toBe('under a minute');
  });

  it('drops the smaller unit when it is zero', () => {
    expect(formatDuration(2 * 86400)).toBe('2d');
    expect(formatDuration(3 * 3600)).toBe('3h');
  });

  it('says expired rather than counting backwards', () => {
    expect(formatDuration(0)).toBe('expired');
    expect(formatDuration(-60)).toBe('expired');
  });
});

describe('urgencyOf', () => {
  it('sorts into now, today and later', () => {
    expect(urgencyOf(0)).toBe('expired');
    expect(urgencyOf(URGENT_SECONDS)).toBe('urgent');
    expect(urgencyOf(URGENT_SECONDS + 1)).toBe('soon');
    expect(urgencyOf(SOON_SECONDS)).toBe('soon');
    expect(urgencyOf(SOON_SECONDS + 1)).toBe('normal');
  });
});

describe('formatTimestamp', () => {
  it('shows UTC, because the audit trail is in UTC', () => {
    expect(formatTimestamp('2026-10-22T09:53:26+00:00')).toBe('2026-10-22 09:53 UTC');
  });

  it('returns an unparseable value unchanged rather than rendering NaN', () => {
    expect(formatTimestamp('not a date')).toBe('not a date');
  });
});
