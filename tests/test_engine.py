"""The decision engine (SPEC §5): pure, table-driven tests for T1–T8 and T11–T13."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from wardlock.engine.codes import BLOCK_CODES, EXPLANATIONS, HOLD_CODES, Code
from wardlock.engine.decide import apply_reason_check, decide
from wardlock.engine.policy import load_policy
from wardlock.engine.types import DecisionInputs, HandleView, OrderView, RefundRequest, SessionView
from wardlock.enums import HandleKind, HandleState, ReasonCode, SessionStatus, Verdict

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
POLICY = load_policy()  # the real policy.yaml: limit 50.00 USD, 2 refunds per session
B, H, A = Verdict.BLOCK, Verdict.HOLD, Verdict.ALLOW
LINE, SHIP, GW, FULL = HandleKind.LINE_ITEM, HandleKind.SHIPPING, HandleKind.GOODWILL_PCT, HandleKind.FULL_REMAINING

ANA = SessionView("ses_ana", "cust_ana", SessionStatus.ACTIVE, NOW + timedelta(minutes=20),
                  refunds_so_far=0, auto_refunded_minor=0)


def handle(handle_id, kind, amount, capture="CAP_A1001", session="ses_ana", state=HandleState.AVAILABLE, currency="USD"):
    return HandleView(handle_id, session, capture, kind, state, amount, currency)


def order(capture, customer, remaining, derivable, *, age_days=5, currency="USD"):
    return OrderView(capture, customer, currency, NOW - timedelta(days=age_days), remaining, derivable)


# Ana's A-1001 options exactly as P3 mints them, plus a few others.
HANDLES = {h.handle_id: h for h in [
    handle("h_bell", LINE, 400), handle("h_cage", LINE, 1598), handle("h_ship", SHIP, 650),
    handle("h_gw10", GW, 200), handle("h_gw25", GW, 500), handle("h_full", FULL, 2648),
    handle("h_pump", LINE, 2499, capture="CAP_A1003"),
    handle("h_helmet_full", FULL, 18900, capture="CAP_A1002"),
    handle("h_fifty", FULL, 5000, capture="CAP_FIFTY"),
    handle("h_fifty_and_a_cent", FULL, 5001, capture="CAP_FIFTY_1"),
    handle("h_other", LINE, 400, session="ses_other"),  # minted in someone else's conversation
]}
ORDERS = {o.capture_id: o for o in [
    order("CAP_A1001", "cust_ana", 2648, {400: {LINE}, 1598: {LINE}, 650: {SHIP}, 200: {GW}, 500: {GW}, 2648: {FULL}}),
    order("CAP_A1003", "cust_ana", 2999, {2499: {LINE}, 500: {SHIP}, 2999: {FULL}}),
    order("CAP_A1002", "cust_ana", 18900, {17900: {LINE}, 1000: {SHIP}, 18900: {FULL}}),
    order("CAP_FIFTY", "cust_ana", 5000, {5000: {FULL}}),
    order("CAP_FIFTY_1", "cust_ana", 5001, {5001: {FULL}}),
    order("CAP_B2001", "cust_ben", 18900, {17900: {LINE}, 18900: {FULL}}),  # the injection target
]}


def run(handles=("h_bell",), reason=ReasonCode.DAMAGED, *, session=ANA, handle_map=HANDLES,
        orders=ORDERS, errors=(), now=NOW, policy=POLICY):
    request = RefundRequest("request_refund", reason, tuple(handles))
    inputs = DecisionInputs(session, {k: v for k, v in handle_map.items() if k in handles}, orders, errors)
    return decide(request, inputs, policy, now)


def run_raw(capture="CAP_A1001", amount=Decimal("4.00"), currency="USD", reason=ReasonCode.DAMAGED,
            *, session=ANA, orders=ORDERS, handle_ids=()):
    request = RefundRequest("request_refund_raw", reason, handle_ids, raw_capture_id=capture,
                            raw_amount=amount, raw_currency=currency)
    return decide(request, DecisionInputs(session, {}, orders), POLICY, NOW)


def with_handle(handle_id, **changes):
    return {**HANDLES, handle_id: replace(HANDLES[handle_id], **changes)}


def with_order(capture, **changes):
    return {**ORDERS, capture: replace(ORDERS[capture], **changes)}


# ---------------------------------------------------------------- request_refund (handles)

HANDLE_CASES = [
    pytest.param(dict(), A, [], id="bell for DAMAGED is allowed"),
    pytest.param(dict(handles=["h_other"]), B, [Code.HANDLE_OTHER_SESSION], id="T1 handle from another session"),
    pytest.param(dict(handles=["h_made_up"]), B, [Code.UNKNOWN_HANDLE], id="T2 invented handle"),
    pytest.param(dict(handles=[]), B, [Code.UNKNOWN_HANDLE], id="T2 no handles at all"),
    pytest.param(dict(handles=["h_bell", "h_made_up"]), B, [Code.UNKNOWN_HANDLE], id="T2 one real and one invented"),
    pytest.param(dict(handle_map=with_handle("h_bell", state=HandleState.CONSUMED)), B,
                 [Code.HANDLE_ALREADY_USED], id="T3 consumed handle"),
    pytest.param(dict(handle_map=with_handle("h_bell", state=HandleState.RESERVED)), B,
                 [Code.HANDLE_ALREADY_USED], id="T3 reserved handle"),
    pytest.param(dict(handle_map=with_handle("h_bell", state=HandleState.EXPIRED)), B,
                 [Code.HANDLE_ALREADY_USED], id="T3 expired handle"),
    pytest.param(dict(orders=with_order("CAP_A1001", customer_id="cust_ben")), B, [Code.NOT_OWNER],
                 id="T4 order reassigned to another customer after minting"),
    pytest.param(dict(handles=["h_bell", "h_pump"]), B, [Code.INVALID_COMBINATION], id="T5 handles from two captures"),
    pytest.param(dict(handles=["h_gw10", "h_gw25"], reason=ReasonCode.GOODWILL), B, [Code.INVALID_COMBINATION],
                 id="two goodwill gestures"),
    pytest.param(dict(handles=["h_bell", "h_bell"]), B, [Code.INVALID_COMBINATION],
                 id="the same handle twice is never counted twice"),
    pytest.param(dict(handles=["h_full", "h_bell"], reason=ReasonCode.NOT_RECEIVED), B,
                 [Code.INVALID_COMBINATION, Code.EXCEEDS_REFUNDABLE, Code.REASON_KIND_MISMATCH],
                 id="full refund combined with an item, every code collected"),
    pytest.param(dict(orders=with_order("CAP_A1001", remaining_minor=300)), B, [Code.EXCEEDS_REFUNDABLE],
                 id="more than what is left to refund"),
    pytest.param(dict(orders=with_order("CAP_A1001", captured_at=NOW - timedelta(days=181))), B,
                 [Code.REFUND_WINDOW_EXPIRED], id="capture older than 180 days"),
    pytest.param(dict(orders=with_order("CAP_A1001", captured_at=NOW - timedelta(days=180))), A, [],
                 id="capture exactly 180 days old is still allowed"),
    pytest.param(dict(reason=ReasonCode.GOODWILL), H, [Code.REASON_KIND_MISMATCH], id="T6 GOODWILL with a line item"),
    pytest.param(dict(handles=["h_helmet_full"], reason=ReasonCode.NOT_RECEIVED), H, [Code.OVER_AUTO_LIMIT],
                 id="T7 the 189.00 helmet is above the limit"),
    pytest.param(dict(handles=["h_fifty"], reason=ReasonCode.NOT_RECEIVED), A, [], id="T7 exactly 50.00 is allowed"),
    pytest.param(dict(handles=["h_fifty_and_a_cent"], reason=ReasonCode.NOT_RECEIVED), H, [Code.OVER_AUTO_LIMIT],
                 id="T7 50.01 is held"),
    pytest.param(dict(handle_map=with_handle("h_bell", currency="EUR"),
                      orders=with_order("CAP_A1001", currency="EUR")), H, [Code.OVER_AUTO_LIMIT],
                 id="a non-USD refund is never compared with the USD limit"),
    pytest.param(dict(session=replace(ANA, refunds_so_far=2)), H, [Code.SESSION_VELOCITY],
                 id="T8 third refund in one session"),
    pytest.param(dict(session=replace(ANA, refunds_so_far=1)), A, [], id="T8 second refund is still allowed"),
    pytest.param(dict(errors=("ledger read failed",)), H, [Code.DEPENDENCY_UNAVAILABLE], id="a dependency failed"),
    pytest.param(dict(orders={}), H, [Code.DEPENDENCY_UNAVAILABLE], id="order data is missing"),
    pytest.param(dict(session=replace(ANA, expires_at=NOW)), B, [Code.SESSION_INVALID], id="expired session"),
    pytest.param(dict(session=replace(ANA, status=SessionStatus.CLOSED)), B, [Code.SESSION_INVALID],
                 id="closed session"),
    pytest.param(dict(session=None), B, [Code.SESSION_INVALID], id="no session"),
    pytest.param(dict(handles=["h_other"], reason=ReasonCode.GOODWILL), B,
                 [Code.HANDLE_OTHER_SESSION, Code.REASON_KIND_MISMATCH], id="BLOCK and HOLD codes together give BLOCK"),
]


@pytest.mark.parametrize(("kwargs", "verdict", "codes"), HANDLE_CASES)
def test_request_refund(kwargs, verdict, codes):
    decision = run(**kwargs)
    assert (decision.verdict, list(decision.reason_codes)) == (verdict, codes)


def test_an_allowed_refund_carries_what_to_execute():
    decision = run(["h_cage", "h_ship"], ReasonCode.WRONG_ITEM)
    assert decision.verdict == A
    assert (decision.amount_minor, decision.currency, decision.capture_id) == (2248, "USD", "CAP_A1001")
    assert decision.handle_ids == ("h_cage", "h_ship")
    assert decision.policy_version == "1"
    assert decision.needs_reason_check is True  # the LLM check still has to run (#12)


def test_more_than_ten_handles_is_an_invalid_combination():
    many = [f"h_{i}" for i in range(11)]
    decision = run(many, handle_map={h: handle(h, LINE, 1) for h in many})
    assert decision.verdict == B and Code.INVALID_COMBINATION in decision.reason_codes


def test_request_refund_never_accepts_a_raw_amount():
    """Invariant 2, enforced inside the engine too."""
    request = RefundRequest("request_refund", ReasonCode.DAMAGED, ("h_bell",), raw_amount=Decimal("189.00"))
    decision = decide(request, DecisionInputs(ANA, {"h_bell": HANDLES["h_bell"]}, ORDERS), POLICY, NOW)
    assert decision.verdict == B and Code.INVALID_COMBINATION in decision.reason_codes


def test_a_reason_left_out_of_the_policy_is_held():
    policy = replace(POLICY, reason_allows={k: v for k, v in POLICY.reason_allows.items() if k != ReasonCode.DAMAGED})
    decision = run(policy=policy)
    assert (decision.verdict, decision.reason_codes) == (H, (Code.REASON_KIND_MISMATCH,))


def test_naive_now_is_refused():
    with pytest.raises(ValueError):
        run(now=datetime(2026, 10, 9, 12, 0))


def test_decisions_are_deterministic():
    assert run(["h_cage", "h_ship"], ReasonCode.WRONG_ITEM) == run(["h_cage", "h_ship"], ReasonCode.WRONG_ITEM)


# ---------------------------------------------------------------- request_refund_raw (compat, SPEC §3.4)

COMPAT_CASES = [
    pytest.param(dict(), A, [], id="4.00 for DAMAGED matches the bell"),
    pytest.param(dict(amount=Decimal("26.48"), reason=ReasonCode.NOT_RECEIVED), A, [], id="full remaining"),
    pytest.param(dict(capture="CAP_B2001"), B, [Code.NOT_OWNER], id="T11 another customer's capture"),
    pytest.param(dict(capture="CAP_NOPE"), B, [Code.NOT_OWNER], id="unknown capture looks the same as not yours"),
    pytest.param(dict(capture=None), B, [Code.NOT_OWNER], id="no capture at all"),
    pytest.param(dict(amount=None), B, [Code.MISSING_AMOUNT], id="T12 no amount"),
    pytest.param(dict(amount=Decimal("3.00")), H, [Code.AMOUNT_NOT_DERIVABLE], id="T13 amount not derivable"),
    pytest.param(dict(reason=ReasonCode.NOT_RECEIVED), H, [Code.REASON_KIND_MISMATCH],
                 id="4.00 is a line item, which NOT_RECEIVED does not allow"),
    pytest.param(dict(currency="EUR"), B, [Code.INVALID_AMOUNT], id="wrong currency"),
    pytest.param(dict(amount=Decimal("-1.00")), B, [Code.INVALID_AMOUNT], id="negative amount"),
    pytest.param(dict(amount=Decimal("0")), B, [Code.INVALID_AMOUNT], id="zero amount"),
    pytest.param(dict(amount=Decimal("4.001")), B, [Code.INVALID_AMOUNT], id="too many decimals"),
    pytest.param(dict(amount=4.0), B, [Code.INVALID_AMOUNT], id="a float is never an amount"),
    pytest.param(dict(amount=Decimal("30.00")), B, [Code.EXCEEDS_REFUNDABLE, Code.AMOUNT_NOT_DERIVABLE],
                 id="more than remaining and not an option"),
    pytest.param(dict(capture="CAP_A1002", amount=Decimal("189.00"), reason=ReasonCode.NOT_RECEIVED), H,
                 [Code.OVER_AUTO_LIMIT], id="the 189.00 helmet through compat is held too"),
    pytest.param(dict(handle_ids=("h_bell",)), B, [Code.INVALID_COMBINATION], id="compat takes no handles"),
    pytest.param(dict(session=replace(ANA, refunds_so_far=2)), H, [Code.SESSION_VELOCITY], id="velocity applies"),
]


@pytest.mark.parametrize(("kwargs", "verdict", "codes"), COMPAT_CASES)
def test_request_refund_raw(kwargs, verdict, codes):
    decision = run_raw(**kwargs)
    assert (decision.verdict, list(decision.reason_codes)) == (verdict, codes)


def test_compat_never_records_another_customers_capture_as_the_target():
    decision = run_raw(capture="CAP_B2001")
    assert decision.capture_id is None and decision.amount_minor is None


# ---------------------------------------------------------------- reason check (#12, invariant 6)


def test_a_consistent_reason_check_keeps_the_allow():
    decision = apply_reason_check(run(), True)
    assert (decision.verdict, decision.reason_codes, decision.needs_reason_check) == (A, (), False)


@pytest.mark.parametrize("result", [False, None, "yes", 1])
def test_anything_but_true_turns_allow_into_hold(result):
    """None covers the T16 timeout. A truthy value that isn't exactly True still holds."""
    decision = apply_reason_check(run(), result)
    assert (decision.verdict, decision.reason_codes) == (H, (Code.REASON_NOT_SUPPORTED_BY_TICKET,))
    assert decision.needs_reason_check is False


@pytest.mark.parametrize("kwargs", [dict(handles=["h_other"]), dict(reason=ReasonCode.GOODWILL)])
def test_the_reason_check_can_never_relax_a_block_or_a_hold(kwargs):
    before = run(**kwargs)
    after = apply_reason_check(before, True)
    assert (after.verdict, after.reason_codes) == (before.verdict, before.reason_codes)
    assert after.verdict != A


def test_only_an_allow_needs_the_reason_check():
    assert run().needs_reason_check is True
    assert run(handles=["h_other"]).needs_reason_check is False
    assert run(reason=ReasonCode.GOODWILL).needs_reason_check is False


# ---------------------------------------------------------------- codes


def test_every_code_is_exactly_one_of_block_or_hold_and_is_explained():
    assert BLOCK_CODES | HOLD_CODES == set(Code)
    assert not BLOCK_CODES & HOLD_CODES
    assert set(EXPLANATIONS) == set(Code)
    assert Code.AMOUNT_NOT_DERIVABLE in HOLD_CODES and Code.MISSING_AMOUNT in BLOCK_CODES  # SPEC §5


def test_a_repeated_handle_is_never_summed_twice():
    decision = run(["h_bell", "h_bell"])
    assert decision.verdict == B
    assert decision.amount_minor == 400  # counted once, even though the request is blocked


def test_a_compat_amount_matching_several_kinds_needs_only_one_allowed():
    # 2.00 is both a 2.00 sticker (LINE_ITEM) and a 10% goodwill: GOODWILL allows one of them.
    orders = with_order("CAP_A1001", derivable={200: frozenset({LINE, GW})})
    decision = run_raw(amount=Decimal("2.00"), reason=ReasonCode.GOODWILL, orders=orders)
    assert (decision.verdict, decision.reason_codes) == (A, ())


def test_missing_order_data_is_always_reported_even_alongside_other_codes():
    """A merchant reviewing this HOLD must see that the order data was missing, not just the
    reason mismatch, or they might approve something nobody could check."""
    decision = run(reason=ReasonCode.GOODWILL, orders={})
    assert decision.verdict == H
    assert decision.reason_codes == (Code.REASON_KIND_MISMATCH, Code.DEPENDENCY_UNAVAILABLE)


# ---------------------------------------------------------------- the limit covers the whole session


def test_small_refunds_cannot_add_up_past_the_limit():
    """Salami slicing: 49.99 was already refunded automatically, so the 4.00 bell must be held."""
    decision = run(session=replace(ANA, refunds_so_far=1, auto_refunded_minor=4999))
    assert (decision.verdict, decision.reason_codes) == (H, (Code.OVER_AUTO_LIMIT,))


def test_reaching_exactly_the_limit_over_the_session_is_allowed():
    decision = run(session=replace(ANA, refunds_so_far=1, auto_refunded_minor=4600))  # 46.00 + 4.00 = 50.00
    assert (decision.verdict, decision.reason_codes) == (A, ())


def test_the_session_total_applies_to_compat_too():
    decision = run_raw(session=replace(ANA, refunds_so_far=1, auto_refunded_minor=4999))
    assert (decision.verdict, decision.reason_codes) == (H, (Code.OVER_AUTO_LIMIT,))


# ---------------------------------------------------------------- approval re-check (SPEC §6.9)

from wardlock.engine.recheck import HoldFacts, recheck_approval  # noqa: E402


def hold_facts(**changes):
    facts = HoldFacts(session_customer_id="cust_ana", amount_minor=18900, order=ORDERS["CAP_A1002"],
                      own_reserved_minor=18900, handle_states=(HandleState.RESERVED,))
    return replace(facts, **changes)


def test_a_still_valid_hold_passes_the_recheck():
    # remaining is 18900 here, and the hold's own 18900 is added back, so 18900 still fits.
    assert recheck_approval(hold_facts(), NOW) == ()


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        (dict(order=replace(ORDERS["CAP_A1002"], customer_id="cust_ben")), Code.NOT_OWNER),
        (dict(handle_states=(HandleState.CONSUMED,)), Code.HANDLE_ALREADY_USED),
        (dict(handle_states=()), Code.HANDLE_ALREADY_USED),
        (dict(order=replace(ORDERS["CAP_A1002"], remaining_minor=0), own_reserved_minor=100), Code.EXCEEDS_REFUNDABLE),
        (dict(order=replace(ORDERS["CAP_A1002"], captured_at=NOW - timedelta(days=181))), Code.REFUND_WINDOW_EXPIRED),
    ],
)
def test_the_recheck_refuses_when_something_changed(changes, code):
    assert code in recheck_approval(hold_facts(**changes), NOW)


def test_missing_order_data_means_wait():
    assert recheck_approval(hold_facts(order=None), NOW) == (Code.DEPENDENCY_UNAVAILABLE,)
