import { describe, expect, it } from 'vitest';

import { type DecisionDraft, isDecidable, validateDraft } from '../decision';
import type { Approval } from '../types';

/** An S04-shaped approval: ₹41,200.00 above the growth-tier cap. */
function approval(overrides: Partial<Approval> = {}): Approval {
  const proposal = {
    kind: 'create_refund',
    capture_id: 'cap_7731C1',
    amount: { amount_minor: 4120000, currency: 'INR' },
    reason_code: 'buyer_request',
    captures_touched: ['cap_7731C1'],
    touches: [],
  };
  const decision = {
    permission: 'needs_approval' as const,
    rule_id: 'R2-elevated-refund',
    risk_tier: 'R2-elevated-refund',
    required_scope: 'payments:refund:write',
    reasons: ['₹41,200.00 is above the growth-tier autonomy cap of ₹5,000.00'],
    inputs: {},
    citations: [],
    expires_at: '2026-10-24T09:00:00+00:00',
  };
  return {
    approval_id: 'apr_abc',
    run_id: 'run_000001',
    ticket_id: 'tkt_apr1',
    thread_id: 'ticket:tkt_apr1',
    tenant_id: 'acme-payments',
    merchant_id: 'M-1012',
    fingerprint: 'f'.repeat(32),
    status: 'pending',
    created_at: '2026-10-22T09:00:00+00:00',
    expires_at: '2026-10-24T09:00:00+00:00',
    decided_at: null,
    reviewer: null,
    reviewer_note: null,
    decision,
    proposal,
    evidence: {
      payment_id: 'pay_7731C',
      captures: [
        {
          capture_id: 'cap_7731C1',
          amount: { amount_minor: 4120000, currency: 'INR' },
          refunded_amount: { amount_minor: 0, currency: 'INR' },
          captured_at: '2026-10-06T13:40:14Z',
        },
      ],
      duplicate_captures: [],
      resolution: {},
      citations: [],
    },
    modified_proposal: null,
    proposed_action: proposal,
    risk_tier: 'R2-elevated-refund',
    rule_id: 'R2-elevated-refund',
    required_scope: 'payments:refund:write',
    reasons: decision.reasons,
    ...overrides,
  };
}

function draft(overrides: Partial<DecisionDraft> = {}): DecisionDraft {
  return { action: 'approve', reviewer: 'dana@acme', note: '', amountInput: '', ...overrides };
}

describe('validateDraft', () => {
  it('accepts an approval with no note', () => {
    const result = validateDraft(draft(), approval());
    expect(result).toEqual({ ok: true, payload: { action: 'approve', reviewer: 'dana@acme' } });
  });

  it('requires a decision to be attributable', () => {
    const result = validateDraft(draft({ reviewer: '   ' }), approval());
    expect(result.ok).toBe(false);
  });

  it('requires a note on reject and on modify', () => {
    expect(validateDraft(draft({ action: 'reject' }), approval()).ok).toBe(false);
    expect(validateDraft(draft({ action: 'modify', amountInput: '20600' }), approval()).ok).toBe(
      false,
    );
  });

  it('lets a reviewer reduce the amount', () => {
    const result = validateDraft(
      draft({ action: 'modify', note: 'merchant kept one unit', amountInput: '20600' }),
      approval(),
    );
    expect(result).toEqual({
      ok: true,
      payload: {
        action: 'modify',
        reviewer: 'dana@acme',
        note: 'merchant kept one unit',
        amount_minor: 2060000,
      },
    });
  });

  it('refuses to raise the amount, naming the way to refund more', () => {
    // The proposal was evaluated against the capture's remainder and the
    // tenant's window; a larger figure has been checked against nothing.
    // The server refuses this too — this check exists so the reviewer is
    // told as they type rather than after a round trip.
    const result = validateDraft(
      draft({ action: 'modify', note: 'trying for more', amountInput: '50000' }),
      approval(),
    );
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.problem).toContain('not raise it');
      expect(result.problem).toContain('reject this');
    }
  });

  it('accepts the proposed amount exactly', () => {
    const result = validateDraft(
      draft({ action: 'modify', note: 'confirming as proposed', amountInput: '41200.00' }),
      approval(),
    );
    expect(result.ok).toBe(true);
  });

  it('refuses a zero or unreadable modification', () => {
    expect(
      validateDraft(draft({ action: 'modify', note: 'n', amountInput: '0' }), approval()).ok,
    ).toBe(false);
    expect(
      validateDraft(draft({ action: 'modify', note: 'n', amountInput: 'lots' }), approval()).ok,
    ).toBe(false);
  });
});

describe('isDecidable', () => {
  it('reads the status, not the clock', () => {
    // A row past its deadline that the sweep has not yet moved is still
    // pending, and the server will accept a decision on it. The console must
    // not refuse on the reviewer's behalf and lose that race for them.
    expect(isDecidable(approval({ status: 'pending' }))).toBe(true);
    expect(isDecidable(approval({ status: 'approved' }))).toBe(false);
    expect(isDecidable(approval({ status: 'expired' }))).toBe(false);
  });
});
