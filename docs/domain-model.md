# Domain model

The deployment target is the merchant support desk of a payments platform. The platform's
customers are **merchants**; the merchants' customers are **buyers**. Concord serves the
merchant, and a buyer is only ever a party referenced inside a merchant's ticket.

Everything below is the vocabulary the whole codebase uses. A name that appears here appears
nowhere else in a different sense.

---

## Entities

### Merchant
The platform's customer and the authenticated subject of a ticket.

| Field | Notes |
| --- | --- |
| `merchant_id` | `M-` prefixed, stable |
| `legal_name`, `display_name` | |
| `tier` | `standard` \| `growth` \| `enterprise` — drives SLA and refund autonomy caps |
| `kyc_status` | `verified` \| `pending` \| `under_review` \| `rejected` |
| `payout_hold` | boolean, and a reason code when true |
| `mcc` | merchant category code; some policies are category-scoped |
| `onboarded_at`, `support_contacts[]` | |

### Payment
An attempted movement of money from a buyer to a merchant. A payment is the unit a buyer
recognises; it is not the unit that money moves in.

| Field | Notes |
| --- | --- |
| `payment_id` | `pay_` prefixed |
| `merchant_id`, `order_ref` | `order_ref` is the merchant's own identifier and is **not unique** |
| `amount` | integer minor units + ISO 4217 code |
| `method` | `card` \| `upi` \| `netbanking` \| `wallet` |
| `status` | see state machine below |
| `captures[]`, `refunds[]` | |
| `created_at`, `authorized_at`, `captured_at` | |

### Capture
The actual debit. A payment may have more than one capture, which is how the duplicate-charge
scenario arises: a client retry after a timeout can produce a second capture against one
authorization.

| Field | Notes |
| --- | --- |
| `capture_id` | `cap_` prefixed |
| `payment_id`, `amount`, `captured_at` | |
| `gateway_reference` | the upstream reference; two captures sharing one are a gateway artifact rather than a genuine double charge |

### Refund
A reversal against a specific capture, never against a payment in the abstract. This is the
single most important modelling decision in the system: refunding "the payment" is ambiguous
when there are two captures, and the ambiguity is where double refunds come from.

| Field | Notes |
| --- | --- |
| `refund_id` | `rfnd_` prefixed |
| `capture_id`, `amount` | amount ≤ unrefunded remainder of that capture |
| `status` | `pending` \| `processed` \| `failed` |
| `reason_code` | `duplicate` \| `buyer_request` \| `goods_not_delivered` \| `fraud` \| `goodwill` |
| `idempotency_key` | supplied by the caller; see below |
| `initiated_by` | `agent` \| `human` \| `merchant` |
| `approval_id` | set when a human gate was crossed |

### Settlement
The platform paying out captured funds to the merchant's bank account, on a schedule, net of
fees and refunds.

| Field | Notes |
| --- | --- |
| `settlement_id` | `setl_` prefixed |
| `merchant_id`, `gross_amount`, `fees`, `refunds_deducted`, `net_amount` | |
| `status` | `scheduled` \| `processing` \| `settled` \| `failed` \| `held` |
| `expected_at`, `settled_at`, `utr` | `utr` is the bank reference, present only once settled |
| `hold_reason` | present when `held` |

### Chargeback
A buyer disputing a capture through their issuing bank. It has a hard external deadline, which
is why it is never resolved autonomously.

| Field | Notes |
| --- | --- |
| `chargeback_id` | `cbk_` prefixed |
| `capture_id`, `amount`, `reason_code`, `network` | |
| `status` | `open` \| `evidence_submitted` \| `won` \| `lost` \| `accepted` |
| `evidence_due_at` | external, non-negotiable |

### Ticket
The inbound unit of work.

| Field | Notes |
| --- | --- |
| `ticket_id` | `tkt_` prefixed |
| `merchant_id`, `channel` | `chat` \| `email` \| `portal` |
| `subject`, `body` | **untrusted content** (ADR 0005) |
| `status` | `open` \| `awaiting_customer` \| `awaiting_approval` \| `resolved` \| `escalated` |
| `priority` | derived from merchant tier and intent, never from the merchant's own assertion |
| `sla_due_at`, `messages[]`, `assignee` | |

### Run
One execution of the agent against one ticket. The audit unit.

| Field | Notes |
| --- | --- |
| `run_id`, `ticket_id`, `tenant_id`, `thread_id` | `thread_id` is the LangGraph checkpoint thread |
| `status` | `queued` \| `running` \| `awaiting_approval` \| `completed` \| `failed` \| `abandoned` |
| `intent`, `resolution` | |
| `steps[]` | ordered, append-only |
| `token_usage`, `latency_ms`, `model_id` | the cost and latency ledger |

### RunStep
Append-only. Never updated, never deleted — a correction is a new step.

| Field | Notes |
| --- | --- |
| `step_id`, `run_id`, `seq`, `node` | |
| `kind` | `model_call` \| `tool_call` \| `policy_decision` \| `approval` \| `error` \| `compensation` |
| `input_digest`, `output_digest` | hashes; payloads stored separately so steps stay cheap to scan |
| `started_at`, `ended_at` | |
| `policy_rule_id` | set on `policy_decision`, so a reviewer sees which rule fired |

### Approval
A human gate crossing.

| Field | Notes |
| --- | --- |
| `approval_id`, `run_id`, `proposed_action` | the typed action object, stored verbatim |
| `risk_tier`, `required_scope`, `requested_at`, `expires_at` | |
| `decision` | `approved` \| `rejected` \| `modified` \| `expired` |
| `decided_by`, `decided_at`, `reviewer_note` | |
| `modified_action` | present when `decision = modified` |

---

## State machines

### Payment

```
created ──> authorized ──> captured ──> partially_refunded ──> refunded
   │            │              │
   │            └──> voided    └──> disputed ──> (chargeback states)
   └──> failed
```

`captured` and `partially_refunded` are the only states a refund may be initiated from.

### Refund

```
pending ──> processed
   └──> failed ──> (retryable: new refund with the same idempotency key)
```

A `failed` refund is not retried by mutating it. A retry is a fresh request carrying the same
idempotency key, which is what makes S09 safe.

### Run

```
queued ──> running ──> completed
              │  ├──> awaiting_approval ──> running ──> completed
              │  └──> failed
              └──> abandoned        (approval expired, per ADR 0001's TTL note)
```

---

## Money

Integer minor units plus an ISO 4217 code, always together, never apart.

```python
Money(amount_minor=4120000, currency="INR")   # ₹41,200.00
```

Rules, enforced by lint rather than by discipline:

- No `float` in any expression that touches an amount.
- Arithmetic between two `Money` values requires identical currency codes; mismatch raises.
- Division only via explicit allocation that distributes remainder deterministically, so split
  refunds across captures always sum exactly to the total.
- Display formatting happens at the boundary, never in business logic.

---

## Identity and resolution

The hard part of entity resolution here is that `order_ref` is the merchant's identifier and
carries no uniqueness guarantee. A merchant can legitimately have two payments with
`order_ref = "8842"` months apart.

Resolution order for a ticket mentioning an order:

1. Scope every lookup by the authenticated `merchant_id`. Never resolve across merchants.
2. Match `order_ref` within that merchant.
3. If exactly one payment matches, proceed.
4. If several match, rank by recency and by consistency with other facts in the ticket (amount,
   buyer last four, date). If the top candidate is not unambiguous, **ask** — this is S11, and
   guessing is the failure the scenario exists to catch.
5. If none match, say so rather than inventing a near-match.

---

## Idempotency

Every mutating tool call carries an `Idempotency-Key`. The key is derived deterministically:

```
idempotency_key = hash(run_id, node_name, tool_name, canonical_json(arguments))
```

Because it is derived from the run and the call rather than generated fresh, a replayed graph
step produces the same key, and the systems of record return the original response instead of
performing the action twice. A node that interrupts for approval performs no mutation before the
interrupt (ADR 0001), so re-entry after resume cannot double-write regardless.

---

## Tenancy

A tenant is one deployment's configuration, in `infra/tenants/<tenant>.yaml`: refund autonomy
caps by merchant tier, policy windows, risk tier thresholds, approval expiry, tone and reply
templates, knowledge sources, and enabled tool scopes. Adding a tenant is adding a file
(ADR 0006, principle 5). No tenant-specific branch may exist in `packages/`.
