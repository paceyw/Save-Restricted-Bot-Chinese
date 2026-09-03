# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0  
# See LICENSE file in the repository root for full license text.

import asyncio
import inspect
import os
import signal
import sys

from shared_client import app, start_client, userbot
from utils.func import init_db_indexes
from utils.health import HealthServer
import importlib


async def load_and_run_plugins():
    await start_client()
    plugin_dir = "plugins"
    plugins = [f[:-3] for f in os.listdir(plugin_dir) if f.endswith(".py") and f != "__init__.py"]

    for plugin in plugins:
        module = importlib.import_module(f"plugins.{plugin}")
        if hasattr(module, f"run_{plugin}_plugin"):
            print(f"Running {plugin} plugin...")
            await getattr(module, f"run_{plugin}_plugin")()  


async def _stop_if_connected(instance, method_name):
    if instance is None:
        return

    try:
        connected = getattr(instance, "is_connected", False)
        if callable(connected):
            connected = connected()
        if inspect.isawaitable(connected):
            connected = await connected
        if not connected:
            return

        result = getattr(instance, method_name)()
        if inspect.isawaitable(result):
            await result
    except Exception as e:
        print(f"Error stopping client: {e}")


async def stop_clients():
    await _stop_if_connected(app, "stop")
    await _stop_if_connected(userbot, "stop")


async def main():
    """Single-process lifecycle: DB indexes -> health server -> plugins.

    The health server (welcome page + /healthz) runs inside this event loop,
    replacing the old standalone Flask process. SIGTERM/SIGINT set the stop
    event; the finally block then stops the health server and both Telegram
    clients before the process exits.
    """
    await init_db_indexes()

    health = HealthServer(port=int(os.environ.get("PORT", "5000")))
    await health.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # non-main thread or unsupported platform: default kill applies

    try:
        await load_and_run_plugins()
        await stop.wait()
        print("Shutting down...")
    finally:
        await health.stop()
        await stop_clients()


if __name__ == "__main__":
    print("Starting clients ...")
    try:
        asyncio.run(main())
    except Exception as e:
        print(e)
        sys.exit(1)
