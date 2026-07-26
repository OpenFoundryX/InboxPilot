from services.chat.describe import describe_action, describe_actions


def test_create_label():
    d = describe_action({"type": "create_label", "name": "Receipts"})
    assert d["type"] == "create_label"
    assert d["label"] == "Create Gmail label “Receipts”"
    assert d["detail"] == ""


def test_add_vip_lists_every_kind():
    d = describe_action({
        "type": "add_vip",
        "domains": ["stripe.com"],
        "addresses": ["a@b.com"],
        "keywords": ["OTP"],
    })
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


def test_manage_routine_uses_human_names():
    d = describe_action({
        "type": "manage_routine",
        "routine": "chase_threads",
        "enabled": True,
    })
    assert d["label"] == "Turn on nudges for threads awaiting a reply"
    assert "_" not in d["label"]


def test_manage_routine_unknown_slug_renders():
    d = describe_action({
        "type": "manage_routine",
        "routine": "unknown_routine",
        "enabled": True,
    })
    assert d["type"] == "manage_routine"
    assert d["label"] == "Turn on unknown_routine"


def test_set_routine_with_custom_times():
    d = describe_action({
        "type": "set_routine",
        "delivery_mode": "custom_daily",
        "custom_times": ["13:00", "18:00"],
    })
    assert "Delivers at 13:00 and 18:00" in d["detail"]
    assert "[" not in d["detail"]
    assert "'" not in d["detail"]


def test_set_routine_with_dnd_enabled():
    d = describe_action({
        "type": "set_routine",
        "dnd_enabled": True,
        "dnd_start": "22:00",
        "dnd_end": "07:00",
    })
    assert "Quiet hours 22:00–07:00" in d["detail"]
    assert "True" not in d["detail"]
    assert "False" not in d["detail"]


def test_describe_actions_maps_all():
    out = describe_actions([{"type": "catch_up_now"}, {"type": "send_briefing_now"}])
    assert [o["label"] for o in out] == ["Send a catch-up summary now", "Send your briefing now"]
