"""agent-baton: a crash-safe task ledger for handing an in-flight task - and
the paid capacity reservation it is bound to - from one AI agent session to a
different one.

See ``CONTRACT.md`` at the repository root for the normative description of
the API, the state machine, and the permission boundary this package
implements. This module only re-exports the small, stable public surface.
"""

from agent_baton.crypto import hash_token, new_client_token, plan_ref_for
from agent_baton.ledger import Broker, Rejected, Wait
from agent_baton.policy import KindPolicy, PolicyTable, load_policy_table_from_json, validate_policy_table

__all__ = [
    "Broker",
    "KindPolicy",
    "PolicyTable",
    "Rejected",
    "Wait",
    "hash_token",
    "load_policy_table_from_json",
    "new_client_token",
    "plan_ref_for",
    "validate_policy_table",
]

__version__ = "0.1.0"
