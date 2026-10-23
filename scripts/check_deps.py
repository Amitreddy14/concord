"""Every third-party import must be a declared dependency.

This exists because it was needed. `langgraph` was installed by hand during
development and never added to ``pyproject.toml``, so the project imported a
package it did not declare. Everything passed locally — the developer's
environment had it — and the first person to install from the manifest got a
``ModuleNotFoundError`` from a module they had not touched.

CI would have caught it on the next push, but only as an opaque pytest
collection error. This says which package is missing and which file imports
it, which is the difference between a two-minute fix and a confusing one.

Run as ``python -m scripts.check_deps`` (part of ``make check``).
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache", "__pycache__"}

# First-party top-level packages, which are not dependencies.
FIRST_PARTY = {"apps", "packages", "evals", "scripts"}

# Import name to distribution name, where they differ.
#
# `opentelemetry` is a namespace package split across several distributions —
# the API, the SDK and each exporter — and nothing is importable as
# `opentelemetry_api`. Mapping it to the API is the honest minimum: that is
# the one every `from opentelemetry import ...` needs, and the SDK and
# exporter are imported under longer paths that resolve through it.
IMPORT_TO_DISTRIBUTION = {
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "jose": "python-jose",
    "sqlalchemy": "sqlalchemy",
    "opentelemetry": "opentelemetry-api",
}

REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def declared(pyproject: dict[str, object]) -> set[str]:
    project = pyproject.get("project", {})
    assert isinstance(project, dict)

    requirements: list[str] = list(project.get("dependencies", []))
    for extra in (project.get("optional-dependencies") or {}).values():
        requirements.extend(extra)

    names = set()
    for requirement in requirements:
        match = REQUIREMENT_NAME.match(requirement.strip())
        if match:
            names.add(match.group(1).lower().replace("_", "-"))
    return names


def imported(root: Path) -> dict[str, set[Path]]:
    """Top-level third-party modules imported by first-party code."""
    found: dict[str, set[Path]] = {}

    for path in sorted(root.rglob("*.py")):
        if SKIP_DIRS.intersection(path.parts):
            continue
        # Relative first: rglob yields absolute paths, so parts[0] is "/" and
        # the first-party test would skip every file, passing vacuously.
        relative = path.relative_to(root)
        if relative.parts[0] not in FIRST_PARTY:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # A relative import has no module to resolve against PyPI.
                modules = [node.module] if node.module and node.level == 0 else []
            else:
                continue

            for module in modules:
                top = module.split(".")[0]
                if top in FIRST_PARTY or top in sys.stdlib_module_names:
                    continue
                found.setdefault(top, set()).add(relative)

    return found


def main() -> int:
    root = Path.cwd()
    manifest = root / "pyproject.toml"
    if not manifest.is_file():
        print("no pyproject.toml in the working directory", file=sys.stderr)
        return 2

    with manifest.open("rb") as handle:
        pyproject = tomllib.load(handle)

    declared_names = declared(pyproject)
    undeclared: list[str] = []

    for module, files in sorted(imported(root).items()):
        distribution = IMPORT_TO_DISTRIBUTION.get(module, module).lower().replace("_", "-")
        if distribution in declared_names:
            continue
        where = ", ".join(str(f) for f in sorted(files)[:3])
        more = f" (+{len(files) - 3} more)" if len(files) > 3 else ""
        undeclared.append(f"  - {module!r} imported by {where}{more}")

    if undeclared:
        print("Third-party imports that pyproject.toml does not declare:")
        print("\n".join(undeclared))
        print("\nAdd them to [project.dependencies] or an extra, or the next clean")
        print("install fails on a module nobody touched.")
        return 1

    print(f"{len(declared_names)} declared dependencies cover every third-party import.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
