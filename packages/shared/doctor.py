"""`make doctor` — verify local inference and measure what it actually does.

Three questions, answered against the real gateway rather than assumed:

1. Is the configured model there, and does it support tool calling? A model
   without tool support ignores a tools array and answers in prose, so the
   failure appears much later as a node that never acts.
2. What is the decode rate on *this* machine? ADR 0003 accepts a throughput
   cost; the number that cost amounts to should be measured, not quoted from
   a blog. Phase 8 uses it as the eval harness's latency baseline.
3. How much VRAM headroom is left once the model is resident? That headroom
   is what bounds the context length the nodes can be given.

Exits non-zero when something is wrong, so it can gate a demo.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx

from packages.shared.llm import LLMError, OllamaGateway

PROBE_PROMPT = "Count from 1 to 40, separated by single spaces. Output only the numbers."
PROBE_MAX_TOKENS = 160

# Below this the model is almost certainly on CPU rather than the GPU.
MIN_USABLE_DECODE_RATE = 10
# Less headroom than this and the context length has to come down.
MIN_VRAM_HEADROOM_MIB = 1500


@dataclass
class Finding:
    label: str
    value: str
    ok: bool = True
    note: str = ""

    def render(self) -> str:
        marker = "ok  " if self.ok else "FAIL"
        line = f"  [{marker}] {self.label:<26} {self.value}"
        return f"{line}\n         {self.note}" if self.note else line


def gpu_memory() -> Finding:
    """VRAM in use, from nvidia-smi if it is present.

    Call this only once the model is resident. Before that it reports idle
    memory, which says nothing about the headroom the context length has to
    fit into.
    """
    if shutil.which("nvidia-smi") is None:
        return Finding("gpu", "no nvidia-smi on PATH", ok=True, note="inference may be on CPU")
    try:
        output = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as error:
        return Finding("gpu", f"nvidia-smi failed: {error}", ok=True)

    name, used, total = (part.strip() for part in output.splitlines()[0].split(","))
    headroom = int(total) - int(used)
    return Finding(
        "gpu",
        f"{name}: {used} of {total} MiB used, {headroom} MiB free",
        ok=True,
        note=(
            "under 1500 MiB free limits the context length the nodes can use"
            if headroom < MIN_VRAM_HEADROOM_MIB
            else ""
        ),
    )


async def measure(gateway: OllamaGateway) -> tuple[Finding, Finding]:
    """A cold call then a warm one, so load time is not counted as decode time."""
    try:
        await gateway.complete(
            system="You are a counter.",
            user=PROBE_PROMPT,
            node="doctor-warmup",
            run_id="doctor",
            budget_tokens=2048,
            max_tokens=PROBE_MAX_TOKENS,
        )
    except LLMError as error:
        return (
            Finding("warm-up", str(error), ok=False),
            Finding("decode rate", "not measured", ok=False),
        )

    started = time.perf_counter()
    completion = await gateway.complete(
        system="You are a counter.",
        user=PROBE_PROMPT,
        node="doctor-measure",
        run_id="doctor",
        budget_tokens=2048,
        max_tokens=PROBE_MAX_TOKENS,
    )
    elapsed = time.perf_counter() - started

    tokens = completion.completion_tokens or 1
    rate = tokens / elapsed if elapsed > 0 else 0

    slow = rate < MIN_USABLE_DECODE_RATE
    return (
        Finding("warm-up", "model resident", ok=True),
        Finding(
            "decode rate",
            f"{rate:.1f} tokens/second ({tokens} tokens in {elapsed:.1f}s)",
            ok=not slow,
            note=(
                "below 10 tok/s suggests the model is on CPU; check `ollama ps` "
                "shows PROCESSOR as GPU"
                if slow
                else ""
            ),
        ),
    )


async def run() -> int:
    base_url = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
    model = os.environ.get("LLM_MODEL_PRIMARY", "qwen3:8b")

    print(f"concord doctor — {base_url}, model {model}\n")
    findings: list[Finding] = []

    async with httpx.AsyncClient() as client:
        gateway = OllamaGateway(base_url=base_url, model=model, client=client)

        try:
            health = await gateway.health()
        except LLMError as error:
            findings.append(Finding("gateway", str(error), ok=False))
            for finding in findings:
                print(finding.render())
            print("\nis ollama running? try: ollama serve")
            return 1

        findings.append(Finding("gateway", "reachable", ok=True))
        findings.append(Finding("model", f"{model} available", ok=True))
        findings.append(
            Finding(
                "tool calling",
                "supported" if health["supports_tool_calling"] else "NOT supported",
                ok=health["supports_tool_calling"],
                note=(
                    ""
                    if health["supports_tool_calling"]
                    else "this model ignores tools and answers in prose; the agent will "
                    "never act. Choose a model with tool support."
                ),
            )
        )

        warmup, rate = await measure(gateway)
        findings.extend([warmup, rate])

        # Read VRAM *after* the model is resident. Reading it first reported
        # the idle figure — "8188 MiB free" on an 8GB card — which is not
        # headroom, is not what the check claims to measure, and would have
        # looked fine right up until a context length was sized against it.
        findings.append(gpu_memory())

    for finding in findings:
        print(finding.render())

    failed = [f for f in findings if not f.ok]
    if failed:
        print(
            f"\n{len(failed)} problem(s). The agent will not work correctly until they are fixed."
        )
        return 1

    print("\nlocal inference is ready.")
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
