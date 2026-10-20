"""Deterministic fault injection for the mock systems of record.

The reason this exists is in ADR 0002: a vendor sandbox will not time out on
command, so the failure handling that phase 7 is built around could not be
tested against one. Untested failure handling is decoration.

Determinism is the whole point. Every decision comes from a counter and a seed,
never from wall-clock time or an unseeded RNG, so a named profile reproduces
the same failure sequence on every run. That is what lets S09 assert "exactly
one refund" across five repeated runs and mean it.

Three profiles:

``off``
    Nothing is injected. The default, and what the happy-path scenarios use.

``flaky``
    Intermittent transient failures — latency, the occasional 500 or 429.
    Everything here is retryable, so a correct tool layer recovers and the run
    still succeeds. This is the profile for "does retry work at all".

``hostile``
    ``flaky`` plus the failure that actually hurts: a mutation accepted
    upstream and then timed out at the client, so a naive retry creates a
    second effect. This is the profile S09 runs under.

A fixture may also pin an exact script of behaviours per endpoint and attempt
number, which is how S09 states its sequence rather than hoping the seed
produces it.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

Behaviour = Literal[
    "ok",
    "latency",
    "server_error",
    "rate_limited",
    "connection_reset",
    "accept_then_client_timeout",
    "replay_original",
]


class FaultProfile(StrEnum):
    OFF = "off"
    FLAKY = "flaky"
    HOSTILE = "hostile"


@dataclass(frozen=True, slots=True)
class ScriptedFault:
    """An exact behaviour pinned to an endpoint and attempt number.

    ``attempt`` is 1-based and counts attempts against that endpoint within a
    single run, so a fixture can say "the first POST /refunds times out after
    being accepted, the second replays the original".
    """

    on: str
    attempt: int
    behaviour: Behaviour
    timeout_ms: int | None = None


@dataclass(frozen=True, slots=True)
class FaultDecision:
    behaviour: Behaviour
    delay_ms: int = 0
    retry_after_s: int | None = None
    scripted: bool = False

    @property
    def is_ok(self) -> bool:
        return self.behaviour == "ok"


# Per-profile odds, expressed as a count out of 100 so the arithmetic stays
# integral and the thresholds are legible. Ordered: the first band a draw falls
# into wins.
_BANDS: dict[FaultProfile, tuple[tuple[Behaviour, int], ...]] = {
    FaultProfile.OFF: (),
    FaultProfile.FLAKY: (
        ("latency", 15),
        ("server_error", 8),
        ("rate_limited", 5),
    ),
    FaultProfile.HOSTILE: (
        ("latency", 20),
        ("server_error", 12),
        ("rate_limited", 8),
        ("connection_reset", 5),
    ),
}

_LATENCY_MS: dict[FaultProfile, int] = {
    FaultProfile.OFF: 0,
    FaultProfile.FLAKY: 400,
    FaultProfile.HOSTILE: 1200,
}


class FaultInjector:
    """Decides what a given request should do, reproducibly.

    The decision is a pure function of (seed, endpoint, attempt number), hashed
    rather than drawn from a stateful RNG. That matters because requests may
    arrive interleaved across endpoints: a shared RNG would make each
    endpoint's sequence depend on the others' timing, and the whole point is
    that it does not.
    """

    def __init__(
        self,
        profile: FaultProfile | str = FaultProfile.OFF,
        seed: int = 1337,
        script: list[ScriptedFault] | None = None,
    ) -> None:
        self.profile = FaultProfile(profile)
        self.seed = seed
        self._script: dict[tuple[str, int], ScriptedFault] = {
            (f.on, f.attempt): f for f in (script or [])
        }
        self._attempts: dict[str, int] = defaultdict(int)

    # -- state -----------------------------------------------------------

    def reset(self) -> None:
        """Clear attempt counters. Called between scenario runs."""
        self._attempts.clear()

    def attempts_for(self, endpoint: str) -> int:
        return self._attempts[endpoint]

    def load_script(self, script: list[ScriptedFault]) -> None:
        self._script = {(f.on, f.attempt): f for f in script}
        self._attempts.clear()

    # -- decision --------------------------------------------------------

    def _draw(self, endpoint: str, attempt: int) -> int:
        """A stable pseudo-random integer in [0, 100) for this exact request."""
        material = f"{self.seed}:{endpoint}:{attempt}".encode()
        digest = hashlib.blake2b(material, digest_size=8).digest()
        return int.from_bytes(digest, "big") % 100

    def decide(self, method: str, path_template: str) -> FaultDecision:
        endpoint = f"{method.upper()} {path_template}"
        self._attempts[endpoint] += 1
        attempt = self._attempts[endpoint]

        # A pinned script always wins. A fixture that states its sequence is
        # describing a requirement, not expressing a preference.
        scripted = self._script.get((endpoint, attempt))
        if scripted is not None:
            return FaultDecision(
                behaviour=scripted.behaviour,
                delay_ms=scripted.timeout_ms or 0,
                scripted=True,
            )

        if self.profile is FaultProfile.OFF:
            return FaultDecision("ok")

        draw = self._draw(endpoint, attempt)
        cumulative = 0
        for behaviour, weight in _BANDS[self.profile]:
            cumulative += weight
            if draw < cumulative:
                return FaultDecision(
                    behaviour=behaviour,
                    delay_ms=_LATENCY_MS[self.profile] if behaviour == "latency" else 0,
                    retry_after_s=1 if behaviour == "rate_limited" else None,
                )
        return FaultDecision("ok")


@dataclass
class FaultConfig:
    """Resolved fault settings for a service process."""

    profile: FaultProfile = FaultProfile.OFF
    seed: int = 1337
    script: list[ScriptedFault] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict[str, str]) -> FaultConfig:
        return cls(
            profile=FaultProfile(env.get("FAULT_PROFILE", "off")),
            seed=int(env.get("FAULT_SEED", "1337")),
        )
