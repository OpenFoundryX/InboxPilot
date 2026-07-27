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
        label = parsed.get("label")
        confidence = float(parsed.get("confidence", 1.0))
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        log.warning("classify.bad_json", raw=raw)
        return NO_VERDICT

    # The model answers with a display name; map it back to a stable key. Be
    # lenient about case and surrounding whitespace before giving up.
    by_name = {c.display_name.strip().casefold(): c.key for c in categories}
    key = by_name.get((label or "").strip().casefold())
    if key is None:
        log.warning("classify.unknown_label", label=label)
        return NO_VERDICT

    return Verdict(key=key, confidence=max(0.0, min(1.0, confidence)))
