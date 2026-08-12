# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

import os
from telethon import TelegramClient
from config import API_ID, API_HASH, BOT_TOKEN, STRING
from pyrogram import Client
import sys

# pyrofork defaults workdir to Path(sys.argv[0]).parent, which is /app (read-only
# image layer) when running "python /app/main.py". Force it to the persistent CWD
# (/data) so session files are created on the writable mounted volume.
_WORKDIR = os.environ.get("TMPDIR", os.getcwd())
if _WORKDIR.endswith("/tmp"):
    _WORKDIR = _WORKDIR[:-4]
_WORKDIR = _WORKDIR or os.getcwd()

client = TelegramClient(os.path.join(_WORKDIR, "telethonbot"), API_ID, API_HASH, receive_updates=False)
app = Client("pyrogrambot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir=_WORKDIR)
userbot = Client("4gbbot", api_id=API_ID, api_hash=API_HASH, session_string=STRING, workdir=_WORKDIR)

async def start_client():
    # Pyrogram starts first; Telethon's background update loop can interfere with
    # pyrofork's SQLite session if started first.
    await app.start()
    print("Pyro App Started...")
    if STRING:
        try:
            await userbot.start()
            print("Userbot started...")
        except Exception as e:
            print(f"Hey honey!! check your premium string session, it may be invalid of expire {e}")
            sys.exit(1)
    if not client.is_connected():
        # Telethon and Pyrogram both use BOT_TOKEN. Telegram delivers bot updates
        # to only one session. Start Telethon to authenticate, then stop its update
        # loop so Pyrogram receives all incoming messages. Reconnect for API calls.
        await client.start(bot_token=BOT_TOKEN)
        await client.disconnect()
        await client.connect()
        print("SpyLib started (API-only, no update loop)...")
    return client, app, userbot
