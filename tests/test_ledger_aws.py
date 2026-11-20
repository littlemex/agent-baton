"""The four scenarios CONTRACT.md s.10 names as v1's own acceptance bar,
re-run against this package's implementation - no mocks, every assertion
is the observed result of a real boto3 call against real DynamoDB/STS/IAM/
SSM. Mirrors the reference prototype's ``handoff_scenarios.py`` 1:1 in
intent; ported to pytest and this package's own module layout.

Run only on a dedicated test runner (never a contributor's laptop - see
README "Testing"), with credentials allowed to create IAM roles and a
DynamoDB table. Everything the ``demo_env``/``policy_table`` fixtures
create is torn down, and teardown is confirmed by direct describe/get
calls, in ``conftest.py``.
"""

from __future__ import annotations

import inspect
import threading
import time
from unittest import mock

import pytest
from botocore.exceptions import ClientError
from conftest import LIGHT_BINDING, assume_session, new_task_id

from agent_baton.crypto import hash_token, new_client_token
from agent_baton.ledger import Broker, Rejected, Wait

pytestmark = pytest.mark.aws


# --------------------------------------------------------------------- race ---


def test_race_exactly_one_of_eight_concurrent_claimants_wins(broker: Broker):
    task_id = new_task_id("race")
    broker.create(task_id, "demo-light", "demo-light-profile", LIGHT_BINDING)
    outcomes: dict[str, tuple] = {}

    def attempt(name: str) -> None:
        token = new_client_token()
        try:
            status, item = broker.claim(task_id, token)
            outcomes[name] = (status, token, item["version"])
        except (Rejected, Wait) as e:
            outcomes[name] = (type(e).__name__, None, None)

    threads = [threading.Thread(target=attempt, args=(f"claimant-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = {k: v for k, v in outcomes.items() if v[0] == "claimed"}
    assert len(winners) == 1, f"outcomes={ {k: v[0] for k, v in outcomes.items()} }"

    item = broker.get(task_id)
    assert item["attempt_count"] == 0  # a fresh claim of a pending task is not a retry

    _, (status, winner_token, _) = next(iter(winners.items()))
    retry_status, _ = broker.claim(task_id, winner_token)
    assert retry_status == "already_owned"  # the winner's own retry, not a fresh race

    other_token = new_client_token()
    with pytest.raises((Wait, Rejected)):
        broker.claim(task_id, other_token)  # a different token is told to wait, never handed the lease


# ----------------------------------------------------- lease and takeover ---


def test_lease_and_takeover(broker: Broker, policy_table):
    task_id = new_task_id("crash")
    broker.create(task_id, "demo-light", "demo-light-profile", LIGHT_BINDING)
    policy_lease = policy_table["demo-light"].lease_seconds

    tok_a = new_client_token()
    status, _ = broker.claim(task_id, tok_a)
    assert status == "claimed"  # session A's initial claim succeeds with no wait

    sig = inspect.signature(Broker.claim)
    assert "lease_seconds" not in sig.parameters, (
        "claim() must have no caller-supplied lease_seconds parameter - "
        "the wait is read from the plan, with no path to shorten it"
    )

    # session A "crashes": no heartbeat, no release call from here on.
    tok_b = new_client_token()
    with pytest.raises(Wait):
        broker.claim(task_id, tok_b)  # immediately after A's claim: Wait, not an instant takeover

    t0 = time.monotonic()
    status, item = broker.claim_wait_loop(task_id, tok_b, poll_interval=0.3, max_wait=20)
    elapsed = time.monotonic() - t0
    assert status == "claimed"
    assert elapsed >= policy_lease - 0.35, (
        f"B reclaimed after only {elapsed:.1f}s, before the plan's own "
        f"lease_seconds ({policy_lease}s) had elapsed"
    )
    assert item["attempt_count"] == 1  # a stale-claimed takeover counts as an attempt

    assert broker.heartbeat(task_id, tok_a) is False  # A's old token is rejected once B has taken over
    assert broker.heartbeat(task_id, tok_b) is True

    # A graceful release-then-reclaim must NOT charge attempt_count.
    assert broker.release(task_id, tok_b, evidence_entry={"type": "note", "by": "session-b"}) is True
    tok_c = new_client_token()
    status, item = broker.claim(task_id, tok_c)
    assert status == "claimed"  # claims the released (pending) task immediately, no wait
    assert item["attempt_count"] == 1  # unchanged from the one real crash-takeover above


# ------------------------------------------------------------- approval gate ---


def test_approval_gate(broker: Broker):
    task_id = new_task_id("approve")
    item = broker.create(task_id, "demo-approval", "demo-approval-profile", LIGHT_BINDING)
    assert item["state"] == "awaiting_approval"

    with pytest.raises(Rejected):
        broker.claim(task_id, new_client_token())  # claim() itself rejects before any human_go exists

    current_plan_ref = item["plan_ref"]
    # grant_go() catches the ConditionalCheckFailedException this stale hash
    # causes and raises Rejected - never a raw botocore error (CONTRACT.md
    # s.8 item 9).
    with pytest.raises(Rejected):
        broker.grant_go(task_id, "WRONG-STALE-HASH", expires_in_seconds=3600, granted_by="littlemex")

    broker.grant_go(task_id, current_plan_ref, expires_in_seconds=3600, granted_by="littlemex")
    tok_ok = new_client_token()
    status, _ = broker.claim(task_id, tok_ok)
    assert status == "claimed"  # claim after a matching, unexpired human_go succeeds

    # N4: the hash is RECOMPUTED from the plan's own stored content at claim
    # time, never trusted from a prior comparison. Mutate the plan item's
    # own content out-of-band and show the NEXT claim rejects it.
    broker.release(task_id, tok_ok)
    broker.t.update_item(
        Key={"task_id": current_plan_ref},
        UpdateExpression="SET #p.max_cost_usd = :tampered",
        ExpressionAttributeNames={"#p": "plan"},  # "plan" is a DynamoDB reserved keyword
        ExpressionAttributeValues={":tampered": 999999},
    )
    with pytest.raises(Rejected, match="does not match"):
        broker.claim(task_id, new_client_token())

    # An expired grant is treated exactly like a missing grant. A distinct
    # resource_binding avoids colliding with the now-tampered plan above.
    task_id2 = new_task_id("expired")
    item2 = broker.create(
        task_id2, "demo-approval", "demo-approval-profile", {"capacity_block_id": "demo-cb-0001", "lane": 2}
    )
    broker.grant_go(task_id2, item2["plan_ref"], expires_in_seconds=1, granted_by="littlemex")
    time.sleep(2)
    with pytest.raises(Rejected):
        broker.claim(task_id2, new_client_token())


# ----------------------------------------------------------- broker boundary ---


def test_broker_boundary(broker: Broker, demo_env):
    work_role_light = demo_env.work_roles[0]
    task_id = new_task_id("boundary")
    broker.create(task_id, "demo-light", "demo-light-profile", LIGHT_BINDING)

    ai_session = assume_session(demo_env.ai_base_role, "ai-base-direct-attempt", region=demo_env.region)
    account = ai_session.client("sts").get_caller_identity()["Account"]
    work_role_arn = f"arn:aws:iam::{account}:role/{work_role_light}"

    with pytest.raises(ClientError) as exc_info:
        ai_session.client("sts").assume_role(RoleArn=work_role_arn, RoleSessionName="ai-tries-work-role")
    assert exc_info.value.response["Error"]["Code"] == "AccessDenied"  # trust policy names only the broker

    with pytest.raises(ClientError) as exc_info:
        ai_session.resource("dynamodb").Table(demo_env.table_name).update_item(
            Key={"task_id": task_id},
            UpdateExpression="SET version = version + :one",
            ExpressionAttributeValues={":one": 1},
        )
    assert (
        exc_info.value.response["Error"]["Code"] == "AccessDeniedException"
    )  # no write path outside the broker

    # Read access (list/show) is still allowed.
    ai_session.resource("dynamodb").Table(demo_env.table_name).get_item(Key={"task_id": task_id})

    tok = new_client_token()
    broker.claim(task_id, tok)
    work_session = broker.vend_work_credentials(task_id, tok)
    allowed_val = work_session.client("ssm", region_name=demo_env.region).get_parameter(
        Name=demo_env.ssm_allowed
    )["Parameter"]["Value"]
    assert allowed_val == "visible-to-demo-light-profile"

    with pytest.raises(ClientError) as exc_info:
        work_session.client("ssm", region_name=demo_env.region).get_parameter(Name=demo_env.ssm_forbidden)
    assert exc_info.value.response["Error"]["Code"] == "AccessDeniedException"  # narrowed by server-side Deny

    with pytest.raises(ClientError) as exc_info:
        work_session.resource("dynamodb", region_name=demo_env.region).Table(demo_env.table_name).update_item(
            Key={"task_id": task_id},
            UpdateExpression="SET version = version + :one",
            ExpressionAttributeValues={":one": 1},
        )
    assert (
        exc_info.value.response["Error"]["Code"] == "AccessDeniedException"
    )  # work role never grants Ledger writes


# -------------------------------------------------- complete()/abandon() + ttl ---
# CONTRACT.md s.8 item 6: "ttl" is a DynamoDB reserved keyword, same class of bug
# as "plan" (item 3). Neither the race/lease/approval/boundary scenarios above
# call complete() or abandon() against real DynamoDB, so this shipped unnoticed
# until a Docker-based smoke test ran `complete` against a real table. These two
# tests are the regression coverage that bug is missing without.


def test_complete_sets_ttl(broker: Broker):
    task_id = new_task_id("complete")
    broker.create(task_id, "demo-light", "demo-light-profile", LIGHT_BINDING)
    token = new_client_token()
    broker.claim(task_id, token)
    ok = broker.complete(task_id, token, evidence_entry={"type": "result", "url": "s3://example/run.log"})
    assert ok is True
    item = broker.get(task_id)
    assert item["state"] == "completed"
    assert "ttl" in item and item["ttl"] > 0
    assert item["evidence"][-1]["type"] == "result"


def test_abandon_sets_ttl(broker: Broker):
    task_id = new_task_id("abandon")
    broker.create(task_id, "demo-light", "demo-light-profile", LIGHT_BINDING)
    broker.abandon(task_id, "manual test abandon")
    item = broker.get(task_id)
    assert item["state"] == "abandoned"
    assert "ttl" in item and item["ttl"] > 0
    assert item["abandon_reason"] == "manual test abandon"


# ---------------------------------------- the central guarantee, injected ---
# The RFC's own Acceptance Criteria name this exact scenario as unverified by
# the prototype: "a heartbeat that lands between a challenger's own staleness
# check and that challenger's own claiming write makes that write fail,
# evidenced by a heartbeat injected at exactly that gap, not only by the
# absence of a code path that happens not to try." An earlier version of
# claim() in this repository re-read the item after confirming staleness and
# conditioned its final write on THAT fresh version instead of the
# confirmed-stale one - silently defeating this guarantee (caught by review,
# not by any test; see CONTRACT.md s.8 item 7). This test is the missing
# evidence, added once that bug was fixed.


def test_heartbeat_injected_at_the_staleness_gap_blocks_the_takeover(broker: Broker, policy_table):
    task_id = new_task_id("gap")
    broker.create(task_id, "demo-light", "demo-light-profile", LIGHT_BINDING)
    lease_seconds = policy_table["demo-light"].lease_seconds

    tok_a = new_client_token()
    status, _ = broker.claim(task_id, tok_a)
    assert status == "claimed"

    tok_b = new_client_token()
    with pytest.raises(Wait):
        broker.claim(task_id, tok_b)  # arms the staleness marker

    time.sleep(lease_seconds + 0.5)  # A does nothing - this is the "crash" half

    real_update_item = broker.t.update_item
    injected = {"done": False}

    def wrapped_update_item(**kwargs):
        # Intercept only B's own claiming write (step 10's distinctive
        # UpdateExpression), and only once - not the heartbeat call we are
        # about to make from inside this same wrapper, nor any other write.
        if not injected["done"] and "lease_holder_hash = :h" in kwargs.get("UpdateExpression", ""):
            injected["done"] = True
            # A renews its OWN lease here, via a real, separate boto3 call -
            # exactly "a heartbeat injected at exactly that gap", landing
            # between _arm_or_wait's confirmed-stale check (already passed,
            # or B would not have reached this write at all) and B's own
            # claiming write (about to happen, below).
            assert broker.heartbeat(task_id, tok_a) is True
        return real_update_item(**kwargs)

    with mock.patch.object(broker.t, "update_item", side_effect=wrapped_update_item), pytest.raises(Wait):
        # B's write loses the CAS against A's just-landed heartbeat,
        # ledger.py catches that ConditionalCheckFailedException and
        # re-runs claim() from a fresh read - which now sees A's renewed
        # lease and must wait out a fresh lease_seconds, not succeed.
        broker.claim(task_id, tok_b)

    assert injected["done"] is True  # the injection actually happened - not a vacuous pass
    item = broker.get(task_id)
    assert item["state"] == "claimed"
    assert item["lease_holder_hash"] == hash_token(tok_a)  # A, never B
    assert broker.heartbeat(task_id, tok_a) is True  # A's lease is alive and well
