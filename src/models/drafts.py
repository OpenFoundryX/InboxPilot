"""Auto-drafted replies: settings and uploaded context files.

Two tables. `draft_settings` is the per-user singleton of knobs (which categories
to draft for, tone, selectivity, signature, follow-ups). `draft_files` holds text
the user uploaded to steer or inform drafting — the extracted text only, never
the original bytes.

Deliberately absent: any record of the drafts themselves. Nothing about a
generated reply is stored, so the guard against drafting the same email twice
cannot live in this database. It lives in Gmail instead, as the
`gmail.DRAFTED_LABEL` marker applied to a message once it has been drafted for —
the same trick `services.digest.scheduling` uses with `inboxos-later`.

The category gate stores `EmailCategory.key` values rather than Gmail label
names because the on-arrival path already has the key in hand — `classify_and_label`
returns it — so gating costs no extra Gmail call.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin

# Where an uploaded file's text lands in the prompt. Instruction text is appended
# to the system prompt (directives are followed far more reliably there);
# knowledge text goes in a REFERENCE block the model may draw facts from.
PURPOSE_INSTRUCTION = "instruction"
PURPOSE_KNOWLEDGE = "knowledge"
FILE_PURPOSES = frozenset({PURPOSE_INSTRUCTION, PURPOSE_KNOWLEDGE})

# Which kind of draft a code path is producing. Nothing stores these — they only
# distinguish the two flows in logs and prompt selection. A reply answers mail
# that arrived; a follow_up nudges a thread of ours that went quiet.
KIND_REPLY = "reply"
KIND_FOLLOW_UP = "follow_up"

# How readily the user replies. Passed to the drafter as a bar the email must
# clear: on `important_only` the model is told it may decline, and a decline
# means no draft is created at all.
SELECTIVITY_ALMOST_ALWAYS = "almost_always"
SELECTIVITY_WHEN_NEEDED = "when_needed"
SELECTIVITY_IMPORTANT_ONLY = "important_only"
SELECTIVITY_LEVELS = frozenset(
    {SELECTIVITY_ALMOST_ALWAYS, SELECTIVITY_WHEN_NEEDED, SELECTIVITY_IMPORTANT_ONLY}
)

TONES = frozenset({"formal", "friendly", "concise", "warm"})
LENGTHS = frozenset({"short", "medium", "long"})

# The two categories that mean "this needs something from me". Everything else
# in the taxonomy is informational, so drafting for it by default would produce
# replies to receipts and newsletters.
DEFAULT_DRAFT_CATEGORY_KEYS = ("to_do", "to_follow_up")

# A single file's stored text. Generous — the prompt budget in
# `services.drafts.context` is what actually protects the LLM call, and storing
# the full text keeps it available if that budget later grows.
MAX_FILE_CHARS = 100_000


def default_category_keys() -> list[str]:
    return list(DEFAULT_DRAFT_CATEGORY_KEYS)


class DraftSettings(UUIDMixin, TimestampMixin, Base):
    """Per-user singleton, same shape as CategorizationSettings / MailmanSettings."""

    __tablename__ = "draft_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    # Off by default. Every other feature here is read-only until asked, but
    # drafting writes objects into the user's mailbox, so it must be opted into.
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # References EmailCategory.key. Not an FK for the same reason as
    # CategorizationRule.category_key: `key` is unique only per user, so this
    # would need a composite reference for no real benefit. Validated in the API
    # layer; a key whose category was deleted simply stops matching.
    category_keys: Mapped[list] = mapped_column(
        JSONB, default=default_category_keys, nullable=False
    )

    selectivity: Mapped[str] = mapped_column(
        String(32), default=SELECTIVITY_WHEN_NEEDED, nullable=False
    )
    tone: Mapped[str] = mapped_column(String(32), default="friendly", nullable=False)
    length: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)

    # The toggle is separate from the text so switching instructions off keeps
    # them for later instead of making the user retype them.
    custom_instructions_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    custom_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    signature_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)

    follow_up_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    follow_up_days: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    # None = use settings.OPENAI_MODEL, mirroring CategorizationSettings.model.
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Per-user due-gating for the sweeps, the same way MailmanSettings tracks
    # last_delivery_at: the beat fires often, each task decides who is due.
    last_sweep_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_follow_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"<DraftSettings user={self.user_id} enabled={self.is_enabled}>"


class DraftFile(UUIDMixin, TimestampMixin, Base):
    """Text extracted from a file the user uploaded to steer or inform drafting.

    The original bytes are discarded after extraction: the only consumer of an
    uploaded file is the LLM prompt, so nothing ever needs them again. That is
    why there is no download endpoint and why `size_bytes` is kept — it is the
    only remaining trace of what was uploaded, and the UI shows it.
    """

    __tablename__ = "draft_files"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Lets a user park a file without deleting it — the text is skipped when
    # assembling the prompt.
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<DraftFile {self.filename} purpose={self.purpose}>"
