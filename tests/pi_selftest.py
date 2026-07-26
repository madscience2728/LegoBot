"""
pi_selftest.py -- run this DIRECTLY ON THE PI, by itself, with the relay
service stopped:

    sudo systemctl stop legobot-relay
    python3 pi_selftest.py

Purpose: answer one question with nothing else in the way -- can THIS
Pi's own Bluetooth radio see and connect to the hub at all? Every prior
attempt went through systemd -> uvicorn -> FastAPI -> hub_controller ->
pylgbst -> bleak, all in a background thread, with a routing decision on
top from the PC. A failure anywhere in that stack looks identical from
the outside. This script strips every layer away except the two that
actually matter: bleak talking to BlueZ, and pylgbst talking to the hub.

Four steps, each one gating the next -- if an early one fails, there's
no point running the later ones:
    0. Is the relay service already running? (it'd fight for the BLE slot)
    1. OS-level Bluetooth: is bluetoothd up, is the radio rfkill-blocked,
       does an hci adapter even exist?
    2. Raw BLE scan via bleak alone -- no pylgbst, no filtering. If this
       finds nothing, the problem is the Pi's BT hardware/driver, full
       stop, and none of the LegoBot code is even relevant yet.
    3. Is the hub specifically among what bleak found?
    4. Full pylgbst connect + one sensor read, using the exact same
       HubController class that already works from the PC -- so success
       here proves the Pi's BT stack is equivalent to the PC's, and
       failure here (with step 2/3 having passed) narrows the problem
       down to pylgbst/BlueZ specifically, not the radio.

Needs (same packages relay_server.py needs -- already installed if
deploy.py has succeeded at least once):
    python3 -m pip install --break-system-packages bleak "pylgbst[bleak]"

Run from the Pi's home directory (~) so `lego_control` is importable as
a sibling folder, same as the relay expects.
"""

import asyncio
import subprocess
import sys


def _step(title: str):
    print(f"\n=== {title} ===")


def check_relay_not_running():
    _step("0. Is legobot-relay already running? (it would fight for the BLE slot)")
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "legobot-relay"],
            capture_output=True, text=True, timeout=5,
        )
        state = (result.stdout or result.stderr).strip()
        print(f"legobot-relay service state: {state or '(unknown)'}")
        if state == "active":
            print(
                "WARNING: legobot-relay is running and may already hold the BLE "
                "connection, or be mid-scan trying to acquire it.\n"
                "Recommended: sudo systemctl stop legobot-relay   (then re-run this script)"
            )
    except Exception as e:
        print(f"Couldn't check legobot-relay state ({e}) -- continuing anyway.")


def check_os_level_bluetooth():
    _step("1. OS-level Bluetooth status")
    checks = [
        ("bluetoothd service", ["systemctl", "is-active", "bluetooth"]),
        ("rfkill (is BT radio blocked?)", ["rfkill", "list", "bluetooth"]),
        ("hci adapter present", ["hciconfig"]),
    ]
    for label, cmd in checks:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            output = (result.stdout or result.stderr).strip() or "(no output)"
            print(f"[{label}]\n{output}\n")
        except FileNotFoundError:
            print(f"[{label}] command not found ({cmd[0]}) -- skipping\n")
        except Exception as e:
            print(f"[{label}] failed to check: {e}\n")


def raw_ble_scan(timeout: float = 6.0) -> dict:
    _step("2. Raw BLE scan (bleak only -- no pylgbst, no filtering)")
    from bleak import BleakScanner

    async def _scan():
        return await BleakScanner.discover(timeout=timeout, return_adv=True)

    devices = asyncio.run(_scan())
    if not devices:
        print(
            "Nothing found at all. This points at the Pi's Bluetooth "
            "hardware/driver itself, not at pylgbst or the hub -- check "
            "the rfkill/hciconfig output above before going any further."
        )
        return devices

    print(f"Found {len(devices)} device(s):\n")
    for address, (device, adv) in devices.items():
        name = device.name or "(no name)"
        print(f"  {name!r:24s} {address}   rssi={adv.rssi}")
    return devices


def find_hub(devices: dict, hub_name: str = "Control+ Hub"):
    _step("3. Looking for the hub specifically")
    for address, (device, adv) in devices.items():
        if device.name == hub_name:
            print(f"Found {hub_name!r} at {address} (rssi={adv.rssi}).")
            return address
    print(f"{hub_name!r} was NOT in the scan results above.")
    print(
        "Either it's off, physically out of range of the Pi specifically "
        "(different antenna/position than the PC), or already connected to "
        "something else (a phone app, the PC bypass, etc)."
    )
    return None


def try_pylgbst_connect(hub_name: str = "Control+ Hub"):
    _step("4. Full pylgbst connect + one sensor read")
    sys.path.insert(0, ".")  # lego_control lives as a sibling dir here
    from lego_control.hub_controller import HubController

    controller = HubController(hub_name=hub_name)
    print("Calling HubController.connect() -- the exact same code path")
    print("that already works from the PC's direct-BLE bypass...\n")
    controller.connect(max_scans=15)  # bounded here, unlike the relay's indefinite retry

    print("\nConnected. Health:", controller.health())

    try:
        angle = controller.read_angle("A", timeout=3.0)
        print("Read angle on port A:", angle)
    except Exception as e:
        print(f"Connected, but reading port A failed: {e}")
        print("(The connection itself is still proven either way -- this is just a port/motor detail.)")

    controller.disconnect()
    print("\nDisconnected cleanly.")


def main():
    check_relay_not_running()
    check_os_level_bluetooth()
    devices = raw_ble_scan()
    address = find_hub(devices)

    if address is None:
        print("\nStopping here -- no point trying pylgbst if bleak itself never saw the hub.")
        print("Turn the hub on, get it in range of the Pi, and re-run this script.")
        return

    try_pylgbst_connect()


if __name__ == "__main__":
    main()
