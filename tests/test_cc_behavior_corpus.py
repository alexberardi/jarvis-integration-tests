"""Behavior lane (T6b): route a voice-command corpus through command-center's
REAL ChatGPTOpenAI provider + full stack against a real cheap cloud model.

This is the convergence of the testing fix and the unit-of-work fix. The PR fast
lane (test_cc_real_smoke.py) proves WIRING against a fake LLM — that CC parses a
canned tool_call shape correctly. THIS lane proves BEHAVIOR: that a genuinely
capable model, handed CC's REAL tool schemas through CC's native tool-calling
path, routes real voice utterances to the correct tool with sensible arguments —
the "wrong tool / answers the literal question" class of regression a human would
otherwise only catch by talking to a device.

Pipeline exercised end-to-end (the first time CC's native path runs in CI):

    pytest client (this file, the "node")
      → POST /api/v0/conversation/start  (client_tools = tools.cc.yaml)
      → POST /api/v0/voice/command       (the utterance)
      → CC ChatGPTOpenAI provider (supports_native_tools=True)
      → llm-proxy API :7704 /v1/chat/completions (model="live")
      → llm-proxy model service :7705 (REST backend)
      → OpenAI gpt-4.1-nano  (native tools + tool_choice="auto")
      → structured tool_calls flow all the way back into VoiceCommandResponse

Gated: skipped unless the behavior stack is up (CC_URL + seeded node creds set by
the behavior-corpus workflow). It is NOT part of the PR fast suite — the runner
invokes test_loop_smoke.py + test_cc_real_smoke.py explicitly, never the whole
dir, so this file only runs when the behavior workflow names it.

The model is PINNED (gpt-4.1-nano-2025-04-14) on the llm-proxy side for
reproducibility; temperature defaults to CC's inference config. Assertions check
tool *selection* and *argument shape*, never exact prose.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - guarded by the load test below
    yaml = None

CC_URL = os.environ.get("CC_URL")
CC_NODE_ID = os.environ.get("CC_NODE_ID", "")
CC_NODE_KEY = os.environ.get("CC_NODE_KEY", "")

SKIP_NO_STACK = "CC_URL unset — behavior stack not up (PR fast-lane / no-stack mode)"
SKIP_NO_NODE = "CC_NODE_ID / CC_NODE_KEY unset — node seed did not run"

_BEHAVIOR_DIR = Path(__file__).parent / "behavior"


def _load_yaml(name: str) -> list:
    if yaml is None:
        return []
    with (_BEHAVIOR_DIR / name).open() as fh:
        return yaml.safe_load(fh) or []


TOOLS = _load_yaml("tools.cc.yaml")
CORPUS = _load_yaml("corpus.cc.yaml")


# --------------------------------------------------------------------------- #
# Matcher engine — ported verbatim from
# jarvis-llm-proxy-api/tests/manual/test_behavior_tool_routing.py so the two
# behavior lanes share identical assertion semantics. Array args (items/tasks)
# are matched against str(value), which the matchers already str()-coerce.
# --------------------------------------------------------------------------- #
def _as_number(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _check_arg(name: str, matcher: dict, args: dict) -> str | None:
    """Return an error message if the arg fails its matcher, else None."""
    if name not in args:
        return f"arg {name!r} missing (got {sorted(args)})"
    value = args[name]
    sval = str(value).strip().lower()

    if "equals" in matcher:
        expected = matcher["equals"]
        num_v, num_e = _as_number(value), _as_number(expected)
        if num_v is not None and num_e is not None:
            if num_v != num_e:
                return f"{name}={value!r} != equals {expected!r}"
        elif sval != str(expected).strip().lower():
            return f"{name}={value!r} != equals {expected!r}"
    if "contains" in matcher:
        if str(matcher["contains"]).strip().lower() not in sval:
            return f"{name}={value!r} does not contain {matcher['contains']!r}"
    if "in" in matcher:
        opts = [str(o).strip().lower() for o in matcher["in"]]
        if sval not in opts:
            return f"{name}={value!r} not in {matcher['in']!r}"
    if "any_of" in matcher:
        opts = [str(o).strip().lower() for o in matcher["any_of"]]
        if not any(o in sval for o in opts):
            return f"{name}={value!r} contains none of {matcher['any_of']!r}"
    return None


def _node_headers() -> dict:
    return {"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"}


def _parse_arguments(raw_args: object) -> dict:
    """Native tool_calls carry `function.arguments` as a JSON string (OpenAI
    shape); be tolerant of an already-parsed dict too."""
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return {}
    if isinstance(raw_args, dict):
        return raw_args
    return {}


def _route_through_cc(conv_id: str, utterance: str) -> tuple[str | None, dict, dict]:
    """Open a conversation seeded with CC's real tools, send one utterance, and
    return (chosen_tool_name_or_None, parsed_arguments, full_response_body)."""
    start = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers=_node_headers(),
        json={
            "conversation_id": conv_id,
            "client_tools": TOOLS,
            "available_commands": [],
            # Skip the warmup inference round-trip: the corpus only needs the
            # /voice/command inference, and skipping halves the per-utterance
            # model cost without affecting which tools are registered.
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
        json={"voice_command": utterance, "conversation_id": conv_id},
        timeout=60.0,
    )
    # Blocking /voice/command returns 200 (complete) or 202 (tool_calls) — both
    # carry a VoiceCommandResponse body; route on the body, not the status code.
    assert resp.status_code in (200, 202), (
        f"/voice/command failed: {resp.status_code} body={resp.text[:400]}"
    )
    body = resp.json()
    tool_calls = body.get("tool_calls") or []
    if not tool_calls:
        return None, {}, body

    fn = tool_calls[0].get("function", {})
    return fn.get("name"), _parse_arguments(fn.get("arguments")), body


@pytest.mark.skipif(not CC_URL, reason=SKIP_NO_STACK)
@pytest.mark.skipif(not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE)
@pytest.mark.behavior
def test_corpus_and_tools_loaded() -> None:
    # In the behavior workflow pyyaml is installed; an empty load here is a real
    # problem, not a silently-empty parametrization.
    assert TOOLS, "tools.cc.yaml failed to load (is pyyaml installed?)"
    assert CORPUS, "corpus.cc.yaml failed to load (is pyyaml installed?)"
    tool_names = {t["function"]["name"] for t in TOOLS}
    for entry in CORPUS:
        expected = entry.get("tool")
        assert expected is None or expected in tool_names, (
            f"corpus references tool {expected!r} not present in tools.cc.yaml "
            f"(have {sorted(tool_names)})"
        )


@pytest.mark.skipif(not CC_URL, reason=SKIP_NO_STACK)
@pytest.mark.skipif(not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE)
@pytest.mark.behavior
@pytest.mark.parametrize(
    "idx, entry",
    list(enumerate(CORPUS)),
    ids=[e["utterance"] for e in CORPUS],
)
def test_utterance_routes_through_cc(idx: int, entry: dict) -> None:
    utterance = entry["utterance"]
    expected_tool = entry.get("tool")
    conv_id = f"ci-behavior-{idx:03d}"

    chosen, args, body = _route_through_cc(conv_id, utterance)
    stop_reason = body.get("stop_reason")

    if expected_tool is None:
        # Small talk: CC must answer directly (complete) or decline (not_for_me),
        # never fire a tool. stop_reason==tool_calls or any tool call is a fail.
        assert chosen is None, (
            f"{utterance!r}: expected NO tool call, but CC routed to {chosen!r} "
            f"args={args!r} (stop_reason={stop_reason!r})"
        )
        assert stop_reason != "tool_calls", (
            f"{utterance!r}: expected a non-tool stop_reason, got "
            f"{stop_reason!r} body={body}"
        )
        return

    assert chosen is not None, (
        f"{utterance!r}: no tool call (stop_reason={stop_reason!r}, "
        f"assistant_message={body.get('assistant_message')!r}, "
        f"commands={body.get('commands')!r})"
    )
    assert chosen == expected_tool, (
        f"{utterance!r} routed to {chosen!r}, expected {expected_tool!r}"
    )
    for arg_name, matcher in (entry.get("args") or {}).items():
        err = _check_arg(arg_name, matcher, args)
        assert err is None, f"{utterance!r} -> {chosen}: {err}"
