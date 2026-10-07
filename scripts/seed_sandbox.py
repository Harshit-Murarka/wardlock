"""Seed the PayPal sandbox with demo orders (SPEC §7). Run it from the repo root.

    python scripts/seed_sandbox.py --dry-run          # check seed files, print payloads; no PayPal, no DB
    python scripts/seed_sandbox.py --only Z-9001      # seed one cheap order first, as a check
    python scripts/seed_sandbox.py                    # seed everything (paid with a sandbox test card)
    python scripts/seed_sandbox.py --approve-in-browser   # fallback: approve each order in a browser
    python scripts/seed_sandbox.py --restore-db       # rebuild DB rows from the state file; no PayPal

It is safe to run again at any point: finished orders are skipped, and unfinished ones
pick up where they left off.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError

from wardlock.db import get_engine, init_db, make_session_factory
from wardlock.paypal.client import PayPalClient
from wardlock.paypal.errors import PayPalError, PayPalRequestError, PayPalUnavailable
from wardlock.seed import (
    SandboxCard,
    SeedData,
    SeedError,
    SeedReport,
    build_order_payload,
    load_seed,
    restore_db,
    run_seed,
    select_orders,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the PayPal sandbox with demo orders.")
    parser.add_argument("--customers", type=Path, default=Path("seed/customers.json"))
    parser.add_argument("--orders", type=Path, default=Path("seed/orders.json"))
    parser.add_argument("--state", type=Path, default=Path("seed/sandbox_state.json"))
    parser.add_argument("--only", action="append", metavar="ORDER_REF", help="seed only this order (repeatable)")
    parser.add_argument("--approve-in-browser", action="store_true",
                        help="pay by approving each order in a browser instead of with a test card")
    parser.add_argument("--card-number", help="a different sandbox test card (dashboard > Credit card generator)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="validate and print payloads only")
    mode.add_argument("--restore-db", action="store_true", help="rebuild DB rows from the state file")
    return parser.parse_args(argv)


def choose_card(args: argparse.Namespace) -> SandboxCard | None:
    if args.approve_in_browser:
        return None
    return SandboxCard(number=args.card_number) if args.card_number else SandboxCard()


def masked(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy of a payload with card details hidden. They are public test values, but
    printing card numbers is a habit worth not having."""
    shown = copy.deepcopy(payload)
    card = shown.get("payment_source", {}).get("card")
    if card:
        card["number"] = "*" * (len(card["number"]) - 4) + card["number"][-4:]
        card["security_code"] = "***"
    return shown


def print_dry_run(data: SeedData, card: SandboxCard | None) -> None:
    for order in data.orders:
        print(f"\n=== {order.order_ref}  customer={order.customer_id}  total={order.total} {order.currency}")
        print(json.dumps(masked(build_order_payload(order, card)), indent=2))
    how = "a sandbox test card" if card else "browser approval"
    orders = f"{len(data.orders)} order{'' if len(data.orders) == 1 else 's'}"
    print(f"\n{len(data.customers)} customers and {orders} are valid (paid with {how}). Nothing was sent.")


def ask_for_approval(pending: list[tuple[str, str]]) -> None:
    print("\nApprove each order in your browser. Log in as your sandbox PERSONAL (buyer) account")
    print("(developer.paypal.com > Testing Tools > Sandbox Accounts), not the business one.\n")
    for order_ref, url in pending:
        print(f"  {order_ref}  {url}")
    input("\nPress Enter when you have approved them (any you skip are picked up next run)... ")


def print_report(report: SeedReport) -> None:
    for title, refs in [
        ("Created in PayPal", report.created),
        ("Captured", report.captured),
        ("Waiting", report.waiting),
        ("Rows written to the database", report.rows_written),
    ]:
        print(f"{title}: {', '.join(refs) if refs else '-'}")
    for problem in report.problems:
        print(f"PROBLEM: {problem}")


async def seed(data: SeedData, state: Path, card: SandboxCard | None) -> SeedReport:
    engine = get_engine()
    init_db(engine)
    async with PayPalClient.from_settings() as paypal:
        return await run_seed(data, paypal, state, make_session_factory(engine), ask_for_approval, card)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    card = None
    try:
        card = choose_card(args)
        data = select_orders(load_seed(args.customers, args.orders), args.only)
        if args.dry_run:
            print_dry_run(data, card)
            return 0
        if args.restore_db:
            engine = get_engine()
            init_db(engine)
            written = restore_db(data, args.state, make_session_factory(engine))
            print(f"Rows written to the database: {', '.join(written) if written else '- (all present)'}")
            return 0
        report = asyncio.run(seed(data, args.state, card))
    except SeedError as exc:
        print(f"Seed problem: {exc}", file=sys.stderr)
        return 2
    except PayPalUnavailable as exc:
        print(f"PayPal did not answer ({exc}). Progress is saved; just run the script again.", file=sys.stderr)
        return 1
    except PayPalRequestError as exc:
        print(f"PayPal refused: {exc.name} issues={exc.issues} debug_id={exc.debug_id}", file=sys.stderr)
        if card is not None:
            print("If this says card processing is not enabled for your sandbox business account, "
                  "run again with --approve-in-browser.", file=sys.stderr)
        return 1
    except PayPalError as exc:
        print(f"PayPal error: {exc}", file=sys.stderr)
        return 1
    except IntegrityError as exc:
        print(f"Database conflict: {exc.orig}. If the seed files changed a lot, delete "
              "wardlock.db and run again with --restore-db.", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as exc:
        print(f"Setup problem: {exc}", file=sys.stderr)
        return 2
    print_report(report)
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
