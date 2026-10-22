"""The durable checkpointer, against a real Postgres.

ADR 0001 chose LangGraph for durable mid-graph pause. That choice is only
worth anything if the checkpointer is durable, and "durable" is not a property
that can be tested against an in-memory saver — the test would pass while the
thing it claims to verify was absent.

These tests skip when no Postgres is configured, so the suite still runs on a
laptop with nothing installed, and CI provides one so the path is genuinely
exercised rather than permanently skipped.
"""

from __future__ import annotations

import os

import pytest

from packages.agent.checkpointing import checkpointer, thread_id_for

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not DATABASE_URL, reason="TEST_DATABASE_URL is not set; skipping the durable path"
)


class TestThreadIdentity:
    def test_a_thread_belongs_to_a_ticket_not_a_run(self) -> None:
        # A ticket resumed after an approval is the same conversation. Keying
        # the thread on the run id would make the resumed run start from
        # nothing, which defeats the reason for having a checkpointer.
        assert thread_id_for("tkt_1") == thread_id_for("tkt_1")
        assert thread_id_for("tkt_1") != thread_id_for("tkt_2")


class TestFallback:
    async def test_no_database_url_yields_an_in_memory_saver(self) -> None:
        from langgraph.checkpoint.memory import MemorySaver

        async with checkpointer(None) as saver:
            assert isinstance(saver, MemorySaver)


@requires_postgres
class TestDurability:
    async def test_state_survives_a_new_checkpointer_instance(self) -> None:
        # The property the whole choice rests on: a graph paused by one
        # process is resumable by another. Asserted by writing through one
        # saver and reading through a second, which is as close to a restart
        # as a test can get without forking.
        from langgraph.checkpoint.memory import MemorySaver  # noqa: F401

        from packages.agent.graph import build_graph

        thread = {"configurable": {"thread_id": thread_id_for("tkt_durable")}}

        async with checkpointer(DATABASE_URL) as saver:
            graph = build_graph(_stub_context(), saver)
            await graph.aupdate_state(thread, {"merchant_id": "M-1007", "visited": ["triage"]})

        async with checkpointer(DATABASE_URL) as second:
            graph = build_graph(_stub_context(), second)
            snapshot = await graph.aget_state(thread)

        assert snapshot.values["merchant_id"] == "M-1007"
        assert "triage" in snapshot.values["visited"]

    async def test_setup_is_idempotent(self) -> None:
        # Called on every startup, so creating the tables twice must not fail.
        async with checkpointer(DATABASE_URL):
            pass
        async with checkpointer(DATABASE_URL):
            pass

    async def test_two_tickets_do_not_share_state(self) -> None:
        from packages.agent.graph import build_graph

        async with checkpointer(DATABASE_URL) as saver:
            graph = build_graph(_stub_context(), saver)
            await graph.aupdate_state(
                {"configurable": {"thread_id": thread_id_for("tkt_a")}},
                {"merchant_id": "M-1001"},
            )
            await graph.aupdate_state(
                {"configurable": {"thread_id": thread_id_for("tkt_b")}},
                {"merchant_id": "M-2002"},
            )
            a = await graph.aget_state({"configurable": {"thread_id": thread_id_for("tkt_a")}})
            b = await graph.aget_state({"configurable": {"thread_id": thread_id_for("tkt_b")}})

        assert a.values["merchant_id"] == "M-1001"
        assert b.values["merchant_id"] == "M-2002"


def _stub_context():  # type: ignore[no-untyped-def]
    """A context whose nodes are never reached; only persistence is exercised."""
    from packages.agent.nodes import NodeContext
    from packages.shared.llm import ScriptedGateway

    return NodeContext(gateway=ScriptedGateway(), invoker=None, tenant_id="acme-payments")  # type: ignore[arg-type]
