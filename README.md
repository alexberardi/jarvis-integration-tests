# jarvis-integration-tests

Cross-service integration **and** behavior test harness for the Jarvis ecosystem.
A green per-service CI check only proves a repo's own units pass; this harness
proves that the services **work together** — real auth/node credential
round-trips, service discovery, tool-call parsing, MQTT publishes — and (next,
T6b) that a real model **routes voice commands to the right tools**.

> Migrated 2026-06-21 from `jarvis-node-setup` (T6a). The runner, compose stack,
> fakes, and CASE suite are a faithful lift; node-setup was an odd owner for an
> ecosystem-wide harness (it's the Pi node runtime). The live wiring still points
> at node-setup until cutover — see [Status](#status--cutover).

---

## What it does

When a PR opens in a participating service repo, that repo's
`integration-trigger.yml` fires a `repository_dispatch` here. The runner
([`.github/workflows/integration-runner.yml`](.github/workflows/integration-runner.yml)):

1. Checks out the originating service's PR source into `_src/<service>/`.
2. Brings up a real, ephemeral service stack on `ubuntu-latest` via
   [`docker-compose.ci.yaml`](docker-compose.ci.yaml).
3. Runs the marker-bound pytest suite (`tests/test_loop_smoke.py`,
   `tests/test_cc_real_smoke.py`) as an `X-API-Key` node client.
4. Parses results with [`tools/parse_junit.py`](tools/parse_junit.py) and posts a
   `<!-- integration-test-results:v1 -->` comment + a commit status back on the
   originating PR — the QA agent stays read-only; CI does the work.

## The stack (5 containers + 3 host fakes)

The minimal voice round-trip (node → CC → LLM → tool → TTS):

- **Containers:** one Postgres (`pgvector/pgvector:pg15`, hosting
  `jarvis_command_center` + `jarvis_auth` + `jarvis_config` via
  `compose/postgres-init.sh`), one Mosquitto, **jarvis-auth**,
  **jarvis-config-service**, **jarvis-command-center**.
- **Host-process fakes** (`tests/fakes/`, started by the runner, reached via
  `host.docker.internal`): `fake_llm_backend:7705`, `fake_whisper:7706`,
  `fake_tts:7707`. Driven by `tests/fakes/canned_responses.yaml`.
- **The node is the pytest client** — it drives CC's HTTP API with seeded
  `X-API-Key` node creds. There is **no node container**; the harness imports no
  Jarvis service code, only `httpx`/`paho-mqtt`.

The bring-up is **two-phase on purpose** (`compose/seed.sh` runs between them):
deps + auth + config come up first, the seed mints CC's app-key **and** the
node-key (an auth row *and* a CC-local `nodes` row), then CC starts with that
key. Collapsing the phases breaks the credential chain.

### Two lanes

| Lane | Trigger | LLM/STT/TTS | Proves | Cost |
|---|---|---|---|---|
| **Fast** | every PR (hot-path repos) | host fakes (canned) | wiring + contracts: auth round-trips, discovery, MQTT, CC tool-call parsing | free, ~3–4 min |
| **Behavior** *(T6b — not yet wired here)* | nightly + on-demand | llm-proxy `REST` → cheap cloud model (gpt-4.1-nano) via CC's real `ChatGPTOpenAI` provider | *does the feature actually work* — a voice-command corpus asserts utterances route to the correct tools with sensible args | pennies/run |

> The behavior corpus already exists, hardened and proven, in
> `jarvis-llm-proxy-api/tests/manual/behavior/{corpus,tools}.yaml` (provider-agnostic
> YAML, 30/30 live). **T6b** routes that same corpus through CC's real provider +
> the full stack here, instead of llm-proxy's inlined stand-in toolset.

## Run it standalone

The runner exposes `workflow_dispatch`, so you can drive it without a PR:

```bash
gh workflow run integration-runner.yml \
  --repo alexberardi/jarvis-integration-tests --ref main \
  -f service=jarvis-command-center \
  -f pr_number=<n> \
  -f head_sha=<sha-on-CC-main> \
  -f originating_repo=alexberardi/jarvis-command-center
```

This requires the [secrets + GHCR access](#status--cutover) below.

For local reproduction, see [`docs/integration-tests.md`](docs/integration-tests.md)
(the deep reference — note its migration banner).

## Layout

```
docker-compose.ci.yaml          # the 5-container CI stack (project name: jarvis-ci)
compose/
  seed.sh                       # two-phase seed: app-keys + node-key + CI user
  postgres-init.sh              # creates jarvis_auth + jarvis_config DBs
  ci-overlays/                  # *-from-source.yaml — swap a service's :dev image for a PR build
tests/
  conftest.py                   # qa_case marker -> JUnit property (joined by parse_junit)
  test_loop_smoke.py            # CASE-001..003 — fakes-only, no stack
  test_cc_real_smoke.py         # CASE-101..215 — full real-stack round-trips
  fakes/                        # fake_llm/whisper/tts + canned_responses.yaml
tools/parse_junit.py            # JUnit XML -> case-status JSON for the PR comment
requirements-ci.txt             # test-client deps (pytest, httpx, paho-mqtt, FastAPI fakes)
.github/workflows/integration-runner.yml
docs/integration-tests.md       # full reference (migrated; see its banner)
```

## CASE catalog

- **CASE-001..003** — fakes-only loop smoke (no service stack).
- **CASE-101..104** — service health endpoints.
- **CASE-201..215** — full voice-command flow: auth + node registration, tool
  execution, validation, multi-tool, mixed server+client tools, MQTT publishes
  (settings / factory-reset / package-install: CASE-212/214/215), streaming audio.

The `repository_dispatch` payload's `plan_cases` (comma-separated CASE-IDs)
selects which cases a given run reports; missing ones are surfaced as
`not-implemented` rather than silently skipped.

## Status / cutover

T6a stood up this repo as a **faithful copy**. The live loop is **not yet
switched over** — node-setup's runner remains authoritative until:

1. **Secrets** in this repo: `INTEGRATION_COMMENT_TOKEN` (fine-grained PAT:
   `Pull requests: write` + `Commit statuses: write` on each participating
   service repo).
2. **GHCR Actions access:** add `jarvis-integration-tests` to the "Manage Actions
   access" allowlist for the `:dev` packages (`jarvis-auth`,
   `jarvis-config-service`, and `jarvis-command-center` if private).
3. **Validate** a standalone `workflow_dispatch` run end-to-end (above).
4. **Retarget** `jarvis-command-center/.github/workflows/integration-trigger.yml`
   to dispatch to `/repos/alexberardi/jarvis-integration-tests/dispatches`, and
   re-scope CC's `INTEGRATION_DISPATCH_TOKEN` to this repo.
5. **Retire** node-setup's runner once a real PR round-trips green here.

`tests/integration/` from node-setup was **deliberately not migrated** — it is a
node-client *unit* layer that imports the node app + `jarvis-command-sdk` and
belongs with that app's source, not in a cross-service harness.

## Provenance

Faithfully migrated from `alexberardi/jarvis-node-setup` on 2026-06-21. See
`prds/testing-infrastructure.md` (T6) in the `jarvis` meta-repo for the rationale.
