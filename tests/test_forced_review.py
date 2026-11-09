"""Eval config B (SPEC §14, "blanket approval"): every refund the rules would allow is HELD."""

from dataclasses import replace

from support import NOW, consistent, token_for

from wardlock.engine.codes import EXPLANATIONS, HOLD_CODES, Code
from wardlock.engine.decide import apply_forced_review
from wardlock.engine.policy import load_policy
from wardlock.engine.types import Decision
from wardlock.enums import ReasonCode, Verdict
from wardlock.executor import Executor
from wardlock.handles import mint_handles
from wardlock.sessions import resolve_session

ALLOW = Decision(Verdict.ALLOW, (), 400, "USD", "CAPA1001", ("h_1",), "1", needs_reason_check=False)


def test_forced_review_turns_only_an_allow_into_a_hold():
    held = apply_forced_review(ALLOW)
    assert (held.verdict, held.reason_codes) == (Verdict.HOLD, (Code.MANUAL_REVIEW_REQUIRED,))
    blocked = replace(ALLOW, verdict=Verdict.BLOCK, reason_codes=(Code.NOT_OWNER,))
    assert apply_forced_review(blocked) == blocked  # it can never unblock
    hold = replace(ALLOW, verdict=Verdict.HOLD, reason_codes=(Code.OVER_AUTO_LIMIT,))
    assert apply_forced_review(hold) == hold
    assert Code.MANUAL_REVIEW_REQUIRED in HOLD_CODES and Code.MANUAL_REVIEW_REQUIRED in EXPLANATIONS


async def test_config_b_holds_the_bell_and_sends_nothing(wl):
    executor = Executor(wl.factory, wl.paypal, load_policy(), consistent, clock=wl.clock, force_hold=True)
    token = token_for(wl)
    with wl.factory() as db, db.begin():
        session = resolve_session(db, token, NOW)
        orders = mint_handles(db, session, goodwill_percentages=[10, 25], now=NOW)
        bell = next(o.handle for order in orders for o in order.refund_options if o.label == "Bike bell x1")
        session_id = session.id

    outcome = await executor.request_refund(session_id, [bell], ReasonCode.DAMAGED)

    assert (outcome.status, outcome.reason_codes) == ("HELD", ("MANUAL_REVIEW_REQUIRED",))
    assert wl.fake.calls == []
