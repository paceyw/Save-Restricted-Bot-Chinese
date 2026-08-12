# Copyright (c) 2025 Gagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

from shared_client import app
from datetime import timedelta
from config import OWNER_ID, PAY_NOTICE
from utils.func import add_premium_user
from pyrogram import filters
from plugins.start import subscribe


@app.on_message(filters.command("add") & filters.private)
async def add_premium_handler(client, message):
    """Handle /add command to add premium users (owner only)"""
    user_id = message.from_user.id
    if user_id not in OWNER_ID:
        await message.reply_text('此命令仅限机器人管理员使用。')
        return
    text = message.text.strip()
    parts = text.split(' ')
    if len(parts) != 4:
        await message.reply_text(
            '格式无效。用法：/add user_id duration_value duration_unit\n示例：/add 123456 1 week'
            )
        return
    try:
        target_user_id = int(parts[1])
        duration_value = int(parts[2])
        duration_unit = parts[3].lower()
        valid_units = ['min', 'hours', 'days', 'weeks', 'month', 'year',
            'decades']
        if duration_unit not in valid_units:
            await message.reply_text(
                f"无效的时长单位。可选：{', '.join(valid_units)}"
                )
            return
        success, result = await add_premium_user(target_user_id,
            duration_value, duration_unit)
        if success:
            expiry_utc = result
            expiry_ist = expiry_utc + timedelta(hours=5, minutes=30)
            formatted_expiry = expiry_ist.strftime('%d-%b-%Y %I:%M:%S %p')
            await message.reply_text(
                f'✅ 用户 {target_user_id} 已添加为高级会员\n会员有效期至：{formatted_expiry} (IST)'
                )
            await app.send_message(target_user_id,
                f'✅ 您已成为高级会员\n**有效期至**：{formatted_expiry} (IST)'
                )
        else:
            await message.reply_text(f'❌ 添加高级会员失败：{result}')
    except ValueError:
        await message.reply_text(
            '用户 ID 或时长数值无效。两者都必须是整数。')
    except Exception as e:
        await message.reply_text(f'错误：{str(e)}')


@app.on_message(filters.command("start"))
async def start_handler(client, message):
    subscription_status = await subscribe(client, message)
    if subscription_status == 1:
        return
    await message.reply_text(PAY_NOTICE)
