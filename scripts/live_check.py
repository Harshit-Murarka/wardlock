"""Live sandbox check of the executor (P5), on the QA order Z-9002 only.

It runs real sandbox refunds through the whole pipeline: rules, reason check, saving the key
first, PayPal, ledger, and a hold that you approve. Run it from the repo root with .env filled:

    python scripts/seed_sandbox.py --only Z-9002     # once: creates and pays the QA order
    python scripts/live_check.py

Each money step asks first. Never point this at demo orders: they must stay unrefunded.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

from sqlalchemy import select

from wardlock.config import get_settings
from wardlock.db import get_engine, init_db, make_session_factory
from wardlock.engine.policy import load_policy
from wardlock.enums import ReasonCode
from wardlock.executor import Executor, RefundOutcome
from wardlock.handles import mint_handles
from wardlock.llm import LLMClient
from wardlock.models import AgentSession, LedgerEntry, Decision, Order
from wardlock.paypal.client import PayPalClient
from wardlock.reason_check import make_reason_checker
from wardlock.sessions import open_session

ORDER_REF = "Z-9002"  # the QA order; nothing else
TICKET = "Hi, the QA sticker in order Z-9002 arrived torn in half. Could I get a refund for it? Thanks."


def ask(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() == "y"


def show(label: str, outcome: RefundOutcome) -> None:
    print(f"  {label}: {outcome.status}  amount={outcome.amount}  codes={list(outcome.reason_codes)}")
    print(f"    agent hears: {outcome.message_for_agent}")


async def run() -> int:
    settings = get_settings()
    engine = get_engine()
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        order = db.scalar(select(Order).where(Order.order_ref == ORDER_REF))
    if order is None:
        print(f"{ORDER_REF} is not seeded yet. Run: python scripts/seed_sandbox.py --only {ORDER_REF}")
        return 2

    llm = LLMClient.from_settings(settings)
    print("LLM reason check:", "configured" if llm else "NOT configured, so every automatic refund will be HELD")
    async with PayPalClient.from_settings() as paypal:
        executor = Executor(factory, paypal, load_policy(), make_reason_checker(llm),
                            hold_timeout=timedelta(minutes=settings.hold_timeout_minutes))
        with factory() as db, db.begin():
            session_id = open_session(db, ticket_id="LIVE-CHECK", customer_id=order.customer_id,
                                      ticket_body=TICKET, ttl_minutes=30).session_id
        with factory() as db, db.begin():
            minted = mint_handles(db, db.get(AgentSession, session_id), goodwill_percentages=[10, 25])
        qa_order = next(o for o in minted if o.order_ref == ORDER_REF)
        options = {option.label: option.handle for option in qa_order.refund_options}
        print("Options Wardlock offers for", ORDER_REF, "->", list(options))

        held = []
        if "QA sticker x1" in options and ask("1) Refund the 2.00 sticker as DAMAGED (expected ALLOWED)?"):
            outcome = await executor.request_refund(session_id, [options["QA sticker x1"]], ReasonCode.DAMAGED,
                                                    "Sorry about the torn sticker.")
            show("sticker", outcome)
            held += [outcome] if outcome.status == "HELD" else []
        if "QA widget x1" in options and ask("2) Ask for the 20.00 widget as GOODWILL (expected HELD: wrong kind)?"):
            outcome = await executor.request_refund(session_id, [options["QA widget x1"]], ReasonCode.GOODWILL)
            show("widget", outcome)
            held += [outcome] if outcome.status == "HELD" else []
        for outcome in held:
            if ask(f"3) Approve the held {outcome.amount} refund as the merchant?"):
                result = await executor.approve_hold(outcome.hold_id, "live-check")
                print(f"  approval: executed={result.executed} status={result.status} -> {result.message}")

    if llm:
        await llm.aclose()
    with factory() as db:
        rows = db.execute(select(LedgerEntry.paypal_refund_id, LedgerEntry.amount_minor, LedgerEntry.status)
                          .join(Decision, LedgerEntry.decision_id == Decision.id)
                          .where(Decision.session_id == session_id)).all()
    print("Ledger rows from this run (PayPal refund id, cents, status):", rows or "none")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
