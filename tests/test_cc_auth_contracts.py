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
  * jarvis-auth/.../api/dependencies/app_auth.py (require_app_client),
    guarding POST /internal/validate-node:
      - missing app headers -> 401 "Missing app credentials"
      - wrong app id/key   -> 401 "Invalid app credentials"

All five send a well-formed request BODY so the only thing under test is the
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
