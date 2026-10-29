"""Rendering a timestamp for a merchant, deterministically.

Money reaches the drafting prompt through ``Money.format()``; dates reached
it as whatever the system of record stored — ``2026-10-17T10:00:00Z``. The
model was therefore the thing turning a timestamp into a sentence, which is a
computation, and ADR 0004 says the system does those.

Two concrete failures came out of that asymmetry. A benchmark assertion
looking for the date the system holds could not find it, because the model
had rendered it some other way; and nothing anywhere decided which day
``2026-10-17T23:30:00Z`` falls on for a merchant reading in Chennai. The
second is the one that matters outside a test: a payout date off by a day is
a support reply that is wrong.

So the zone is explicit and it is UTC. Not because UTC is what a merchant
wants to read, but because a settlement date is the platform's own business
day — the day the money movement is booked — and booking it in the reader's
local zone would make the same settlement fall on two different dates for
two merchants. When per-tenant business zones arrive, that is a tenant
setting threaded through here, not a default anyone has to remember.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["PROMPT_DAY_FORMAT", "format_day", "parse_instant"]

# Long month, unpadded day, four-digit year: "17 October 2026". Unambiguous
# in a way 10/17 and 17/10 are not, which matters because the reader is a
# merchant who may be anywhere.
#
# Built from the parts rather than with a format string: the unpadded-day
# directive is `%-d` on glibc and `%#d` on Windows, and a date format that
# depends on the host libc is a defect waiting for whoever runs this
# somewhere other than WSL.
PROMPT_DAY_FORMAT = "<day> <Month> <year>"


def parse_instant(value: str) -> datetime:
    """Parse an ISO 8601 instant from a system of record, as UTC.

    A naive timestamp is rejected rather than assumed: a settlement time with
    no zone is a defect in the contract, and guessing one here would turn it
    into a wrong date in a merchant's inbox.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no timezone, so its day is undefined: {value!r}")
    return parsed.astimezone(UTC)


def format_day(value: str) -> str:
    """Render the UTC calendar day of an instant, for a prompt or a reply.

    Only ever called at a boundary, never in logic — the same rule as
    ``Money.format``. Logic compares instants; prose shows days.
    """
    instant = parse_instant(value)
    return f"{instant.day} {instant.strftime('%B')} {instant.year}"
