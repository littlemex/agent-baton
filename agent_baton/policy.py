"""The maintainer-owned policy table.

CONTRACT.md s.5: ``lease_seconds``, ``timebox_minutes``, ``max_cost_usd``,
``max_retries``, ``requires_approval``, and the allow-lists of profiles,
queues and Capacity Blocks all live here - never as a caller-supplied
parameter to ``create()``/``claim()``. An AI session can request a ``kind``,
but cannot request one this table does not define, and cannot shorten a
wait by passing its own value.

This is deliberately *not* a global, hardcoded dict inside ``ledger.py``: a
maintainer builds a ``PolicyTable`` (plain dict of ``KindPolicy``) however
they like - inline, from a JSON/YAML file, from a secrets manager - and
passes it to ``Broker.__init__``. ``ledger.py`` itself names no ``kind``
and defines no policy: the mechanism is generic, the vocabulary is the
caller's own (see the global rule this project follows: a mechanism that
hardcodes which closed-vocabulary values are "useful" stops being a
mechanism).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class KindPolicy:
    """One row of the policy table, keyed by ``kind`` in a ``PolicyTable``."""

    allowed_profiles: tuple[str, ...]
    work_role: str
    lease_seconds: int
    timebox_minutes: int
    max_cost_usd: float
    max_retries: int
    requires_approval: bool
    allowed_queues: tuple[str, ...] = field(default_factory=tuple)
    allowed_capacity_blocks: tuple[str, ...] = field(default_factory=tuple)
    # Session-policy Deny statements layered on top of the Allow-everything
    # baseline when vend_work_credentials() assumes work_role (see
    # Broker.vend_work_credentials and CONTRACT.md s.5/s.8 item 5). Each
    # entry is a plain IAM statement dict, e.g.
    # {"Effect": "Deny", "Action": "ssm:GetParameter", "Resource": "..."}.
    deny_statements: tuple[dict, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if self.timebox_minutes <= 0:
            raise ValueError("timebox_minutes must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if not self.allowed_profiles:
            raise ValueError("allowed_profiles must name at least one profile")


PolicyTable = Mapping[str, KindPolicy]


def validate_policy_table(policy_table: PolicyTable) -> None:
    """Fail fast and loud at startup rather than inside a live claim().

    Every ``kind`` must resolve a plan to at least one concrete resource
    binding target (a queue or a Capacity Block) it is allowed to use;
    a kind naming neither is a policy authoring mistake, not a runtime
    condition ``claim()`` should have to reject one caller at a time.
    """
    for kind, policy in policy_table.items():
        if not isinstance(policy, KindPolicy):
            raise TypeError(f"policy_table[{kind!r}] is not a KindPolicy")
        if not policy.allowed_queues and not policy.allowed_capacity_blocks:
            raise ValueError(
                f"kind {kind!r} allows neither a queue nor a Capacity Block - "
                "no resource_binding could ever be created for it"
            )


def load_policy_table_from_json(path: str) -> PolicyTable:
    """Load a maintainer-owned policy table from a JSON file, for the CLI
    and for operators who do not want to write Python. Shape (a mapping of
    ``kind`` to the :class:`KindPolicy` fields, as a plain JSON object)::

        {
          "verify-run-light": {
            "allowed_profiles": ["verify-run-light-profile"],
            "work_role": "agent-baton-work-verify-light",
            "lease_seconds": 600,
            "timebox_minutes": 180,
            "max_cost_usd": 60,
            "max_retries": 2,
            "requires_approval": false,
            "allowed_capacity_blocks": ["cr-..."]
          }
        }
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    table: dict[str, KindPolicy] = {}
    for kind, fields_ in raw.items():
        kwargs = dict(fields_)
        for list_field in ("allowed_profiles", "allowed_queues", "allowed_capacity_blocks"):
            if list_field in kwargs:
                kwargs[list_field] = tuple(kwargs[list_field])
        if "deny_statements" in kwargs:
            kwargs["deny_statements"] = tuple(kwargs["deny_statements"])
        table[kind] = KindPolicy(**kwargs)
    validate_policy_table(table)
    return table
