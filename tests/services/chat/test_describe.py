from services.chat.describe import describe_action, describe_actions


def test_create_label():
    d = describe_action({"type": "create_label", "name": "Receipts"})
    assert d["type"] == "create_label"
    assert d["label"] == "Create Gmail label “Receipts”"
    assert d["detail"] == ""


def test_add_vip_lists_every_kind():
    d = describe_action(
        {"type": "add_vip", "domains": ["stripe.com"], "addresses": ["a@b.com"], "keywords": ["OTP"]}
    )
    assert d["label"] == "Add to your VIP list"
    assert "stripe.com" in d["detail"]
    assert "a@b.com" in d["detail"]
    assert "OTP" in d["detail"]


def test_destructive_rule_is_spelled_out():
    d = describe_action(
        {"type": "create_rule", "criteria": {"from": "spam@x.com"}, "trash": True,
         "apply_to_existing": True}
    )
    assert d["label"] == "Create a Gmail rule"
    assert "move to trash" in d["detail"]
    assert "existing mail" in d["detail"]


def test_set_reminder_shows_the_time():
    d = describe_action(
        {"type": "set_reminder", "remind_at": "2026-07-28T15:00:00+05:30", "title": "Call the bank"}
    )
    assert d["label"] == "Set a reminder: “Call the bank”"
    assert "2026-07-28T15:00:00+05:30" in d["detail"]


def test_unknown_type_still_renders():
    d = describe_action({"type": "teleport"})
    assert d["type"] == "teleport"
    assert d["label"] == "teleport"


def test_describe_actions_maps_all():
    out = describe_actions([{"type": "catch_up_now"}, {"type": "send_briefing_now"}])
    assert [o["label"] for o in out] == ["Send a catch-up summary now", "Send your briefing now"]
