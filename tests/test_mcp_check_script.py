"""scripts/mcp_check.py, run in-process against the test app: it must pass, and catch a leak."""

import importlib.util
from pathlib import Path

import httpx2
from support import SERVICE_KEY

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "mcp_check.py"


def load_script():
    spec = importlib.util.spec_from_file_location("mcp_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_mcp_check_passes_against_a_working_server(wl, capsys):
    script = load_script()
    code = await script.run("http://localhost:8000", SERVICE_KEY, "cust_ana",
                            transport=httpx2.ASGITransport(app=wl.app))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "FAIL" not in out and "PASS: the MCP server works" in out
    assert wl.fake.calls == []  # it moves no money


async def test_the_mcp_check_fails_if_a_capture_id_leaks(wl, capsys, monkeypatch):
    import wardlock.mcp_tools as tools

    real = tools.RefundOptionOut

    def leaky(**fields):
        return real(**{**fields, "label": fields["label"] + " 8MC585209K746392H"})

    monkeypatch.setattr(tools, "RefundOptionOut", leaky)
    script = load_script()
    code = await script.run("http://localhost:8000", SERVICE_KEY, "cust_ana",
                            transport=httpx2.ASGITransport(app=wl.app))
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL no PayPal capture id is shown to the agent" in out
