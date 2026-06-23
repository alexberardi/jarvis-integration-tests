# Coverage gaps ledger

Durable, human-visible backlog of integration-coverage gaps discovered while
scanning the Jarvis services, plus their disposition. Maintained by the
qa-author pass. A gap leaves this list only when it is authored (→ CASE-NNN) or
explicitly judged not worth a test (with a reason).

Status legend: **open** (worth a test, not yet written) · **authored → CASE-NNN
(PR pending)** · **covered** (merged) · **rejected** (didn't clear the value
bar — reason given).

## 2026-06-22 — negative auth/credential contracts (CC + auth)

The real-stack suite (CASE-101..215) was found to be almost entirely happy-path:
it proves valid credentials are accepted but never that bad/missing ones are
rejected. The credential REJECT paths are the actual security contract — a
"fail open" regression on any of them passes the happy-path suite silently.
Authored as a cohesive cohort in the integration-runner lane
(`tests/test_cc_auth_contracts.py`):

| Gap | Disposition |
| --- | --- |
| CC node-authed endpoint accepts an unknown `id:key` (centralized-auth reject path) | **covered** (CASE-216, merged #7) |
| CC node-authed endpoint accepts a malformed no-colon key (legacy-fallback reject path) | **covered** (CASE-217, merged #7) |
| CC `/admin/nodes` (node-provisioning) accepts a wrong admin key | **covered** (CASE-218, merged #7) |
| auth `/internal/validate-node` accepts a wrong app key (app-to-app gate) | **covered** (CASE-219, merged #7) |
| auth `/internal/validate-node` accepts missing app headers (anonymous caller) | **covered** (CASE-220, merged #7) |

## 2026-06-22 — llm-proxy app-auth REJECT contracts (from-source lane)

The from-source llm-proxy lane proves the ACCEPT side of the proxy's app-to-app
gate (CASE-302 sends the valid seeded key and gets 200) but never the REJECT
side. `/v1/chat/completions` is the gateway in front of the model service —
guarded by `require_app_auth` (`jarvis-llm-proxy-api/auth/app_auth.py`), which
forwards the app headers to auth `/internal/app-ping`. A fail-open regression
there lets any caller spend model compute / read completions. This is the exact
mirror of the merged CC+auth CASE-219/220 pair, one layer out at the proxy.
Verified against source: missing headers → 401 "Missing app credentials";
wrong key → auth 401 → proxy 401 "Invalid app credentials". Authored in the
from-source lane (`tests/test_from_source_services.py`, gated on
`LLM_PROXY_URL`); wired into the lane's `jarvis-llm-proxy-api` plan and the
cross-repo resolver KNOWN as `always_cases` (backend-agnostic — they reject
before the backend, so they hold on both MOCK and REST).

| Gap | Disposition |
| --- | --- |
| llm-proxy `/v1/chat/completions` accepts a WRONG app key (app-to-app gate) | **covered** (CASE-303, merged #10) |
| llm-proxy `/v1/chat/completions` accepts MISSING app headers (anonymous caller) | **covered** (CASE-304, merged #10) |

## 2026-06-22 — negative auth contracts: the user-JWT / mobile-admin surface (CC)

CASE-216..220 cover CC's node `X-API-Key` reject paths and the app-to-app
(`X-Jarvis-App-*`) boundary, but the THIRD CC auth mechanism —
`verify_user_jwt` (`Authorization: Bearer <jwt>`, `deps.py:243`), the user/mobile
surface guarding node settings-requests, k2, and factory-reset — had ZERO
negative coverage. Its reject branches are the security contract for the mobile
app: a fail-open regression lets an anonymous caller mutate node settings or
dispatch a device-bricking factory-reset. Authored as a cohesive cohort in the
integration-runner lane (`tests/test_cc_auth_contracts.py`, gated on `CC_URL`).
verify_user_jwt is evaluated as a dependency before the handler body, so a
literal nonexistent `node_id` still yields the 401 (not a 404) — the assertion
isolates the auth gate. Verified against source: missing/non-Bearer header → 401
"Missing or invalid Authorization header"; undecodable Bearer → 401 "Invalid
token" (`jwt.decode` JWTError branch).

| Gap | Disposition |
| --- | --- |
| CC `/nodes/{id}/settings/requests` accepts a MISSING `Authorization` header (presence-check branch of verify_user_jwt) | **covered** (CASE-221, merged #11) |
| CC `/nodes/{id}/settings/requests` accepts a present-but-undecodable Bearer token (jwt.decode reject branch) | **covered** (CASE-222, merged #11) |
| CC `/admin/nodes/{id}/factory-reset` (device-bricking surface) accepts a MISSING `Authorization` header — proves the destructive endpoint is auth-gated at all | **covered** (CASE-223, merged #11) |

## 2026-06-22 — authenticated request-contract edges (CC, fast lane)

The 401 credential-reject surface is now well covered (CASE-216..223). The next
layer down had ZERO coverage: a request that IS authenticated with valid node
credentials but is *out-of-order* (a conversation that was never started) or
*malformed* (a body missing a required field). Those are real contracts CC
implements in handler code; the happy-path suite (CASE-101..215) never trips
them because it always sequences correctly and always sends complete bodies, so
a fail-open regression would pass silently. Authored as a cohesive cohort in the
integration-runner fast lane (`tests/test_cc_request_contracts.py`, gated on
`CC_URL` + `CC_NODE_ID`/`CC_NODE_KEY`). Verified against jarvis-command-center
source: `app/main.py:719-721` and `:882-887` (the conversation precondition
guard, `conversation_cache.get_tools(...) is None` → 400 "Conversation not
initialized for tool-based flow") and `app/main.py:108-124` (the custom
`RequestValidationError` handler → 400 `{"error":"validation_error",...}`
envelope, deliberately NOT FastAPI's default 422).

| Gap | Disposition |
| --- | --- |
| CC `/voice/command/stream` runs the pipeline for a conversation_id that was never started (precondition guard fails open → 500 instead of 400) | **covered** (CASE-224, merged #12) |
| CC `/voice/command` (blocking twin — previously ZERO coverage) lacks the same precondition guard, a copy-paste-drift hazard from its streaming sibling | **covered** (CASE-225, merged #12) |
| CC malformed-body responses regress from the custom 400 `validation_error` envelope to FastAPI's default 422, breaking node/mobile clients that render `details` | **covered** (CASE-226, merged #12) |

## 2026-06-22 — node-scoped resource-access contracts (CC settings-requests, fast lane)

The 401 (bad creds), 400 (precondition/malformed) layers are now covered
(CASE-216..226), but the AUTHORIZATION layer between them had ZERO coverage: a
request made with VALID node credentials that targets a resource the node does
NOT own, or one that does not exist. CC's settings-request endpoints
(`/nodes/{node_id}/settings/requests/...`, `verify_api_key`) take the target
`node_id` in the URL path and compare it against the authenticated node's
identity before any DB lookup — that comparison is the cross-node isolation
boundary. A fail-open regression (a collapsed comparison, or copy-paste drift
between the read handler and the upload handler) would let any registered node
READ or OVERWRITE another node's pending encrypted settings snapshot — a
cross-tenant data leak / tamper that the happy-path suite (CASE-101..215, which
only ever touches a node's OWN freshly-created requests, e.g. CASE-212) keeps
green through. Authored as a cohesive cohort in the integration-runner fast lane
(`tests/test_cc_node_scope_contracts.py`, gated on `CC_URL` +
`CC_NODE_ID`/`CC_NODE_KEY`). All three branches are reachable deterministically
with the seeded node creds — no waiting, no pre-seeded state — because the
ownership guard fires before the request lookup. Verified against
jarvis-command-center source: `app/node_settings.py:243` (GET node_id mismatch →
403 "Cannot access other node's requests"), `:252` (GET request not found → 404
"Request not found"), `:280` (PUT node_id mismatch → 403 "Cannot upload to other
node's requests"); router mounted at `/api/v0` (`app/main.py:429`).

| Gap | Disposition |
| --- | --- |
| CC GET `/nodes/{id}/settings/requests/{rid}` with valid node creds but a DIFFERENT node_id in the path leaks/serves another node's request (cross-node READ isolation fails open) | **covered** (CASE-227, merged) |
| CC GET own-node settings-request for a nonexistent request_id regresses from a clean 404 to a 500 (or a silent fall-through) | **covered** (CASE-228, merged) |
| CC PUT `/nodes/{id}/settings/requests/{rid}/snapshot` to a DIFFERENT node's request (the write/tamper twin of CASE-227, a separate handler prone to copy-paste drift) overwrites another node's encrypted snapshot | **covered** (CASE-229, merged) |

## 2026-06-22 — config-service `/services` registry contract edges (fast lane)

The CC, auth, and llm-proxy reject contracts are now well covered (CASE-216..229,
303/304), but **jarvis-config-service** — the service-discovery backbone every
Jarvis app reads at boot, and which the fast lane brings up on every stack run —
had only `/health` asserted (CASE-103). Its `/services` registry CONTRACT was
untested: who may MUTATE the registry, and what an unknown lookup returns. The
three mutating routes each INDEPENDENTLY wire `Depends(require_admin)`
(`app/routes/services.py`), so the admin-token write-gate can regress on one verb
without the others — and each verb's fail-open has a distinct blast radius: POST =
inject a rogue service entry (discovery poisoning / MITM), PUT = silently repoint
an existing entry (stealth MITM, separate handler), DELETE = deregister a live
service (DoS, third handler). The read side (`GET /services/{name}`) owes clients a
clean 404-vs-5xx distinction so boot-time discovery can tell "not registered yet"
from "registry broken". The happy-path suite keeps all of this green: the seed only
ever writes with the VALID admin token and reads back services it just registered.

Authored as a cohesive cohort in the integration-runner fast lane
(`tests/test_config_service_registry_contracts.py`, gated on `CC_URL` exactly like
CASE-103/104; `CONFIG_URL` defaults to the compose-mapped port). All four are
deterministic and need NO valid credentials — the 401s send a present-but-WRONG
`X-Admin-Token` (the CI stack sets a non-empty `JARVIS_CONFIG_ADMIN_TOKEN`, so
`require_admin` reaches its compare-and-reject branch, not the unconfigured-500
one), with COMPLETE valid bodies so the auth gate (resolved before body
validation) is the only thing under test; the 404 is an unauthenticated GET. The
mutating verbs target an ABSENT service name so even a failed guard could not touch
a registered entry. Verified against jarvis-config-service source: `app/auth.py`
(`require_admin` wrong token → 401 "Invalid admin token"),
`app/routes/services.py` (`create_service`/`update_service`/`delete_service` each
`Depends(require_admin)`; `get_service` unknown → 404 "Service '{name}' not found").

| Gap | Disposition |
| --- | --- |
| config-service POST `/services` accepts a WRONG admin token (registration write-gate fails open → rogue-service injection / discovery poisoning) | **authored → CASE-230 (PR pending)** |
| config-service PUT `/services/{name}` accepts a WRONG admin token (update write-gate, a separate handler → silent repoint / stealth MITM) | **authored → CASE-231 (PR pending)** |
| config-service DELETE `/services/{name}` accepts a WRONG admin token (deregistration write-gate, a third handler → drop a live service / DoS) | **authored → CASE-232 (PR pending)** |
| config-service GET `/services/{name}` for an unknown service regresses from a clean 404 to a 500 / fall-through (breaks clients distinguishing 'not registered' from 'registry broken') | **authored → CASE-233 (PR pending)** |

### Related negative-path gaps still open (future cohorts)

- **open** — CC `/voice/command/stream` oversized `voice_command` payload
  (boundary → 413/422) against the real stack. Distinct from CASE-226's
  missing-field path; needs the real body-size limit confirmed against source
  before authoring (avoid asserting a framework default).

- **open** — CC node-authed endpoint with a *missing* `X-API-Key` header returns
  422 (FastAPI required-header default). Lower value (framework default, not a
  service decision); deferred to avoid vanity.
- **partially covered** — CC user-JWT endpoints (`verify_user_jwt`): missing
  header → 401 and undecodable Bearer → 401 are **covered** (CASE-221/222,
  merged #11); factory-reset missing-auth → 401 is **covered** (CASE-223,
  merged #11).
  Still **open** (need a JWT-signing fixture or a seeded second user; larger
  setup): expired-token → 401 (`ExpiredSignatureError` branch) and
  valid-but-non-member → 403 (`verify_household_role`).
- **open** — auth `/internal/app-ping` negative paths (wrong/missing app creds →
  401) — same require_app_client gate as CASE-219/220 on a different endpoint;
  likely redundant, evaluate before authoring.
- **open** — `/voice/command/stream` body-contract edges (missing
  `voice_command`, oversized payload → 413/422) against the real stack.
- **covered** (CASE-303/304, merged #10) — llm-proxy `/v1/chat/completions`
  negative app-auth (401 on bad/missing `X-Jarvis-App-*`) in the from-source
  lane — mirror of CASE-219/220 one layer out. See the cohort section above.
