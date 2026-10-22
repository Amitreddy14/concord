import { describe, expect, it } from 'vitest';

import { runTraceSchema } from '../types';

/**
 * The trace's shape, against what the ledger actually emits.
 *
 * `started` is the one that matters. The tool layer records a call twice —
 * once on the way out, once on the way back — so a viewer that treats
 * anything other than `success` as a failure paints half of a healthy trace
 * red. The outcomes are started, success, error and refused.
 */
describe('runTraceSchema', () => {
  it('parses a trace carrying both halves of a tool call', () => {
    const trace = runTraceSchema.parse({
      run_id: 'run_000001',
      steps: [
        {
          seq: 1,
          node: 'gather',
          kind: 'tool_call',
          outcome: 'started',
          tool: 'get_merchant',
          scope: 'crm:merchant:read',
          attempt: 1,
          duration_ms: null,
          error_code: null,
        },
        {
          seq: 2,
          node: 'gather',
          kind: 'tool_call',
          outcome: 'success',
          tool: 'get_merchant',
          scope: 'crm:merchant:read',
          attempt: 1,
          duration_ms: 6,
          error_code: null,
        },
      ],
      scopes_used: ['crm:merchant:read'],
    });

    expect(trace.steps).toHaveLength(2);
    expect(trace.steps[0]?.outcome).toBe('started');
    expect(trace.steps[0]?.duration_ms).toBeNull();
  });

  it('accepts a refused step with its error code', () => {
    const trace = runTraceSchema.parse({
      run_id: 'run_000002',
      steps: [
        {
          seq: 9,
          node: 'act',
          kind: 'tool_call',
          outcome: 'refused',
          tool: 'create_refund',
          scope: 'payments:refund:write',
          attempt: 1,
          duration_ms: 2,
          error_code: 'scope_violation',
        },
      ],
      scopes_used: [],
    });

    expect(trace.steps[0]?.error_code).toBe('scope_violation');
  });
});
