"""Fixtures for the ``aws``-marked integration tests.

These create real DynamoDB/IAM/SSM resources, named with a unique prefix
derived from the current time and a random suffix so a run never collides
with a previous one or with anything else in the account, and tear
everything down at the end of the session - confirmed by a direct,
separate ``describe``/``get`` call per resource, never by trusting the
teardown call's own exit code (CONTRACT.md s.10).

Run only against an AWS account you are allowed to create IAM roles and a
DynamoDB table in. Per this project's own rule, run on a dedicated test
runner, never on a contributor's laptop.
"""
from __future__ import annotations

import os
import time
import uuid

import boto3
import pytest

from agent_baton.aws_setup import (
    confirm_deleted,
    plan_demo_environment,
    setup_demo_environment,
    teardown_demo_environment,
)
from agent_baton.ledger import Broker
from agent_baton.policy import KindPolicy

REGION = os.environ.get("AGENT_BATON_TEST_REGION", "us-east-1")


@pytest.fixture(scope="session")
def demo_env():
    prefix = os.environ.get("AGENT_BATON_TEST_PREFIX") or f"agent-baton-ci-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    env = plan_demo_environment(prefix, REGION)
    setup_demo_environment(env)
    try:
        yield env
    finally:
        teardown_demo_environment(env)
        leftover = confirm_deleted(env)
        assert leftover == [], f"resources not confirmed deleted after teardown: {leftover}"


@pytest.fixture(scope="session")
def policy_table(demo_env):
    light_role, approval_role = demo_env.work_roles
    return {
        "demo-light": KindPolicy(
            allowed_profiles=("demo-light-profile",),
            work_role=light_role,
            lease_seconds=3,
            timebox_minutes=5,
            max_cost_usd=5,
            max_retries=2,
            requires_approval=False,
            allowed_capacity_blocks=("demo-cb-0001",),
            deny_statements=(
                {
                    "Effect": "Deny",
                    "Action": "ssm:GetParameter",
                    "Resource": f"arn:aws:ssm:{REGION}:*:parameter{demo_env.ssm_forbidden}",
                },
            ),
        ),
        "demo-approval": KindPolicy(
            allowed_profiles=("demo-approval-profile",),
            work_role=approval_role,
            lease_seconds=3,
            timebox_minutes=5,
            max_cost_usd=5,
            max_retries=2,
            requires_approval=True,
            allowed_capacity_blocks=("demo-cb-0001",),
        ),
    }


def assume_session(role_name: str, session_name: str, region: str = REGION, duration: int = 900) -> boto3.Session:
    sts = boto3.client("sts", region_name=region)
    account = sts.get_caller_identity()["Account"]
    role_arn = f"arn:aws:iam::{account}:role/{role_name}"
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name, DurationSeconds=duration)["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


@pytest.fixture
def broker(demo_env, policy_table) -> Broker:
    """A Broker built from the demo broker role's OWN assumed session -
    never the test process's ambient credentials and never the AI base
    role. Mirrors exactly how a real deployment's Lambda would hold only
    its own, narrowly-scoped identity.
    """
    session = assume_session(demo_env.broker_role, "broker-under-test", region=demo_env.region)
    table = session.resource("dynamodb").Table(demo_env.table_name)
    return Broker(table, policy_table, session=session)


LIGHT_BINDING = {"capacity_block_id": "demo-cb-0001", "lane": 1}


def new_task_id(prefix: str) -> str:
    return f"agent-baton-test-{prefix}-{uuid.uuid4().hex[:8]}"
