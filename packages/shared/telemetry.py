"""Traces and structured logs, with one rule that shapes everything else.

**A span attribute is an egress channel.** Whatever goes into one leaves the
process, reaches a collector, and is indexed and searchable by anyone with
access to the backend — which is a wider audience than the people allowed to
read a merchant's ticket. ADR 0005 keeps untrusted text out of prompts and out
of the reviewer console for the same reason it has to stay out of here: a
ticket body in a span is the same leak, with a longer life and no reviewer.

So attributes carry ids, classifications, counts and durations. Never the
ticket subject or body, never a drafted reply, never a merchant's name. The
helpers below take explicit keyword arguments rather than a dict precisely so
adding a field is a decision somebody makes rather than a `**state` that
quietly grows.

The second rule is that telemetry never breaks a run. A collector that is
down, misconfigured or absent must cost nothing: the tracer falls back to a
no-op, every span is best-effort, and an exporter failure is logged once
rather than raised. A refund that failed because Jaeger was unreachable would
be an own goal of the first order.

Three levels of span, matching the three questions an engineer asks about a
run that went wrong: what did the run do (`run`), which node was it in
(`node`), and what did it ask a dependency (`tool`). The tool spans already
have their facts in the ledger; the spans add the timing relationships the
ledger's flat sequence cannot show.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

LOGGER = logging.getLogger("concord")


#: Whether `configure` has run. A mutable holder rather than a rebound
#: module global, so no function needs `global` — and so a test can reset it
#: by assigning one field instead of reaching for the module's namespace.
class _State:
    configured: bool = False


_STATE = _State()

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "concord-api")


def configure(*, endpoint: str | None = None, service: str | None = None) -> bool:
    """Set up tracing and structured logging. Returns whether tracing is live.

    Absent configuration is a supported state, not a degraded one: a developer
    running the API with no collector gets the no-op tracer and no warnings
    beyond one line, because an install that nags about optional
    infrastructure teaches people to ignore its output.
    """
    if _STATE.configured:
        return trace.get_tracer_provider().__class__.__name__ != "NoOpTracerProvider"

    _configure_logging()
    _STATE.configured = True

    target = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not target:
        LOGGER.info("tracing disabled: no OTEL_EXPORTER_OTLP_ENDPOINT configured")
        return False

    try:
        # Imported here rather than at module scope so the no-collector path
        # carries no SDK import cost and no grpc dependency at runtime.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        provider = TracerProvider(
            resource=Resource.create({"service.name": service or SERVICE_NAME})
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=target, insecure=True))
        )
        trace.set_tracer_provider(provider)
    except Exception as error:
        # Tracing is the one subsystem whose failure must never reach a
        # caller. A refund that did not happen because a collector was
        # unreachable would be an own goal of the first order.
        LOGGER.warning("tracing unavailable, continuing without it: %s", error)
        return False

    LOGGER.info("tracing enabled, exporting to %s", target)
    return True


def _configure_logging() -> None:
    """Structured logs, as one line of JSON per record.

    JSON because these are read by a log aggregator far more often than by a
    person, and a person reading them has `jq`. The correlation id is on every
    record that has one, which is what makes a run's logs findable after the
    fact.
    """
    if LOGGER.handlers:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _JsonFormatter() if os.environ.get("LOG_FORMAT", "json") == "json" else logging.Formatter()
    )
    LOGGER.addHandler(handler)
    LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    LOGGER.propagate = False


class _JsonFormatter(logging.Formatter):
    #: Fields `logging` puts on every record. Anything outside this set was
    #: passed by a caller as `extra` and belongs in the output.
    _BUILTIN = frozenset(
        {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "message",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "stacklevel",
            "thread",
            "threadName",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        import json  # noqa: PLC0415

        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in self._BUILTIN})

        # The current trace, so a log line and a span can be joined without
        # the caller having to pass ids around.
        span = trace.get_current_span()
        context = span.get_span_context()
        if context.is_valid:
            payload["trace_id"] = format(context.trace_id, "032x")
            payload["span_id"] = format(context.span_id, "016x")

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def tracer() -> trace.Tracer:
    return trace.get_tracer("concord")


# ---------------------------------------------------------------------------
# The three levels of span
# ---------------------------------------------------------------------------


@contextmanager
def run_span(
    *,
    run_id: str,
    ticket_id: str,
    tenant_id: str,
    merchant_id: str,
    intent: str | None = None,
) -> Iterator[Span]:
    """One span per run, the root everything else hangs from.

    Ids only. A merchant id identifies an account to someone who already has
    access to it; a merchant *name* in a trace backend is customer data in a
    place nobody audited for it.
    """
    with tracer().start_as_current_span("concord.run") as span:
        _set(
            span,
            **{
                "concord.run_id": run_id,
                "concord.ticket_id": ticket_id,
                "concord.tenant": tenant_id,
                "concord.merchant_id": merchant_id,
                "concord.intent": intent,
            },
        )
        yield span


@contextmanager
def node_span(name: str, *, run_id: str) -> Iterator[Span]:
    """One span per graph node, so the path a run took is visible as a shape.

    The ledger records which nodes ran; the span records how long each took
    and what nested inside it, which is the part a flat sequence cannot show.
    """
    with tracer().start_as_current_span(f"node.{name}") as span:
        _set(span, **{"concord.run_id": run_id, "concord.node": name})
        yield span


@contextmanager
def tool_span(
    tool: str,
    *,
    run_id: str,
    node: str,
    service: str,
    scope: str,
    attempt: int = 1,
    idempotent: bool = False,
) -> Iterator[Span]:
    """One span per tool call, including each retry.

    Each attempt gets its own span rather than one span with a count, because
    the question worth answering is "how long did the attempt that failed
    take" — and a single span with `attempts=3` hides exactly that.

    The scope is recorded because an audit asking what authority a run used
    should not have to join two systems to find out.
    """
    with tracer().start_as_current_span(f"tool.{tool}") as span:
        _set(
            span,
            **{
                "concord.run_id": run_id,
                "concord.node": node,
                "concord.tool": tool,
                "concord.service": service,
                "concord.scope": scope,
                "concord.attempt": attempt,
                "concord.idempotent": idempotent,
            },
        )
        yield span


def record_failure(span: Span, *, kind: str, code: str, detail: str | None = None) -> None:
    """Mark a span failed, with the classification rather than a stack trace.

    `detail` is the error's own message, which comes from a service's problem
    document and not from a merchant — but it is still truncated, because an
    unbounded string on a span is a way for one bad response to become a very
    expensive trace.
    """
    span.set_status(Status(StatusCode.ERROR, code))
    _set(
        span,
        **{
            "concord.error.kind": kind,
            "concord.error.code": code,
            "concord.error.detail": (detail or "")[:200] or None,
        },
    )


def _set(span: Span, **attributes: Any) -> None:
    """Set attributes, skipping the ones that are absent.

    A span full of `None` is harder to read than a span without the keys, and
    some backends index the literal string "None".
    """
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)
