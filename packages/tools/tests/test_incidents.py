"""The incident status split must cover the contract, exhaustively.

Enumerated from `contracts/knowledge.yaml` rather than from a list written
here, because a list written here is a second opinion about the contract and
the whole bug was two opinions disagreeing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from packages.tools.incidents import ACTIVE_STATUSES, TERMINAL_STATUSES, is_active

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "knowledge.yaml"


def _contract_statuses() -> set[str]:
    document: dict[str, Any] = yaml.safe_load(CONTRACT.read_text())
    incident = document["components"]["schemas"]["Incident"]
    return set(incident["properties"]["status"]["enum"])


def test_every_contract_status_is_classified() -> None:
    """The totality test. A new status must be classified by a person.

    Left unclassified, `is_active` returns False and the incident silently
    stops being citable — or, in the version of this bug that shipped, the
    harness decided the opposite of what the prompt said.
    """
    classified = ACTIVE_STATUSES | TERMINAL_STATUSES
    statuses = _contract_statuses()

    assert statuses - classified == set(), (
        f"these incident statuses are in the contract and classified nowhere: "
        f"{sorted(statuses - classified)}"
    )
    assert classified - statuses == set(), (
        f"these statuses are classified here and absent from the contract: "
        f"{sorted(classified - statuses)}"
    )


def test_the_two_sets_do_not_overlap() -> None:
    assert not ACTIVE_STATUSES & TERMINAL_STATUSES


def test_identified_and_monitoring_are_active() -> None:
    """The two the old hand-written set omitted.

    An incident whose cause is known, or which is being watched after a fix,
    is still the reason a merchant's payout is late. S07 seeds `identified`
    and a model citing it was marked wrong.
    """
    assert is_active({"status": "identified"})
    assert is_active({"status": "monitoring"})


def test_resolved_is_not_active() -> None:
    assert not is_active({"status": "resolved"})


def test_an_unknown_status_is_not_active() -> None:
    # "Might be open" is not a basis for telling a merchant why money is late.
    assert not is_active({"status": "open"})
    assert not is_active({})
