"""policy.yaml loading (SPEC §5): a bad policy must stop the server, never run with surprises."""

import pytest
import yaml

from wardlock.engine.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy, parse_policy
from wardlock.enums import HandleKind, ReasonCode


def real_policy_data():
    return yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))


def test_the_real_policy_file_loads():
    policy = load_policy()
    assert (policy.version, policy.currency, policy.auto_approve_limit_minor) == ("1", "USD", 5000)
    assert (policy.max_refunds_per_session, policy.goodwill_percentages) == (2, (10, 25))
    assert policy.reason_allows[ReasonCode.DAMAGED] == {HandleKind.LINE_ITEM, HandleKind.GOODWILL_PCT}
    assert policy.reason_allows[ReasonCode.NOT_RECEIVED] == {HandleKind.FULL_REMAINING}


@pytest.mark.parametrize(
    "change",
    [
        {"auto_approve_limit": 50.0},  # what an unquoted 50.00 becomes in YAML
        {"auto_approve_limit": "50.001"},
        {"auto_approve_limit": "-1.00"},
        {"auto_approve_limit": "fifty"},
        {"currency": "XYZ"},
        {"max_refunds_per_session": -1},
        {"max_refunds_per_session": True},
        {"max_refunds_per_session": "2"},
        {"goodwill_percentages": [0]},
        {"goodwill_percentages": [101]},
        {"goodwill_percentages": [10, 10]},
        {"goodwill_percentages": [2.5]},
        {"goodwill_percentages": [True]},
        {"goodwill_percentages": 10},
        {"reason_allows": {"BROKEN": ["LINE_ITEM"]}},
        {"reason_allows": {"DAMAGED": ["REFUND_EVERYTHING"]}},
        {"reason_allows": {"DAMAGED": "LINE_ITEM"}},
        {"reason_allows": ["DAMAGED"]},
        {"version": ""},
        {"version": True},
        {"auto_aprove_limit": "500.00"},  # a typo'd key must not be silently ignored
    ],
)
def test_invalid_policies_are_refused(change):
    with pytest.raises(PolicyError):
        parse_policy({**real_policy_data(), **change})


@pytest.mark.parametrize("key", ["version", "currency", "auto_approve_limit", "reason_allows"])
def test_every_key_is_required(key):
    data = real_policy_data()
    del data[key]
    with pytest.raises(PolicyError):
        parse_policy(data)


@pytest.mark.parametrize("data", [None, [], "auto_approve_limit: 50"])
def test_a_policy_must_be_a_mapping(data):
    with pytest.raises(PolicyError):
        parse_policy(data)
