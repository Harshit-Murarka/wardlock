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


ENGINE = PACKAGE / "engine"


def test_the_engine_imports_nothing_that_can_do_io():
    """SPEC §5: the engine is pure. Checked in a fresh interpreter so other tests can't hide it."""
    import subprocess
    import sys

    code = (
        "import sys, wardlock.engine.decide, wardlock.engine.checks, wardlock.engine.policy, wardlock.engine.types\n"
        "banned = ('sqlalchemy', 'httpx', 'fastapi', 'pydantic', 'wardlock.db', 'wardlock.models',\n"
        "          'wardlock.config', 'wardlock.paypal.client', 'wardlock.handles', 'wardlock.sessions')\n"
        "print([m for m in banned if m in sys.modules])"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


def test_the_engine_never_reads_the_clock_itself():
    """The current time is always passed in, so every decision can be reproduced in a test."""
    forbidden = ("datetime.now", "utcnow", "time.time", "time.monotonic", "date.today")
    offenders = [
        f"{path.name}: {word}"
        for path in ENGINE.glob("*.py")
        for word in forbidden
        if word in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
