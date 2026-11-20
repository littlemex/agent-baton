"""Pure unit tests for the policy table - no AWS."""

import json

import pytest

from agent_baton.policy import KindPolicy, load_policy_table_from_json, validate_policy_table


def _policy(**overrides):
    base = dict(
        allowed_profiles=("p1",),
        work_role="role",
        lease_seconds=60,
        timebox_minutes=10,
        max_cost_usd=5,
        max_retries=2,
        requires_approval=False,
        allowed_capacity_blocks=("cb-1",),
    )
    base.update(overrides)
    return KindPolicy(**base)


def test_kind_policy_rejects_non_positive_lease_seconds():
    with pytest.raises(ValueError):
        _policy(lease_seconds=0)


def test_kind_policy_rejects_non_positive_timebox():
    with pytest.raises(ValueError):
        _policy(timebox_minutes=0)


def test_kind_policy_rejects_negative_max_retries():
    with pytest.raises(ValueError):
        _policy(max_retries=-1)


def test_kind_policy_rejects_empty_allowed_profiles():
    with pytest.raises(ValueError):
        _policy(allowed_profiles=())


def test_validate_policy_table_rejects_a_kind_with_no_resource_target():
    table = {"orphan": _policy(allowed_capacity_blocks=(), allowed_queues=())}
    with pytest.raises(ValueError):
        validate_policy_table(table)


def test_validate_policy_table_accepts_a_well_formed_table():
    table = {"ok": _policy()}
    validate_policy_table(table)  # must not raise


def test_load_policy_table_from_json_round_trips(tmp_path):
    raw = {
        "verify-run-light": {
            "allowed_profiles": ["verify-run-light-profile"],
            "work_role": "agent-baton-work-verify-light",
            "lease_seconds": 600,
            "timebox_minutes": 180,
            "max_cost_usd": 60,
            "max_retries": 2,
            "requires_approval": False,
            "allowed_capacity_blocks": ["cr-example"],
        }
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    table = load_policy_table_from_json(str(path))
    assert set(table) == {"verify-run-light"}
    policy = table["verify-run-light"]
    assert policy.allowed_profiles == ("verify-run-light-profile",)
    assert policy.lease_seconds == 600
    assert policy.requires_approval is False


def test_load_policy_table_from_json_rejects_a_kind_with_no_resource_target(tmp_path):
    raw = {
        "broken": {
            "allowed_profiles": ["p"],
            "work_role": "r",
            "lease_seconds": 10,
            "timebox_minutes": 5,
            "max_cost_usd": 1,
            "max_retries": 0,
            "requires_approval": False,
        }
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_policy_table_from_json(str(path))
