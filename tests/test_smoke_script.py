"""The manual smoke script, run against a fake PayPal (no network, no .env)."""

import importlib.util
from pathlib import Path

import httpx
import pytest
import respx

from wardlock.paypal.client import PayPalClient

SANDBOX = "https://api-m.sandbox.paypal.com"
CAPTURE = "2GG279541U471931P"
REFUND = {"id": "1JU08902781691411", "status": "COMPLETED", "amount": {"value": "1.00", "currency_code": "USD"}}

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke_refund.py"


@pytest.fixture
def smoke(monkeypatch):
    """Load scripts/smoke_refund.py, with a test client instead of one built from .env."""
    spec = importlib.util.spec_from_file_location("smoke_refund", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def no_sleep(_seconds):
        pass

    def test_client(cls, settings=None, **kwargs):
        return PayPalClient(SANDBOX, "test-id", "test-secret", sleep=no_sleep)

    monkeypatch.setattr(PayPalClient, "from_settings", classmethod(test_client))
    return module


@pytest.fixture
def paypal():
    with respx.mock(base_url=SANDBOX, assert_all_called=False) as router:
        router.post("/v1/oauth2/token").respond(200, json={"access_token": "tok", "expires_in": 3600})
        router.get(f"/v2/payments/captures/{CAPTURE}").respond(
            200, json={"id": CAPTURE, "status": "COMPLETED", "amount": {"value": "26.48", "currency_code": "USD"}}
        )
        router.get(f"/v2/payments/refunds/{REFUND['id']}").respond(200, json=REFUND)
        yield router


def test_refunds_and_reads_back(smoke, paypal, capsys):
    refund_route = paypal.post(f"/v2/payments/captures/{CAPTURE}/refund").respond(201, json=REFUND)

    code = smoke.main(["--capture", CAPTURE, "--amount", "1.00", "--request-id", "req-123", "--yes"])

    assert code == 0
    assert refund_route.calls[0].request.headers["PayPal-Request-Id"] == "req-123"
    out = capsys.readouterr().out
    assert "Refund 1JU08902781691411: status=COMPLETED amount=1.00 USD" in out


def test_declining_the_prompt_sends_nothing(smoke, paypal, monkeypatch, capsys):
    refund_route = paypal.post(f"/v2/payments/captures/{CAPTURE}/refund")
    monkeypatch.setattr("builtins.input", lambda _question: "n")

    code = smoke.main(["--capture", CAPTURE, "--amount", "1.00"])

    assert code == 1
    assert refund_route.call_count == 0
    assert "nothing was sent" in capsys.readouterr().out


def test_unknown_outcome_tells_you_to_reuse_the_request_id(smoke, paypal, capsys):
    paypal.post(f"/v2/payments/captures/{CAPTURE}/refund").mock(side_effect=httpx.ReadTimeout)

    code = smoke.main(["--capture", CAPTURE, "--amount", "1.00", "--yes"])

    assert code == 1
    assert "same --request-id" in capsys.readouterr().err


def test_bad_amount_is_rejected_before_paypal(smoke, paypal, capsys):
    code = smoke.main(["--capture", CAPTURE, "--amount", "1.005", "--yes"])

    assert code == 2
    assert paypal.calls.call_count == 2  # token + capture read only; no refund was attempted
