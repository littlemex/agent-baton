# CONTRACT

This names what `agent-baton`'s v1 implementation does, exactly, and what it
does not do. It is the thing a reviewer checks an implementation against, not
a design essay — see the originating RFC
([distai-platform#38](https://github.com/littlemex/distai-platform/issues/38))
for the rationale behind each line here.

## 1. Problem this solves

Hand a long-lived task — and the capacity reservation it is bound to — from
one AI session to a different one: safely, without two sessions holding the
same task at once, and without a crashed session stranding a paid reservation
forever.

## 2. What is in v1, what is deferred

| In v1 | Deferred, named in README as not implemented |
| --- | --- |
| `create`, `claim`, `heartbeat`, `release`, `park`, `resume`, `complete`, `fail`, `abandon`, `sweep`, `append_evidence` | `replan`, `create_child`, work-credential renewal |
| Broker-only writes to the Ledger, work credentials minted by `sts:AssumeRole` with a server-side `Deny`-narrowed session policy | Kueue/resource-scheduler adapter, Capacity Block end-time enforcement |
| A maintainer-owned `grant_go` write, bound to a plan hash, with expiry | The MFA- and allow-list-gated API Gateway front door (Decision 6) — v1's `grant_go` is a plain library call any caller of the `Broker` class with broker credentials can make; wiring it behind a real gated endpoint is a separate issue |
| `force_release` | — |
| A CLI (`agent-baton create/claim/...`) over the same library | A hosted broker service (Lambda) — v1 runs the `Broker` class in-process against real DynamoDB, with the caller's own AWS credentials; nothing here deploys a service |

v1 therefore proves the Ledger's own state machine and permission boundary
(as library + CLI, exercised against real DynamoDB/IAM/STS), not the full
production topology (Lambda-fronted broker, API Gateway MFA gate, Kueue
binding). Each deferred item has a tracking issue (see `ISSUES/`).

## 3. API (the `Broker` class, and what the CLI exposes)

All of these mutate the Ledger. **Nothing else may write it** — see §5.

| Operation | Caller | Effect |
| --- | --- | --- |
| `create(task_id, kind, profile, resource_binding)` | any allow-listed caller | Writes an immutable plan item (if new) and a task item in `pending` (no-approval `kind`) or `awaiting_approval` |
| `claim(task_id, client_token)` | any allow-listed caller | Returns `("claimed", item)`, `("already_owned", item)`, raises `Wait(retry_after)`, or raises `Rejected(reason)`. See §4 for the full decision order |
| `heartbeat(task_id, client_token)` | current holder | Renews the lease; `True`/`False` |
| `park(task_id, client_token, evidence=None)` | current holder | `claimed` → `parked`; no attempt charge |
| `resume(task_id, client_token)` | anyone | Identical to `claim()` — no special case for the original parker |
| `release(task_id, client_token, evidence=None)` | current holder | `claimed` → `pending`; no attempt charge |
| `complete(task_id, client_token, evidence=None)` | current holder | `claimed` → `completed`; sets `ttl` |
| `fail(task_id, client_token, evidence=None)` | current holder | `claimed` → `failed` |
| `abandon(task_id, reason)` | maintainer only | → `abandoned`, any non-terminal state |
| `force_release(task_id, reason)` | maintainer only | Clears `lease_holder_hash`; does not change state, does not skip the wait |
| `grant_go(task_id, plan_hash, expires_in_seconds, granted_by)` | maintainer only | Conditional on `plan_hash` matching the task's *current* `plan_ref` |
| `append_evidence(task_id, client_token, entry)` | current holder (`claimed` or `parked`) | Appends to the bounded `evidence` list |
| `sweep()` | scheduled / session-start | Never claims; only ever calls `abandon()` internally for an objectively over-budget task; otherwise only notifies |
| `vend_work_credentials(task_id, client_token, duration=900)` | current holder | `sts:AssumeRole` on the plan's `work_role`, with an `Allow: *` baseline session policy plus the profile's narrowing `Deny` statements |
| `get(task_id)` / `list()` | anyone | Read-only |

## 4. `claim()`'s decision order (normative — an implementation that reorders this is wrong)

1. Reject outright if the task is `completed`, `abandoned`, or `awaiting_approval`.
2. If `broker_now() > deadline_at`, abandon and reject — before computing anything else.
3. Recompute `plan_ref` from the plan's own stored content; reject on mismatch (never trust a stored comparison).
4. If this `kind`'s policy no longer exists, reject (fail closed).
5. Determine `owned` = state is `claimed` and `lease_holder_hash == sha256(client_token)`.
6. If live (`claimed` or `parked`) and not owned: run the two-call wait (§6); only proceed past this step once the task is confirmed not live, or live-and-stale.
7. Decide whether this attempt charges `attempt_count` (a stale takeover, or a `failed → claimed` retry — **never** a graceful release-then-claim or a park-then-resume).
8. If charging and `attempt_count >= max_retries`, abandon and reject.
9. Check whether the **current** policy requires a Go (not only at creation time); if required and no valid, matching, unexpired grant exists, move to `awaiting_approval` and reject.
10. Write the claim, conditioned on `version` unchanged since the value confirmed in step 5/6 — the actual compare-and-swap. Every write in steps 7–10 carries this same condition, independently.

## 5. Permission boundary (Decision 5)

- Every write to the Ledger table goes through the `Broker` class's own AWS
  credentials. No other IAM principal is ever granted a Ledger write action.
  v1's tests prove this with a real, denied `UpdateItem` call from a
  separately-scoped role, not only by the absence of a code path that happens
  not to try.
- `vend_work_credentials()` always builds its STS client from the broker's
  **own** session — never a fresh, ambient-credentialed client. (This exact
  bug was caught and fixed in the prototype; see §8.)
- A session policy passed to `AssumeRole` is an **intersection**, not a
  blocklist: the correct shape is `Allow: *` (the target role's own identity
  policy is the real ceiling) plus the intended `Deny` on top. A `Deny`-only
  session policy intersects to nothing.
- `lease_seconds`, `timebox_minutes`, `max_cost_usd`, `max_retries`,
  `requires_approval`, and `allowed_profiles`/`allowed_queues`/
  `allowed_capacity_blocks` all live in a maintainer-owned `KIND_POLICY`
  table, read by `create()`/`claim()`/`grant_go()` only. No caller-supplied
  parameter can shorten a wait or add a `kind`.

## 6. The two-call wait (Decision 4)

A takeover never compares any caller's local clock. The first call that finds
a live, not-owned task writes a `wait_started_at`/`wait_started_version`
marker on the **same item** (this single write does not increment `version`
— the one stated exception) and raises `Wait(lease_seconds)`. Every later
call is a fresh, complete re-run of `claim()` from step 1 — never a resumed
continuation. Only once `lease_seconds` has elapsed **and** `version` is
unchanged since the marker does the caller proceed to the claiming write.
`lease_seconds` is sized for a heartbeat cadence shorter than it, not for one
tool call's duration; the same caller need not make both calls.

## 7. Data shapes (DynamoDB, single table, partition key `task_id`)

Task item and plan item schemas are exactly `design.md`'s Decision 2 (see the
Issue). `plan_ref` is `"plan:sha256:" + sha256(canonical_json(plan))`, where
`canonical_json` is `json.dumps(obj, sort_keys=True, separators=(",", ":"))`
with a `Decimal → int|float` encoder (DynamoDB hands every number back as
`Decimal`, even one written as a plain `int` — without this, a read-back plan
hashes differently from the one `create()` wrote, for reasons that have
nothing to do with the plan's own content). `lease_holder_hash` is
`"sha256:" + sha256(client_token)` — the plaintext token is never stored.

## 8. Known-fixed implementation mistakes (do not reintroduce)

These are the five real bugs the second prototype run caught and fixed,
carried forward as regression tests, not just prose:

1. A parameter-name mistake in the write-skip ("already owned") path.
2. The `Decimal`/`float` serialization mismatch above.
3. A DynamoDB reserved-keyword collision (`plan` is reserved — use
   `ExpressionAttributeNames`).
4. `vend_work_credentials()` building a fresh, ambient-credentialed STS
   client instead of the broker's own session.
5. An `AssumeRole` session policy with only a `Deny` statement, intersecting
   to zero permissions (needs an explicit `Allow: *` baseline first).

## 9. Out of scope for v1 (stated, not silently dropped)

- Kueue/resource-scheduler object naming and adoption (Scenario A) — needs a
  live EKS cell; deferred to the first real consumer.
- Real hardware MFA and the API Gateway dual-`Deny` gate (Decision 6) — v1's
  `grant_go` is a plain, broker-credentialed library call.
- `max_cost_usd` and Capacity-Block-end-time enforcement — policy-table
  values for a human to reason about, not a live check.
- Exactly-once execution — the guarantee is at-least-once with idempotent
  steps; an already-in-flight side effect from a deposed holder is not
  fenced by this mechanism.
- `replan()`, `create_child()`, work-credential renewal, a hosted broker
  service, a dashboard.

## 10. Acceptance (what "done" means for v1)

The four scenarios (`race`, `lease-and-takeover`, `approval-gate`,
`broker-boundary`) from the RFC's Verification section, re-run against this
repository's own implementation on real AWS (DynamoDB/IAM/STS), all passing,
with every created resource confirmed deleted afterward by a direct,
separate `describe`/`get` call — never by trusting a cleanup script's exit
code.
