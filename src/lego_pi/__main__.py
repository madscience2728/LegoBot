"""
Entrypoint for `python3 -m lego_pi` -- run this ON THE PI.

Boots BOTH FastAPI apps concurrently in one process:
    relay_server.py -- robot commands, port 8000
    media_relay.py  -- camera + mic streaming, port 8001

Deliberately one process/one systemd unit rather than two services.
media_relay has nothing to do with BLE/hub_controller -- it only touches
the camera and mic -- so there's no shared-state reason to keep it
separate. Splitting it out would mean a second unit file for deploy.py's
sync_service() to know about, and a second thing that can silently not
be running after a reboot if someone only remembers to check one
`systemctl status`. Two ports, one lifecycle.

This is what the "legobot-relay" systemd unit invokes (see
robot_files/legobot-relay.service) -- deploy.py pushes code and restarts
that one service; there's nothing else to wire up separately.
"""

import asyncio

import uvicorn

from lego_pi.relay_server import app as relay_app
from lego_pi.media_relay import app as media_app


async def _serve(app, port: int):
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    await asyncio.gather(
        _serve(relay_app, 8000),
        _serve(media_app, 8001),
    )


if __name__ == "__main__":
    asyncio.run(main())
