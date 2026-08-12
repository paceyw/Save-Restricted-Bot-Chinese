# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

from datetime import timedelta, datetime
from shared_client import app
from pyrogram import filters
from utils.func import (
    get_premium_details, get_display_name, get_user_data,
    premium_users_collection, is_premium_user,
)
from config import OWNER_ID, PAY_NOTICE
import logging
logging.basicConfig(format=
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger('teamspy')


@app.on_message(filters.command("status") & filters.private)
async def status_handler(client, message):
    """Handle /status command to check user session and bot status"""
    user_id = message.from_user.id
    user_data = await get_user_data(user_id)

    session_active = bool(user_data and "session_string" in user_data)
    bot_active = bool(user_data and "bot_token" in user_data)

    premium_status = "❌ 不是高级会员"
    premium_details = await get_premium_details(user_id)
    if premium_details:
        expiry_utc = premium_details["subscription_end"]
        expiry_ist = expiry_utc + timedelta(hours=5, minutes=30)
        formatted_expiry = expiry_ist.strftime("%d-%b-%Y %I:%M:%S %p")
        premium_status = f"✅ 高级会员有效期至 {formatted_expiry} (IST)"

    await message.reply_text(
        "**您当前的状态：**\n\n"
        f"**登录状态：** {'✅ 活跃' if session_active else '❌ 未激活'}\n"
        f"**自定义机器人：** {'✅ 已设置' if bot_active else '❌ 未设置'}\n"
        f"**会员：** {premium_status}"
    )


@app.on_message(filters.command("myplan") & filters.private)
async def myplan_handler(client, message):
    """Handle /myplan command to show the user's current subscription plan"""
    user_id = message.from_user.id
    premium_details = await get_premium_details(user_id)
    expiry_utc = premium_details.get("subscription_end") if premium_details else None
    if expiry_utc and expiry_utc > datetime.now():
        expiry_utc = premium_details["subscription_end"]
        expiry_ist = expiry_utc + timedelta(hours=5, minutes=30)
        formatted_expiry = expiry_ist.strftime("%d-%b-%Y %I:%M:%S %p")
        await message.reply_text(
            "**您的会员方案：**\n\n"
            f"💎 高级会员\n"
            f"⏰ 有效期至：{formatted_expiry} (IST)"
        )
    else:
        await message.reply_text(f"❌ 您当前没有任何会员套餐。\n\n{PAY_NOTICE}")


@app.on_message(filters.command("transfer") & filters.private)
async def transfer_premium_handler(client, message):
    user_id = message.from_user.id
    sender = message.from_user
    sender_name = get_display_name(sender)
    args = message.text.split()
    if len(args) != 2:
        await message.reply_text(
            '用法：/transfer user_id\n示例：/transfer 123456789')
        return
    try:
        target_user_id = int(args[1])
    except ValueError:
        await message.reply_text(
            '❌ 用户 ID 无效。请提供有效的数字用户 ID。')
        return
    if target_user_id <= 0:
        await message.reply_text(
            '❌ 用户 ID 无效。请提供有效的正数用户 ID。')
        return
    if target_user_id == user_id:
        await message.reply_text('❌ 您不能将高级会员转赠给自己。')
        return

    try:
        target_entity = await app.get_users(target_user_id)
        if target_entity is None:
            raise ValueError("target user was not found")
        target_name = get_display_name(target_entity)
    except Exception as e:
        logger.warning(f'Could not get target user {target_user_id}: {e}')
        await message.reply_text(
            '❌ 无法找到目标用户，请提供有效的用户 ID。')
        return

    if await is_premium_user(target_user_id):
        await message.reply_text(
            '❌ 目标用户已有高级会员订阅。')
        return

    try:
        premium_details = await get_premium_details(user_id)
        if not premium_details:
            await message.reply_text('❌ 获取您的会员详情时出错。')
            return
        expiry_date = premium_details.get('subscription_end')
        now = datetime.now()
        claim_result = await premium_users_collection.delete_one({
            'user_id': user_id,
            'subscription_end': {'$gt': now},
        })
        if claim_result.deleted_count != 1:
            await message.reply_text(
                '❌ 您的会员订阅已过期或已被转赠。')
            return

        try:
            await premium_users_collection.update_one({'user_id':
                target_user_id}, {'$set': {'user_id': target_user_id,
                'subscription_start': now, 'subscription_end': expiry_date,
                'expireAt': expiry_date, 'transferred_from': user_id,
                'transferred_from_name': sender_name}}, upsert=True)
        except Exception:
            # Compensate the non-transactional two-step write: the claim
            # delete already removed the sender's document, so a failed
            # target upsert must restore it instead of losing the plan.
            try:
                await premium_users_collection.insert_one(premium_details)
            except Exception as e:
                logger.error(
                    f'Premium transfer rollback failed for user {user_id}: {e}')
            raise
        expiry_ist = expiry_date + timedelta(hours=5, minutes=30)
        formatted_expiry = expiry_ist.strftime('%d-%b-%Y %I:%M:%S %p')
        await message.reply_text(
            f'✅ 高级会员订阅已成功转赠给 {target_name}（{target_user_id}）。您的会员权限已被移除。'
            )
        try:
            await app.send_message(target_user_id,
                f'🎁 您已收到来自 {sender_name}（{user_id}）的高级会员转赠。您的会员有效期至 {formatted_expiry} (IST)。'
                )
        except Exception as e:
            logger.error(f'Could not notify target user {target_user_id}: {e}')
        try:
            owner_id = OWNER_ID[0] if isinstance(OWNER_ID, list) else int(OWNER_ID)
            await app.send_message(owner_id,
                f'♻️ 高级会员转赠：{sender_name}（{user_id}）已将会员转赠给 {target_name}（{target_user_id}）。到期时间：{formatted_expiry}'
                )
        except Exception as e:
            logger.error(f'Could not notify owner about premium transfer: {e}')
        return
    except Exception as e:
        logger.error(
            f'Error transferring premium from {user_id} to {target_user_id}: {e}'
            )
        await message.reply_text(f'❌ 转赠高级会员时出错：{str(e)}')
        return


@app.on_message(filters.command("rem") & filters.private)
async def remove_premium_handler(client, message):
    user_id = message.from_user.id
    if user_id not in OWNER_ID:
        return
    args = message.text.split()
    if len(args) != 2:
        await message.reply_text('用法：/rem user_id\n示例：/rem 123456789')
        return
    try:
        target_user_id = int(args[1])
    except ValueError:
        await message.reply_text(
            '❌ 用户 ID 无效。请提供有效的数字用户 ID。')
        return
    if not await is_premium_user(target_user_id):
        await message.reply_text(
            f'❌ 用户 {target_user_id} 没有高级会员订阅。')
        return
    try:
        target_name = '未知用户'
        try:
            target_entity = await app.get_users(target_user_id)
            target_name = get_display_name(target_entity)
        except Exception as e:
            logger.warning(f'Could not get target user name: {e}')
        result = await premium_users_collection.delete_one({'user_id':
            target_user_id})
        if result.deleted_count > 0:
            await message.reply_text(
                f'✅ 已成功从 {target_name}（{target_user_id}）移除高级会员订阅。'
                )
            try:
                await app.send_message(target_user_id,
                    '⚠️ 您的高级会员订阅已被管理员移除。'
                    )
            except Exception as e:
                logger.error(
                    f'Could not notify user {target_user_id} about premium removal: {e}'
                    )
        else:
            await message.reply_text(
                f'❌ 从用户 {target_user_id} 移除高级会员失败。')
        return
    except Exception as e:
        logger.error(f'Error removing premium from {target_user_id}: {e}')
        await message.reply_text(f'❌ 移除高级会员时出错：{str(e)}')
        return
