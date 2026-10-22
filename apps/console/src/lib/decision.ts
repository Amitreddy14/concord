/**
 * What the reviewer may submit, checked before it is sent.
 *
 * Every rule here is also enforced in `apps/api/approvals.py`, and that is
 * the one that counts. This is not defence in depth — it is the UX half of a
 * rule whose authority lives on the server. A reviewer who types 50,000 into
 * a field and learns it was refused after a round trip has been told late;
 * one who is told as they type has been told usefully.
 *
 * The distinction matters because it decides what happens when the two
 * disagree. If this module is laxer than the server, the server refuses and
 * the console shows the problem detail — annoying, correct. If this module
 * were the only check, a reviewer with a browser console would be the policy
 * engine. So nothing here is ever the reason an amount is accepted; it is
 * only ever a reason to stop and say so.
 */

import { parseMoneyInput } from './money';
import type { Approval, DecisionPayload } from './types';

export type Action = 'approve' | 'reject' | 'modify';

/** Statuses the tenant requires a note on. Mirrors `require_reviewer_note_on`. */
export const NOTE_REQUIRED_ON: Readonly<Record<Action, boolean>> = {
  approve: false,
  reject: true,
  modify: true,
};

export type DecisionDraft = {
  readonly action: Action;
  readonly reviewer: string;
  readonly note: string;
  readonly amountInput: string;
};

export type ValidationResult =
  | { readonly ok: true; readonly payload: DecisionPayload }
  | { readonly ok: false; readonly problem: string };

export function validateDraft(draft: DecisionDraft, approval: Approval): ValidationResult {
  const reviewer = draft.reviewer.trim();
  if (reviewer === '') {
    return { ok: false, problem: 'A decision has to be attributable; enter who is deciding.' };
  }

  const note = draft.note.trim();
  if (NOTE_REQUIRED_ON[draft.action] && note === '') {
    return {
      ok: false,
      problem: `This tenant requires a reviewer note on ${draft.action}.`,
    };
  }

  if (draft.action !== 'modify') {
    // An amount alongside an approval would be a second, undocumented way to
    // modify — one that skips the note a modification requires. The server
    // refuses it; the form does not offer it.
    return {
      ok: true,
      payload:
        note === '' ? { action: draft.action, reviewer } : { action: draft.action, reviewer, note },
    };
  }

  const proposed = approval.proposal.amount;
  if (!proposed) {
    return { ok: false, problem: 'This proposal has no amount to modify.' };
  }

  const amountMinor = parseMoneyInput(draft.amountInput, proposed.currency);
  if (amountMinor === null) {
    return {
      ok: false,
      problem: `Enter an amount in ${proposed.currency}, to at most 2 decimal places.`,
    };
  }
  if (amountMinor <= 0) {
    return { ok: false, problem: 'A modified refund has to be more than zero.' };
  }
  if (amountMinor > proposed.amount_minor) {
    // The proposal was evaluated against the capture's unrefunded remainder
    // and the tenant's window. A larger figure has been checked against
    // nothing, and would arrive at the mutating node carrying an approval
    // that appears to authorise it.
    return {
      ok: false,
      problem:
        'A reviewer may reduce the proposed amount but not raise it. ' +
        'To refund more, reject this and let the agent re-evaluate.',
    };
  }

  return { ok: true, payload: { action: 'modify', reviewer, note, amount_minor: amountMinor } };
}

/**
 * Whether this approval can still be decided.
 *
 * Read from the status rather than from the clock. A row whose deadline has
 * passed but which the sweep has not yet moved is still `pending`, and the
 * server will accept a decision on it — the race is real and whoever gets
 * there first wins, so the console must not pre-emptively refuse on the
 * reviewer's behalf.
 */
export function isDecidable(approval: Approval): boolean {
  return approval.status === 'pending';
}
