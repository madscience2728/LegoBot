"""
media_relay.py -- runs ON THE PI, alongside relay_server.py.

Deliberately dumb, same philosophy as relay_server.py: this file does not
know what "speech" or "an utterance" is. It just grabs JPEG frames from
the Kiyo and raw PCM16 audio from its mic, timestamps each chunk, and
pushes it over a WebSocket as fast as it can. All the "is this silence,
should I keep this" decisions belong on the PC (see
scripts/pc_media_client.py) -- that's what keeps this a true capture-and-
forward pass-through instead of a second brain that might quietly drop
something you needed.

Run with:
    pip install fastapi uvicorn opencv-python-headless sounddevice numpy
    uvicorn lego_pi.media_relay:app --host 0.0.0.0 --port 8001

Runs on a separate port (8001) from relay_server.py's command port
(8000) so robot control and media never share a socket -- a slow/blocked
video frame should never be able to delay a stop command.

Wire framing (binary, sent as WebSocket binary messages):
    [1 byte type][8 byte double timestamp, big-endian][payload]
    type 0x01 = video frame -- payload is JPEG bytes
    type 0x02 = audio frame -- payload is raw PCM16 mono bytes

Audio is captured at 16000 Hz mono, 30ms frames (480 samples / 960
bytes) -- that frame size is a hard requirement of webrtcvad on the PC
side (10/20/30ms only), so it's fixed here rather than left configurable.
"""

import asyncio
import struct
import threading
import time

import cv2
import numpy as np
import sounddevice as sd
from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

VIDEO_FPS = 5
JPEG_QUALITY = 70

AUDIO_SAMPLE_RATE = 16000
AUDIO_FRAME_MS = 30
AUDIO_FRAME_SAMPLES = int(AUDIO_SAMPLE_RATE * AUDIO_FRAME_MS / 1000)  # 480

TYPE_VIDEO = 0x01
TYPE_AUDIO = 0x02

app = FastAPI()


def _pack(msg_type: int, payload: bytes) -> bytes:
    return struct.pack(">Bd", msg_type, time.time()) + payload


class MediaSource:
    """Owns the camera + mic hardware and fans captured chunks out to
    an asyncio.Queue that the WebSocket handler drains. Capture runs on
    background threads (cv2 and sounddevice are both blocking APIs) --
    queue.put_nowait is safe to call from those threads since asyncio
    queues aren't thread-safe by default, so we go through
    call_soon_threadsafe instead of touching the queue directly.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self._loop = loop
        self._queue = queue
        self._stop = threading.Event()
        self._video_thread = threading.Thread(target=self._video_loop, daemon=True)
        self._audio_stream = None

    def start(self):
        self._video_thread.start()
        try:
            self._audio_stream = sd.InputStream(
                samplerate=AUDIO_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=AUDIO_FRAME_SAMPLES,
                callback=self._audio_callback,
            )
            self._audio_stream.start()
        except Exception:
            # The video thread above is ALREADY running by this point --
            # if we just let this exception propagate, nothing ever
            # tells that thread to stop (self._stop never gets set,
            # nobody calls stop()). It keeps running forever, holding
            # /dev/video0 open, and the NEXT connection attempt's camera
            # open then fails too -- one leaked thread per failed
            # attempt, compounding. Clean up what we already started
            # before re-raising, so a failed start() doesn't leak.
            self.stop()
            raise

    def stop(self):
        self._stop.set()
        if self._audio_stream is not None:
            self._audio_stream.stop()
            self._audio_stream.close()

    def _put(self, msg_type: int, payload: bytes):
        packed = _pack(msg_type, payload)
        self._loop.call_soon_threadsafe(self._queue.put_nowait, packed)

    def _video_loop(self):
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            print("[media_relay] WARNING: could not open camera device 0.")
            return
        interval = 1.0 / VIDEO_FPS
        try:
            while not self._stop.is_set():
                start = time.time()
                ok, frame = cap.read()
                if ok:
                    ok, buf = cv2.imencode(
                        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
                    )
                    if ok:
                        self._put(TYPE_VIDEO, buf.tobytes())
                elapsed = time.time() - start
                time.sleep(max(0.0, interval - elapsed))
        finally:
            cap.release()

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            print(f"[media_relay] audio status: {status}")
        # indata is float32 or int16 depending on dtype above (int16 here)
        self._put(TYPE_AUDIO, np.asarray(indata).tobytes())


@app.websocket("/media")
async def media_ws(websocket: WebSocket):
    await websocket.accept()
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    source = MediaSource(loop, queue)
    try:
        # Inside the try now, not before it -- a failed start() (camera
        # or mic busy/unavailable) used to skip the finally entirely,
        # which is the actual leak this whole fix is for. Now any
        # failure here still reaches finally: source.stop() below.
        source.start()
        print("[media_relay] client connected, capture started.")
        while True:
            packed = await queue.get()
            await websocket.send_bytes(packed)
    except WebSocketDisconnect:
        print("[media_relay] client disconnected.")
    except Exception as e:
        # Covers source.start() failing (device busy/unavailable) as
        # well as anything going wrong mid-stream. Close with a real
        # code/reason instead of letting the exception propagate up
        # through ASGI and kill the connection with no close frame at
        # all -- which is indistinguishable, from the client's side,
        # from the server just vanishing.
        print(f"[media_relay] error ({e}), closing connection.")
        try:
            await websocket.close(code=1011, reason=str(e)[:120])
        except Exception:
            pass
    finally:
        source.stop()
