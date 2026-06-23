"""Node-scoped resource-access contracts on CC's settings-request surface — the
authorization (403) and not-found (404) edges the happy-path real-stack suite
silently relies on but never asserts.

The negative-auth suite (test_cc_auth_contracts.py, CASE-216..223) proves CC
rejects BAD or MISSING credentials (401). The request-contract suite
(test_cc_request_contracts.py, CASE-224..226) proves it rejects out-of-order /
malformed AUTHENTICATED calls (400). Neither covers the next layer: a request
made with VALID node credentials that asks to touch a resource the node does NOT
own, or a resource that does not exist. Those are the per-node authorization and
existence contracts CC enforces in `node_settings.py`, and a regression on any
of them fails open silently — the happy-path suite keeps passing because it only
ever reads/writes the node's OWN, freshly-created requests.

CC's settings-request endpoints (`/nodes/{node_id}/settings/requests/...`,
guarded by `verify_api_key`) take the target `node_id` in the URL path and
compare it against the authenticated node's identity BEFORE any DB lookup:

    if node_context.node.node_id != node_id:
        raise HTTPException(status_code=403, detail="Cannot access ...")

That ownership check is the cross-node isolation boundary. If it ever failed
open (a collapsed comparison, a copy-paste drift between the read and the upload
handler), any registered node could READ another node's pending encrypted
settings snapshot, or OVERWRITE it — a cross-tenant data leak / tamper. The
happy-path CASE-212 proves a node can drive its OWN settings request; nothing
proves it is fenced out of every OTHER node's.

Three distinct branches are pinned here, all reachable with the seeded node
credentials the integration-runner already exports (CC_NODE_ID/CC_NODE_KEY),
deterministically and with no waiting or pre-seeded state:

1. **Cross-node READ isolation (403).** GET the settings-request of a DIFFERENT
   node_id than the one we authenticated as. The ownership guard
   (node_settings.py:243) fires before the DB lookup, so no request need exist.
2. **Not-found (404).** GET our OWN node's settings-request for a request_id
   that was never created. Ownership passes; the lookup misses
   (node_settings.py:252). This is a DISTINCT branch from #1 and pins that a
   miss is a clean 404, not a 500 or a leak of another node's row.
3. **Cross-node WRITE/upload isolation (403).** PUT a (well-formed) snapshot to a
   DIFFERENT node's request. Distinct handler from #1 (node_settings.py:280, the
   upload twin), and the more dangerous write surface — the body is a complete
   SnapshotUpload so the ONLY failure under test is the ownership check, not body
   validation.

Verified against jarvis-command-center source at authoring time:
  * app/node_settings.py:243    GET   node_id mismatch -> 403 "Cannot access other node's requests"
  * app/node_settings.py:252    GET   request not found  -> 404 "Request not found"
  * app/node_settings.py:280    PUT   node_id mismatch -> 403 "Cannot upload to other node's requests"
  * app/main.py:429             router mounted at prefix /api/v0

Gated on CC_URL + CC_NODE_ID/CC_NODE_KEY so the file is a clean no-op skip in the
fakes-only lanes and local runs (mirrors test_cc_request_contracts.py's gates).
The "other node" id and the nonexistent request_id are unique CI literals that
can never collide with a real seeded node or a request another case created.
"""

from __future__ import annotations

import os

import httpx
import pytest

CC_URL = os.environ.get("CC_URL")
CC_NODE_ID = os.environ.get("CC_NODE_ID", "")
CC_NODE_KEY = os.environ.get("CC_NODE_KEY", "")

SKIP_REASON = "CC_URL unset — skipping real-stack node-scope contract tests (no stack)"
SKIP_NO_NODE = "CC_NODE_ID / CC_NODE_KEY unset — node seed did not run"

# Valid node X-API-Key (node_id:node_key) — the same format CASE-203 proves CC
# accepts end-to-end. Auth passes, so the ONLY thing under test is CC's
# in-handler per-node ownership / existence check, not the credential check.
_NODE_KEY_HEADER = f"{CC_NODE_ID}:{CC_NODE_KEY}"

# A node id that is NOT the authenticated node. Used only as a URL-path target so
# the ownership comparison (authenticated node_id != path node_id) is guaranteed
# to differ regardless of what the seeded node is called.
_OTHER_NODE_ID = "ci-not-my-node-zzz"

# A complete, valid SnapshotUpload body (all six required fields). Lets the PUT
# reach the handler so the ownership 403 — not a body-validation 4xx — is what we
# assert. The values are placeholders; the request is rejected before they matter.
_SNAPSHOT_BODY = {
    "ciphertext": "AAAA",
    "nonce": "AAAA",
    "tag": "AAAA",
    "aad_schema_version": 1,
    "aad_commands_schema_version": 1,
    "aad_revision": 1,
}


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE)
@pytest.mark.qa_case("CASE-227")
def test_cc_settings_request_get_rejects_other_node_with_403():
    """CC GET settings-request for a DIFFERENT node than authenticated -> 403.

    GETs /nodes/{other_node}/settings/requests/{rid} authenticated as our own
    seeded node. CC's handler compares the authenticated node's id against the
    path node_id and raises 403 "Cannot access other node's requests"
    (node_settings.py:243) BEFORE looking the request up — so no request need
    exist for the gate to fire. Pins the cross-node READ isolation boundary: a
    regression collapsing that ownership check would let any registered node read
    another node's pending encrypted settings snapshot. The happy-path suite only
    ever reads a node's OWN requests, so it would stay green through such a leak.
    """
    resp = httpx.get(
        f"{CC_URL}/api/v0/nodes/{_OTHER_NODE_ID}/settings/requests/ci-scope-227-any",
        headers={"X-API-Key": _NODE_KEY_HEADER},
        timeout=15.0,
    )
    assert resp.status_code == 403, (
        f"expected 403 reading another node's settings request, got "
        f"{resp.status_code} body={resp.text[:300]} — CC's per-node ownership "
        f"guard on the settings-request READ path may be failing open."
    )
    assert "other node" in resp.text.lower(), (
        f"expected the 'Cannot access other node's requests' ownership detail, got "
        f"body={resp.text[:300]} — the 403 may be coming from a different check."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE)
@pytest.mark.qa_case("CASE-228")
def test_cc_settings_request_get_unknown_request_returns_404():
    """CC GET our OWN node's settings-request for an unknown request_id -> 404.

    Uses the authenticated node's OWN id in the path (so the ownership check at
    node_settings.py:243 passes) but a request_id that was never created. The DB
    lookup misses and CC raises 404 "Request not found" (node_settings.py:252).
    This is a DISTINCT branch from CASE-227 (ownership passes, existence fails)
    and pins that an unknown request is a clean 404 — not a 500, and not a silent
    fall-through that could surface another node's row. The happy-path suite only
    ever GETs requests it just created, so the miss path is otherwise unexercised.
    """
    resp = httpx.get(
        f"{CC_URL}/api/v0/nodes/{CC_NODE_ID}/settings/requests/ci-scope-228-never-created",
        headers={"X-API-Key": _NODE_KEY_HEADER},
        timeout=15.0,
    )
    assert resp.status_code == 404, (
        f"expected 404 for an unknown request_id on the node's own settings "
        f"requests, got {resp.status_code} body={resp.text[:300]} — the "
        f"not-found branch may be regressing to a 500 or failing open."
    )
    assert "not found" in resp.text.lower(), (
        f"expected the 'Request not found' detail, got body={resp.text[:300]} — "
        f"the 404 may be coming from routing rather than the handler's lookup."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE)
@pytest.mark.qa_case("CASE-229")
def test_cc_settings_snapshot_upload_rejects_other_node_with_403():
    """CC PUT settings snapshot to a DIFFERENT node's request -> 403.

    PUTs a complete, well-formed SnapshotUpload to
    /nodes/{other_node}/settings/requests/{rid}/snapshot authenticated as our own
    seeded node. CC's upload handler enforces the SAME per-node ownership boundary
    as CASE-227 but in a SEPARATE handler (node_settings.py:280, "Cannot upload to
    other node's requests"), so it can regress independently — a copy-paste drift
    that fixed the read guard but not the upload guard would let a node OVERWRITE
    another node's encrypted snapshot, the higher-risk write/tamper surface. The
    body is valid so the ownership check, not body validation, is the only thing
    under test; the guard fires before the request is ever looked up.
    """
    resp = httpx.put(
        f"{CC_URL}/api/v0/nodes/{_OTHER_NODE_ID}/settings/requests/ci-scope-229-any/snapshot",
        headers={"X-API-Key": _NODE_KEY_HEADER},
        json=_SNAPSHOT_BODY,
        timeout=15.0,
    )
    assert resp.status_code == 403, (
        f"expected 403 uploading a snapshot to another node's request, got "
        f"{resp.status_code} body={resp.text[:300]} — CC's per-node ownership "
        f"guard on the snapshot UPLOAD path may be failing open (cross-node "
        f"tamper). A 4xx body-validation error here means the SnapshotUpload "
        f"shape drifted and the test body needs updating."
    )
    assert "other node" in resp.text.lower(), (
        f"expected the 'Cannot upload to other node's requests' ownership detail, "
        f"got body={resp.text[:300]} — the 403 may be coming from a different check."
    )
