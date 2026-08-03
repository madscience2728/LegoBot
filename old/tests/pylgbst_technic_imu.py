"""
Technic Control+ Hub: motor + built-in IMU (accelerometer & gyro) over BLE.

pylgbst has no built-in classes for this hub's internal accelerometer/gyro
(it falls back to a generic Peripheral that just returns raw, undecoded
bytes). The classes below add real decoding, using scale factors confirmed
against bricknil's own LWP3 mode definitions for these exact device types:

  Accelerometer (device 0x39): 3x int16, full range +/-8000 mG -> g = raw/4096.0
  Gyro          (device 0x3a): 3x int16, full range +/-2000 dps -> dps = raw*0.07

pip install "pylgbst[bleak]"
"""

import struct
import time
import bleak

# --- bleak compatibility patch (see earlier script for why) ---
if not hasattr(bleak, "discover"):
    from bleak import BleakScanner

    async def _discover(timeout=5.0, **kwargs):
        return await BleakScanner.discover(timeout=timeout, **kwargs)

    bleak.discover = _discover
# ----------------------------------------------------------------

from pylgbst import get_connection_bleak
from pylgbst.hub import Hub, DevTypes, PERIPHERAL_TYPES
from pylgbst.peripherals import Peripheral

HUB_NAME = "Control+ Hub"
PORT_A = 0x00


class TechnicHubAccelerometer(Peripheral):
    MODE_GRAVITY = 0x00  # only mode with real accel data; other mode is calibration

    def subscribe(self, callback, mode=MODE_GRAVITY, granularity=1):
        super().subscribe(callback, mode, granularity)

    def _decode_port_data(self, msg):
        x, y, z = struct.unpack("<hhh", msg.payload[0:6])
        g = 1 / 4096.0
        return x * g, y * g, z * g  # (x, y, z) in units of standard gravity


class TechnicHubGyro(Peripheral):
    MODE_ROTATION = 0x00

    def subscribe(self, callback, mode=MODE_ROTATION, granularity=1):
        super().subscribe(callback, mode, granularity)

    def _decode_port_data(self, msg):
        x, y, z = struct.unpack("<hhh", msg.payload[0:6])
        scale = 0.07
        return x * scale, y * scale, z * scale  # (x, y, z) in deg/sec


# Register the new classes so pylgbst instantiates them automatically
# instead of the generic fallback Peripheral, the moment it sees these
# device types attach.
PERIPHERAL_TYPES[DevTypes.TECHNIC_MEDIUM_HUB_ACCELEROMETER] = TechnicHubAccelerometer
PERIPHERAL_TYPES[DevTypes.TECHNIC_MEDIUM_HUB_GYRO_SENSOR] = TechnicHubGyro


def main():
    print(f"Scanning for a hub named {HUB_NAME!r} over BLE...")
    connection = get_connection_bleak(hub_name=HUB_NAME)
    hub = Hub(connection)

    print("Waiting for peripherals to attach...")
    motor = accel = gyro = None
    for i in range(100):  # ~10 seconds
        motor = motor or hub.peripherals.get(PORT_A)
        for p in hub.peripherals.values():
            if isinstance(p, TechnicHubAccelerometer):
                accel = p
            elif isinstance(p, TechnicHubGyro):
                gyro = p
        if motor and accel and gyro:
            break
        time.sleep(0.1)

    print(f"Raw peripherals dict after waiting: {hub.peripherals}")

    print(f"motor={motor}  accel={accel}  gyro={gyro}")

    if accel:
        accel.subscribe(lambda x, y, z: print(f"accel g: x={x:+.2f} y={y:+.2f} z={z:+.2f}"))
    if gyro:
        gyro.subscribe(lambda x, y, z: print(f"gyro dps: x={x:+7.1f} y={y:+7.1f} z={z:+7.1f}"))

    print("Streaming for 60s -- slowly rotate the hub to rest on each of its")
    print("6 faces in turn and note which axis reads ~+1.00 or ~-1.00 g.")
    print("Whichever axis/sign is showing ~1g is the one pointing straight down.")
    print("(Ctrl+C any time to stop early)")
    try:
        time.sleep(60)
    except KeyboardInterrupt:
        pass

    if accel:
        accel.unsubscribe()
    if gyro:
        gyro.unsubscribe()


if __name__ == "__main__":
    main()
