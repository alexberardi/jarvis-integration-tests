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

    LLM_PROXY_URL          -> CASE-301  (real proxy reachable directly)
    LLM_PROXY_FROM_SOURCE  -> CASE-302  (CC -> real proxy, MOCK backend)
    TTS_FROM_SOURCE        -> CASE-311  (CC -> real Piper TTS, real audio)
    WHISPER_FROM_SOURCE    -> CASE-321  (CC -> real whisper, real transcribe)

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
LLM_PROXY_FROM_SOURCE = os.environ.get("LLM_PROXY_FROM_SOURCE", "")
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
# CASE-302 — CC routes a voice command through the real proxy (MOCK backend).
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not CC_URL, reason=SKIP_NO_STACK)
@pytest.mark.skipif(not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE)
@pytest.mark.skipif(not LLM_PROXY_FROM_SOURCE, reason="LLM_PROXY_FROM_SOURCE unset — llm-proxy not built from source")
@pytest.mark.qa_case("CASE-302")
def test_cc_voice_command_through_real_proxy_mock_backend():
    """CC -> real llm-proxy (MOCK) -> back into VoiceCommandResponse.

    The MOCK backend echoes the prompt as plain text with tool_calls=None
    (backends/mock_backend.py), so CC's text parser falls back to a `complete`
    response whose assistant_message is the MOCK echo ("[mock-text:...] ...").
    Asserting that the echo flowed back proves the WHOLE chain end-to-end
    through real code: CC -> real proxy API /v1/chat/completions -> model
    service /internal/model/chat -> MOCK -> OpenAI-shaped response -> CC parse.
    (Tool routing is the behavior lane's job; MOCK can't route.)
    """
    conv_id = "ci-fs-302"
    _start_conversation(conv_id)
    resp = httpx.post(
        f"{CC_URL}/api/v0/voice/command",
        headers=_node_headers(),
        json={"voice_command": "hello there jarvis", "conversation_id": conv_id},
        timeout=60.0,
    )
    assert resp.status_code in (200, 202), (
        f"/voice/command failed: {resp.status_code} body={resp.text[:400]}"
    )
    body = resp.json()
    assert body.get("stop_reason") == "complete", (
        f"expected stop_reason=complete (MOCK returns plain text, no tools), "
        f"got body={body}"
    )
    assistant_message = (body.get("assistant_message") or "").lower()
    assert "mock" in assistant_message, (
        f"expected the MOCK backend echo in assistant_message (proves CC reached "
        f"the real proxy + model service), got {body.get('assistant_message')!r}"
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
