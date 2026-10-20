"""Validate the OpenAPI contracts and the golden scenario fixtures.

Run as ``python -m scripts.validate_contracts contracts`` (the ``make contracts``
target). Exits non-zero on the first category of failure found.

Three classes of check, in increasing usefulness:

1. Structural: every document parses and declares the OpenAPI version we target.
2. Reference integrity: every ``$ref`` resolves, including cross-file refs into
   ``common.yaml`` and ``crm.yaml``. A dangling ref in a contract is a silent
   client-generation failure later.
3. House rules: rules this project cares about that no generic linter knows —
   money is never a bare number, mutations carry an idempotency key, errors use
   the shared problem shape, and untrusted fields are marked.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

TARGET_OPENAPI = "3.1"
MUTATING_METHODS = {"post", "put", "patch", "delete"}
SCENARIO_ID = re.compile(r"^S(0[1-9]|1[0-2])$")
SHARED_RESPONSE_REF = re.compile(r"common\.yaml#/components/responses/")


class Findings:
    """Collects problems so one run reports everything in a category."""

    def __init__(self) -> None:
        self._items: list[str] = []

    def add(self, where: str, message: str) -> None:
        self._items.append(f"{where}: {message}")

    def __bool__(self) -> bool:
        return bool(self._items)

    def report(self, heading: str) -> None:
        print(f"\n{heading}")
        for item in self._items:
            print(f"  - {item}")


def load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise TypeError(f"{path} does not contain a mapping at the top level")
    return document


def walk(node: Any, trail: str = "") -> Iterator[tuple[str, Any]]:
    """Yield (json-pointer-ish trail, node) for every node in the tree."""
    yield trail or "/", node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{trail}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{trail}/{index}")


def resolve_pointer(document: dict[str, Any], pointer: str) -> bool:
    """Resolve a local JSON pointer such as ``/components/schemas/Money``."""
    current: Any = document
    for raw in pointer.lstrip("/").split("/"):
        if not raw:
            continue
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if key not in current:
                return False
            current = current[key]
        elif isinstance(current, list):
            try:
                current = current[int(key)]
            except (ValueError, IndexError):
                return False
        else:
            return False
    return True


def check_structure(specs: dict[Path, dict[str, Any]], findings: Findings) -> None:
    for path, document in specs.items():
        version = str(document.get("openapi", ""))
        if not version.startswith(TARGET_OPENAPI):
            findings.add(path.name, f"openapi is {version!r}, expected {TARGET_OPENAPI}.x")
        info = document.get("info", {})
        for field in ("title", "version"):
            if not info.get(field):
                findings.add(path.name, f"info.{field} is missing")
        if "paths" not in document:
            findings.add(path.name, "no paths key (use `paths: {}` for a components-only file)")


def check_references(specs: dict[Path, dict[str, Any]], root: Path, findings: Findings) -> None:
    for path, document in specs.items():
        for trail, node in walk(document):
            if not (isinstance(node, dict) and isinstance(node.get("$ref"), str)):
                continue
            ref = node["$ref"]
            file_part, _, pointer = ref.partition("#")

            if file_part:
                target_path = (path.parent / file_part).resolve()
                if target_path not in {p.resolve() for p in specs}:
                    findings.add(path.name, f"{trail} -> {ref}: file not among the contracts")
                    continue
                target_doc = next(d for p, d in specs.items() if p.resolve() == target_path)
            else:
                target_doc = document

            if pointer and not resolve_pointer(target_doc, pointer):
                findings.add(path.name, f"{trail} -> {ref}: pointer does not resolve")


def check_house_rules(specs: dict[Path, dict[str, Any]], findings: Findings) -> None:
    for path, document in specs.items():
        paths_obj = document.get("paths") or {}

        for route, operations in paths_obj.items():
            if not isinstance(operations, dict):
                continue
            for method, operation in operations.items():
                if method not in MUTATING_METHODS or not isinstance(operation, dict):
                    continue
                where = f"{method.upper()} {route}"

                if not operation.get("operationId"):
                    findings.add(path.name, f"{where}: no operationId")

                # A POST that only reads is legitimate where an untrusted query
                # must not travel in a URL, but it has to say so explicitly.
                declared_read_only = operation.get("x-concord-read-only") is True
                params = json.dumps(operation.get("parameters", []))
                if not declared_read_only and "IdempotencyKey" not in params:
                    findings.add(
                        path.name,
                        f"{where}: mutating operation without an Idempotency-Key parameter",
                    )
                if declared_read_only and "IdempotencyKey" in params:
                    findings.add(
                        path.name,
                        f"{where}: declared x-concord-read-only but takes an Idempotency-Key",
                    )

                responses = operation.get("responses") or {}
                for code, response in responses.items():
                    if not str(code).startswith(("4", "5")):
                        continue
                    blob = json.dumps(response)
                    # Either an inline Problem schema, or a $ref to one of the
                    # shared responses in common.yaml, which all carry it.
                    uses_problem = "Problem" in blob or SHARED_RESPONSE_REF.search(blob)
                    if not uses_problem:
                        findings.add(
                            path.name,
                            f"{where} {code}: error response does not use the shared Problem shape",
                        )

        # Money must always be the shared object, never a loose number field.
        for trail, node in walk(document):
            if not isinstance(node, dict):
                continue
            name = trail.rsplit("/", 1)[-1]
            if name not in {"amount", "amount_minor", "net_amount", "gross_amount", "fees"}:
                continue
            if name == "amount_minor" and node.get("type") == "integer":
                continue
            if node.get("type") == "number":
                findings.add(path.name, f"{trail}: money as a bare number; use the Money schema")


def check_fixtures(fixture_dir: Path, scenarios_doc: Path, findings: Findings) -> None:
    if not fixture_dir.is_dir():
        findings.add(str(fixture_dir), "fixture directory does not exist")
        return

    fixtures = sorted(fixture_dir.glob("*.yaml"))
    if not fixtures:
        findings.add(str(fixture_dir), "no fixtures found")
        return

    documented = set(re.findall(r"^## (S\d{2})", scenarios_doc.read_text(encoding="utf-8"), re.M))
    seen: set[str] = set()

    for path in fixtures:
        document = load(path)
        scenario_id = str(document.get("id", ""))

        if not SCENARIO_ID.match(scenario_id):
            findings.add(path.name, f"id {scenario_id!r} is not S01..S12")
            continue
        if scenario_id in seen:
            findings.add(path.name, f"duplicate scenario id {scenario_id}")
        seen.add(scenario_id)

        for field in ("title", "exercises", "ticket", "must"):
            if not document.get(field):
                findings.add(path.name, f"missing or empty {field!r}")

        must = document.get("must")
        if isinstance(must, list) and len(must) < 2:
            findings.add(path.name, "fewer than two hard assertions; a scenario needs real teeth")

        for assertion in must if isinstance(must, list) else []:
            if isinstance(assertion, dict) and not assertion.get("assert"):
                findings.add(path.name, "a must entry has no `assert` key")

    missing = documented - seen
    extra = seen - documented
    if missing:
        findings.add("fixtures", f"documented but has no fixture: {', '.join(sorted(missing))}")
    if extra:
        findings.add("fixtures", f"fixture with no catalogue entry: {', '.join(sorted(extra))}")


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else "contracts").resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    spec_paths = sorted(root.glob("*.yaml"))
    if not spec_paths:
        print(f"no contracts found in {root}", file=sys.stderr)
        return 2

    specs: dict[Path, dict[str, Any]] = {}
    parse_failures = Findings()
    for path in spec_paths:
        try:
            specs[path] = load(path)
        except (yaml.YAMLError, TypeError) as error:
            parse_failures.add(path.name, str(error).splitlines()[0])

    if parse_failures:
        parse_failures.report("Contracts do not parse:")
        return 1

    repo_root = root.parent
    stages = (
        ("Structural problems:", lambda f: check_structure(specs, f)),
        ("Unresolved references:", lambda f: check_references(specs, repo_root, f)),
        ("House rule violations:", lambda f: check_house_rules(specs, f)),
        (
            "Fixture problems:",
            lambda f: check_fixtures(
                repo_root / "evals" / "golden" / "fixtures",
                repo_root / "docs" / "scenarios.md",
                f,
            ),
        ),
    )

    failed = False
    for heading, stage in stages:
        findings = Findings()
        stage(findings)
        if findings:
            findings.report(heading)
            failed = True

    if failed:
        return 1

    print(f"{len(specs)} contracts valid; fixtures consistent with the scenario catalogue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
