import Link from 'next/link';

import { ApiError, fetchQueue } from '@/lib/api';
import { formatMoney } from '@/lib/money';
import { formatDuration, urgencyOf } from '@/lib/time';
import type { QueueRow } from '@/lib/types';

/*
 * The queue is the hero of this console. Not a row of metrics about the
 * queue — a reviewer opening this wants the work, and a count of pending
 * approvals is something they can see by looking at the list.
 *
 * Rows are ledger lines on hairlines rather than cards. Time remaining is
 * the leftmost column because it is the triage key: a reviewer works this
 * list by what is about to expire, not by who it belongs to.
 */
export default async function QueuePage() {
  try {
    const queue = await fetchQueue();
    return queue.count === 0 ? <Empty /> : <Queue rows={queue.approvals} />;
  } catch (error) {
    return <Unreachable error={error} />;
  }
}

function Queue({ rows }: { rows: readonly QueueRow[] }) {
  return (
    <>
      <h1 className="lede">
        {rows.length === 1
          ? '1 refund is waiting on a human'
          : `${rows.length} refunds are waiting on a human`}
      </h1>
      <p className="subhead">
        Oldest first. Each one names the rule that sent it here and expires without a decision.
      </p>

      <div className="ledger">
        {rows.map((row) => (
          <QueueLine key={row.approval_id} row={row} />
        ))}
      </div>
    </>
  );
}

function QueueLine({ row }: { row: QueueRow }) {
  const urgency = urgencyOf(row.expires_in_seconds);
  const amount = row.proposed_action.amount;
  const [firstReason] = row.reasons;

  return (
    <Link href={`/approvals/${row.approval_id}`} className={`row row--${urgency}`}>
      <span className="row__clock">{formatDuration(row.expires_in_seconds)}</span>

      <span className="row__who">
        <span className="row__ids">
          {row.merchant_id}
          <span className="row__ticket">{row.ticket_id}</span>
        </span>
        <span className="row__reason">{firstReason ?? 'Above the agent’s autonomy'}</span>
      </span>

      <span className="row__tier">{row.risk_tier ?? row.rule_id}</span>

      <span className="amount">{amount ? formatMoney(amount) : '—'}</span>
    </Link>
  );
}

function Empty() {
  return (
    <div className="empty">
      <h2>Nothing is waiting on a human</h2>
      <p>
        Refunds within a merchant’s tier cap go through without review. This queue fills when the
        policy engine finds one it cannot authorise alone.
      </p>
    </div>
  );
}

function Unreachable({ error }: { error: unknown }) {
  const detail =
    error instanceof ApiError ? error.message : 'The approval queue could not be loaded.';
  return (
    <div className="empty">
      <h2>Can’t load the queue</h2>
      <p>{detail}</p>
      <p className="hint">
        Start the API with <code>make api</code>, then reload.
      </p>
    </div>
  );
}
