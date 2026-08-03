"""
media_client.py -- shared PC-side pieces for talking to the Pi's
lego_pi/media_relay.py WebSocket: the wire-format constants, the VAD
speech segmenter, and a MediaClient class that keeps a persistent,
auto-reconnecting connection open and holds the latest frame/utterance
in memory.

Two things import this, for two different purposes:
    scripts/pc_media_client.py -- manual debug tool. Writes what it
        receives to disk (debug_media/) so you can eyeball a frame or
        listen to an utterance and confirm the pipeline is healthy.
    lego_brain/pc_server.py -- the real server. Wraps the SAME demux +
        segmenter logic, but keeps the latest frame/utterance as plain
        in-memory attributes instead of writing files, so GET /frame
        and GET /audio can answer instantly without touching the
        network or disk on every request -- see MediaClient below.

Wire format constants here MUST match lego_pi/media_relay.py. Same
PC/Pi deployment boundary as everywhere else in this codebase (see
deploy.py's docstring on why lego_pi ships flattened, no src/ wrapper):
the Pi side and PC side are two different deployment targets, so there's
no single shared import across that gap -- keep these in sync by hand
if media_relay.py's framing ever changes. Unlike that boundary, though,
this module and pc_media_client.py/pc_server.py all run on the SAME
machine (the PC), so there's no excuse for duplicating this logic a
second time between them -- that duplication is what this module
removes.
"""

import asyncio
import io
import struct
import wave
from collections import deque
from typing import Callable, Optional

import webrtcvad
import websockets

TYPE_VIDEO = 0x01
TYPE_AUDIO = 0x02

AUDIO_SAMPLE_RATE = 16000
AUDIO_FRAME_MS = 30
AUDIO_FRAME_BYTES = int(AUDIO_SAMPLE_RATE * AUDIO_FRAME_MS / 1000) * 2  # int16

PREROLL_FRAMES = 10   # ~300ms kept before a trigger, in case speech starts there
HANGOVER_FRAMES = 20  # ~600ms of continued silence tolerated before closing out
VAD_AGGRESSIVENESS = 2  # 0 (permissive) - 3 (strict); 2 is a reasonable default


class Segmenter:
    """Turns a stream of 30ms PCM16 frames into complete utterances,
    using a pre-roll buffer (don't miss the start) and hangover (don't
    cut the end). emit_fn(pcm_bytes) is called once per completed
    utterance.
    """

    def __init__(self, emit_fn: Callable[[bytes], None]):
        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self._preroll = deque(maxlen=PREROLL_FRAMES)
        self._triggered = False
        self._voiced = []
        self._silence_run = 0
        self._emit = emit_fn

    def process(self, frame: bytes):
        is_speech = self._vad.is_speech(frame, AUDIO_SAMPLE_RATE)

        if not self._triggered:
            self._preroll.append(frame)
            if is_speech:
                self._triggered = True
                self._voiced = list(self._preroll)  # recover the onset
                self._preroll.clear()
                self._silence_run = 0
            return

        self._voiced.append(frame)
        if is_speech:
            self._silence_run = 0
        else:
            self._silence_run += 1
            if self._silence_run >= HANGOVER_FRAMES:
                self._emit(b"".join(self._voiced))
                self._triggered = False
                self._voiced = []
                self._silence_run = 0

    def flush(self):
        """Call on disconnect so an in-progress utterance isn't lost."""
        if self._triggered and self._voiced:
            self._emit(b"".join(self._voiced))
            self._triggered = False
            self._voiced = []


def pcm_to_wav_bytes(pcm_bytes: bytes) -> bytes:
    """In-memory PCM16 mono -> WAV container, no disk touched. Used by
    MediaClient so /audio can hand back a file-like blob a caller can
    play/save directly, without pc_server.py needing to know anything
    about WAV framing itself."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(AUDIO_SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


class MediaClient:
    """Persistent, auto-reconnecting connection to the Pi's media
    WebSocket, run as a background asyncio task (see run_forever()).

    latest_frame and latest_utterance_wav are plain attributes any
    number of request handlers can read directly -- they're updated by
    the background task as data arrives, never fetched fresh on demand.
    That's the whole point: a GET /frame handler that awaited a fresh
    WebSocket round-trip per request would block on network I/O and
    contend with every other in-flight request for the same connection.
    Reading an attribute never blocks.
    """

    def __init__(self, uri: str, reconnect_delay: float = 3.0):
        self.uri = uri
        self.reconnect_delay = reconnect_delay
        self.latest_frame: Optional[bytes] = None
        self.latest_utterance_wav: Optional[bytes] = None
        self._segmenter = Segmenter(self._on_utterance)
        self._audio_carry = b""

    def _on_utterance(self, pcm_bytes: bytes):
        self.latest_utterance_wav = pcm_to_wav_bytes(pcm_bytes)

    async def run_forever(self):
        """Never returns (until cancelled) -- reconnects on any drop
        instead of leaving latest_frame/latest_utterance_wav stale
        forever after one network blip (Pi restart, WiFi hiccup, etc).
        """
        while True:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[media_client] connection lost ({e}), reconnecting in {self.reconnect_delay}s...")
            await asyncio.sleep(self.reconnect_delay)

    async def _connect_once(self):
        async with websockets.connect(self.uri, max_size=None) as ws:
            print(f"[media_client] connected to {self.uri}")
            async for message in ws:
                msg_type, _ts = struct.unpack(">Bd", message[:9])
                payload = message[9:]

                if msg_type == TYPE_VIDEO:
                    self.latest_frame = payload

                elif msg_type == TYPE_AUDIO:
                    self._audio_carry += payload
                    while len(self._audio_carry) >= AUDIO_FRAME_BYTES:
                        frame = self._audio_carry[:AUDIO_FRAME_BYTES]
                        self._audio_carry = self._audio_carry[AUDIO_FRAME_BYTES:]
                        self._segmenter.process(frame)
