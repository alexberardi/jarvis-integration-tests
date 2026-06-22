"""T10 — cross-repo composition + routing cases.

A case here only runs (and only passes) when >=2 services are built from source
TOGETHER in one stack, proving a multi-repo feature composes as ONE unit — the
convergence of the testing fix and the unit-of-work fix. The cross-repo lane
(.github/workflows/cross-repo-services.yml) brings up {originating} ∪
keys(linked_prs) from source; tools/resolve_cross_repo.py sets the CROSS_REPO_*
gate flags below.

Two mutually-exclusive modes (the llm-proxy backend is one or the other per
bring-up):

  CASE-401  COMPOSITION (no key, default).  llm-proxy on the MOCK backend.
            A direct app-auth'd PLAIN /v1/chat to the from-source proxy using
            CC's seed.sh-minted command-center creds. Green proves BOTH PR builds
            booted, the CC-issued credential validates against the SAME auth from
            INSIDE the linked-proxy container (the cross-service credential
            chain), and the API->model internal-token hop works in BOTH PRs'
            code. It is a COMPOSITION/credential signal, NOT a routing signal —
            a no-key CC->proxy routing test is impossible (CC's default
            json_object voice path 500s on MOCK; chat_runner requires_json).

  CASE-402  ROUTING (key-gated).  llm-proxy on the REST backend -> gpt-4.1-nano.
            One voice utterance routed through CC's REAL ChatGPTOpenAI native
            tool-calling path -> the from-source proxy -> the cloud model, and
            back as a structured tool_call. This is the full cross-repo behavior
            signal: a real model, handed CC's real tools through CC's real
            provider, served by the PR-built proxy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - the workflow installs pyyaml
    yaml = None

# Set by tools/resolve_cross_repo.py via the lane's test_env.
CROSS_REPO_CC = os.environ.get("CROSS_REPO_CC")
CROSS_REPO_LLM = os.environ.get("CROSS_REPO_LLM")
# Set ONLY in the key-gated routing variant (REST backend + llm.interface flip).
CROSS_REPO_ROUTING = os.environ.get("CROSS_REPO_ROUTING")

LLM_PROXY_URL = os.environ.get("LLM_PROXY_URL", "")
# CASE-401 reuses CC's SEEDED app creds (minted by the same auth CC uses), so a
# 200 proves the credential chain holds across the two independently-built PRs.
LLM_PROXY_APP_ID = os.environ.get("LLM_PROXY_APP_ID", "command-center")
LLM_PROXY_APP_KEY = os.environ.get("LLM_PROXY_APP_KEY", "")

CC_URL = os.environ.get("CC_URL")
CC_NODE_ID = os.environ.get("CC_NODE_ID", "")
CC_NODE_KEY = os.environ.get("CC_NODE_KEY", "")

_CROSS_REPO_PAIR = bool(CROSS_REPO_CC and CROSS_REPO_LLM)
SKIP_NOT_PAIR = "needs command-center AND llm-proxy both built from source"


# --------------------------------------------------------------------------- #
# CASE-401 — CC + llm-proxy compose as ONE unit (NO cloud key; default demo).
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _CROSS_REPO_PAIR, reason=SKIP_NOT_PAIR)
@pytest.mark.skipif(
    bool(CROSS_REPO_ROUTING),
    reason="routing mode (REST backend) — the MOCK-echo composition probe N/A",
)
@pytest.mark.skipif(not LLM_PROXY_APP_KEY, reason="CC seeded app key not passed")
@pytest.mark.qa_case("CASE-401")
def test_cc_and_llm_proxy_compose_as_a_unit() -> None:
    # (a) the from-source proxy API reached the from-source model service.
    health = httpx.get(f"{LLM_PROXY_URL.rstrip('/')}/health", timeout=15.0)
    assert health.status_code == 200, (
        f"proxy /health {health.status_code}: {health.text[:300]}"
    )
    assert health.json().get("status") == "healthy", (
        f"API server did not reach the model service (internal-token hop): "
        f"{health.json()}"
    )

    # (b) CC's seed-minted app credential validates against auth from INSIDE the
    # linked-proxy build, and the full API -> model -> MOCK -> OpenAI-shaped
    # response chain works. PLAIN message (no response_format) so MOCK is happy.
    resp = httpx.post(
        f"{LLM_PROXY_URL.rstrip('/')}/v1/chat/completions",
        headers={
            "X-Jarvis-App-Id": LLM_PROXY_APP_ID,
            "X-Jarvis-App-Key": LLM_PROXY_APP_KEY,
        },
        json={"model": "live", "messages": [{"role": "user", "content": "hello cross-repo"}]},
        timeout=60.0,
    )
    assert resp.status_code == 200, (
        f"cross-repo /v1/chat failed: {resp.status_code} {resp.text[:400]} — a 401 "
        f"means CC's app key did not validate against auth from inside the linked "
        f"proxy build (credential-chain break across the two PRs)."
    )
    content = (
        resp.json().get("choices", [{}])[0].get("message", {}).get("content") or ""
    ).lower()
    assert "mock" in content, (
        f"expected the MOCK backend echo (proves the whole API->model->MOCK chain "
        f"in PR-built code), got {content!r}"
    )


# --------------------------------------------------------------------------- #
# CASE-402 — CC routes a voice command through the from-source proxy to the
# cloud model (key-gated routing variant).
# --------------------------------------------------------------------------- #
_BEHAVIOR_DIR = Path(__file__).parent / "behavior"


def _load_tools() -> list:
    if yaml is None:
        return []
    with (_BEHAVIOR_DIR / "tools.cc.yaml").open() as fh:
        return yaml.safe_load(fh) or []


def _node_headers() -> dict:
    return {"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"}


@pytest.mark.behavior
@pytest.mark.skipif(not _CROSS_REPO_PAIR, reason=SKIP_NOT_PAIR)
@pytest.mark.skipif(
    not CROSS_REPO_ROUTING,
    reason="routing variant not enabled (needs OPENAI key + llm.interface flip)",
)
@pytest.mark.skipif(
    not (CC_URL and CC_NODE_ID and CC_NODE_KEY),
    reason="CC stack / node creds not set",
)
@pytest.mark.qa_case("CASE-402")
def test_cc_routes_voice_through_from_source_proxy_to_cloud() -> None:
    """One unambiguous utterance through CC's real native tool path.

    Mirrors test_cc_behavior_corpus.py but a single, deterministic utterance —
    the cross-repo lane proves the COMPOSITION end to end (CC's real
    ChatGPTOpenAI provider -> the PR-built proxy -> the cloud model -> a
    structured tool_call), not the full routing corpus (that's the behavior
    lane's job). Asserts tool selection + the one reliably-filled arg.
    """
    tools = _load_tools()
    assert tools, "tools.cc.yaml failed to load (is pyyaml installed?)"

    conv_id = "ci-xrepo-402"
    start = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers=_node_headers(),
        json={
            "conversation_id": conv_id,
            "client_tools": tools,
            "available_commands": [],
            "skip_warmup_inference": True,
        },
        timeout=60.0,
    )
    assert start.status_code == 200, (
        f"/conversation/start failed: {start.status_code} body={start.text[:300]}"
    )

    resp = httpx.post(
        f"{CC_URL}/api/v0/voice/command",
        headers=_node_headers(),
        json={"voice_command": "set a timer for 5 minutes", "conversation_id": conv_id},
        timeout=60.0,
    )
    assert resp.status_code in (200, 202), (
        f"/voice/command failed: {resp.status_code} body={resp.text[:400]}"
    )
    body = resp.json()
    tool_calls = body.get("tool_calls") or []
    assert tool_calls, (
        f"expected a tool_call routed through the from-source proxy, got none "
        f"(stop_reason={body.get('stop_reason')!r}, "
        f"assistant_message={body.get('assistant_message')!r})"
    )
    fn = tool_calls[0].get("function", {})
    assert fn.get("name") == "set_timer", (
        f"'set a timer for 5 minutes' routed to {fn.get('name')!r}, expected set_timer"
    )
    raw = fn.get("arguments")
    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    assert float(args.get("duration_seconds", 0)) == 300.0, (
        f"expected duration_seconds=300, got {args.get('duration_seconds')!r}"
    )
