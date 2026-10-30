"""Who may see the page: a signed-in person who holds a role on a scanned subscription.

The sign-in itself is App Service's: it adds the X-MS-CLIENT-PRINCIPAL header with the person's
claims. The headers can be trusted because "External requests aren't allowed to set these
headers, so they're present only if App Service sets them", source:
https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-user-identities

Azure, not the token, resolves group membership. The role assignment list filtered with
assignedTo(<object id>) "is transitive for groups which means that if the user is a member of a
group and that group is a member of another group that has a role assignment, that role
assignment is also listed", so a role given to a group counts, nested groups included, with no
`groups` claim and no token overage. Source:
https://learn.microsoft.com/en-us/azure/role-based-access-control/role-assignments-list-rest
atScope() keeps the assignments "at or above the scope", the subscription, source:
https://learn.microsoft.com/en-us/rest/api/authorization/role-assignments/list-for-subscription

The check fails closed: any error reading roles is ERROR, never DENY, so a broken identity does
not look like "no access" and does not let anyone in.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Mapping
from enum import StrEnum

from azure.core.exceptions import AzureError

from .clients import Clients, error_code

log = logging.getLogger(__name__)

# The claim holding the object ID, in the order tried; the first equals X-MS-CLIENT-PRINCIPAL-ID
# when App Service signs a person in with Entra.
OID_CLAIMS = ("http://schemas.microsoft.com/identity/claims/objectidentifier", "oid")
PRINCIPAL_HEADER = "x-ms-client-principal"
GUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII | re.IGNORECASE
)


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ERROR = "error"


def object_id(headers: Mapping[str, str]) -> str | None:
    """The signed-in person's object ID, casefolded, or None if the header is missing or odd.

    Header names are compared case-insensitively, because frameworks present them in different
    formats. Only a GUID is returned: the value ends up inside a role assignment filter.
    """
    encoded = next((v for k, v in headers.items() if k.casefold() == PRINCIPAL_HEADER), None)
    if encoded is None:
        return None
    try:
        data = json.loads(base64.b64decode(encoded, validate=True))
    except ValueError:  # bad base64, not UTF-8 and bad JSON are all ValueErrors
        return None
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        return None
    for typ in OID_CLAIMS:
        for claim in claims:
            if not isinstance(claim, dict) or claim.get("typ") != typ:
                continue
            val = claim.get("val")
            if isinstance(val, str) and GUID_RE.fullmatch(val):
                return val.casefold()
    return None


def assignment_filter(oid: str) -> str:
    """The role assignment filter for one person; refuses anything but a GUID.

    The SDK sends $filter unquoted (azure-mgmt-authorization _role_assignments_operations.py,
    skip_quote=True), so this check is the only thing between a claim value and the query.
    """
    if not GUID_RE.fullmatch(oid):
        raise ValueError("not an object ID")
    return f"atScope() and assignedTo('{oid}')"


def may_view(clients: Clients, oid: str) -> Decision:
    """ALLOW if `oid` holds any role on any readable subscription, DENY if on none.

    atScope() leaves out roles held only on a resource group (or below), so such a person is
    denied. An AzureError, or no readable subscription, is ERROR. WriteRefused propagates.
    """
    query = assignment_filter(oid)
    try:
        subs = [
            sub.subscription_id.casefold() for sub in clients.subscriptions().subscriptions.list()
        ]
        if not subs:
            log.warning("role check failed: no subscription readable")
            return Decision.ERROR
        for sub in subs:
            assignments = clients.authorization(sub).role_assignments
            if next(iter(assignments.list_for_subscription(filter=query)), None) is not None:
                return Decision.ALLOW
    except AzureError as exc:
        log.warning("role check failed: %s", error_code(exc))
        return Decision.ERROR
    return Decision.DENY
