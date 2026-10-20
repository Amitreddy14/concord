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

Run as ``python -m scripts.check_money`` (part of ``make check``).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache", "__pycache__"}

# A waiver must carry a reason, so the regex requires at least one word after the colon.
ALLOW_FLOAT = re.compile(r"#\s*allow-float:\s*\S+")

# A module is money-touching if it says so by name, or if it mentions the
# domain's amount vocabulary anywhere in its source.
MONEY_TOKENS = (
    "amount_minor",
    "Money",
    "refund",
    "capture",
    "settlement",
    "gross_amount",
    "net_amount",
    "fees",
)

# Identifiers whose division is almost certainly a money bug.
AMOUNT_NAMES = {
    "amount",
    "amount_minor",
    "gross_amount",
    "net_amount",
    "fees",
    "refunded_amount",
    "total",
    "subtotal",
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


def is_money_module(path: Path, source: str) -> bool:
    if "money" in path.name:
        return True
    return any(token in source for token in MONEY_TOKENS)


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
        if not is_money_module(path, source):
            continue
        scanned += 1
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as error:
            findings.append(f"{path.relative_to(root)}:{error.lineno}: does not parse")
            continue
        visitor = MoneyVisitor(path)
        visitor.visit(tree)

        lines = source.splitlines()
        for lineno, message in visitor.findings:
            line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
            if ALLOW_FLOAT.search(line):
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
