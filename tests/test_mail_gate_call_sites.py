"""Every entry point into the mail pipeline must consult the gate.

`may_process_mail` being correct is worth nothing if a job forgets to ask it.
These tests pin each entry point: with the gate shut, the job must return
without touching Gmail at all. Each one fails if someone deletes the guard from
the job it covers.

The gate itself is patched rather than driven through the database — what is
under test here is "does this job ask, and does it obey", not the rule, which
`test_mail_access.py` covers.
"""

import pytest

from workers.jobs import classify_new_email as classify_mod
from workers.jobs import gmail_poll as poll_mod
from workers.jobs import sync_last_7_days as sync_mod

USER_ID = "8f14e45f-ce2f-4c65-9b3a-000000000001"


class _ExplodingGmail:
    """Any attribute access is a Google call the gate should have prevented."""

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise AssertionError(f"gate was shut but gmail.{name}() was called")

        return _boom


@pytest.fixture
def shut_gate(monkeypatch):
    def _shut(module):
        monkeypatch.setattr(module, "mail_gate_open", lambda user_id: False)
        monkeypatch.setattr(module, "gmail", _ExplodingGmail(), raising=False)

    return _shut


@pytest.fixture
def open_gate(monkeypatch):
    def _open(module):
        monkeypatch.setattr(module, "mail_gate_open", lambda user_id: True)

    return _open


# --------------------------------------------------------------------------
# The onboarding sync — the job the OAuth callback used to fire immediately
# --------------------------------------------------------------------------


def test_sync_last_7_days_does_nothing_when_gated(shut_gate):
    shut_gate(sync_mod)

    result = sync_mod.sync_last_7_days(USER_ID)

    assert result == {"skipped": "gated"}


def test_sync_last_7_days_creates_no_labels_when_gated(shut_gate):
    """The visible harm: InboxPilot labels appearing in an unpaid mailbox."""
    shut_gate(sync_mod)

    sync_mod.sync_last_7_days(USER_ID)  # _ExplodingGmail asserts if touched


# --------------------------------------------------------------------------
# The poller — push and the reconciliation beat both land here
# --------------------------------------------------------------------------


def test_poll_user_skips_when_gated(shut_gate, monkeypatch):
    """Gated before the Redis lock, so this needs no broker to run."""
    shut_gate(poll_mod)
    monkeypatch.setattr(poll_mod, "_poll", lambda user_id: pytest.fail("polled while gated"))

    assert poll_mod.poll_user(USER_ID) == {"skipped": "gated"}


def test_install_watch_refuses_when_gated(shut_gate):
    """A watch is a standing subscription to someone's mail. Don't install one."""
    shut_gate(poll_mod)

    assert poll_mod.install_watch(USER_ID) is False


# --------------------------------------------------------------------------
# The classifier — work can already be queued when a trial lapses mid-flight
# --------------------------------------------------------------------------


def test_classify_new_email_skips_when_gated(shut_gate, monkeypatch):
    shut_gate(classify_mod)
    monkeypatch.setattr(
        classify_mod,
        "classify_and_label",
        lambda *a, **k: pytest.fail("classified while gated"),
    )

    result = classify_mod.classify_new_email(USER_ID, "msg-1")

    assert result == {"skipped": "gated", "user_id": USER_ID, "message_id": "msg-1"}


def test_classify_new_email_proceeds_when_gate_is_open(open_gate, monkeypatch):
    """The guard must not become an unconditional off switch."""
    open_gate(classify_mod)
    monkeypatch.setattr(classify_mod, "classify_and_label", lambda *a, **k: "fyi")

    result = classify_mod.classify_new_email(USER_ID, "msg-1")

    assert result["label"] == "fyi"
