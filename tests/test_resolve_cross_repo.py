"""Unit tests for tools/resolve_cross_repo.py (the T10 cross-repo resolver).

Runs in the standard pytest collect — no service stack, no Docker. Drives the
resolver as a subprocess (the way the workflow does) with GITHUB_OUTPUT unset so
it prints KEY=VAL lines to stdout, and asserts the mapping the cross-repo lane
relies on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "tools" / "resolve_cross_repo.py"


def _run(service: str, ref: str, linked: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("GITHUB_OUTPUT", None)  # force stdout emission
    return subprocess.run(
        [sys.executable, str(RESOLVER), "--service", service,
         "--source-ref", ref, "--linked-prs", linked],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )


def _out(proc: subprocess.CompletedProcess) -> dict[str, str]:
    assert proc.returncode == 0, f"resolver failed: {proc.stderr}"
    return dict(line.split("=", 1) for line in proc.stdout.strip().splitlines() if "=" in line)


def test_cc_plus_llm_proxy_union():
    o = _out(_run("jarvis-command-center", "ccsha", '{"jarvis-llm-proxy-api": "feat/streaming"}'))
    assert o["from_source_count"] == "2"
    assert o["cc_in_set"] == "true" and o["llm_in_set"] == "true"
    # canonical order: cc before llm-proxy
    assert o["overlay_flags"] == (
        "-f compose/ci-overlays/jarvis-command-center-from-source.yaml "
        "-f compose/ci-overlays/jarvis-llm-proxy-api-from-source.yaml"
    )
    assert o["up_non_cc"] == "jarvis-llm-proxy-model jarvis-llm-proxy-api"
    assert "LLM_PROXY_HOST=jarvis-llm-proxy-api" in o["seed_env"]
    assert o["start_fake_llm"] == "false"  # llm real -> no fake
    assert o["start_fake_whisper"] == "true" and o["start_fake_tts"] == "true"
    assert "CROSS_REPO_CC=1" in o["test_env"] and "CROSS_REPO_LLM=1" in o["test_env"]
    assert "LLM_PROXY_URL=http://localhost:7704" in o["test_env"]
    # composition plan: T9 llm cases + the cross-repo composition case
    assert o["plan_cases"] == "CASE-301,CASE-302,CASE-401"
    # routing plan drops the MOCK-asserting cases (302/401), adds 402
    assert o["routing_possible"] == "true"
    assert o["plan_cases_routing"] == "CASE-301,CASE-402"
    checkout = json.loads(o["checkout_json"])
    assert checkout == [
        {"repo": "jarvis-command-center", "ref": "ccsha", "path": "_src/jarvis-command-center"},
        {"repo": "jarvis-llm-proxy-api", "ref": "feat/streaming", "path": "_src/jarvis-llm-proxy-api"},
    ]


def test_single_service_has_no_cross_repo_case():
    o = _out(_run("jarvis-command-center", "ccsha", "{}"))
    assert o["from_source_count"] == "1"
    assert "CASE-401" not in o["plan_cases"]
    assert o["routing_possible"] == "false"
    # nothing real but CC -> all fakes start
    assert o["start_fake_llm"] == "true"
    assert o["start_fake_whisper"] == "true"
    assert o["start_fake_tts"] == "true"
    assert o["up_non_cc"] == ""


def test_originating_ref_wins_over_self_entry():
    # a self-entry in linked_prs must not override the actual PR head
    o = _out(_run("jarvis-llm-proxy-api", "realhead",
                  '{"jarvis-command-center": "cc-branch", "jarvis-llm-proxy-api": "stale"}'))
    refs = {c["repo"]: c["ref"] for c in json.loads(o["checkout_json"])}
    assert refs["jarvis-llm-proxy-api"] == "realhead"
    assert refs["jarvis-command-center"] == "cc-branch"


def test_whisper_combo_uses_whisper_case_and_skips_whisper_fake():
    o = _out(_run("jarvis-command-center", "ccsha", '{"jarvis-whisper-api": "w-branch"}'))
    assert o["plan_cases"] == "CASE-321,CASE-401"
    assert o["routing_possible"] == "false"  # no llm -> no routing variant
    assert o["plan_cases_routing"] == "CASE-321"
    assert o["start_fake_whisper"] == "false"
    assert o["start_fake_llm"] == "true"


def test_unknown_repo_fails_fast():
    proc = _run("jarvis-command-center", "x", '{"jarvis-bogus": "y"}')
    assert proc.returncode == 1
    assert "unknown repo" in proc.stderr and "jarvis-bogus" in proc.stderr


def test_invalid_json_fails_fast():
    proc = _run("jarvis-command-center", "x", "{not json")
    assert proc.returncode == 1
    assert "not valid JSON" in proc.stderr


def test_unknown_originating_service_fails():
    proc = _run("jarvis-bogus", "x", "{}")
    assert proc.returncode == 1
