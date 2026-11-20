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
from decimal import Decimal
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
    (also the broker's own credentials) is **required**, not optional: it is
    used for the one other AWS call a broker action makes on a caller's
    behalf - minting work credentials via ``sts:AssumeRole`` in
    :meth:`vend_work_credentials`. An earlier draft of this class let
    ``session`` default to ``None`` and silently fell back to the ambient
    ``boto3`` module-level credentials, which is exactly the bug CONTRACT.md
    s.8 item 4 already named as fixed once (in ``vend_work_credentials``
    itself) and reintroduced one layer up, in the constructor - caught by
    review, not by any test. There is no safe default here: a caller that
    forgets to pass its own narrow identity must fail loudly, not run with
    whatever ambient credentials happen to be present.
    """

    def __init__(self, table: Any, policy_table: PolicyTable, session: Any):
        self.t = table
        self.policy_table = policy_table
        self.session = session

    def _sts(self):
        return self.session.client("sts")

    # ------------------------------------------------------------- reads ---

    def get(self, task_id: str) -> dict | None:
        # ConsistentRead: every caller of get() uses its result to decide an
        # ownership- or security-relevant question (claim()'s initial read,
        # heartbeat/park/release/_terminal's holder check,
        # vend_work_credentials()'s "is this still the current holder").
        # An eventually consistent read could show a just-released or
        # just-abandoned task as still claimed for a brief window.
        return self.t.get_item(Key={"task_id": task_id}, ConsistentRead=True).get("Item")

    def list_tasks(self) -> list[dict]:
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

    @staticmethod
    def _has_valid_go(item: dict, policy: KindPolicy) -> bool:
        """Shared by claim() (step 8) and sweep() - round-2 review finding
        F3: the two had drifted (sweep() abandoned a stale, over-budget
        task without this check at all), which CONTRACT.md s.3 says must
        never happen."""
        if not policy.requires_approval:
            return True
        hg = item.get("human_go")
        if hg is None or hg.get("plan_hash") != item["plan_ref"]:
            return False
        return broker_now() <= hg.get("expires_at", 0)

    # ----------------------------------------------------------- create() --

    def create(self, task_id: str, kind: str, requested_profile: str, resource_binding: dict) -> dict:
        if task_id.startswith("plan:"):
            # "plan:" is the reserved prefix plan items use in this same
            # partition-key space (Decision 2's single-table overload). A
            # task_id in that namespace would be invisible to list_tasks()'s
            # own filter and could collide with a real plan item's key.
            raise Rejected("task_id may not start with the reserved prefix 'plan:'")
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
            # boto3's DynamoDB resource API refuses a native Python float
            # outright - any policy with a non-integer max_cost_usd (the
            # field's own declared type, e.g. 60.5) would otherwise make
            # create() raise TypeError. str() first, same as crypto.broker_now(),
            # to avoid binary-float imprecision leaking into the Decimal.
            "max_cost_usd": Decimal(str(policy.max_cost_usd)),
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
            return item  # the item we just wrote, not a second read
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

        # step 5/6 - the two-call wait. _arm_or_wait raises Wait() until it can
        # confirm, by its own fresh read, that `item`'s own version has been
        # unchanged for at least lease_seconds; it returns normally only once
        # that is true. `item` (read once, at the very top of this call) is
        # therefore ALREADY the confirmed-stale snapshot - CONTRACT.md s.4
        # step 10 requires every subsequent write to condition on this exact
        # version, not a fresh re-read's. A fresh re-read here was an earlier,
        # real bug (caught by review, not by any test): it let a write that
        # lands in the gap between the staleness check and this call's own
        # claiming write go undetected, because the re-read would pick up
        # that very write's new version and condition on IT instead of on
        # the version that was actually confirmed stale - defeating the
        # central guarantee this two-call protocol exists to provide. `owned`
        # is already correctly False here (that is why this branch was
        # entered at all), so nothing needs recomputing.
        if not owned and item["state"] in LIVE_STATES:
            self._arm_or_wait(task_id, item, policy)

        # step 7 - does this attempt charge the retry budget? `pending_charge`
        # carries a "yes" across an approval detour (step 8, below): a stale
        # takeover or failed-retry that gets redirected to awaiting_approval
        # must still be charged once a fresh Go lets it reach this point
        # again as a "pending" task - otherwise the retry cap is bypassable
        # by repeatedly re-granting a Go (round-2 review finding F2).
        charge = (not owned) and (
            item["state"] in ("claimed", "failed") or item.get("pending_charge") is True
        )

        # step 8/9 - the current policy's approval requirement, re-checked now
        if not self._has_valid_go(item, policy):
            try:
                self.t.update_item(
                    Key={"task_id": task_id},
                    UpdateExpression="SET #s = :aa, version = version + :one, pending_charge = :pc",
                    ConditionExpression="version = :v0",
                    ExpressionAttributeNames={"#s": "state"},
                    ExpressionAttributeValues={
                        ":aa": "awaiting_approval",
                        ":one": 1,
                        ":pc": charge,
                        ":v0": item["version"],
                    },
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                # the holder renewed (or someone else wrote) in the gap
                # between the confirmed-stale read and this write - the
                # task is no longer in the state this decision was made
                # against; re-run claim() from a fresh read instead of
                # surfacing a raw ClientError to the caller.
                return self.claim(task_id, client_token)
            raise Rejected("awaiting human Go")

        if charge and item["attempt_count"] >= policy.max_retries:
            if not self._abandon_internal(task_id, "retries exhausted", expected_version=item["version"]):
                # the task changed under us (e.g. the holder renewed) - the
                # abandon did not land, so this is no longer a confirmed
                # over-budget task; re-run claim() fresh rather than report
                # a terminal rejection for a task that is still live.
                return self.claim(task_id, client_token)
            raise Rejected("retries exhausted")

        if owned:
            return ("already_owned", item)

        # step 10 - the actual compare-and-swap, conditioned on the version
        # confirmed stale (or owned) in step 5/6 - never a fresher one.
        try:
            self.t.update_item(
                Key={"task_id": task_id},
                UpdateExpression=(
                    "SET lease_holder_hash = :h, #s = :claimed, "
                    "claimed_at = if_not_exists(claimed_at, :now), "
                    "attempt_count = attempt_count + :charge, version = version + :one "
                    "REMOVE pending_charge"
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
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # this is the central guarantee the two-call wait exists to
            # provide: a holder that renewed (or anyone else who wrote) in
            # the gap between the staleness check and this write makes this
            # write fail. Re-run claim() fresh rather than report a false
            # success or leak a raw botocore error.
            return self.claim(task_id, client_token)
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
        # enough time has passed, by this item's own marker, with version
        # unchanged throughout - `item` IS the confirmed-stale snapshot.
        # claim() must use THIS version for its own CAS, never a fresh
        # re-read taken after this point returns (that was a real bug,
        # fixed - see CONTRACT.md s.8 item 7 - do not reintroduce it here).

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
        try:
            self.t.update_item(
                Key={"task_id": task_id},
                UpdateExpression=expr,
                ConditionExpression="lease_holder_hash = :h AND #s = :claimed",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues=vals,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            raise Rejected("lease lost before park() could commit") from e

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
        names = {"#s": "state"}
        if new_state == "completed":
            vals[":ttl"] = broker_now() + 7 * 86400
            names["#t"] = "ttl"  # "ttl" is a DynamoDB reserved keyword, same class of
            # bug as CONTRACT.md s.8 item 3 ("plan"); caught by a real Docker-based
            # smoke test running complete() against real DynamoDB, not by any mock.
            expr = (
                "SET #s = :s, version = version + :one, #t = :ttl, "
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
                ExpressionAttributeNames=names,
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

    def _abandon_internal(self, task_id: str, reason: str, expected_version: int | None) -> bool:
        """Returns whether this call actually committed the abandon. A
        version-guarded call that loses the race (the task changed under
        it - e.g. the holder renewed) is a no-op, not an error - but
        `claim()`'s own retry-cap check (step 9) must know the difference,
        rather than assuming the abandon always lands (CONTRACT.md s.8
        item 13 / round-2 review finding F1).
        """
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
                    "SET #s = :s, version = version + :one, #t = :ttl, "
                    "abandon_reason = :reason REMOVE lease_holder_hash"
                ),
                ConditionExpression=cond,
                ExpressionAttributeNames={"#s": "state", "#t": "ttl"},
                ExpressionAttributeValues=vals,
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return False  # lost the race - a no-op, not an error, but not a commit either

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
        """Never calls claim(), never writes lease_holder_hash, and never
        takes ownership of anything (an earlier design that had the reaper
        call ordinary claim() made the reaper the new holder of everything
        it scanned, charging an attempt every cycle regardless of whether
        the original holder was ever actually dead). The only state changes
        it makes are abandon() for an objectively over-budget task, and - to
        stay in step with claim()'s own order (CONTRACT.md s.8 item 15) -
        moving a stale claimed task with no valid Go to awaiting_approval.
        """
        notifications: list[tuple[str, str]] = []
        for item in self.list_tasks():
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
                if state in LIVE_STATES and not self._has_valid_go(item, policy):
                    # mirrors claim() step 8 (which resume() reaches too,
                    # being exactly claim()): the Go requirement applies to
                    # a stale PARKED task the same as a stale CLAIMED one,
                    # not just an over-budget one (round-2 review finding
                    # F3 - sweep() had drifted from claim()'s own order
                    # here; leaving `parked` out would also have reported a
                    # misleadingly plain "reclaimable" for a task whose next
                    # claim()/resume() actually lands in awaiting_approval).
                    try:
                        self.t.update_item(
                            Key={"task_id": tid},
                            UpdateExpression="SET #s = :aa, version = version + :one, pending_charge = :pc",
                            ConditionExpression="version = :v0",
                            ExpressionAttributeNames={"#s": "state"},
                            ExpressionAttributeValues={
                                ":aa": "awaiting_approval",
                                ":one": 1,
                                # sweep() always treats a stale claimed task
                                # as a presumed takeover and charges it - it
                                # has no caller/token to ask "owned?" the way
                                # claim() does. If the ORIGINAL holder is the
                                # one who eventually claims after the fresh
                                # Go, it is charged here too; this matches
                                # how sweep() already treats any stale
                                # claimed task, not a new asymmetry.
                                ":pc": True,
                                ":v0": item["version"],
                            },
                        )
                    except ClientError as e:
                        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                            raise
                        # lost the race (the holder renewed) - nothing to
                        # do; the next sweep cycle re-evaluates from fresh
                elif state == "claimed" and item["attempt_count"] >= policy.max_retries:
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
        try:
            self._grant_go_write(task_id, plan_hash, granted_by, expires_in_seconds, plan)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # the plan_hash this grant names does not match the task's
            # CURRENT plan_ref, or the task is no longer awaiting_approval -
            # either way, a clean Rejected, never a raw botocore error.
            raise Rejected(
                "grant_go rejected: plan_hash does not match the current plan, "
                "or the task is no longer awaiting_approval"
            ) from e

    def _grant_go_write(
        self, task_id: str, plan_hash: str, granted_by: str, expires_in_seconds: int, plan: dict
    ) -> None:
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
