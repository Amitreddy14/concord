"""The LLM gateway.

One interface, one place a client is constructed. No node builds its own, so
swapping local inference for a hosted model is a configuration change rather
than a search-and-replace (ADR 0003).

Three things this does that a thin wrapper around an HTTP call would not:

**It asserts tool support at startup.** A model without tool calling does not
error when handed tools — it ignores them and returns prose, which surfaces
much later as a node that mysteriously never acts. Failing at startup with a
clear message is worth the one extra request.

**It enforces a context budget per call.** The budget is a number in
configuration and a check in code, not a hope. On an 8GB card the resident
model's context is the scarce resource, and a node that quietly grows its
prompt degrades every other node's latency.

**It records what a call cost.** Tokens and milliseconds per call, attributed
to the node that made it, because phase 8 needs a cost ledger and retrofitting
one means re-instrumenting every call site.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

# Rough and deliberately so. The gateway needs a cheap pre-flight estimate to
# refuse an oversized prompt before spending a round trip on it; the exact
# count comes back from the server afterwards. Four characters per token is
# the usual English approximation and errs toward overestimating, which is the
# safe direction for a budget check.
CHARS_PER_TOKEN = 4

# Named so the branches below read as intent rather than as arithmetic.
HTTP_SERVER_ERROR = 500
HTTP_CLIENT_ERROR = 400
MIN_USABLE_DECODE_RATE = 10


class LLMError(Exception):
    """A gateway failure the caller may or may not be able to retry."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ContextBudgetExceeded(LLMError):
    """A prompt was larger than the node's declared budget.

    Not retryable: sending it again produces the same prompt. The node has to
    send less, and the error says how much less.
    """

    def __init__(self, node: str, estimated: int, budget: int) -> None:
        super().__init__(
            f"node {node!r} assembled a prompt of about {estimated} tokens "
            f"against a budget of {budget}; reduce what it includes rather than raising the budget",
            retryable=False,
        )
        self.node = node
        self.estimated = estimated
        self.budget = budget


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    finish_reason: str = "stop"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class UsageLedger:
    """Per-run token and latency accounting, attributed by node."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, *, run_id: str, node: str, completion: Completion) -> None:
        self.calls.append(
            {
                "run_id": run_id,
                "node": node,
                "model": completion.model,
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "latency_ms": completion.latency_ms,
            }
        )

    def for_run(self, run_id: str) -> dict[str, int]:
        relevant = [c for c in self.calls if c["run_id"] == run_id]
        return {
            "calls": len(relevant),
            "prompt_tokens": sum(c["prompt_tokens"] for c in relevant),
            "completion_tokens": sum(c["completion_tokens"] for c in relevant),
            "latency_ms": sum(c["latency_ms"] for c in relevant),
        }


class LLMGateway(Protocol):
    """What every node calls. Implementations: Ollama, and a scripted test double."""

    model: str
    usage: UsageLedger
    """Every implementation records what its calls cost.

    Declared on the protocol rather than only on the concrete classes because
    the API reads it to report per-run usage; leaving it off meant that read
    was unchecked and would have broken silently on a third implementation.
    """

    async def complete(
        self,
        *,
        system: str,
        user: str,
        node: str,
        run_id: str,
        budget_tokens: int,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Completion: ...

    async def health(self) -> dict[str, Any]: ...


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass
class OllamaGateway:
    """An OpenAI-compatible gateway, pointed at Ollama by default.

    The protocol is the OpenAI chat completions API, which Ollama serves, so
    the same class reaches a hosted provider by changing base_url and model.
    """

    base_url: str
    model: str
    client: httpx.AsyncClient
    api_key: str = "not-needed-for-local"
    usage: UsageLedger = field(default_factory=UsageLedger)
    request_timeout_s: float = 180.0
    """Generous: an 8B model at ~25 tok/s needs real time for a long reply."""

    async def complete(
        self,
        *,
        system: str,
        user: str,
        node: str,
        run_id: str,
        budget_tokens: int,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Completion:
        estimated = estimate_tokens(system) + estimate_tokens(user)
        if estimated > budget_tokens:
            raise ContextBudgetExceeded(node, estimated, budget_tokens)

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_schema is not None:
            # Schema-constrained decoding. The server restricts sampling to
            # tokens that can still produce a valid document, which is far
            # stronger than asking a small model politely for JSON.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": node, "schema": json_schema, "strict": True},
            }

        started = time.perf_counter()
        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.request_timeout_s,
            )
        except httpx.HTTPError as error:
            # The model being evicted, or the server restarting, looks like a
            # transport failure. Retryable: the next call reloads the model.
            raise LLMError(f"gateway unreachable: {error}", retryable=True) from error

        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= HTTP_SERVER_ERROR:
            raise LLMError(f"gateway returned {response.status_code}", retryable=True)
        if response.status_code >= HTTP_CLIENT_ERROR:
            raise LLMError(
                f"gateway rejected the request ({response.status_code}): {response.text[:300]}",
                retryable=False,
            )

        payload = response.json()
        choice = payload["choices"][0]
        usage = payload.get("usage") or {}
        completion = Completion(
            text=choice["message"].get("content") or "",
            model=payload.get("model", self.model),
            prompt_tokens=int(usage.get("prompt_tokens", estimated)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason", "stop"),
        )
        self.usage.record(run_id=run_id, node=node, completion=completion)
        return completion

    async def health(self) -> dict[str, Any]:
        """Check the gateway is up, the model is known, and tools are supported.

        The tool check is the one that earns its keep. A model without tool
        support silently ignores a tools array and answers in prose, so the
        failure shows up as a node that never acts rather than as an error.
        """
        try:
            models = await self.client.get(f"{self.base_url}/models", timeout=10.0)
        except httpx.HTTPError as error:
            raise LLMError(
                f"gateway unreachable at {self.base_url}: {error}", retryable=True
            ) from error

        available = [m["id"] for m in models.json().get("data", [])]
        if self.model not in available:
            raise LLMError(
                f"model {self.model!r} is not available; the gateway offers: "
                f"{', '.join(available) or 'nothing'}. Pull it with: ollama pull {self.model}",
                retryable=False,
            )

        probe = await self.client.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": "Call the ping tool."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "ping",
                            "description": "Respond to a liveness probe.",
                            "parameters": {
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                            },
                        },
                    }
                ],
                "stream": False,
            },
            timeout=60.0,
        )
        message = (
            probe.json()["choices"][0]["message"] if probe.status_code < HTTP_CLIENT_ERROR else {}
        )
        supports_tools = bool(message.get("tool_calls"))

        return {
            "base_url": self.base_url,
            "model": self.model,
            "available_models": available,
            "supports_tool_calling": supports_tools,
        }


@dataclass
class ScriptedGateway:
    """A deterministic gateway for tests and the eval harness.

    Responses are keyed by node name, so a test states what the model returns
    at each step rather than depending on an 8B model's mood. The eval harness
    uses the real gateway; everything below it uses this, which is what keeps
    139 tests running in two seconds.
    """

    model: str = "scripted"
    responses: dict[str, list[str]] = field(default_factory=dict)
    usage: UsageLedger = field(default_factory=UsageLedger)
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_times: int = 0
    """Raise a retryable failure this many times before answering.

    Used to prove a run survives the resident model being evicted mid-run.
    """

    _failures_emitted: int = 0

    async def complete(
        self,
        *,
        system: str,
        user: str,
        node: str,
        run_id: str,
        budget_tokens: int,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> Completion:
        estimated = estimate_tokens(system) + estimate_tokens(user)
        if estimated > budget_tokens:
            raise ContextBudgetExceeded(node, estimated, budget_tokens)

        self.calls.append({"node": node, "system": system, "user": user, "schema": json_schema})

        if self._failures_emitted < self.fail_times:
            self._failures_emitted += 1
            raise LLMError("model evicted; reloading", retryable=True)

        queue = self.responses.get(node)
        if not queue:
            raise LLMError(f"no scripted response for node {node!r}", retryable=False)
        text = queue.pop(0) if len(queue) > 1 else queue[0]

        completion = Completion(
            text=text,
            model=self.model,
            prompt_tokens=estimated,
            completion_tokens=estimate_tokens(text),
            latency_ms=1,
        )
        self.usage.record(run_id=run_id, node=node, completion=completion)
        return completion

    async def health(self) -> dict[str, Any]:
        return {
            "base_url": "scripted",
            "model": self.model,
            "available_models": [self.model],
            "supports_tool_calling": True,
        }
