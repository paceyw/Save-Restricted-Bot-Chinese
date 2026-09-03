# Copyright (c) 2025 devgagan : https://github.com/devgajanin.
# Licensed under the GNU General Public License v3.0
# See LICENSE file in the repository root for full license text.

"""Welcome/health HTTP server running inside the bot's own asyncio loop.

Replaces the former standalone Flask process (app.py + `flask run` in
bot-entrypoint.sh): one less resident Python interpreter, one lifecycle.
Routes:
  /        — static welcome page (templates/welcome.html has no Jinja
             variables, so it is served verbatim; Content-Type pinned).
  /healthz — fixed-success JSON endpoint for the Compose healthcheck. It is
             served by the same event loop as the bot, so a blocked loop
             fails the probe naturally — no extra liveness logic needed.
"""

import os
from pathlib import Path

from aiohttp import web

_WELCOME_PAGE = Path(__file__).resolve().parent.parent / "templates" / "welcome.html"


async def welcome_handler(_request: web.Request) -> web.Response:
    return web.FileResponse(_WELCOME_PAGE, headers={"Content-Type": "text/html"})


async def healthz_handler(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_app() -> web.Application:
    http_app = web.Application()
    http_app.router.add_get("/", welcome_handler)
    http_app.router.add_get("/healthz", healthz_handler)
    return http_app


class HealthServer:
    """ aiohttp site bound to the bot process; start/stop from main.py lifecycle. """

    def __init__(self, host: str = "0.0.0.0", port: int | None = None):
        self._host = host
        self._port = port if port is not None else int(os.environ.get("PORT", "5000"))
        self._runner: web.AppRunner | None = None
        # bound_port: self._port, or the OS-assigned port when 0 (tests)
        self.bound_port: int | None = None

    async def start(self) -> "HealthServer":
        self._runner = web.AppRunner(build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        # runner.addresses is the public API for bound sockaddrs; port 0 lets
        # the OS pick (used by tests to avoid collisions).
        self.bound_port = self._runner.addresses[0][1] if self._runner.addresses else self._port
        print(f"Health server listening on {self._host}:{self.bound_port} (/ and /healthz)")
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self.bound_port = None
