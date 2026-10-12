"""Fixtures shared by several test files."""

import pytest
from support import in_process_wardlock


@pytest.fixture
async def wl():
    """The real app, in-process, with an in-memory DB and a fake PayPal (see support.py)."""
    async with in_process_wardlock() as env:
        yield env
