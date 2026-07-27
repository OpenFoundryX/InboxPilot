"""LLM classification of an email into one of the user's categories.

The taxonomy is a parameter, not a constant: each user has their own set of
categories with their own names and guidance (see `models.categorization`).
Blocking OpenAI call — invoke from a worker.
"""

import json
from dataclasses import dataclass
from functools import lru_cache

from openai import OpenAI

from core.config import settings
from core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Category:
    """One choice offered to the model. `key` is what comes back out."""

    key: str
    display_name: str
    description: str


@dataclass(frozen=True)
class Verdict:
    key: str | None
    confidence: float


NO_VERDICT = Verdict(key=None, confidence=0.0)


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _system_prompt(categories: list[Category], extra_instructions: str | None) -> str:
    lines = [f'- "{c.display_name}": {c.description}' for c in categories]
    prompt = (
        "You classify an incoming email into exactly one category for a busy user. "
        "Categories:\n" + "\n".join(lines) + "\n\n"
        'Respond ONLY as JSON: {"label": "<one category name exactly as written above>", '
        '"confidence": <number between 0 and 1>}. '
        "Pick the single best fit. When unsure between an actionable and an informational "
        "category, prefer the actionable one. `confidence` is how sure you are of the pick."
    )
    if extra_instructions:
        prompt += f"\n\nAdditional instructions from the user:\n{extra_instructions.strip()}"
    return prompt


def classify(
    sender: str | None,
    subject: str | None,
    snippet: str | None,
    *,
    categories: list[Category],
    model: str | None = None,
    extra_instructions: str | None = None,
) -> Verdict:
    """Return the chosen category key and confidence, or NO_VERDICT."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    if not categories:
        return NO_VERDICT

    user = (
        f"From: {sender or '(unknown)'}\n"
        f"Subject: {subject or '(no subject)'}\n"
        f"Preview: {(snippet or '')[:500]}"
    )
    resp = _client().chat.completions.create(
        model=model or settings.OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _system_prompt(categories, extra_instructions)},
            {"role": "user", "content": user},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("classify.bad_json", raw=raw)
        return NO_VERDICT
    if not isinstance(parsed, dict):
        log.warning("classify.bad_json", raw=raw)
        return NO_VERDICT

    label = parsed.get("label")
    # A malformed confidence must not void an otherwise-valid label: parse it
    # independently and fall back to a safe default. The pipeline's threshold
    # check (`verdict.confidence < config.confidence_threshold`) DOES consume
    # this value, so an unparseable confidence must fail CLOSED (score 0.0)
    # rather than open — otherwise it would clear every threshold a user sets
    # and bypass the safety net they configured.
    confidence = _coerce_confidence(parsed.get("confidence"))

    # `response_format={"type": "json_object"}` guarantees valid JSON, not a
    # typed schema — the model can hand back a label that is an int, list,
    # dict, bool, or null. This call sits on the path Celery retries with
    # autoretry_for=(Exception,); an uncaught AttributeError here (from
    # calling .strip() on a non-string) would burn three deterministic
    # retries (temperature=0, identical input) before failing outright,
    # never labeling the message and never degrading to NO_VERDICT.
    if not isinstance(label, str):
        log.warning("classify.non_string_label", label=label)
        return NO_VERDICT

    # Map the display name back to a stable key. Lenient about case and
    # surrounding whitespace. `display_name` carries no uniqueness constraint
    # (only `key` does), so two categories can normalize to the same lookup
    # key — build first-wins, deterministically, and log the collision so a
    # silently-shadowed category is diagnosable instead of misdirecting
    # classification forever with no signal.
    by_name: dict[str, str] = {}
    for c in categories:
        norm = c.display_name.strip().casefold()
        if norm in by_name:
            log.warning(
                "classify.duplicate_display_name",
                display_name=c.display_name,
                kept_key=by_name[norm],
                shadowed_key=c.key,
            )
            continue
        by_name[norm] = c.key

    key = by_name.get(label.strip().casefold())
    if key is None:
        log.warning("classify.unknown_label", label=label)
        return NO_VERDICT

    return Verdict(key=key, confidence=confidence)


def _coerce_confidence(value: object) -> float:
    """Coerce the model's confidence to [0, 1]; fail CLOSED (0.0) on anything
    unusable so a garbled/missing confidence never clears a set threshold."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0.0
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, conf))
