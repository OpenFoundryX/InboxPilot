"""LLM classification of an email into one of InboxPilot's org labels.

Hardcoded taxonomy for now (the six organizational labels provisioned by
`gmail.ensure_labels`). Uses OpenAI; blocking call, invoke from a worker.
"""

import json
from functools import lru_cache

from openai import OpenAI

from core.config import settings
from core.logging import get_logger

log = get_logger(__name__)

# Hardcoded categories -> guidance the model uses to choose. Keys MUST match the
# provisioned Gmail label names (see integrations.composio.gmail.INBOXPILOT_LABELS).
LABELS: dict[str, str] = {
    "to do": "Needs an action or reply from me; a real request, task, or question directed at me.",
    "to follow up": "A thread I'm waiting on or should chase; awaiting someone's reply, or a nudge I must track.",
    "notification": "Automated transactional notice: receipts, confirmations, alerts, security codes, system messages.",
    "fyi": "Informational and relevant, from a person or team, but needs no action from me.",
    "marketing": "Promotional or sales: newsletters, product offers, campaigns, cold pitches.",
    "noise": "Low-value bulk or social clutter; spam-like, unimportant, safe to ignore.",
}
LABEL_NAMES = list(LABELS)


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _system_prompt() -> str:
    lines = [f'- "{name}": {desc}' for name, desc in LABELS.items()]
    return (
        "You classify an incoming email into exactly one category for a busy user. "
        "Categories:\n" + "\n".join(lines) + "\n\n"
        'Respond ONLY as JSON: {"label": "<one category name exactly as written above>"}. '
        "Pick the single best fit. When unsure between an actionable and an informational "
        'category, prefer the actionable one ("to do" / "to follow up").'
    )


def classify(sender: str | None, subject: str | None, snippet: str | None) -> str | None:
    """Return one label name, or None if classification fails/misfires."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    user = (
        f"From: {sender or '(unknown)'}\n"
        f"Subject: {subject or '(no subject)'}\n"
        f"Preview: {(snippet or '')[:500]}"
    )
    resp = _client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": user},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        label = json.loads(raw).get("label")
    except (json.JSONDecodeError, AttributeError):
        log.warning("classify.bad_json", raw=raw)
        return None

    if label not in LABELS:
        # Be lenient about case/whitespace before giving up.
        norm = {n.casefold(): n for n in LABEL_NAMES}
        label = norm.get((label or "").strip().casefold())
    return label
