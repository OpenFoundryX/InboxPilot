"""C1: `services.drafts.create` is the single gate + meter for every
draft-producing caller (the arrival job `workers.jobs.reply_draft_job`, the
catch-up sweep `services.drafts.sweep`, and the follow-up sweep
`services.drafts.follow_up`).

Before this fix, none of the three callers was gated on entitlement, and only
the two sweeps (not the arrival job) counted a draft against the monthly
quota — and they did it in a bulk `add_drafts` call after their loop, not per
draft. These tests pin the gate and the meter at the one place both draft
functions actually create a draft (`_create_and_mark`), using monkeypatches
for the LLM call and the Gmail writes so nothing here depends on a live
Postgres row being visible across `create.py`'s own `with_worker_session`
connection (a different connection than the `db` fixture's SAVEPOINT).
"""

from services.drafts import create as draft_create
from services.drafts.context import DraftConfig
from services.drafts.generate import Draft


def _config(**overrides) -> DraftConfig:
    base = dict(
        is_enabled=True,
        category_keys=("work",),
        selectivity="when_needed",
        tone="friendly",
        length="medium",
        custom_instructions=None,
        signature=None,
        follow_up_enabled=True,
        follow_up_days=3,
        model=None,
    )
    base.update(overrides)
    return DraftConfig(**base)


def test_draft_reply_never_generates_when_not_entitled(monkeypatch):
    """The entitlement gate runs *before* the LLM call, not just before the
    Gmail write — a denied draft must not spend a token either."""
    monkeypatch.setattr(draft_create, "_entitled_for_draft", lambda user_id: False)

    def _boom(*a, **k):
        raise AssertionError("generate_reply was called for a non-entitled user")

    monkeypatch.setattr(draft_create, "generate_reply", _boom)
    monkeypatch.setattr(draft_create, "gmail", type("G", (), {"create_draft": _boom})())

    result = draft_create.draft_reply(
        "user-1",
        message_id="msg-1",
        sender="a@example.com",
        subject="Hi",
        body="body",
        category_key="work",
        config=_config(),
    )
    assert result is None


def test_draft_follow_up_never_generates_when_not_entitled(monkeypatch):
    monkeypatch.setattr(draft_create, "_entitled_for_draft", lambda user_id: False)

    def _boom(*a, **k):
        raise AssertionError("generate_follow_up was called for a non-entitled user")

    monkeypatch.setattr(draft_create, "generate_follow_up", _boom)

    result = draft_create.draft_follow_up(
        "user-1",
        message_id="msg-1",
        recipient_raw="a@example.com",
        subject="Hi",
        body="body",
        days_quiet=3,
        config=_config(),
    )
    assert result is None


def _stub_gmail_and_activity(monkeypatch):
    monkeypatch.setattr(draft_create, "_ensure_labels_once", lambda user_id: True)
    monkeypatch.setattr(draft_create.gmail_ops, "add_label", lambda *a, **k: None)
    monkeypatch.setattr(draft_create, "record_draft_created", lambda *a, **k: None)


def test_draft_reply_meters_exactly_once_when_a_draft_is_created(monkeypatch):
    """A created draft must increment the quota exactly once — the point of
    moving metering into `_create_and_mark`, the one funnel every caller
    (arrival job, catch-up sweep, follow-up sweep) shares."""
    monkeypatch.setattr(draft_create, "_entitled_for_draft", lambda user_id: True)
    monkeypatch.setattr(
        draft_create, "generate_reply", lambda *a, **k: Draft(should_draft=True, body="reply body", reason="ok")
    )
    monkeypatch.setattr(draft_create.gmail, "create_draft", lambda *a, **k: "gmail-draft-1")
    _stub_gmail_and_activity(monkeypatch)

    meter_calls = []
    monkeypatch.setattr(draft_create, "_meter_draft", lambda user_id: meter_calls.append(user_id))

    result = draft_create.draft_reply(
        "user-1",
        message_id="msg-1",
        sender="a@example.com",
        subject="Hi",
        body="body",
        category_key="work",
        config=_config(),
    )
    assert result == "gmail-draft-1"
    assert meter_calls == ["user-1"]


def test_draft_follow_up_meters_exactly_once_when_a_draft_is_created(monkeypatch):
    monkeypatch.setattr(draft_create, "_entitled_for_draft", lambda user_id: True)
    monkeypatch.setattr(
        draft_create,
        "generate_follow_up",
        lambda *a, **k: Draft(should_draft=True, body="nudge body", reason="ok"),
    )
    monkeypatch.setattr(draft_create.gmail, "create_draft", lambda *a, **k: "gmail-draft-2")
    _stub_gmail_and_activity(monkeypatch)

    meter_calls = []
    monkeypatch.setattr(draft_create, "_meter_draft", lambda user_id: meter_calls.append(user_id))

    result = draft_create.draft_follow_up(
        "user-1",
        message_id="msg-1",
        recipient_raw="a@example.com",
        subject="Hi",
        body="body",
        days_quiet=3,
        config=_config(),
    )
    assert result == "gmail-draft-2"
    assert meter_calls == ["user-1"]


def test_declined_draft_is_not_metered(monkeypatch):
    """The model choosing not to draft is the ordinary "no draft" outcome, not
    a billable event — `_meter_draft` must not run for it."""
    monkeypatch.setattr(draft_create, "_entitled_for_draft", lambda user_id: True)
    monkeypatch.setattr(
        draft_create, "generate_reply", lambda *a, **k: Draft(should_draft=False, body="", reason="declined")
    )
    _stub_gmail_and_activity(monkeypatch)

    meter_calls = []
    monkeypatch.setattr(draft_create, "_meter_draft", lambda user_id: meter_calls.append(user_id))

    result = draft_create.draft_reply(
        "user-1",
        message_id="msg-1",
        sender="a@example.com",
        subject="Hi",
        body="body",
        category_key="work",
        config=_config(),
    )
    assert result is None
    assert meter_calls == []


def test_declined_draft_still_marks_the_message_drafted(monkeypatch):
    """A decline is deterministic and must still stop the 15-minute sweep from
    re-asking the same question forever — unaffected by the new entitlement
    gate, which runs before this and only blocks non-entitled users."""
    monkeypatch.setattr(draft_create, "_entitled_for_draft", lambda user_id: True)
    monkeypatch.setattr(
        draft_create, "generate_reply", lambda *a, **k: Draft(should_draft=False, body="", reason="declined")
    )
    marks = []
    monkeypatch.setattr(draft_create, "_mark_drafted", lambda user_id, message_id: marks.append(message_id))

    draft_create.draft_reply(
        "user-1",
        message_id="msg-1",
        sender="a@example.com",
        subject="Hi",
        body="body",
        category_key="work",
        config=_config(),
    )
    assert marks == ["msg-1"]


def test_no_entitlement_does_not_mark_the_message_drafted(monkeypatch):
    """Unlike a content decline, a quota/lock denial is not permanent — the
    account may resubscribe or a new billing month may start, so the message
    must remain eligible for a future sweep rather than being marked drafted
    forever."""
    monkeypatch.setattr(draft_create, "_entitled_for_draft", lambda user_id: False)
    marks = []
    monkeypatch.setattr(draft_create, "_mark_drafted", lambda user_id, message_id: marks.append(message_id))

    draft_create.draft_reply(
        "user-1",
        message_id="msg-1",
        sender="a@example.com",
        subject="Hi",
        body="body",
        category_key="work",
        config=_config(),
    )
    assert marks == []
