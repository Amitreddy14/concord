"""Money as integer minor units with an ISO 4217 currency code.

Floats never appear in this module and never should in any caller. The reason
is not fastidiousness: ``0.1 + 0.2`` is not ``0.3`` in binary floating point,
and a refund that is one paisa adrift from the capture it reverses is a defect
that reconciliation will find months later. ``scripts/check_money.py`` enforces
the prohibition across the tree, so the rule in CONTRIBUTING is checkable
rather than aspirational.

The two operations worth reading before use are :meth:`Money.allocate`, which
distributes a remainder deterministically rather than losing it to rounding,
and :meth:`Money.__sub__`, which refuses to produce a negative amount because
there is no such thing as a negative capture in this domain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Self

CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")

# Currencies this deployment handles, with the exponent needed to render minor
# units. Deliberately a small explicit table: guessing an exponent from a
# currency code is how a JPY amount ends up a hundred times too large.
MINOR_UNIT_EXPONENT: dict[str, int] = {
    "INR": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "AED": 2,
    "SGD": 2,
    "JPY": 0,
}


class MoneyError(ValueError):
    """Raised when an operation on money is not meaningful."""


class CurrencyMismatch(MoneyError):
    """Raised when two amounts in different currencies are combined."""

    def __init__(self, left: str, right: str) -> None:
        super().__init__(f"cannot combine {left} and {right}")
        self.left = left
        self.right = right


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An exact amount in the minor units of one currency.

    ``Money(245000, "INR")`` is ₹2,450.00. The amount is always an ``int``;
    constructing one from a float raises rather than rounding silently.
    """

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise MoneyError(
                f"amount_minor must be an int, got {type(self.amount_minor).__name__}; "
                "money is never constructed from a float"
            )
        if not isinstance(self.currency, str) or not CURRENCY_CODE.match(self.currency):
            raise MoneyError(f"currency must be an ISO 4217 alphabetic code, got {self.currency!r}")
        if self.currency not in MINOR_UNIT_EXPONENT:
            raise MoneyError(f"currency {self.currency} is not configured for this deployment")

    # -- construction ----------------------------------------------------

    @classmethod
    def zero(cls, currency: str) -> Self:
        return cls(0, currency)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        """Build from the contract's Money object.

        Rejects a float even when it happens to be integral, because a float in
        a payload means some upstream caller is doing arithmetic we cannot see.
        """
        try:
            amount = payload["amount_minor"]
            currency = payload["currency"]
        except (KeyError, TypeError) as error:
            raise MoneyError(f"not a money payload: {payload!r}") from error
        if isinstance(amount, float):
            raise MoneyError(f"amount_minor arrived as a float: {amount!r}")
        return cls(int(amount), str(currency))

    def to_payload(self) -> dict[str, int | str]:
        return {"amount_minor": self.amount_minor, "currency": self.currency}

    # -- arithmetic ------------------------------------------------------

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(self.currency, other.currency)

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        result = self.amount_minor - other.amount_minor
        if result < 0:
            raise MoneyError(
                f"subtraction would produce a negative amount: {self} - {other}. "
                "No amount in this domain is negative; check the caller's ordering."
            )
        return Money(result, self.currency)

    def __mul__(self, factor: int) -> Money:
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise MoneyError("money may only be multiplied by an int")
        if factor < 0:
            raise MoneyError("money may not be multiplied by a negative number")
        return Money(self.amount_minor * factor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor < other.amount_minor

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor <= other.amount_minor

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor > other.amount_minor

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor >= other.amount_minor

    # -- allocation ------------------------------------------------------

    def allocate(self, parts: int) -> list[Money]:
        """Split into ``parts`` amounts that sum exactly to this one.

        ₹10.00 across 3 ways is 334 + 333 + 333 paise, not three times 333.33.
        The remainder goes to the earliest parts, which is arbitrary but
        deterministic, and determinism is the property that matters when the
        same split has to be reproduced during a replay.
        """
        if parts < 1:
            raise MoneyError("parts must be at least 1")
        base, remainder = divmod(self.amount_minor, parts)
        return [Money(base + (1 if i < remainder else 0), self.currency) for i in range(parts)]

    def allocate_by(self, weights: list[int]) -> list[Money]:
        """Split in proportion to integer weights, summing exactly to this one.

        Used where a refund spans several captures of unequal size. Largest
        remainder takes the leftover minor units, so the result does not drift
        toward whichever part happens to be first.
        """
        if not weights or any(w < 0 for w in weights):
            raise MoneyError("weights must be a non-empty list of non-negative ints")
        total_weight = sum(weights)
        if total_weight == 0:
            raise MoneyError("weights must not sum to zero")

        shares = [self.amount_minor * w // total_weight for w in weights]
        remainder = self.amount_minor - sum(shares)

        # Rank by the fractional part that was truncated, descending.
        fractions = sorted(
            range(len(weights)),
            key=lambda i: (-(self.amount_minor * weights[i] % total_weight), i),
        )
        for i in fractions[:remainder]:
            shares[i] += 1
        return [Money(s, self.currency) for s in shares]

    # -- presentation ----------------------------------------------------

    def format(self) -> str:
        """Render for display. Only ever called at a boundary, never in logic."""
        exponent = MINOR_UNIT_EXPONENT[self.currency]
        if exponent == 0:
            return f"{self.currency} {self.amount_minor:,}"
        units, minor = divmod(abs(self.amount_minor), 10**exponent)
        return f"{self.currency} {units:,}.{minor:0{exponent}d}"

    def __str__(self) -> str:
        return self.format()

    def __repr__(self) -> str:
        return f"Money({self.amount_minor}, {self.currency!r})"
