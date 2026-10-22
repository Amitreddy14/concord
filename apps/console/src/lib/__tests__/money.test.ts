import { describe, expect, it } from 'vitest';

import { exponentFor, formatMoney, parseMoneyInput } from '../money';

describe('formatMoney', () => {
  it('splits the integer rather than dividing it', () => {
    // S04's proposal, as a reviewer sees it in the queue.
    expect(formatMoney({ amount_minor: 4120000, currency: 'INR' })).toBe('₹41,200.00');
    expect(formatMoney({ amount_minor: 89000, currency: 'INR' })).toBe('₹890.00');
  });

  it('keeps trailing zeros a division would lose', () => {
    // 4120000 / 100 is 41200, and 41200.toFixed(2) gets the string back —
    // but only because the value happened to be exact. These are the cases
    // where the string surgery and the arithmetic stay identical, which is
    // the point of not relying on the arithmetic.
    expect(formatMoney({ amount_minor: 100, currency: 'INR' })).toBe('₹1.00');
    expect(formatMoney({ amount_minor: 10, currency: 'INR' })).toBe('₹0.10');
    expect(formatMoney({ amount_minor: 1, currency: 'INR' })).toBe('₹0.01');
    expect(formatMoney({ amount_minor: 0, currency: 'INR' })).toBe('₹0.00');
  });

  it('groups thousands', () => {
    expect(formatMoney({ amount_minor: 100000000, currency: 'USD' })).toBe('$1,000,000.00');
    expect(formatMoney({ amount_minor: 99999, currency: 'USD' })).toBe('$999.99');
  });

  it('respects a zero-decimal currency', () => {
    // A ¥1,000 amount stored as 1000 minor units is ¥1,000, not ¥10.00. A
    // formatter assuming two decimals everywhere is wrong by a factor of a
    // hundred, which on a refund is not a cosmetic bug.
    expect(exponentFor('JPY')).toBe(0);
    expect(formatMoney({ amount_minor: 1000, currency: 'JPY' })).toBe('¥1,000');
  });

  it('falls back to the code for a currency it has no symbol for', () => {
    expect(formatMoney({ amount_minor: 12345, currency: 'AED' })).toBe('AED 123.45');
  });

  it('renders a negative amount', () => {
    expect(formatMoney({ amount_minor: -5000, currency: 'INR' })).toBe('-₹50.00');
  });
});

describe('parseMoneyInput', () => {
  it('reads a reviewer’s amount into minor units without multiplying', () => {
    expect(parseMoneyInput('20600', 'INR')).toBe(2060000);
    expect(parseMoneyInput('20600.00', 'INR')).toBe(2060000);
    expect(parseMoneyInput('20600.01', 'INR')).toBe(2060001);
    expect(parseMoneyInput('0.07', 'INR')).toBe(7);
  });

  it('is immune to the float the obvious implementation would produce', () => {
    // `Math.round(Number('20600.01') * 100)` happens to work; without the
    // round it is 2060000.9999999998. Reading digits cannot land there at
    // all, so there is no rounding step to forget.
    expect(parseMoneyInput('20600.01', 'INR')).toBe(2060001);
    expect(parseMoneyInput('1.1', 'INR')).toBe(110);
    expect(parseMoneyInput('8.07', 'INR')).toBe(807);
  });

  it('tolerates the separators a person types', () => {
    expect(parseMoneyInput(' 41,200.00 ', 'INR')).toBe(4120000);
  });

  it('refuses more precision than the currency has', () => {
    // Truncating silently would turn a typo into a slightly different
    // refund, which is the reviewer's decision to make and not this
    // function's.
    expect(parseMoneyInput('100.001', 'INR')).toBeNull();
    expect(parseMoneyInput('100.5', 'JPY')).toBeNull();
  });

  it('refuses what is not an amount', () => {
    expect(parseMoneyInput('', 'INR')).toBeNull();
    expect(parseMoneyInput('abc', 'INR')).toBeNull();
    expect(parseMoneyInput('-50', 'INR')).toBeNull();
    expect(parseMoneyInput('1e5', 'INR')).toBeNull();
    expect(parseMoneyInput('100.', 'INR')).toBeNull();
  });

  it('round-trips through formatMoney', () => {
    for (const minor of [1, 10, 99, 100, 89000, 2060000, 4120000]) {
      const text = formatMoney({ amount_minor: minor, currency: 'INR' }).replace('₹', '');
      expect(parseMoneyInput(text, 'INR')).toBe(minor);
    }
  });
});
