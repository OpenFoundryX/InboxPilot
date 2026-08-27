"""Validation for the host-defined questions on a booking form.

The shape of these answers isn't known at import time — it is whatever the host
configured on the event type — so Pydantic can't check them and this does. Kept
apart from the booking flow because it is pure, total, and the only place that
decides what a valid answer is.
"""

QUESTION_TYPES = ("text", "textarea", "select", "checkbox")

MAX_ANSWER_LENGTH = 2000


class InvalidAnswers(ValueError):
    """Guest input that the event type's questions do not accept."""


def normalise_definitions(questions: list[dict]) -> list[dict]:
    """Clean a host's question list: stable keys, known types, sane options."""
    cleaned: list[dict] = []
    seen: set[str] = set()
    for index, question in enumerate(questions):
        label = str(question.get("label") or "").strip()
        if not label:
            continue
        kind = question.get("type") if question.get("type") in QUESTION_TYPES else "text"
        key = str(question.get("key") or "").strip() or f"q{index + 1}"
        while key in seen:
            key = f"{key}x"
        seen.add(key)

        entry = {
            "key": key,
            "label": label[:200],
            "type": kind,
            "required": bool(question.get("required")),
        }
        if kind == "select":
            options = [
                str(o).strip()[:120] for o in (question.get("options") or []) if str(o).strip()
            ]
            # A select with nothing to select from is a text box wearing a hat.
            if not options:
                entry["type"] = "text"
            else:
                entry["options"] = options[:20]
        cleaned.append(entry)
    return cleaned[:15]


def validate(questions: list[dict], answers: dict) -> dict:
    """Check a guest's answers against the definitions; return them cleaned.

    Unknown keys are dropped rather than rejected — a form left open in a tab
    while the host edited the event type should not fail to submit over a
    question that no longer exists.
    """
    result: dict[str, str] = {}
    for question in questions:
        key = question["key"]
        raw = answers.get(key)
        kind = question.get("type", "text")

        if kind == "checkbox":
            checked = bool(raw)
            if question.get("required") and not checked:
                raise InvalidAnswers(f"{question['label']} is required")
            result[key] = "Yes" if checked else "No"
            continue

        value = "" if raw is None else str(raw).strip()
        if not value:
            if question.get("required"):
                raise InvalidAnswers(f"{question['label']} is required")
            continue
        if len(value) > MAX_ANSWER_LENGTH:
            raise InvalidAnswers(f"{question['label']} is too long")
        if kind == "select" and value not in question.get("options", []):
            raise InvalidAnswers(f"{value!r} is not an option for {question['label']}")
        result[key] = value
    return result


def labelled(questions: list[dict], answers: dict) -> dict[str, str]:
    """Answers keyed by human label, for email bodies and the host's list."""
    by_key = {q["key"]: q["label"] for q in questions}
    return {by_key.get(key, key): value for key, value in answers.items()}
