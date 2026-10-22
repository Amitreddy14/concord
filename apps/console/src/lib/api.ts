/**
 * The API client.
 *
 * One place that knows the base URL, one place that validates responses, and
 * one error type. Every response is parsed through a zod schema rather than
 * cast, so a field the API renamed surfaces here as a parse failure instead
 * of as `undefined` rendered into a reviewer's evidence panel.
 *
 * Errors are RFC 9457 problem details, and the status code carries meaning
 * the console acts on:
 *
 * - **404** the approval is gone; the queue is stale, reload it.
 * - **409** someone else decided it, or the sweep expired it. Reload and show
 *   who. Never retry — a retry is asking the same question again after the
 *   answer arrived.
 * - **400** the submission was malformed. Retrying it unchanged repeats the
 *   mistake, so the console shows `detail` and leaves the form populated.
 */

import { z } from 'zod';

import {
  type Approval,
  approvalSchema,
  type DecisionPayload,
  problemSchema,
  type Queue,
  queueSchema,
  type RunTrace,
  runTraceSchema,
} from './types';

/** What the decision endpoint returns: the recorded row, plus the resume. */
const decisionResultSchema = approvalSchema.extend({
  resumed: z.boolean(),
  detail: z.string().optional(),
});

export type DecisionResult = z.infer<typeof decisionResultSchema>;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | undefined;

  constructor(status: number, detail: string, code?: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }

  /** Someone else got there first: reload rather than retry. */
  get isConflict(): boolean {
    return this.status === 409;
  }

  get isGone(): boolean {
    return this.status === 404;
  }
}

function baseUrl(): string {
  return process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000';
}

async function request<T>(
  path: string,
  schema: { parse: (value: unknown) => T },
  init?: RequestInit,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl()}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...init?.headers },
      // A queue or a trace served from cache is a reviewer deciding against
      // a world that has moved.
      cache: 'no-store',
    });
  } catch {
    // A network failure is not a problem document, and saying "the API is
    // unreachable" is more use than surfacing a TypeError from fetch.
    throw new ApiError(0, `Could not reach the API at ${baseUrl()}.`, 'unreachable');
  }

  if (!response.ok) {
    const problem = problemSchema.safeParse(await response.json().catch(() => ({})));
    const detail = problem.success
      ? (problem.data.detail ?? problem.data.title ?? response.statusText)
      : response.statusText;
    throw new ApiError(response.status, detail, problem.success ? problem.data.code : undefined);
  }

  return schema.parse(await response.json());
}

export function fetchQueue(): Promise<Queue> {
  return request('/approvals', queueSchema);
}

export function fetchApproval(approvalId: string): Promise<Approval> {
  return request(`/approvals/${encodeURIComponent(approvalId)}`, approvalSchema);
}

export function fetchRunTrace(runId: string): Promise<RunTrace> {
  return request(`/runs/${encodeURIComponent(runId)}/steps`, runTraceSchema);
}

/**
 * Submit a reviewer's decision.
 *
 * The response carries the recorded approval plus whether a run was resumed.
 * `resumed: false` is not a failure — the decision is recorded either way,
 * and it means there was no live run in the API process to continue. The
 * console says so rather than implying the refund is on its way.
 */
export function submitDecision(
  approvalId: string,
  payload: DecisionPayload,
): Promise<DecisionResult> {
  return request(`/approvals/${encodeURIComponent(approvalId)}/decision`, decisionResultSchema, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}
