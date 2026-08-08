import pytest

from services.scheduling.questions import (
    InvalidAnswers,
    labelled,
    normalise_definitions,
    validate,
)


def q(**overrides) -> dict:
    base = {"key": "topic", "label": "What's this about?", "type": "text", "required": False}
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Host-side definitions
# --------------------------------------------------------------------------


def test_a_question_without_a_label_is_dropped():
    assert normalise_definitions([{"label": "  "}, {"label": "Real"}]) == [
        {"key": "q2", "label": "Real", "type": "text", "required": False}
    ]


def test_missing_keys_are_generated():
    cleaned = normalise_definitions([{"label": "One"}, {"label": "Two"}])
    assert [c["key"] for c in cleaned] == ["q1", "q2"]


def test_duplicate_keys_are_made_unique():
    """Two questions sharing a key would silently overwrite each other's answer."""
    cleaned = normalise_definitions([{"key": "a", "label": "One"}, {"key": "a", "label": "Two"}])
    assert len({c["key"] for c in cleaned}) == 2


def test_an_unknown_type_falls_back_to_text():
    assert normalise_definitions([{"label": "X", "type": "wat"}])[0]["type"] == "text"


def test_a_select_with_no_options_becomes_a_text_field():
    """Otherwise the guest gets a dropdown they cannot satisfy."""
    cleaned = normalise_definitions([{"label": "Pick", "type": "select", "options": []}])
    assert cleaned[0]["type"] == "text"
    assert "options" not in cleaned[0]


def test_select_options_survive():
    cleaned = normalise_definitions(
        [{"label": "Pick", "type": "select", "options": ["a", "b"]}]
    )
    assert cleaned[0]["options"] == ["a", "b"]


# --------------------------------------------------------------------------
# Guest answers
# --------------------------------------------------------------------------


def test_optional_unanswered_questions_are_simply_absent():
    assert validate([q()], {}) == {}


def test_required_questions_must_be_answered():
    with pytest.raises(InvalidAnswers):
        validate([q(required=True)], {})


def test_whitespace_is_not_an_answer():
    with pytest.raises(InvalidAnswers):
        validate([q(required=True)], {"topic": "   "})


def test_answers_are_trimmed():
    assert validate([q()], {"topic": "  hi  "}) == {"topic": "hi"}


def test_an_over_long_answer_is_rejected():
    with pytest.raises(InvalidAnswers):
        validate([q()], {"topic": "x" * 2001})


def test_select_answers_must_be_one_of_the_options():
    question = q(type="select", options=["Demo", "Support"])
    assert validate([question], {"topic": "Demo"}) == {"topic": "Demo"}
    with pytest.raises(InvalidAnswers):
        validate([question], {"topic": "Something else"})


def test_checkboxes_record_both_states():
    question = q(type="checkbox")
    assert validate([question], {"topic": True}) == {"topic": "Yes"}
    assert validate([question], {"topic": False}) == {"topic": "No"}


def test_a_required_checkbox_must_be_ticked():
    with pytest.raises(InvalidAnswers):
        validate([q(type="checkbox", required=True)], {"topic": False})


def test_unknown_answer_keys_are_ignored_rather_than_rejected():
    """A form open in a tab while the host edited the event type must still
    submit — the stale field is dropped, not turned into an error."""
    assert validate([q()], {"topic": "hi", "removed_question": "value"}) == {"topic": "hi"}


def test_labelled_rekeys_answers_for_humans():
    assert labelled([q()], {"topic": "hi"}) == {"What's this about?": "hi"}


def test_labelled_passes_through_keys_it_no_longer_recognises():
    assert labelled([], {"gone": "value"}) == {"gone": "value"}
