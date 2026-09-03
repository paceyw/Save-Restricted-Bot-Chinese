import asyncio
import json
import os

os.environ.setdefault('MASTER_KEY', 'health-test-master')
os.environ.setdefault('IV_KEY', 'health-test-iv')

import aiohttp

from utils.health import HealthServer, _WELCOME_PAGE


async def _probe(port, path):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}{path}") as resp:
            return resp.status, resp.headers.get("Content-Type", ""), await resp.read()


def test_healthz_returns_fixed_success():
    async def scenario():
        server = await HealthServer(host="127.0.0.1", port=0).start()
        try:
            status, ctype, body = await _probe(server.bound_port, "/healthz")
            assert status == 200
            assert "application/json" in ctype
            assert json.loads(body) == {"ok": True}
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_welcome_page_serves_template_bytes():
    async def scenario():
        server = await HealthServer(host="127.0.0.1", port=0).start()
        try:
            status, ctype, body = await _probe(server.bound_port, "/")
            assert status == 200
            assert "text/html" in ctype
            # Same bytes the Flask render_template used to emit: the template
            # has no Jinja variables, so verbatim serving is behavior-identical.
            assert body == _WELCOME_PAGE.read_bytes()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_stop_closes_socket_and_is_idempotent():
    async def scenario():
        server = await HealthServer(host="127.0.0.1", port=0).start()
        port = server.bound_port
        await server.stop()
        assert server.bound_port is None
        # second stop must not raise
        await server.stop()
        # socket is really closed
        try:
            await _probe(port, "/healthz")
            reachable = True
        except aiohttp.ClientConnectorError:
            reachable = False
        assert not reachable

    asyncio.run(scenario())


def test_unknown_path_is_404_not_welcome():
    async def scenario():
        server = await HealthServer(host="127.0.0.1", port=0).start()
        try:
            status, _, _ = await _probe(server.bound_port, "/nope")
            assert status == 404
        finally:
            await server.stop()

    asyncio.run(scenario())
