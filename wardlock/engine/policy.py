"""Load policy.yaml, the only source of refund limits (SPEC §5).

An invalid policy raises PolicyError, so the server refuses to start rather than running
with a limit nobody intended. Typos are caught too: unknown keys and names are errors,
and the limit must be a quoted string, because YAML turns an unquoted 50.00 into a float.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from wardlock.enums import HandleKind, ReasonCode
from wardlock.money import SUPPORTED_CURRENCIES, to_minor

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "policy.yaml"

REQUIRED_KEYS = frozenset({
    "version", "currency", "auto_approve_limit", "max_refunds_per_session",
    "goodwill_percentages", "reason_allows",
})


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    version: str
    currency: str  # the currency auto_approve_limit is in
    auto_approve_limit_minor: int
    max_refunds_per_session: int
    goodwill_percentages: tuple[int, ...]
    reason_allows: Mapping[ReasonCode, frozenset[HandleKind]]


def load_policy(path: Path | str = DEFAULT_POLICY_PATH) -> Policy:
    with open(path, encoding="utf-8") as f:
        return parse_policy(yaml.safe_load(f))  # safe_load: plain data only, never code


def parse_policy(data: Any) -> Policy:
    if not isinstance(data, dict):
        raise PolicyError("policy must be a mapping")
    unknown, missing = set(data) - REQUIRED_KEYS, REQUIRED_KEYS - set(data)
    if unknown:
        raise PolicyError(f"unknown policy keys: {sorted(map(str, unknown))}")
    if missing:
        raise PolicyError(f"missing policy keys: {sorted(missing)}")

    currency = data["currency"]
    if currency not in SUPPORTED_CURRENCIES:
        raise PolicyError("currency must be a supported currency code such as USD")
    return Policy(
        version=_version(data["version"]),
        currency=currency,
        auto_approve_limit_minor=_limit(data["auto_approve_limit"], currency),
        max_refunds_per_session=_non_negative_int(data["max_refunds_per_session"], "max_refunds_per_session"),
        goodwill_percentages=_percentages(data["goodwill_percentages"]),
        reason_allows=_reason_allows(data["reason_allows"]),
    )


def _version(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or str(value) == "":
        raise PolicyError("version must be a number or a non-empty string")
    return str(value)


def _limit(value: Any, currency: str) -> int:
    if not isinstance(value, str):
        raise PolicyError('auto_approve_limit must be a quoted string such as "50.00"')
    try:
        return to_minor(Decimal(value), currency)
    except (InvalidOperation, ValueError):
        raise PolicyError(f"auto_approve_limit must be an exact, non-negative {currency} amount") from None


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PolicyError(f"{name} must be a whole number of 0 or more")
    return value


def _percentages(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise PolicyError("goodwill_percentages must be a list")
    for pct in value:
        if isinstance(pct, bool) or not isinstance(pct, int) or not 1 <= pct <= 100:
            raise PolicyError("goodwill_percentages must be whole numbers from 1 to 100")
    if len(set(value)) != len(value):
        raise PolicyError("goodwill_percentages must not repeat")
    return tuple(sorted(value))


def _reason_allows(value: Any) -> dict[ReasonCode, frozenset[HandleKind]]:
    if not isinstance(value, dict):
        raise PolicyError("reason_allows must be a mapping")
    result = {}
    for reason, kinds in value.items():
        if reason not in ReasonCode.__members__:
            raise PolicyError(f"unknown reason in reason_allows: {reason!r}")
        if not isinstance(kinds, list) or any(k not in HandleKind.__members__ for k in kinds):
            raise PolicyError(f"reason_allows[{reason}] must be a list of handle kinds")
        # A reason left out of the policy allows nothing, so such requests are HELD.
        result[ReasonCode(reason)] = frozenset(HandleKind(k) for k in kinds)
    return result
