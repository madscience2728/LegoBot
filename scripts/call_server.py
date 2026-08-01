"""
call_server.py -- manual test client for pc_server.py.

Sends one command per invocation and prints the result. This is the
"script of cmd command" stand-in for Gemma, until Gemma exists -- it
talks to pc_server.py over plain HTTP exactly the way any future real
client would, it's just a human typing the command instead of a model
deciding one.

Usage:
    python3 scripts/call_server.py stop port=A
    python3 scripts/call_server.py set_speed port=A speed=0.3
    python3 scripts/call_server.py read_angle port=A
    python3 scripts/call_server.py goto_angle port=A target_degrees=90

    python3 scripts/call_server.py --frame   # GET /frame, save to debug_server/frame.jpg
    python3 scripts/call_server.py --audio   # GET /audio, save to debug_server/audio.wav
    python3 scripts/call_server.py --health  # GET /health

key=value args are parsed as JSON when possible (so speed=0.3 becomes
a float, not the string "0.3"), falling back to a plain string
otherwise.

Env var LEGOBOT_SERVER (default http://localhost:9000) points this at
pc_server.py.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

SERVER = os.environ.get("LEGOBOT_SERVER", "http://localhost:9000")

# Fixed location inside the repo -- no path argument to get wrong (a
# bare "/" or "out.jpg" resolves relative to wherever you happened to
# run this from, and can land somewhere with no write permission, e.g.
# actual filesystem root). Same folder scripts/pc_media_client.py
# already uses, so both tools' output lands in one predictable place.
_OUT_DIR = Path(__file__).parent.parent / "debug_server"


def _parse_args(pairs: list[str]) -> dict:
    args = {}
    for pair in pairs:
        if "=" not in pair:
            print(f"error: expected key=value, got {pair!r}", file=sys.stderr)
            sys.exit(1)
        key, _, value = pair.partition("=")
        try:
            args[key] = json.loads(value)
        except json.JSONDecodeError:
            args[key] = value
    return args


def main():
    parser = argparse.ArgumentParser(description="Manual test client for pc_server.py")
    parser.add_argument("cmd", nargs="?", help="command name, e.g. goto_angle")
    parser.add_argument("kv", nargs="*", help="key=value command args")
    parser.add_argument("--health", action="store_true", help="GET /health instead of sending a command")
    parser.add_argument("--frame", action="store_true", help=f"GET /frame, save to {_OUT_DIR}/frame.jpg")
    parser.add_argument("--audio", action="store_true", help=f"GET /audio, save to {_OUT_DIR}/audio.wav")
    args = parser.parse_args()

    if args.health:
        r = requests.get(f"{SERVER}/health", timeout=5)
        print(r.status_code, r.json())
        return

    if args.frame:
        r = requests.get(f"{SERVER}/frame", timeout=5)
        if r.status_code != 200:
            print(r.status_code, r.text)
            sys.exit(1)
        _OUT_DIR.mkdir(exist_ok=True)
        path = _OUT_DIR / "frame.jpg"
        path.write_bytes(r.content)
        print(f"wrote {path} ({len(r.content)} bytes)")
        return

    if args.audio:
        r = requests.get(f"{SERVER}/audio", timeout=5)
        if r.status_code != 200:
            print(r.status_code, r.text)
            sys.exit(1)
        _OUT_DIR.mkdir(exist_ok=True)
        path = _OUT_DIR / "audio.wav"
        path.write_bytes(r.content)
        print(f"wrote {path} ({len(r.content)} bytes)")
        return

    if not args.cmd:
        parser.error("a command is required unless using --health, --frame, or --audio")

    body = {"cmd": args.cmd, "args": _parse_args(args.kv)}
    r = requests.post(f"{SERVER}/command", json=body, timeout=20)
    print(r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)


if __name__ == "__main__":
    main()
