import pytest

from schemas.email import EmailSummary
from services.chat.sources import email_source
from services.chat.sources.base import history_preamble


def _hit(**kw) -> EmailSummary:
    base = dict(
        id="m1",
        thread_id="t1",
        sender="pradeep@example.com",
        subject="Mahindra users",
        body="Attached the sheet",
        snippet="Attached the sheet",
        attachments=["users.xlsx"],
    )
    base.update(kw)
    return EmailSummary(**base)


@pytest.fixture
def retriever():
    return email_source.EmailRetriever(account_email="me@example.com")


async def test_retrieve_maps_hits_to_excerpts(monkeypatch, retriever):
    monkeypatch.setattr(email_source.ask, "plan_queries", lambda s, b: ["from:pradeep mahindra"])
    monkeypatch.setattr(email_source.ask, "search_all", lambda uid, q, per_query=6: [_hit()])

    got = await retriever.retrieve("user-1", "did pradeep send the sheet?", [])

    assert len(got) == 1
    ex = got[0]
    assert ex.kind == "email"
    assert ex.title == "Mahindra users"
    assert ex.sender == "pradeep@example.com"
    assert ex.attachment_count == 1
    assert ex.link == "https://mail.google.com/mail/u/me@example.com/#all/t1"
    assert ex.text == "Attached the sheet"


async def test_retrieve_returns_empty_when_no_queries_planned(monkeypatch, retriever):
    monkeypatch.setattr(email_source.ask, "plan_queries", lambda s, b: [])
    called = []

    def fake_search(*args, **kwargs):
        called.append(1)
        return []

    monkeypatch.setattr(email_source.ask, "search_all", fake_search)

    got = await retriever.retrieve("user-1", "hello there", [])

    assert got == []
    assert called == []


async def test_history_is_passed_to_the_planner(monkeypatch, retriever):
    seen = {}

    def fake_plan(subject, body):
        seen["body"] = body
        return ["q"]

    monkeypatch.setattr(email_source.ask, "plan_queries", fake_plan)
    monkeypatch.setattr(email_source.ask, "search_all", lambda uid, q, per_query=6: [])

    history = [
        {"role": "user", "content": "any invoices from AWS?"},
        {"role": "assistant", "content": "Yes, two."},
    ]
    await retriever.retrieve("user-1", "what about the second one?", history)

    # The planner must see the prior turns, or "the second one" is unresolvable.
    assert "any invoices from AWS?" in seen["body"]
    assert "what about the second one?" in seen["body"]


def test_history_preamble_is_empty_without_history():
    assert history_preamble([]) == ""
    assert history_preamble([{"role": "user", "content": ""}]) == ""


def test_excerpt_as_dict_is_json_safe():
    from services.chat.sources.base import Excerpt

    d = Excerpt(kind="email", title="t", sender="s", date="2026-07-27T00:00:00+00:00",
                link="http://x", text="body", ref_id="m1", thread_id="t1",
                attachment_count=2).as_dict()
    import json

    assert json.loads(json.dumps(d))["attachment_count"] == 2
