import Link from 'next/link';
import { notFound } from 'next/navigation';

import { ApiError, fetchApproval } from '@/lib/api';
import { formatMoney } from '@/lib/money';
import { formatTimestamp } from '@/lib/time';
import type { Evidence, Money } from '@/lib/types';

import { DecisionForm } from './decision-form';

/*
 * The decision, beside the evidence.
 *
 * Two columns rather than a stack, because the reviewer's job is comparing
 * the ask against the facts, and stacking them means scrolling between the
 * two things being compared.
 */
export default async function ApprovalPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  let approval: Awaited<ReturnType<typeof fetchApproval>>;
  try {
    approval = await fetchApproval(id);
  } catch (error) {
    if (error instanceof ApiError && error.isGone) {
      notFound();
    }
    throw error;
  }

  const proposal = approval.modified_proposal ?? approval.proposal;
  const amount = proposal.amount;
  const { evidence } = approval;

  return (
    <>
      <p className="subhead">
        <Link href="/">Approvals</Link> / <span className="mono">{approval.ticket_id}</span>
      </p>

      <div className="split">
        <section>
          <p className="headline-amount">{amount ? formatMoney(amount) : 'No amount'}</p>
          <p className="subhead">
            {proposal.kind.replace(/_/g, ' ')} against{' '}
            <span className="mono">{proposal.capture_id ?? '—'}</span>, reason{' '}
            <span className="mono">{proposal.reason_code ?? '—'}</span>
          </p>

          <Why approval={approval} />

          {approval.status === 'pending' ? (
            <DecisionForm approval={approval} />
          ) : (
            <Settled approval={approval} />
          )}
        </section>

        <aside>
          <div className="panel">
            <h2 className="panel__title">Evidence</h2>
            <dl className="kv">
              <dt>Merchant</dt>
              <dd>{approval.merchant_id}</dd>
              <dt>Payment</dt>
              <dd>{evidence.payment_id ?? '—'}</dd>
              <dt>Ticket</dt>
              <dd>{approval.ticket_id}</dd>
              <dt>Run</dt>
              <dd>
                <Link href={`/runs/${approval.run_id}`}>{approval.run_id}</Link>
              </dd>
              <dt>Raised</dt>
              <dd>{formatTimestamp(approval.created_at)}</dd>
              <dt>Expires</dt>
              <dd>{formatTimestamp(approval.expires_at)}</dd>
            </dl>
          </div>

          <Captures evidence={evidence} />

          {evidence.citations.length > 0 ? (
            <div className="panel">
              <h2 className="panel__title">Grounded in</h2>
              <ul className="kv" style={{ display: 'block', paddingLeft: '1.1rem', margin: 0 }}>
                {evidence.citations
                  .filter((c): c is string => Boolean(c))
                  .map((citation) => (
                    <li key={citation} className="mono" style={{ fontSize: 'var(--t-xs)' }}>
                      {citation}
                    </li>
                  ))}
              </ul>
            </div>
          ) : null}
        </aside>
      </div>
    </>
  );
}

/**
 * Why this needs a human, in the policy engine's own words.
 *
 * The rule id is shown rather than hidden behind a tooltip. It is the thing
 * that makes the decision auditable, and a reviewer who cannot see which rule
 * fired is being asked to trust a sentence.
 */
function Why({ approval }: { approval: Awaited<ReturnType<typeof fetchApproval>> }) {
  const refusing =
    approval.decision.permission === 'forbidden' || approval.decision.permission === 'refused';

  return (
    <div className={refusing ? 'why why--stop' : 'why'}>
      <p className="why__rule">
        {approval.risk_tier ?? approval.rule_id}
        {approval.risk_tier && approval.risk_tier !== approval.rule_id
          ? `, decided by ${approval.rule_id}`
          : ''}
      </p>
      <ul>
        {approval.reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
    </div>
  );
}

/**
 * The arithmetic, set as a ledger subtotal.
 *
 * The reviewer is shown the remainder rather than told it. The figure they
 * authorise against is then something they can check, which is the whole
 * difference between an evidence panel and an assertion.
 */
function Captures({ evidence }: { evidence: Evidence }) {
  if (evidence.captures.length === 0) {
    return null;
  }

  return (
    <div className="panel">
      <h2 className="panel__title">
        {evidence.captures.length === 1 ? 'The capture' : 'The captures'}
      </h2>

      {evidence.captures.map((capture) => {
        const remainder = subtract(capture.amount, capture.refunded_amount);
        return (
          <div key={capture.capture_id ?? 'unknown'} style={{ marginBottom: 'var(--gap-4)' }}>
            <p className="mono" style={{ fontSize: 'var(--t-xs)', marginBottom: 'var(--gap-2)' }}>
              {capture.capture_id ?? '—'}
              {capture.captured_at ? `, captured ${capture.captured_at.slice(0, 10)}` : ''}
            </p>
            <dl className="arith">
              <dt>Captured</dt>
              <dd>{capture.amount ? formatMoney(capture.amount) : '—'}</dd>
              <dt>Already refunded</dt>
              <dd>{capture.refunded_amount ? formatMoney(capture.refunded_amount) : '—'}</dd>
              <dt className="arith__total">Unrefunded remainder</dt>
              <dd className="arith__total">{remainder ? formatMoney(remainder) : '—'}</dd>
            </dl>
          </div>
        );
      })}

      {evidence.duplicate_captures.length > 0 ? (
        <p className="hint">
          Duplicate captures under one order reference:{' '}
          <span className="mono">{evidence.duplicate_captures.join(', ')}</span>
        </p>
      ) : null}
    </div>
  );
}

function Settled({ approval }: { approval: Awaited<ReturnType<typeof fetchApproval>> }) {
  return (
    <div style={{ marginTop: 'var(--gap-4)' }}>
      <span className="settled">
        <span className="settled__status">{approval.status}</span>
        <span className="mono" style={{ color: 'var(--ink-soft)' }}>
          {approval.reviewer ?? 'no reviewer'}
          {approval.decided_at ? `, ${formatTimestamp(approval.decided_at)}` : ''}
        </span>
      </span>
      {approval.reviewer_note ? (
        <p className="subhead" style={{ marginTop: 'var(--gap-3)' }}>
          {approval.reviewer_note}
        </p>
      ) : null}
      {approval.modified_proposal?.amount && approval.proposal.amount ? (
        <p className="hint" style={{ marginTop: 'var(--gap-3)' }}>
          The agent proposed {formatMoney(approval.proposal.amount)}; the reviewer allowed{' '}
          {formatMoney(approval.modified_proposal.amount)}.
        </p>
      ) : null}
    </div>
  );
}

/** Integer subtraction, same currency only. Never a division, never a float. */
function subtract(a: Money | null, b: Money | null): Money | null {
  if (!a) {
    return null;
  }
  if (!b) {
    return a;
  }
  if (a.currency !== b.currency) {
    // Mixing currencies is a bug upstream, and inventing a conversion rate
    // to render something is the wrong way to surface it.
    return null;
  }
  return { amount_minor: a.amount_minor - b.amount_minor, currency: a.currency };
}
