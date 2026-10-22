"""Choosing a checkpointer.

ADR 0001 chose LangGraph for durable mid-graph pause, which is only durable if
the checkpointer is. In-memory checkpoints are fine for a test and useless for
the thing the choice was made for: a refund awaiting a reviewer's approval has
to survive the process restarting.

One thread per ticket. The thread id is the ticket id rather than the run id,
because a ticket resumed after an approval is the same conversation — a second
run against the same ticket should see what the first established, not start
from nothing.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.memory import MemorySaver


def thread_id_for(ticket_id: str) -> str:
    """The checkpoint thread a ticket's runs share."""
    return f"ticket:{ticket_id}"


@asynccontextmanager
async def checkpointer(
    database_url: str | None = None, *, setup: bool = True
) -> AsyncIterator[Any]:
    """Yield a checkpointer, Postgres-backed when a database URL is configured.

    Falls back to memory only when no URL is present. That fallback is a
    development convenience and nothing more: a deployment that silently ran
    without durable checkpoints would lose every pending approval on restart
    and look fine until it did.
    """
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        yield MemorySaver()
        return

    # Imported lazily so the memory path carries no psycopg dependency, and
    # a development machine without Postgres installed can still run.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: PLC0415

    # LangGraph's own URL form is plain libpq; the project's DATABASE_URL
    # carries SQLAlchemy's driver prefix for alembic's benefit.
    libpq_url = url.replace("postgresql+psycopg://", "postgresql://")

    async with AsyncPostgresSaver.from_conn_string(libpq_url) as saver:
        if setup:
            # Idempotent: creates the checkpoint tables if they are absent.
            await saver.setup()
        yield saver
