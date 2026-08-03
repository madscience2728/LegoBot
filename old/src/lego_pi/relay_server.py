"""
relay_server.py -- runs ON THE PI.

Deliberately dumb. This file does not know what "goto_angle" means -- it
just takes a {"cmd": ..., "args": ...} body off the wire and hands it to
lego_control.hub_controller.dispatch(), which is the same dispatch used
if the PC bypasses this relay entirely and talks BLE directly. If you
find yourself wanting to add robot-specific logic in THIS file, it
belongs in hub_controller.py instead -- that's what keeps the relay a
true 1:1 pass-through instead of a second brain.

TWO HUBS: this robot is a 4-wheel skid-steer split across a front hub
("Control+ Hub") and a rear hub ("Daril"). Both get connected and held
open at once -- a command just says which one it's for via the "hub"
field (defaults to "front" for backward compatibility with anything
that doesn't know about the rear hub yet).

Connected SEQUENTIALLY, not concurrently, on purpose: two BLE discovery
scans running at once on the same radio is exactly what caused the
org.bluez.Error.InProgress mess this project already went through and
fixed for the single-hub case -- see hub_controller.py's
_force_adapter_reset and robot_files/legobot-relay.service's
ExecStartPre for that history. Front fully connects (scan, find,
handshake) before rear's scan even starts. Once connected, a hub isn't
scanning anymore, so there's never a second scan session competing with
the first.

Run with:
    pip install fastapi uvicorn
    uvicorn lego_pi.relay_server:app --host 0.0.0.0 --port 8000

Suggested: wire this into a systemd unit (see SpiderBot's
robot_files/spider-robot.service for the pattern) so it's running
persistently and doesn't need a manual SSH session.
"""

import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from lego_control.hub_controller import HubController, HubNotReady, dispatch

# Concrete defaults, not a generic env-var-only knob -- these ARE the
# two hubs on this specific robot. Still overridable via env var if a
# hub ever gets renamed, same LEGOBOT_* pattern robot_client.py already
# uses, but nothing needs to be set for the normal case to work.
FRONT_HUB_NAME = os.environ.get("LEGOBOT_FRONT_HUB_NAME", "Control+ Hub")
REAR_HUB_NAME = os.environ.get("LEGOBOT_REAR_HUB_NAME", "Daril")

controllers = {
    "front": HubController(hub_name=FRONT_HUB_NAME),
    "rear": HubController(hub_name=REAR_HUB_NAME),
}
_connect_lock = threading.Lock()


class Command(BaseModel):
    cmd: str
    args: dict = {}
    hub: str = "front"  # "front" or "rear" -- see module docstring


def _connect_in_background():
    """Runs the (intentionally indefinite) discovery-and-connect loop on
    a daemon thread, one hub fully at a time -- see module docstring for
    why this is sequential rather than parallel. Startup must NOT block
    on this directly -- a hub might be off for a while, and /health
    still needs to answer in the meantime. Scan progress goes to stdout,
    which lands in the journal (`journalctl -u legobot-relay -f`), so
    it's still visible even with nobody at a terminal.

    Retries connect() itself indefinitely on failure, per hub -- most
    transient scan-level errors are now handled inside
    hub_controller.wait_for_hub, but this is the outer safety net for
    anything that still gets through (e.g. pylgbst's handshake failing
    after the hub was already found). Without this, a single failed
    attempt left the relay permanently stuck reporting connected: false
    with nothing left to retry it -- which is exactly what happened
    before this fix.
    """
    if not _connect_lock.acquire(blocking=False):
        print("[relay] connect already in progress, ignoring duplicate request.")
        return
    try:
        for name, controller in controllers.items():
            while not controller.connected:
                try:
                    controller.connect()
                    print(f"[relay] {name} hub ({controller.hub_name}) connected.")
                except Exception as e:
                    print(f"[relay] {name} hub connect attempt failed ({e}), retrying in 5s...")
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
    print("[relay] startup: launching background connect thread (front, then rear).")
    threading.Thread(target=_connect_in_background, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {name: controller.health() for name, controller in controllers.items()}


@app.post("/connect")
def connect():
    if all(c.connected for c in controllers.values()):
        return {name: c.health() for name, c in controllers.items()}
    threading.Thread(target=_connect_in_background, daemon=True).start()
    return {"status": "connect attempt started in background, poll /health"}


@app.post("/command")
def command(body: Command):
    controller = controllers.get(body.hub)
    if controller is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown hub {body.hub!r}, expected one of {list(controllers)}",
        )
    try:
        return dispatch(controller, body.cmd, body.args)
    except HubNotReady as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"hub command failed: {e}")
