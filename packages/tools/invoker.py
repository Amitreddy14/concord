"""The tool invoker.

One path through which every tool call travels, so the guarantees hold for
all of them rather than for the ones someone remembered:

1. **Arguments are validated** against the tool's Pydantic model. A bad
   argument never reaches the network.
2. **Scope is checked against tenant configuration** before anything else
   that costs money or time. Nothing derived from a ticket participates in
   this decision (ADR 0005).
3. **Mutations carry a derived idempotency key**, so a retry after a timeout
   returns the original effect rather than creating a second one.
4. **The circuit breaker is consulted per dependency**, so a dead payments
   service does not stop the agent reading the knowledge base.
5. **Every attempt is written to the ledger** before and after, so a run that
   dies mid-call leaves evidence that the call was started.

The ordering of 1, 2 and 3 is the part worth defending. Validating first means
a scope check never runs on arguments that are not even well-formed. Checking
scope second means an impermissible call costs nothing. Deriving the key third
means it is derived from *validated* arguments, so two spellings of the same
request cannot produce two keys.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import ValidationError

from packages.policy.tenant import TenantConfig
from packages.shared.ledger import InMemoryLedger, StepKind, record_step
from packages.tools import registry
from packages.tools.breaker import BreakerRegistry, CircuitOpen
from packages.tools.errors import (
    ErrorKind,
    ScopeViolation,
    ToolError,
    ToolFailure,
    classify,
)
from packages.tools.keys import derive_idempotency_key
from packages.tools.retry import RetryPolicy, delay_for_attempt

CORRELATION_HEADER = "X-Correlation-Id"
IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotency-Replayed"


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: str
    data: Any
    attempts: int
    replayed: bool
    """True when the system of record returned a previously stored response.

    Surfaced rather than hidden: a node that needs to know whether it caused
    an effect or merely observed one — reconciliation, or an audit reply —
    cannot tell from the body alone.
    """

    status: int = 200


@dataclass
class ToolInvoker:
    """Calls tools on behalf of one run."""

    tenant: TenantConfig
    base_urls: dict[str, str]
    client: httpx.AsyncClient
    ledger: InMemoryLedger = field(default_factory=InMemoryLedger)
    breakers: BreakerRegistry = field(default_factory=BreakerRegistry)
    sleep: Any = asyncio.sleep
    """Injected so tests exercise the retry schedule without waiting it out."""

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        run_id: str,
        node: str,
        correlation_id: str | None = None,
    ) -> ToolResult:
        spec = registry.get(tool_name)
        arguments = arguments or {}

        # 1. Validate. A poisoned argument never reaches the network, and the
        #    failure is classified distinctly because there is nothing
        #    upstream to reconcile or compensate.
        try:
            validated = spec.args_model.model_validate(arguments)
        except ValidationError as error:
            failure = ToolFailure(
                kind=ErrorKind.POISONED_INPUT,
                code="invalid_arguments",
                status=None,
                detail=_first_error(error),
                tool=tool_name,
            )
            self._record_refusal(run_id, node, spec, failure)
            raise ToolError(failure) from error

        payload = validated.to_payload()

        # 2. Scope, before anything costs anything. A ScopeViolation is not a
        #    ToolError: a retry cannot help, and something attempting what the
        #    deployment forbids is a different conversation from a flaky
        #    dependency.
        if not self.tenant.permits_scope(spec.scope):
            violation = ScopeViolation(tool_name, spec.scope, self.tenant.tenant_id)
            self._record_refusal(
                run_id,
                node,
                spec,
                ToolFailure(
                    kind=ErrorKind.FATAL,
                    code="scope_violation",
                    status=None,
                    detail=str(violation),
                    tool=tool_name,
                ),
            )
            raise violation

        # 3. Derive the key from the validated arguments, so two spellings of
        #    one request cannot produce two keys.
        idempotency_key = (
            derive_idempotency_key(run_id=run_id, node=node, tool=tool_name, arguments=payload)
            if spec.mutating
            else None
        )

        return await self._attempt_loop(
            spec,
            payload,
            run_id=run_id,
            node=node,
            correlation_id=correlation_id or run_id,
            idempotency_key=idempotency_key,
        )

    # -- internals -------------------------------------------------------

    async def _attempt_loop(
        self,
        spec: registry.ToolSpec,
        payload: dict[str, Any],
        *,
        run_id: str,
        node: str,
        correlation_id: str,
        idempotency_key: str | None,
    ) -> ToolResult:
        policy = RetryPolicy.for_class(spec.retry_class)
        breaker = self.breakers.for_dependency(spec.service)
        last_failure: ToolFailure | None = None

        for attempt in range(1, policy.max_attempts + 1):
            if attempt > 1:
                delay_ms = delay_for_attempt(
                    policy,
                    attempt,
                    run_id=run_id,
                    node=node,
                    tool=spec.name,
                    retry_after_s=last_failure.retry_after_s if last_failure else None,
                )
                await self.sleep(delay_ms / 1000)

            try:
                breaker.before_call()
            except CircuitOpen as open_circuit:
                failure = ToolFailure(
                    kind=ErrorKind.RETRYABLE,
                    code="circuit_open",
                    status=None,
                    detail=str(open_circuit),
                    tool=spec.name,
                    attempt=attempt,
                )
                self._record_refusal(run_id, node, spec, failure)
                raise ToolError(failure) from open_circuit

            try:
                with record_step(
                    self.ledger,
                    run_id=run_id,
                    node=node,
                    kind=StepKind.TOOL_CALL,
                    tool=spec.name,
                    scope=spec.scope,
                    attempt=attempt,
                    request=payload,
                ) as box:
                    result = await self._send(
                        spec,
                        payload,
                        attempt=attempt,
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key,
                    )
                    box["output"] = result.data
            except ToolError as error:
                last_failure = error.failure
                if last_failure.kind in (ErrorKind.RETRYABLE, ErrorKind.INDETERMINATE):
                    breaker.record_failure()
                else:
                    # A 404 or a 422 is the dependency working correctly and
                    # telling us something true. Counting it toward the
                    # breaker would trip the circuit on our own bad requests.
                    breaker.record_success()
                    raise
                if attempt == policy.max_attempts:
                    raise
                continue
            else:
                breaker.record_success()
                return result

        raise ToolError(last_failure) if last_failure else AssertionError("unreachable")

    async def _send(
        self,
        spec: registry.ToolSpec,
        payload: dict[str, Any],
        *,
        attempt: int,
        correlation_id: str,
        idempotency_key: str | None,
    ) -> ToolResult:
        base_url = self.base_urls.get(spec.service)
        if base_url is None:
            raise KeyError(f"no base URL configured for service {spec.service!r}")

        query, body = spec.split_arguments(payload)
        headers = {CORRELATION_HEADER: correlation_id}
        if idempotency_key is not None:
            headers[IDEMPOTENCY_HEADER] = idempotency_key
        if spec.name == "post_message" and self.tenant.requires_citations:
            headers["X-Require-Citations"] = "true"

        try:
            response = await self.client.request(
                spec.method,
                f"{base_url}{spec.resolve_path(payload)}",
                params=query or None,
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as error:
            raise ToolError(
                classify(
                    tool=spec.name,
                    status=None,
                    problem=None,
                    attempt=attempt,
                    transport_error=f"{type(error).__name__}: {error}",
                )
            ) from error

        if response.status_code >= 400:
            problem = _problem_body(response)
            raise ToolError(
                classify(
                    tool=spec.name, status=response.status_code, problem=problem, attempt=attempt
                )
            )

        return ToolResult(
            tool=spec.name,
            data=response.json() if response.content else None,
            attempts=attempt,
            replayed=response.headers.get(REPLAYED_HEADER) == "true",
            status=response.status_code,
        )

    def _record_refusal(
        self,
        run_id: str,
        node: str,
        spec: registry.ToolSpec,
        failure: ToolFailure,
    ) -> None:
        """Write a refusal that never reached the network.

        Recorded with the scope it *would* have used, so a reviewer can see
        what was attempted — a refused call is evidence, not an absence.
        """
        try:
            with record_step(
                self.ledger,
                run_id=run_id,
                node=node,
                kind=StepKind.TOOL_CALL,
                tool=spec.name,
                scope=None,
                request={"refused": failure.code, "would_require": spec.scope},
            ) as box:
                box["error_code"] = failure.code
                raise ToolError(failure)
        except ToolError:
            pass


def _first_error(error: ValidationError) -> str:
    first = error.errors()[0]
    location = ".".join(str(p) for p in first["loc"]) or "<root>"
    return f"{location}: {first['msg']}"


def _problem_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {"detail": response.text[:300]}
    return body if isinstance(body, dict) else {"detail": str(body)[:300]}