"""Shared fixtures for the mock service tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from apps.mocks.faults import FaultConfig, FaultProfile
from apps.mocks.serve import build
from evals.fixtures import Scenario, load_all, reset_payload


@pytest.fixture(scope="session")
def scenarios() -> dict[str, Scenario]:
    return load_all()


def _client(service: str, profile: FaultProfile = FaultProfile.OFF) -> TestClient:
    return TestClient(build(service, FaultConfig(profile=profile)))


@pytest.fixture
def crm() -> Iterator[TestClient]:
    with _client("crm") as client:
        yield client


@pytest.fixture
def payments() -> Iterator[TestClient]:
    with _client("payments") as client:
        yield client


@pytest.fixture
def ticketing() -> Iterator[TestClient]:
    with _client("ticketing") as client:
        yield client


@pytest.fixture
def knowledge() -> Iterator[TestClient]:
    with _client("knowledge") as client:
        yield client


def seed(client: TestClient, scenario: Scenario) -> None:
    """Reset a service and load one scenario into it."""
    response = client.post("/_test/reset", json=reset_payload(scenario))
    assert response.status_code == 200, response.text
