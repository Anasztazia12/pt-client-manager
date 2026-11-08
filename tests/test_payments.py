"""Stripe payment flow, with Stripe's API mocked and webhook signatures computed for real."""
import hashlib
import hmac
import json
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_db_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_db_dir, "test.db").replace("\\", "/")
os.environ["DEMO_MODE"] = ""

from app import app as flask_app  # noqa: E402
from models import db, Client, Payment  # noqa: E402

WEBHOOK_SECRET = "whsec_test"


@pytest.fixture
def client():
    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        STRIPE_SECRET_KEY="sk_test_x",
        STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET,
        STRIPE_CURRENCY="gbp",
        PUBLIC_URL="https://pt.example",
    )
    with flask_app.app_context():
        Payment.query.delete()
        Client.query.delete()
        db.session.add(Client(id=1, name="Emma Clarke", email="emma@example.com"))
        db.session.commit()
    yield flask_app.test_client()


def stripe_session(session_id="cs_test_1", payment_status="unpaid"):
    return SimpleNamespace(
        id=session_id,
        url="https://checkout.stripe.test/" + session_id,
        expires_at=int(time.time()) + 3600,
        payment_status=payment_status,
    )


def create_payment(http, amount="45.50"):
    with patch("stripe.checkout.Session.create", return_value=stripe_session()) as create:
        response = http.post("/clients/1/payments", data={"description": "4 sessions", "amount": amount})
    return response, create


def send_webhook(http, event_type, session, secret=WEBHOOK_SECRET):
    payload = json.dumps({"type": event_type, "data": {"object": session}})
    stamp = int(time.time())
    signature = hmac.new(secret.encode(), f"{stamp}.{payload}".encode(), hashlib.sha256).hexdigest()
    return http.post("/stripe/webhook", data=payload, headers={"Stripe-Signature": f"t={stamp},v1={signature}"})


def status():
    with flask_app.app_context():
        return Payment.query.one().status


def test_create_sends_correct_checkout_to_stripe(client):
    response, create = create_payment(client)
    assert response.status_code == 302
    params = create.call_args.kwargs
    assert params["line_items"][0]["price_data"]["unit_amount"] == 4550
    assert params["line_items"][0]["price_data"]["currency"] == "gbp"
    assert params["customer_email"] == "emma@example.com"
    assert params["success_url"] == "https://pt.example/payments/result?session_id={CHECKOUT_SESSION_ID}"
    assert status() == "pending"
    assert b"Copy link" in client.get("/clients/1").data
    assert b"45.50" in client.get("/payments").data


@pytest.mark.parametrize("amount", ["", "0", "-5", "abc"])
def test_invalid_amount_never_reaches_stripe(client, amount):
    _, create = create_payment(client, amount)
    create.assert_not_called()


def test_webhook_marks_paid_and_repeat_is_harmless(client):
    create_payment(client)
    session = {"id": "cs_test_1", "payment_status": "paid"}
    assert send_webhook(client, "checkout.session.completed", session).status_code == 200
    assert status() == "paid"
    assert send_webhook(client, "checkout.session.expired", session).status_code == 200
    assert status() == "paid"


def test_webhook_rejects_bad_signature(client):
    create_payment(client)
    session = {"id": "cs_test_1", "payment_status": "paid"}
    assert send_webhook(client, "checkout.session.completed", session, secret="whsec_wrong").status_code == 400
    assert status() == "pending"


def test_delayed_payment_waits_then_succeeds_or_fails(client):
    create_payment(client)
    session = {"id": "cs_test_1", "payment_status": "unpaid"}
    send_webhook(client, "checkout.session.completed", session)
    assert status() == "pending"
    send_webhook(client, "checkout.session.async_payment_failed", session)
    assert status() == "failed"


def test_result_page_checks_stripe_without_webhook(client):
    create_payment(client)
    with patch("stripe.checkout.Session.retrieve", return_value=stripe_session(payment_status="paid")):
        response = client.get("/payments/result?session_id=cs_test_1")
    assert b"Payment received" in response.data
    assert status() == "paid"


def test_result_page_unknown_session_is_404(client):
    assert client.get("/payments/result?session_id=nope").status_code == 404


def test_without_stripe_key_links_are_off(client):
    flask_app.config["STRIPE_SECRET_KEY"] = ""
    _, create = create_payment(client)
    create.assert_not_called()
    assert b"Stripe is not set up yet" in client.get("/payments").data
