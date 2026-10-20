# shared

Domain types, money handling, the error taxonomy, the LLM gateway interface and telemetry.

`money.py` is the one place amounts are represented: integer minor units plus an ISO 4217 code.
Arithmetic requires matching currencies, division goes through explicit allocation that
distributes remainder deterministically, and `float` is prohibited in any path touching an
amount — enforced by a lint rule.

`doctor.py` backs `make doctor`: it verifies the configured model is resident, asserts it
supports tool calling (a model without it fails silently rather than erroring), and reports
measured decode throughput as the eval harness's latency baseline.
