"""
Closed-loop angle control for a LEGO Technic Control+ motor over BLE (pylgbst).

Why this exists: motor.goto_position() hands control to the hub's own onboard
profile and you just wait for it to finish -- you don't get to inspect
encoder feedback or intervene mid-move. This script does it ourselves:
drive at a speed proportional to the remaining error, watch the encoder via
subscribe(), and keep correcting (including reversing on overshoot) until
we're settled inside a tolerance band.

pip install "pylgbst[bleak]"
"""

import time
import bleak

# --- bleak compatibility patch (same as the other scripts) ---
if not hasattr(bleak, "discover"):
    from bleak import BleakScanner

    async def _discover(timeout=5.0, **kwargs):
        return await BleakScanner.discover(timeout=timeout, **kwargs)

    bleak.discover = _discover
# ---------------------------------------------------------------

from struct import pack
from pylgbst import get_connection_bleak
from pylgbst.hub import Hub

HUB_NAME = "Control+ Hub"
PORT_A = 0x00


def start_speed_nonblocking(motor, speed, max_power=1.0, use_profile=0b11):
    """
    Same wire command as motor.start_speed(), but sent with wait_complete=False.

    motor.start_speed() hardcodes wait_complete=True, and a continuous "start
    speed" command never gets a "completed" status back from the hub (only
    finite commands like timed()/goto_position() do) -- so the stock call
    just blocks forever waiting for a reply that's never coming. For a tight
    control loop we need each speed update to return immediately once the
    hub acknowledges it's "in progress", not wait for completion.
    """
    params = b""
    params += pack("<b", motor._speed_abs(speed))
    params += pack("<B", int(100 * max_power))
    params += pack("<B", use_profile)
    motor._send_cmd(motor.SUBCMD_START_SPEED, params, wait_complete=False)


def goto_angle_closed_loop(
    motor,
    target_degrees,
    tolerance=2.0,       # +/- degrees considered "close enough"
    kp=0.012,            # speed per degree of error (tune this first)
    min_speed=0.16,      # floor speed to overcome static friction (tune second)
    max_speed=0.6,       # cap so it never slams at full power
    settle_reads=5,      # consecutive in-tolerance reads required to call it done
    poll_interval=0.02,  # seconds between control-loop iterations
    timeout=6.0,         # give up after this long (stalled motor, bad cable, etc.)
):
    """
    Drive `motor` to `target_degrees` using proportional speed control off
    the live encoder angle, correcting for both undershoot and overshoot.
    Returns True if it settled within tolerance, False on timeout.
    """
    state = {"angle": None}

    def on_angle(angle):
        state["angle"] = angle

    motor.subscribe(on_angle, mode=motor.SENSOR_ANGLE)

    # Wait for the first real reading before we start commanding speed.
    start_wait = time.time()
    while state["angle"] is None:
        if time.time() - start_wait > 2.0:
            motor.unsubscribe(on_angle)
            raise RuntimeError("No angle data from encoder -- is it subscribed/attached?")
        time.sleep(0.01)

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
                # Still call it "close": creep at minimum speed rather than
                # coasting, so small drift gets corrected instead of ignored.
                speed = min_speed * (1 if error >= 0 else -1) * 0.5
            else:
                consecutive_in_tolerance = 0
                raw_speed = kp * error
                # Clamp to max, but never let it fall below the floor speed
                # (in the commanded direction) or the motor just stalls.
                magnitude = min(max(abs(raw_speed), min_speed), max_speed)
                speed = magnitude if error > 0 else -magnitude

            start_speed_nonblocking(motor, speed)
            time.sleep(poll_interval)

    finally:
        motor.stop()
        motor.unsubscribe(on_angle)

    final_error = target_degrees - state["angle"]
    print(f"{'Settled' if settled else 'Timed out'} at {state['angle']:.1f} deg "
          f"(target {target_degrees}, error {final_error:+.1f} deg)")
    return settled


def main():
    print(f"Scanning for a hub named {HUB_NAME!r} over BLE...")
    connection = get_connection_bleak(hub_name=HUB_NAME)
    hub = Hub(connection)

    print("Waiting for a motor to show up on port A...")
    motor = None
    for _ in range(50):
        motor = hub.peripherals.get(PORT_A)
        if motor is not None:
            break
        time.sleep(0.1)

    if motor is None:
        print("No device detected on port A -- check the cable/port.")
        return

    print(f"Found: {motor}")

    print("Closed-loop move to 90 degrees...")
    goto_angle_closed_loop(motor, 90)

    print("Closed-loop move to -45 degrees...")
    goto_angle_closed_loop(motor, -45)


if __name__ == "__main__":
    main()
