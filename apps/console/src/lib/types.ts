/**
 * The API's shapes, validated at the boundary.
 *
 * Zod rather than hand-written interfaces for the same reason the Python side
 * uses Pydantic v2 for every boundary type: a type assertion is a promise,
 * and a promise about a payload that arrived over a network is a promise
 * nobody checked. `parse` here is the console's equivalent of
 * `model_validate` there.
 *
 * The schemas are deliberately a little loose where the API is open-ended —
 * `inputs` on a decision is a map whose keys depend on which rule fired, and
 * pinning it would mean editing the console every time the policy engine
 * records one more fact. They are strict about everything a decision is
 * rendered from.
 */

import { z } from 'zod';

export const moneySchema = z.object({
  amount_minor: z.number().int(),
  currency: z.string().min(3).max(3),
});

export const permissionSchema = z.enum(['permitted', 'needs_approval', 'forbidden', 'refused']);

export const approvalStatusSchema = z.enum([
  'pending',
  'approved',
  'rejected',
  'modified',
  'expired',
]);

export const proposedActionSchema = z.object({
  kind: z.string(),
  capture_id: z.string().nullable(),
  amount: moneySchema.nullable(),
  reason_code: z.string().nullable(),
  captures_touched: z.array(z.string()),
  touches: z.array(z.string()),
});

export const decisionSchema = z.object({
  permission: permissionSchema,
  rule_id: z.string(),
  risk_tier: z.string().nullable(),
  required_scope: z.string().nullable(),
  reasons: z.array(z.string()),
  inputs: z.record(z.string(), z.unknown()),
  citations: z.array(z.string()),
  expires_at: z.string().nullable(),
});

const captureSchema = z.object({
  capture_id: z.string().nullable(),
  amount: moneySchema.nullable(),
  refunded_amount: moneySchema.nullable(),
  captured_at: z.string().nullable(),
});

/**
 * The evidence panel's contents.
 *
 * Ledger facts and passage ids only. The ticket body is absent by design and
 * not by omission: ADR 0005 rule 4 holds that the panel which authorises
 * money carries no merchant-supplied text, because an injected instruction
 * that reaches a reviewer has found a human to carry it. If this schema ever
 * grows a `body` or `subject` field, that is the bug.
 */
export const evidenceSchema = z.object({
  payment_id: z.string().nullable().optional(),
  captures: z.array(captureSchema).default([]),
  duplicate_captures: z.array(z.string()).default([]),
  resolution: z.record(z.string(), z.unknown()).default({}),
  citations: z.array(z.string().nullable()).default([]),
});

export const approvalSchema = z.object({
  approval_id: z.string(),
  run_id: z.string(),
  ticket_id: z.string(),
  thread_id: z.string(),
  tenant_id: z.string(),
  merchant_id: z.string(),
  fingerprint: z.string(),
  status: approvalStatusSchema,
  created_at: z.string(),
  expires_at: z.string(),
  decided_at: z.string().nullable(),
  reviewer: z.string().nullable(),
  reviewer_note: z.string().nullable(),
  decision: decisionSchema,
  proposal: proposedActionSchema,
  evidence: evidenceSchema,
  modified_proposal: proposedActionSchema.nullable(),

  // Lifted to the top level by `to_review_payload` so a queue row does not
  // hide the risk tier one level down.
  proposed_action: proposedActionSchema,
  risk_tier: z.string().nullable(),
  rule_id: z.string(),
  required_scope: z.string().nullable(),
  reasons: z.array(z.string()),
});

export const queueRowSchema = approvalSchema.extend({
  expires_in_seconds: z.number().int().nonnegative(),
});

export const queueSchema = z.object({
  count: z.number().int().nonnegative(),
  approvals: z.array(queueRowSchema),
});

export const runStepSchema = z.object({
  seq: z.number().int(),
  node: z.string(),
  kind: z.string(),
  outcome: z.string(),
  tool: z.string().nullable(),
  scope: z.string().nullable(),
  policy_rule_id: z.string().nullable().default(null),
  attempt: z.number().int().nullable(),
  duration_ms: z.number().int().nullable(),
  error_code: z.string().nullable(),
});

export const runTraceSchema = z.object({
  run_id: z.string(),
  steps: z.array(runStepSchema),
  scopes_used: z.array(z.string()),
});

/**
 * The body the decision endpoint accepts. Mirrors `ReviewerDecision` in
 * `apps/api/approvals.py`, including that `amount_minor` is optional and
 * permitted only on `modify` — the server refuses it elsewhere, because
 * "approve with an amount" would be an undocumented second way to modify
 * that skips the note a modification requires.
 */
export const decisionPayloadSchema = z.object({
  action: z.enum(['approve', 'reject', 'modify']),
  reviewer: z.string().min(1).max(200),
  note: z.string().max(2000).optional(),
  amount_minor: z.number().int().positive().optional(),
});

/** RFC 9457 problem details — the single error shape this API speaks. */
export const problemSchema = z.object({
  type: z.string().optional(),
  title: z.string().optional(),
  status: z.number().int().optional(),
  detail: z.string().optional(),
  code: z.string().optional(),
});

export type Money = z.infer<typeof moneySchema>;
export type Permission = z.infer<typeof permissionSchema>;
export type ApprovalStatus = z.infer<typeof approvalStatusSchema>;
export type ProposedAction = z.infer<typeof proposedActionSchema>;
export type Decision = z.infer<typeof decisionSchema>;
export type Evidence = z.infer<typeof evidenceSchema>;
export type Approval = z.infer<typeof approvalSchema>;
export type QueueRow = z.infer<typeof queueRowSchema>;
export type Queue = z.infer<typeof queueSchema>;
export type RunStep = z.infer<typeof runStepSchema>;
export type RunTrace = z.infer<typeof runTraceSchema>;
export type Problem = z.infer<typeof problemSchema>;
export type DecisionPayload = z.infer<typeof decisionPayloadSchema>;
