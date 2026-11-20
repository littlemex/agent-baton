# agent-baton

A crash-safe task ledger for handing an in-flight task - and the paid capacity reservation it
is bound to - from one AI agent session to a different one, without two sessions holding the
same task at once, and without a crashed session stranding a reservation forever.

This project grew out of [littlemex/distai-platform#38](https://github.com/littlemex/distai-platform/issues/38),
which has the full design rationale (why DynamoDB conditional writes over a Kubernetes
`Lease`, Temporal, or SQS visibility timeouts; why resource scheduling and task ownership are
two separate concerns). **`CONTRACT.md`** in this repository is the normative spec for what
v1 actually implements - read it before relying on any guarantee not repeated below.

This is not an official AWS project.

## What this is for

A long-lived AI task - "run verification lane 1 under the current Capacity Block", "finish
the migration started by an earlier session" - outlives any one session's context/time
budget. A session can be background-detached, killed, or simply replaced by a fresh one with
no shared process memory. `agent-baton` gives a different session a safe way to:

- Discover an in-flight task and the capacity it is bound to.
- Take over a task a crashed session never released, only after a plan-defined wait, measured
  by the Ledger's own clock - never a caller's.
- Resume from whatever evidence the previous holder already appended, without re-running
  finished work.
- Get work credentials scoped to exactly that task's own profile, narrower than a broad
  standing role.

What this guarantees, precisely: **at most one current Ledger owner at any instant,
at-least-once execution of a task's steps, idempotent steps** - not exactly-once execution.
An already-in-flight side effect from a deposed holder (a running process, STS credentials
that remain valid for up to an hour) is not fenced by this mechanism. See `CONTRACT.md` s.9
for the full list of what this does not cover in v1.

## Installation

```bash
pip install agent-baton   # once published; until then, pip install -e . from a clone
```

## Using the library

```python
import boto3
from agent_baton import Broker, KindPolicy

session = boto3.Session()  # the broker's OWN, narrowly-scoped credentials in production
table = session.resource("dynamodb").Table("my-agent-baton-ledger")

policy_table = {
    "verify-run-light": KindPolicy(
        allowed_profiles=("verify-run-light-profile",),
        work_role="my-agent-baton-work-verify-light",
        lease_seconds=600,
        timebox_minutes=180,
        max_cost_usd=60,
        max_retries=2,
        requires_approval=False,
        allowed_capacity_blocks=("cr-0123456789abcdef0",),
    ),
}

broker = Broker(table, policy_table, session=session)
item = broker.create(
    "cb-2026w47-lane1",
    "verify-run-light",
    "verify-run-light-profile",
    {"capacity_block_id": "cr-0123456789abcdef0", "lane": 1},
)

token = "a-caller-generated-csprng-token"  # see agent_baton.new_client_token()
status, item = broker.claim(item["task_id"], token)  # "claimed", or raises Wait/Rejected
broker.heartbeat(item["task_id"], token)
broker.complete(item["task_id"], token, evidence_entry={"type": "result", "url": "s3://..."})
```

## Using the CLI

```bash
agent-baton --table my-agent-baton-ledger --policy-file policy.json \
  create cb-2026w47-lane1 verify-run-light verify-run-light-profile \
  '{"capacity_block_id": "cr-0123456789abcdef0", "lane": 1}'

agent-baton --table my-agent-baton-ledger --policy-file policy.json claim cb-2026w47-lane1
agent-baton --table my-agent-baton-ledger --policy-file policy.json sweep
```

`policy.json` is a JSON object mapping each `kind` to the fields of `KindPolicy` - see
`agent_baton/policy.py` for the exact shape and `agent_baton.policy.load_policy_table_from_json`.

## What this needs to run

This repository ships **no infrastructure-as-code** (no Terraform, CDK, or CloudFormation).
How you run the broker - a Lambda, a container on EKS, a long-lived process on EC2 - is the
caller's own choice; what it needs is:

### 1. A DynamoDB table

Single table, on-demand billing, partition key `task_id` (plan items and task items share
the same table, overloading the `task_id` partition key - a plan item's own key is its
`plan_ref`):

```json
{
  "TableName": "my-agent-baton-ledger",
  "AttributeDefinitions": [{"AttributeName": "task_id", "AttributeType": "S"}],
  "KeySchema": [{"AttributeName": "task_id", "KeyType": "HASH"}],
  "BillingMode": "PAY_PER_REQUEST"
}
```

Enable TTL on the `ttl` attribute (set by the broker only once a task reaches a terminal
state):

```json
{"TimeToLiveSpecification": {"Enabled": true, "AttributeName": "ttl"}}
```

### 2. IAM: the broker's own identity (the only writer)

The broker's execution role needs exactly this on the table it is given - never a wildcard,
and never the union of every work-credential role's own permissions (that would let the
broker bypass `AssumeRole` and act with a profile's full access on its own identity):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": ["arn:aws:iam::ACCOUNT_ID:role/my-agent-baton-work-*"]
    },
    {
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan"],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT_ID:table/my-agent-baton-ledger"
    }
  ]
}
```

### 3. IAM: an AI-facing session's own role (read-only on the Ledger)

A session that is not the broker itself should never be able to write the table at all - an
explicit `Deny`, not only the absence of an `Allow`, so a later, broader identity policy added
elsewhere cannot silently reopen it:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadOnlyLedger", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT_ID:table/my-agent-baton-ledger"
    },
    {
      "Sid": "DenyAllLedgerWrites", "Effect": "Deny",
      "Action": [
        "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem", "dynamodb:TransactWriteItems",
        "dynamodb:PartiQLInsert", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete"
      ],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT_ID:table/my-agent-baton-ledger"
    }
  ]
}
```

### 4. IAM: a per-profile work-credential role

Each `KindPolicy.work_role` is a role whose trust policy names only the broker's own
identity, and whose identity policy is the actual ceiling a vended credential can reach
(`vend_work_credentials()` layers `Deny` statements from `KindPolicy.deny_statements` on top
of an `Allow: *` session policy - the role's own identity policy is never widened by that
session policy, only ever narrowed further):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::ACCOUNT_ID:role/my-agent-baton-broker"}, "Action": "sts:AssumeRole"}
  ]
}
```

### Environment variables (CLI only)

| Variable | Used by |
| --- | --- |
| `AWS_REGION` / `AWS_PROFILE` / standard AWS SDK credential variables | boto3, as usual - the CLI takes no credential of its own |

`--table` and `--policy-file` are CLI flags, not environment variables, so a policy table is
never read from an ambient, easily-overridden value.

## What is not implemented in v1

See `CONTRACT.md` s.2 and s.9 for the full, normative list. In short: `replan()`,
`create_child()`, work-credential renewal, a Kueue/resource-scheduler adapter, the MFA- and
allow-list-gated API Gateway front door for `grant_go`, and `max_cost_usd`/Capacity-Block-
end-time enforcement are all designed in the originating RFC but not built here yet. Each has
a tracking issue.

## Testing

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy agent_baton
pytest -m "not aws"     # pure unit tests - no AWS, safe anywhere
pytest -m aws           # creates and deletes its own DynamoDB table + IAM roles - see below
```

**The `aws`-marked tests call real AWS** (DynamoDB, IAM, STS, SSM) with whatever credentials
the environment provides. Each test run uses a fresh, uniquely-prefixed set of resources
(`tests/conftest.py`) and tears them all down at the end of the session, confirmed by a
direct `describe`/`get` call per resource - never by trusting the teardown call's own exit
code. Run this only on a dedicated test runner with credentials allowed to create IAM roles
and a DynamoDB table, never on a contributor's laptop.

## Docker

```bash
docker build -t agent-baton:latest .
docker run --rm -e AWS_REGION=us-east-1 agent-baton:latest \
  --table my-agent-baton-ledger --policy-file /policy.json list
```

Build and run the image on the same dedicated test runner used for `pytest -m aws`, not on a
contributor's laptop - the image needs real AWS credentials to do anything useful.

## Contributing

Every change starts from an issue and lands through a pull request. See
[CONTRIBUTING.md](./CONTRIBUTING.md) and `CONTRACT.md`.

## Security

See [SECURITY.md](./SECURITY.md) for how to report a vulnerability.

## License

This project is licensed under the Apache-2.0 License. See [LICENSE](./LICENSE).
