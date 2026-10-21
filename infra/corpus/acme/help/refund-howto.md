---
document_id: refund-howto
doc_type: help_centre
title: Issuing a refund
version: v4
effective_from: 2026-02-01
---

## From the dashboard

Open the payment, select the capture to reverse, and choose Refund. Enter the amount, which may
be the full captured amount or part of it, and a reason. The refund is submitted immediately and
appears against that capture.

## Finding the right capture

Where a payment shows more than one capture, check the gateway reference before refunding. Two
captures sharing a reference indicate a retry rather than two separate charges, and only the
later one should be reversed.

## After submitting

A submitted refund cannot be cancelled. If an incorrect amount was refunded, the resolution is a
fresh charge to the buyer with their agreement, not a reversal of the refund.

## Refund failures

A refund can fail where the original instrument is closed or expired. The platform retries
automatically for three working days. If it still fails the amount returns to the merchant's
balance and the buyer must be paid by another route.
