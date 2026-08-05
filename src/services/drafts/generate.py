"""The one OpenAI call that writes a draft. Blocking — invoke from a worker.

Fails closed throughout: anything the model returns that we cannot read as a
complete, non-empty reply produces `DECLINED` rather than a draft. A missing
draft is a non-event the user never notices; a garbled one is an object sitting
in their mailbox with their name on it, one keystroke from being sent.
"""

import json
from dataclasses import dataclass
from functools import lru_cache

from openai import OpenAI

from core.config import settings
from core.logging import get_logger
from services.drafts.context import (
    DraftConfig,
    append_signature,
    build_follow_up_prompt,
    build_system_prompt,
    build_user_prompt,
)

log = get_logger(__name__)

# Not 0, unlike the classifier. Classification wants the same answer every time;
# prose at temperature 0 reads mechanically. Idempotency here comes from
# `uq_draft_replies_user_source_kind`, not from determinism, so there is no
# reason to keep it flat.
TEMPERATURE = 0.4

# Below this the "reply" is a fragment — a stray greeting or a truncated first
# line — not something worth putting in a mailbox.
MIN_BODY_CHARS = 20

RESPONSE_CONTRACT = (
    '\n\nRespond ONLY as JSON: {"should_draft": <true|false>, '
    '"reason": "<a few words, why you did or did not draft>", '
    '"body": "<the reply body, plain text, or empty string if should_draft is false>"}'
)


@dataclass(frozen=True)
class Draft:
    should_draft: bool
    body: str
    reason: str


DECLINED = Draft(should_draft=False, body="", reason="declined")


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _call(config: DraftConfig, system: str, user: str) -> Draft:
    resp = _client().chat.completions.create(
        model=config.model or settings.OPENAI_MODEL,
        temperature=TEMPERATURE,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system + RESPONSE_CONTRACT},
            {"role": "user", "content": user},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("drafts.bad_json", raw=raw[:500])
        return DECLINED
    if not isinstance(parsed, dict):
        log.warning("drafts.bad_json", raw=raw[:500])
        return DECLINED

    reason = parsed.get("reason")
    reason = reason.strip()[:200] if isinstance(reason, str) else ""

    # `response_format={"type": "json_object"}` guarantees valid JSON, not a
    # typed schema: `should_draft` can come back as a string, and `body` as a
    # list or null. Treat anything other than a literal `true` as a decline —
    # the permissive reading ("truthy") would turn the string "false" into a
    # draft, which is exactly the direction that must not fail open.
    if parsed.get("should_draft") is not True:
        return Draft(should_draft=False, body="", reason=reason or "declined")

    body = parsed.get("body")
    if not isinstance(body, str):
        log.warning("drafts.non_string_body", body_type=type(body).__name__)
        return DECLINED

    body = body.strip()
    if len(body) < MIN_BODY_CHARS:
        log.warning("drafts.body_too_short", chars=len(body))
        return DECLINED

    return Draft(should_draft=True, body=append_signature(body, config), reason=reason)


def generate_reply(
    config: DraftConfig,
    *,
    sender: str | None,
    subject: str | None,
    body: str | None,
    to: str | None = None,
    cc: str | None = None,
    thread_excerpt: str | None = None,
    user_name: str | None = None,
) -> Draft:
    """Draft a reply to an incoming email, or decline."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    return _call(
        config,
        build_system_prompt(config, user_name),
        build_user_prompt(
            config=config,
            sender=sender,
            subject=subject,
            body=body,
            to=to,
            cc=cc,
            thread_excerpt=thread_excerpt,
        ),
    )


def generate_follow_up(
    config: DraftConfig,
    *,
    recipient: str | None,
    subject: str | None,
    body: str | None,
    days_quiet: int,
    user_name: str | None = None,
) -> Draft:
    """Draft a nudge for a thread of the user's that went unanswered, or decline."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    return _call(
        config,
        build_system_prompt(config, user_name),
        build_follow_up_prompt(
            config=config,
            recipient=recipient,
            subject=subject,
            body=body,
            days_quiet=days_quiet,
        ),
    )
