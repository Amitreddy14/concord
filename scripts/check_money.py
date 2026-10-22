"""Enforce the prohibition on floats in any code path that touches an amount.

CONTRIBUTING states that floats are banned where money is involved and that the
rule is enforced rather than left to discipline. This is that enforcement.

The check is an AST scan, not a grep, so it does not fire on the word "float"
in a comment and does not miss ``1e-3``. It flags, inside any module whose name
or contents indicate money handling:

* a float literal;
* a call to ``float()``;
* ``/`` applied to anything named like an amount (use ``Money.allocate``);
* ``round()`` on an amount, which is the usual way a float sneaks back in.

A line may opt out with a trailing ``# allow-float: <reason>``. The reason is
mandatory and is not checked for sense — the point is that waiving the rule
costs a sentence, so it happens where a test asserts a float is rejected and
nowhere else.

The waiver is honoured anywhere within the statement the finding belongs to,
not only on the exact line. A formatter is free to split an expression across
lines, and it will put a trailing comment on the closing bracket rather than
beside the literal; requiring an exact line match would mean `make lint` and
`make money` could not both pass on the same file.

Run as ``python -m scripts.check_money`` (part of ``make check``).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache", "__pycache__"}

# A waiver must carry a reason, so the regex requires at least one word after the colon.
ALLOW_FLOAT = re.compile(r"#\s*allow-float:\s*\S+")

# Modules that define money handling by name, regardless of contents.
MONEY_FILENAMES = ("money",)

# Importing this module is the strongest signal a file handles amounts.
MONEY_MODULE = "packages.shared.money"

# Identifiers whose division is almost certainly a money bug.
#
# Deliberately specific. `total` and `subtotal` were here and pulled in the
# retry module, whose only `total` counts milliseconds — and a checker that
# flags code handling no money is a checker that gets switched off. The
# domain's amount fields are named precisely enough that the generic words
# cost more in false positives than they earn.
AMOUNT_NAMES = {
    "amount",
    "amount_minor",
    "gross_amount",
    "net_amount",
    "fees",
    "refunded_amount",
    "refunds_deducted",
    "autonomy_cap_minor",
}


class MoneyVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: list[tuple[int, str]] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, float):
            self.findings.append(
                (node.lineno, f"float literal {node.value!r}; amounts are integer minor units")
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if node.func.id == "float":
                self.findings.append((node.lineno, "float() call in a money-handling module"))
            elif node.func.id == "round" and self._mentions_amount(node.args):
                self.findings.append(
                    (node.lineno, "round() on an amount; use Money.allocate to split exactly")
                )
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div) and self._mentions_amount([node.left]):
            self.findings.append(
                (
                    node.lineno,
                    "true division on an amount; use // with an explicit remainder, "
                    "or Money.allocate",
                )
            )
        self.generic_visit(node)

    @staticmethod
    def _mentions_amount(nodes: list[ast.expr]) -> bool:
        for node in nodes:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id in AMOUNT_NAMES:
                    return True
                if isinstance(sub, ast.Attribute) and sub.attr in AMOUNT_NAMES:
                    return True
        return False


def _waiver_ranges(tree: ast.Module, lines: list[str]) -> list[tuple[int, int]]:
    """Line spans of statements whose last line carries a waiver.

    A formatter puts a trailing comment on the closing bracket, so a finding
    on the literal inside a split expression needs the waiver on the
    statement, not on its own line.
    """
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        end = getattr(node, "end_lineno", None)
        if end is None:
            continue
        for lineno in range(node.lineno, end + 1):
            if 0 < lineno <= len(lines) and ALLOW_FLOAT.search(lines[lineno - 1]):
                spans.append((node.lineno, end))
                break
    return spans


def _is_waived(lineno: int, lines: list[str], spans: list[tuple[int, int]]) -> bool:
    line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
    if ALLOW_FLOAT.search(line):
        return True
    return any(start <= lineno <= end for start, end in spans)


def is_money_module(path: Path, tree: ast.Module) -> bool:
    """Whether this module actually handles amounts.

    Decided from the syntax tree, not from a substring search over the source.
    An earlier version matched the word "refund" anywhere in a file, which
    swept in the knowledge service — whose only floats are relevance scores,
    not money — and would have forced three dishonest waivers onto code that
    handles no amounts at all. A rule that cries wolf gets switched off.

    The signals are: the filename says money; the module imports the Money
    type; or the module names one of the amount fields from the domain model.
    """
    if any(token in path.name for token in MONEY_FILENAMES):
        return True

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == MONEY_MODULE:
            return True
        if isinstance(node, ast.Import) and any(a.name == MONEY_MODULE for a in node.names):
            return True
        if isinstance(node, ast.Name) and node.id in AMOUNT_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in AMOUNT_NAMES:
            return True
        # A dict literal key such as {"amount_minor": 1} is how money is
        # written in a payload, and is not a Name or an Attribute.
        if isinstance(node, ast.Constant) and node.value in AMOUNT_NAMES:
            return True
    return False


def main() -> int:
    root = Path.cwd()
    findings: list[str] = []
    scanned = 0
    waived = 0

    for path in sorted(root.rglob("*.py")):
        if SKIP_DIRS.intersection(path.parts):
            continue
        # This file names the thing it forbids, by necessity.
        if path.name == "check_money.py":
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as error:
            findings.append(f"{path.relative_to(root)}:{error.lineno}: does not parse")
            continue
        if not is_money_module(path, tree):
            continue
        scanned += 1
        visitor = MoneyVisitor(path)
        visitor.visit(tree)

        lines = source.splitlines()
        waiver_ranges = _waiver_ranges(tree, lines)
        for lineno, message in visitor.findings:
            if _is_waived(lineno, lines, waiver_ranges):
                waived += 1
                continue
            findings.append(f"{path.relative_to(root)}:{lineno}: {message}")

    if findings:
        print("Float usage in money-handling code:")
        for finding in findings:
            print(f"  - {finding}")
        return 1

    suffix = f" ({waived} waived)" if waived else ""
    print(f"{scanned} money-handling modules scanned; no floats{suffix}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
