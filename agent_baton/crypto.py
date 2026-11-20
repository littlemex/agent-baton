"""Hashing, canonical encoding, and the broker's own clock.

Every function here exists because of a real mistake the reference
implementation made and fixed once (see ``CONTRACT.md`` ss.8). Changing the
encoding here without re-running the plan-hash-recomputation tests is how
that mistake comes back.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from decimal import Decimal
from typing import Any


def broker_now() -> Decimal:
    """The broker's OWN clock. Every elapsed-time decision in claim()/sweep()
    compares this value against a value this same process wrote earlier -
    never a value a caller supplies, and never compared across machines.

    Decimal, built via ``str()`` (never ``float()`` directly): boto3's
    DynamoDB resource API refuses a native Python ``float`` outright
    ("Float types are not supported"). Going through ``str()`` avoids
    binary-float imprecision leaking into the ``Decimal`` boto3 does accept.
    """
    return Decimal(str(time.time()))


def _decimal_default(o: Any) -> Any:
    """json.dumps() default= hook: renders a Decimal exactly as plain
    json.dumps would render the native int/float it came from.

    boto3's DynamoDB resource API returns every number read back from the
    table as decimal.Decimal, even one written as a plain Python int. A
    read-back plan item would otherwise hash differently from the plan
    create() originally hashed, breaking claim()'s own hash recomputation
    for a reason that has nothing to do with the plan's own content.
    """
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def canonical_json(obj: Any) -> str:
    """The one, fixed encoding every plan hash is computed from. Sorted keys
    and no incidental whitespace, so the same plan content always produces
    the same bytes regardless of dict insertion order or a DynamoDB
    round-trip.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_decimal_default)


def plan_ref_for(plan: dict) -> str:
    """The plan's content address. claim() recomputes this from the plan's
    own stored content on every single claim, rather than trusting a stored
    comparison (CONTRACT.md s.4 step 3).
    """
    return "plan:sha256:" + hashlib.sha256(canonical_json(plan).encode()).hexdigest()


def hash_token(client_token: str) -> str:
    """The Ledger never stores a client_token in plaintext - only this
    hash, as ``lease_holder_hash``.
    """
    return "sha256:" + hashlib.sha256(client_token.encode()).hexdigest()


def new_client_token() -> str:
    """256 bits from a CSPRNG. A guessable token would let a stranger forge
    ``already_owned`` or a heartbeat for a lease it never won.
    """
    return uuid.uuid4().hex + uuid.uuid4().hex
