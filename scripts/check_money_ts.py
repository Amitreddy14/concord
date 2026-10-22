"""Extend the no-floats-near-money rule to the console's TypeScript.

CONTRIBUTING puts the rule on the repository, not on the Python: "floats are
banned in any path that touches an amount; this is enforced by a lint rule,
not by discipline." The console displays and parses the same amounts the
agent refunds, and `amount_minor / 100` is the obvious way to render one —
which is exactly why the rule has to reach here.

This is a narrower check than ``check_money.py``, and the narrowness is
deliberate rather than an omission. That one is an AST scan, because an
earlier regex version over-matched three separate times. There is no
TypeScript parser in this repository's dependencies, and adding one — or a
Node subprocess — to enforce a four-rule policy would be a worse trade than
scoping the rules tightly enough that a line-based scan is honest:

* arithmetic (``/``, ``*``) applied to an identifier named like an amount;
* ``parseFloat`` and ``Number.parseFloat`` anywhere in a money module;
* ``toFixed``, which is how a float is usually laundered back into a string.

What it cannot see is a float that arrives through a variable it cannot name.
That gap is covered differently: ``money.ts`` carries unit tests asserting
every amount is read and rendered by string surgery, and a reviewer changing
it has to change those too.

A line may opt out with ``// allow-float: <reason>``. The reason is mandatory,
for the same reason as in the Python checker: waiving costs a sentence.

Run as ``python -m scripts.check_money_ts`` (part of ``make check``).
"""

from __future__ import annotations

import re
from pathlib import Path

CONSOLE = Path("apps/console/src")
SKIP_DIRS = {"node_modules", ".next", "__pycache__"}

ALLOW_FLOAT = re.compile(r"//\s*allow-float:\s*\S+")

# Identifiers whose arithmetic is almost certainly a money bug. Kept in step
# with AMOUNT_NAMES in check_money.py, in the spellings TypeScript uses.
AMOUNT_NAMES = (
    "amount_minor",
    "amountMinor",
    "amount",
    "refunded_amount",
    "refundedAmount",
    "autonomy_cap_minor",
    "autonomyCapMinor",
    "minor",
)

_NAMES = "|".join(sorted(AMOUNT_NAMES, key=len, reverse=True))

# `x.amount_minor / 100` and `amount * 100`, in either order.
ARITHMETIC = re.compile(rf"\b(?:{_NAMES})\b\s*[/*]|[/*]\s*\b(?:{_NAMES})\b")

RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (ARITHMETIC, "arithmetic on an amount; split the integer instead of dividing it"),
    (
        re.compile(r"\bparseFloat\b|\bNumber\.parseFloat\b"),
        "parseFloat on money; read the digits with parseMoneyInput",
    ),
    (
        re.compile(r"\.toFixed\s*\("),
        "toFixed on money; formatMoney renders from the integer",
    ),
)

# A comment or a string is not code. Stripping them is crude and sufficient:
# the alternative is a parser, and the rules above are narrow enough that a
# false negative inside a string literal costs nothing.
LINE_COMMENT = re.compile(r"//.*$")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
STRING = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")


def _strip(line: str) -> str:
    return STRING.sub("''", LINE_COMMENT.sub("", line))


def _sources(root: Path) -> list[Path]:
    base = root / CONSOLE
    if not base.exists():
        return []
    return sorted(
        path
        for path in base.rglob("*.ts*")
        if not SKIP_DIRS & set(path.parts) and path.suffix in {".ts", ".tsx"}
    )


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    findings: list[str] = []
    waived = 0
    scanned = 0

    for path in _sources(root):
        source = BLOCK_COMMENT.sub("", path.read_text())
        scanned += 1
        for lineno, raw in enumerate(source.splitlines(), start=1):
            code = _strip(raw)
            if not code.strip():
                continue
            for pattern, message in RULES:
                if not pattern.search(code):
                    continue
                if ALLOW_FLOAT.search(raw):
                    waived += 1
                    continue
                findings.append(f"{path.relative_to(root)}:{lineno}: {message}")

    if findings:
        print("Float usage in money-handling TypeScript:")
        for finding in findings:
            print(f"  - {finding}")
        return 1

    suffix = f" ({waived} waived)" if waived else ""
    print(f"{scanned} console modules scanned; no money arithmetic{suffix}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
