# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

import asyncio
import inspect
from shared_client import app, start_client, userbot
import importlib
import os
import sys

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
    await load_and_run_plugins()
    while True:
        await asyncio.sleep(1)  

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    print("Starting clients ...")
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("Shutting down...")
    except Exception as e:
        print(e)
        sys.exit(1)
    finally:
        try:
            if not loop.is_closed():
                loop.run_until_complete(stop_clients())
        except Exception as e:
            print(f"Error during shutdown: {e}")
        finally:
            try:
                loop.close()
            except Exception:
                pass
