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
| CC `/nodes/{id}/settings/requests` accepts a MISSING `Authorization` header (presence-check branch of verify_user_jwt) | **authored → CASE-221 (PR pending)** |
| CC `/nodes/{id}/settings/requests` accepts a present-but-undecodable Bearer token (jwt.decode reject branch) | **authored → CASE-222 (PR pending)** |
| CC `/admin/nodes/{id}/factory-reset` (device-bricking surface) accepts a MISSING `Authorization` header — proves the destructive endpoint is auth-gated at all | **authored → CASE-223 (PR pending)** |

### Related negative-path gaps still open (future cohorts)

- **open** — CC node-authed endpoint with a *missing* `X-API-Key` header returns
  422 (FastAPI required-header default). Lower value (framework default, not a
  service decision); deferred to avoid vanity.
- **partially covered** — CC user-JWT endpoints (`verify_user_jwt`): missing
  header → 401 and undecodable Bearer → 401 are now **authored → CASE-221/222
  (PR pending)**; factory-reset missing-auth → 401 is **CASE-223 (PR pending)**.
  Still **open** (need a JWT-signing fixture or a seeded second user; larger
  setup): expired-token → 401 (`ExpiredSignatureError` branch) and
  valid-but-non-member → 403 (`verify_household_role`).
- **open** — auth `/internal/app-ping` negative paths (wrong/missing app creds →
  401) — same require_app_client gate as CASE-219/220 on a different endpoint;
  likely redundant, evaluate before authoring.
- **open** — `/voice/command/stream` body-contract edges (missing
  `voice_command`, oversized payload → 413/422) against the real stack.
- **authored → CASE-303/304 (PR pending)** — llm-proxy `/v1/chat/completions`
  negative app-auth (401 on bad/missing `X-Jarvis-App-*`) in the from-source
  lane — mirror of CASE-219/220 one layer out. See the cohort section above.
