"""``agent-baton``: a thin CLI over :class:`agent_baton.ledger.Broker`.

This is a convenience wrapper for operating the library from a shell or a
Claude Code hook, run with whatever AWS credentials the caller already has
- it does not itself enforce the permission boundary CONTRACT.md s.5
describes (that boundary lives in IAM, on the credentials the caller
supplies, not in this CLI). It is not the production broker service either
(CONTRACT.md s.2): there is no network endpoint here, no MFA gate - this
process *is* the broker, for whoever can run it.
"""

from __future__ import annotations

import argparse
import json
import sys

import boto3

from agent_baton.ledger import Broker, Rejected, Wait, new_client_token
from agent_baton.policy import load_policy_table_from_json


def _broker(args) -> Broker:
    session = boto3.Session(region_name=args.region) if args.region else boto3.Session()
    table = session.resource("dynamodb").Table(args.table)
    policy_table = load_policy_table_from_json(args.policy_file)
    return Broker(table, policy_table, session=session)


def _print(obj) -> None:
    print(json.dumps(obj, default=str, indent=2, sort_keys=True))


def cmd_create(args) -> int:
    b = _broker(args)
    resource_binding = json.loads(args.resource_binding)
    try:
        item = b.create(args.task_id, args.kind, args.profile, resource_binding)
    except Rejected as e:
        print(f"rejected: {e.reason}", file=sys.stderr)
        return 1
    _print(item)
    return 0


def cmd_claim(args) -> int:
    b = _broker(args)
    token = args.token or new_client_token()
    try:
        status, item = b.claim(args.task_id, token)
    except Wait as w:
        print(json.dumps({"status": "wait", "retry_after": float(w.retry_after), "token": token}))
        return 2
    except Rejected as e:
        print(f"rejected: {e.reason}", file=sys.stderr)
        return 1
    _print({"status": status, "token": token, "item": item})
    return 0


def cmd_heartbeat(args) -> int:
    ok = _broker(args).heartbeat(args.task_id, args.token)
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


def cmd_release(args) -> int:
    ok = _broker(args).release(args.task_id, args.token)
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


def cmd_park(args) -> int:
    try:
        _broker(args).park(args.task_id, args.token)
    except Rejected as e:
        print(f"rejected: {e.reason}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True}))
    return 0


def cmd_resume(args) -> int:
    return cmd_claim(args)


def cmd_complete(args) -> int:
    ok = _broker(args).complete(args.task_id, args.token)
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


def cmd_fail(args) -> int:
    ok = _broker(args).fail(args.task_id, args.token)
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


def cmd_abandon(args) -> int:
    _broker(args).abandon(args.task_id, args.reason)
    print(json.dumps({"ok": True}))
    return 0


def cmd_force_release(args) -> int:
    ok = _broker(args).force_release(args.task_id, args.reason)
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


def cmd_grant_go(args) -> int:
    _broker(args).grant_go(args.task_id, args.plan_hash, args.expires_in_seconds, args.granted_by)
    print(json.dumps({"ok": True}))
    return 0


def cmd_sweep(args) -> int:
    _print(_broker(args).sweep())
    return 0


def cmd_show(args) -> int:
    item = _broker(args).get(args.task_id)
    if item is None:
        print("not found", file=sys.stderr)
        return 1
    _print(item)
    return 0


def cmd_list(args) -> int:
    _print(_broker(args).list())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-baton")
    p.add_argument("--table", required=True, help="DynamoDB Ledger table name")
    p.add_argument(
        "--policy-file", required=True, help="path to a JSON policy table (see agent_baton.policy)"
    )
    p.add_argument("--region", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("create")
    sp.add_argument("task_id")
    sp.add_argument("kind")
    sp.add_argument("profile")
    sp.add_argument("resource_binding", help='JSON object, e.g. \'{"capacity_block_id": "cr-..."}\'')
    sp.set_defaults(fn=cmd_create)

    sp = sub.add_parser("claim")
    sp.add_argument("task_id")
    sp.add_argument("--token", default=None, help="client token; a fresh one is generated if omitted")
    sp.set_defaults(fn=cmd_claim)

    sp = sub.add_parser("heartbeat")
    sp.add_argument("task_id")
    sp.add_argument("token")
    sp.set_defaults(fn=cmd_heartbeat)

    sp = sub.add_parser("release")
    sp.add_argument("task_id")
    sp.add_argument("token")
    sp.set_defaults(fn=cmd_release)

    sp = sub.add_parser("park")
    sp.add_argument("task_id")
    sp.add_argument("token")
    sp.set_defaults(fn=cmd_park)

    sp = sub.add_parser("resume")
    sp.add_argument("task_id")
    sp.add_argument("--token", required=True)
    sp.set_defaults(fn=cmd_resume)

    sp = sub.add_parser("complete")
    sp.add_argument("task_id")
    sp.add_argument("token")
    sp.set_defaults(fn=cmd_complete)

    sp = sub.add_parser("fail")
    sp.add_argument("task_id")
    sp.add_argument("token")
    sp.set_defaults(fn=cmd_fail)

    sp = sub.add_parser("abandon", description="maintainer-only")
    sp.add_argument("task_id")
    sp.add_argument("reason")
    sp.set_defaults(fn=cmd_abandon)

    sp = sub.add_parser("force-release", description="maintainer-only")
    sp.add_argument("task_id")
    sp.add_argument("reason")
    sp.set_defaults(fn=cmd_force_release)

    sp = sub.add_parser("grant-go", description="maintainer-only")
    sp.add_argument("task_id")
    sp.add_argument("plan_hash")
    sp.add_argument("--expires-in-seconds", type=int, default=3600)
    sp.add_argument("--granted-by", required=True)
    sp.set_defaults(fn=cmd_grant_go)

    sp = sub.add_parser("sweep")
    sp.set_defaults(fn=cmd_sweep)

    sp = sub.add_parser("show")
    sp.add_argument("task_id")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("list")
    sp.set_defaults(fn=cmd_list)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
