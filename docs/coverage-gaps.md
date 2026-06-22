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
| CC node-authed endpoint accepts an unknown `id:key` (centralized-auth reject path) | **authored → CASE-216 (PR pending)** |
| CC node-authed endpoint accepts a malformed no-colon key (legacy-fallback reject path) | **authored → CASE-217 (PR pending)** |
| CC `/admin/nodes` (node-provisioning) accepts a wrong admin key | **authored → CASE-218 (PR pending)** |
| auth `/internal/validate-node` accepts a wrong app key (app-to-app gate) | **authored → CASE-219 (PR pending)** |
| auth `/internal/validate-node` accepts missing app headers (anonymous caller) | **authored → CASE-220 (PR pending)** |

### Related negative-path gaps still open (future cohorts)

- **open** — CC node-authed endpoint with a *missing* `X-API-Key` header returns
  422 (FastAPI required-header default). Lower value (framework default, not a
  service decision); deferred to avoid vanity.
- **open** — CC user-JWT endpoints (`verify_user_jwt`): expired token → 401,
  malformed Bearer → 401, valid-but-non-member → 403 (`verify_household_role`).
  Needs a JWT-signing fixture or a seeded second user; larger setup, separate
  cohort.
- **open** — auth `/internal/app-ping` negative paths (wrong/missing app creds →
  401) — same require_app_client gate as CASE-219/220 on a different endpoint;
  likely redundant, evaluate before authoring.
- **open** — `/voice/command/stream` body-contract edges (missing
  `voice_command`, oversized payload → 413/422) against the real stack.
- **open** — llm-proxy `/v1/chat/completions` negative app-auth (401 on bad
  `X-Jarvis-App-*`) in the from-source lane — mirror of CASE-219 one layer out.
