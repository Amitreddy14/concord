'use client';

import { useRouter } from 'next/navigation';
import { useState, useTransition } from 'react';

import { ApiError, submitDecision } from '@/lib/api';
import { type Action, type DecisionDraft, validateDraft } from '@/lib/decision';
import { formatMoney } from '@/lib/money';
import type { Approval } from '@/lib/types';

/*
 * The decision.
 *
 * Three actions, and the form changes shape for the third: a modification
 * needs an amount and a note, an approval needs neither. Offering an amount
 * field on every action would be offering "approve with an amount", which is
 * a second way to modify that skips the note a modification requires — the
 * API refuses it, and the form should not suggest it.
 *
 * Every rule checked here is also checked on the server, and the server's
 * check is the one with authority. This one exists so a reviewer is told as
 * they type rather than after a round trip.
 */
export function DecisionForm({ approval }: { approval: Approval }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [action, setAction] = useState<Action>('approve');
  const [reviewer, setReviewer] = useState('');
  const [note, setNote] = useState('');
  const [amountInput, setAmountInput] = useState('');
  const [problem, setProblem] = useState<{ title: string; detail: string } | null>(null);

  const proposed = approval.proposal.amount;

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setProblem(null);

    const draft: DecisionDraft = { action, reviewer, note, amountInput };
    const checked = validateDraft(draft, approval);
    if (!checked.ok) {
      setProblem({ title: 'Check the decision', detail: checked.problem });
      return;
    }

    startTransition(async () => {
      try {
        const result = await submitDecision(approval.approval_id, checked.payload);
        if (!result.resumed) {
          // The decision is recorded either way. Saying so beats implying a
          // refund is on its way when no run was there to continue.
          setProblem({
            title: `Recorded as ${result.status}, but no run was resumed`,
            detail:
              result.detail ??
              'The decision is saved. The run that was waiting is not in this API process, ' +
                'so nothing has resumed yet.',
          });
          return;
        }
        router.refresh();
      } catch (error) {
        setProblem(describe(error));
      }
    });
  }

  return (
    <form onSubmit={onSubmit}>
      {problem ? (
        <div className="problem" role="alert">
          <strong>{problem.title}</strong>
          {problem.detail}
        </div>
      ) : null}

      <fieldset>
        <legend>Your decision</legend>
        <div className="choices">
          {(['approve', 'reject', 'modify'] as const).map((value) => (
            <label key={value} className="choice">
              <input
                type="radio"
                name="action"
                value={value}
                checked={action === value}
                onChange={() => setAction(value)}
              />
              {label(value, proposed)}
            </label>
          ))}
        </div>
      </fieldset>

      {action === 'modify' && proposed ? (
        <div className="field">
          <label htmlFor="amount">Refund instead</label>
          <input
            id="amount"
            className="amount-input"
            type="text"
            inputMode="decimal"
            value={amountInput}
            onChange={(event) => setAmountInput(event.target.value)}
            placeholder={formatMoney(proposed).replace(/^[^\d-]+/, '')}
            autoComplete="off"
          />
          <p className="hint">
            In {proposed.currency}, up to {formatMoney(proposed)}. To refund more, reject this and
            let the agent re-evaluate against the ledger.
          </p>
        </div>
      ) : null}

      <div className="field">
        <label htmlFor="reviewer">Deciding as</label>
        <input
          id="reviewer"
          type="text"
          value={reviewer}
          onChange={(event) => setReviewer(event.target.value)}
          placeholder="you@acme"
          autoComplete="off"
        />
      </div>

      <div className="field">
        <label htmlFor="note">
          {action === 'approve' ? 'Note (optional)' : 'Why — this tenant requires a note'}
        </label>
        <textarea id="note" value={note} onChange={(event) => setNote(event.target.value)} />
      </div>

      <button type="submit" disabled={pending}>
        {pending ? 'Recording…' : submitLabel(action)}
      </button>
    </form>
  );
}

function label(action: Action, proposed: Approval['proposal']['amount']): string {
  switch (action) {
    case 'approve':
      return proposed ? `Approve ${formatMoney(proposed)}` : 'Approve';
    case 'reject':
      return 'Reject';
    case 'modify':
      return 'Refund less';
  }
}

/** The button says what happens, and keeps the name the action had. */
function submitLabel(action: Action): string {
  switch (action) {
    case 'approve':
      return 'Approve refund';
    case 'reject':
      return 'Reject refund';
    case 'modify':
      return 'Approve reduced refund';
  }
}

/**
 * Turn a failure into something a reviewer can act on.
 *
 * The status codes mean different things to do, which is why the API uses
 * them: a conflict means reload, a bad request means fix the form, and
 * neither should be retried blind.
 */
function describe(error: unknown): { title: string; detail: string } {
  if (!(error instanceof ApiError)) {
    return {
      title: 'Something went wrong',
      detail: 'The decision was not recorded. Reload and check whether it landed.',
    };
  }
  if (error.isConflict) {
    return {
      title: 'Already decided',
      detail: `${error.message} Reload to see the recorded decision.`,
    };
  }
  if (error.isGone) {
    return { title: 'This approval is gone', detail: 'Reload the queue.' };
  }
  if (error.status === 0) {
    return { title: 'Can’t reach the API', detail: error.message };
  }
  return { title: 'The decision was refused', detail: error.message };
}
