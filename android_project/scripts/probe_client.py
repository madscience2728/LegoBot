#!/usr/bin/env python3
"""
probe_client.py -- hit all four probe endpoints from the PC and print
what each one reports. Same phone-and-PC-on-same-WiFi assumption as
lego_brain's PC<->Pi relay traffic.

Usage:
    python3 scripts/probe_client.py <phone-ip> [port]
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

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/probe_client.py <phone-ip> [port]")
        sys.exit(1)
    ip = sys.argv[1]
    port = sys.argv[2] if len(sys.argv) > 2 else "8765"
    base = f"http://{ip}:{port}"

    checks = [
        ("health",    f"{base}/health"),
        ("ble scan",  f"{base}/ble/scan?seconds=4"),
        ("camera",    f"{base}/cam/test"),
        ("mic",       f"{base}/mic/test?seconds=2"),
    ]

    for label, url in checks:
        print(f"\n== {label} ({url}) ==")
        result = get(url)
        print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
