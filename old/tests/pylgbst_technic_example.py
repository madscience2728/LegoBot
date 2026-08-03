"""
pylgbst example: control a LEGO Technic Control+ Hub directly from your PC
over Bluetooth (stock firmware, no flashing, no bricknil).

INSTALL (bleak is an *optional* extra for pylgbst — it is NOT pulled in by
plain `pip install pylgbst`, you must ask for it explicitly):

    pip install "pylgbst[bleak]"

Note: pylgbst has no dedicated "Technic Hub" class (only MoveHub for Boost,
and SmartHub for the 88009 handset) — but its generic Hub base class
auto-discovers whatever's attached to each port regardless of hub model, so
it works fine here. You just address ports by number (0=A, 1=B, 2=C, 3=D)
via hub.peripherals instead of named attributes like hub.motor_A.

Turn your hub on (button) before running this.
"""

import time
import asyncio
import bleak

# --- Compatibility patch -----------------------------------------------
# pylgbst's bleak driver calls `bleak.discover(...)`, which was a real
# module-level function in old bleak but was removed years ago in favor of
# `BleakScanner.discover(...)`. pylgbst hasn't been updated for this, so we
# patch it back in here rather than editing site-packages.
if not hasattr(bleak, "discover"):
    from bleak import BleakScanner

    async def _discover(timeout=5.0, **kwargs):
        return await BleakScanner.discover(timeout=timeout, **kwargs)

    bleak.discover = _discover
# -------------------------------------------------------------------------

from pylgbst import get_connection_bleak
from pylgbst.hub import Hub

PORT_A = 0x00  # Technic hub ports: A=0, B=1, C=2, D=3


HUB_NAME = "Control+ Hub"  # factory default name for the Technic Control+ Hub


def main():
    print(f"Scanning for a hub named {HUB_NAME!r} over BLE...")
    # pylgbst requires either hub_name or hub_mac -- passing neither raises
    # an AssertionError once it actually gets to matching a device.
    connection = get_connection_bleak(hub_name=HUB_NAME)
    hub = Hub(connection)

    print("Waiting for a motor to show up on port A...")
    motor = None
    for _ in range(50):  # ~5 seconds
        motor = hub.peripherals.get(PORT_A)
        if motor is not None:
            break
        time.sleep(0.1)

    if motor is None:
        print("No device detected on port A — check the cable/port.")
        return

    print(f"Found: {motor}")

    # Optional: subscribe to angle updates (mode=2 is SENSOR_ANGLE for
    # EncodedMotor; pylgbst calls your callback whenever it changes).
    def on_angle_change(angle):
        print(f"angle: {angle}")

    motor.subscribe(on_angle_change, mode=motor.SENSOR_ANGLE)

    print("Ramping to full speed for 1.5s...")
    motor.timed(1.5, speed_primary=0.6)  # speed is -1.0..1.0, not -100..100

    print("Going to an absolute position (90 degrees)...")
    motor.goto_position(90, speed=0.3)

    print("Stopping.")
    motor.stop()

    motor.unsubscribe(on_angle_change)


if __name__ == "__main__":
    main()
