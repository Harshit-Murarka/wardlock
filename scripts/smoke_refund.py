"""Manual smoke test: refund a small amount on ONE real PayPal sandbox capture.

Run it from the repo root with .env filled in (sandbox credentials only):

    python scripts/smoke_refund.py --capture <CAPTURE_ID> --amount 1.00

It prints the PayPal-Request-Id BEFORE calling PayPal. Re-run with
--request-id <that id> to prove idempotency: PayPal must return the SAME refund
id instead of refunding a second time.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from decimal import Decimal, InvalidOperation

from wardlock.paypal.client import PayPalClient
from wardlock.paypal.errors import PayPalError, PayPalRequestError, PayPalUnavailable


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refund a small amount on one sandbox capture.")
    parser.add_argument("--capture", required=True, help="PayPal sandbox capture id")
    parser.add_argument("--amount", required=True, type=_decimal, help="e.g. 1.00")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--note", default="Wardlock smoke test")
    parser.add_argument("--request-id", help="reuse an earlier PayPal-Request-Id (idempotency check)")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return parser.parse_args(argv)


def _decimal(text: str) -> Decimal:
    # Parse straight to Decimal so the amount never passes through float.
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None


async def run(args: argparse.Namespace) -> int:
    request_id = args.request_id or str(uuid.uuid4())
    async with PayPalClient.from_settings() as paypal:
        capture = await paypal.get_capture(args.capture)
        money = capture.get("amount") or {}
        print(f"Capture {args.capture}: status={capture.get('status')} "
              f"amount={money.get('value')} {money.get('currency_code')}")
        print(f"PayPal-Request-Id: {request_id}")
        print(f"  (re-run with --request-id {request_id} to check idempotency)")

        question = f"Refund {args.amount} {args.currency} on capture {args.capture} in the SANDBOX? [y/N] "
        if not args.yes and input(question).strip().lower() != "y":
            print("Cancelled; nothing was sent.")
            return 1

        refund = await paypal.refund_capture(
            args.capture,
            args.amount,
            args.currency,
            request_id,
            note=args.note,
            custom_id=f"smoke-{request_id[:8]}",
        )
        print(f"Refund {refund.id}: status={refund.status} amount={refund.amount} {refund.currency}")

        again = await paypal.get_refund(refund.id)
        print(f"Read back from PayPal: {again.id} status={again.status} amount={again.amount}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(run(args))
    except PayPalRequestError as exc:
        print(f"PayPal refused: {exc.name} issues={exc.issues} debug_id={exc.debug_id}", file=sys.stderr)
    except PayPalUnavailable as exc:
        print(f"Outcome UNKNOWN: {exc}. Re-run with the same --request-id; "
              "never use a new one or you may refund twice.", file=sys.stderr)
    except PayPalError as exc:
        print(f"PayPal error: {exc}", file=sys.stderr)
    except ValueError as exc:
        print(f"Invalid input or settings: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
