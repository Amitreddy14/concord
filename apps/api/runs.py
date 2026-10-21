"""Run records and the in-process run store.

A resolution on local inference takes tens of seconds (ADR 0003), so the API
cannot be request/response: a client would hold a connection open through the
whole thing and a mobile network would drop it. Submitting returns a run id
immediately, and progress is read or streamed.

The store is in-process for phase 3. Phase 5 moves it to Postgres alongside
the LangGraph checkpointer; the shape here is the shape that migrates.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass
class RunEvent:
    """One progress event, as delivered over SSE."""

    seq: int
    at: datetime
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at.isoformat().replace("+00:00", "Z"),
            "kind": self.kind,
            **self.detail,
        }


@dataclass
class Run:
    run_id: str
    ticket_id: str
    tenant_id: str
    merchant_id: str
    status: RunStatus = RunStatus.QUEUED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    result: dict[str, Any] | None = None
    failure: str | None = None
    events: list[RunEvent] = field(default_factory=list)

    # Subscribers get their own queue, so a slow client cannot block a fast
    # one and a client that disconnects mid-run does not stall the run.
    _subscribers: list[asyncio.Queue[RunEvent | None]] = field(default_factory=list)

    def emit(self, kind: str, **detail: Any) -> RunEvent:
        event = RunEvent(seq=len(self.events) + 1, at=datetime.now(UTC), kind=kind, detail=detail)
        self.events.append(event)
        for queue in self._subscribers:
            queue.put_nowait(event)
        return event

    def finish(self, status: RunStatus, **detail: Any) -> None:
        self.status = status
        self.ended_at = datetime.now(UTC)
        self.emit("run.finished", status=str(status), **detail)
        for queue in self._subscribers:
            queue.put_nowait(None)  # sentinel: close the stream
        self._subscribers.clear()

    def subscribe(self) -> asyncio.Queue[RunEvent | None]:
        """Attach a stream, replaying what has already happened.

        Replay matters because a client that connects a second after
        submitting would otherwise miss the first node's events and see a run
        that appears to start halfway through.
        """
        queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        for event in self.events:
            queue.put_nowait(event)
        if self.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ABANDONED):
            queue.put_nowait(None)
        else:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[RunEvent | None]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ticket_id": self.ticket_id,
            "tenant_id": self.tenant_id,
            "merchant_id": self.merchant_id,
            "status": str(self.status),
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "ended_at": self.ended_at.isoformat().replace("+00:00", "Z") if self.ended_at else None,
            "result": self.result,
            "failure": self.failure,
            "events": [e.to_payload() for e in self.events],
        }


@dataclass
class RunStore:
    runs: dict[str, Run] = field(default_factory=dict)
    _counter: int = 0

    def create(self, *, ticket_id: str, tenant_id: str, merchant_id: str) -> Run:
        self._counter += 1
        run = Run(
            run_id=f"run_{self._counter:06d}",
            ticket_id=ticket_id,
            tenant_id=tenant_id,
            merchant_id=merchant_id,
        )
        self.runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    def clear(self) -> None:
        self.runs.clear()
        self._counter = 0
