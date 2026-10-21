"""Tests for Money.

The allocation tests are the ones that matter. Everything else guards against
a careless edit; those guard against a class of bug that reconciliation finds
months later, when a refund is one paisa adrift from the capture it reversed.
"""

from __future__ import annotations

import pytest

from packages.shared.money import CurrencyMismatch, Money, MoneyError


class TestConstruction:
    def test_rejects_a_float_even_when_integral(self) -> None:
        with pytest.raises(MoneyError, match="never constructed from a float"):
            Money(100.0, "INR")  # type: ignore[arg-type]  # allow-float: asserts rejection

    def test_rejects_a_bool_masquerading_as_an_int(self) -> None:
        with pytest.raises(MoneyError):
            Money(True, "INR")  # type: ignore[arg-type]

    def test_rejects_an_unconfigured_currency(self) -> None:
        with pytest.raises(MoneyError, match="not configured"):
            Money(100, "XYZ")

    def test_rejects_a_malformed_currency_code(self) -> None:
        with pytest.raises(MoneyError, match="ISO 4217"):
            Money(100, "inr")

    def test_from_payload_rejects_a_float_amount(self) -> None:
        with pytest.raises(MoneyError, match="arrived as a float"):
            Money.from_payload(
                {"amount_minor": 2450.0, "currency": "INR"}
            )  # allow-float: asserts rejection

    def test_payload_round_trip(self) -> None:
        original = Money(245000, "INR")
        assert Money.from_payload(original.to_payload()) == original


class TestArithmetic:
    def test_addition(self) -> None:
        assert Money(245000, "INR") + Money(89000, "INR") == Money(334000, "INR")

    def test_mixing_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatch):
            Money(100, "INR") + Money(100, "USD")

    def test_subtraction_below_zero_raises(self) -> None:
        # A negative capture does not exist in this domain, so producing one
        # means the caller has its operands the wrong way round.
        with pytest.raises(MoneyError, match="negative"):
            Money(100, "INR") - Money(101, "INR")

    def test_comparison_requires_matching_currency(self) -> None:
        with pytest.raises(CurrencyMismatch):
            _ = Money(100, "INR") < Money(100, "USD")

    def test_multiplication_by_a_float_raises(self) -> None:
        with pytest.raises(MoneyError, match="only be multiplied by an int"):
            Money(100, "INR") * 1.5  # type: ignore[operator]  # allow-float: asserts rejection


class TestAllocation:
    def test_even_split_loses_nothing(self) -> None:
        parts = Money(1000, "INR").allocate(3)
        assert [p.amount_minor for p in parts] == [334, 333, 333]
        assert sum(p.amount_minor for p in parts) == 1000

    @pytest.mark.parametrize("total", [1, 2, 7, 99, 100, 245000, 4120000])
    @pytest.mark.parametrize("parts", [1, 2, 3, 7, 13])
    def test_allocation_always_sums_to_the_original(self, total: int, parts: int) -> None:
        pieces = Money(total, "INR").allocate(parts)
        assert sum(p.amount_minor for p in pieces) == total
        assert len(pieces) == parts

    def test_weighted_split_sums_to_the_original(self) -> None:
        pieces = Money(1001, "INR").allocate_by([3, 1])
        assert [p.amount_minor for p in pieces] == [751, 250]
        assert sum(p.amount_minor for p in pieces) == 1001

    @pytest.mark.parametrize(
        "total,weights",
        [
            (100, [1, 1, 1]),
            (245000, [245000, 245000]),
            (7, [5, 3, 1]),
            (1, [1, 1]),
            (4120000, [1, 2, 3, 4]),
        ],
    )
    def test_weighted_allocation_is_exact(self, total: int, weights: list[int]) -> None:
        pieces = Money(total, "INR").allocate_by(weights)
        assert sum(p.amount_minor for p in pieces) == total

    def test_weighted_allocation_is_deterministic(self) -> None:
        # Replay depends on this: the same split must come out the same way
        # every time, or a replayed run diverges from the original.
        first = Money(1000, "INR").allocate_by([1, 1, 1])
        second = Money(1000, "INR").allocate_by([1, 1, 1])
        assert first == second

    def test_zero_weights_raise(self) -> None:
        with pytest.raises(MoneyError, match="sum to zero"):
            Money(100, "INR").allocate_by([0, 0])


class TestPresentation:
    def test_two_exponent_currency(self) -> None:
        assert Money(4120000, "INR").format() == "INR 41,200.00"

    def test_sub_rupee_amount(self) -> None:
        assert Money(7, "INR").format() == "INR 0.07"

    def test_zero_exponent_currency_has_no_decimal_part(self) -> None:
        # JPY has no minor unit; rendering 5000 as "50.00" would be wrong by
        # two orders of magnitude.
        assert Money(5000, "JPY").format() == "JPY 5,000"
