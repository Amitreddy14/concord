"""The drafting prompt must be answerable.

Two benchmark failures in a row came from a prompt that was correct about
*what* to do and silent about *how*. The triage prompt named seven intents it
never defined; the drafting prompt required a citation for every claim and
never said that the ids go in a field, where to find them, or what happens to
a reply without them. Both models then did the same wrong thing, which is the
signature of an underspecified instruction rather than a weak model.

These tests do not check wording. They check that the contract and the facts
block agree: every field the drafter must populate is named, every section the
contract sends the model to exists, and an id copied from where the contract
says it will be is an id grounding accepts. A prompt can be badly phrased and
still pass these; it cannot be unanswerable and pass them.
"""

from __future__ import annotations

from packages.agent import grounding
from packages.agent.nodes import (
    ACTIONS_LABEL,
    DRAFT_CONTRACT,
    PASSAGES_LABEL,
    action_citations,
    draft_facts,
)
from packages.agent.state import AgentState, DraftResult

# A valid money payload: the contract's key is `amount_minor`. Written as
# `minor` here until the conformance suite caught it, which meant
# `action_facts` was taking its "amount could not be read" fallback and
# these tests were passing without ever rendering an amount.
REFUND = {"refund_id": "re_1", "amount": {"amount_minor": 500, "currency": "USD"}}

PASSAGE = {
    "passage_id": "kb:payouts:hold-review-window",
    "title": "Payout holds",
    "text": "A hold placed for review is released within three business days.",
}


def _state(**over: object) -> AgentState:
    base: dict[str, object] = {
        "run_id": "run-1",
        "merchant": {"display_name": "Blue Bottle Supply", "tier": "standard"},
        "passages": [PASSAGE],
    }
    base.update(over)
    return base  # type: ignore[return-value]


def _ids_under(facts: str, label: str) -> list[str]:
    """The ids a model following the contract would copy out of one section.

    Reads them the way the contract describes them — the first token of each
    line, up to the colon — rather than from the state that produced them, so
    this measures what the prompt actually offers.
    """
    lines = facts.splitlines()
    if label not in lines:
        return []
    ids: list[str] = []
    for line in lines[lines.index(label) + 1 :]:
        if not line.strip() or (line.startswith("[") and line.endswith("]")):
            break
        if line.startswith(" "):  # a continuation line, not an id of its own
            continue
        # Colon-space, not colon: every id in this system is itself colon
        # delimited (`kb:payouts:…`, `run:refund:…`), so splitting on the
        # first colon returns a namespace prefix rather than an id.
        head, sep, _ = line.partition(": ")
        if sep:
            ids.append(head)
    return ids


def test_contract_names_every_field_the_drafter_must_fill() -> None:
    """Enumerated, not sampled: a new field is a new thing to explain.

    `citations` existed and went unmentioned for eight phases. The next field
    added to DraftResult should not be able to repeat that quietly.
    """
    unexplained = [name for name in DraftResult.model_fields if name not in DRAFT_CONTRACT]
    assert not unexplained, (
        f"DRAFT_CONTRACT never mentions {unexplained}, so the model is being asked "
        "to populate a field it was never told about"
    )


def test_every_section_the_contract_cites_is_a_section_the_prompt_emits() -> None:
    facts = draft_facts(_state(refund=REFUND))
    for label in (PASSAGES_LABEL, ACTIONS_LABEL):
        assert label in DRAFT_CONTRACT, f"the contract does not name {label}"
        assert label in facts, (
            f"the contract sends the model to {label} for ids, but the facts block "
            "does not emit that section"
        )


def test_a_passage_id_sits_where_the_contract_says_it_sits() -> None:
    """The contract promises the id is 'at the start of a line'. Check that."""
    facts = draft_facts(_state())
    assert _ids_under(facts, PASSAGES_LABEL) == [PASSAGE["passage_id"]]


def test_an_action_id_is_citable_and_grounding_agrees() -> None:
    """The actions section is citable, and the id offered is the id accepted.

    This is the pair that matters: what the prompt hands the model, and what
    `verify` will take. If they diverge, a drafter does exactly as told and the
    reply is still refused.
    """
    state = _state(refund=REFUND)
    offered = _ids_under(draft_facts(state), ACTIONS_LABEL)
    accepted = action_citations(state)

    assert offered == accepted, (
        f"the prompt offers {offered} but grounding accepts {accepted}; a drafter "
        "copying what it was shown would be refused"
    )
    assert all(i.startswith(grounding.SELF_EVIDENT_PREFIX) for i in accepted)


def test_the_contract_states_the_consequence_of_not_citing() -> None:
    """A rule with no stated consequence reads as a preference.

    Both models produced a good reply and an empty citations array, which is
    what 'you must cite' gets you when nothing says the reply is discarded
    without them.
    """
    assert "discarded" in DRAFT_CONTRACT
