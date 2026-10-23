"""The dead-letter endpoints an operator works from.

Three verbs and no automation. Replay is something a person asks for, because
the failures that reach this queue are the ones where the right action depends
on facts outside the system — whether the gateway's incident is over, whether
the merchant was already refunded by hand. A queue that drained itself would
be a retry loop with extra storage.

The refusal is the part worth reading. A letter whose effect upstream is
unknown cannot be replayed at all, and the endpoint returns 409 rather than
replaying with a warning: a warning on a replay button is a warning people
click through, and what is on the other side of this one is a second refund.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, ConfigDict, Field

from packages.agent.deadletter import DeadLetterError, LetterStatus
from packages.shared.problem import STATE_CONFLICT, ProblemError, not_found

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from apps.api.main import Dependencies


class HandleLetter(BaseModel):
    """What an operator decided about a failed run."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["replay", "resolve", "abandon"]
    handled_by: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


ACTION_STATUS: dict[str, LetterStatus] = {
    "replay": LetterStatus.REPLAYED,
    "resolve": LetterStatus.RESOLVED,
    "abandon": LetterStatus.ABANDONED,
}


def build_router(
    *,
    get_deps: Callable[[Request], Dependencies],
    replay: Callable[..., Coroutine[Any, Any, None]],
    background: set[asyncio.Task[None]],
) -> APIRouter:
    router = APIRouter(prefix="/dead-letters", tags=["dead-letters"])

    @router.get("")
    async def list_letters(request: Request) -> dict[str, Any]:
        deps = get_deps(request)
        rows = deps.dead_letters.open_letters(tenant_id=deps.tenant.tenant_id)
        return {
            "count": len(rows),
            "dead_letters": [row.to_payload() for row in rows],
            # Surfaced as its own number because it is the one an operator
            # acts on differently: these need a person at the gateway before
            # anything else can happen.
            "needing_investigation": sum(1 for row in rows if row.effect_is_unknown),
        }

    @router.get("/{letter_id}")
    async def get_letter(letter_id: str, request: Request) -> dict[str, Any]:
        deps = get_deps(request)
        row = deps.dead_letters.get(letter_id)
        if row is None:
            raise not_found("dead letter", letter_id)
        return {**row.to_payload(), "state": row.state}

    @router.post("/{letter_id}/handle")
    async def handle_letter(
        letter_id: str,
        request: Request,
        decision: Annotated[HandleLetter, Body()],
    ) -> dict[str, Any]:
        deps = get_deps(request)
        row = deps.dead_letters.get(letter_id)
        if row is None:
            raise not_found("dead letter", letter_id)

        replay_run_id: str | None = None
        if decision.action == "replay":
            if not row.replayable:
                # 409 and not 400: the request is well formed and the
                # conflict is with what is known about the world.
                raise ProblemError(
                    STATE_CONFLICT,
                    f"dead letter {letter_id} cannot be replayed: its effect upstream is "
                    "unknown. Establish what happened at the gateway, then resolve or "
                    "abandon it.",
                )
            replay_run_id = f"{row.run_id}_replay"

        try:
            handled = deps.dead_letters.mark(
                letter_id,
                status=ACTION_STATUS[decision.action],
                handled_by=decision.handled_by,
                note=decision.note,
                replay_run_id=replay_run_id,
            )
        except DeadLetterError as error:
            raise ProblemError(STATE_CONFLICT, str(error)) from error

        if replay_run_id is not None:
            task: asyncio.Task[None] = asyncio.create_task(
                replay(handled, deps, replay_run_id=replay_run_id)
            )
            background.add(task)
            task.add_done_callback(background.discard)

        return {**handled.to_payload(), "replay_run_id": replay_run_id}

    return router
