"""Who a draft is for, and whether the thread still needs one.

Two reported bugs, one shared cause: the draft path threw away information it
already held. `EmailSummary.to` was populated and dropped, so nothing — query,
code, or prompt — could tell a message addressed to the user from one they were
merely copied on. And the catch-up query excluded the user's *sent messages*
but not *threads they had already replied to*, so an old incoming message kept
its category label, stayed a candidate, and got a draft attached to the thread
underneath the user's own reply.
"""

from services.drafts import sweep
from services.drafts.context import DraftConfig, build_system_prompt, build_user_prompt

ME = "nilesh@chronon.co.in"


def _config(**overrides) -> DraftConfig:
    base = dict(
        is_enabled=True,
        category_keys=("to do",),
        selectivity="when_needed",
        tone="friendly",
        length="medium",
        custom_instructions=None,
        signature=None,
        follow_up_enabled=False,
        follow_up_days=3,
        model=None,
        account_email=ME,
    )
    base.update(overrides)
    return DraftConfig(**base)


# --- the candidate query -------------------------------------------------


def test_query_requires_the_user_to_be_a_recipient():
    """The reported bug: drafts for mail that was never addressed to them."""
    assert "to:me" in sweep.candidate_query("to do")


def test_query_keeps_every_previous_exclusion():
    q = sweep.candidate_query("to do")
    assert 'label:"to do"' in q
    assert f"newer_than:{sweep.LOOKBACK_DAYS}d" in q
    assert "-in:sent" in q
    assert "-in:draft" in q
    assert "-label:" in q


def test_query_quotes_multi_word_labels():
    assert 'label:"to follow up"' in sweep.candidate_query("to follow up")


# --- threads the user already replied to ---------------------------------


class _FakeEmail:
    def __init__(self, thread_id):
        self.thread_id = thread_id


def test_replied_threads_are_collected_in_one_query(monkeypatch):
    seen = {}

    def fake_fetch(user_id, query, max_results=None, verbose=False):
        seen["query"] = query
        seen["calls"] = seen.get("calls", 0) + 1
        return [_FakeEmail("t1"), _FakeEmail("t2"), _FakeEmail("t1"), _FakeEmail(None)]

    monkeypatch.setattr(sweep.gmail, "fetch_by_query", fake_fetch)
    out = sweep.replied_thread_ids("u1")

    assert out == {"t1", "t2"}
    assert seen["calls"] == 1, "one query for the whole pass, not one per candidate"
    assert "in:sent" in seen["query"]
    assert f"newer_than:{sweep.LOOKBACK_DAYS}d" in seen["query"]


def test_a_dead_sent_query_does_not_fail_the_pass(monkeypatch):
    """Losing this filter should cost precision, not the whole sweep."""

    def boom(*args, **kwargs):
        raise RuntimeError("gmail down")

    monkeypatch.setattr(sweep.gmail, "fetch_by_query", boom)
    assert sweep.replied_thread_ids("u1") == set()


# --- what the model is told ----------------------------------------------


def test_prompt_states_who_the_message_was_addressed_to():
    prompt = build_user_prompt(
        config=_config(),
        sender="invoicing@aws.com",
        subject="GST Invoice",
        body="Your invoice is ready.",
        to="pradeep@chronon.co.in",
        cc=ME,
    )
    assert "pradeep@chronon.co.in" in prompt
    assert "To:" in prompt
    assert "Cc:" in prompt


def test_prompt_identifies_which_address_is_the_user():
    """Listing recipients is useless if the model can't spot the user among them."""
    prompt = build_user_prompt(
        config=_config(),
        sender="invoicing@aws.com",
        subject="GST Invoice",
        body="body",
        to="pradeep@chronon.co.in",
        cc=ME,
    )
    assert ME in prompt
    assert "your address" in prompt.lower()


def test_prompt_omits_recipient_lines_when_unknown():
    """The webhook payload may not carry them; absent must not read as empty."""
    prompt = build_user_prompt(
        config=_config(account_email=None),
        sender="a@b.com",
        subject="s",
        body="body",
    )
    assert "To:" not in prompt
    assert "Cc:" not in prompt


def test_system_prompt_tells_the_model_to_decline_mail_not_addressed_to_them():
    text = build_system_prompt(_config()).lower()
    assert "addressed" in text
    assert "copied" in text or "cc" in text
