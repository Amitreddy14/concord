/**
 * Money, formatted without ever dividing it.
 *
 * CONTRIBUTING: "Money is always integer minor units plus an ISO 4217
 * currency code. Floats are banned in any path that touches an amount; this
 * is enforced by a lint rule, not by discipline." The Python side has had
 * that lint rule since phase 1. The console displays the same amounts, and
 * `amount_minor / 100` is the obvious way to do it — which is exactly why
 * this module exists.
 *
 * The objection to dividing is not that ₹41,200.00 would render wrong today.
 * It is that once an amount is a float it is a float everywhere downstream:
 * a reviewer's edit typed as 20600.00 and multiplied back by 100 can arrive
 * as 2059999.9999999998, and the integer that reaches the refund API is then
 * off by one minor unit. So minor units never become a fraction. Formatting
 * splits the integer; parsing reads digits.
 */

export type Money = {
  readonly amount_minor: number;
  readonly currency: string;
};

/** Minor units per major unit, by ISO 4217 exponent. */
const EXPONENT: Record<string, number> = {
  INR: 2,
  USD: 2,
  EUR: 2,
  GBP: 2,
  // Zero-decimal currencies are not a hypothetical: a ¥1,000 amount stored
  // as 1000 minor units is ¥1,000 and not ¥10.00, and a formatter that
  // assumes two decimals everywhere is wrong by a factor of a hundred.
  JPY: 0,
  KRW: 0,
};
const DEFAULT_EXPONENT = 2;

const SYMBOL: Record<string, string> = {
  INR: '₹',
  USD: '$',
  EUR: '€',
  GBP: '£',
  JPY: '¥',
  KRW: '₩',
};

export function exponentFor(currency: string): number {
  return EXPONENT[currency.toUpperCase()] ?? DEFAULT_EXPONENT;
}

/** Group an integer's digits in threes, without Intl's locale guessing. */
function groupDigits(digits: string): string {
  let out = '';
  for (let i = 0; i < digits.length; i += 1) {
    if (i > 0 && (digits.length - i) % 3 === 0) {
      out += ',';
    }
    out += digits[i];
  }
  return out;
}

/**
 * Render an amount for a human.
 *
 * Pure string surgery on the integer: the minor units are split at the
 * exponent, never divided.
 */
export function formatMoney(money: Money): string {
  const exponent = exponentFor(money.currency);
  const negative = money.amount_minor < 0;
  const digits = String(Math.abs(money.amount_minor)).padStart(exponent + 1, '0');

  const major = digits.slice(0, digits.length - exponent) || '0';
  const minor = exponent > 0 ? digits.slice(digits.length - exponent) : '';

  const symbol = SYMBOL[money.currency.toUpperCase()] ?? `${money.currency} `;
  const body = minor ? `${groupDigits(major)}.${minor}` : groupDigits(major);
  return `${negative ? '-' : ''}${symbol}${body}`;
}

/**
 * Read a reviewer's typed amount into minor units.
 *
 * Returns null rather than throwing or guessing, because the caller has to
 * show the reviewer what was wrong with it. Digits are read as digits and
 * padded; nothing is multiplied by a power of ten, so "20600.01" cannot
 * arrive as 2060000.9999999998.
 */
export function parseMoneyInput(raw: string, currency: string): number | null {
  const text = raw.trim().replace(/[,\s]/g, '');
  if (text === '') {
    return null;
  }

  const exponent = exponentFor(currency);
  const match = /^(\d+)(?:\.(\d+))?$/.exec(text);
  if (!match) {
    return null;
  }

  const [, majorPart = '0', minorPart = ''] = match;
  if (minorPart.length > exponent) {
    // More precision than the currency has. Truncating silently would turn a
    // typo into a slightly different refund, so this is the caller's problem
    // to report.
    return null;
  }

  const minor = minorPart.padEnd(exponent, '0');
  const combined = `${majorPart}${minor}`;
  const value = Number.parseInt(combined, 10);
  return Number.isSafeInteger(value) ? value : null;
}
