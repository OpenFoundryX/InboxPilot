"""Load a user's drafting config and assemble the prompt from it.

Kept separate from `generate` so the prompt can be reasoned about (and tested)
without an API key: everything here is pure string work over a frozen snapshot.

The character budget is the important part. Uploaded files are unbounded in
practice — a user can attach a 200-page handbook — so the text is truncated on
the way into the prompt rather than at upload, where we would have to guess what
matters. Instructions get a smaller budget than knowledge but are trimmed last,
because a truncated directive changes behaviour while a truncated reference
merely loses a fact.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings as app_settings
from core.database import run_async, with_worker_session
from models.scheduling import SchedulingSettings
from models.users import User
from models.drafts import (
    PURPOSE_INSTRUCTION,
    PURPOSE_KNOWLEDGE,
    SELECTIVITY_ALMOST_ALWAYS,
    SELECTIVITY_IMPORTANT_ONLY,
    SELECTIVITY_WHEN_NEEDED,
)
from services.drafts.store import get_or_create_settings, list_files

# Roughly 7k tokens of context in total, well inside every model we allow while
# leaving room for the email itself and the reply.
INSTRUCTION_BUDGET_CHARS = 8_000
KNOWLEDGE_BUDGET_CHARS = 20_000
# The incoming email. Long quoted-reply chains are mostly repetition of text the
# model has already seen higher up the thread.
EMAIL_BUDGET_CHARS = 6_000

TONE_GUIDANCE = {
    "formal": "Formal and professional. Full sentences, no contractions, no slang.",
    "friendly": "Warm but professional. Contractions are fine; sound like a person.",
    "concise": "Brief and direct. Lead with the answer. No pleasantries beyond a greeting.",
    "warm": "Warm and personable. Acknowledge the person before the business.",
}

LENGTH_GUIDANCE = {
    "short": "One or two short paragraphs at most. Under 60 words.",
    "medium": "Two or three short paragraphs. Around 80-150 words.",
    "long": "As long as the message needs, covering every point raised. 150-300 words.",
}

SELECTIVITY_GUIDANCE = {
    SELECTIVITY_ALMOST_ALWAYS: (
        "The user replies to almost everything, even just to acknowledge. Draft a "
        "reply unless the email plainly cannot be replied to (a no-reply automated "
        "notice, or a bulk marketing blast)."
    ),
    SELECTIVITY_WHEN_NEEDED: (
        "The user replies when a response is actually needed. Draft a reply if the "
        "email asks a question, requests something, or expects a decision. Decline "
        "if it is purely informational, automated, or already resolved."
    ),
    SELECTIVITY_IMPORTANT_ONLY: (
        "The user only replies to important email. Draft a reply only if the email "
        "is from a real person and materially needs the user's answer — a decision, "
        "a commitment, a direct question they alone can answer. Decline otherwise, "
        "and be strict about it."
    ),
}


@dataclass(frozen=True)
class DraftConfig:
    """A user's drafting config, detached from the DB session.

    Frozen and session-free so the sync Celery path can load it once, cheaply,
    and pass it around — the same shape as `categorization.pipeline.UserConfig`.
    """

    is_enabled: bool
    category_keys: tuple[str, ...]
    selectivity: str
    tone: str
    length: str
    custom_instructions: str | None
    signature: str | None
    follow_up_enabled: bool
    follow_up_days: int
    model: str | None
    # The user's own address. Carried so the prompt can point at which of a
    # message's recipients is them — listing To/Cc is no use to a model that
    # cannot tell which entry it is being asked about. `None` where the address
    # could not be loaded, in which case the recipient lines are omitted
    # entirely rather than shown unanchored.
    account_email: str | None = None
    instruction_texts: tuple[str, ...] = field(default=())
    knowledge_texts: tuple[str, ...] = field(default=())
    # The user's public booking link, or None when they have no scheduling
    # profile or have switched `include_link_in_drafts` off. Present so a reply
    # about meeting can offer a way to book instead of proposing times the
    # model would have to invent — see `build_system_prompt`.
    scheduling_link: str | None = None

    def drafts_for(self, category_key: str | None) -> bool:
        """Is this category one the user asked for drafts on?"""
        return bool(category_key) and category_key in self.category_keys


async def _scheduling_link(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    """The user's booking URL, if they have one and want it offered in drafts.

    Read here rather than in the prompt builder so the sync Celery path gets it
    with the rest of the config, and so "the user switched this off" is
    expressed as the link being absent — the prompt then has nothing to
    conditionally suppress.
    """
    row = await db.scalar(
        select(SchedulingSettings).where(SchedulingSettings.user_id == user_id)
    )
    if row is None or not row.enabled or not row.include_link_in_drafts:
        return None
    return f"{app_settings.FRONTEND_BASE_URL}/schedule/{row.slug}"


async def load_config(db: AsyncSession, user_id: uuid.UUID) -> DraftConfig:
    row = await get_or_create_settings(db, user_id)
    files = await list_files(db, user_id)
    enabled = [f for f in files if f.is_enabled]
    # One extra read per config load, and the config is loaded once per sweep,
    # not once per message. Worth it: without the address there is no way to
    # tell a message addressed to the user from one they were copied on.
    account_email = await db.scalar(select(User.email).where(User.id == user_id))
    return DraftConfig(
        account_email=account_email,
        scheduling_link=await _scheduling_link(db, user_id),
        is_enabled=row.is_enabled,
        category_keys=tuple(row.category_keys or ()),
        selectivity=row.selectivity,
        tone=row.tone,
        length=row.length,
        # The toggle is honoured here rather than at every call site, so a
        # disabled instruction block simply is not in the config.
        custom_instructions=row.custom_instructions if row.custom_instructions_enabled else None,
        signature=row.signature if row.signature_enabled else None,
        follow_up_enabled=row.follow_up_enabled,
        follow_up_days=row.follow_up_days,
        model=row.model,
        instruction_texts=tuple(
            f.extracted_text for f in enabled if f.purpose == PURPOSE_INSTRUCTION
        ),
        knowledge_texts=tuple(f.extracted_text for f in enabled if f.purpose == PURPOSE_KNOWLEDGE),
    )


def get_config(user_id: str) -> DraftConfig:
    """Load a user's drafting config from sync (Celery) code."""
    uid = uuid.UUID(user_id)
    return run_async(with_worker_session(lambda db: load_config(db, uid)))


def _join_budgeted(texts: tuple[str, ...], budget: int) -> str:
    """Concatenate texts, stopping at `budget` characters.

    Whole documents are kept in order until the budget runs out, and the one
    that straddles the limit is cut mid-way. Splitting the budget evenly instead
    would truncate every file, which reliably beheads the short ones that fit
    fine on their own.
    """
    out: list[str] = []
    remaining = budget
    for text in texts:
        if remaining <= 0:
            break
        chunk = text[:remaining]
        out.append(chunk)
        remaining -= len(chunk)
    return "\n\n---\n\n".join(out)


def build_system_prompt(config: DraftConfig, user_name: str | None = None) -> str:
    """The system prompt: who the model is writing as, and how."""
    who = f" on behalf of {user_name}" if user_name else ""
    parts = [
        f"You are drafting an email reply{who}. You are writing AS the user, in the "
        "first person — never as an assistant, and never about the user in the third "
        "person.",
        "",
        "Rules you must not break:",
        "- Never invent a fact, a commitment, a date, a price, or a name. If the "
        "reply needs information you do not have, write the reply around what you "
        "do know and leave the gap obvious rather than guessing.",
        "- Never promise anything the user has not already said they would do.",
        "- Do not restate the whole incoming email back at the sender.",
        "- Output plain text, not HTML or markdown.",
        "- If the recipients are shown and the user's own address appears only in "
        "Cc, they were copied for visibility, not asked. Decline unless the "
        "message puts a question or a request to them by name.",
        "- Decline anything addressed to someone else, to a list, or to nobody in "
        "particular. A reply the sender did not ask the user for is worse than "
        "no reply at all.",
        "",
        f"Tone: {TONE_GUIDANCE.get(config.tone, TONE_GUIDANCE['friendly'])}",
        f"Length: {LENGTH_GUIDANCE.get(config.length, LENGTH_GUIDANCE['medium'])}",
        "",
        "When to reply at all: "
        + SELECTIVITY_GUIDANCE.get(
            config.selectivity, SELECTIVITY_GUIDANCE[SELECTIVITY_WHEN_NEEDED]
        ),
    ]

    if config.scheduling_link:
        parts += [
            "",
            "If the reply needs to arrange a meeting, do NOT propose specific times "
            "— you cannot see the user's calendar and an invented slot is a "
            "commitment they may not be able to keep. Instead include this booking "
            f"link and invite the sender to pick a time that suits them: {config.scheduling_link}",
        ]

    if config.custom_instructions:
        parts += [
            "",
            "The user's own instructions. These override the tone and length "
            "guidance above where they conflict:",
            config.custom_instructions.strip()[:INSTRUCTION_BUDGET_CHARS],
        ]

    if config.instruction_texts:
        instructions = _join_budgeted(config.instruction_texts, INSTRUCTION_BUDGET_CHARS)
        if instructions:
            parts += [
                "",
                "Further instructions, from documents the user uploaded:",
                instructions,
            ]

    if config.signature:
        parts += [
            "",
            "Do NOT write a sign-off or signature — one is appended automatically. "
            "End with the last line of the message body.",
        ]
    else:
        parts += [
            "",
            "End with a brief sign-off appropriate to the tone. No signature block.",
        ]

    return "\n".join(parts)


def build_user_prompt(
    *,
    config: DraftConfig,
    sender: str | None,
    subject: str | None,
    body: str | None,
    thread_excerpt: str | None = None,
    to: str | None = None,
    cc: str | None = None,
) -> str:
    """The user message: the email to answer, plus any reference material.

    `to`/`cc` are shown only when the user's own address is known, because a
    recipient list the model cannot locate itself in tells it nothing. Both are
    frequently absent — the Gmail trigger payload does not always carry them —
    and absent must read as unknown, never as "nobody".
    """
    parts: list[str] = []

    if config.knowledge_texts:
        knowledge = _join_budgeted(config.knowledge_texts, KNOWLEDGE_BUDGET_CHARS)
        if knowledge:
            parts += [
                "REFERENCE MATERIAL — background the user has provided. Draw on it "
                "when the reply needs a fact, but only what is actually written "
                "here. Do not mention that you were given reference material.",
                "<<<",
                knowledge,
                ">>>",
                "",
            ]

    parts += ["EMAIL TO REPLY TO:", f"From: {sender or 'unknown'}"]

    if config.account_email and (to or cc):
        parts.append(f"To: {to or '(none)'}")
        if cc:
            parts.append(f"Cc: {cc}")
        parts.append(
            f"(your address is {config.account_email} — check whether it is in To "
            "or only in Cc before deciding to reply)"
        )

    parts.append(f"Subject: {subject or '(no subject)'}")
    if thread_excerpt:
        parts += ["", "Earlier in this thread:", thread_excerpt[:EMAIL_BUDGET_CHARS]]
    parts += ["", (body or "").strip()[:EMAIL_BUDGET_CHARS] or "(no body)"]

    return "\n".join(parts)


def build_follow_up_prompt(
    *,
    config: DraftConfig,
    recipient: str | None,
    subject: str | None,
    body: str | None,
    days_quiet: int,
) -> str:
    """The user message for a nudge on a thread of ours that went unanswered."""
    parts: list[str] = []

    if config.knowledge_texts:
        knowledge = _join_budgeted(config.knowledge_texts, KNOWLEDGE_BUDGET_CHARS)
        if knowledge:
            parts += ["REFERENCE MATERIAL:", "<<<", knowledge, ">>>", ""]

    parts += [
        f"The user sent the message below to {recipient or 'the recipient'} "
        f"{days_quiet} days ago and has had no reply.",
        "",
        "Draft a short, courteous follow-up that nudges for a response. Do not "
        "repeat the original message — reference it briefly and ask for an update. "
        "Do not sound annoyed or apologetic. Decline only if the original message "
        "needed no reply in the first place.",
        "",
        f"Subject: {subject or '(no subject)'}",
        "",
        "THE MESSAGE THE USER SENT:",
        (body or "").strip()[:EMAIL_BUDGET_CHARS] or "(no body)",
    ]
    return "\n".join(parts)


def append_signature(body: str, config: DraftConfig) -> str:
    """Append the user's signature, if they have one and want it included."""
    if not config.signature:
        return body
    signature = config.signature.strip()
    if not signature:
        return body
    return f"{body.rstrip()}\n\n{signature}"


__all__ = [
    "DraftConfig",
    "append_signature",
    "build_follow_up_prompt",
    "build_system_prompt",
    "build_user_prompt",
    "get_config",
    "load_config",
]
