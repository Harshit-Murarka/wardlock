"""T19 (SPEC §13): a scripted malicious agent moves zero money.

The attacker holds a real session for Ana and steals handles from a real session for Ben,
then tries everything in demo_agent/scripted_attacker.py against the in-process app.
"""

import json

import httpx2
from sqlalchemy import func, select
from support import SERVICE_KEY

from demo_agent.scripted_attacker import Attempt, AttackReport, Target, format_report, load_targets, run_attack
from wardlock.models import Decision, LedgerEntry

TARGETS = [
    Target("A-1001", "cust_ana", "CAPA1001", "26.48", "USD"),
    Target("A-1002", "cust_ana", "CAPA1002", "189.00", "USD"),
    Target("B-2001", "cust_ben", "CAPB2001", "189.00", "USD"),
]


async def test_t19_the_scripted_attacker_moves_no_money(wl):
    report = await run_attack("http://localhost:8000", SERVICE_KEY, attacker="cust_ana", victim="cust_ben",
                              targets=TARGETS, transport=httpx2.ASGITransport(app=wl.app))

    assert report.passed, format_report(report)
    assert wl.fake.calls == []  # PayPal was never asked to refund anything
    with wl.factory() as db:
        assert db.scalar(select(func.count()).select_from(LedgerEntry)) == 0
        audited = db.scalar(select(func.count()).select_from(Decision))
    assert audited == sum(a.status != "REFUSED" for a in report.attempts)  # every refund attempt is on record

    by_name = {a.name: a for a in report.attempts}
    assert len(report.attempts) >= 12
    assert by_name["forged handle"].reason_codes == ("UNKNOWN_HANDLE",)
    assert by_name["other customer's capture (B-2001), full amount"].reason_codes == ("NOT_OWNER",)
    assert by_name["own capture, no amount (PayPal would refund it ALL)"].reason_codes == ("MISSING_AMOUNT",)
    assert any("HANDLE_OTHER_SESSION" in a.reason_codes for a in report.attempts)
    assert by_name["forged handles, 50 at once"].status in ("BLOCKED", "REFUSED")


def test_an_allowed_attempt_or_a_refund_ref_fails_the_run():
    assert not AttackReport([Attempt("x", "request_refund", "ALLOWED")]).passed
    assert not AttackReport([Attempt("x", "request_refund", "BLOCKED", refund_ref="rf_1")]).passed
    assert not AttackReport([]).passed  # an attack that tried nothing proves nothing
    assert AttackReport([Attempt("x", "request_refund", "HELD")]).passed


def test_targets_come_from_the_seeding_state(tmp_path):
    orders = tmp_path / "orders.json"
    state = tmp_path / "state.json"
    orders.write_text(json.dumps([{"order_ref": "A-1001", "customer_id": "cust_ana"},
                                  {"order_ref": "B-2001", "customer_id": "cust_ben"}]), encoding="utf-8")
    state.write_text(json.dumps({"orders": {
        "A-1001": {"capture_id": "CAP1", "captured_minor": 2648, "currency": "USD"},
        "B-2001": {"capture_id": None, "captured_minor": None, "currency": "USD"},  # not paid yet: skipped
    }}), encoding="utf-8")
    assert load_targets(orders, state) == [Target("A-1001", "cust_ana", "CAP1", "26.48", "USD")]
