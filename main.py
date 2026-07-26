"""
main.py -- single entry point for LegoBot, run from the PC.

Before this: it was a guessing game between tests/pylgbst_technic_example.py,
scripts/ble_scan_diagnostic.py, and whatever else looked runnable. This
script is now the one command you run; everything else is a supporting
module underneath it.

Usage:
    python3 main.py                # health check, then interactive control
    python3 main.py --deploy       # push src/lego_pi + src/lego_control to
                                    # the Pi first, then health check + control
    python3 main.py --health-only  # report reachability/route and exit

What this does NOT do:
    - Does not start or manage anything ON the Pi. lego_pi's relay
      (src/lego_pi/relay_server.py) is expected to already be running
      there -- either manually (`python3 -m lego_pi`) or, once you've set
      one up, as a persistent systemd service. --deploy only pushes code
      and restarts that service if it exists; it doesn't bring the relay
      up from nothing on a Pi that's never run it before.
    - Does not care which route ends up serving each command. Whether
      the Pi relay or a direct BLE bypass from this PC handles it is
      entirely lego_brain.robot_client's decision -- see its "_route"
      field in the health check output below.
"""

import argparse
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).parent / "src"
_SCRIPTS_DIR = Path(__file__).parent / "scripts"
sys.path.insert(0, str(_SRC_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))


def _run_deploy():
    print("== Deploying src/lego_pi + src/lego_control to the Pi ==")
    from deploy import main as deploy_main
    deploy_main()
    print()


def _report_health() -> bool:
    from lego_brain import robot_client
    try:
        h = robot_client.health()
        route = h.get("_route", "unknown")
        print(f"[main] Robot reachable via: {route}")
        print(f"[main] {h}")
        return True
    except Exception as e:
        print(
            f"[main] WARNING: could not reach the robot via the Pi relay OR "
            f"direct BLE ({e}). Check LEGOBOT_PI_HOST, that the hub is on, "
            f"and that nothing else already holds the BLE connection."
        )
        return False


def _interactive_session():
    from pc_test_client import main as test_main
    test_main()


def main():
    parser = argparse.ArgumentParser(description="LegoBot entry point")
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="push src/lego_pi + src/lego_control to the Pi and restart its relay service first",
    )
    parser.add_argument(
        "--health-only",
        action="store_true",
        help="report reachability/route and exit, skip the interactive control session",
    )
    args = parser.parse_args()

    if args.deploy:
        _run_deploy()

    reachable = _report_health()

    if args.health_only:
        return

    if not reachable:
        print("[main] Skipping interactive session -- fix connectivity first (see warning above).")
        return

    _interactive_session()


if __name__ == "__main__":
    main()
