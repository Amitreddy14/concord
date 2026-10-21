"""Test rig for the tool layer.

The invoker is exercised against the *real* mock services rather than against
stubbed HTTP responses. Stubs would let the tool layer and the services drift
apart — the tool layer would pass while integration failed — and the whole
argument for building the mocks first (ADR 0002) was to avoid exactly that.

Services are reached in-process through a transport that routes by base URL,
so there are no ports to allocate and no servers to wait for, while the
invoker still goes through real request building, headers and status codes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from apps.mocks.faults import FaultConfig, FaultProfile
from apps.mocks.serve import build
from evals.fixtures import Scenario, load_all, reset_payload
from packages.policy.tenant import TenantConfig, load_tenant
from packages.shared.ledger import InMemoryLedger
from packages.tools.breaker import BreakerRegistry
from packages.tools.invoker import ToolInvoker

BASE_URLS = {
    "crm": "http://crm.test",
    "payments": "http://payments.test",
    "ticketing": "http://ticketing.test",
    "knowledge": "http://knowledge.test",
}


class RoutingTransport(httpx.AsyncBaseTransport):
    """Dispatch to one of several ASGI apps by request host."""

    def __init__(self, apps: dict[str, object]) -> None:
        self._transports = {
            host: httpx.ASGITransport(app=app)  # type: ignore[arg-type]
            for host, app in apps.items()
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        transport = self._transports.get(host)
        if transport is None:
            raise httpx.ConnectError(f"no service registered for host {host!r}")
        return await transport.handle_async_request(request)


class FrozenClock:
    """A clock the breaker tests advance by hand rather than sleeping."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingSleep:
    """Collects the delays the invoker asked for instead of waiting them out.

    Asserting on the schedule is more useful than asserting that time passed,
    and it keeps the suite fast enough to run on every commit.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture(scope="session")
def scenarios() -> dict[str, Scenario]:
    return load_all()


@pytest.fixture
def tenant() -> TenantConfig:
    load_tenant.cache_clear()
    return load_tenant("acme-payments")


@pytest.fixture
def services() -> dict[str, object]:
    """One app per service, with faults off unless a test rebuilds them."""
    return {
        "crm": build("crm", FaultConfig(profile=FaultProfile.OFF)),
        "payments": build("payments", FaultConfig(profile=FaultProfile.OFF)),
        "ticketing": build("ticketing", FaultConfig(profile=FaultProfile.OFF)),
        "knowledge": build("knowledge", FaultConfig(profile=FaultProfile.OFF)),
    }


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def sleeper() -> RecordingSleep:
    return RecordingSleep()


@pytest.fixture
async def invoker(
    tenant: TenantConfig,
    services: dict[str, object],
    clock: FrozenClock,
    sleeper: RecordingSleep,
) -> AsyncIterator[ToolInvoker]:
    apps = {httpx.URL(url).host: app for url, app in zip(BASE_URLS.values(), services.values())}
    transport = RoutingTransport(apps)
    async with httpx.AsyncClient(transport=transport) as client:
        yield ToolInvoker(
            tenant=tenant,
            base_urls=dict(BASE_URLS),
            client=client,
            ledger=InMemoryLedger(),
            breakers=BreakerRegistry(failure_threshold=3, cooldown=timedelta(seconds=30), clock=clock),
            sleep=sleeper,
        )


def seed_all(services: dict[str, object], scenario: Scenario) -> None:
    """Load one scenario into every service, synchronously, before a test."""
    from fastapi.testclient import TestClient

    payload = reset_payload(scenario)
    for app in services.values():
        with TestClient(app) as client:  # type: ignore[arg-type]
            response = client.post("/_test/reset", json=payload)
            assert response.status_code == 200, response.text
