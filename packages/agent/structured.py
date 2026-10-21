"""Structured output: constrained decoding, then validation, then repair.

Three layers, because each catches what the one before it misses.

**Constrained decoding** asks the server to restrict sampling to tokens that
can still produce a document matching the schema. When the server supports it
this is nearly airtight, and it is far stronger than asking a small model
politely for JSON.

**Validation** against the Pydantic model catches what the schema cannot
express — an id that matches a pattern but names nothing real, an enum value
outside the set the server's schema support happened to ignore.

**Repair** gives the model one bounded chance to fix its own output, told
exactly what was wrong. One chance, not a loop until it works: a model that
cannot satisfy the schema on the second attempt is not going to on the fifth,
and an unbounded repair loop is how a node silently costs thirty seconds.

The repair prompt carries the *error*, never the original untrusted text
again. Re-sending the ticket body on every repair would multiply the
injection surface by the number of attempts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from packages.shared.llm import Completion, LLMGateway

T = TypeVar("T", bound=BaseModel)

MAX_REPAIR_ATTEMPTS = 1

REPAIR_INSTRUCTION = (
    "Your previous output did not satisfy the required schema. "
    "Return only a corrected JSON document. Do not explain, apologise, or add prose. "
    "The errors were:"
)


class StructuredOutputError(Exception):
    """The model could not produce a valid document within the repair budget."""

    def __init__(self, node: str, attempts: int, errors: str, raw: str) -> None:
        super().__init__(
            f"node {node!r} failed to produce valid output after {attempts} attempt(s): {errors}"
        )
        self.node = node
        self.attempts = attempts
        self.errors = errors
        self.raw = raw


@dataclass(frozen=True, slots=True)
class StructuredResult[M: BaseModel]:
    value: M
    attempts: int
    repaired: bool
    completion: Completion


def extract_json(text: str) -> str:
    """Pull a JSON document out of a response that may carry extra prose.

    Constrained decoding should make this unnecessary. It is here because a
    gateway that silently ignores ``response_format`` — which some do — would
    otherwise fail every call, and failing softly on a recoverable formatting
    quirk is the right trade for a layer that already validates afterwards.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1] if "```" in stripped[3:] else stripped[3:]
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip("`").strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        return stripped
    return stripped[start : end + 1]


def _describe(error: ValidationError) -> str:
    lines = []
    for item in error.errors()[:5]:
        location = ".".join(str(p) for p in item["loc"]) or "<root>"
        lines.append(f"- {location}: {item['msg']}")
    return "\n".join(lines)


async def generate_structured[M: BaseModel](
    gateway: LLMGateway,
    model_type: type[M],
    *,
    system: str,
    user: str,
    node: str,
    run_id: str,
    budget_tokens: int,
    max_tokens: int = 1024,
) -> StructuredResult[M]:
    """Produce a validated instance of ``model_type``, repairing once if needed."""
    schema = model_type.model_json_schema()
    attempt = 0
    last_errors = ""
    last_raw = ""

    current_system = system
    current_user = user

    while attempt <= MAX_REPAIR_ATTEMPTS:
        attempt += 1
        completion = await gateway.complete(
            system=current_system,
            user=current_user,
            node=node,
            run_id=run_id,
            budget_tokens=budget_tokens,
            json_schema=schema,
            max_tokens=max_tokens,
        )
        last_raw = completion.text

        try:
            payload = json.loads(extract_json(completion.text))
        except json.JSONDecodeError as error:
            last_errors = f"- output was not valid JSON: {error.msg}"
        else:
            try:
                value = model_type.model_validate(payload)
            except ValidationError as error:
                last_errors = _describe(error)
            else:
                return StructuredResult(
                    value=value,
                    attempts=attempt,
                    repaired=attempt > 1,
                    completion=completion,
                )

        if attempt > MAX_REPAIR_ATTEMPTS:
            break

        # The repair turn carries the error and the schema, never the original
        # untrusted text. Re-sending the ticket body would multiply the
        # injection surface by the number of attempts.
        current_system = (
            f"{REPAIR_INSTRUCTION}\n{last_errors}\n\n"
            f"Required schema:\n{json.dumps(schema, separators=(',', ':'))}"
        )
        current_user = f"Your previous output was:\n{last_raw[:1500]}"

    raise StructuredOutputError(node, attempt, last_errors, last_raw)
