"""
PC-side sanity check for the LegoBot pipeline -- exercises robot_client
directly (not raw HTTP) so it tests the *whole* routing decision, not
just one path. Watch the "_route" field in the output: it tells you
whether the Pi relay or direct BLE bypass actually handled each call.

Usage:
    set LEGOBOT_PI_HOST=legobot.local   (or export on mac/linux)
    python3 scripts/pc_test_client.py

Normally launched via `python3 main.py` at the repo root, which puts
src/ on sys.path first -- this file's own sys.path handling below only
matters if you run it standalone.

If LEGOBOT_PI_HOST is unset or the Pi is unreachable, this will fall
straight into direct-BLE bypass mode on the PC -- that's expected
behavior, not a bug, per the fallback design.
"""

import os
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from lego_brain import robot_client


def main():
    print("1. Health check...")
    h = robot_client.health()
    print(f"   {h}\n")

    print("2. Read angle on port A...")
    try:
        print(f"   {robot_client.read_angle('A')}\n")
    except Exception as e:
        print(f"   failed: {e}\n")

    print("3. Interactive goto_angle test.")
    print("   Enter a target angle in degrees, or 'q' to quit.\n")

    while True:
        cmd = input("> ").strip().lower()
        if cmd == "q":
            break
        try:
            target = float(cmd)
        except ValueError:
            print("   Enter a number (degrees) or 'q'.")
            continue
        result = robot_client.goto_angle("A", target)
        print(f"   {result}")

    print("\nSending final stop...")
    print(robot_client.stop("A"))
    print("Done.")


if __name__ == "__main__":
    main()
