"""Structural checks that keep the SPEC §0 invariants true as the codebase grows."""

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "wardlock"

# Invariant 1: only the PayPal client talks to PayPal. config.py may hold the URL constant.
ALLOWED_TO_NAME_PAYPAL_HOSTS = {PACKAGE / "paypal" / "client.py", PACKAGE / "config.py"}

# PayPal REST API hosts (sandbox and live, old and new names). Doc links such as
# developer.paypal.com are fine; only the API hosts can move money.
PAYPAL_API_HOST = re.compile(r"api(-m)?\.(sandbox\.)?paypal\.com", re.IGNORECASE)


def test_only_the_paypal_client_knows_paypal_api_hosts():
    offenders = [
        str(path.relative_to(PACKAGE))
        for path in PACKAGE.rglob("*.py")
        if path not in ALLOWED_TO_NAME_PAYPAL_HOSTS
        and PAYPAL_API_HOST.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
