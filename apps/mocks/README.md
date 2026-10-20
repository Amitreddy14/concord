# mocks

The four systems of record, implemented from `contracts/` with real state: a refund changes the
capture it refunds.

All four share one fault-injection middleware driven by `FAULT_PROFILE` and `FAULT_SEED`, so a
named profile reproduces the same failure sequence on every run. That determinism is what makes
S09 testable in CI rather than anecdotal (ADR 0002).
