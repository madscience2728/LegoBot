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

The actual demux + VAD segmenting logic lives in
lego_brain/media_client.py now, shared with lego_brain/pc_server.py --
this file is just the disk-writing debug wrapper around it. See that
module's docstring for the pre-roll/hangover reasoning.

Usage:
    pip install -r ../src/lego_brain/requirements.txt opencv-python-headless
    set LEGOBOT_PI_HOST=legobot.local   (or export on mac/linux)
    python3 scripts/pc_media_client.py            # save-to-disk mode
    python3 scripts/pc_media_client.py --preview  # also pop a live cv2 window
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np

from lego_brain.media_client import MediaClient

OUT_DIR = Path("debug_media")


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
    media_client = MediaClient(f"ws://{host}:8001/media")

    last_seen_frame = None
    last_seen_utterance = None
    OUT_DIR.mkdir(exist_ok=True)

    print(f"[pc_media_client] connecting to {media_client.uri} ...")
    task = asyncio.create_task(media_client.run_forever())
    try:
        print("[pc_media_client] connected. Ctrl+C to stop.")
        while True:
            # Polling media_client's in-memory state rather than
            # re-implementing the WebSocket demux here too -- that
            # logic (and the pre-roll/hangover segmenter) lives in
            # media_client.py now, shared with pc_server.py.
            if media_client.latest_frame is not last_seen_frame:
                last_seen_frame = media_client.latest_frame
                _save_frame(last_seen_frame, preview)

            if media_client.latest_utterance_wav is not last_seen_utterance:
                last_seen_utterance = media_client.latest_utterance_wav
                path = OUT_DIR / f"utterance_{time.strftime('%Y%m%d_%H%M%S')}.wav"
                path.write_bytes(last_seen_utterance)
                print(f"[pc_media_client] wrote {path}")

            await asyncio.sleep(0.05)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        task.cancel()


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
