# Mobile e2e lane (provisioning) — runbook

The top of the mobile testing pyramid (`prds/mobile-app-delivery.md` Phase 2 → L2):
drive a **real `jarvis-node-mobile` dev-client build** in an iOS simulator through
the provisioning happy-path, against the dockerized **fake node** on the core
auth/config/CC stack, and assert the node registered **via command-center**.

This doc is the canonical record for the lane: what already runs, the runner-topology
decision, the networking + config-discovery constraints, and a shakeout checklist.

---

## What runs today vs. what is scaffold

| Piece | Where | Status |
|---|---|---|
| **Node-side wire contract** | `tests/test_provisioning_contract.py` (here) | ✅ runs (pure HTTP, gated on `FAKE_NODE_URL`) |
| **Mobile-side wire contract** | `jarvis-node-mobile/__tests__/api/provisioningApi.contract.test.ts` | ✅ runs every node-mobile PR (jest) |
| **Maestro flow + testIDs + EAS profile** | `jarvis-node-mobile/.maestro/*`, `development-e2e` profile | ✅ authored, selectors verified; runs once a sim build exists |
| **CC node-online assertion** | `tools/assert_node_online.py` (here) | ✅ logic smoke-tested; runs in the lane |
| **The CI lane itself** | `.github/workflows/mobile-e2e.yml` | 🟡 **SCAFFOLD** — advisory, `workflow_dispatch` only; needs shakeout |

The two **wire-contract** tests are the durable win and run now — they pin the
hand-mirrored TS ↔ Pydantic provisioning contract so a rename / required-ness flip
on either side turns red instead of shipping silently to a Pi. The **CI lane** wires
the actual sim run; it has not yet gone green end-to-end (needs a macOS runner +
simulator + an EAS dev-client build) and is the work the checklist below covers.

---

## The wire contract (validate locally — no simulator)

The provisioning HTTP contract is hand-mirrored with **no build-time link**:
`jarvis-node-setup/provisioning/{models.py,api.py}` ↔
`jarvis-node-mobile/src/{types/Provisioning.ts,api/provisioningApi.ts}`. Two live
drift risks the contract pins:

- `NodeInfo.previously_provisioned` exists node-side, absent from the TS type.
- The **K2** request is camelCase in TS (`nodeId`/`createdAt`) but snake_case on the
  wire, bridged ONLY by a hand-written transform (`provisioningApi.ts:147-152`). A
  K2 field added to the type but not the transform POSTs a body the node 422s.

Both tests assert the same field sets (`PROVISIONING_WIRE_CONTRACT`). Keep the two
copies in lockstep — a node-side change should fail `test_provisioning_contract.py`
*and* prompt the matching edit to `Provisioning.ts` + the mobile test.

**Node side** — boot the SAME FastAPI app a real Pi runs, then run the test:

```bash
# 1) a lightweight venv (provisioning.api needs only pydantic/fastapi/httpx/
#    cryptography/uvicorn + the local jarvis-log-client; NOT the full Pi deps)
python3 -m venv /tmp/prov && /tmp/prov/bin/pip install \
  pydantic 'fastapi>=0.100' httpx 'cryptography>=41' pytz sqlalchemy python-dotenv \
  'uvicorn>=0.22' pytest pyyaml
/tmp/prov/bin/pip install -e ../jarvis-log-client

# 2) boot the simulated provisioning server (no root; captive-portal :80 fails closed)
( cd ../jarvis-node-setup && JARVIS_SIMULATE_PROVISIONING=true JARVIS_PROVISIONING_PORT=8080 \
    /tmp/prov/bin/python scripts/run_provisioning.py & )

# 3) run the contract test against it
FAKE_NODE_URL=http://localhost:8080 /tmp/prov/bin/python -m pytest tests/test_provisioning_contract.py -v
# => 20 passed.  With no server reachable it cleanly skips (the CI default).
```

**Mobile side** — pure jest (no node/server):

```bash
cd ../jarvis-node-mobile && npx jest __tests__/api/provisioningApi.contract.test.ts
```

---

## Runner topology (the load-bearing decision)

The backend stack is **linux docker**; the iOS simulator needs **macOS**. The sim
and the stack must share a network. The options:

| Topology | How the sim reaches the backend | Verdict |
|---|---|---|
| **Single Intel macOS + colima** (what `mobile-e2e.yml` scaffolds) | one `macos-13` runner runs colima (Docker) **and** the sim → plain `localhost` | ✅ self-contained on GitHub-hosted; ❌ `macos-13` is **deprecated**, colima boot is slow/occasionally flaky |
| **Two jobs (ubuntu docker + Apple-Silicon macOS sim) + tunnel** | a `cloudflared`/`ngrok` tunnel from the ubuntu job; the sim hits the public URL | future-proof runner images, but needs tunnel glue + cross-job state + keeping the backend job alive while the sim job runs |
| **Self-hosted macOS runner on the same LAN as a linux docker host** ⭐ | the sim hits the docker host by LAN IP | **recommended target** — no nested-virt, no tunnel, no deprecated image; Jarvis already has LAN infra (the mac + the Pi/GPU box) |

Apple-Silicon GitHub macOS runners (`macos-14/15`) **cannot run Docker** (no nested
virt), which is why the single-job path is pinned to Intel `macos-13`. The scaffold
picks single-job colima because it's the only **self-contained GitHub-hosted** path
today; treat it as a stopgap and move to the **self-hosted LAN** topology for a
durable lane.

---

## Networking — seed the HOST LAN IP, not localhost

The app sends its OWN `command_center_url` into the fake node's `POST /provision`,
and the node then registers with CC at that URL. For one URL to be reachable from
**both** the simulator (host side) **and** the fake-node container:

- `localhost` → the host from the sim, but the *container itself* from inside it ✗
- `host.docker.internal` → the host from the container, but unresolvable from the sim ✗
- the runner's **host LAN IP** (e.g. `http://<lan-ip>:7703`) → reachable from both ✓

In the **single-host colima** topology the sim and the colima-published ports share
the host net, so `localhost:7703` / `localhost:8080` work for the sim, and the fake
node reaches CC via the compose network (`jarvis-command-center:8002`) /
`host.docker.internal`. In any **split-host** topology, seed the app's CC URL as the
LAN IP. (`compose/ci-overlays/fake-node.yaml` documents the same constraint.)

---

## Config discovery — the main open shakeout item

Before provisioning, the app must reach **config-service** to fetch a provisioning
token. A fresh `clearState` app discovers config via (in order) a manual URL in
AsyncStorage → cached config → mDNS → a `/24` sweep. None of these resolve cleanly in
CI, so the most robust fix is a **small app change**: in `DEV_MODE`, seed the manual
config URL from an `EXPO_PUBLIC_MANUAL_CONFIG_URL` env (baked by the `development-e2e`
profile, like `EXPO_PUBLIC_DEV_MODE`). Then `connect-simulator-button` →
token-fetch → provision works without a UI detour. Until that lands, the Maestro flow
will stall at the token fetch — this is the first thing to wire during shakeout.

---

## CC assertion — registered vs. online

The app's Success screen reports success **even on node error**, so the lane asserts
via CC (`tools/assert_node_online.py` → `GET /api/v0/admin/nodes`):

- **registered** (default): ≥1 node registered for the seeded household. This proves
  the app → fake-node → CC registration chain ran. The floor signal.
- **`--require-online`**: also require CC `online == true` (`node.is_online()` =
  `last_seen` within 15 min). The provisioning **server** does not heartbeat like the
  node runtime, so `online` may never flip in sim mode. Keep this **off** until
  shakeout confirms whether the fake node heartbeats (if not, either have the
  provisioning path send one heartbeat, or gate only on `registered`).

---

## Prerequisites / secrets

- `EXPO_TOKEN` secret on this repo (for `eas build --local --non-interactive`).
- GHCR read for the base `:dev` images (already configured for the other lanes).
- The `development-e2e` EAS profile in `jarvis-node-mobile/eas.json`
  (`EXPO_PUBLIC_DEV_MODE=true` baked).
- `secrets:` referenced by the workflow are otherwise the same as the other lanes
  (`GITHUB_TOKEN` for GHCR).

---

## Shakeout checklist (ordered)

1. **Backend boots on the chosen topology** — `docker compose ... --profile core
   --profile mobile-e2e up --wait` reaches `CC /api/v0/health` + fake-node
   `/api/v1/info`. Run `tests/test_provisioning_contract.py` (it gates the rest).
2. **`eas build --local --profile development-e2e`** produces a sim `.app`; confirm
   `EXPO_PUBLIC_DEV_MODE=true` is baked (the "Show Developer Options" panel appears).
   Add CocoaPods/Pods/Hermes caching + a native-dep-hash rebuild gate (cold build is
   ~12–25 min on a macOS runner).
3. **Config-discovery injection** — wire `EXPO_PUBLIC_MANUAL_CONFIG_URL` (app change
   above) so the app fetches a provisioning token in CI.
4. **Maestro selectors** — confirm the two text-tap steps the flow marks `SHAKEOUT`
   (the "Nodes" bottom-tab label, the "Add Node" FAB); add testIDs if flaky. The
   provisioning-screen taps already use testIDs added in the node-mobile P3 PR.
5. **node-online assertion** — confirm `registered` passes; decide whether `online`
   is achievable (heartbeat) before flipping `--require-online`.
6. **Stabilise → gate** — per the locked decision, run **advisory nightly** first;
   hard-gate the provisioning happy-path only after **3 consecutive stable runs**,
   then add Android (works on GitHub-hosted ubuntu via KVM). Keep the
   per-release **manual hardware checklist** (1 Pi + 1 phone) for the coverage the
   simulator can't reach: the real SoftAP join, `nmcli`/`hostapd`, AP-drop timing,
   captive portal, real-radio scan, and the wrong-creds → node-ERROR → app-still-Success
   case.

---

## Files

- `tests/test_provisioning_contract.py` — node-side wire contract (here).
- `tools/assert_node_online.py` — the CC registration/online assertion (here).
- `.github/workflows/mobile-e2e.yml` — the lane scaffold (here).
- `compose/ci-overlays/fake-node.yaml` — the fake-node overlay (P2, here).
- `jarvis-node-mobile/.maestro/*` — the Maestro flows + a local README.
- `jarvis-node-mobile/__tests__/api/provisioningApi.contract.test.ts` — mobile-side mirror.
- `jarvis-node-mobile/eas.json` — the `development-e2e` profile.
