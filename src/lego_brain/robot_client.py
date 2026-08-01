"""
robot_client.py -- the ONLY place your app code should talk to LegoBot.

This is the "universal adapter" from the design discussion: callers use
goto_angle(), set_speed(), stop() and never know or care whether the
command went over HTTP to the Pi relay or straight out the PC's own
Bluetooth radio. Both paths ultimately run the exact same
hub_controller code (see lego_control/hub_controller.py) -- this module
only decides WHICH machine's radio does the talking.

Routing rule (Pi-first, ~99% of use, direct BLE as the rare exception):
    1. Retry the Pi relay's /health for a few seconds -- covers the
       normal "just restarted the service" case, not just a genuinely
       dead Pi. A single quick check isn't enough runway for uvicorn to
       finish booting after `systemctl restart`.
    2. Answers -> route there. Every call after that just uses the Pi,
       no per-call re-probing.
    3. Never answers -> ask ONCE (interactively) whether to bypass to
       direct BLE from this PC. If yes, stick with direct BLE for a
       while without re-asking; periodically re-check in the background
       so it can quietly switch back to the Pi once it's reachable
       again, without needing to be told.

Caveat: BLE supports one active central connection to the hub at a time.
If you're in direct-BLE mode, the PC currently holds that connection --
the Pi relay can't steal it back mid-session, it'll just keep scanning
harmlessly until you free it up (stop this process, or call
release_direct_ble()).

Env vars (same idea as SpiderBot's SPIDER_BOT_HOST):
    LEGOBOT_PI_HOST           -- Pi's hostname or IP, e.g. "legobot.local"
    LEGOBOT_PI_PORT           -- default 8000
    LEGOBOT_HUB_NAME          -- default "Control+ Hub"
    LEGOBOT_PI_WAIT_ATTEMPTS  -- default 6 (health-check retries before giving up on the Pi)
    LEGOBOT_PI_WAIT_DELAY     -- default 1.5 (seconds between those retries)
"""

import os
import threading
import time

import requests

from lego_control.hub_controller import HubController, dispatch

PI_PORT = int(os.environ.get("LEGOBOT_PI_PORT", "8000"))
HUB_NAME = os.environ.get("LEGOBOT_HUB_NAME", "Control+ Hub")
PI_WAIT_ATTEMPTS = int(os.environ.get("LEGOBOT_PI_WAIT_ATTEMPTS", "6"))
PI_WAIT_DELAY = float(os.environ.get("LEGOBOT_PI_WAIT_DELAY", "1.5"))
# Separate, longer budget for "relay is up but hasn't connected to the
# hub yet" (its own BLE scan-and-connect loop can take a few passes) --
# distinct from PI_WAIT_ATTEMPTS/DELAY above, which only wait for the
# relay *process* to answer HTTP at all.
PI_CONNECT_WAIT_ATTEMPTS = int(os.environ.get("LEGOBOT_PI_CONNECT_WAIT_ATTEMPTS", "10"))
PI_CONNECT_WAIT_DELAY = float(os.environ.get("LEGOBOT_PI_CONNECT_WAIT_DELAY", "2.0"))

# How long to stay in direct-BLE mode before quietly re-checking whether
# the Pi has come back, once we've already asked the user and gone
# direct. Keeps a long session from hammering /health on every call
# while still self-healing back to the preferred route eventually.
_DIRECT_RECHECK_INTERVAL = 30.0

_local_controller = None
_local_lock = threading.Lock()

_route_lock = threading.Lock()
_route_mode = None          # None (undecided) | "pi" | "direct"
_route_decided_at = 0.0


def _pi_base_url():
    host = os.environ.get("LEGOBOT_PI_HOST")
    if not host:
        return None
    return f"http://{host}:{PI_PORT}"


def _pi_health_once(base_url: str, timeout: float = 2.0):
    """A single /health attempt. Returns the parsed dict on success, or
    None on any failure (connection refused, timeout, non-200, etc.)."""
    try:
        r = requests.get(f"{base_url}/health", timeout=timeout)
        if r.ok:
            return r.json()
    except requests.RequestException:
        pass
    return None


def _wait_for_pi(base_url: str, attempts: int = PI_WAIT_ATTEMPTS, delay: float = PI_WAIT_DELAY):
    """Retries /health for a few seconds before concluding the Pi is
    actually down. This is what the single-shot check used to skip --
    right after a deploy/restart, uvicorn can take a couple seconds to
    bind its port, and a Pi that's merely still booting looked identical
    to one that's truly unreachable."""
    for attempt in range(1, attempts + 1):
        result = _pi_health_once(base_url)
        if result is not None:
            return result
        if attempt < attempts:
            time.sleep(delay)
    return None


def _wait_for_pi_connected(
    base_url: str, attempts: int = PI_CONNECT_WAIT_ATTEMPTS, delay: float = PI_CONNECT_WAIT_DELAY
) -> bool:
    """Waits for the Pi's *own* BLE connection to the hub to finish, as
    opposed to _wait_for_pi which only waits for the relay process to
    answer HTTP at all. These are genuinely different conditions: the
    relay can be fully up and responding (health check passes instantly)
    while its background discovery thread is still a few scan passes
    away from actually connecting to the hub (see relay_server.py's
    startup -- that connect runs on its own thread precisely so /health
    doesn't block on it). A 409 from /command means exactly this state.

    Waiting here instead of bypassing matters: bypassing to direct BLE
    the moment the Pi hasn't connected yet would have the PC grab the
    one BLE slot the Pi's thread is actively trying to acquire, which
    would strand the Pi permanently rather than just being slow.
    """
    for attempt in range(1, attempts + 1):
        result = _pi_health_once(base_url)
        if result is not None and result.get("connected"):
            return True
        if attempt < attempts:
            time.sleep(delay)
    return False


def _local() -> HubController:
    """Lazily connect a local (PC) BLE controller -- only touched once
    direct-BLE mode is actually chosen, so a healthy Pi setup never has
    the PC's own Bluetooth radio scanning at all."""
    global _local_controller
    with _local_lock:
        if _local_controller is None:
            controller = HubController(hub_name=HUB_NAME)
            controller.connect()
            _local_controller = controller
        return _local_controller


def _disconnect_local() -> None:
    """Actually releases this PC's BLE connection, if it's holding one.
    Necessary (not just cosmetic) any time we hand routing back to the
    Pi: BLE only allows one central connection to the hub, so the Pi's
    relay can see the hub over its own scan just fine but will get
    HubNotReady on every command until this PC actually lets go of the
    connection -- switching the route label alone doesn't free the radio."""
    global _local_controller
    with _local_lock:
        if _local_controller is not None:
            _local_controller.disconnect()
            _local_controller = None


def _prompt_direct_ble_bypass() -> bool:
    print(
        "[robot_client] Pi relay isn't responding after "
        f"{PI_WAIT_ATTEMPTS} attempts (~{PI_WAIT_ATTEMPTS * PI_WAIT_DELAY:.0f}s)."
    )
    reply = input(
        "Connect directly via this PC's Bluetooth instead? [y/N] "
    ).strip().lower()
    return reply in ("y", "yes")


def _resolve_route() -> str:
    """Decide "pi" or "direct" for this call, per the routing rule in
    the module docstring. Caches the decision so a long session doesn't
    re-probe /health (or re-prompt the user) on every single command."""
    global _route_mode, _route_decided_at

    base_url = _pi_base_url()
    if base_url is None:
        if _route_mode != "direct":
            print("[robot_client] LEGOBOT_PI_HOST not set -- using direct BLE.")
        _route_mode = "direct"
        return "direct"

    now = time.time()

    # Already committed to direct BLE recently -- don't re-probe yet,
    # but do periodically check in the background so we can switch back
    # to the preferred Pi route on our own once it's back.
    if _route_mode == "direct" and (now - _route_decided_at) < _DIRECT_RECHECK_INTERVAL:
        return "direct"

    if _route_mode == "direct":
        # Recheck window elapsed -- a quick single probe is enough here
        # (not the full retry budget), since we're not deciding to give
        # up on the Pi for the first time, just peeking.
        if _pi_health_once(base_url) is not None:
            print("[robot_client] Pi relay is back -- releasing direct BLE and switching back to it.")
            _disconnect_local()
            _route_mode = "pi"
        _route_decided_at = now
        return _route_mode

    # Undecided, or previously "pi" but about to find out it's down --
    # give the Pi a real chance before concluding anything.
    if _wait_for_pi(base_url) is not None:
        if _route_mode != "pi":
            print("[robot_client] Pi relay reachable -- using it.")
        _route_mode = "pi"
        _route_decided_at = now
        return "pi"

    # Genuinely unreachable -- ask, once, rather than silently bypassing.
    if _prompt_direct_ble_bypass():
        _route_mode = "direct"
        _route_decided_at = now
        return "direct"

    _route_mode = None
    raise RuntimeError(
        "Pi relay unreachable and direct BLE bypass declined. "
        "Fix the Pi (or its network) and try again."
    )


def release_direct_ble() -> None:
    """Drop the PC's own BLE connection so the Pi relay can take over
    again without waiting out the full recheck interval or restarting
    this process."""
    global _route_mode
    _disconnect_local()
    _route_mode = None


def _send(cmd: str, args: dict, timeout: float = 15.0) -> dict:
    route = _resolve_route()

    if route == "pi":
        base_url = _pi_base_url()
        try:
            r = requests.post(
                f"{base_url}/command", json={"cmd": cmd, "args": args}, timeout=timeout
            )

            if r.status_code == 409:
                # Relay is up and reachable, just hasn't finished
                # connecting to the hub itself yet -- wait for that,
                # don't bypass. Bypassing here would have the PC grab
                # the one BLE slot the Pi's background thread is
                # actively trying to acquire, permanently stranding it
                # instead of just being early.
                print(
                    "[robot_client] Pi relay hasn't connected to the hub yet -- "
                    "waiting rather than grabbing BLE out from under it."
                )
                if _wait_for_pi_connected(base_url):
                    r = requests.post(
                        f"{base_url}/command", json={"cmd": cmd, "args": args}, timeout=timeout
                    )
                else:
                    print(
                        "[robot_client] Pi still hasn't connected after waiting -- "
                        "falling back to direct BLE for this command."
                    )
                    route = "direct"

            if route == "pi":
                r.raise_for_status()
                result = r.json()
                result["_route"] = "pi_relay"
                return result
        except requests.RequestException as e:
            print(f"[robot_client] Pi relay call failed mid-flight ({e}), "
                  f"falling back to direct BLE for this command.")
            route = "direct"

    result = dispatch(_local(), cmd, args)
    result["_route"] = "direct_ble"
    return result


# -- public API --------------------------------------------------------
# Mirrors HubController's methods 1:1. If you add a command to
# hub_controller.COMMANDS, add the matching one-liner here too.

def health() -> dict:
    route = _resolve_route()
    if route == "pi":
        base_url = _pi_base_url()
        result = _pi_health_once(base_url, timeout=3)
        if result is not None:
            result["_route"] = "pi_relay"
            return result
        # answered a moment ago in _resolve_route but not now -- treat
        # like any other mid-flight failure and fall through to direct.
    result = _local().health()
    result["_route"] = "direct_ble"
    return result


def stop(port: str = "A") -> dict:
    return _send("stop", {"port": port})


def set_speed(port: str, speed: float) -> dict:
    return _send("set_speed", {"port": port, "speed": speed})


def read_angle(port: str = "A") -> dict:
    return _send("read_angle", {"port": port})


def goto_angle(port: str, target_degrees: float, **kwargs) -> dict:
    return _send("goto_angle", {"port": port, "target_degrees": target_degrees, **kwargs})


# Generic dispatch, mirroring hub_controller.COMMANDS/dispatch() 1:1 --
# same reasoning as that module's own comment: adding a command means
# adding one function above and one line here, nowhere else. Exists so
# callers that receive a command as data (e.g. pc_server.py's /command
# endpoint, taking {"cmd": ..., "args": ...} off the wire) have a single
# entrypoint instead of hand-rolling an if/elif per command name.
COMMANDS = {
    "stop": stop,
    "set_speed": set_speed,
    "read_angle": read_angle,
    "goto_angle": goto_angle,
}


def dispatch(cmd: str, args: dict) -> dict:
    if cmd not in COMMANDS:
        raise ValueError(f"unknown command {cmd!r}, expected one of {list(COMMANDS)}")
    return COMMANDS[cmd](**args)
