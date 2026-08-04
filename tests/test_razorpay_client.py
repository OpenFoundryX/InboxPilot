"""Razorpay client payload contracts — no network."""

from services.billing import razorpay_client


def test_create_customer_passes_fail_existing_as_string_zero(monkeypatch):
    """Integer 0 is treated as omitted by Razorpay (default fail); retries then
    400 with "Customer already exists for the merchant". The API wants "0"."""
    captured = {}

    class _Customers:
        def create(self, data):
            captured["data"] = data
            return {"id": "cust_existing"}

    class _Client:
        customer = _Customers()

    monkeypatch.setattr(razorpay_client, "_client", lambda: _Client())

    assert razorpay_client.create_customer(email="a@example.com", name="A") == "cust_existing"
    assert captured["data"]["fail_existing"] == "0"
    assert captured["data"]["email"] == "a@example.com"


def test_create_subscription_omits_start_at_when_none(monkeypatch):
    """`start_at=None` must not reach the SDK as a literal `null`.

    Razorpay's API treats a present-but-null `start_at` as invalid, not as
    "unset" — the key has to be absent from the payload for "start billing
    immediately on mandate authorisation" to take effect. This is what
    `start_checkout` relies on for a trial that's already consumed (or about
    to lapse before the request lands): passing a stale/past timestamp is
    exactly what Razorpay's `start_at cannot be lesser than the current time`
    rejects in production, and omitting it side-steps that entirely.
    """
    captured = {}

    class _Subscriptions:
        def create(self, data):
            captured["data"] = data
            return {"id": "sub_new", "status": "created"}

    class _Client:
        subscription = _Subscriptions()

    monkeypatch.setattr(razorpay_client, "_client", lambda: _Client())

    razorpay_client.create_subscription(
        plan_id="plan_pro_monthly",
        customer_id="cust_1",
        start_at=None,
        total_count=120,
        notes={"user_id": "u1"},
    )

    assert "start_at" not in captured["data"]
    assert captured["data"]["plan_id"] == "plan_pro_monthly"
    assert captured["data"]["customer_id"] == "cust_1"
    assert captured["data"]["total_count"] == 120


def test_create_subscription_includes_start_at_when_given(monkeypatch):
    """The future-trial path must be unaffected: a real `start_at` still
    reaches the SDK payload verbatim."""
    captured = {}

    class _Subscriptions:
        def create(self, data):
            captured["data"] = data
            return {"id": "sub_new", "status": "created"}

    class _Client:
        subscription = _Subscriptions()

    monkeypatch.setattr(razorpay_client, "_client", lambda: _Client())

    razorpay_client.create_subscription(
        plan_id="plan_pro_monthly",
        customer_id="cust_1",
        start_at=1893456000,
        total_count=120,
        notes={"user_id": "u1"},
    )

    assert captured["data"]["start_at"] == 1893456000
