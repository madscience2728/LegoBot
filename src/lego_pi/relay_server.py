"""
relay_server.py -- runs ON THE PI.

Deliberately dumb. This file does not know what "goto_angle" means -- it
just takes a {"cmd": ..., "args": ...} body off the wire and hands it to
lego_control.hub_controller.dispatch(), which is the same dispatch used
if the PC bypasses this relay entirely and talks BLE directly. If you
find yourself wanting to add robot-specific logic in THIS file, it
belongs in hub_controller.py instead -- that's what keeps the relay a
true 1:1 pass-through instead of a second brain.

Run with:
    pip install fastapi uvicorn
    uvicorn lego_pi.relay_server:app --host 0.0.0.0 --port 8000

Suggested: wire this into a systemd unit (see SpiderBot's
robot_files/spider-robot.service for the pattern) so it's running
persistently and doesn't need a manual SSH session.
"""

import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from lego_control.hub_controller import HubController, HubNotReady, dispatch

controller = HubController()
_connect_lock = threading.Lock()


class Command(BaseModel):
    cmd: str
    args: dict = {}


def _connect_in_background():
    """Runs the (intentionally indefinite) discovery-and-connect loop on
    a daemon thread. Startup must NOT block on this directly -- the hub
    might be off for a while, and /health still needs to answer in the
    meantime. Scan progress goes to stdout, which lands in the journal
    (`journalctl -u legobot-relay -f`), so it's still visible even with
    nobody at a terminal.

    Retries connect() itself indefinitely on failure -- most transient
    scan-level errors are now handled inside hub_controller.wait_for_hub,
    but this is the outer safety net for anything that still gets
    through (e.g. pylgbst's handshake failing after the hub was already
    found). Without this, a single failed attempt left the relay
    permanently stuck reporting connected: false with nothing left to
    retry it -- which is exactly what happened before this fix.
    """
    if not _connect_lock.acquire(blocking=False):
        print("[relay] connect already in progress, ignoring duplicate request.")
        return
    try:
        while not controller.connected:
            try:
                controller.connect()
                print("[relay] connected.")
            except Exception as e:
                print(f"[relay] connect attempt failed ({e}), retrying in 5s...")
                time.sleep(5)
    finally:
        _connect_lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Deliberately NOT using the older @app.on_event("startup") hook --
    # it's deprecated, and depending on the installed FastAPI/Starlette
    # version it can silently fail to fire at all instead of raising an
    # error. That failure mode is indistinguishable from "still
    # scanning" from the outside: /health answers fine either way, no
    # exception is ever logged, and the only symptom is the connect
    # thread's output never showing up. `lifespan` is the current,
    # guaranteed-to-run mechanism across all maintained FastAPI versions.
    print("[relay] startup: launching background connect thread.")
    threading.Thread(target=_connect_in_background, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return controller.health()


@app.post("/connect")
def connect():
    if controller.connected:
        return controller.health()
    threading.Thread(target=_connect_in_background, daemon=True).start()
    return {"status": "connect attempt started in background, poll /health"}


@app.post("/command")
def command(body: Command):
    try:
        return dispatch(controller, body.cmd, body.args)
    except HubNotReady as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"hub command failed: {e}")
