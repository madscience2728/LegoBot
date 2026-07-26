"""
Entrypoint for `python3 -m lego_pi` -- run this ON THE PI.
Boots the FastAPI relay on 0.0.0.0:8000.

This is what a systemd unit should invoke once you set one up (see
SpiderBot's robot_files/spider-robot.service + docs/SYSTEMD_REFERENCE.md
for the pattern to copy -- LegoBot doesn't have its own yet).
"""

import uvicorn

from lego_pi.relay_server import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
