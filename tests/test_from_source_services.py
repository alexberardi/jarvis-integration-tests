"""T9 — real feature round-trips for the FROM-SOURCE service lanes.

The PR fast lane (test_cc_real_smoke.py) runs the hot-path stack with the LLM,
whisper, and TTS all faked. These cases instead exercise ONE of those services
built from a PR's real source, wired into the real cross-service stack, so a
PR in jarvis-llm-proxy-api / jarvis-whisper-api / jarvis-tts gets a genuine
cross-service signal instead of fakes-only smoke.

Each case is gated on a per-service env flag that the from-source workflow
(.github/workflows/from-source-services.yml) sets only for the lane it's
running, so the file is a clean no-op skip everywhere else (PR fast lane, local
runs, the behavior lane):

    LLM_PROXY_URL          -> CASE-301  (real proxy /health reaches model svc)
    LLM_PROXY_URL + app key-> CASE-302  (real proxy /v1/chat/completions, MOCK)
    LLM_PROXY_URL          -> CASE-303  (real proxy /v1/chat REJECTS a wrong app key)
    LLM_PROXY_URL          -> CASE-304  (real proxy /v1/chat REJECTS missing app headers)
    TTS_FROM_SOURCE        -> CASE-311  (CC -> real Piper TTS, real audio)
    WHISPER_FROM_SOURCE    -> CASE-321  (CC -> real whisper, real transcribe)

Note on the llm-proxy lane: it validates the proxy's OWN contract directly, not
CC -> proxy routing. CC's voice flow sends response_format=json_object, which the
proxy enforces (chat_runner: requires_json) — a JSON-returning model is required,
so the MOCK backend can only be exercised on a plain (non-json) chat request hit
directly. CC -> real-model routing is the behavior lane's job (real cloud model).

The 3xx CASEs that route through CC reuse the same seeded node creds + URL env
as test_cc_real_smoke.py (CC_URL, CC_NODE_ID, CC_NODE_KEY).

Assertions are deliberately CONTRACT/SHAPE-level, not content-level:
  * MOCK echoes a fixed string (no real routing) -> assert the chain delivered
    SOME assistant text, not a specific tool.
  * Real Piper output is non-deterministic audio -> assert "lots of real PCM"
    (>> the fake's 32 zero-bytes), not exact samples.
  * Real whisper on a generated clip may transcribe to empty text -> assert the
    response SHAPE (text str + segments list + speaker), which proves the model
    loaded, decoded the WAV, and returned the contract — without a flaky
    exact-transcript match.
"""

from __future__ import annotations

import io
import os
import wave

import httpx
import pytest

CC_URL = os.environ.get("CC_URL")
CC_NODE_ID = os.environ.get("CC_NODE_ID", "")
CC_NODE_KEY = os.environ.get("CC_NODE_KEY", "")

LLM_PROXY_URL = os.environ.get("LLM_PROXY_URL", "")
# The proxy's /v1/chat/completions is app-auth'd; CASE-302 reuses CC's seeded
# app credentials (the from-source workflow passes them through).
LLM_PROXY_APP_ID = os.environ.get("LLM_PROXY_APP_ID", "command-center")
LLM_PROXY_APP_KEY = os.environ.get("LLM_PROXY_APP_KEY", "")
TTS_FROM_SOURCE = os.environ.get("TTS_FROM_SOURCE", "")
WHISPER_FROM_SOURCE = os.environ.get("WHISPER_FROM_SOURCE", "")

SKIP_NO_STACK = "CC_URL unset — real-stack from-source cases skipped"
SKIP_NO_NODE = "CC_NODE_ID / CC_NODE_KEY unset — node seed did not run"


def _node_headers() -> dict[str, str]:
    return {"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"}


def _start_conversation(conv_id: str) -> None:
    """Open a CC conversation with no tools (we're testing the service hop, not
    routing) and skip the warmup inference round-trip."""
    resp = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers=_node_headers(),
        json={
            "conversation_id": conv_id,
            "client_tools": [],
            "available_commands": [],
            "skip_warmup_inference": True,
        },
        timeout=30.0,
    )
    assert resp.status_code == 200, (
        f"/conversation/start failed: {resp.status_code} body={resp.text[:300]}"
    )


def _silent_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """A valid 16 kHz mono 16-bit PCM WAV of near-silence.

    whisper.cpp parses the WAV header and resamples internally, so a real (but
    empty-of-speech) clip exercises the full decode path and returns a
    well-formed {text, segments, speaker} body — text is typically empty, which
    is exactly why CASE-321 asserts shape, not transcript.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# CASE-301 — real llm-proxy is up and its API server reaches the model service.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not LLM_PROXY_URL, reason="LLM_PROXY_URL unset — llm-proxy not built from source")
@pytest.mark.qa_case("CASE-301")
def test_real_llm_proxy_health_reaches_model_service():
    """GET /health on the from-source proxy API.

    The API server's /health proxies to the model service over X-Internal-Token
    (api/health_routes.py:_get_health_status). A `status: healthy` response
    therefore proves more than "the API booted": it proves the real API ->
    real model service internal hop (URL + token) works end-to-end through the
    PR's code — the contract every chat/embeddings call depends on.
    """
    resp = httpx.get(f"{LLM_PROXY_URL.rstrip('/')}/health", timeout=15.0)
    assert resp.status_code == 200, (
        f"expected 200, got {resp.status_code} body={resp.text[:300]}"
    )
    body = resp.json()
    assert body.get("status") == "healthy", (
        f"expected status=healthy (API reached the model service), got {body}"
    )


# --------------------------------------------------------------------------- #
# CASE-302 — real proxy /v1/chat/completions OpenAI contract (MOCK backend).
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not LLM_PROXY_URL, reason="LLM_PROXY_URL unset — llm-proxy not built from source")
@pytest.mark.skipif(not LLM_PROXY_APP_KEY, reason="LLM_PROXY_APP_KEY unset — seed app key not passed")
# T10: the cross-repo lane's key-gated ROUTING variant runs the proxy on the REST
# backend (-> a real cloud model), where this case's "mock" echo assertion can't
# hold. No-op for the T9 from-source lane, which never sets CROSS_REPO_ROUTING.
@pytest.mark.skipif(
    bool(os.environ.get("CROSS_REPO_ROUTING")),
    reason="cross-repo routing mode uses the REST backend; the MOCK-echo assertion is N/A",
)
@pytest.mark.qa_case("CASE-302")
def test_real_proxy_chat_completions_contract_mock_backend():
    """POST /v1/chat/completions on the from-source proxy (app-auth'd).

    Validates the proxy's OpenAI contract end-to-end through real code on the
    MOCK backend: API server -> X-Internal-Token -> model service
    /internal/model/chat -> MOCK -> OpenAI-shaped response -> back. MOCK echoes
    the prompt as plain text ("[mock-text:...] ..."), so asserting the echo lands
    in choices[0].message.content proves the whole proxy chain + app-auth work.

    Hit DIRECTLY (not through CC): CC's voice flow sends
    response_format=json_object, which the proxy enforces (chat_runner:
    requires_json) and MOCK can't satisfy — that CC->real-model path is the
    behavior lane's job. A plain chat request (no response_format) is the right
    contract probe for a model-less proxy build.
    """
    resp = httpx.post(
        f"{LLM_PROXY_URL.rstrip('/')}/v1/chat/completions",
        headers={
            "X-Jarvis-App-Id": LLM_PROXY_APP_ID,
            "X-Jarvis-App-Key": LLM_PROXY_APP_KEY,
        },
        json={
            "model": "live",
            "messages": [{"role": "user", "content": "hello from ci"}],
        },
        timeout=60.0,
    )
    assert resp.status_code == 200, (
        f"expected 200 from /v1/chat/completions, got {resp.status_code} "
        f"body={resp.text[:400]}"
    )
    body = resp.json()
    choices = body.get("choices") or []
    assert choices, f"expected OpenAI choices[], got body={body}"
    content = (choices[0].get("message", {}).get("content") or "").lower()
    assert "mock" in content, (
        f"expected the MOCK backend echo in choices[0].message.content (proves "
        f"the real API -> model service -> MOCK chain), got {content!r}"
    )


# --------------------------------------------------------------------------- #
# CASE-303 — real proxy /v1/chat/completions REJECTS a wrong app key (401).
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not LLM_PROXY_URL, reason="LLM_PROXY_URL unset — llm-proxy not built from source")
@pytest.mark.qa_case("CASE-303")
def test_real_proxy_chat_completions_rejects_wrong_app_key():
    """POST /v1/chat/completions with a WRONG X-Jarvis-App-Key -> 401.

    The proxy gates /v1/chat/completions with require_app_auth (auth/app_auth.py),
    which forwards the app headers to jarvis-auth /internal/app-ping; a non-200
    there becomes a 401 "Invalid app credentials". CASE-302 only ever sends the
    VALID seeded key (the accept side). This is the reject side: the proxy is the
    app-to-app gateway in front of the model service, so if this gate failed open
    any caller presenting arbitrary app headers could spend model compute or read
    completions. Mirror of CASE-219 (auth's validate-node wrong-app-key) one layer
    out, at the proxy. A well-formed chat body (model + messages, as CASE-302
    sends) isolates the failure to the credential check, not body validation.
    """
    resp = httpx.post(
        f"{LLM_PROXY_URL.rstrip('/')}/v1/chat/completions",
        headers={
            "X-Jarvis-App-Id": LLM_PROXY_APP_ID,
            "X-Jarvis-App-Key": "ci-wrong-app-key",
        },
        json={
            "model": "live",
            "messages": [{"role": "user", "content": "hello from ci"}],
        },
        timeout=30.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 for a wrong app key on /v1/chat/completions, got "
        f"{resp.status_code} body={resp.text[:400]} — the proxy's app-to-app gate "
        f"on the chat endpoint may be failing open."
    )


# --------------------------------------------------------------------------- #
# CASE-304 — real proxy /v1/chat/completions REJECTS missing app headers (401).
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not LLM_PROXY_URL, reason="LLM_PROXY_URL unset — llm-proxy not built from source")
@pytest.mark.qa_case("CASE-304")
def test_real_proxy_chat_completions_rejects_missing_app_headers():
    """POST /v1/chat/completions with NO app headers -> 401.

    The missing-credential branch of require_app_auth (app_auth.py: no app id/key
    -> 401 "Missing app credentials") is a DISTINCT code path from CASE-303's
    wrong-key branch and is the more common real regression: a refactor that
    validates a key when present but forgets the presence check, or a header-name
    typo, would leave the proxy's chat endpoint open to anonymous callers. Sending
    a valid body with no app headers pins that the gateway refuses unauthenticated
    callers outright. Mirror of CASE-220 (auth's validate-node missing-headers) at
    the proxy layer.
    """
    resp = httpx.post(
        f"{LLM_PROXY_URL.rstrip('/')}/v1/chat/completions",
        json={
            "model": "live",
            "messages": [{"role": "user", "content": "hello from ci"}],
        },
        timeout=30.0,
    )
    assert resp.status_code == 401, (
        f"expected 401 for missing app headers on /v1/chat/completions, got "
        f"{resp.status_code} body={resp.text[:400]} — the proxy's chat endpoint "
        f"may be accepting anonymous callers."
    )


# --------------------------------------------------------------------------- #
# CASE-311 — CC streams a complete response through the real Piper TTS.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not CC_URL, reason=SKIP_NO_STACK)
@pytest.mark.skipif(not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE)
@pytest.mark.skipif(not TTS_FROM_SOURCE, reason="TTS_FROM_SOURCE unset — tts not built from source")
@pytest.mark.qa_case("CASE-311")
def test_cc_voice_stream_through_real_tts_produces_real_audio():
    """CC /voice/command/stream "hello jarvis" -> real Piper synthesis.

    Mirrors CASE-209 (the complete-text 200-audio path), but with the REAL tts
    container instead of the fake. The LLM + whisper stay faked: the fake LLM's
    canned "hello" reply ("Hello! How can I help?") drives CC's complete path,
    which streams the text to the real tts /speak/stream and forwards the PCM.

    The fake TTS yields 32 bytes of zero-PCM; real Piper synthesizing a full
    sentence yields tens of KB. Asserting > 1000 bytes (plus the audio/raw
    content-type and an X-Audio-Sample-Rate header) proves real synthesis ran
    end-to-end — without pinning a sample rate (Piper "low" voices are 16 kHz,
    not the fake's 22.05 kHz) or exact samples.
    """
    conv_id = "ci-fs-311"
    _start_conversation(conv_id)
    with httpx.stream(
        "POST",
        f"{CC_URL}/api/v0/voice/command/stream",
        headers=_node_headers(),
        json={"voice_command": "hello jarvis", "conversation_id": conv_id},
        timeout=60.0,
    ) as resp:
        assert resp.status_code == 200, (
            f"expected 200 audio (complete path), got {resp.status_code} "
            f"body={resp.read()[:400]!r}"
        )
        content_type = resp.headers.get("content-type", "")
        assert content_type.startswith("audio/raw"), (
            f"expected content-type=audio/raw, got {content_type!r} — if JSON, "
            f"the response landed in a non-audio branch (LLM didn't return a "
            f"plain complete reply)."
        )
        assert resp.headers.get("X-Audio-Sample-Rate"), (
            f"expected X-Audio-Sample-Rate header from the real tts, "
            f"headers={dict(resp.headers)}"
        )
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
    assert len(body) > 1000, (
        f"expected real Piper audio (tens of KB) but got {len(body)} bytes — "
        f"the fake TTS yields 32 zero-bytes, so a tiny body means CC is NOT "
        f"reaching the real tts container (check discovery/JARVIS_TTS_URL) or "
        f"Piper produced nothing."
    )


# --------------------------------------------------------------------------- #
# CASE-321 — CC proxies a real transcription through the real whisper.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not CC_URL, reason=SKIP_NO_STACK)
@pytest.mark.skipif(not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE)
@pytest.mark.skipif(not WHISPER_FROM_SOURCE, reason="WHISPER_FROM_SOURCE unset — whisper not built from source")
@pytest.mark.qa_case("CASE-321")
def test_cc_media_transcribe_through_real_whisper_returns_contract():
    """CC /media/whisper/transcribe -> real whisper -> {text, segments, speaker}.

    Mirrors CASE-208 (the media proxy), but against the REAL whisper container
    with its baked ggml-base.en model instead of the fake. We send a generated,
    valid 16 kHz WAV; the real service loads the model, decodes the audio, runs
    the speaker pass, and returns the real contract shape (app/main.py:273-277,
    which the fake's body omits `segments` from).

    Assertion is SHAPE-only: the generated clip is near-silence so the
    transcript is typically empty — asserting `text` is a str, `segments` is a
    list, and `speaker` is present proves the real model ran end-to-end without
    a flaky exact-transcript match. (Audio bytes are arbitrary here; CASE-208
    already covers the field-name/proxy wiring against the fake.)
    """
    files = {"file": ("ci_clip.wav", _silent_wav(), "audio/wav")}
    resp = httpx.post(
        f"{CC_URL}/api/v0/media/whisper/transcribe",
        headers=_node_headers(),
        files=files,
        timeout=60.0,
    )
    assert resp.status_code == 200, (
        f"expected 200, got {resp.status_code} body={resp.text[:400]}"
    )
    body = resp.json()
    assert isinstance(body.get("text"), str), (
        f"expected a string `text` field from real whisper, got body={body}"
    )
    assert isinstance(body.get("segments"), list), (
        f"expected a `segments` list (real whisper returns it; the fake omits "
        f"it), got body={body}"
    )
    assert "speaker" in body, (
        f"expected a `speaker` field in the response, got body={body}"
    )
