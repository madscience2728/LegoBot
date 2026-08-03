"""
pc_server.py -- runs ON THE PC.

The front door this codebase didn't have yet: a real HTTP server that
something external (a manual test script for now -- see
scripts/call_server.py -- Gemma's control loop later) can send requests
to, instead of driving robot_client.py from an interactive CLI. Doesn't
replace main.py's CLI; that's still there for manual testing. This is
for a caller that isn't a human at a keyboard.

Three endpoints, two very different contracts on purpose:

    POST /command   BLOCKS until the robot actually finishes the move.
                     robot_client.py's own retry/fallback logic (health
                     retries, waiting for the Pi's BLE connect, falling
                     back to direct BLE) can itself take several
                     seconds -- that's fine here. A caller asking the
                     robot to do something wants to know when it's
                     actually done, not get an immediate "sure, maybe".

    GET  /frame     NEVER blocks. Returns whatever JPEG is currently
    GET  /audio     cached in memory -- kept up to date by a persistent
                     background WebSocket connection to the Pi's
                     media_relay (see lego_brain/media_client.py), not
                     fetched fresh per request. A request that arrives
                     between frames just gets the last one; it never
                     waits on the network. Same contract for /audio,
                     which returns the most recently completed speech
                     utterance as a WAV blob.

Run with:
    pip install -r lego_brain/requirements.txt
    set LEGOBOT_PI_HOST=legobot.local   (or export on mac/linux)
    uvicorn lego_brain.pc_server:app --host 0.0.0.0 --port 9000
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from lego_brain import robot_client
from lego_brain.media_client import MediaClient

PI_HOST = os.environ.get("LEGOBOT_PI_HOST", "legobot.local")
PI_MEDIA_PORT = int(os.environ.get("LEGOBOT_PI_MEDIA_PORT", "8001"))

media_client = MediaClient(f"ws://{PI_HOST}:{PI_MEDIA_PORT}/media")


class Command(BaseModel):
    cmd: str
    args: dict = {}
    hub: str = "front"  # "front" or "rear" -- see relay_server.py's Command


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Background task, started once at server boot -- keeps
    # media_client's latest_frame/latest_utterance_wav current for the
    # entire lifetime of the process, independent of any individual
    # /frame or /audio request. This is what makes those endpoints
    # non-blocking: by the time a request arrives, the data's already
    # there or it isn't yet, nothing gets fetched on demand.
    task = asyncio.create_task(media_client.run_forever())
    print(f"[pc_server] media client connecting to {media_client.uri} in the background.")
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    try:
        return robot_client.health()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"health check failed: {e}")


@app.post("/command")
def command(body: Command):
    """Blocking on purpose -- see module docstring."""
    try:
        return robot_client.dispatch(body.cmd, body.args, hub=body.hub)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"robot command failed: {e}")


@app.get("/frame")
def frame():
    """Non-blocking -- returns the cached latest JPEG, never waits on
    the network. See module docstring and MediaClient's docstring."""
    jpeg = media_client.latest_frame
    if jpeg is None:
        raise HTTPException(status_code=503, detail="no frame received from the Pi yet")
    return Response(content=jpeg, media_type="image/jpeg")


@app.get("/audio")
def audio():
    """Non-blocking -- returns the cached most recent completed
    utterance as a WAV blob, never waits on the network."""
    wav_bytes = media_client.latest_utterance_wav
    if wav_bytes is None:
        raise HTTPException(status_code=503, detail="no utterance captured yet")
    return Response(content=wav_bytes, media_type="audio/wav")
