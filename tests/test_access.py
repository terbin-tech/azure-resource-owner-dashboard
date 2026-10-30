from __future__ import annotations

import base64
import json
import logging

import pytest

from dashboard.access import (
    OID_CLAIMS,
    Decision,
    assignment_filter,
    may_view,
    object_id,
)
from tests.fakes import (
    FakeClients,
    FakeRoleAssignments,
    FakeSubscriptionClient,
    http_error,
    sdk_subscription,
)
from tests.helpers import SUB, SUB2, principal

OID = "00000000-0000-0000-0000-000000000011"
OID_CLAIM, SHORT_CLAIM = OID_CLAIMS
HEADER = "X-MS-CLIENT-PRINCIPAL"


def encoded(value: str) -> dict[str, str]:
    return {HEADER: base64.b64encode(value.encode()).decode()}


def test_object_id_from_the_principal_claims():
    headers = principal([(OID_CLAIM, OID)])
    assert object_id({"x-ms-client-principal": headers[HEADER]}) == OID
    upper = principal([(OID_CLAIM, OID.upper())])
    assert object_id({HEADER.upper(): upper[HEADER]}) == OID
    assert object_id(principal([(SHORT_CLAIM, OID)])) == OID


def test_object_id_missing_or_broken_is_none():
    cases = [
        {},
        {HEADER: "not base64!"},
        encoded("not json"),
        encoded(json.dumps({"claims": "x"})),
        principal([("name", "student-a"), ("tid", OID)]),
        encoded(json.dumps({"claims": [{"typ": OID_CLAIM, "val": 5}]})),
        encoded(json.dumps({"claims": ["x"]})),
    ]
    for headers in cases:
        assert object_id(headers) is None


def test_object_id_must_be_a_guid():
    assert object_id(principal([(OID_CLAIM, "x') or true or ('")])) is None


def test_filter_refuses_anything_but_a_guid():
    assert assignment_filter(OID) == f"atScope() and assignedTo('{OID}')"
    for bad in ("x') or true or ('", "", OID + "\n"):
        with pytest.raises(ValueError):
            assignment_filter(bad)


def clients(*assignments: FakeRoleAssignments, empty: bool = False) -> FakeClients:
    ids = [SUB, SUB2][: len(assignments)]
    subs = [] if empty else [sdk_subscription(sub, f"sub {sub[-1]}") for sub in ids]
    return FakeClients(
        FakeSubscriptionClient(subs), authorizations=dict(zip(ids, assignments, strict=False))
    )


def test_any_assignment_on_any_subscription_allows():
    first, second = FakeRoleAssignments([]), FakeRoleAssignments(["assignment"])
    assert may_view(clients(first, second), OID) == Decision.ALLOW
    assert first.filters == second.filters == [assignment_filter(OID)]


def test_no_assignment_anywhere_denies():
    assert may_view(clients(FakeRoleAssignments([]), FakeRoleAssignments([])), OID) == Decision.DENY


@pytest.mark.parametrize(
    "error", [http_error("AuthorizationFailed"), http_error("TooManyRequests", 429)]
)
def test_role_read_failure_is_an_error_not_a_deny(error, caplog):
    both = clients(FakeRoleAssignments([]), FakeRoleAssignments(error=error))
    with caplog.at_level(logging.DEBUG):
        assert may_view(both, OID) == Decision.ERROR
    messages = [r.getMessage() for r in caplog.records]
    assert f"role check failed: {error.error.code}" in messages
    assert all(OID not in m for m in messages)


def test_subscription_list_failure_is_an_error():
    broken = FakeClients(FakeSubscriptionClient([], error=http_error("AuthorizationFailed")))
    assert may_view(broken, OID) == Decision.ERROR


def test_no_readable_subscription_is_an_error(caplog):
    with caplog.at_level(logging.WARNING):
        assert may_view(clients(empty=True), OID) == Decision.ERROR
    assert "role check failed: no subscription readable" in caplog.text
