"""Gmail permalinks.

The regression these guard against: putting the account's email address in the
`/mail/u/<...>/` path slot. Google stopped resolving that form once a fragment
is present, so every "View email" link in chat returned a hard 404 — the
account-disambiguation intent was right, the mechanism was not.
"""

from services.commands.ask import thread_link

THREAD = "19d4b1dc0ba05852"
EMAIL = "nilesh@chronon.co.in"


def test_no_thread_id_has_no_link():
    assert thread_link(None, EMAIL) == "(no link)"
    assert thread_link("", EMAIL) == "(no link)"


def test_email_never_lands_in_the_path_slot():
    """The 404 regression, stated directly."""
    assert f"/mail/u/{EMAIL}/" not in thread_link(THREAD, EMAIL)


def test_path_slot_is_always_a_numeric_index():
    for account in (EMAIL, None, "", "someone+tag@example.com"):
        assert "/mail/u/0/" in thread_link(THREAD, account)


def test_thread_id_is_the_fragment():
    assert thread_link(THREAD, EMAIL).endswith(f"#all/{THREAD}")


def test_account_is_passed_as_authuser():
    assert "?authuser=nilesh%40chronon.co.in" in thread_link(THREAD, EMAIL)


def test_authuser_precedes_the_fragment():
    """Query before fragment, or the browser never sends it."""
    link = thread_link(THREAD, EMAIL)
    assert link.index("authuser") < link.index("#")


def test_no_account_means_no_authuser():
    assert thread_link(THREAD, None) == f"https://mail.google.com/mail/u/0/#all/{THREAD}"
    assert thread_link(THREAD, "") == f"https://mail.google.com/mail/u/0/#all/{THREAD}"


def test_special_characters_in_the_address_are_encoded():
    link = thread_link(THREAD, "a+b@example.com")
    # A bare "+" in a query value decodes to a space and loses the sub-address.
    assert "a%2Bb%40example.com" in link


def test_full_link_shape():
    assert thread_link(THREAD, EMAIL) == (
        f"https://mail.google.com/mail/u/0/?authuser=nilesh%40chronon.co.in#all/{THREAD}"
    )
