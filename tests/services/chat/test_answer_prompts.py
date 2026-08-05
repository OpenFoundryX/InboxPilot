"""The two answer surfaces format differently and must stay that way.

Email has no sources panel, so its answer carries the per-email bulleted link
list. Chat renders `SourceList` beneath every answer, so the same rules there
printed each email twice — once as a bullet with a link, once as a card. The
grounding rules are shared; only presentation forks.
"""

from services.chat.engine import CHAT_ANSWER_SYS
from services.commands.ask import ANSWER_RULES, GROUNDING_RULES

EMAIL_SYS = ANSWER_RULES


def test_both_surfaces_share_the_grounding_rules():
    for prompt in (EMAIL_SYS, CHAT_ANSWER_SYS):
        assert GROUNDING_RULES in prompt


def test_grounding_survives_in_both():
    for prompt in (EMAIL_SYS, CHAT_ANSWER_SYS):
        assert "grounded ONLY in the email excerpts" in prompt
        assert "Never invent emails" in prompt


def test_email_keeps_its_per_email_link_list():
    assert "mail.google.com" in EMAIL_SYS
    assert "Markdown link" in EMAIL_SYS


def test_chat_never_asks_for_a_link_list():
    """The duplication regression, stated directly."""
    # No worked example of a Gmail link to copy the shape from...
    assert "mail.google.com" not in CHAT_ANSWER_SYS
    # ...and an explicit prohibition rather than mere silence.
    assert "never\n  write a Markdown link or a URL" in CHAT_ANSWER_SYS
    assert "Do not produce a bulleted list of the emails" in CHAT_ANSWER_SYS


def test_chat_asks_for_prose_rather_than_bullets():
    assert "bullet" not in CHAT_ANSWER_SYS.lower().replace("bulleted list", "")
    assert "prose" in CHAT_ANSWER_SYS.lower()


def test_chat_says_the_cards_carry_the_list():
    """The model needs to know why it may omit the list, not just that it must."""
    lowered = CHAT_ANSWER_SYS.lower()
    assert "card" in lowered
    assert "source email" in lowered
    assert "do not rebuild it" in lowered


def test_chat_still_forbids_a_sign_off():
    assert "sign off" in CHAT_ANSWER_SYS.lower() or "sign-off" in CHAT_ANSWER_SYS.lower()


def test_chat_may_still_name_specific_emails():
    """A summary that refuses to name anything reads as evasive."""
    assert "subject" in CHAT_ANSWER_SYS.lower()
