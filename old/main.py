"""
main.py -- single entry point for LegoBot, run from the PC.

Before this: it was a guessing game between tests/pylgbst_technic_example.py,
scripts/ble_scan_diagnostic.py, and whatever else looked runnable. This
script is now the one command you run; everything else is a supporting
module underneath it.

Usage:
    python3 main.py           # run pc_server.py (blocking) --
                               # POST /command, GET /frame, GET /audio.
                               # Everything talks to the robot THROUGH
                               # this -- there's no separate "just check
                               # health" or "just poke it interactively"
                               # mode anymore. Use scripts/call_server.py
                               # to actually send it commands while it's
                               # running.
    python3 main.py --deploy  # push src/lego_pi + src/lego_control to
                               # the Pi, restart its relay service, exit.
                               # A one-off maintenance action, not a way
                               # of talking to the robot -- kept separate
                               # from the line above on purpose.

What this does NOT do:
    - Does not start or manage anything ON the Pi. lego_pi's relay
      (src/lego_pi/relay_server.py) is expected to already be running
      there -- either manually (`python3 -m lego_pi`) or, once you've set
      one up, as a persistent systemd service. --deploy only pushes code
      and restarts that service if it exists; it doesn't bring the relay
      up from nothing on a Pi that's never run it before.
    - Does not care which route ends up serving each command. Whether
      the Pi relay or a direct BLE bypass from this PC handles it is
      entirely lego_brain.robot_client's decision -- pc_server.py's own
      /health endpoint reports it (see the "_route" field).
"""

import argparse
import os
import sys
from pathlib import Path


_SRC_DIR = Path(__file__).parent / "src"
_SCRIPTS_DIR = Path(__file__).parent / "scripts"
sys.path.insert(0, str(_SRC_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))


def _run_deploy():
    print("== Deploying src/lego_pi + src/lego_control to the Pi ==")
    from scripts.deploy import main as deploy_main
    deploy_main()
    print()


def _run_server():
    """Runs lego_brain/pc_server.py in-process via uvicorn.run() --
    blocks until Ctrl+C. POST /command, GET /frame, GET /audio. This is
    now the only way anything (a script, later Gemma) talks to the
    robot from the PC side."""
    import uvicorn

    port = int(os.environ.get("LEGOBOT_SERVER_PORT", "9000"))
    print(f"[main] Starting pc_server on 0.0.0.0:{port} (Ctrl+C to stop)...")
    uvicorn.run("lego_brain.pc_server:app", host="0.0.0.0", port=port)


def main():
    parser = argparse.ArgumentParser(description="LegoBot entry point")
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="push src/lego_pi + src/lego_control to the Pi, restart its relay service, then exit",
    )
    args = parser.parse_args()

    if args.deploy:
        _run_deploy()
        return

    _run_server()


if __name__ == "__main__":
    main()
