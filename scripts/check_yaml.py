"""Parse every YAML document in the tree and report the ones that do not load.

The contract validator covers ``contracts/`` and the golden fixtures in depth.
This is the cheap backstop for everything else — tenant configuration, the
compose stack, CI workflows — where a mis-indented line would otherwise only
surface when a container refuses to start.

Run as ``python -m scripts.check_yaml`` (part of ``make check``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache"}


def yaml_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*.y*ml")):
        if SKIP_DIRS.intersection(path.parts):
            continue
        if path.suffix not in {".yaml", ".yml"}:
            continue
        found.append(path)
    return found


def main() -> int:
    root = Path.cwd()
    paths = yaml_files(root)

    if not paths:
        print("no YAML documents found", file=sys.stderr)
        return 2

    failures: list[str] = []
    for path in paths:
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            first_line = str(error).splitlines()[0]
            failures.append(f"{path.relative_to(root)}: {first_line}")
        except UnicodeDecodeError:
            failures.append(f"{path.relative_to(root)}: not valid UTF-8")

    if failures:
        print("YAML documents that do not parse:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"{len(paths)} YAML documents parse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
