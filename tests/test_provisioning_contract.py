"""Mobile <-> node provisioning WIRE CONTRACT — node side.

jarvis-node-mobile and jarvis-node-setup hand-mirror the provisioning HTTP
contract on two sides that no test crosses today:

  * node-setup  : provisioning/models.py (Pydantic) + provisioning/api.py (routes)
  * node-mobile : src/types/Provisioning.ts + src/api/provisioningApi.ts (axios)

There is NO build-time link between them — a field rename, a required-ness flip,
or a dropped key on either side ships silently and only surfaces on a real Pi
during onboarding. The recon for this lane found two live drift risks already:
`NodeInfo.previously_provisioned` exists node-side but is absent from the TS type,
and the K2 request is camelCase in TS (`nodeId`/`createdAt`) but snake_case on the
wire, bridged ONLY by a hand-written transform at provisioningApi.ts:147-152.

This file pins the NODE side of the wire over pure HTTP against the running
fake-node provisioning server — the SAME byte-for-byte FastAPI app a real Pi runs
(jarvis-node-setup/scripts/run_provisioning.py), SimulatedWiFi-backed. The MIRROR
test lives in jarvis-node-mobile (__tests__/api/provisioningApi.contract.test.ts)
and pins the mobile side (the exact wire bodies the app POSTs + response parsing).
Both ends assert against PROVISIONING_WIRE_CONTRACT below — keep the two copies in
lockstep (the runbook, docs/mobile-e2e.md, explains the pairing).

Pure-HTTP by design (this harness imports no ecosystem code — it talks to
services over HTTP only). Gated on FAKE_NODE_URL (the published provisioning
server, default http://localhost:8080); skips cleanly wherever the server isn't
up. It runs for real in the mobile-e2e lane (.github/workflows/mobile-e2e.yml)
and against a locally-booted sim server — see docs/mobile-e2e.md
("Validate the contract test locally").
"""

from __future__ import annotations

import os

import httpx
import pytest

FAKE_NODE_URL = os.environ.get("FAKE_NODE_URL", "http://localhost:8080").rstrip("/")


def _node_is_up() -> bool:
    """True only if a provisioning server actually answers /api/v1/info."""
    try:
        r = httpx.get(f"{FAKE_NODE_URL}/api/v1/info", timeout=2.0)
        return r.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


# The whole module is a clean no-op unless a provisioning server is reachable.
pytestmark = pytest.mark.skipif(
    not _node_is_up(),
    reason=f"no provisioning server at {FAKE_NODE_URL} (set FAKE_NODE_URL) — "
    "runs in the mobile-e2e lane / against a locally-booted sim server",
)


# --- The single source of truth, mirrored by the node-mobile contract test. ---
# Each entry is the set of wire field names the mobile app depends on, split by
# required vs optional. `required` MUST be present; `optional` MAY be absent.
# Together they are the FULL set of properties the schema exposes (the tests
# assert properties == required | optional, so a NEW field on either side is a
# deliberate, reviewed change — not a silent drift).
PROVISIONING_WIRE_CONTRACT: dict[str, dict[str, set[str]]] = {
    # GET /api/v1/info response. previously_provisioned is node-only today (the TS
    # NodeInfo omits it); it is pinned as optional so the node may emit it without
    # breaking the app, but a drop/rename is still caught.
    "NodeInfo": {
        "required": {"node_id", "firmware_version", "hardware", "mac_address", "state"},
        "optional": {"capabilities", "previously_provisioned"},
    },
    "NetworkInfo": {
        "required": {"ssid", "signal_strength", "security"},
        "optional": set(),
    },
    "ScanNetworksResponse": {
        "required": set(),
        "optional": {"networks"},
    },
    # POST /api/v1/provision body. Mirrors ApiProvisioningRequest in the app.
    "ProvisionRequest": {
        "required": {
            "wifi_ssid", "wifi_password", "room", "command_center_url",
            "household_id", "node_id", "provisioning_token",
        },
        "optional": {"config_service_url"},
    },
    "ProvisionResponse": {
        "required": {"success", "message"},
        "optional": set(),
    },
    # GET /api/v1/status response. Mirrors ApiProvisioningStatus in the app.
    "ProvisionStatus": {
        "required": {"state", "message"},
        "optional": {"progress_percent", "error"},
    },
    # POST /api/v1/provision/k2 body — snake_case ON THE WIRE. The app's TS type is
    # camelCase (nodeId/createdAt) and is transformed to these snake_case keys
    # before the POST; if that transform drops a field, the node rejects it (422),
    # which is exactly what test_k2_wire_is_snake_case_only proves.
    "K2ProvisionRequest": {
        "required": {"node_id", "kid", "k2", "created_at"},
        "optional": set(),
    },
    "K2ProvisionResponse": {
        "required": {"success"},
        "optional": {"node_id", "kid", "error"},
    },
}

PROVISIONING_STATE_VALUES = {"AP_MODE", "CONNECTING", "REGISTERING", "PROVISIONED", "ERROR"}

# A representative, fully-valid POST /api/v1/provision body (the exact shape the
# app builds in provisioningApi.ts:76-85). config_service_url is included; it is
# the only optional field.
_PROVISION_BODY = {
    "wifi_ssid": "HomeWiFi",
    "wifi_password": "correct-horse",
    "room": "kitchen",
    "command_center_url": "http://10.0.0.5:7703",
    "config_service_url": "http://10.0.0.5:7700",
    "household_id": "11111111-1111-1111-1111-111111111111",
    "node_id": "22222222-2222-2222-2222-222222222222",
    "provisioning_token": "prov-token-abc",
}


@pytest.fixture(scope="module")
def openapi() -> dict:
    """The live server's component schemas (FastAPI auto-generates these from the
    Pydantic models — they ARE the wire contract)."""
    r = httpx.get(f"{FAKE_NODE_URL}/openapi.json", timeout=5.0)
    r.raise_for_status()
    return r.json()["components"]["schemas"]


def _fields(schema: dict) -> tuple[set[str], set[str]]:
    """(required, optional) field-name sets for one component schema."""
    props = set(schema.get("properties", {}))
    required = set(schema.get("required", []))
    return required, props - required


@pytest.mark.parametrize("model", sorted(PROVISIONING_WIRE_CONTRACT))
def test_wire_schema_matches_contract(openapi: dict, model: str) -> None:
    """Every provisioning model's required/optional field sets match the contract
    the mobile app is coded against — a rename or required-ness flip turns red."""
    assert model in openapi, f"node-setup no longer exposes a {model} schema"
    required, optional = _fields(openapi[model])
    expected = PROVISIONING_WIRE_CONTRACT[model]
    assert required == expected["required"], (
        f"{model}: required wire fields drifted from the mobile contract. "
        f"node={sorted(required)} expected={sorted(expected['required'])}. "
        "Update src/types/Provisioning.ts + the node-mobile contract test in lockstep."
    )
    assert optional == expected["optional"], (
        f"{model}: optional wire fields drifted. "
        f"node={sorted(optional)} expected={sorted(expected['optional'])}."
    )


def test_provisioning_state_enum_matches(openapi: dict) -> None:
    """The node state enum the app switch-maps (provisioningApi.ts:99-114) is
    pinned — a renamed/added/removed state would silently mis-map in the UI."""
    assert "ProvisioningState" in openapi
    assert set(openapi["ProvisioningState"]["enum"]) == PROVISIONING_STATE_VALUES


def test_info_response_carries_contract_fields() -> None:
    """GET /api/v1/info returns every required NodeInfo field the app reads."""
    data = httpx.get(f"{FAKE_NODE_URL}/api/v1/info", timeout=5.0).json()
    for field in PROVISIONING_WIRE_CONTRACT["NodeInfo"]["required"]:
        assert field in data, f"/api/v1/info dropped {field!r}"
    assert data["state"] in PROVISIONING_STATE_VALUES


def test_scan_networks_response_shape() -> None:
    """GET /api/v1/scan-networks returns {networks: [...]} — the app reads
    response.data.networks (provisioningApi.ts:63)."""
    data = httpx.get(f"{FAKE_NODE_URL}/api/v1/scan-networks", timeout=5.0).json()
    assert "networks" in data and isinstance(data["networks"], list)
    for net in data["networks"]:
        assert PROVISIONING_WIRE_CONTRACT["NetworkInfo"]["required"] <= set(net)


def test_provision_accepts_the_app_wire_body() -> None:
    """The exact body provisioningApi.ts builds is accepted (200) and returns the
    {success, message} shape the app parses."""
    r = httpx.post(f"{FAKE_NODE_URL}/api/v1/provision", json=_PROVISION_BODY, timeout=5.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= PROVISIONING_WIRE_CONTRACT["ProvisionResponse"]["required"]
    assert isinstance(body["success"], bool) and isinstance(body["message"], str)


@pytest.mark.parametrize("dropped", sorted(PROVISIONING_WIRE_CONTRACT["ProvisionRequest"]["required"]))
def test_provision_requires_every_contract_field(dropped: str) -> None:
    """Dropping any required provision field is a 422 — the contract has teeth, so
    the app can't quietly stop sending one (and a node that made one optional,
    diverging from the app's required type, turns red here)."""
    body = {k: v for k, v in _PROVISION_BODY.items() if k != dropped}
    r = httpx.post(f"{FAKE_NODE_URL}/api/v1/provision", json=body, timeout=5.0)
    assert r.status_code == 422, f"omitting {dropped!r} should be rejected, got {r.status_code}"


def test_k2_camelcase_body_is_rejected() -> None:
    """The K2 endpoint is the fragile seam: the app's TS type is camelCase
    (nodeId/createdAt) but it POSTs snake_case via a hand-written transform
    (provisioningApi.ts:147-152). The node REQUIRES snake_case — already pinned by
    test_wire_schema_matches_contract[K2ProvisionRequest]. Here we pin the other
    half: a camelCase body (a forgotten/half-done transform) is REJECTED at the
    validation layer (422), which is WHY the transform must exist. A future K2
    field added to the TS type but not to the transform would POST a body missing
    the snake_case key and 422 the same way — caught by the mobile-side mirror.

    (We deliberately do NOT assert the snake_case-accepted path here: a valid body
    runs real is_provisioned()/save_k2 node logic — slow, state-mutating, and not
    the wire contract's concern. The schema test already pins the snake_case
    requirement; the 422 below proves nothing else is accepted.)"""
    camel = {
        "nodeId": "22222222-2222-2222-2222-222222222222",
        "kid": "k2-2026-06",
        "k2": "YWJj",
        "createdAt": "2026-06-23T00:00:00Z",
    }
    r = httpx.post(f"{FAKE_NODE_URL}/api/v1/provision/k2", json=camel, timeout=5.0)
    assert r.status_code == 422, (
        "camelCase K2 body should be rejected at validation (this is WHY "
        f"provisioningApi.ts transforms to snake_case before POSTing), got {r.status_code}"
    )
