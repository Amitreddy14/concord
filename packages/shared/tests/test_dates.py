"""Day rendering, including the cases that make a support reply wrong.

A payout date off by one is not a formatting nit — it is a reply that tells a
merchant the wrong day. The interesting inputs are therefore the ones near a
boundary and the ones with no zone at all.
"""

from __future__ import annotations

import pytest

from packages.shared.dates import format_day, parse_instant


class TestFormatDay:
    def test_renders_unambiguously(self) -> None:
        # Not 10/17 or 17/10: the reader may be anywhere.
        assert format_day("2026-10-17T10:00:00Z") == "17 October 2026"

    def test_the_day_is_not_zero_padded(self) -> None:
        assert format_day("2026-10-01T00:00:00Z") == "1 October 2026"

    def test_late_utc_stays_on_its_utc_day(self) -> None:
        """The case that silently moves a date.

        23:30Z is already tomorrow in Chennai. Rendering in the reader's zone
        would put the same settlement on two different days for two
        merchants, so the platform's business day wins and it is UTC.
        """
        assert format_day("2026-10-17T23:30:00Z") == "17 October 2026"

    def test_an_offset_is_converted_rather_than_ignored(self) -> None:
        # 23:30+05:30 is 18:00Z the same day.
        assert format_day("2026-10-17T23:30:00+05:30") == "17 October 2026"
        # 02:00+05:30 is 20:30Z the *previous* day.
        assert format_day("2026-10-18T02:00:00+05:30") == "17 October 2026"

    def test_both_spellings_of_utc_agree(self) -> None:
        assert format_day("2026-10-17T10:00:00Z") == format_day("2026-10-17T10:00:00+00:00")

    def test_a_timestamp_with_no_zone_is_refused(self) -> None:
        """Rejected, not assumed.

        A settlement time with no zone is a defect in the contract. Guessing
        UTC here would turn it into a wrong date in a merchant's inbox, which
        is worse than failing the run.
        """
        with pytest.raises(ValueError, match="no timezone"):
            format_day("2026-10-17T10:00:00")

    def test_a_date_only_value_is_refused_for_the_same_reason(self) -> None:
        with pytest.raises(ValueError, match="no timezone"):
            format_day("2026-10-17")


class TestParseInstant:
    def test_normalises_to_utc(self) -> None:
        assert parse_instant("2026-10-18T02:00:00+05:30").hour == 20

    def test_rejects_nonsense(self) -> None:
        with pytest.raises(ValueError):
            parse_instant("not a date")
