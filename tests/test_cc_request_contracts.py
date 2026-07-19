"""Request-contract edges CC enforces on AUTHENTICATED node calls — the 4xx
paths the happy-path real-stack suite silently relies on but never asserts.

The real-stack suite (test_cc_real_smoke.py, CASE-101..215) is happy-path: it
proves that a *correctly sequenced, well-formed* request succeeds. The
negative-auth suite (test_cc_auth_contracts.py, CASE-216..223) proves that
*bad/missing credentials* are rejected (401). Neither covers the layer in
between: a request that IS authenticated with valid node credentials but is
out-of-order (a conversation that was never started) or malformed (a body
missing a required field). Those are real API contracts CC implements in
handler code, and a regression on any of them fails open silently — the
happy-path suite keeps passing because it always sequences correctly and always
sends complete bodies.

Two distinct contracts are pinned here, both reachable with the seeded node
credentials the integration-runner already exports (CC_NODE_ID/CC_NODE_KEY):

1. **Conversation precondition (business-rule 400).** `/voice/command` and
   `/voice/command/stream` both refuse to run the tool pipeline for a
   conversation_id that was never initialised via `/conversation/start`
   (`conversation_cache.get_tools(...) is None` -> 400 "Conversation not
   initialized for tool-based flow"). CASE-204 (the happy path) documents
   working *around* this by starting a conversation with `client_tools: []`
   first; nothing asserts the guard itself. If it regressed, the pipeline would
   run against uninitialised state and surface a 500 (or worse, partial
   behaviour) instead of a clean client-correctable 400.

2. **Structured validation envelope (400, not FastAPI's default 422).** CC
   installs a custom `RequestValidationError` handler (main.py:108-124) that
   converts pydantic body-validation failures into a **400** with a
   `{"error": "validation_error", "message": ..., "details": [...]}` envelope —
   deliberately NOT the framework-default 422. Every node/mobile client keys on
   that shape. A regression that dropped the handler would flip the status to
   422 and change the body shape, breaking clients, while every happy-path case
   (which never sends a malformed body) stayed green.

Verified against jarvis-command-center source at authoring time:
  * app/main.py:719-721   /voice/command         tools is None -> 400
  * app/main.py:882-887   /voice/command/stream  tools is None -> 400
  * app/main.py:108-124   custom RequestValidationError handler -> 400 envelope
  * app/request_models/conversation_start_request.py  conversation_id required

Gated on CC_URL + CC_NODE_ID/CC_NODE_KEY so the file is a clean no-op skip in
the fakes-only lanes and local runs (mirrors test_cc_real_smoke.py's gates).
Each conversation_id is unique and never started, so it can never collide with
a conversation another case warmed in the same run.
"""

from __future__ import annotations

import os

import httpx
import pytest

CC_URL = os.environ.get("CC_URL")
CC_NODE_ID = os.environ.get("CC_NODE_ID", "")
CC_NODE_KEY = os.environ.get("CC_NODE_KEY", "")

SKIP_REASON = "CC_URL unset — skipping real-stack request-contract tests (no stack)"
SKIP_NO_NODE = "CC_NODE_ID / CC_NODE_KEY unset — node seed did not run"

# Valid node X-API-Key (node_id:node_key) — the same format CASE-203 proves CC
# accepts end-to-end. Auth passes, so the ONLY thing under test is CC's
# in-handler request validation, not the credential check.
_NODE_KEY_HEADER = f"{CC_NODE_ID}:{CC_NODE_KEY}"


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE)
@pytest.mark.qa_case("CASE-224")
def test_cc_voice_stream_rejects_unstarted_conversation_with_400():
    """CC /voice/command/stream on a never-started conversation -> 400.

    POSTs a complete, well-formed VoiceCommandRequest (voice_command +
    conversation_id both present) with valid node credentials, but for a
    conversation_id that was never passed to /conversation/start. CC's
    handler looks up `conversation_cache.get_tools(...)`, gets None, and raises
    400 "Conversation not initialized for tool-based flow" (main.py:882-887)
    BEFORE invoking the model pipeline. The happy-path CASE-204 sidesteps this
    by starting the conversation first; this pins the guard. A regression that
    dropped it would push uninitialised state into the LLM/tool loop and turn a
    clean 400 into a 500, breaking every node that reconnects mid-conversation.
    """
    resp = httpx.post(
        f"{CC_URL}/api/v0/voice/command/stream",
        headers={"X-API-Key": _NODE_KEY_HEADER},
        json={
            "voice_command": "set a 5 minute timer",
            "conversation_id": "ci-req-224-never-started",
        },
        timeout=15.0,
    )
    assert resp.status_code == 400, (
        f"expected 400 for an un-started conversation on /voice/command/stream, "
        f"got {resp.status_code} body={resp.text[:300]} — CC's "
        f"conversation-precondition guard may be failing open into the pipeline."
    )
    assert "not initialized" in resp.text.lower(), (
        f"expected the 'Conversation not initialized' precondition detail, got "
        f"body={resp.text[:300]} — the 400 may be coming from a different check."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE)
@pytest.mark.qa_case("CASE-225")
def test_cc_voice_command_unstarted_conversation_returns_422():
    """CC /voice/command (blocking) on a never-started conversation -> HTTP 422.

    Contract updated 2026-07-18 for jarvis-command-center d113c6d ("surface
    blocking-endpoint precondition failures as 422", per the QA plan on
    jarvis-roadmap#51): whole-request precondition failures now propagate as an
    HTTP 422 with FastAPI's `{"detail": ...}` shape instead of being swallowed
    into the 200 batch envelope. The historical contract (200 +
    `commands[0].success == False`) is retired; per-command errors in the 200
    body remain the shape for failures of individual commands *within* a
    started conversation. The streaming twin still returns 400 for the same
    precondition (CASE-224) — the divergence is now 422-vs-400 rather than
    200-vs-400. Node impact verified: the node's RestClient raise_for_status()
    treats any 4xx identically (log + None + retry via /conversation/start).
    """
    resp = httpx.post(
        f"{CC_URL}/api/v0/voice/command",
        headers={"X-API-Key": _NODE_KEY_HEADER},
        json={
            "voice_command": "set a 5 minute timer",
            "conversation_id": "ci-req-225-never-started",
        },
        timeout=15.0,
    )
    assert resp.status_code == 422, (
        f"expected 422 for a whole-request precondition failure (CC d113c6d) — "
        f"got {resp.status_code} body={resp.text[:300]}"
    )
    detail = resp.json().get("detail", "")
    assert "not initialized" in str(detail).lower(), (
        f"expected the 'Conversation not initialized' precondition detail, got "
        f"detail={detail!r}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE)
@pytest.mark.qa_case("CASE-226")
def test_cc_returns_structured_400_envelope_on_malformed_body():
    """CC returns its custom 400 `validation_error` envelope on a malformed body.

    POSTs /conversation/start with valid node credentials but a body missing the
    required `conversation_id` field. Rather than FastAPI's default 422, CC's
    app-level RequestValidationError handler (main.py:108-124) returns a **400**
    with `{"error": "validation_error", "message": ..., "details": [...]}`. That
    envelope is a deliberate, client-facing contract (node/mobile clients render
    `details`); a regression removing the handler would silently revert to a 422
    with the default `{"detail": [...]}` shape and break those clients. No
    happy-path case sends a malformed body, so this is the only guard on the
    envelope. Asserts the 400 status AND the `error == "validation_error"` +
    non-empty `details` shape, isolating the override from a generic 400.
    """
    resp = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers={"X-API-Key": _NODE_KEY_HEADER},
        json={"node_context": {"room": "ci-room"}},  # missing required conversation_id
        timeout=15.0,
    )
    assert resp.status_code == 400, (
        f"expected CC's custom 400 validation envelope for a malformed body, got "
        f"{resp.status_code} body={resp.text[:300]} — the RequestValidationError "
        f"handler may have regressed to FastAPI's default 422."
    )
    body = resp.json()
    assert body.get("error") == "validation_error", (
        f"expected error=='validation_error' in the envelope, got body={body} — "
        f"the custom validation handler's shape may have changed."
    )
    assert isinstance(body.get("details"), list) and body["details"], (
        f"expected a non-empty 'details' list naming the bad field(s), got "
        f"body={body} — the envelope may no longer surface field-level errors."
    )
