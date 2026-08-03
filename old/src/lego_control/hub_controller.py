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
from struct import pack, unpack

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


def _force_adapter_reset() -> None:
    """Recovery for a specific bleak/BlueZ failure mode: if BleakScanner's
    internal start() sends StartDiscovery over D-Bus but then raises
    during its OWN post-start setup (before __aenter__ returns), the
    `async with` block that's supposed to guarantee stop()/StopDiscovery
    never gets entered -- __aexit__ only runs if __aenter__ succeeded.
    BlueZ is left thinking discovery is running, forever, and every
    _scan_once() call after that fails immediately with
    org.bluez.Error.InProgress.

    This used to call `bluetoothctl scan off`, then `bluetoothctl power
    off` -> `power on`. Neither was enough -- confirmed in production
    that a `power off` request can itself get REJECTED by bluetoothd:
        bluetoothctl: Failed to set power off: org.bluez.Error.Failed
    bluetoothctl talks to bluetoothd over D-Bus, and when the daemon is
    wedged badly enough it can't even process the request meant to
    unwedge it -- asking the stuck thing to fix itself, through the
    interface that's stuck. `power on` then "succeeds" trivially (the
    adapter's already on) without having cleared anything.

    What's confirmed to actually work: a full `sudo reboot`, which kills
    and restarts the bluetoothd PROCESS, giving it genuinely fresh
    in-memory state rather than asking it nicely. Restarting just
    bluetooth.service does the same thing -- a fresh daemon process,
    same as the systemd fix in
    robot_files/legobot-relay.service's ExecStartPre -- without a whole
    OS reboot. Requires root (fine on the Pi, where this runs as root
    under systemd; best-effort/silently-caught if this ever runs
    unprivileged via the PC bypass path in robot_client.py).

    Safe to call here: we only reach this path while still scanning,
    i.e. before connect() has succeeded, so there's no live hub
    connection to drop.

    Same subprocess-bluetoothctl style as HubController._force_ble_disconnect,
    for the same underlying reason: bleak/pylgbst don't expose a way to
    do this from inside the failed call itself."""
    try:
        subprocess.run(
            ["systemctl", "restart", "bluetooth.service"],
            capture_output=True, text=True, timeout=10,
        )
        time.sleep(2.0)
        subprocess.run(
            ["bluetoothctl", "power", "on"],
            capture_output=True, text=True, timeout=5,
        )
        time.sleep(1.0)
    except Exception:
        # Not fatal -- worst case this particular retry still hits
        # InProgress and we try again next pass.
        pass


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
            _force_adapter_reset()
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
        # Same idea, but for the motor's TRUE magnet-based absolute
        # position (mode 3, "APOS") -- see _ensure_apos_subscription and
        # home_angle. Confirmed via describe_port against the actual
        # 88017 Large Angular Motor: 16-bit signed, range -180..179.
        self._apos_state: dict = {}
        self._apos_subscribed_ports: set = set()

    # This motor's true magnet-based absolute position mode, confirmed
    # via describe_port's live query of the actual device (Mode 3,
    # "APOS", 16-bit signed, -180..179 degrees) -- NOT pylgbst's
    # SENSOR_ANGLE (mode 2, "POS"), which is a 32-bit RELATIVE counter
    # that doesn't persist across reconnects.
    APOS_MODE = 3

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

    def describe_port(self, port: str, max_mode: int = 7) -> dict:
        """Diagnostic only -- queries the LIVE device for its actual mode
        table (names, byte widths, ranges), needed before reading a
        non-standard mode directly: pylgbst's built-in EncodedMotor only
        knows how to parse mode 2 (SENSOR_ANGLE, its "POS"/relative
        position, 4-byte int) -- everything else falls through to a
        1-byte parse, which would silently misread mode 3 (APOS, this
        motor's true magnet-based absolute position -- see
        preset_zero's docstring and the 88017 Large Angular Motor's
        spec sheet). Rather than assume APOS's exact wire width and
        risk shipping code that parses garbage without any error, ask
        the actual connected device what its mode 3 really looks like,
        once, and use that ground truth.

        Deliberately NOT pylgbst's own describe_possible_modes() --
        that brute-forces mode numbers 0-255, each a separate BLE
        round-trip, which blew straight through every timeout in the
        chain (robot_client's 15s to the Pi, call_server.py's 20s on
        top of that) for a motor that only has a handful of real modes.
        Same underlying per-mode query (Peripheral._describe_mode),
        just over a small range instead of all 256."""
        motor = self._motor(port)
        return {"port": port, "modes": [motor._describe_mode(m) for m in range(max_mode)]}

    def _switch_mode_and_subscribe(self, motor, mode: int, callback) -> None:
        """Shared by _ensure_angle_subscription (mode 2, POS) and
        _ensure_apos_subscription (mode 3, APOS) -- pylgbst only
        supports one active subscription mode per port at a time
        (Peripheral.subscribe() raises ValueError if you try to switch
        modes while a callback from the OTHER mode is still registered).
        That's a real constraint here: this project reads a port in
        mode 2 via goto_angle/read_angle and in mode 3 via
        read_apos/home_angle, and a given port might switch between
        them across a session (e.g. try goto_angle first, then switch
        to the APOS-based approach once that proves unreliable for a
        bounded servo -- exactly what happened during development).

        Bypasses motor.subscribe()'s guard on purpose: clears whatever
        was subscribed before (stale callbacks from the other mode
        would otherwise silently keep receiving data under a
        misleading key, e.g. mode-2's on_angle receiving mode-3 apos
        values), then calls set_port_mode directly -- the actual mode
        switch underneath that wrapper, just without the restriction
        that doesn't fit "one port, two modes, different times."
        """
        motor._subscribers.clear()
        motor.set_port_mode(mode, True, 1)
        motor._subscribers.add(callback)

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

        Reads mode 2 ("POS") -- a 32-bit RELATIVE counter, not this
        motor's true absolute position. Fine for continuously-rotating
        motors (wheels) that don't have real absolute sensing anyway.
        For a bounded servo with genuine magnet-based absolute
        positioning, see read_apos/home_angle instead -- goto_angle
        built on THIS mode is what needed manual re-calibration every
        session; that limitation is inherent to mode 2, not fixable
        here.
        """
        motor = self._motor(port)
        state = self._angle_state.setdefault(port, {"angle": None})

        if port not in self._angle_subscribed_ports:
            def on_angle(angle, _state=state):
                _state["angle"] = angle

            self._switch_mode_and_subscribe(motor, motor.SENSOR_ANGLE, on_angle)
            self._angle_subscribed_ports.add(port)
            self._apos_subscribed_ports.discard(port)  # mode-3 subscription (if any) no longer valid

            deadline = time.time() + wait_timeout
            while state["angle"] is None and time.time() < deadline:
                time.sleep(0.01)

        return state

    @staticmethod
    def _patch_apos_decoding(motor) -> None:
        """pylgbst's EncodedMotor._decode_port_data() only knows how to
        parse mode 2 (4-byte int) and mode 1 (1-byte int) -- for mode 3
        (APOS) it hits an unhandled else branch and returns an empty
        tuple, so a callback subscribed to mode 3 through pylgbst's
        normal API silently never fires at all, no error raised.

        Patches THIS motor instance (not the class -- other motors on
        other ports are unaffected) to also decode mode 3 correctly,
        alongside the original logic for every other mode. Format
        confirmed via describe_port's live query of the actual device:
        16-bit signed, matching APOS's real -180..179 degree range
        (a signed byte, pylgbst's fallback width, can't even represent
        that range -- would silently truncate/wrap)."""
        if getattr(motor, "_apos_decode_patched", False):
            return
        original_decode = motor._decode_port_data

        def patched_decode(msg, _original=original_decode, _motor=motor):
            if _motor._port_mode.mode == HubController.APOS_MODE:
                apos = unpack("<h", msg.payload[0:2])[0]
                return (apos,)
            return _original(msg)

        motor._decode_port_data = patched_decode
        motor._apos_decode_patched = True

    def _ensure_apos_subscription(self, port: str, wait_timeout: float = 3.0) -> dict:
        """Same idea as _ensure_angle_subscription, but for the motor's
        TRUE magnet-based absolute position (mode 3, "APOS") instead of
        the relative "POS" mode (2) pylgbst's SENSOR_ANGLE reads.

        This is the mode that doesn't need calibrating every session --
        APOS is tied to a physical magnet, not a counter that starts
        wherever tracking happens to begin. See read_apos/home_angle."""
        motor = self._motor(port)
        state = self._apos_state.setdefault(port, {"apos": None})

        if port not in self._apos_subscribed_ports:
            self._patch_apos_decoding(motor)

            def on_apos(apos, _state=state):
                _state["apos"] = apos

            self._switch_mode_and_subscribe(motor, self.APOS_MODE, on_apos)
            self._apos_subscribed_ports.add(port)
            self._angle_subscribed_ports.discard(port)  # mode-2 subscription (if any) no longer valid

            deadline = time.time() + wait_timeout
            while state["apos"] is None and time.time() < deadline:
                time.sleep(0.01)

        return state

    def read_apos(self, port: str = "A", timeout: float = 2.0) -> dict:
        """The motor's TRUE magnet-based absolute position. Unlike
        read_angle (mode 2, relative), this needs no calibration --
        ever -- since it's tied to a physical magnet position, not a
        counter that resets to "wherever tracking started" each
        session."""
        state = self._ensure_apos_subscription(port, wait_timeout=timeout)
        if state["apos"] is None:
            raise HubNotReady(f"no APOS data from encoder on port {port}")
        return {"port": port, "apos": state["apos"]}

    def home_angle(
        self,
        port: str,
        target_degrees: float = 0.0,
        speed: float = 0.5,
        max_power: float = 1.0,
        invert: bool = False,
        timeout: float = 6.0,
    ) -> dict:
        """Moves to a target angle using the motor's TRUE absolute
        position (APOS) as the reference -- no manual calibration
        needed, ever, since APOS is magnet-based and survives
        reconnects/power cycles. This is the actual fix for "servo
        needs to find 0 on its own."

        GotoAbsolutePosition (pylgbst's goto_position / this project's
        own set_position) does NOT honor APOS despite its name --
        confirmed independently: it's aligned with the relative POS
        counter instead (see set_position's docstring for the source).
        So this reads the current TRUE position, computes the needed
        RELATIVE move (shortest path across the -180/180 wraparound),
        and sends that via angled() -- a genuinely relative rotation
        command -- rather than trusting GotoAbsolutePosition with a
        target it doesn't actually respect.

        target_degrees should stay within roughly -170..170. APOS
        itself is hard-bounded to -180..179 (this motor's real
        mechanical/sensor range, confirmed via describe_port) -- there's
        no "multiple turns" concept here the way the old POS-based
        goto_angle assumed, which is what let it accumulate thousands of
        degrees of nonsensical drift in the first place.
        """
        state = self._ensure_apos_subscription(port, wait_timeout=2.0)
        if state["apos"] is None:
            raise HubNotReady(f"no APOS data from encoder on port {port}")

        current = state["apos"]
        delta = target_degrees - current
        if delta > 180:
            delta -= 360
        elif delta < -180:
            delta += 360

        motor = self._motor(port)
        actual_delta = -delta if invert else delta
        motor.angled(actual_delta, speed_primary=speed, max_power=max_power, wait_complete=True)

        state = self._ensure_apos_subscription(port, wait_timeout=timeout)
        final_apos = state["apos"]
        return {
            "ok": True,
            "port": port,
            "target": target_degrees,
            "final_apos": final_apos,
        }

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

    def set_speed(self, port: str, speed: float, invert: bool = False) -> dict:
        """Continuous speed, -1.0..1.0. Runs until the next stop/command.

        invert: some motors are mounted with their physical rotation
        direction reversed relative to the software's positive-speed
        convention -- this robot has several (see relay_server.py's
        wheel/port map). Rather than making every caller remember to
        negate `speed` for those specific ports, invert=True does it
        here, once, so the port's quirk lives next to the port instead
        of scattered across every call site that happens to know about
        it."""
        motor = self._motor(port)
        actual_speed = -speed if invert else speed
        self._start_speed_nonblocking(motor, actual_speed)
        return {"ok": True, "port": port, "speed": speed, "invert": invert}

    def read_angle(self, port: str = "A", timeout: float = 2.0) -> dict:
        state = self._ensure_angle_subscription(port, wait_timeout=timeout)
        if state["angle"] is None:
            raise HubNotReady(f"no angle data from encoder on port {port}")
        return {"port": port, "angle": state["angle"]}

    def preset_zero(self, port: str) -> dict:
        """Recalibrates this motor's encoder so its CURRENT physical
        position becomes angle 0, going forward. Necessary any time a
        motor's raw zero reference doesn't match its actual physical
        neutral -- e.g. a Technic angular servo whose dots-aligned
        (true mechanical center) position reads as 180 instead of 0.
        Every goto_angle() target is computed relative to whatever the
        encoder currently calls "0", so with an uncalibrated offset like
        that, targets end up pointed at physically out-of-range
        positions -- which looks exactly like a jammed motor (spins/
        vibrates, doesn't move) when it's actually just straining
        against a real mechanical end-stop it was never going to reach.

        IMPORTANT: call this ONLY while the motor is actually sitting at
        the physical position you want to become "0" (e.g. the servo's
        alignment dots lined up). Calling it anywhere else bakes in a
        wrong reference just as surely as the one it's meant to fix.

        Wraps pylgbst's EncodedMotor.preset_encoder() -- see
        https://lego.github.io/lego-ble-wireless-protocol-docs/index.html#output-sub-command-presetencoder-position-n-a
        This is a firmware-level command; the hub remembers the new
        zero itself, not just this process's in-memory state.
        """
        motor = self._motor(port)
        motor.preset_encoder(degrees=0)
        # Angle subscription state can lag a beat behind the hardware
        # actually re-zeroing -- read back post-calibration so the
        # caller sees confirmation, not a stale pre-calibration value.
        state = self._ensure_angle_subscription(port, wait_timeout=2.0)
        return {"ok": True, "port": port, "angle_after_preset": state["angle"]}

    def set_position(
        self,
        port: str,
        target_degrees: float,
        speed: float = 1.0,
        max_power: float = 1.0,
        invert: bool = False,
        timeout: float = 6.0,
    ) -> dict:
        """Firmware-level absolute move, using pylgbst's native
        EncodedMotor.goto_position() instead of this project's own
        hand-rolled goto_angle() P-loop.

        goto_angle() exists specifically because it lets you inspect
        encoder feedback and intervene mid-move (see its own docstring,
        and tests/goto_angle_closed_loop.py) -- valuable for the drive
        wheels, but goto_angle is P-only: proportional term, no
        derivative/damping, no smooth deceleration ramp. That's a
        textbook recipe for exactly the overshoot/hunting oscillation
        this project hit tuning a bounded tilt servo by hand (bounces
        between two angles, doesn't settle) -- a well-known limitation
        of P-only control, not a pylgbst bug.

        set_position hands the move to the hub's OWN onboard
        acceleration/deceleration profile instead (the "use_profile"
        wire parameter) -- LEGO's firmware handles ramping smoothly, no
        PC-side polling loop needed. Right tool for "go to this angle
        and stop", which is all a tilt servo needs; goto_angle remains
        the right tool where mid-move encoder intervention actually
        matters.

        invert: same meaning as set_speed/goto_angle's -- negates what's
        sent to the motor, not target_degrees itself.

        https://lego.github.io/lego-ble-wireless-protocol-docs/index.html#output-sub-command-gotoabsoluteposition-abspos-speed-maxpower-endstate-useprofile-0x0d
        """
        motor = self._motor(port)
        actual_target = -target_degrees if invert else target_degrees
        motor.goto_position(
            actual_target,
            speed=speed,
            max_power=max_power,
            wait_complete=True,
        )
        state = self._ensure_angle_subscription(port, wait_timeout=timeout)
        final_angle = state["angle"]
        return {
            "ok": True,
            "port": port,
            "target": target_degrees,
            "final_angle": final_angle,
        }

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
        invert: bool = False,
    ) -> dict:
        """Closed-loop move, ported from tests/goto_angle_closed_loop.py.
        See that file's docstring for why this exists instead of the
        stock motor.goto_position().

        invert: same meaning as set_speed's -- some motors' physical
        rotation is reversed relative to software's positive-speed
        convention. Deliberately only flips the sign of what's actually
        sent to the motor (see the one line below) -- target_degrees,
        error, and final_angle all stay in normal, non-inverted terms
        for the caller. The closed loop still converges correctly
        either way, since error is computed from the encoder's own
        self-consistent readings, not from anything invert touches."""
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

                self._start_speed_nonblocking(motor, -speed if invert else speed)
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
    "preset_zero": HubController.preset_zero,
    "describe_port": HubController.describe_port,
    "read_apos": HubController.read_apos,
    "home_angle": HubController.home_angle,
    "set_position": HubController.set_position,
    "goto_angle": HubController.goto_angle,
}


def dispatch(controller: HubController, cmd: str, args: dict) -> dict:
    if cmd not in COMMANDS:
        raise ValueError(f"unknown command {cmd!r}, expected one of {list(COMMANDS)}")
    return COMMANDS[cmd](controller, **args)
