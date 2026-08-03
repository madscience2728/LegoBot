"""
find_pi.py -- scans 192.168.4.1-254 for open port 22 (SSH) or 8000
(legobot-relay) and prints whatever it finds. No admin rights needed,
no router access needed -- just plain TCP connect attempts with a short
timeout, run in parallel across threads.

Usage:
    python find_pi.py
"""

import socket
from concurrent.futures import ThreadPoolExecutor

SUBNET = "192.168.4."
PORTS = [22, 8000]
TIMEOUT = 0.3


def check(i):
    ip = f"{SUBNET}{i}"
    open_ports = []
    for port in PORTS:
        try:
            with socket.create_connection((ip, port), timeout=TIMEOUT):
                open_ports.append(port)
        except OSError:
            pass
    if open_ports:
        print(f"{ip} -- open: {open_ports}")


with ThreadPoolExecutor(max_workers=100) as pool:
    pool.map(check, range(1, 255))

print("done.")
