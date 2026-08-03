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
