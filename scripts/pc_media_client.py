"""
pc_media_client.py -- PC-side sanity check for lego_pi/media_relay.py.

Connects to the Pi's media WebSocket, demuxes video/audio, and writes
both to disk so you can verify the pipeline BEFORE wiring it into Gemma:

    debug_media/latest.jpg       -- overwritten every frame; open it in
                                     any image viewer, or pass --preview
                                     to pop a live cv2 window instead.
    debug_media/utterance_*.wav  -- one file per detected speech segment.
                                     Listen back to confirm nothing got
                                     clipped at the start or end before
                                     you trust the segmenter with Gemma.

This is also where the "trim dead air without losing speech" logic
lives -- deliberately on the PC, not the Pi (see media_relay.py's
docstring for why). Two things do the actual work:

  - PRE-ROLL ring buffer: the last ~300ms of audio is always kept even
    while nothing is triggering yet. When voice IS detected, that
    buffer is prepended to the segment -- so the first syllable, which
    happens before webrtcvad has seen enough consecutive voiced frames
    to confirm "yes, that's speech", doesn't get lost.
  - HANGOVER: after voice stops, the segment stays open for ~600ms of
    continued silence before being closed out. A normal pause between
    sentences is shorter than that, so a person trailing off mid-
    thought doesn't get cut short.

Usage:
    pip install websockets opencv-python-headless numpy webrtcvad
    set LEGOBOT_PI_HOST=legobot.local   (or export on mac/linux)
    python3 scripts/pc_media_client.py            # save-to-disk mode
    python3 scripts/pc_media_client.py --preview  # also pop a live cv2 window
"""

import argparse
import asyncio
import os
import struct
import time
import wave
from collections import deque
from pathlib import Path

import numpy as np
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

OUT_DIR = Path("debug_media")


class Segmenter:
    """Turns a stream of 30ms PCM16 frames into complete utterances,
    using a pre-roll buffer (don't miss the start) and hangover (don't
    cut the end). emit_fn(pcm_bytes) is called once per completed
    utterance -- swap that for a Gemma call once this looks right.
    """

    def __init__(self, emit_fn):
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


def _save_wav(pcm_bytes: bytes):
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"utterance_{time.strftime('%Y%m%d_%H%M%S')}.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(AUDIO_SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    duration = len(pcm_bytes) / 2 / AUDIO_SAMPLE_RATE
    print(f"[pc_media_client] wrote {path} ({duration:.2f}s)")


def _save_frame(jpeg_bytes: bytes, preview: bool):
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "latest.jpg").write_bytes(jpeg_bytes)
    if preview:
        import cv2

        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            cv2.imshow("LegoBot media preview", img)
            cv2.waitKey(1)


async def run(host: str, preview: bool):
    uri = f"ws://{host}:8001/media"
    segmenter = Segmenter(_save_wav)
    audio_carry = b""  # handles the case where a WS message doesn't land
                        # exactly on a 30ms frame boundary

    print(f"[pc_media_client] connecting to {uri} ...")
    async with websockets.connect(uri, max_size=None) as ws:
        print("[pc_media_client] connected. Ctrl+C to stop.")
        try:
            async for message in ws:
                msg_type, _ts = struct.unpack(">Bd", message[:9])
                payload = message[9:]

                if msg_type == TYPE_VIDEO:
                    _save_frame(payload, preview)

                elif msg_type == TYPE_AUDIO:
                    audio_carry += payload
                    while len(audio_carry) >= AUDIO_FRAME_BYTES:
                        frame = audio_carry[:AUDIO_FRAME_BYTES]
                        audio_carry = audio_carry[AUDIO_FRAME_BYTES:]
                        segmenter.process(frame)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            segmenter.flush()


def main():
    parser = argparse.ArgumentParser(description="LegoBot media test client")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="also show a live cv2 window of incoming frames (needs a display)",
    )
    args = parser.parse_args()

    host = os.environ.get("LEGOBOT_PI_HOST", "legobot.local")
    try:
        asyncio.run(run(host, args.preview))
    except KeyboardInterrupt:
        print("\n[pc_media_client] stopped.")


if __name__ == "__main__":
    main()
