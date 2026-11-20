"""Pure unit tests - no AWS, no network. These exist because the reference
implementation shipped and then fixed a real bug in exactly this area
(CONTRACT.md s.8 item 2): a plan read back from DynamoDB hashed differently
from the plan ``create()`` originally hashed, because every number comes
back as ``decimal.Decimal`` even when it was written as a plain ``int``.
"""

from decimal import Decimal

from agent_baton.crypto import canonical_json, hash_token, new_client_token, plan_ref_for


def test_canonical_json_is_key_order_independent():
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b


def test_canonical_json_renders_decimal_like_the_native_number_it_came_from():
    """A plan written with a plain int(3) and later read back from DynamoDB
    as Decimal('3') must hash identically - otherwise claim()'s own hash
    recomputation (CONTRACT.md s.4 step 3) rejects every plan for a reason
    that has nothing to do with the plan's own content.
    """
    written = canonical_json({"max_retries": 3})
    read_back = canonical_json({"max_retries": Decimal("3")})
    assert written == read_back == '{"max_retries":3}'


def test_canonical_json_renders_non_integral_decimal_as_float():
    assert canonical_json({"max_cost_usd": Decimal("5.5")}) == '{"max_cost_usd":5.5}'


def test_plan_ref_is_a_stable_content_hash():
    plan = {"kind": "demo", "lease_seconds": 3}
    ref1 = plan_ref_for(plan)
    ref2 = plan_ref_for(dict(plan))  # a fresh dict with the same content
    assert ref1 == ref2
    assert ref1.startswith("plan:sha256:")


def test_plan_ref_changes_with_content():
    assert plan_ref_for({"a": 1}) != plan_ref_for({"a": 2})


def test_hash_token_never_returns_the_plaintext():
    token = "super-secret-token"
    hashed = hash_token(token)
    assert token not in hashed
    assert hashed.startswith("sha256:")


def test_hash_token_is_deterministic():
    token = new_client_token()
    assert hash_token(token) == hash_token(token)


def test_new_client_token_is_not_trivially_guessable():
    tokens = {new_client_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(t) == 64 for t in tokens)  # two uuid4().hex, 32 hex chars each
