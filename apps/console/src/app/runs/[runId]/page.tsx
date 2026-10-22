import Link from 'next/link';

import { ApiError, fetchRunTrace } from '@/lib/api';
import type { RunStep } from '@/lib/types';

/*
 * The run trace: what the agent actually did.
 *
 * Read from the ledger the tool layer writes, not from a summary. Every tool
 * call appears with the scope it used, the attempt number and the outcome, so
 * a reviewer asking "did it retry?" or "what did it have permission to do?"
 * reads the answer instead of inferring it.
 */
export default async function RunPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;

  try {
    const trace = await fetchRunTrace(runId);
    return (
      <>
        <p className="subhead">
          <Link href="/">Approvals</Link> / run <span className="mono">{trace.run_id}</span>
        </p>
        <h1 className="lede">
          {trace.steps.length} {trace.steps.length === 1 ? 'step' : 'steps'}
        </h1>

        {trace.steps.length === 0 ? (
          <p className="subhead">This run recorded no steps.</p>
        ) : (
          <Trace steps={trace.steps} />
        )}

        {trace.scopes_used.length > 0 ? (
          <p className="scopes">
            Scopes used:{' '}
            {trace.scopes_used.map((scope, index) => (
              <span key={scope}>
                {index > 0 ? ', ' : ''}
                <code>{scope}</code>
              </span>
            ))}
          </p>
        ) : null}
      </>
    );
  } catch (error) {
    const detail = error instanceof ApiError ? error.message : 'The run trace could not be loaded.';
    return (
      <div className="empty">
        <h2>Can’t load this run</h2>
        <p>{detail}</p>
      </div>
    );
  }
}

function Trace({ steps }: { steps: readonly RunStep[] }) {
  return (
    <table className="trace">
      <thead>
        <tr>
          <th className="num">#</th>
          <th>Node</th>
          <th>Kind</th>
          <th>Tool or rule</th>
          <th>Scope</th>
          <th className="num">Try</th>
          <th className="num">ms</th>
          <th>Outcome</th>
        </tr>
      </thead>
      <tbody>
        {steps.map((step) => (
          <tr key={step.seq} className={isFailure(step) ? 'failed' : undefined}>
            <td className="num">{step.seq}</td>
            <td className="id">{step.node}</td>
            <td>{step.kind}</td>
            <td className="id">{step.tool ?? step.policy_rule_id ?? '—'}</td>
            <td className="id">{step.scope ?? '—'}</td>
            <td className="num">{step.attempt ?? '—'}</td>
            <td className="num">{step.duration_ms ?? '—'}</td>
            <td className="outcome">
              {step.outcome}
              {step.error_code ? ` ${step.error_code}` : ''}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Outcomes that went wrong, listed rather than inferred.
 *
 * The ledger has four: started, success, error, refused. Treating "not
 * success" as failure marked every `started` row red — and a tool call is
 * recorded twice, once on the way out and once on the way back, so half the
 * trace lit up as broken.
 */
const FAILED_OUTCOMES = new Set(['error', 'refused']);

function isFailure(step: RunStep): boolean {
  return FAILED_OUTCOMES.has(step.outcome);
}
