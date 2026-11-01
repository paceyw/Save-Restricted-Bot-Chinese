# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025 Save Restricted Bot Chinese contributors.

from pyrogram import filters
from shared_client import app
from config import PAY_NOTICE


@app.on_message(filters.command("pay") & filters.private)
async def pay_handler(client, message):
    await message.reply_text(PAY_NOTICE)
