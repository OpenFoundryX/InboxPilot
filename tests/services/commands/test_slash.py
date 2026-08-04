"""Every resolution rule, including the ones that look like edge cases and
are actually the common ways a user gets this wrong."""

from services.commands import slash


def test_prose_is_not_a_command():
    r = slash.resolve("show me my important emails")
    assert r.kind == slash.KIND_NONE
    assert r.command is None


def test_a_slash_later_in_the_message_is_prose():
    r = slash.resolve("what about /r/email as a source")
    assert r.kind == slash.KIND_NONE


def test_leading_whitespace_still_resolves():
    r = slash.resolve("   /vip add stripe.com")
    assert r.kind == slash.KIND_COMMAND
    assert r.command.name == "vip"
    assert r.args == "add stripe.com"


def test_bare_slash_is_help_not_unknown():
    assert slash.resolve("/").kind == slash.KIND_HELP


def test_help_is_help():
    assert slash.resolve("/help").kind == slash.KIND_HELP
    assert slash.resolve("/HELP").kind == slash.KIND_HELP
    assert slash.resolve("/help  ").kind == slash.KIND_HELP


def test_names_match_case_insensitively():
    assert slash.resolve("/VIP add x@y.com").command.name == "vip"


def test_args_keep_their_original_case():
    # Args carry addresses and label names; lowercasing them corrupts both.
    r = slash.resolve("/label create Receipts From AWS")
    assert r.args == "create Receipts From AWS"


def test_command_with_no_args_has_empty_args():
    r = slash.resolve("/catchup")
    assert r.kind == slash.KIND_COMMAND
    assert r.command.name == "catchup"
    assert r.args == ""


def test_trailing_whitespace_after_a_bare_command_is_not_args():
    assert slash.resolve("/catchup   ").args == ""


def test_unknown_command_reports_the_name_it_saw():
    r = slash.resolve("/sdfsd do a thing")
    assert r.kind == slash.KIND_UNKNOWN
    assert r.raw_name == "sdfsd"
    assert r.command is None


def test_empty_message_is_not_a_command():
    assert slash.resolve("").kind == slash.KIND_NONE
    assert slash.resolve("   ").kind == slash.KIND_NONE


def test_multiline_args_are_preserved():
    r = slash.resolve("/do archive x\nand star y")
    assert r.kind == slash.KIND_COMMAND
    assert r.args == "archive x\nand star y"
