"""
hub_controller.py -- the ONLY place BLE/pylgbst logic lives.

Both the Pi relay (lego_pi/relay_server.py) and the PC bypass path
(lego_brain/robot_client.py, when the Pi relay isn't reachable) import
THIS SAME CLASS and call THIS SAME CODE. Whichever machine's Bluetooth
radio ends up talking to the hub, the actual command logic is identical --
there is no separate "Pi version" and "PC version" of goto_angle or
set_speed to keep in sync.

This module has zero networking code in it. It doesn't know or care
whether it's being driven by an HTTP handler on the Pi or a direct call
on the PC. That separation is what makes the relay a true 1:1 passthrough
instead of a second implementation.

pip install "pylgbst[bleak]"
"""

import subprocess
import time
from struct import pack

import bleak

# --- bleak compatibility patch (pylgbst calls the old removed bleak.discover) ---
if not hasattr(bleak, "discover"):
    from bleak import BleakScanner

    async def _discover(timeout=5.0, **kwargs):
        return await BleakScanner.discover(timeout=timeout, **kwargs)

    bleak.discover = _discover
# ---------------------------------------------------------------------------

from pylgbst import get_connection_bleak
from pylgbst.hub import Hub

PORTS = {"A": 0x00, "B": 0x01, "C": 0x02, "D": 0x03}


class HubNotReady(RuntimeError):
    """Raised when a command is sent before connect() succeeded, or the
    requested port has no motor attached."""


def _scan_once(timeout: float = 4.0) -> dict:
    """One BLE scan pass. Synchronous wrapper so callers (including the
    relay's plain background thread) don't need to think about asyncio."""
    import asyncio
    from bleak import BleakScanner

    return asyncio.run(BleakScanner.discover(timeout=timeout, return_adv=True))


def _format_devices(devices: dict) -> str:
    if not devices:
        return "  (nothing visible)"
    lines = []
    for address, (device, adv) in devices.items():
        name = device.name or "(no name)"
        lines.append(f"  {name!r:24s} {address}   rssi={adv.rssi}")
    return "\n".join(lines)


def wait_for_hub(hub_name: str, scan_timeout: float = 4.0, max_scans: int = None) -> str:
    """
    Repeatedly scans for BLE devices, printing everything visible on each
    pass, until one named `hub_name` shows up (or max_scans is hit).

    This exists because pylgbst's own connect just silently scans and
    retries internally -- if the hub is off, out of range, or paired to
    something else, you get zero feedback about what's actually out
    there. This surfaces that before handing off to pylgbst, and doubles
    as the "please turn the hub on now" prompt.

    Each scan pass takes ~scan_timeout seconds on its own, which is what
    provides the periodic cadence -- no extra sleep needed between passes.
    """
    print(f"Looking for a hub named {hub_name!r} over BLE.")
    print("Turn the hub on now (press the green button) if you haven't.\n")

    attempt = 0
    consecutive_scan_errors = 0
    while True:
        attempt += 1
        try:
            devices = _scan_once(timeout=scan_timeout)
        except Exception as e:
            # Transient adapter-level errors happen -- most commonly
            # right after the relay starts, if BlueZ hasn't finished
            # registering the adapter over D-Bus yet. Treating this as
            # fatal (the original behavior) meant one bad scan pass
            # permanently killed the whole connect attempt with nothing
            # left to retry it. Retry the scan itself instead; only give
            # up for real if it keeps failing well past what a boot-time
            # blip would explain.
            consecutive_scan_errors += 1
            print(f"[scan #{attempt}] scan itself failed ({e}), retrying...")
            if max_scans is not None and attempt >= max_scans:
                raise HubNotReady(f"BLE scanning kept failing: {e}")
            if consecutive_scan_errors >= 15:
                raise HubNotReady(f"BLE scanning failed {consecutive_scan_errors} times in a row: {e}")
            time.sleep(2.0)
            continue

        consecutive_scan_errors = 0
        print(f"[scan #{attempt}] devices currently visible:")
        print(_format_devices(devices))

        for address, (device, adv) in devices.items():
            if device.name == hub_name:
                print(f"\nFound {hub_name!r} at {address}.\n")
                return address

        if max_scans is not None and attempt >= max_scans:
            raise HubNotReady(f"never saw a hub named {hub_name!r} after {attempt} scan(s)")

        print("...not seen yet, scanning again.\n")


class HubController:
    """
    Wraps a single BLE connection to one Technic Control+ Hub.

    NOTE: BLE only supports one active central connection to the hub at a
    time. Whichever process calls connect() successfully first "owns" the
    hub until it disconnects -- this class doesn't do any arbitration
    itself, that's the caller's (robot_client's) job.
    """

    def __init__(self, hub_name: str = "Control+ Hub"):
        self.hub_name = hub_name
        self._connection = None
        self._hub = None
        self._hub_address = None
        # Persistent angle subscriptions, one per port, kept alive for
        # the life of the connection (see _ensure_angle_subscription).
        self._angle_state: dict = {}
        self._angle_subscribed_ports: set = set()

    # -- lifecycle -----------------------------------------------------

    def connect(
        self,
        wait_for_port: str = "A",
        timeout: float = 5.0,
        max_scans: int = None,
    ) -> None:
        """
        Discover the hub over BLE (printing what's visible each pass so
        connecting is never a silent black box), then hand off to
        pylgbst for the actual protocol-level connection.

        max_scans=None (default) means keep scanning indefinitely --
        appropriate both for an interactive session where a person is
        watching and will flip the hub on, and for the Pi relay's
        background retry thread, which has nowhere else to be.
        """
        self._hub_address = wait_for_hub(self.hub_name, max_scans=max_scans)

        self._connection = get_connection_bleak(hub_name=self.hub_name)
        self._hub = Hub(self._connection)

        # Give the requested port a moment to enumerate so an immediate
        # command right after connect() doesn't race an empty peripherals
        # dict. Not fatal if it never shows up -- individual commands will
        # raise HubNotReady when they actually need that port.
        deadline = time.time() + timeout
        port_code = PORTS.get(wait_for_port.upper())
        while time.time() < deadline:
            if port_code is not None and self._hub.peripherals.get(port_code):
                break
            time.sleep(0.1)

    def disconnect(self) -> None:
        if self._connection is not None:
            try:
                # pylgbst's own disconnect() (BleakDriver.disconnect, in
                # pylgbst/comms/cbleak.py) only sets an internal _abort
                # flag telling its background thread's write-loop to
                # stop -- it never actually calls BleakClient.disconnect()
                # on the real connection, which lives as a local variable
                # buried inside that thread's closure with no way to
                # reach it from here. The practical effect: calling this
                # leaves the GATT connection open at the BlueZ level
                # (the hub's LED stays solid) until it eventually times
                # out on its own, which can take a while.
                self._connection.disconnect()
            except Exception:
                pass
        self._force_ble_disconnect()
        self._connection = None
        self._hub = None
        self._angle_state = {}
        self._angle_subscribed_ports = set()

    def _force_ble_disconnect(self) -> None:
        """Best-effort real disconnect at the BlueZ level, working
        around the pylgbst limitation described in disconnect() above.
        Uses bluetoothctl directly by address rather than relying on
        pylgbst to expose (it doesn't) a way to reach the actual
        BleakClient. Safe to call even if there's nothing to disconnect --
        bluetoothctl just no-ops on an address that isn't connected."""
        if not self._hub_address:
            return
        try:
            subprocess.run(
                ["bluetoothctl", "disconnect", self._hub_address],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            # Not fatal -- worst case the connection times out on its
            # own the way it always did before this fix existed.
            pass

    @property
    def connected(self) -> bool:
        return self._hub is not None

    def health(self) -> dict:
        return {
            "connected": self.connected,
            "hub_name": self.hub_name,
            "ports_attached": [
                name for name, code in PORTS.items()
                if self.connected and self._hub.peripherals.get(code) is not None
            ] if self.connected else [],
        }

    # -- internal --------------------------------------------------------

    def _motor(self, port: str):
        if not self.connected:
            raise HubNotReady("hub is not connected")
        code = PORTS.get(port.upper())
        if code is None:
            raise HubNotReady(f"unknown port {port!r}, expected one of {list(PORTS)}")
        motor = self._hub.peripherals.get(code)
        if motor is None:
            raise HubNotReady(f"no motor attached on port {port}")
        return motor

    def _ensure_angle_subscription(self, port: str, wait_timeout: float = 3.0) -> dict:
        """
        Subscribe to a port's angle sensor exactly once, and leave it
        subscribed for the life of the connection. Returns the shared
        state dict, which the callback keeps updating in the background
        as the hub pushes new readings.

        This exists because re-subscribing to a mode right after
        unsubscribing from it isn't reliable: if the value hasn't
        changed since the last reading, the hub has no reason to push a
        fresh notification, so a subscribe -> unsubscribe -> subscribe
        cycle (e.g. calling read_angle right before goto_angle) can hang
        waiting for a callback that never comes. Subscribing once and
        reading the live-updated dict sidesteps that entirely.
        """
        motor = self._motor(port)
        state = self._angle_state.setdefault(port, {"angle": None})

        if port not in self._angle_subscribed_ports:
            def on_angle(angle, _state=state):
                _state["angle"] = angle

            motor.subscribe(on_angle, mode=motor.SENSOR_ANGLE)
            self._angle_subscribed_ports.add(port)

            deadline = time.time() + wait_timeout
            while state["angle"] is None and time.time() < deadline:
                time.sleep(0.01)

        return state

    @staticmethod
    def _start_speed_nonblocking(motor, speed, max_power=1.0, use_profile=0b11):
        """Same wire command as motor.start_speed(), sent with
        wait_complete=False -- see tests/goto_angle_closed_loop.py for why
        the stock blocking call doesn't work for a tight control loop."""
        params = b""
        params += pack("<b", motor._speed_abs(speed))
        params += pack("<B", int(100 * max_power))
        params += pack("<B", use_profile)
        motor._send_cmd(motor.SUBCMD_START_SPEED, params, wait_complete=False)

    # -- commands --------------------------------------------------------
    # Each of these is a self-contained unit of work: one dict in, one
    # dict out. That's deliberate -- it's what lets relay_server.py and
    # robot_client.py both dispatch on the same {"cmd": ..., "args": ...}
    # shape without any translation layer in between.

    def stop(self, port: str = "A") -> dict:
        motor = self._motor(port)
        motor.stop()
        return {"ok": True, "port": port}

    def set_speed(self, port: str, speed: float) -> dict:
        """Continuous speed, -1.0..1.0. Runs until the next stop/command."""
        motor = self._motor(port)
        self._start_speed_nonblocking(motor, speed)
        return {"ok": True, "port": port, "speed": speed}

    def read_angle(self, port: str = "A", timeout: float = 2.0) -> dict:
        state = self._ensure_angle_subscription(port, wait_timeout=timeout)
        if state["angle"] is None:
            raise HubNotReady(f"no angle data from encoder on port {port}")
        return {"port": port, "angle": state["angle"]}

    def goto_angle(
        self,
        port: str,
        target_degrees: float,
        tolerance: float = 2.0,
        kp: float = 0.012,
        min_speed: float = 0.16,
        max_speed: float = 0.6,
        settle_reads: int = 5,
        poll_interval: float = 0.02,
        timeout: float = 6.0,
    ) -> dict:
        """Closed-loop move, ported from tests/goto_angle_closed_loop.py.
        See that file's docstring for why this exists instead of the
        stock motor.goto_position()."""
        motor = self._motor(port)
        state = self._ensure_angle_subscription(port, wait_timeout=2.0)
        if state["angle"] is None:
            raise HubNotReady(f"no angle data from encoder on port {port}")

        consecutive_in_tolerance = 0
        start_time = time.time()
        settled = False

        try:
            while True:
                if time.time() - start_time > timeout:
                    break

                error = target_degrees - state["angle"]

                if abs(error) <= tolerance:
                    consecutive_in_tolerance += 1
                    if consecutive_in_tolerance >= settle_reads:
                        settled = True
                        break
                    speed = min_speed * (1 if error >= 0 else -1) * 0.5
                else:
                    consecutive_in_tolerance = 0
                    raw_speed = kp * error
                    magnitude = min(max(abs(raw_speed), min_speed), max_speed)
                    speed = magnitude if error > 0 else -magnitude

                self._start_speed_nonblocking(motor, speed)
                time.sleep(poll_interval)
        finally:
            # Deliberately NOT unsubscribing here -- the angle
            # subscription is shared and persistent (see
            # _ensure_angle_subscription); tearing it down after every
            # move is what caused the original bug.
            motor.stop()

        final_angle = state["angle"]
        return {
            "ok": True,
            "port": port,
            "settled": settled,
            "final_angle": final_angle,
            "target": target_degrees,
            "error": target_degrees - final_angle,
        }


# Single dispatch table shared by relay_server.py (HTTP -> dict -> here)
# and robot_client.py (direct call -> dict -> here). Adding a new command
# means adding one method above and one line here -- nowhere else.
COMMANDS = {
    "stop": HubController.stop,
    "set_speed": HubController.set_speed,
    "read_angle": HubController.read_angle,
    "goto_angle": HubController.goto_angle,
}


def dispatch(controller: HubController, cmd: str, args: dict) -> dict:
    if cmd not in COMMANDS:
        raise ValueError(f"unknown command {cmd!r}, expected one of {list(COMMANDS)}")
    return COMMANDS[cmd](controller, **args)
