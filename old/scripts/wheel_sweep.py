"""
wheel_sweep.py -- interactive port-identification sweep.

Reads /health to find which ports are actually attached on each hub,
then for every (hub, port) pair: spins it briefly, asks you what
physically moved and which way, and records your answer. At the end it
writes a summary to debug_server/wheel_sweep.json AND prints it, so you
can paste the printed table back into chat (or re-upload the JSON) and
Claude can read the whole mapping in one shot instead of you reporting
each port one command at a time.

Usage:
    python3 scripts/wheel_sweep.py
    python3 scripts/wheel_sweep.py --speed 0.4 --duration 1.5

Requires pc_server.py already running (python -m main) -- this is a
client, same as call_server.py, not a replacement for the server.
"""

import argparse
import json
import os
import time
from pathlib import Path

import requests

SERVER = os.environ.get("LEGOBOT_SERVER", "http://localhost:9000")
OUT_DIR = Path(__file__).parent.parent / "debug_server"


def _post(cmd: str, hub: str, args: dict, timeout: float = 10.0) -> dict:
    body = {"cmd": cmd, "hub": hub, "args": args}
    r = requests.post(f"{SERVER}/command", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _get_attached_ports() -> dict:
    """{"front": ["A", "B", "C"], "rear": ["A", "B"]} -- only test ports
    that actually have something plugged in, per the hub's own report,
    rather than guessing at A-D blindly.

    pc_server.py's /health doesn't return the Pi's response verbatim --
    robot_client.health() adds a top-level "_route" string key (e.g.
    "pi_relay") alongside the per-hub dicts. Filtering to dict values
    only, rather than assuming every top-level key is a hub, is what
    keeps that from blowing up here.
    """
    r = requests.get(f"{SERVER}/health", timeout=5)
    r.raise_for_status()
    health = r.json()
    return {
        hub: data.get("ports_attached", [])
        for hub, data in health.items()
        if isinstance(data, dict)
    }


def main():
    parser = argparse.ArgumentParser(description="Interactive port-identification sweep")
    parser.add_argument("--speed", type=float, default=0.3, help="test speed, -1.0 to 1.0 (default 0.3)")
    parser.add_argument("--duration", type=float, default=1.0, help="seconds to spin each port (default 1.0)")
    args = parser.parse_args()

    print(f"Checking {SERVER}/health for attached ports...")
    attached = _get_attached_ports()
    total = sum(len(ports) for ports in attached.values())
    if total == 0:
        print("No ports reported as attached on any hub -- is the relay actually connected? Aborting.")
        return
    print(f"Found {total} attached port(s): {attached}\n")

    results = {}
    tested = 0
    for hub, ports in attached.items():
        for port in ports:
            tested += 1
            label = f"{hub}/{port}"
            input(f"[{tested}/{total}] Ready to test {label}? Press Enter when you're watching it...")
            print(f"  Spinning {label} at speed={args.speed} for {args.duration}s...")

            try:
                _post("set_speed", hub, {"port": port, "speed": args.speed})
                time.sleep(args.duration)
                _post("stop", hub, {"port": port})
            except Exception as e:
                # One flaky port (timeout, 409, connection drop) used to
                # kill the whole sweep and lose every result gathered so
                # far. Record the failure and move on instead -- and
                # always try to stop, even on failure, so a motor that
                # DID start moving doesn't keep spinning unattended.
                print(f"  ERROR on {label}: {e}")
                try:
                    _post("stop", hub, {"port": port}, timeout=5.0)
                except Exception:
                    print(f"  (also failed to send a follow-up stop for {label} -- check it by hand)")
                results[label] = {"hub": hub, "port": port, "observation": f"ERROR: {e}"}
                print()
                continue

            observation = input(
                f"  What moved for {label}? (e.g. 'front-left, forward' or 'nothing') > "
            ).strip()
            results[label] = {"hub": hub, "port": port, "observation": observation}
            print()

    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / "wheel_sweep.json"
    out_path.write_text(json.dumps(results, indent=2))

    print("=" * 50)
    print("Sweep complete. Summary:")
    for label, r in results.items():
        print(f"  {label:12s} -> {r['observation']}")
    print(f"\nWrote {out_path} -- paste the summary above into chat, or re-upload this file.")


if __name__ == "__main__":
    main()
