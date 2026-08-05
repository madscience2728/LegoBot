#!/usr/bin/env python3
"""
server.py -- local GUI for the phone probe stage.

Run it, open http://localhost:8000 in a browser, type in the phone's
WiFi IP (shown on the phone's own screen after launch), hit Connect.

What it does:
  - Polls the phone's HTTP probe endpoints (port 8765):
        /health     every HEALTH_POLL_SECONDS
        /ble/scan   every BLE_SCAN_SECONDS (each scan itself takes
                    BLE_SCAN_DURATION seconds on the phone)
  - Holds a persistent client connection to the phone's media relay
    WebSocket (ws://<phone-ip>:8001/media) and decodes the binary
    frames using the exact same framing as MediaFraming.kt / the PC's
    existing media_relay.py:
        1 byte type (0x01 video JPEG, 0x02 audio PCM16 mono 16kHz)
        8 byte big-endian double, unix seconds, when the phone captured it
        payload
  - Fans all of that out as JSON (+ base64 JPEG for video) over its own
    WebSocket (/ws) to any browser tabs that have the dashboard open.
  - Tracks per-channel latency (receipt time on this PC minus the
    phone's embedded capture timestamp), observed fps, and time since
    last frame, so the dashboard can show whether the network link is
    keeping up.

Only the standard library plus fastapi/uvicorn/websockets/requests are
used -- nothing else needs to be installed.
"""
import asyncio
import base64
import struct
import time
from collections import deque
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from typing import Literal
import uvicorn

# ---------------------------------------------------------------------------
# Config -- matches the phone-side ports/paths in ProbeService.kt and
# relay/MediaRelayServer.kt exactly. Don't change these unless the phone
# app changes.
# ---------------------------------------------------------------------------
PROBE_HTTP_PORT = 8765
MEDIA_WS_PORT = 8001
MEDIA_WS_PATH = "/media"

TYPE_VIDEO = 0x01
TYPE_AUDIO = 0x02

HEALTH_POLL_SECONDS = 3
BLE_SCAN_SECONDS = 15          # how often to kick off a new scan
BLE_SCAN_DURATION = 4          # "seconds" param sent to /ble/scan
# The phone now serializes BLE scanning (BleScanLock) so this scan and a
# hub-connect scan never collide on the one radio -- worst case, this
# request has to wait out a full hub-connect scan (~12s) before its own
# BLE_SCAN_DURATION even starts. Timeout needs headroom for that wait,
# not just the scan itself.
HTTP_TIMEOUT = 12 + BLE_SCAN_DURATION + 6

AUDIO_BROADCAST_HZ = 10         # throttle mic-level updates sent to browser
TIMING_BROADCAST_HZ = 2         # how often the timing panel refreshes
LATENCY_WINDOW = 150            # samples kept for rolling stats
FPS_WINDOW_SECONDS = 5

STATIC_DIR = Path(__file__).parent

# This dashboard's index.html/style.css change often during active
# development -- browsers caching a stale copy after a refresh (no hard
# reload) has already caused at least one confusing "the CSS I just
# shipped isn't showing up" report. Small dev tool, not worth the
# complexity of cache-busting filenames; just tell the browser not to
# cache these two at all.
_NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate"}

# Same vocabulary as old/src/lego_control/hub_controller.py's COMMANDS
# dict, and ProbeService.kt's KNOWN_COMMANDS on the phone -- duplicated
# in all three since Python/Kotlin can't share source here. Nothing
# executes these yet (no hub attached), this just keeps garbage cmd
# names from silently round-tripping as "ok" the way a bare dict body
# used to let them.
KNOWN_COMMANDS = {
    "stop", "set_speed", "read_angle", "preset_zero", "describe_port",
    "read_apos", "home_angle", "set_position", "goto_angle",
}


class CommandRequest(BaseModel):
    cmd: str
    args: dict = Field(default_factory=dict)
    hub: Literal["front", "rear"] = "front"

    @field_validator("cmd")
    @classmethod
    def cmd_known(cls, v: str) -> str:
        v = v.strip()
        if v not in KNOWN_COMMANDS:
            raise ValueError(f"unknown cmd {v!r}, expected one of {sorted(KNOWN_COMMANDS)}")
        return v


# ---------------------------------------------------------------------------
# Small helper: rolling latency / fps tracker per channel (video, audio)
# ---------------------------------------------------------------------------
class ChannelTiming:
    def __init__(self):
        self.latencies_ms = deque(maxlen=LATENCY_WINDOW)
        self.receipt_times = deque(maxlen=LATENCY_WINDOW)  # monotonic
        self.last_receipt_monotonic: Optional[float] = None

    def record(self, latency_ms: float):
        now = time.monotonic()
        self.latencies_ms.append(latency_ms)
        self.receipt_times.append(now)
        self.last_receipt_monotonic = now

    def snapshot(self) -> dict:
        now = time.monotonic()
        if not self.latencies_ms:
            return {
                "fps": 0.0,
                "avg_latency_ms": None,
                "min_latency_ms": None,
                "max_latency_ms": None,
                "last_latency_ms": None,
                "last_frame_age_ms": None,
                "sparkline": [],
            }
        recent_count = sum(
            1 for t in self.receipt_times if now - t <= FPS_WINDOW_SECONDS
        )
        last_age = (
            (now - self.last_receipt_monotonic) * 1000
            if self.last_receipt_monotonic is not None
            else None
        )
        vals = list(self.latencies_ms)
        return {
            "fps": round(recent_count / FPS_WINDOW_SECONDS, 1),
            "avg_latency_ms": round(sum(vals) / len(vals), 1),
            "min_latency_ms": round(min(vals), 1),
            "max_latency_ms": round(max(vals), 1),
            "last_latency_ms": round(vals[-1], 1),
            "last_frame_age_ms": round(last_age, 1) if last_age is not None else None,
            "sparkline": vals[-60:],
        }


# ---------------------------------------------------------------------------
# Global connection state
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.phone_ip: Optional[str] = None
        self.media_connected = False
        self.health_ok: Optional[bool] = None
        self.last_health: Optional[dict] = None
        self.last_ble: Optional[dict] = None
        self.last_hub_status: Optional[dict] = None
        self.last_video_b64: Optional[str] = None
        self.video_timing = ChannelTiming()
        self.audio_timing = ChannelTiming()
        self.command_history: deque = deque(maxlen=50)
        self.tasks: list[asyncio.Task] = []
        self.clients: set[WebSocket] = set()

    def status_payload(self) -> dict:
        return {
            "type": "conn_status",
            "phone_ip": self.phone_ip,
            "media_connected": self.media_connected,
            "health_ok": self.health_ok,
        }


state = State()
app = FastAPI()


# ---------------------------------------------------------------------------
# Broadcasting to browser tabs
# ---------------------------------------------------------------------------
async def broadcast(message: dict):
    dead = []
    for ws in state.clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.clients.discard(ws)


# ---------------------------------------------------------------------------
# Background task: media relay WebSocket client -> decode -> broadcast
# ---------------------------------------------------------------------------
async def media_relay_loop(ip: str):
    import websockets

    url = f"ws://{ip}:{MEDIA_WS_PORT}{MEDIA_WS_PATH}"
    last_audio_broadcast = 0.0

    while True:
        try:
            async with websockets.connect(url, open_timeout=6, max_size=8 * 1024 * 1024) as ws:
                state.media_connected = True
                await broadcast(state.status_payload())

                async for message in ws:
                    if not isinstance(message, (bytes, bytearray)) or len(message) < 9:
                        continue
                    msg_type = message[0]
                    (phone_ts,) = struct.unpack(">d", message[1:9])
                    payload = message[9:]
                    now = time.time()
                    latency_ms = (now - phone_ts) * 1000

                    if msg_type == TYPE_VIDEO:
                        state.video_timing.record(latency_ms)
                        b64 = base64.b64encode(payload).decode("ascii")
                        state.last_video_b64 = b64
                        await broadcast({
                            "type": "video_frame",
                            "b64": b64,
                            "bytes": len(payload),
                            "latency_ms": round(latency_ms, 1),
                            "ts": phone_ts,
                        })

                    elif msg_type == TYPE_AUDIO:
                        state.audio_timing.record(latency_ms)
                        level, dbfs = pcm16_level(payload)
                        nowm = time.monotonic()
                        if nowm - last_audio_broadcast >= 1.0 / AUDIO_BROADCAST_HZ:
                            last_audio_broadcast = nowm
                            await broadcast({
                                "type": "audio_level",
                                "level": level,
                                "dbfs": dbfs,
                                "latency_ms": round(latency_ms, 1),
                                "ts": phone_ts,
                            })

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.media_connected = False
            await broadcast({**state.status_payload(), "error": f"media link: {exc}"})
            await asyncio.sleep(3)
            continue

        # ws closed cleanly (e.g. phone stopped the service) -- retry
        state.media_connected = False
        await broadcast(state.status_payload())
        await asyncio.sleep(3)


def pcm16_level(payload: bytes):
    """RMS level (0..1, clamped) and dBFS for a PCM16 mono little-endian chunk."""
    n_samples = len(payload) // 2
    if n_samples == 0:
        return 0.0, -120.0
    samples = struct.unpack(f"<{n_samples}h", payload[: n_samples * 2])
    mean_sq = sum(s * s for s in samples) / n_samples
    rms = mean_sq ** 0.5
    if rms < 1:
        return 0.0, -120.0
    import math
    dbfs = 20 * math.log10(max(rms, 1e-6) / 32768)
    level = max(0.0, min(1.0, (dbfs + 60) / 60))  # -60dBFS..0dBFS -> 0..1
    return round(level, 3), round(dbfs, 1)


# ---------------------------------------------------------------------------
# Background task: /health polling
# ---------------------------------------------------------------------------
async def health_poll_loop(ip: str):
    url = f"http://{ip}:{PROBE_HTTP_PORT}/health"
    while True:
        try:
            resp = await asyncio.to_thread(requests.get, url, timeout=5)
            data = resp.json()
            state.health_ok = data.get("status") == "ok"
            state.last_health = data
            await broadcast({"type": "health", "ok": True, "data": data, "ts": time.time()})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.health_ok = False
            await broadcast({"type": "health", "ok": False, "error": str(exc), "ts": time.time()})
        await asyncio.sleep(HEALTH_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Background task: /ble/scan polling
# ---------------------------------------------------------------------------
async def ble_scan_loop(ip: str):
    url = f"http://{ip}:{PROBE_HTTP_PORT}/ble/scan?seconds={BLE_SCAN_DURATION}"
    while True:
        try:
            resp = await asyncio.to_thread(requests.get, url, timeout=HTTP_TIMEOUT)
            data = resp.json()
            state.last_ble = data
            await broadcast({"type": "ble", "ok": True, "data": data, "ts": time.time()})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await broadcast({"type": "ble", "ok": False, "error": str(exc), "ts": time.time()})
        await asyncio.sleep(BLE_SCAN_SECONDS)


# ---------------------------------------------------------------------------
# Background task: timing panel refresh
# ---------------------------------------------------------------------------
async def timing_loop():
    while True:
        await broadcast({
            "type": "timing",
            "video": state.video_timing.snapshot(),
            "audio": state.audio_timing.snapshot(),
        })
        await asyncio.sleep(1.0 / TIMING_BROADCAST_HZ)


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------
async def start_connection(ip: str):
    await stop_connection()
    state.phone_ip = ip
    state.video_timing = ChannelTiming()
    state.audio_timing = ChannelTiming()
    state.tasks = [
        asyncio.create_task(media_relay_loop(ip)),
        asyncio.create_task(health_poll_loop(ip)),
        asyncio.create_task(ble_scan_loop(ip)),
    ]


async def stop_connection():
    for t in state.tasks:
        t.cancel()
    for t in state.tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    state.tasks = []
    state.media_connected = False
    state.phone_ip = None


# ---------------------------------------------------------------------------
# Timing loop runs regardless of connection state (just reports "no data" until connected)
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(timing_loop())


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    # Cache-Control headers alone weren't reliably bypassing a stale
    # cached style.css in testing (browser/proxy caching a same-URL
    # resource is notoriously inconsistent about revalidating even with
    # no-store set). Appending style.css's own mtime as a query string
    # forces a genuinely different URL every time the file's content
    # actually changes, which no cache can silently serve around --
    # self-maintaining, no manual version bump needed on future edits.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css_version = int((STATIC_DIR / "style.css").stat().st_mtime)
    html = html.replace('href="/style.css"', f'href="/style.css?v={css_version}"')
    return HTMLResponse(html, headers=_NO_CACHE_HEADERS)


@app.get("/style.css")
async def style():
    return FileResponse(STATIC_DIR / "style.css", media_type="text/css", headers=_NO_CACHE_HEADERS)


@app.post("/api/connect")
async def api_connect(payload: dict):
    ip = (payload or {}).get("ip", "").strip()
    if not ip:
        return JSONResponse({"status": "error", "message": "No IP given."}, status_code=400)
    await start_connection(ip)
    return {"status": "connecting", "ip": ip}


@app.post("/api/disconnect")
async def api_disconnect():
    await stop_connection()
    await broadcast(state.status_payload())
    return {"status": "disconnected"}


@app.get("/api/status")
async def api_status():
    return {
        "phone_ip": state.phone_ip,
        "media_connected": state.media_connected,
        "health_ok": state.health_ok,
        "last_health": state.last_health,
        "last_ble": state.last_ble,
        "last_video_b64": state.last_video_b64,
        "video_timing": state.video_timing.snapshot(),
        "audio_timing": state.audio_timing.snapshot(),
        "command_history": list(state.command_history),
        "last_hub_status": state.last_hub_status,
    }


@app.get("/api/commands/known")
async def api_known_commands():
    return {"commands": sorted(KNOWN_COMMANDS), "hubs": ["front", "rear"]}


@app.post("/api/command")
async def api_command(payload: CommandRequest):
    """Proxies a dummy command to the phone's /command endpoint (see
    ProbeService.kt) and times the full round trip from this PC's
    perspective -- complementary to the media-channel latency numbers,
    since this is the channel real robot commands (and later, the LLM
    layer's tool calls) will actually travel over.

    cmd/hub are validated by CommandRequest before this even runs --
    FastAPI returns a 422 with the allowed values for anything outside
    the known vocabulary, so a typo'd command never reaches the phone
    or gets logged as if it were real.
    """
    if not state.phone_ip:
        return JSONResponse({"status": "error", "message": "Not connected to a phone."}, status_code=400)

    cmd, args, hub = payload.cmd, payload.args, payload.hub

    url = f"http://{state.phone_ip}:{PROBE_HTTP_PORT}/command"
    sent_at = time.time()
    t0 = time.monotonic()
    try:
        resp = await asyncio.to_thread(
            requests.post, url, json={"cmd": cmd, "args": args, "hub": hub}, timeout=8
        )
        round_trip_ms = (time.monotonic() - t0) * 1000
        data = resp.json()
        result = {
            "type": "command_result",
            "ok": True,
            "cmd": cmd, "args": args, "hub": hub,
            "round_trip_ms": round(round_trip_ms, 1),
            "sent_at": sent_at,
            "response": data,
        }
    except Exception as exc:
        round_trip_ms = (time.monotonic() - t0) * 1000
        result = {
            "type": "command_result",
            "ok": False,
            "cmd": cmd, "args": args, "hub": hub,
            "round_trip_ms": round(round_trip_ms, 1),
            "sent_at": sent_at,
            "error": str(exc),
        }

    state.command_history.appendleft(result)
    await broadcast(result)
    return result


@app.post("/api/hub/connect")
async def api_hub_connect(payload: dict):
    """Proxies to the phone's /hub/connect (see HubConnector.kt), which
    does a real BLE scan + GATT connect + notify-subscribe. This can
    take up to ~25s on the phone (scan + handshake), so the timeout here
    is generous -- this is NOT the low-latency dummy /command channel,
    it's a slow one-shot action, same as /ble/scan already is.
    """
    if not state.phone_ip:
        return JSONResponse({"status": "error", "message": "Not connected to a phone."}, status_code=400)

    hub = (payload or {}).get("hub", "").strip().lower()
    if hub not in ("front", "rear"):
        return JSONResponse({"status": "error", "message": "hub must be 'front' or 'rear'."}, status_code=400)

    url = f"http://{state.phone_ip}:{PROBE_HTTP_PORT}/hub/connect"
    try:
        resp = await asyncio.to_thread(requests.post, url, json={"hub": hub}, timeout=35)
        data = resp.json()
    except Exception as exc:
        data = {"status": "error", "message": str(exc)}

    # Fold this hub's result into the cached combined status so /api/status
    # and freshly-loaded tabs see it without waiting for a /hub/status poll.
    hub_result = data.get("hub") if isinstance(data.get("hub"), dict) else None
    if hub_result:
        state.last_hub_status = state.last_hub_status or {"status": "ok", "hubs": {}}
        state.last_hub_status.setdefault("hubs", {})[hub] = hub_result

    await broadcast({"type": "hub_status", "hub": hub, "data": data})
    return data


@app.post("/api/hub/disconnect")
async def api_hub_disconnect(payload: dict):
    """Plain teardown, no reconnect attempt -- see HubConnector's
    disconnect() / ProbeService's hubDisconnect() docstrings for why
    this is a separate endpoint from /hub/connect rather than routing
    'Reconnect' back through connect()."""
    if not state.phone_ip:
        return JSONResponse({"status": "error", "message": "Not connected to a phone."}, status_code=400)

    hub = (payload or {}).get("hub", "").strip().lower()
    if hub not in ("front", "rear"):
        return JSONResponse({"status": "error", "message": "hub must be 'front' or 'rear'."}, status_code=400)

    url = f"http://{state.phone_ip}:{PROBE_HTTP_PORT}/hub/disconnect"
    try:
        resp = await asyncio.to_thread(requests.post, url, json={"hub": hub}, timeout=8)
        data = resp.json()
    except Exception as exc:
        data = {"status": "error", "message": str(exc)}

    hub_result = data.get("hub") if isinstance(data.get("hub"), dict) else None
    if hub_result:
        state.last_hub_status = state.last_hub_status or {"status": "ok", "hubs": {}}
        state.last_hub_status.setdefault("hubs", {})[hub] = hub_result

    await broadcast({"type": "hub_status", "hub": hub, "data": data})
    return data


@app.get("/api/hub/status")
async def api_hub_status():
    """Non-blocking -- just asks the phone what it already knows, same
    as the phone's own /hub/status. Safe to poll."""
    if not state.phone_ip:
        return JSONResponse({"status": "error", "message": "Not connected to a phone."}, status_code=400)

    url = f"http://{state.phone_ip}:{PROBE_HTTP_PORT}/hub/status"
    try:
        # /hub/status itself is instant on the phone, but the phone's
        # single request-handling capacity can be under real load from a
        # concurrent /hub/connect (BLE scan+GATT for the OTHER hub) --
        # 5s was tight enough to occasionally 502 during that, even
        # though the phone was healthy and would've answered shortly after.
        resp = await asyncio.to_thread(requests.get, url, timeout=12)
        data = resp.json()
        state.last_hub_status = data
        await broadcast({"type": "hub_status", "hub": None, "data": data})
        return data
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=502)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    state.clients.add(ws)
    try:
        await ws.send_json(state.status_payload())
        while True:
            # Browser doesn't need to send anything; just keep the socket
            # open and drop the connection if the client goes away.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        state.clients.discard(ws)


def main():
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
