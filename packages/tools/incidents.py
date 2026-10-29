"""Which incident statuses are an acceptable explanation, and which are not.

An incident is the most tempting thing in the whole corpus to blame. It is a
real, specific, externally-caused reason that absolves everyone in the
conversation, and a reply citing one reads as a good answer whether or not it
is true. Blaming an incident that has closed is therefore not a small error:
it is a fluent, reassuring falsehood, and it is the shape of answer the eval
suite exists to catch.

So the classification lives here, once, rather than being re-decided by every
caller. It was previously re-decided twice and the two did not agree: the
facts block labelled every incident in state "active incident" without
reading its status, while the harness check accepted `{open, active,
investigating}` — two statuses the contract does not define, and missing two
it does. A reply correctly citing an `identified` incident failed an
assertion that was wrong.

The split is exhaustive over the contract's enum and a totality test holds it
that way. A status that is neither classified here nor terminal fails that
test, which is the point: a new incident status must be classified by a
person, and the unsafe default is the one where it quietly becomes a reason
to blame something that is over.
"""

from __future__ import annotations

__all__ = ["ACTIVE_STATUSES", "TERMINAL_STATUSES", "is_active"]

#: Still happening, so still a legitimate explanation for a delay.
ACTIVE_STATUSES = frozenset({"investigating", "identified", "monitoring"})

#: Over. Must never be offered to a merchant as the reason for anything.
TERMINAL_STATUSES = frozenset({"resolved"})


def is_active(incident: dict[str, object]) -> bool:
    """Whether this incident may be cited as a current explanation.

    Unknown statuses are not active. An incident whose status this module has
    never heard of might be open, but "might be" is not a basis for telling a
    merchant why their money is late.
    """
    return str(incident.get("status", "")) in ACTIVE_STATUSES
