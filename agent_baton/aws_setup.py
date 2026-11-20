"""Test/demo-only AWS scaffolding: a Ledger table, a broker role with a
permissions boundary, an AI-facing base role with no write access, and two
example per-profile work-credential roles.

**Not a production deployment.** CONTRACT.md s.2/s.9: a real broker runs as
a Lambda behind an MFA- and allow-list-gated API Gateway front door; this
module stands the caller's own ambient AWS identity in for "however the
broker's own process gets invoked", which is a real, named simplification -
the property actually exercised by the test suite that uses this module is
the WORK-CREDENTIAL roles' and the TABLE's own trust/permission boundary
against the AI-facing role, not who may invoke the broker's code.

Every resource this module creates is named with the caller-supplied
``prefix`` and is deleted by :func:`teardown_demo_environment`. Nothing here
is created implicitly by importing this module - every function must be
called explicitly, and every integration test that calls ``setup_*`` is
paired with a fixture that calls the matching ``teardown_*`` even on
failure.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError


@dataclass(frozen=True)
class DemoEnvironment:
    prefix: str
    region: str
    table_name: str
    broker_role: str
    ai_base_role: str
    work_roles: tuple[str, ...]
    ssm_allowed: str
    ssm_forbidden: str
    boundary_policy_name: str


def plan_demo_environment(prefix: str, region: str) -> DemoEnvironment:
    """Pure name computation - no AWS calls. Call this first so a test can
    assert on names before anything is created.
    """
    return DemoEnvironment(
        prefix=prefix,
        region=region,
        table_name=f"{prefix}-ledger",
        broker_role=f"{prefix}-broker-role",
        ai_base_role=f"{prefix}-ai-base-role",
        work_roles=(f"{prefix}-work-light", f"{prefix}-work-approval"),
        ssm_allowed=f"/{prefix}/allowed",
        ssm_forbidden=f"/{prefix}/forbidden",
        boundary_policy_name=f"{prefix}-broker-boundary",
    )


def _create_or_update_role(iamc, name: str, trust_doc: dict) -> None:
    last_err = None
    for _ in range(10):
        try:
            iamc.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust_doc))
            return
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "EntityAlreadyExists":
                iamc.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust_doc))
                return
            if code == "MalformedPolicyDocument":
                last_err = e
                time.sleep(5)
                continue
            raise
    raise last_err


def _policy_exists(iamc, policy_arn: str) -> bool:
    try:
        iamc.get_policy(PolicyArn=policy_arn)
        return True
    except ClientError:
        return False


def setup_demo_environment(env: DemoEnvironment) -> None:
    """Idempotent: safe to call again against an already-set-up
    environment of the same ``prefix``.
    """
    session = boto3.Session(region_name=env.region)
    sts = session.client("sts")
    caller = sts.get_caller_identity()
    account = caller["Account"]
    runner_arn = caller["Arn"]
    table_arn = f"arn:aws:dynamodb:{env.region}:{account}:table/{env.table_name}"
    broker_arn = f"arn:aws:iam::{account}:role/{env.broker_role}"

    ddbc = session.client("dynamodb")
    try:
        ddbc.create_table(
            TableName=env.table_name,
            AttributeDefinitions=[{"AttributeName": "task_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "task_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddbc.get_waiter("table_exists").wait(TableName=env.table_name)
        ddbc.update_time_to_live(
            TableName=env.table_name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise

    ssm = session.client("ssm")
    for name, value in (
        (env.ssm_allowed, "visible-to-demo-light-profile"),
        (env.ssm_forbidden, "must-never-be-readable-by-demo-light-profile"),
    ):
        try:
            ssm.put_parameter(Name=name, Value=value, Type="String")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ParameterAlreadyExists":
                raise

    iamc = session.client("iam")

    # broker role: the Ledger's only writer.
    broker_trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"AWS": runner_arn}, "Action": "sts:AssumeRole"}],
    }
    _create_or_update_role(iamc, env.broker_role, broker_trust)
    boundary_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Resource": [f"arn:aws:iam::{account}:role/{r}" for r in env.work_roles],
            },
            {
                "Effect": "Allow",
                "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"],
                "Resource": table_arn,
            },
            {
                "Effect": "Allow",
                "Action": "ssm:GetParameter",
                "Resource": f"arn:aws:ssm:{env.region}:{account}:parameter/{env.prefix}/*",
            },
        ],
    }
    boundary_arn = f"arn:aws:iam::{account}:policy/{env.boundary_policy_name}"
    if not _policy_exists(iamc, boundary_arn):
        iamc.create_policy(PolicyName=env.boundary_policy_name, PolicyDocument=json.dumps(boundary_doc))
    iamc.put_role_permissions_boundary(RoleName=env.broker_role, PermissionsBoundary=boundary_arn)
    iamc.put_role_policy(
        RoleName=env.broker_role, PolicyName=f"{env.prefix}-broker-perms", PolicyDocument=json.dumps(boundary_doc),
    )

    # AI-facing base role: no write action on the Ledger at all.
    ai_trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"AWS": runner_arn}, "Action": "sts:AssumeRole"}],
    }
    _create_or_update_role(iamc, env.ai_base_role, ai_trust)
    ai_perms = {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "ReadOnlyLedger", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"], "Resource": table_arn},
            {
                "Sid": "DenyAllLedgerWrites",
                "Effect": "Deny",
                "Action": [
                    "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                    "dynamodb:BatchWriteItem", "dynamodb:TransactWriteItems",
                    "dynamodb:PartiQLInsert", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete",
                ],
                "Resource": table_arn,
            },
        ],
    }
    iamc.put_role_policy(RoleName=env.ai_base_role, PolicyName=f"{env.prefix}-ai-base-perms", PolicyDocument=json.dumps(ai_perms))

    # Work-credential roles: trust ONLY the broker.
    work_trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"AWS": broker_arn}, "Action": "sts:AssumeRole"}],
    }
    work_perms = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "ssm:GetParameter", "Resource": f"arn:aws:ssm:{env.region}:{account}:parameter{env.ssm_allowed}"},
        ],
    }
    for role in env.work_roles:
        _create_or_update_role(iamc, role, work_trust)
        iamc.put_role_policy(RoleName=role, PolicyName=f"{env.prefix}-work-perms", PolicyDocument=json.dumps(work_perms))

    time.sleep(15)  # IAM propagation


def teardown_demo_environment(env: DemoEnvironment) -> None:
    session = boto3.Session(region_name=env.region)
    iamc = session.client("iam")
    sts = session.client("sts")
    account = sts.get_caller_identity()["Account"]

    for role in (env.ai_base_role, *env.work_roles, env.broker_role):
        for pol in (f"{env.prefix}-ai-base-perms", f"{env.prefix}-work-perms", f"{env.prefix}-broker-perms"):
            try:
                iamc.delete_role_policy(RoleName=role, PolicyName=pol)
            except ClientError:
                pass
        try:
            iamc.delete_role_permissions_boundary(RoleName=role)
        except ClientError:
            pass
        try:
            iamc.delete_role(RoleName=role)
        except ClientError:
            pass

    boundary_arn = f"arn:aws:iam::{account}:policy/{env.boundary_policy_name}"
    try:
        iamc.delete_policy(PolicyArn=boundary_arn)
    except ClientError:
        pass

    ssm = session.client("ssm")
    for name in (env.ssm_allowed, env.ssm_forbidden):
        try:
            ssm.delete_parameter(Name=name)
        except ClientError:
            pass

    ddbc = session.client("dynamodb")
    try:
        ddbc.delete_table(TableName=env.table_name)
        ddbc.get_waiter("table_not_exists").wait(TableName=env.table_name)
    except ClientError:
        pass


def confirm_deleted(env: DemoEnvironment) -> list[str]:
    """Called after teardown: a direct, separate describe/get call per
    resource, never trusting teardown's own exit code (CONTRACT.md s.10).
    Returns a list of resources that are NOT confirmed deleted - empty on
    success.
    """
    session = boto3.Session(region_name=env.region)
    iamc = session.client("iam")
    ddbc = session.client("dynamodb")
    ssm = session.client("ssm")
    leftover = []

    try:
        ddbc.describe_table(TableName=env.table_name)
        leftover.append(f"table:{env.table_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    for role in (env.ai_base_role, *env.work_roles, env.broker_role):
        try:
            iamc.get_role(RoleName=role)
            leftover.append(f"role:{role}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise

    for name in (env.ssm_allowed, env.ssm_forbidden):
        try:
            ssm.get_parameter(Name=name)
            leftover.append(f"ssm:{name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ParameterNotFound":
                raise

    return leftover
