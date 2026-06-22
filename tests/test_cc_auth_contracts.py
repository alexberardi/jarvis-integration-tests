"""Negative auth/credential contracts for the real CC + auth stack.

The existing real-stack suite (test_cc_real_smoke.py, CASE-101..215) is almost
entirely happy-path: it proves valid credentials are ACCEPTED. None of it proves
the inverse — that BAD or MISSING credentials are REJECTED. That inverse is the
security contract: every node endpoint, every admin endpoint, and the app-to-app
node-validation endpoint are only safe if they 401 on bad input. A regression
that makes any of them "fail open" (accept an unknown node, a wrong admin key, or
an unauthenticated app) would sail through the happy-path suite untouched.

These cases close that gap. They run in the same lane as test_cc_real_smoke.py
(the integration-runner workflow, which brings up postgres + auth + config +
CC), so they share its env gate: skipped cleanly whenever CC_URL is unset (the
stack isn't up), erroring nowhere.

Verified against source at authoring time:
  * jarvis-command-center/app/deps.py
      - verify_api_key:  invalid "id:key" -> 401 "Invalid API Key";
                         legacy (no-colon) miss -> 401 "Invalid API Key"
      - verify_admin_key: x_api_key != ADMIN_API_KEY -> 401 "Invalid Admin API Key"
      - verify_user_jwt (the user/mobile auth surface):
          missing/non-Bearer Authorization -> 401 "Missing or invalid Authorization header"
          present-but-undecodable Bearer token -> 401 "Invalid token"
  * jarvis-auth/.../api/dependencies/app_auth.py (require_app_client),
    guarding POST /internal/validate-node:
      - missing app headers -> 401 "Missing app credentials"
      - wrong app id/key   -> 401 "Invalid app credentials"

CASE-216..220 cover the node X-API-Key and the app-to-app (X-Jarvis-App-*)
boundaries. CASE-221..223 close the THIRD, previously-uncovered auth mechanism:
the user-JWT surface (`Authorization: Bearer <jwt>`, deps.py verify_user_jwt)
that guards the mobile / admin endpoints — node settings-requests, k2, and
factory-reset. A fail-open regression there lets an anonymous caller mutate
node settings or dispatch a device-bricking factory-reset. verify_user_jwt is a
FastAPI dependency evaluated BEFORE the handler body, so the path `node_id`
need not exist — the 401 is purely the credential check (CASE-221/223 even use
a literal nonexistent node).

Every case sends a well-formed request so the only thing under test is the
credential check (an ill-formed body could surface a 422 before auth runs).
"""

from __future__ import annotations

import os

import httpx
import pytest

CC_URL = os.environ.get("CC_URL")
AUTH_URL = os.environ.get("AUTH_URL", "http://localhost:7701")
CC_APP_ID = os.environ.get("CC_APP_ID", "command-center")

# The stack (CC + auth) is only up when the integration-runner brings it up,
# which is exactly when CC_URL is set. Gate every case on it so the file is a
# clean no-op skip in the fakes-only lanes / local runs.
SKIP_REASON = "CC_URL unset — skipping real-stack auth-contract tests (no stack)"


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-216")
def test_cc_rejects_bogus_node_credentials_with_401():
    """CC /conversation/start with a well-formed but unknown node key -> 401.

    Exercises verify_api_key's centralized-auth branch (deps.py:138 -> the
    "id:key" path): CC forwards the creds to auth's /internal/validate-node,
    auth returns valid=false ("Node not found"), and CC raises
    401 "Invalid API Key". This is the gate every node-authed endpoint shares;
    if it ever fails open, any caller with a syntactically valid X-API-Key
    would be treated as a registered node. The happy-path CASE-203 proves the
    accept side; this proves the reject side.
    """
    resp = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers={"X-API-Key": "ci-bogus-node:ci-bogus-key"},
        json={"conversation_id": "ci-neg-216"},
        timeout=15.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 for unknown node creds, got {resp.status_code} "
        f"body={resp.text[:300]} — CC's centralized node-auth reject path may "
        f"be failing open."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-217")
def test_cc_rejects_malformed_api_key_with_401():
    """CC /conversation/start with a no-colon (legacy-shaped) key -> 401.

    A key with no ":" never reaches centralized auth — verify_api_key falls
    through to the legacy local-DB lookup (deps.py:155-157) and 401s when no
    row matches. This is a DISTINCT code path from CASE-216 (legacy fallback,
    not the auth round-trip), and a common refactor hazard: collapsing the two
    branches could accidentally accept arbitrary opaque keys. Asserting 401
    pins the legacy-miss contract.
    """
    resp = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers={"X-API-Key": "ci-bogus-legacy-key-no-colon"},
        json={"conversation_id": "ci-neg-217"},
        timeout=15.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 for malformed (no-colon) API key, got {resp.status_code} "
        f"body={resp.text[:300]} — the legacy-fallback reject path may be "
        f"accepting unknown opaque keys."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-218")
def test_cc_admin_endpoint_rejects_wrong_admin_key_with_401():
    """CC POST /admin/nodes with a wrong admin key -> 401.

    /admin/nodes (admin.py:323) is gated by verify_admin_key, which 401s when
    X-API-Key != ADMIN_API_KEY. This endpoint MINTS node credentials and
    registers nodes in auth — the highest-privilege surface on CC. If its admin
    gate regressed, an attacker could provision themselves a valid node. The
    body is a complete, valid NodeCreate (node_id/household_id/room) so the only
    failure under test is the credential check, not body validation. Phase 2.5
    of the lane proves the accept side with the real admin key; this proves the
    reject side.
    """
    resp = httpx.post(
        f"{CC_URL}/api/v0/admin/nodes",
        headers={"X-API-Key": "ci-wrong-admin-key"},
        json={
            "node_id": "ci-neg-218",
            "household_id": "ci-bogus-household",
            "room": "ci-room",
            "name": "neg-218",
        },
        timeout=15.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 for wrong admin key on /admin/nodes, got "
        f"{resp.status_code} body={resp.text[:300]} — the admin gate on the "
        f"node-provisioning endpoint may be failing open."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-219")
def test_auth_validate_node_rejects_bad_app_key_with_401():
    """auth /internal/validate-node with a WRONG app key -> 401.

    /internal/validate-node is the endpoint EVERY service calls to authenticate
    a node (CC's verify_api_key depends on it). It is guarded by
    require_app_client, which 401s ("Invalid app credentials") when the app
    id/key don't verify. CASE-201 only ever sends a VALID app key — the bad-app
    -key reject path is uncovered. If app-auth here regressed, an unauthenticated
    caller could probe node validity or impersonate a trusted service against
    the node-auth chain. We send a valid request body (node_id/node_key/
    service_id) so the 401 is purely the app-credential check.
    """
    resp = httpx.post(
        f"{AUTH_URL}/internal/validate-node",
        headers={
            "X-Jarvis-App-Id": CC_APP_ID,
            "X-Jarvis-App-Key": "ci-wrong-app-key",
        },
        json={
            "node_id": "ci-neg-219",
            "node_key": "ci-neg-219-key",
            "service_id": "command-center",
        },
        timeout=10.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 for a wrong app key on /internal/validate-node, got "
        f"{resp.status_code} body={resp.text[:300]} — the app-to-app gate on "
        f"the node-validation endpoint may be failing open."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-220")
def test_auth_validate_node_rejects_missing_app_headers_with_401():
    """auth /internal/validate-node with NO app headers -> 401.

    The missing-credential branch of require_app_client (app_auth.py:22 ->
    401 "Missing app credentials") is distinct from the wrong-key branch in
    CASE-219, and is the more common real bug: a refactor that validates a key
    when present but forgets the presence check, or a header-name typo, would
    leave the endpoint unauthenticated. Sending a valid body with no app headers
    pins that the endpoint refuses anonymous callers outright.
    """
    resp = httpx.post(
        f"{AUTH_URL}/internal/validate-node",
        json={
            "node_id": "ci-neg-220",
            "node_key": "ci-neg-220-key",
            "service_id": "command-center",
        },
        timeout=10.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 for missing app headers on /internal/validate-node, got "
        f"{resp.status_code} body={resp.text[:300]} — the node-validation "
        f"endpoint may be accepting anonymous callers."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-221")
def test_cc_user_jwt_endpoint_rejects_missing_authorization_with_401():
    """CC user-JWT endpoint with NO Authorization header -> 401.

    POST /nodes/{node_id}/settings/requests is the mobile-app surface for
    pulling a node's settings; it depends on verify_user_jwt (deps.py:243).
    verify_user_jwt's FIRST branch (`if not authorization or not
    authorization.startswith("Bearer ")` -> 401 "Missing or invalid
    Authorization header") is the presence check: it must refuse an anonymous
    caller outright. This is the user-JWT analogue of CASE-220 (missing app
    headers) — none of CASE-216..220 touch the JWT auth path at all. Because
    verify_user_jwt is evaluated as a dependency before the handler's node
    lookup, a literal nonexistent node_id still yields the 401 (not a 404), so
    the assertion isolates the auth gate. A regression that dropped the
    Depends, or made the header optional, would let anyone create settings
    requests (and trigger the node-signalling MQTT publish) unauthenticated.
    """
    resp = httpx.post(
        f"{CC_URL}/api/v0/nodes/ci-neg-221-node/settings/requests",
        timeout=15.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 for missing Authorization on a user-JWT endpoint, got "
        f"{resp.status_code} body={resp.text[:300]} — CC's verify_user_jwt "
        f"presence check may be failing open on the mobile settings surface."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-222")
def test_cc_user_jwt_endpoint_rejects_undecodable_bearer_with_401():
    """CC user-JWT endpoint with a present-but-undecodable Bearer token -> 401.

    Distinct code path from CASE-221: the Authorization header IS present and
    Bearer-prefixed, so verify_user_jwt proceeds past the presence check to
    `jwt.decode(...)`, which raises JWTError on a non-JWT string and maps to
    401 "Invalid token" (deps.py:267-268). This is the user-JWT analogue of
    CASE-219 (wrong app key): it pins that CC actually *verifies* the token
    signature rather than trusting any Bearer value. A refactor that decoded
    without verifying (or swallowed the decode error) would fail open here
    while CASE-221 still passed. (The stack has JARVIS_AUTH_SECRET_KEY
    configured — CASE-212/214 round-trip a real seeded JWT through CC — so the
    secret-missing 500 branch at deps.py:256 does not apply; this is a clean
    401.)
    """
    resp = httpx.post(
        f"{CC_URL}/api/v0/nodes/ci-neg-222-node/settings/requests",
        headers={"Authorization": "Bearer not-a-real-jwt-token"},
        timeout=15.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 for an undecodable Bearer token on a user-JWT endpoint, "
        f"got {resp.status_code} body={resp.text[:300]} — CC may be accepting "
        f"unverified tokens (the jwt.decode reject branch failing open)."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-223")
def test_cc_factory_reset_rejects_missing_authorization_with_401():
    """CC factory-reset (highest-blast-radius user endpoint) with NO auth -> 401.

    POST /admin/nodes/{node_id}/factory-reset (admin.py:442) tells a node to
    wipe local state and re-provision — the device-bricking surface. It is
    guarded by the same verify_user_jwt dependency (admin.py:448) as CASE-214's
    positive path. This case proves THAT specific endpoint is wrapped in the
    auth gate at all: a regression dropping its Depends (a common copy-paste/
    refactor hazard on admin routes) would let any anonymous caller dispatch a
    factory-reset to any node. Asserting 401 on a no-Authorization request pins
    that the endpoint refuses unauthenticated callers before it ever mints a
    reset token or publishes the factory-reset MQTT signal. Distinct from
    CASE-221 (different router/handler, the destructive surface).
    """
    resp = httpx.post(
        f"{CC_URL}/api/v0/admin/nodes/ci-neg-223-node/factory-reset",
        timeout=15.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 for missing Authorization on factory-reset, got "
        f"{resp.status_code} body={resp.text[:300]} — the device-bricking "
        f"factory-reset endpoint may be accepting anonymous callers."
    )
