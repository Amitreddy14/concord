# api

The intake and run-control API.

Asynchronous by design: `POST /tickets` enqueues a run and returns a run id, because a
resolution on local inference takes tens of seconds (ADR 0003). Progress streams over SSE;
`GET /runs/{id}` returns the current state and the step ledger.

Phase 6 adds the approval endpoints the reviewer console calls.
