# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

from pyrogram import filters
from shared_client import app
from config import PAY_NOTICE


@app.on_message(filters.command("pay") & filters.private)
async def pay_handler(client, message):
    await message.reply_text(PAY_NOTICE)
