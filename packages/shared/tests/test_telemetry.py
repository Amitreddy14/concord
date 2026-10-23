"""Telemetry, and the one property it must never violate.

A span attribute leaves the process, reaches a collector, and is indexed and
searchable by anyone with access to the backend — a wider audience than the
people allowed to read a merchant's ticket. ADR 0005 keeps untrusted text out
of prompts and out of the reviewer console; the same text in a span is the same
leak with a longer life and no reviewer.

So the test that matters is the one that runs a scenario with a hostile ticket
body and asserts that body appears in no span, on no attribute, anywhere.
"""

from __future__ import annotations

import json
import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from packages.shared import telemetry

#: OpenTelemetry permits setting the global provider exactly once per
#: process, and warns rather than raising on a second attempt — so a
#: per-test provider silently left every test after the first writing to the
#: first test's exporter. One provider for the session, cleared between
#: tests, which is what the library's own contract allows.
_EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="session", autouse=True)
def _provider() -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    trace.set_tracer_provider(provider)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    """A real tracer writing to memory, so attributes can be inspected."""
    _EXPORTER.clear()
    return _EXPORTER


class TestSpansCarryNoUntrustedText:
    def test_a_run_span_carries_ids_and_not_names(self, exporter: InMemorySpanExporter) -> None:
        with telemetry.run_span(
            run_id="run_1",
            ticket_id="tkt_1",
            tenant_id="acme-payments",
            merchant_id="M-1012",
            intent="refund_request",
        ):
            pass

        [span] = exporter.get_finished_spans()
        attributes = dict(span.attributes or {})
        assert attributes["concord.merchant_id"] == "M-1012"
        assert attributes["concord.intent"] == "refund_request"

        # The helper takes explicit keywords, so there is no path by which a
        # merchant's display name or a ticket body could arrive here. The
        # assertion is on the shape: every value is an id or a label.
        for key, value in attributes.items():
            assert isinstance(value, (str, int, bool)), key
            if isinstance(value, str):
                assert len(value) < 100, f"{key} is long enough to be free text"

    def test_the_helpers_accept_no_free_text_channel(self) -> None:
        # A `**state` or a `details: dict` parameter would be the way a
        # ticket body eventually ends up in a trace. There isn't one, and
        # this test is what keeps it that way.
        import inspect

        for helper in (telemetry.run_span, telemetry.node_span, telemetry.tool_span):
            signature = inspect.signature(helper)
            kinds = {p.kind for p in signature.parameters.values()}
            assert inspect.Parameter.VAR_KEYWORD not in kinds, (
                f"{helper.__name__} accepts **kwargs, which is a channel for "
                "arbitrary content to reach a span"
            )

    def test_absent_attributes_are_omitted_rather_than_stringified(
        self, exporter: InMemorySpanExporter
    ) -> None:
        # Some backends index the literal string "None", which is worse than
        # a missing key in every way.
        with telemetry.run_span(
            run_id="run_2", ticket_id="tkt_2", tenant_id="acme", merchant_id="M-1", intent=None
        ):
            pass
        [span] = exporter.get_finished_spans()
        assert "concord.intent" not in dict(span.attributes or {})


class TestSpanShape:
    def test_a_tool_span_records_the_scope_it_used(self, exporter: InMemorySpanExporter) -> None:
        # An audit asking what authority a run used should not have to join
        # two systems to find out.
        with telemetry.tool_span(
            "create_refund",
            run_id="run_3",
            node="act",
            service="payments",
            scope="payments:refund:write",
            attempt=2,
            idempotent=True,
        ):
            pass
        [span] = exporter.get_finished_spans()
        attributes = dict(span.attributes or {})
        assert span.name == "tool.create_refund"
        assert attributes["concord.scope"] == "payments:refund:write"
        assert attributes["concord.attempt"] == 2
        assert attributes["concord.idempotent"] is True

    def test_a_failure_records_the_classification_not_a_trace(
        self, exporter: InMemorySpanExporter
    ) -> None:
        with telemetry.tool_span(
            "create_refund",
            run_id="run_4",
            node="act",
            service="payments",
            scope="payments:refund:write",
        ) as span:
            telemetry.record_failure(
                span, kind="indeterminate", code="upstream_timeout", detail="x" * 500
            )

        [finished] = exporter.get_finished_spans()
        attributes = dict(finished.attributes or {})
        assert attributes["concord.error.kind"] == "indeterminate"
        assert attributes["concord.error.code"] == "upstream_timeout"
        # Truncated: one unbounded error string is how a bad response becomes
        # a very expensive trace.
        assert len(str(attributes["concord.error.detail"])) <= 200


class TestItNeverBreaksARun:
    def test_an_unreachable_collector_is_not_an_error(self) -> None:
        # The property the whole module is written around. A refund that did
        # not happen because a collector was down would be an own goal.
        telemetry._STATE.configured = False
        try:
            live = telemetry.configure(endpoint="http://127.0.0.1:1/nothing-here")
            # Either it set up an exporter that will fail quietly in the
            # background, or it declined and said so. Neither raises.
            assert isinstance(live, bool)
            with telemetry.node_span("triage", run_id="run_5"):
                pass
        finally:
            telemetry._STATE.configured = False

    def test_no_endpoint_is_a_supported_state(self) -> None:
        telemetry._STATE.configured = False
        try:
            assert telemetry.configure(endpoint=None) is False
        finally:
            telemetry._STATE.configured = False


class TestStructuredLogs:
    def test_a_record_is_one_line_of_json_carrying_its_extras(self) -> None:
        formatter = telemetry._JsonFormatter()
        record = logging.LogRecord(
            name="concord",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="refund issued",
            args=(),
            exc_info=None,
        )
        record.run_id = "run_6"  # type: ignore[attr-defined]
        record.refund_id = "rfnd_1"  # type: ignore[attr-defined]

        payload = json.loads(formatter.format(record))
        assert payload["message"] == "refund issued"
        assert payload["run_id"] == "run_6"
        assert payload["refund_id"] == "rfnd_1"
        # The noise `logging` puts on every record stays out.
        assert "msecs" not in payload
        assert "relativeCreated" not in payload


class TestARealRunLeaksNothing:
    """The assertion the module exists for, against a real graph run.

    S10's ticket body imitates system instructions. It must reach no span
    attribute anywhere — not on the run, not on a node, not on a tool call.
    The shape tests above check the helpers; this checks the whole path,
    because the helpers being safe does not prove nobody bypassed them.
    """

    async def test_s10s_injected_body_appears_in_no_span(
        self, exporter: InMemorySpanExporter
    ) -> None:
        import httpx
        from fastapi.testclient import TestClient
        from langgraph.checkpoint.memory import MemorySaver

        from apps.mocks.faults import FaultConfig, FaultProfile
        from apps.mocks.serve import build
        from evals.fixtures import load_all, reset_payload
        from evals.harness.gateway import HarnessGateway
        from packages.agent.context import NodeContext
        from packages.agent.graph import build_graph, initial_state
        from packages.policy.approval import InMemoryApprovalStore
        from packages.policy.tenant import load_tenant
        from packages.shared.ledger import InMemoryLedger
        from packages.tools.invoker import ToolInvoker
        from packages.tools.tests.conftest import BASE_URLS, RoutingTransport

        scenario = load_all()["S10"]
        body = str(scenario.ticket["body"])
        assert "ignore" in body.lower() or "instruction" in body.lower(), (
            "S10's body no longer looks like an injection attempt, so this test "
            "would pass for the wrong reason"
        )

        services = {n: build(n, FaultConfig(profile=FaultProfile.OFF)) for n in BASE_URLS}
        payload = reset_payload(scenario)
        for app in services.values():
            with TestClient(app) as client:
                assert client.post("/_test/reset", json=payload).status_code == 200
        with TestClient(services["ticketing"]) as client:
            ticket_id = client.post(
                "/tickets",
                json={
                    "merchant_id": scenario.ticket["merchant_id"],
                    "channel": "chat",
                    "subject": scenario.ticket["subject"],
                    "body": body,
                },
                headers={"Idempotency-Key": "intake:S10:telemetry"},
            ).json()["ticket_id"]

        hosts = {
            httpx.URL(u).host: a
            for u, a in zip(BASE_URLS.values(), services.values(), strict=False)
        }
        load_tenant.cache_clear()
        tenant = load_tenant("acme-payments")
        exporter.clear()

        async with httpx.AsyncClient(transport=RoutingTransport(hosts)) as client:
            context = NodeContext(
                gateway=HarnessGateway("S10"),  # type: ignore[arg-type]
                invoker=ToolInvoker(
                    tenant=tenant,
                    base_urls=dict(BASE_URLS),
                    client=client,
                    ledger=InMemoryLedger(),
                ),
                tenant_id="acme-payments",
                approvals=InMemoryApprovalStore(),
            )
            graph = build_graph(context, MemorySaver())
            with telemetry.run_span(
                run_id="run_s10_tel",
                ticket_id=ticket_id,
                tenant_id="acme-payments",
                merchant_id=str(scenario.ticket["merchant_id"]),
            ):
                await graph.ainvoke(
                    initial_state(
                        run_id="run_s10_tel",
                        ticket_id=ticket_id,
                        tenant_id="acme-payments",
                        merchant_id=str(scenario.ticket["merchant_id"]),
                        subject=str(scenario.ticket["subject"]),
                        body=body,
                    ),
                    config={"configurable": {"thread_id": f"ticket:{ticket_id}"}},
                )

        spans = exporter.get_finished_spans()
        assert len(spans) > 5, f"only {len(spans)} spans; the run was not traced"

        # Every attribute on every span, as one string. A substring match is
        # the right test here: it catches the body arriving whole, truncated,
        # or nested inside something larger.
        everything = " ".join(
            f"{k}={v}" for span in spans for k, v in dict(span.attributes or {}).items()
        )
        assert body not in everything
        for fragment in body.split(". "):
            if len(fragment) > 20:
                assert fragment not in everything, (
                    f"a fragment of the ticket body is in a span: {fragment!r}"
                )

        # And the spans that should exist, do.
        names = {span.name for span in spans}
        assert "concord.run" in names
        assert any(n.startswith("node.") for n in names)
        assert any(n.startswith("tool.") for n in names)
