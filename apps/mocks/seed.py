"""Load scenario fixtures into running mock services.

Backs ``make seed``. The services are reset and seeded over their ``/_test``
control plane rather than by reaching into their process, so the seeder has no
coupling to them beyond HTTP — the same path the eval harness uses.

Seeding is per scenario, not cumulative. Two scenarios both define merchant
M-1007 with different expectations around it, so loading all twelve at once
would produce a state that matches none of them.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from evals.fixtures import Scenario, load_all, reset_payload

DEFAULT_SERVICES = {
    "crm": "http://localhost:8101",
    "payments": "http://localhost:8102",
    "ticketing": "http://localhost:8103",
    "knowledge": "http://localhost:8104",
}


def post_json(url: str, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body: dict[str, Any] = json.loads(response.read())
        return body


def seed_scenario(scenario: Scenario, services: dict[str, str]) -> dict[str, str]:
    """Reset every service and load this scenario's state into each.

    All four get the same payload. Each keeps only the part it owns, which
    keeps the fixture readable as one description of a situation rather than
    four disjoint fragments.
    """
    payload = reset_payload(scenario)
    results: dict[str, str] = {}
    for name, base_url in services.items():
        try:
            response = post_json(f"{base_url}/_test/reset", payload)
            results[name] = response.get("status", "unknown")
        except urllib.error.URLError as error:
            results[name] = f"unreachable: {error.reason}"
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the mock systems from a golden fixture")
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="fixture directory (defaults to evals/golden/fixtures)",
    )
    parser.add_argument(
        "--scenario",
        default="S02",
        help="scenario id to load, e.g. S09. Seeding is per scenario, not cumulative.",
    )
    parser.add_argument("--list", action="store_true", help="list available scenarios and exit")
    args = parser.parse_args(argv)

    scenarios = load_all(args.fixtures)

    if args.list:
        for scenario_id, scenario in scenarios.items():
            print(f"{scenario_id}  {scenario.title}")
        return 0

    requested = args.scenario.upper()
    if requested not in scenarios:
        print(
            f"unknown scenario {requested!r}; available: {', '.join(scenarios)}",
            file=sys.stderr,
        )
        return 2
    scenario = scenarios[requested]

    results = seed_scenario(scenario, DEFAULT_SERVICES)
    print(f"{scenario.scenario_id}  {scenario.title}")
    failed = False
    for name, status in results.items():
        marker = "ok" if status == "reset" else "FAILED"
        if status != "reset":
            failed = True
        print(f"  {name:10s} {marker:7s} {status}")
    if failed:
        print("\nsome services were unreachable; is `make up` running?", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
