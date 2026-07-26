"""
Bare-bones BLE scan, no pylgbst involved. Run this first to check whether
your PC can see the hub at the Bluetooth layer at all, before troubleshooting
anything LEGO-specific.

pip install bleak
"""

import asyncio
from bleak import BleakScanner


async def main():
    print("Scanning for 10 seconds... (press the green button on the hub now)")
    devices = await BleakScanner.discover(timeout=10, return_adv=True)

    if not devices:
        print("Nothing found at all — this points to the adapter/dongle, not the hub or the code.")
        return

    print(f"Found {len(devices)} device(s):\n")
    for address, (device, adv) in devices.items():
        name = device.name or "(no name)"
        print(f"  {name!r:20s}  {address}   rssi={adv.rssi}")


if __name__ == "__main__":
    asyncio.run(main())
