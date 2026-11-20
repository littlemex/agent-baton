"""The Ledger's sole writer: the ``Broker`` class.

CONTRACT.md s.3-s.6 is the normative spec this file implements. Every
public method here is a "broker action" in that contract's vocabulary -
nothing else is ever allowed to write the DynamoDB table this class is
given. ``Broker`` does not create that table, any IAM role, or any AWS
resource; see ``agent_baton.aws_setup`` for the (test/demo-only) scaffolding
that stands up a table and the per-profile work-credential roles this class
assumes into.
"""

from __future__ import annotations

import time
from typing import Any

from botocore.exceptions import ClientError

from agent_baton.crypto import broker_now, hash_token, new_client_token, plan_ref_for
from agent_baton.policy import KindPolicy, PolicyTable

__all__ = ["Broker", "Rejected", "Wait", "new_client_token"]

LIVE_STATES = ("claimed", "parked")
TERMINAL_STATES = ("completed", "abandoned")


class Rejected(Exception):
    """claim()/grant_go() refuse outright - no wait is implied."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Wait(Exception):
    """claim() found a live, not-owned task and started (or is still
    running) the two-call wait described in CONTRACT.md s.6. ``retry_after``
    is a lower bound on how long the caller should wait before calling
    claim() again - never a promise the task will be claimable at exactly
    that moment.
    """

    def __init__(self, retry_after):
        super().__init__(f"wait {retry_after}s")
        self.retry_after = retry_after


class Broker:
    """The Ledger's only writer.

    ``table`` is a boto3 DynamoDB ``Table`` resource built from the
    broker's *own* credentials - never an AI-facing session's. ``session``
    (also the broker's own credentials) is used for the one other AWS call
    a broker action makes on a caller's behalf: minting work credentials
    via ``sts:AssumeRole`` in :meth:`vend_work_credentials`. Passing no
    session falls back to the ambient ``boto3`` module-level credentials,
    which is only ever correct for a single-identity test/demo process -
    never for a real broker deployment, where ``session`` must be
    constructed from the broker's own, narrowly-scoped identity.
    """

    def __init__(self, table: Any, policy_table: PolicyTable, session: Any = None):
        self.t = table
        self.policy_table = policy_table
        self.session = session

    def _sts(self):
        if self.session is not None:
            return self.session.client("sts")
        import boto3

        return boto3.client("sts")

    # ------------------------------------------------------------- reads ---

    def get(self, task_id: str) -> dict | None:
        return self.t.get_item(Key={"task_id": task_id}).get("Item")

    def list(self) -> list[dict]:
        """CONTRACT.md s.3: read actions are reachable identically from
        anywhere with the same identity any session already has. A real
        deployment paginates and/or uses a GSI; v1's ``scan()`` is sized
        for the single-table, moderate-task-count shape this project
        states as its own ceiling (CONTRACT.md / design.md Decision 12).
        """
        items = []
        resp = self.t.scan()
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = self.t.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        return [i for i in items if not str(i["task_id"]).startswith("plan:")]

    def _plan(self, plan_ref: str) -> dict:
        item = self.t.get_item(Key={"task_id": plan_ref}).get("Item")
        if item is None:
            raise Rejected("plan not found")
        return item["plan"]

    def _policy(self, kind: str) -> KindPolicy | None:
        return self.policy_table.get(kind)

    # ----------------------------------------------------------- create() --

    def create(self, task_id: str, kind: str, requested_profile: str, resource_binding: dict) -> dict:
        policy = self._policy(kind)
        if policy is None or requested_profile not in policy.allowed_profiles:
            raise Rejected("unknown kind or profile not allowed for this kind")
        has_queue = "cluster_queue" in resource_binding
        has_block = "capacity_block_id" in resource_binding
        if not (has_queue or has_block):
            raise Rejected("resource_binding names neither a cluster_queue nor a capacity_block_id")
        if has_queue and resource_binding["cluster_queue"] not in policy.allowed_queues:
            raise Rejected("cluster_queue not allowed for this kind")
        if has_block and resource_binding["capacity_block_id"] not in policy.allowed_capacity_blocks:
            raise Rejected("capacity_block_id not allowed for this kind")

        plan = {
            "kind": kind,
            "profile": requested_profile,
            "resource_binding": resource_binding,
            "lease_seconds": policy.lease_seconds,
            "timebox_minutes": policy.timebox_minutes,
            "max_cost_usd": policy.max_cost_usd,
            "max_retries": policy.max_retries,
            "work_role": policy.work_role,
            "deny_statements": list(policy.deny_statements),
        }
        plan_ref = plan_ref_for(plan)
        try:
            self.t.put_item(
                Item={"task_id": plan_ref, "plan": plan},
                ConditionExpression="attribute_not_exists(task_id)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            existing = self.t.get_item(Key={"task_id": plan_ref}).get("Item")
            if existing is None or existing["plan"] != plan:
                raise Rejected("plan_ref collision with different content") from e

        requires_approval = policy.requires_approval
        item: dict[str, Any] = {
            "task_id": task_id,
            "plan_ref": plan_ref,
            "version": 0,
            "attempt_count": 0,
            "state": "awaiting_approval" if requires_approval else "pending",
            "created_at": broker_now(),
        }
        if not requires_approval:
            # CONTRACT.md Decision 2: deadline starts NOW for a no-approval
            # kind - nothing to wait on.
            item["deadline_at"] = broker_now() + policy.timebox_minutes * 60
        try:
            self.t.put_item(Item=item, ConditionExpression="attribute_not_exists(task_id)")
            return self.get(task_id)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            existing = self.get(task_id)
            if existing is not None and existing.get("plan_ref") == plan_ref:
                return existing  # true retry of the same request: report success, write nothing
            raise Rejected(f"task_id {task_id} already exists with different content") from e

    # ------------------------------------------------------------- claim() --

    def claim(self, task_id: str, client_token: str):
        """Implements CONTRACT.md s.4's ten-step decision order exactly.
        Returns ``("claimed", item)`` or ``("already_owned", item)``;
        raises :class:`Wait` or :class:`Rejected` otherwise.
        """
        item = self.get(task_id)
        if item is None:
            raise Rejected("no such task")

        # step 1
        if item["state"] in ("completed", "abandoned", "awaiting_approval"):
            raise Rejected(f"not claimable: {item['state']}")

        # step 2 - deadline is an absolute cutoff, checked before anything else
        if broker_now() > item.get("deadline_at", 0):
            self._abandon_internal(task_id, "deadline exceeded", expected_version=None)
            raise Rejected("deadline exceeded")

        # step 3 - recompute, never trust a stored comparison
        plan = self._plan(item["plan_ref"])
        if plan_ref_for(plan) != item["plan_ref"]:
            raise Rejected("plan content does not match plan_ref")

        # step 4 - fail closed if a kind was removed from the policy table
        policy = self._policy(plan["kind"])
        if policy is None:
            raise Rejected("kind removed from policy")

        token_hash = hash_token(client_token)
        owned = item["state"] == "claimed" and item.get("lease_holder_hash") == token_hash

        # step 5/6 - the two-call wait
        if not owned and item["state"] in LIVE_STATES:
            self._arm_or_wait(task_id, item, policy)
            item = self.get(task_id)  # version may have moved; recompute fresh
            owned = item["state"] == "claimed" and item.get("lease_holder_hash") == token_hash

        # step 7 - does this attempt charge the retry budget?
        charge = (not owned) and item["state"] in ("claimed", "failed")

        # step 8/9 - the current policy's approval requirement, re-checked now
        if policy.requires_approval:
            hg = item.get("human_go")
            if (
                hg is None
                or hg.get("plan_hash") != item["plan_ref"]
                or broker_now() > hg.get("expires_at", 0)
            ):
                self.t.update_item(
                    Key={"task_id": task_id},
                    UpdateExpression="SET #s = :aa, version = version + :one",
                    ConditionExpression="version = :v0",
                    ExpressionAttributeNames={"#s": "state"},
                    ExpressionAttributeValues={":aa": "awaiting_approval", ":one": 1, ":v0": item["version"]},
                )
                raise Rejected("awaiting human Go")

        if charge and item["attempt_count"] >= policy.max_retries:
            self._abandon_internal(task_id, "retries exhausted", expected_version=item["version"])
            raise Rejected("retries exhausted")

        if owned:
            return ("already_owned", item)

        # step 10 - the actual compare-and-swap
        self.t.update_item(
            Key={"task_id": task_id},
            UpdateExpression=(
                "SET lease_holder_hash = :h, #s = :claimed, "
                "claimed_at = if_not_exists(claimed_at, :now), "
                "attempt_count = attempt_count + :charge, version = version + :one"
            ),
            ConditionExpression="version = :v0",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":h": token_hash,
                ":claimed": "claimed",
                ":now": broker_now(),
                ":charge": 1 if charge else 0,
                ":one": 1,
                ":v0": item["version"],
            },
        )
        return ("claimed", self.get(task_id))

    def _arm_or_wait(self, task_id: str, item: dict, policy: KindPolicy) -> None:
        """CONTRACT.md s.6: the broker's own clock, never a caller's,
        decides. The marker write does not bump ``version`` - the one
        stated exception - so the comparison it exists to support is not
        invalidated by its own write.
        """
        v0 = item["version"]
        if item.get("wait_started_version") != v0:
            try:
                self.t.update_item(
                    Key={"task_id": task_id},
                    UpdateExpression="SET wait_started_at=:now, wait_started_version=:v0",
                    ConditionExpression=(
                        "version = :v0 AND "
                        "(attribute_not_exists(wait_started_version) OR wait_started_version <> :v0)"
                    ),
                    ExpressionAttributeValues={":now": broker_now(), ":v0": v0},
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                # someone else started the clock for this exact version a
                # moment ago - fall through and let the caller retry rather
                # than treat this as a hard failure.
            raise Wait(policy.lease_seconds)
        elapsed = broker_now() - item["wait_started_at"]
        if elapsed < policy.lease_seconds:
            raise Wait(policy.lease_seconds - elapsed)
        # enough time has passed by this item's own marker; the caller must
        # re-read fresh (done by claim() immediately after this returns)

    def claim_wait_loop(
        self, task_id: str, client_token: str, poll_interval: float = 0.3, max_wait: float = 30
    ):
        """Convenience for a caller that wants to block: repeatedly calls
        ``claim()`` until it stops raising :class:`Wait`. The elapsed-time
        decision itself still happens inside ``claim()``, against the
        broker's own clock - this loop only decides how often to ask again.
        """
        deadline = time.monotonic() + max_wait
        while True:
            try:
                return self.claim(task_id, client_token)
            except Wait:
                if time.monotonic() > deadline:
                    raise
                time.sleep(poll_interval)

    # --------------------------------------------------------- heartbeat() --

    def heartbeat(self, task_id: str, client_token: str) -> bool:
        item = self.get(task_id)
        if (
            item is None
            or item.get("lease_holder_hash") != hash_token(client_token)
            or item["state"] != "claimed"
        ):
            return False
        if broker_now() > item.get("deadline_at", 0):
            return False  # self-fencing signal; claim() handles the state transition
        try:
            self.t.update_item(
                Key={"task_id": task_id},
                UpdateExpression="SET heartbeat_at = :now, version = version + :one",
                ConditionExpression="lease_holder_hash = :h AND #s = :claimed",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={
                    ":now": broker_now(),
                    ":one": 1,
                    ":h": hash_token(client_token),
                    ":claimed": "claimed",
                },
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # -------------------------------------------------------------- park() --

    def park(self, task_id: str, client_token: str, evidence_entry: dict | None = None) -> None:
        item = self.get(task_id)
        if (
            item is None
            or item.get("lease_holder_hash") != hash_token(client_token)
            or item["state"] != "claimed"
        ):
            raise Rejected("not the current holder of a claimed task")
        if broker_now() > item.get("deadline_at", 0):
            raise Rejected("deadline exceeded")  # parking never extends past the deadline
        expr = "SET #s = :parked, version = version + :one"
        vals: dict[str, Any] = {
            ":parked": "parked",
            ":one": 1,
            ":h": hash_token(client_token),
            ":claimed": "claimed",
        }
        if evidence_entry is not None:
            expr += ", evidence = list_append(if_not_exists(evidence, :empty), :ev)"
            vals[":empty"] = []
            vals[":ev"] = [evidence_entry]
        self.t.update_item(
            Key={"task_id": task_id},
            UpdateExpression=expr,
            ConditionExpression="lease_holder_hash = :h AND #s = :claimed",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues=vals,
        )

    def resume(self, task_id: str, client_token: str):
        """Exactly ``claim()`` - no special case for the original parker
        (CONTRACT.md: every resume, including the original parker's own
        first attempt, waits out the same lease_seconds any stranger's
        takeover would, and never charges an attempt - that non-charging
        is claim()'s own, not something resume() adds)."""
        return self.claim(task_id, client_token)

    # ----------------------------------------------------------- release() --

    def release(self, task_id: str, client_token: str, evidence_entry: dict | None = None) -> bool:
        item = self.get(task_id)
        if (
            item is None
            or item.get("lease_holder_hash") != hash_token(client_token)
            or item["state"] != "claimed"
        ):
            return False
        expr = "SET #s = :pending, version = version + :one REMOVE lease_holder_hash"
        vals: dict[str, Any] = {
            ":pending": "pending",
            ":one": 1,
            ":h": hash_token(client_token),
            ":claimed": "claimed",
        }
        if evidence_entry is not None:
            expr = (
                "SET #s = :pending, version = version + :one, "
                "evidence = list_append(if_not_exists(evidence, :empty), :ev) REMOVE lease_holder_hash"
            )
            vals[":empty"] = []
            vals[":ev"] = [evidence_entry]
        try:
            self.t.update_item(
                Key={"task_id": task_id},
                UpdateExpression=expr,
                ConditionExpression="lease_holder_hash = :h AND #s = :claimed",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues=vals,
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # --------------------------------------------------- complete() / fail() --

    def _terminal(self, task_id: str, client_token: str, new_state: str, evidence_entry: dict | None) -> bool:
        item = self.get(task_id)
        if (
            item is None
            or item.get("lease_holder_hash") != hash_token(client_token)
            or item["state"] != "claimed"
        ):
            return False
        expr = "SET #s = :s, version = version + :one REMOVE lease_holder_hash"
        vals: dict[str, Any] = {
            ":s": new_state,
            ":one": 1,
            ":h": hash_token(client_token),
            ":claimed": "claimed",
        }
        if new_state == "completed":
            vals[":ttl"] = broker_now() + 7 * 86400
            expr = (
                "SET #s = :s, version = version + :one, ttl = :ttl, "
                "evidence = list_append(if_not_exists(evidence, :empty), :ev) REMOVE lease_holder_hash"
            )
            vals[":empty"] = []
            vals[":ev"] = [evidence_entry or {}]
        elif evidence_entry is not None:
            expr = (
                "SET #s = :s, version = version + :one, "
                "evidence = list_append(if_not_exists(evidence, :empty), :ev) REMOVE lease_holder_hash"
            )
            vals[":empty"] = []
            vals[":ev"] = [evidence_entry]
        try:
            self.t.update_item(
                Key={"task_id": task_id},
                UpdateExpression=expr,
                ConditionExpression="lease_holder_hash = :h AND #s = :claimed",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues=vals,
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def complete(self, task_id: str, client_token: str, evidence_entry: dict | None = None) -> bool:
        return self._terminal(task_id, client_token, "completed", evidence_entry)

    def fail(self, task_id: str, client_token: str, evidence_entry: dict | None = None) -> bool:
        return self._terminal(task_id, client_token, "failed", evidence_entry)

    # -------------------------------------------------------- append_evidence --

    def append_evidence(self, task_id: str, client_token: str, evidence_entry: dict) -> bool:
        item = self.get(task_id)
        if item is None or item.get("lease_holder_hash") != hash_token(client_token):
            return False
        if item["state"] not in LIVE_STATES:
            return False
        if item["state"] == "claimed" and broker_now() > item.get("deadline_at", 0):
            return False  # cannot keep an over-deadline lease alive via evidence calls
        try:
            self.t.update_item(
                Key={"task_id": task_id},
                UpdateExpression=(
                    "SET version = version + :one, "
                    "evidence = list_append(if_not_exists(evidence, :empty), :ev)"
                ),
                ConditionExpression="lease_holder_hash = :h AND (#s = :claimed OR #s = :parked)",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":empty": [],
                    ":ev": [evidence_entry],
                    ":h": hash_token(client_token),
                    ":claimed": "claimed",
                    ":parked": "parked",
                },
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # -------------------------------------------------------- abandon() etc --

    def _abandon_internal(self, task_id: str, reason: str, expected_version: int | None) -> None:
        cond = "#s <> :completed AND #s <> :abandoned"
        vals: dict[str, Any] = {
            ":s": "abandoned",
            ":one": 1,
            ":ttl": broker_now() + 7 * 86400,
            ":reason": reason,
            ":completed": "completed",
            ":abandoned": "abandoned",
        }
        if expected_version is not None:
            cond += " AND version = :v0"
            vals[":v0"] = expected_version
        try:
            self.t.update_item(
                Key={"task_id": task_id},
                UpdateExpression=(
                    "SET #s = :s, version = version + :one, ttl = :ttl, "
                    "abandon_reason = :reason REMOVE lease_holder_hash"
                ),
                ConditionExpression=cond,
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues=vals,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise  # a version-guarded abandon that lost the race is a no-op, not an error

    def abandon(self, task_id: str, reason: str) -> None:
        """Maintainer-only, external shape (CONTRACT.md s.3)."""
        self._abandon_internal(task_id, reason, expected_version=None)

    def force_release(self, task_id: str, reason: str) -> bool:
        """Maintainer-only break-glass: clears the lease hash but does NOT
        change state and does NOT skip the wait - the next claim() still
        runs the full two-call protocol, never an instant "pending"
        shortcut.
        """
        try:
            self.t.update_item(
                Key={"task_id": task_id},
                UpdateExpression=(
                    "SET version = version + :one, force_released_reason = :r REMOVE lease_holder_hash"
                ),
                ConditionExpression="#s = :claimed OR #s = :parked",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":r": reason,
                    ":claimed": "claimed",
                    ":parked": "parked",
                },
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # -------------------------------------------------------------- sweep() --

    def sweep(self) -> list[tuple[str, str]]:
        """Never calls claim(), never writes lease_holder_hash. The only
        state change it ever makes is abandon() for an objectively
        over-budget task - it never takes ownership of anything (an
        earlier design that had the reaper call ordinary claim() made the
        reaper the new holder of everything it scanned, charging an
        attempt every cycle regardless of whether the original holder was
        ever actually dead).
        """
        notifications: list[tuple[str, str]] = []
        for item in self.list():
            tid = item["task_id"]
            state = item["state"]
            if state in TERMINAL_STATES:
                continue
            if state == "awaiting_approval":
                if "deadline_at" in item and broker_now() > item["deadline_at"]:
                    self._abandon_internal(tid, "deadline exceeded", expected_version=item["version"])
                else:
                    notifications.append((tid, "awaiting a Go"))
                continue
            if broker_now() > item.get("deadline_at", 0):
                self._abandon_internal(tid, "deadline exceeded", expected_version=None)
                continue
            if state in LIVE_STATES:
                plan = self._plan(item["plan_ref"])
                policy = self._policy(plan["kind"])
                if policy is None:
                    continue
                v0 = item["version"]
                if item.get("wait_started_version") != v0:
                    try:
                        self.t.update_item(
                            Key={"task_id": tid},
                            UpdateExpression="SET wait_started_at=:now, wait_started_version=:v0",
                            ConditionExpression=(
                                "version = :v0 AND "
                                "(attribute_not_exists(wait_started_version) OR wait_started_version <> :v0)"
                            ),
                            ExpressionAttributeValues={":now": broker_now(), ":v0": v0},
                        )
                    except ClientError as e:
                        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                            raise
                    continue
                if broker_now() - item["wait_started_at"] < policy.lease_seconds:
                    continue
                if state == "claimed" and item["attempt_count"] >= policy.max_retries:
                    self._abandon_internal(tid, "retries exhausted", expected_version=item["version"])
                else:
                    notifications.append((tid, "reclaimable"))
            elif state == "failed":
                notifications.append((tid, "reclaimable (failed, never retried)"))
        return notifications

    # ------------------------------------------------------------- grant_go --

    def grant_go(self, task_id: str, plan_hash: str, expires_in_seconds: int, granted_by: str) -> None:
        """In a production deployment this is reachable ONLY through the
        MFA- and allow-list-gated front door (CONTRACT.md s.9) - this
        library call is the mechanism the gate protects, not the gate
        itself (see README "Not implemented" for the gate's own status).
        """
        item = self.get(task_id)
        if item is None:
            raise Rejected("no such task")
        plan = self._plan(item["plan_ref"])
        if plan is None:
            raise Rejected("no such task")
        self.t.update_item(
            Key={"task_id": task_id},
            UpdateExpression=(
                "SET human_go = :hg, #s = :pending, version = version + :one, "
                "deadline_at = if_not_exists(deadline_at, :deadline)"
            ),
            ConditionExpression="plan_ref = :ph AND #s = :aa",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":hg": {
                    "granted_by": granted_by,
                    "granted_at": broker_now(),
                    "plan_hash": plan_hash,
                    "expires_at": broker_now() + expires_in_seconds,
                },
                ":pending": "pending",
                ":one": 1,
                ":ph": plan_hash,
                ":aa": "awaiting_approval",
                ":deadline": broker_now() + plan["timebox_minutes"] * 60,
            },
        )

    # ------------------------------------------------------- work credentials --

    def vend_work_credentials(self, task_id: str, client_token: str, duration: int = 900):
        """Mints work credentials by ``AssumeRole``-ing the plan's own
        ``work_role`` with an explicit, Deny-narrowed session policy
        (CONTRACT.md s.5: server-side narrowing, never a second
        client-side chain).
        """
        item = self.get(task_id)
        if (
            item is None
            or item.get("lease_holder_hash") != hash_token(client_token)
            or item["state"] != "claimed"
        ):
            raise Rejected("not the current holder")
        plan = self._plan(item["plan_ref"])
        sts = self._sts()  # the BROKER's own credentials - never a fresh,
        # ambient-credentialed client (CONTRACT.md s.8 item 4).
        account = sts.get_caller_identity()["Account"]
        work_role_arn = f"arn:aws:iam::{account}:role/{plan['work_role']}"
        session_policy = {
            # AWS's documented AssumeRole rule: the EFFECTIVE permissions
            # are the INTERSECTION of the role's identity policy and this
            # session policy - a Deny-only session policy intersects to
            # NOTHING at all (CONTRACT.md s.8 item 5). The correct shape is
            # an Allow-everything-the-role-already-grants baseline PLUS the
            # narrowing Deny on top; the role's own identity policy remains
            # the real ceiling.
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]
            + list(plan.get("deny_statements", [])),
        }
        creds = self._assume(sts, work_role_arn, task_id, duration, session_policy)
        import boto3

        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )

    @staticmethod
    def _assume(sts, role_arn: str, task_id: str, duration: int, session_policy: dict) -> dict:
        import json

        return sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"work-{task_id[:40]}",
            DurationSeconds=duration,
            Policy=json.dumps(session_policy),
        )["Credentials"]
