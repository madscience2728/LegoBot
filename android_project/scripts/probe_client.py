#!/usr/bin/env python3
"""
probe_client.py -- hit the probe endpoints from the PC and print what
each one reports. Same phone-and-PC-on-same-WiFi assumption as
lego_brain's PC<->Pi relay traffic.

Usage:
    python3 scripts/probe_client.py <phone-ip> [port]                    # the original GET-only smoke test
    python3 scripts/probe_client.py <phone-ip> [port] hub-smoke [front|rear] [A|B|C|D]
        connect the named hub (default: front), describe_port on the
        named port (default: A), then flash its LED green -- the
        first end-to-end proof the phone can actually drive a real hub,
        not just log that a command arrived.
"""
import sys
import json
import urllib.request

def get(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"status": "error", "message": f"request failed: {e}"}

def post(url, body, timeout=30):
    # Generous timeout -- /hub/connect alone can take 15-25s worst case
    # (scan + GATT handshake + subscribe), same reasoning as
    # HubConnector.connect()'s own docstring.
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"status": "error", "message": f"request failed: {e}"}

def run_probe_smoke(base):
    checks = [
        ("health",    f"{base}/health"),
        ("ble scan",  f"{base}/ble/scan?seconds=4"),
        ("camera",    f"{base}/cam/test"),
        ("mic",       f"{base}/mic/test?seconds=2"),
    ]
    for label, url in checks:
        print(f"\n== {label} ({url}) ==")
        print(json.dumps(get(url), indent=2))

def run_hub_smoke(base, hub, port):
    steps = [
        ("hub connect",      f"{base}/hub/connect",       {"hub": hub}),
        ("describe_port",    f"{base}/hub/describe_port",  {"hub": hub, "port": port}),
        ("LED -> green",     f"{base}/hub/led",            {"hub": hub, "r": 0, "g": 255, "b": 0}),
    ]
    for label, url, body in steps:
        print(f"\n== {label} ({url}) ==")
        print(json.dumps(post(url, body), indent=2))

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    ip = sys.argv[1]
    rest = sys.argv[2:]

    # port, if given, is always the next purely-numeric token right after
    # the IP -- "hub-smoke" and the hub/port names after it are never
    # numeric, so this can't misparse them as a port number.
    port_arg = "8765"
    if rest and rest[0].isdigit():
        port_arg = rest[0]
        rest = rest[1:]
    base = f"http://{ip}:{port_arg}"

    if rest and rest[0] == "hub-smoke":
        args = rest[1:]
        hub = args[0] if len(args) > 0 else "front"
        motor_port = args[1] if len(args) > 1 else "A"
        run_hub_smoke(base, hub, motor_port)
    else:
        run_probe_smoke(base)

if __name__ == "__main__":
    main()
