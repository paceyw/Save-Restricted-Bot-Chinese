# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

import os
from config import API_ID, API_HASH, BOT_TOKEN, STRING
from pyrogram import Client
from pyrogram.raw.types.messages import Messages as _RawMessages
import inspect
import sys


# pyrofork 2.3.69 bug: send_media_group builds
# raw.types.messages.Messages(messages=…, users=…, chats=…) but the TL layer
# makes `topics` a REQUIRED keyword — the constructor raises
# "Messages.__init__() missing 1 required keyword-only argument: 'topics'"
# AFTER the album is already delivered on the wire, so callers misread it
# as a delivery failure and re-send duplicates (observed live 2026-08-15).
# Default topics to [] until the library fixes its own call sites.
try:
    _topics_has_default = (
        inspect.signature(_RawMessages.__init__)
        .parameters.get("topics").default is not inspect.Parameter.empty
    )
except (AttributeError, ValueError):
    _topics_has_default = True  # can't inspect: assume fine, don't patch
if not _topics_has_default:
    _orig_raw_messages_init = _RawMessages.__init__

    def _raw_messages_init_with_topics(self, *args, **kwargs):
        kwargs.setdefault("topics", [])
        _orig_raw_messages_init(self, *args, **kwargs)

    _RawMessages.__init__ = _raw_messages_init_with_topics

# pyrofork defaults workdir to Path(sys.argv[0]).parent, which is /app (read-only
# image layer) when running "python /app/main.py". Force it to the persistent CWD
# (/data) so session files are created on the writable mounted volume.
_WORKDIR = os.environ.get("TMPDIR", os.getcwd())
if _WORKDIR.endswith("/tmp"):
    _WORKDIR = _WORKDIR[:-4]
_WORKDIR = _WORKDIR or os.getcwd()

app = Client("pyrogrambot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir=_WORKDIR)
userbot = Client("4gbbot", api_id=API_ID, api_hash=API_HASH, session_string=STRING, workdir=_WORKDIR)

async def start_client():
    await app.start()
    print("Pyro App Started...")
    if STRING:
        try:
            await userbot.start()
            print("Userbot started...")
        except Exception as e:
            print(f"Hey honey!! check your premium string session, it may be invalid of expire {e}")
            sys.exit(1)
    from utils.func import cleanup_stale_downloads
    await cleanup_stale_downloads()
