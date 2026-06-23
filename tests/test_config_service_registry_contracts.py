"""Service-registry contract edges on jarvis-config-service's /services API — the
admin-token write-gate (401 reject) on each mutating verb plus the read-side
not-found (404), the contracts the happy-path real-stack suite silently relies on
but never asserts.

config-service is the discovery backbone of the Jarvis mesh: every app reads
`GET /services` at boot to locate its peers (the CI seed registers the fake
llm-proxy/whisper there, and CC reads it to find them). The fast lane brings the
service up on every stack run, but the only assertion against it is CASE-103
(`/health == ok`). Its actual CONTRACT — who may MUTATE the registry, and what an
unknown lookup returns — has zero coverage. A fail-open regression there is
exactly the kind the happy-path suite keeps green: the seed only ever writes with
the VALID admin token and reads back services it just registered.

The three mutating routes (POST/PUT/DELETE /services) each INDEPENDENTLY wire
`Depends(require_admin)` (app/routes/services.py), which rejects a wrong
`X-Admin-Token` with 401 "Invalid admin token" (app/auth.py). Because each handler
re-declares that dependency, they can regress independently — a refactor or
copy-paste that drops the guard from ONE verb fails open on just that route, and
the blast radius differs per verb:

  * POST   fail-open -> any caller INJECTS a rogue service entry (e.g. a fake
    `jarvis-llm-proxy-api` pointing at an attacker host) -> CC routes completions
    to it. Discovery poisoning / MITM.
  * PUT    fail-open -> any caller REPOINTS an existing entry's host/port -> the
    same MITM, but silent (no new row to notice). A SEPARATE handler from POST.
  * DELETE fail-open -> any caller DEREGISTERS a service -> CC's discovery of it
    fails -> the voice loop loses llm-proxy/whisper. Denial of service. A SEPARATE
    handler again.

CASE-233 pins the read side: `GET` an unknown service name -> a clean 404 "not
found", not a 500 or a fall-through. That 404-vs-5xx distinction is the contract
every client's boot-time discovery depends on to tell "not registered yet" apart
from "the registry is broken".

All four are deterministic and need NO valid credentials. The 401s send a
DELIBERATELY WRONG admin token — a present-but-invalid header, so `require_admin`
passes its `Header(...)` presence check and reaches its compare-and-reject branch
(the CI stack sets a non-empty `JARVIS_CONFIG_ADMIN_TOKEN`, so the
unconfigured-500 branch never fires). The mutating tests send a COMPLETE, valid
body so the ONLY thing that can fail is the auth gate, not body validation
(`require_admin` is resolved before the request body is validated, and the bodies
are within every schema bound). The 404 is an unauthenticated GET.

Gated on CC_URL so the file is a clean no-op skip in the fakes-only lanes and
local runs — mirrors CASE-103/104, which gate the config-service health check the
same way (CONFIG_URL defaults to the compose-mapped port). The service names used
are unique CI literals that can never collide with a real seeded service, and the
mutating verbs target an ABSENT name so even a catastrophic guard-failure could
not touch a registered entry.

Verified against jarvis-config-service source at authoring time:
  * app/auth.py:require_admin              wrong X-Admin-Token -> 401 "Invalid admin token"
  * app/routes/services.py:create_service  POST   ""       Depends(require_admin)
  * app/routes/services.py:update_service  PUT    /{name}  Depends(require_admin)
  * app/routes/services.py:delete_service  DELETE /{name}  Depends(require_admin)
  * app/routes/services.py:get_service     GET    /{name}  unknown -> 404 "Service '{name}' not found"
"""

from __future__ import annotations

import os

import httpx
import pytest

CC_URL = os.environ.get("CC_URL")
CONFIG_URL = os.environ.get("CONFIG_URL", "http://localhost:7700")

SKIP_REASON = (
    "CC_URL unset — config-service stack not up (fakes-only lane); "
    "skipping registry contract tests"
)

# A present-but-WRONG admin token. require_admin (app/auth.py) sees a non-empty
# header (so it passes the `Header(...)` presence requirement) that does NOT equal
# the server's configured JARVIS_CONFIG_ADMIN_TOKEN -> its compare-and-reject
# branch fires with 401. We never send the real token, so no valid creds needed.
_WRONG_ADMIN = {"X-Admin-Token": "ci-wrong-config-admin-token"}

# A complete, valid ServiceCreate body (every required field within its schema
# bound) so a POST reaches the handler and the ONLY thing that can fail is
# require_admin — never body validation. Throwaway CI literals; the request is
# rejected at the auth gate before anything is persisted.
_VALID_SERVICE_BODY = {
    "name": "ci-registry-230-should-never-persist",
    "host": "127.0.0.1",
    "port": 9999,
    "scheme": "http",
    "health_path": "/health",
    "description": "rejected at the admin-token gate; must never be created",
}

# A valid ServiceUpdate body (all fields optional, all within bounds) for the PUT
# gate — same isolation reasoning: the ownership/auth check, not body validation.
_VALID_UPDATE_BODY = {"host": "127.0.0.1", "port": 9998}

# A service name guaranteed absent from the registry (the seed registers the real
# fakes under their canonical names). Used as the path target for the mutating
# verbs so even a failed guard could not touch a registered service; require_admin
# fires before the name is ever looked up, so it need not exist for the 401.
_ABSENT_NAME = "ci-registry-absent-zzz"


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-230")
def test_config_service_create_rejects_wrong_admin_token_with_401():
    """config-service POST /services with a WRONG X-Admin-Token -> 401.

    The service-registration write-gate. POSTs a complete, valid ServiceCreate
    body with a present-but-invalid admin token. require_admin
    (app/auth.py) compares it to JARVIS_CONFIG_ADMIN_TOKEN, mismatches, and raises
    401 "Invalid admin token" BEFORE the handler body runs — so nothing is
    persisted. Pins that the registry's create path refuses an unauthenticated
    writer: a fail-open here lets any caller inject a rogue service entry (e.g. a
    counterfeit jarvis-llm-proxy-api pointing at an attacker host), poisoning the
    discovery every Jarvis app reads at boot. The valid body isolates the failure
    to the credential check, not request validation.
    """
    resp = httpx.post(
        f"{CONFIG_URL}/services",
        headers=_WRONG_ADMIN,
        json=_VALID_SERVICE_BODY,
        timeout=15.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 creating a service with a wrong admin token, got "
        f"{resp.status_code} body={resp.text[:300]} — config-service's "
        f"registration write-gate (require_admin) may be failing open, letting "
        f"any caller poison service discovery."
    )
    assert "admin token" in resp.text.lower(), (
        f"expected the 'Invalid admin token' detail, got body={resp.text[:300]} — "
        f"the 401 may be coming from a different check than require_admin."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-231")
def test_config_service_update_rejects_wrong_admin_token_with_401():
    """config-service PUT /services/{name} with a WRONG X-Admin-Token -> 401.

    The update write-gate — a SEPARATE handler (update_service) from CASE-230's
    create path, so it can regress on its own. PUTs a valid ServiceUpdate body to
    an absent service name with a present-but-invalid admin token; require_admin
    rejects with 401 before the name is ever looked up. Pins the higher-stealth
    MITM surface: a fail-open update lets any caller silently REPOINT an existing
    entry's host/port (no new row to notice) so CC's traffic to that service is
    redirected. Targets an absent name so even a failed guard could not mutate a
    real registered entry.
    """
    resp = httpx.put(
        f"{CONFIG_URL}/services/{_ABSENT_NAME}",
        headers=_WRONG_ADMIN,
        json=_VALID_UPDATE_BODY,
        timeout=15.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 updating a service with a wrong admin token, got "
        f"{resp.status_code} body={resp.text[:300]} — config-service's update "
        f"write-gate (require_admin on update_service) may be failing open, "
        f"letting any caller silently repoint a registered service (MITM)."
    )
    assert "admin token" in resp.text.lower(), (
        f"expected the 'Invalid admin token' detail, got body={resp.text[:300]} — "
        f"the 401 may be coming from a different check than require_admin."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-232")
def test_config_service_delete_rejects_wrong_admin_token_with_401():
    """config-service DELETE /services/{name} with a WRONG X-Admin-Token -> 401.

    The deregistration write-gate — a THIRD, SEPARATE handler (delete_service),
    the highest-blast-radius mutation on the registry. DELETEs an absent service
    name with a present-but-invalid admin token; require_admin rejects with 401
    before any lookup. Pins that the destructive verb is auth-gated at all: a
    fail-open delete lets any caller DEREGISTER a live service (e.g. drop
    jarvis-llm-proxy-api from discovery), and CC's next boot-time lookup misses it
    — a denial of service on the voice loop. Distinct from CASE-230/231 because a
    copy-paste that fixed the create/update guards but not delete would still leak
    here. Targets an absent name so a failed guard could not delete a real entry.
    """
    resp = httpx.delete(
        f"{CONFIG_URL}/services/{_ABSENT_NAME}",
        headers=_WRONG_ADMIN,
        timeout=15.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 deleting a service with a wrong admin token, got "
        f"{resp.status_code} body={resp.text[:300]} — config-service's "
        f"deregistration write-gate (require_admin on delete_service) may be "
        f"failing open, letting any caller deregister a live service (DoS)."
    )
    assert "admin token" in resp.text.lower(), (
        f"expected the 'Invalid admin token' detail, got body={resp.text[:300]} — "
        f"the 401 may be coming from a different check than require_admin."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-233")
def test_config_service_get_unknown_service_returns_404():
    """config-service GET /services/{name} for an unknown service -> 404.

    The read-side discovery contract. GETs (unauthenticated, as the route is) a
    service name that was never registered; get_service's lookup misses and raises
    404 "Service '{name}' not found" (app/routes/services.py). Pins that an
    unknown service is a clean 404 — not a 500, and not a silent fall-through —
    which is the distinction every client's boot-time discovery relies on to tell
    "not registered yet" apart from "the registry itself is broken". The
    happy-path suite only ever looks up services it (or the seed) just registered,
    so this miss path is otherwise unexercised. The name is a unique CI literal
    that can never collide with a seeded service or the GET /services/health route.
    """
    resp = httpx.get(f"{CONFIG_URL}/services/{_ABSENT_NAME}", timeout=15.0)
    assert resp.status_code == 404, (
        f"expected 404 for an unknown service name, got {resp.status_code} "
        f"body={resp.text[:300]} — the registry lookup's not-found branch may be "
        f"regressing to a 500 or falling through, breaking clients that "
        f"distinguish 'not registered' from 'registry broken'."
    )
    assert "not found" in resp.text.lower(), (
        f"expected the 'Service ... not found' detail, got body={resp.text[:300]} "
        f"— the 404 may be coming from routing rather than the handler's lookup."
    )
