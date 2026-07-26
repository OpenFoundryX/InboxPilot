"""The chat engine reuses ask.py's helpers, so they must be public and the
answer rules must be separable from the email-only sign-off."""

from services.commands import ask
from schemas.email import EmailSummary


def test_helpers_are_public():
    assert callable(ask.plan_queries)
    assert callable(ask.search_all)
    assert callable(ask.build_corpus)
    assert callable(ask.thread_link)


def test_answer_rules_have_no_signoff():
    # Chat renders a live transcript; a "— InboxOS" sign-off belongs to email only.
    assert "InboxOS" not in ask.ANSWER_RULES.split("Never invent")[-1]
    assert "sign-off" not in ask.ANSWER_RULES


def test_email_answer_prompt_still_signs_off():
    assert ask._ANSWER_SYS.startswith(ask.ANSWER_RULES)
    assert ask._ANSWER_SYS.rstrip().endswith("— InboxOS")


def test_thread_link_pins_the_account():
    link = ask.thread_link("18f2ab", "someone@example.com")
    assert link == "https://mail.google.com/mail/u/someone@example.com/#all/18f2ab"
    assert ask.thread_link(None, "a@b.com") == "(no link)"


def test_build_corpus_reports_attachment_counts():
    hits = [
        EmailSummary(
            id="1",
            thread_id="t1",
            sender="pradeep@example.com",
            subject="Mahindra users",
            body="here it is",
            attachments=["users.xlsx"],
        ),
        EmailSummary(id="2", thread_id="t2", sender="b@example.com", subject="No files"),
    ]
    corpus = ask.build_corpus(hits, "me@example.com")
    assert "Attachments: 1 file(s)" in corpus
    assert "Attachments: none" in corpus
    assert "#all/t1" in corpus
