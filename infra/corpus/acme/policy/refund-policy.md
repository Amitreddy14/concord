---
document_id: refund-policy
doc_type: policy
title: Refund policy
version: v3
effective_from: 2026-01-01
---

## Eligibility window

A refund may be initiated within 180 days of the original capture. Beyond that period the
transaction can no longer be reversed through the platform, and the merchant must settle with
the buyer directly or raise a case with their account manager.

The window runs from the capture date, not from the authorization date or the order date. Where
a payment has several captures, each capture carries its own window.

## Credit timelines

Once a refund is processed, the funds are returned to the original payment instrument. Card
refunds reach the cardholder's statement in 5 to 7 working days. UPI and wallet refunds are
typically credited within 3 working days. Netbanking refunds take 5 to 7 working days.

The platform has no control over the issuing bank's posting schedule. A refund shown as
processed on the dashboard has left the platform, and any further delay is at the bank.

## Partial refunds

A capture may be refunded in part, and more than once, up to the total captured amount. Each
refund is a separate record against that capture. The unrefunded remainder shown on a capture is
authoritative; a merchant calculating it independently may be working from a stale figure.

## Duplicate charges

Where a single authorization produced more than one capture, the later capture is the duplicate
and is the one to reverse. Captures created by a client retry share a gateway reference, which
distinguishes them from two deliberate charges that happen to match in amount.

## What a refund does not do

A refund does not withdraw a chargeback that has already been raised, and does not prevent one.
A buyer who has disputed a payment with their bank must withdraw that dispute themselves; the
two processes run independently and a merchant who refunds a disputed capture may lose both
amounts.
