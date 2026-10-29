"""Both tenants, decided by their own configuration.

Phase 9 claims a customer is a file rather than a branch (ADR 0006,
principle 5). This is the test of that claim, and it is built to be capable
of failing it: the expectations in `evals/golden/conformance.yaml` are written
by hand from the two tenant files, and every case records whether the desks
diverge and on which configured dimension.

The divergence bookkeeping is the part that does the work. An engine that
ignored tenant configuration entirely — a hardcoded policy, or a
tenant-specific branch guarded by an `if` — would satisfy a suite in which
the two tenants always agree. So `test_every_dimension_actually_diverges`
requires each dimension named in the file to change an outcome somewhere, and
the agreement cases are kept so the suite is not merely asserting that the
two tenants differ about everything.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from packages.policy.engine import Permission, ProposedAction, evaluate
from packages.policy.tenant import TenantConfig, load_tenant
from packages.shared.money import Money

CONFORMANCE = Path(__file__).resolve().parents[3] / "evals" / "golden" / "conformance.yaml"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

DOCUMENT: dict[str, Any] = yaml.safe_load(CONFORMANCE.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = DOCUMENT["decisions"]
SCOPE_CASES: list[dict[str, Any]] = DOCUMENT["scopes"]
DIMENSIONS: list[str] = DOCUMENT["divergence_dimensions"]
TENANTS = ("acme-payments", "northwind-pay")


def _action(spec: dict[str, Any], tenant: TenantConfig) -> ProposedAction:
    """Build the proposal, with money in this tenant's own currency.

    The currency comes from the tenant rather than the case, which is what
    makes the cap cases meaningful: the same minor-unit amount is a different
    sum of money on each desk, and that is exactly the situation a shared
    policy engine has to get right.
    """
    amount_minor = spec.get("amount_minor")
    return ProposedAction(
        kind=str(spec["kind"]),
        capture_id=spec.get("capture_id"),
        amount=Money(int(amount_minor), tenant.default_currency) if amount_minor else None,
        reason_code=spec.get("reason_code"),
        captures_touched=tuple(spec.get("captures_touched") or ()),
        touches=tuple(spec.get("touches") or ()),
    )


def _capture(spec: dict[str, Any] | None, tenant: TenantConfig) -> dict[str, Any] | None:
    if spec is None:
        return None
    captured_at = NOW - timedelta(days=int(spec["captured_days_ago"]))
    currency = tenant.default_currency
    return {
        "capture_id": "cap_1",
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "amount": {"amount_minor": int(spec["amount_minor"]), "currency": currency},
        "refunded_amount": {"amount_minor": int(spec["refunded_minor"]), "currency": currency},
    }


def _decide(case: dict[str, Any], tenant_id: str) -> Any:
    tenant = load_tenant(tenant_id)
    return evaluate(
        _action(case["action"], tenant),
        tenant=tenant,
        merchant=dict(case["merchant"]),
        capture=_capture(case.get("capture"), tenant),
        now=NOW,
    )


@pytest.mark.parametrize("tenant_id", TENANTS)
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_the_decision_matches_the_tenants_own_expectation(
    case: dict[str, Any], tenant_id: str
) -> None:
    expected = case["expect"][tenant_id]
    decision = _decide(case, tenant_id)

    assert str(decision.permission) == expected["permission"], (
        f"{case['id']} on {tenant_id}: expected {expected['permission']}, "
        f"got {decision.permission} because {'; '.join(decision.reasons)}"
    )
    assert decision.rule_id == expected["rule_id"], (
        f"{case['id']} on {tenant_id}: {expected['rule_id']} decided it, not {decision.rule_id}"
    )
    assert decision.risk_tier == expected["risk_tier"], (
        f"{case['id']} on {tenant_id}: classified {decision.risk_tier}, "
        f"expected {expected['risk_tier']}"
    )


@pytest.mark.parametrize("tenant_id", TENANTS)
@pytest.mark.parametrize("case", SCOPE_CASES, ids=[c["scope"] for c in SCOPE_CASES])
def test_scope_permission_matches_the_tenants_own_expectation(
    case: dict[str, Any], tenant_id: str
) -> None:
    permitted = load_tenant(tenant_id).permits_scope(str(case["scope"]))
    assert permitted is bool(case["expect"][tenant_id])


class TestTheSuiteCouldFailTheClaim:
    def test_every_dimension_actually_diverges(self) -> None:
        """Each configured difference must change an outcome somewhere.

        Without this, the suite passes against a hardcoded policy. It is the
        only assertion here that tests the *tests*, and it is the reason the
        conformance file records a dimension per divergent case rather than
        just an expectation.
        """
        covered = {c["divergence"] for c in CASES + SCOPE_CASES if c.get("divergence")}
        missing = [d for d in DIMENSIONS if d not in covered]

        assert not missing, (
            f"these configured differences change no outcome in any case: {missing}; "
            "either the dimension does not matter or the suite is not exercising it"
        )

    def test_each_declared_divergence_is_a_real_one(self) -> None:
        """A case claiming divergence must actually decide differently.

        The label is documentation, and documentation that disagrees with the
        behaviour it describes is worse than none — this is what stops a case
        keeping a `divergence:` tag after the configuration that caused it has
        been changed.
        """
        for case in CASES:
            if not case.get("divergence"):
                continue
            outcomes = {t: case["expect"][t] for t in TENANTS}
            assert outcomes[TENANTS[0]] != outcomes[TENANTS[1]], (
                f"{case['id']} is tagged divergence={case['divergence']} and both "
                "tenants expect the same decision"
            )
        for case in SCOPE_CASES:
            if not case.get("divergence"):
                continue
            assert case["expect"][TENANTS[0]] != case["expect"][TENANTS[1]], (
                f"{case['scope']} is tagged divergence={case['divergence']} and both "
                "tenants expect the same answer"
            )

    def test_the_suite_contains_agreement_cases_too(self) -> None:
        """Not every case may diverge.

        A suite where the two tenants differ on everything is consistent with
        an engine that reads the tenant id and nothing else.
        """
        agreements = [c for c in CASES if not c.get("divergence")]
        assert len(agreements) >= 3, (
            "too few cases where the desks agree; the suite would pass against an "
            "engine that keyed off the tenant id alone"
        )

    def test_every_case_covers_both_tenants(self) -> None:
        for case in CASES:
            assert set(case["expect"]) == set(TENANTS), f"{case['id']} is missing a tenant"
        for case in SCOPE_CASES:
            assert set(case["expect"]) == set(TENANTS), f"{case['scope']} is missing a tenant"

    def test_a_refusal_carries_no_risk_tier(self) -> None:
        """The shape the window and remainder cases assert, stated once.

        Both are checked before any tier is matched, because the obstacle is
        the state of the world rather than the agent's authority. A refusal
        arriving with a tier would mean a tier had been consulted about
        something no reviewer can authorise.
        """
        for case in CASES:
            for tenant_id in TENANTS:
                expected = case["expect"][tenant_id]
                if expected["permission"] == Permission.REFUSED:
                    assert expected["risk_tier"] is None, f"{case['id']} on {tenant_id}"


class TestTheRuleVocabularyIsHonest:
    """Every rule id the engine can return must be accounted for.

    Two constants — `W1-reason-code-not-autonomous` and
    `W2-multi-capture-requires-approval` — sat in this module unused for three
    phases. They were not harmless: they were leftovers from the design where
    the engine decided approval itself, and that is the defect Phase 6 fixed
    by making the tier table the single authority. A constant naming the old
    design is an invitation to use it and reintroduce two places deciding the
    same thing.

    So the vocabulary is pinned against the conformance file, which is the
    only place the reachable ids are written down independently of the code.
    """

    #: Reachable, and not reachable from these two tenants. A tier table with
    #: a gap returns T0, and neither Acme nor Northwind has one — both carry a
    #: catch-all refund tier, Northwind deliberately so, because omitting it
    #: would make a missing tier rather than a stated `approval` the thing
    #: holding a refund. Exercised in the engine's own unit tests instead.
    NOT_IN_CONFORMANCE: ClassVar[set[str]] = {"T0-no-tier-matched"}

    def _declared(self) -> dict[str, str]:
        from packages.policy import engine

        return {
            name: value
            for name, value in vars(engine).items()
            if name.startswith("RULE_") and isinstance(value, str)
        }

    def test_every_rule_id_is_reachable(self) -> None:
        text = CONFORMANCE.read_text(encoding="utf-8")
        unreachable = [
            f"{name} ({value})"
            for name, value in self._declared().items()
            if value not in text and value not in self.NOT_IN_CONFORMANCE
        ]

        assert not unreachable, (
            f"these rule ids are defined and no case can produce them: {unreachable}; "
            "either a case is missing or the constant is a leftover"
        )

    def test_the_retired_codes_have_not_come_back(self) -> None:
        values = set(self._declared().values())
        assert "W1-reason-code-not-autonomous" not in values
        assert "W2-multi-capture-requires-approval" not in values


class TestEveryTenantIsSelfContained:
    """A tenant file must not reference things that are not there.

    The loader parses the whole document deliberately — a typo in an autonomy
    cap should fail loudly rather than wait for the policy engine. The corpus
    paths were the gap in that: they are strings the loader keeps and never
    resolves, so a tenant could declare a corpus that does not exist and
    nothing would notice until retrieval returned nothing at runtime, which
    reads as a grounding failure rather than a missing directory.
    """

    REPO = Path(__file__).resolve().parents[3]

    @pytest.mark.parametrize("tenant_id", TENANTS)
    def test_every_declared_corpus_path_exists(self, tenant_id: str) -> None:
        tenant = load_tenant(tenant_id)
        corpus = tenant.grounding.get("corpus") or []
        assert corpus, f"{tenant_id} declares no corpus"

        missing = [
            str(entry.get("source"))
            for entry in corpus
            if not (self.REPO / str(entry.get("source"))).is_dir()
        ]
        assert not missing, f"{tenant_id} declares corpus directories that do not exist: {missing}"

    @pytest.mark.parametrize("tenant_id", TENANTS)
    def test_every_declared_corpus_path_holds_documents(self, tenant_id: str) -> None:
        # An empty directory satisfies the check above and still grounds
        # nothing, which is the same failure one step later.
        tenant = load_tenant(tenant_id)
        for entry in tenant.grounding.get("corpus") or []:
            source = self.REPO / str(entry["source"])
            assert any(source.glob("*.md")), f"{tenant_id}: {entry['source']} holds no documents"


class TestNoTenantSpecificBranch:
    """The claim ADR 0006 principle 5 makes, enforced rather than asserted.

    A customer is a file. If a tenant id appears in `packages/` as *code*,
    something has been special-cased and the next customer is a code change —
    which is the thing this architecture exists to avoid and the easiest thing
    to do accidentally under deadline.

    Deliberately an AST walk rather than a grep, and the first version was a
    grep: it failed on `packages/knowledge/corpus.py`, whose docstring records
    the bug where the corpus directory was named `acme` while the tenant was
    `acme-payments`. That file is the fix for tenant-specific behaviour, and a
    guard that fires on the prose explaining the thing it guards is the same
    mistake `check_money.py` was rewritten to avoid — its docstring says it is
    "not a grep, so it does not fire on the word 'float' in a comment".

    Comments never reach the AST. Docstrings are skipped explicitly, because a
    docstring is documentation and `if tenant_id == "acme-payments"` is not.
    """

    PACKAGES = Path(__file__).resolve().parents[2]

    def _sources(self) -> list[Path]:
        return [
            path
            for path in self.PACKAGES.rglob("*.py")
            if "__pycache__" not in path.parts and "tests" not in path.parts
        ]

    @staticmethod
    def _docstring_nodes(tree: ast.AST) -> set[int]:
        """Ids of the string constants that are docstrings, to be ignored."""
        ids: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(
                node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            ):
                continue
            body = getattr(node, "body", None) or []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
        return ids

    @classmethod
    def _code_strings(cls, path: Path) -> list[str]:
        """Every string literal in a module except its docstrings."""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = cls._docstring_nodes(tree)
        return [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
        ]

    def _offenders(self, needles: list[str]) -> list[str]:
        found: list[str] = []
        for path in self._sources():
            strings = self._code_strings(path)
            for needle in needles:
                if not needle:
                    continue
                if any(needle in value for value in strings):
                    found.append(f"{path.relative_to(self.PACKAGES.parent)} contains {needle!r}")
        return found

    @pytest.mark.parametrize("tenant_id", TENANTS)
    def test_no_module_names_a_tenant(self, tenant_id: str) -> None:
        assert not self._offenders([tenant_id]), (
            f"{tenant_id} appears as code in "
            f"{self._offenders([tenant_id])}; a tenant belongs in "
            "infra/tenants/<id>.yaml, not in a module"
        )

    @pytest.mark.parametrize("tenant_id", TENANTS)
    def test_no_display_name_or_sign_off_leaks_in(self, tenant_id: str) -> None:
        """The subtler form: not the id, but a value only one tenant has.

        A sign-off string or a display name hardcoded as a fallback is the
        same special case wearing a different hat, and harder to spot because
        it reads as a sensible default rather than as a customer name.
        """
        tenant = load_tenant(tenant_id)
        needles = [tenant.display_name, str(tenant.reply_style.get("sign_off", ""))]
        assert not self._offenders(needles), self._offenders(needles)

    def test_the_guard_sees_a_real_special_case(self) -> None:
        """The guard must be able to fail.

        An AST walk that skipped too much would pass on everything. This
        parses a module that special-cases a tenant the way a hurried change
        would, and requires it to be caught.
        """
        planted = ast.parse(
            '"""A module docstring mentioning acme-payments harmlessly."""\n'
            "def cap(tenant_id: str) -> int:\n"
            '    if tenant_id == "acme-payments":\n'
            "        return 200000\n"
            "    return 0\n"
        )
        skip = self._docstring_nodes(planted)
        strings = [
            node.value
            for node in ast.walk(planted)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
        ]

        assert "acme-payments" in strings, "the guard would not catch a special case"
        assert not any("harmlessly" in s for s in strings), "the guard reads docstrings"
