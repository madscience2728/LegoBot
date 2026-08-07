"""
senses.py -- pulls the robot's current sight and hearing from
gui/server.py's /api/senses/latest endpoint, and packages both into
the shapes the model call actually needs (a JPEG data URI for vision,
a WAV data URI for audio).

Deliberately does NOT talk to the phone directly. gui/server.py
already owns the live WebSocket connection to the phone's media relay
(media_relay_loop, patched to also buffer audio -- see its own
comments) and already does the JPEG/PCM decoding. This module's only
job is turning that already-decoded snapshot into request-ready data,
so there's exactly one thing in the whole project holding the phone
WebSocket open, not two competing connections.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

AUDIO_SAMPLE_RATE = 16000  # must match MicStream.kt's SAMPLE_RATE


@dataclass(frozen=True)
class SensesSnapshot:
    media_connected: bool
    jpeg_bytes: bytes | None
    video_ts: float | None
    wav_bytes: bytes | None
    audio_seconds: float

    @property
    def has_vision(self) -> bool:
        return self.jpeg_bytes is not None

    @property
    def has_audio(self) -> bool:
        return self.wav_bytes is not None

    def image_data_uri(self) -> str | None:
        if self.jpeg_bytes is None:
            return None
        b64 = base64.b64encode(self.jpeg_bytes).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"

    def audio_data_uri(self) -> str | None:
        if self.wav_bytes is None:
            return None
        b64 = base64.b64encode(self.wav_bytes).decode("ascii")
        return f"data:audio/wav;base64,{b64}"


def _decode_or_none(b64_str: str | None) -> bytes | None:
    return base64.b64decode(b64_str) if b64_str else None


async def fetch_latest_senses(gui_base_url: str, client: httpx.AsyncClient | None = None) -> SensesSnapshot:
    """Fetches one snapshot of current sight/hearing from gui/server.py.

    Args:
        gui_base_url: e.g. "http://127.0.0.1:8000" -- wherever
            gui/server.py's FastAPI app is running.
        client: optional pre-built httpx.AsyncClient to reuse across
            calls (avoids reconnect overhead in a tight tick loop);
            a short-lived one is created and closed if omitted.
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)

    try:
        response = await client.get(f"{gui_base_url}/api/senses/latest")
        response.raise_for_status()
        data = response.json()
    finally:
        if owns_client:
            await client.aclose()

    jpeg_bytes = _decode_or_none(data.get("video_b64"))

    # gui/server.py's /api/senses/latest now does VAD-based utterance
    # segmentation server-side (see gui/segmenter.py, ported from the
    # old Pi project) and hands back an already-complete WAV, or None
    # if nothing new was actually said since the last poll. No more
    # raw-PCM re-wrapping needed here -- that's the whole point of
    # moving segmentation upstream: silence never reaches this client
    # at all, real speech arrives pre-packaged.
    wav_bytes = _decode_or_none(data.get("audio_wav_b64"))

    return SensesSnapshot(
        media_connected=bool(data.get("media_connected")),
        jpeg_bytes=jpeg_bytes,
        video_ts=data.get("video_ts"),
        wav_bytes=wav_bytes,
        audio_seconds=float(data.get("audio_seconds", 0.0)),
    )
