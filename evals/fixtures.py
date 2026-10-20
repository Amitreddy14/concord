"""Loading and typing the golden scenario fixtures.

One loader shared by the mock seeder and the eval harness. If the two read the
fixtures separately they will eventually disagree about what S02 means, and
the disagreement will show up as a confusing test failure rather than as an
obvious one.

The fixtures are the scope definition, the seed data and the acceptance
criteria at once, which is why they are typed here rather than passed around
as raw dictionaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from apps.mocks.faults import FaultProfile, ScriptedFault
from apps.mocks.state import normalise

FIXTURE_DIR = Path(__file__).resolve().parent / "golden" / "fixtures"
SCENARIO_ID = re.compile(r"^S(0[1-9]|1[0-2])$")


@dataclass(frozen=True, slots=True)
class Assertion:
    """One ``must`` or ``should`` entry.

    ``must`` entries are deterministic and fail CI. ``should`` entries are
    graded. The split is deliberate: whether an approval gate was skipped is a
    fact, and a fact must never be handed to a judge.
    """

    name: str
    value: Any = None
    detail: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> Assertion:
        if isinstance(raw, str):
            return cls(name=raw)
        if not isinstance(raw, dict):
            raise TypeError(f"assertion must be a string or mapping, got {type(raw).__name__}")
        if "assert" in raw:
            known = {"assert", "value", "detail"}
            return cls(
                name=str(raw["assert"]),
                value=raw.get("value"),
                detail=raw.get("detail"),
                extra={k: v for k, v in raw.items() if k not in known},
            )
        # A `should` entry is a single-key mapping of check to expectation.
        name, value = next(iter(raw.items()))
        return cls(name=str(name), value=value)


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    title: str
    exercises: list[str]
    tenant: str
    seed: dict[str, Any]
    ticket: dict[str, Any]
    must: list[Assertion]
    should: list[Assertion]
    fault_profile: FaultProfile
    fault_seed: int
    fault_script: list[ScriptedFault]
    branches: dict[str, Any]
    variants: list[dict[str, Any]]
    repeat: dict[str, Any]
    evaluation_date: str | None
    source: Path

    @property
    def repeat_runs(self) -> int:
        """How many times the harness runs this scenario.

        S09 asks for five. One run producing one refund proves nothing about
        whether the idempotency key is derived or merely happened to collide.
        """
        return int(self.repeat.get("runs", 1))

    @property
    def requires_identical_outcome(self) -> bool:
        return bool(self.repeat.get("require_identical_outcome", False))


def load_scenario(path: Path) -> Scenario:
    raw = normalise(yaml.safe_load(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise TypeError(f"{path} does not contain a mapping")

    scenario_id = str(raw.get("id", ""))
    if not SCENARIO_ID.match(scenario_id):
        raise ValueError(f"{path.name}: id {scenario_id!r} is not S01..S12")

    return Scenario(
        scenario_id=scenario_id,
        title=str(raw.get("title", "")),
        exercises=list(raw.get("exercises") or []),
        tenant=str(raw.get("tenant", "acme-payments")),
        seed=dict(raw.get("seed") or {}),
        ticket=dict(raw.get("ticket") or {}),
        must=[Assertion.parse(a) for a in raw.get("must") or []],
        should=[Assertion.parse(a) for a in raw.get("should") or []],
        fault_profile=FaultProfile(raw.get("fault_profile", "off")),
        fault_seed=int(raw.get("fault_seed", 1337)),
        fault_script=[
            ScriptedFault(
                endpoint=str(entry["endpoint"]),
                attempt=int(entry["attempt"]),
                behaviour=entry["behaviour"],
                timeout_ms=entry.get("timeout_ms"),
            )
            for entry in raw.get("fault_script") or []
        ],
        branches=dict(raw.get("branches") or {}),
        variants=list(raw.get("variants") or []),
        repeat=dict(raw.get("repeat") or {}),
        evaluation_date=raw.get("evaluation_date"),
        source=path,
    )


def load_all(directory: Path | None = None) -> dict[str, Scenario]:
    """Every fixture, keyed by scenario id, in catalogue order."""
    directory = directory or FIXTURE_DIR
    scenarios: dict[str, Scenario] = {}
    for path in sorted(directory.glob("*.yaml")):
        scenario = load_scenario(path)
        if scenario.scenario_id in scenarios:
            raise ValueError(f"duplicate scenario id {scenario.scenario_id} in {path.name}")
        scenarios[scenario.scenario_id] = scenario
    return dict(sorted(scenarios.items()))


def reset_payload(scenario: Scenario) -> dict[str, Any]:
    """The body for a service's ``POST /_test/reset``."""
    return {
        "seed": scenario.seed,
        "fault_profile": str(scenario.fault_profile),
        "fault_seed": scenario.fault_seed,
        "fault_script": [
            {
                "endpoint": f.endpoint,
                "attempt": f.attempt,
                "behaviour": f.behaviour,
                **({"timeout_ms": f.timeout_ms} if f.timeout_ms is not None else {}),
            }
            for f in scenario.fault_script
        ],
    }
