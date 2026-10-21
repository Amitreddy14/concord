---
document_id: settlement-policy
doc_type: policy
title: Settlement and payout policy
version: v2
effective_from: 2025-11-01
---

## Settlement schedule

Captured funds are settled to the merchant's registered bank account on the schedule agreed at
onboarding, net of platform fees and of refunds processed in that period. Standard-tier merchants
settle on a T+3 cycle, growth-tier on T+2, and enterprise-tier on T+1.

A settlement moves through scheduled, processing and settled. A settlement in processing has
been instructed to the bank and is no longer under platform control.

## Bank reference

Once a settlement completes, a UTR is recorded against it. A merchant reconciling a deposit
should match on the UTR rather than on the amount, because several settlements may carry the
same net value.

## Delays

A settlement that has passed its expected date and remains in processing is usually waiting on
the partner bank. Where this affects more than one merchant it is recorded as a platform
incident, and the incident notice states what is known.

Where no incident is recorded and a settlement remains in processing more than one working day
past its expected date, the merchant should be escalated to the settlements team rather than
told to wait.

## Holds

A settlement may be held rather than processed. A hold is recorded with a reason category, and
the merchant is told the category. Holds arising from verification are resolved by completing
verification; holds arising from risk review are resolved by the risk team and have no merchant
action.
