"""Settings that people type into a hosting dashboard must not crash startup (Render, P11)."""

import pytest

from wardlock.config import Settings

DEFAULT_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]


@pytest.mark.parametrize(
    ("typed", "hosts"),
    [
        ("", DEFAULT_HOSTS),  # left empty in the dashboard
        ("   ", DEFAULT_HOSTS),
        ('["wardlock.onrender.com"]', ["wardlock.onrender.com"]),
        ("wardlock.onrender.com", ["wardlock.onrender.com"]),
        ("a.onrender.com, b.onrender.com", ["a.onrender.com", "b.onrender.com"]),
        ("[“wardlock.onrender.com”]", ["wardlock.onrender.com"]),  # curly quotes from copy-paste
        ("[wardlock.onrender.com]", ["wardlock.onrender.com"]),  # brackets but no quotes
        ('"wardlock.onrender.com"', ["wardlock.onrender.com"]),
    ],
)
def test_allowed_hosts_accept_what_people_type(monkeypatch, typed, hosts):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", typed)
    assert Settings(_env_file=None).mcp_allowed_hosts == hosts


def test_allowed_origins_parse_the_same_way(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://wardlock.onrender.com")
    assert Settings(_env_file=None).mcp_allowed_origins == ["https://wardlock.onrender.com"]
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "")
    assert Settings(_env_file=None).mcp_allowed_origins[0] == "http://127.0.0.1:*"


def test_unset_means_localhost_only(monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    assert Settings(_env_file=None).mcp_allowed_hosts == DEFAULT_HOSTS
