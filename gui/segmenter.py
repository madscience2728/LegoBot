"""
segmenter.py -- ported near-verbatim from the old Pi-based project's
src/lego_brain/media_client.py (see old.zip's Segmenter class). Turns
a stream of 30ms PCM16 audio frames into complete spoken utterances
using webrtcvad, so downstream consumers (the LLM tick loop) only ever
see real speech -- silence between utterances is never emitted at all.

This replaces the naive fixed-length rolling buffer (state.audio_ring)
that was built before this old project was found -- that version sent
whatever was in the last ~6 seconds regardless of whether anyone had
actually spoken. This version only fires emit_fn when webrtcvad
detects an actual utterance, complete with pre-roll (don't miss the
first syllable) and hangover (don't cut off the last one).

Kept intentionally close to the original tuning (same
VAD_AGGRESSIVENESS/PREROLL_FRAMES/HANGOVER_FRAMES) rather than
re-derived from scratch, since it's a working, already-tuned solution
to exactly the problem being solved here.
"""
from __future__ import annotations

import io
import wave
from collections import deque
from typing import Callable

import webrtcvad

AUDIO_SAMPLE_RATE = 16000
AUDIO_FRAME_MS = 30
AUDIO_FRAME_BYTES = int(AUDIO_SAMPLE_RATE * AUDIO_FRAME_MS / 1000) * 2  # 960 bytes, PCM16

PREROLL_FRAMES = 10   # ~300ms kept before a trigger, in case speech starts there
HANGOVER_FRAMES = 20  # ~600ms of continued silence tolerated before closing out
VAD_AGGRESSIVENESS = 2  # 0 (permissive) - 3 (strict); 2 is a reasonable default
# Safety valve, not a real duration limit -- an utterance running this
# long almost certainly means the VAD got latched onto something that
# isn't actually a single spoken utterance (most likely the robot's own
# TTS bleeding into its mic). ~15s of continuous "speech" force-emits
# and resets rather than growing forever. See process()'s comment.
MAX_UTTERANCE_FRAMES = 500  # ~15s at 30ms/frame


class Segmenter:
    """Feed it 30ms PCM16 frames via process(); emit_fn(pcm_bytes) is
    called once per COMPLETE utterance. Frames belonging to silence
    (no utterance in progress) are simply absorbed into the pre-roll
    ring and never passed to emit_fn at all.
    """

    def __init__(self, emit_fn: Callable[[bytes], None]):
        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self._preroll: deque[bytes] = deque(maxlen=PREROLL_FRAMES)
        self._triggered = False
        self._voiced: list[bytes] = []
        self._silence_run = 0
        self._emit = emit_fn

    def process(self, frame: bytes):
        if len(frame) != AUDIO_FRAME_BYTES:
            # webrtcvad requires exact 10/20/30ms frames or it raises --
            # drop anything malformed rather than crashing the whole
            # media relay loop over one bad frame. MicStream.kt always
            # sends exactly AUDIO_FRAME_BYTES per frame, so this should
            # only ever trigger on a genuinely corrupt/partial frame.
            return

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
            return

        if len(self._voiced) >= MAX_UTTERANCE_FRAMES:
            # Safety valve: force-emit and reset rather than staying
            # triggered forever. Without this, any sustained run of
            # frames the VAD classifies as continuous speech -- the
            # robot picking up its own TTS through its own mic being
            # the leading suspect, but any cause -- would grow _voiced
            # without bound and never emit again, which looks exactly
            # like "it only heard audio once and then stopped."
            self._emit(b"".join(self._voiced))
            self._triggered = False
            self._voiced = []
            self._silence_run = 0

    def flush(self):
        """Call on disconnect so an in-progress utterance isn't lost
        just because the connection dropped mid-sentence."""
        if self._triggered and self._voiced:
            self._emit(b"".join(self._voiced))
        self._triggered = False
        self._voiced = []


def pcm_to_wav_bytes(pcm_bytes: bytes) -> bytes:
    """In-memory PCM16 mono -> WAV container, no disk touched."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(AUDIO_SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()
