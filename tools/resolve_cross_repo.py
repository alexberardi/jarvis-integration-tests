#!/usr/bin/env python3
"""T10 — resolve a cross-repo `linked_prs` set into everything the cross-repo
lane needs to bring up N services from source together.

The cross-repo lane (.github/workflows/cross-repo-services.yml) validates a
multi-repo feature as ONE unit: given an originating PR plus a `linked_prs` map
({repo: branch_or_sha}), it builds {originating} ∪ keys(linked_prs) from source
in a single stack and runs the cross-repo case(s). This resolver is the pure,
unit-tested mapping at the heart of that lane (mirrors tools/parse_junit.py:
stdlib only, no third-party deps, GITHUB_OUTPUT-aware).

It emits (to $GITHUB_OUTPUT, or stdout when that's unset):

  from_source_names   space-separated repo slugs in the union (canonical order)
  from_source_count   len(union)
  cc_in_set           true|false  — command-center is built from source
  llm_in_set          true|false  — llm-proxy is built from source
  overlay_flags       the composed `-f compose/ci-overlays/<svc>-from-source.yaml`
                      chain (canonical order; merge VERIFIED with `compose config`)
  up_non_cc           the add-container services (llm-proxy/whisper/tts) in the
                      set to bring up in Phase 1.5 (NOT cc/auth/config — those
                      come up in the deps/Phase-2 steps)
  seed_env            space-separated KEY=VAL pairs for seed.sh so discovery
                      points each real service at its container (host fakes
                      otherwise)
  test_env            space-separated KEY=VAL gate flags consumed by the test
                      files (CROSS_REPO_*, LLM_PROXY_URL, *_FROM_SOURCE)
  start_fake_llm/whisper/tts   true|false — start a host fake ONLY for a service
                      NOT built from source (else its port collides with the
                      real container's host mapping)
  plan_cases          composition-mode (MOCK backend) case set: per-service T9
                      cases + CASE-401 when >=2 services build together
  plan_cases_routing  routing-mode (REST->cloud backend) case set: the
                      MOCK-asserting cases (302/401) dropped, CASE-402 added when
                      cc+llm both build (used only when an OPENAI key is present)
  routing_possible    true|false — cc AND llm both in the set (a CC->proxy
                      routing variant is meaningful)
  checkout_json       JSON list of {repo, ref, path} for the clone loop

An unknown repo name (no matching *-from-source.yaml overlay) or a missing
overlay file is a hard ::error:: BEFORE any build, so a typo'd dependency is
loud rather than failing 8 minutes into a Docker build with an empty context.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

# repo slug == compose service name == overlay stem. The six services that have a
# *-from-source.yaml overlay are the cross-repo vocabulary.
#   phase: how the service is brought up.
#     "deps" — named in the Phase-1 deps bring-up (postgres+auth+config+mqtt)
#     "cc"   — command-center, brought up last in Phase 2
#     "add"  — an add-container service (llm/whisper/tts), Phase 1.5
#   up:   compose service(s) for that repo
#   fake: the host-fake key to SKIP when this service is real (None for cc/auth/config)
#   seed: seed.sh discovery override(s) pointing CC at the real container
#   test: gate env the test files read
#   always_cases:      T9 cases that run regardless of llm backend
#   composition_cases: extra cases valid only on the MOCK backend
KNOWN: dict[str, dict] = {
    "jarvis-auth": dict(
        overlay="jarvis-auth-from-source.yaml", phase="deps", up=["jarvis-auth"],
        fake=None, seed="", test="CROSS_REPO_AUTH=1",
        always_cases=[], composition_cases=[],
    ),
    "jarvis-config-service": dict(
        overlay="jarvis-config-service-from-source.yaml", phase="deps",
        up=["jarvis-config-service"], fake=None, seed="", test="CROSS_REPO_CONFIG=1",
        always_cases=[], composition_cases=[],
    ),
    "jarvis-command-center": dict(
        overlay="jarvis-command-center-from-source.yaml", phase="cc",
        up=["jarvis-command-center"], fake=None, seed="", test="CROSS_REPO_CC=1",
        always_cases=[], composition_cases=[],
    ),
    "jarvis-llm-proxy-api": dict(
        overlay="jarvis-llm-proxy-api-from-source.yaml", phase="add",
        up=["jarvis-llm-proxy-model", "jarvis-llm-proxy-api"], fake="llm",
        seed="LLM_PROXY_HOST=jarvis-llm-proxy-api LLM_PROXY_PORT=7704",
        test="CROSS_REPO_LLM=1 LLM_PROXY_FROM_SOURCE=1 LLM_PROXY_URL=http://localhost:7704",
        # 301 = API->model hop (backend-agnostic). 303/304 = app-auth REJECT paths
        # (wrong key / missing headers -> 401), which short-circuit before the
        # backend, so they hold on MOCK and REST alike -> always. 302 = direct
        # /v1/chat MOCK echo (MOCK-only — it asserts "mock" in the reply, which a
        # REST->cloud backend would not return, so it's composition-only).
        always_cases=["CASE-301", "CASE-303", "CASE-304"],
        composition_cases=["CASE-302"],
    ),
    "jarvis-whisper-api": dict(
        overlay="jarvis-whisper-api-from-source.yaml", phase="add",
        up=["jarvis-whisper-api"], fake="whisper",
        seed="WHISPER_HOST=jarvis-whisper-api WHISPER_PORT=7706",
        test="CROSS_REPO_WHISPER=1 WHISPER_FROM_SOURCE=1",
        always_cases=["CASE-321"], composition_cases=[],
    ),
    "jarvis-tts": dict(
        overlay="jarvis-tts-from-source.yaml", phase="add", up=["jarvis-tts"],
        fake="tts", seed="TTS_HOST=jarvis-tts TTS_PORT=7707",
        test="CROSS_REPO_TTS=1 TTS_FROM_SOURCE=1",
        always_cases=["CASE-311"], composition_cases=[],
    ),
    "jarvis-phone-gateway": dict(
        overlay="jarvis-phone-gateway-from-source.yaml", phase="add",
        up=["jarvis-phone-gateway"],
        # Downstream CONSUMER of CC/config/auth — replaces no host fake and
        # needs no CC seed repoint (the inverse of the llm/whisper/tts lanes).
        fake=None, seed="",
        test="PHONE_GATEWAY_URL=http://localhost:7713",
        # 331 = /health body with no Twilio creds; 332 = media-WS upgrade
        # handled + fails closed on a bogus session token (403, not 404).
        # Neither touches the LLM backend -> both always.
        always_cases=["CASE-331", "CASE-332"], composition_cases=[],
    ),
}

# Canonical bring-up order: deps first, then CC, then add-containers. Keeps the
# overlay -f chain deterministic (order-independent for merge, but stable output
# makes the unit tests and CI logs readable).
ORDER = [
    "jarvis-auth",
    "jarvis-config-service",
    "jarvis-command-center",
    "jarvis-llm-proxy-api",
    "jarvis-whisper-api",
    "jarvis-tts",
]


def _fail(msg: str) -> NoReturn:
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def _emit(out: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    fh = open(path, "a") if path else sys.stdout
    try:
        for key, val in out.items():
            fh.write(f"{key}={val}\n")
    finally:
        if path:
            fh.close()


def resolve(service: str, source_ref: str, linked_prs: str, overlay_dir: str) -> dict[str, str]:
    try:
        linked = json.loads(linked_prs or "{}")
    except json.JSONDecodeError as exc:
        _fail(f"linked_prs is not valid JSON: {exc} (got {linked_prs!r})")
    if not isinstance(linked, dict):
        _fail("linked_prs must be a JSON object {repo: ref}")

    # The originating service is always part of the set and wins on its own ref
    # (a self-entry in linked_prs cannot override the actual PR head).
    fset: dict[str, str] = dict(linked)
    fset[service] = source_ref

    odir = Path(overlay_dir)
    for name in fset:
        if name not in KNOWN:
            _fail(
                f"unknown repo '{name}' in the cross-repo set — no from-source "
                f"overlay exists. Known: {', '.join(sorted(KNOWN))}"
            )
        overlay = odir / KNOWN[name]["overlay"]
        if not overlay.exists():
            _fail(f"overlay missing for '{name}': {overlay}")

    names = [n for n in ORDER if n in fset]
    count = len(names)
    cc_in = "jarvis-command-center" in fset
    llm_in = "jarvis-llm-proxy-api" in fset

    overlay_flags = " ".join(f"-f {overlay_dir}/{KNOWN[n]['overlay']}" for n in names)
    up_non_cc = " ".join(
        svc for n in names if KNOWN[n]["phase"] == "add" for svc in KNOWN[n]["up"]
    )
    seed_env = " ".join(KNOWN[n]["seed"] for n in names if KNOWN[n]["seed"]).strip()
    test_env = " ".join(KNOWN[n]["test"] for n in names if KNOWN[n]["test"]).strip()

    fakes = {"llm": "true", "whisper": "true", "tts": "true"}
    for n in names:
        key = KNOWN[n]["fake"]
        if key:
            fakes[key] = "false"

    always = sorted({c for n in names for c in KNOWN[n]["always_cases"]})
    composition = sorted({c for n in names for c in KNOWN[n]["composition_cases"]})

    plan = list(always) + list(composition)
    if count >= 2:
        plan.append("CASE-401")  # cross-repo composition signal (MOCK backend)

    # Routing plan: REST->cloud backend, so the MOCK-asserting cases (302 + the
    # MOCK half of 401) don't apply; CASE-402 (CC routes voice -> from-source
    # proxy -> cloud) replaces them. Only meaningful when cc AND llm both build.
    plan_routing = list(always)
    if cc_in and llm_in:
        plan_routing.append("CASE-402")

    checkout = [{"repo": n, "ref": fset[n], "path": f"_src/{n}"} for n in names]

    return {
        "from_source_names": " ".join(names),
        "from_source_count": str(count),
        "cc_in_set": "true" if cc_in else "false",
        "llm_in_set": "true" if llm_in else "false",
        "overlay_flags": overlay_flags,
        "up_non_cc": up_non_cc,
        "seed_env": seed_env,
        "test_env": test_env,
        "start_fake_llm": fakes["llm"],
        "start_fake_whisper": fakes["whisper"],
        "start_fake_tts": fakes["tts"],
        "plan_cases": ",".join(plan),
        "plan_cases_routing": ",".join(plan_routing),
        "routing_possible": "true" if (cc_in and llm_in) else "false",
        "checkout_json": json.dumps(checkout),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--service", required=True, help="originating service slug")
    ap.add_argument("--source-ref", required=True, help="originating ref (sha/branch)")
    ap.add_argument("--linked-prs", default="{}", help="JSON map {repo: ref}")
    ap.add_argument("--overlay-dir", default="compose/ci-overlays")
    args = ap.parse_args()
    if args.service not in KNOWN:
        _fail(
            f"originating service '{args.service}' has no from-source overlay. "
            f"Known: {', '.join(sorted(KNOWN))}"
        )
    _emit(resolve(args.service, args.source_ref, args.linked_prs, args.overlay_dir))


if __name__ == "__main__":
    main()
