# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

from shared_client import app
from pyrogram import filters
from pyrogram.errors import UserNotParticipant
from pyrogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from config import LOG_GROUP, OWNER_ID, FORCE_SUB, PAY_NOTICE, ADMIN_CONTACT

async def subscribe(app, message):
    if not FORCE_SUB:
        return 0

    try:
        user = await app.get_chat_member(FORCE_SUB, message.from_user.id)
        status = str(getattr(user, "status", "")).rsplit(".", 1)[-1].upper()
        is_member = (
            status in {"MEMBER", "ADMINISTRATOR", "CREATOR", "OWNER"}
            or (status == "RESTRICTED" and bool(getattr(user, "is_member", False)))
        )
        if is_member:
            return 0

        if status == "BANNED":
            await message.reply_text("您已被封禁。请联系 -- Team SPY")
            return 1

        link = await app.export_chat_invite_link(FORCE_SUB)
        caption = f"加入我们的频道后即可使用机器人"
        await message.reply_photo(photo="https://graph.org/file/d44f024a08ded19452152.jpg",caption=caption, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("立即加入...", url=f"{link}")]]))
        return 1
    except UserNotParticipant:
        link = await app.export_chat_invite_link(FORCE_SUB)
        caption = f"加入我们的频道后即可使用机器人"
        await message.reply_photo(photo="https://graph.org/file/d44f024a08ded19452152.jpg",caption=caption, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("立即加入...", url=f"{link}")]]))
        return 1
    except Exception as ggn:
        await message.reply_text(f"出现错误。请联系管理员……以下是错误信息：{ggn}")
        return 1
     
@app.on_message(filters.command("set"))
async def set(_, message):
    if message.from_user.id not in OWNER_ID:
        await message.reply("您没有权限使用此命令。")
        return
    await app.set_bot_commands([
        BotCommand("start", "🚀 启动机器人"),
        BotCommand("batch", "🫠 批量提取"),
        BotCommand("single", "🔖 单条提取"),
        BotCommand("merge", "🧩 合并多条链接为一条消息/相册"),
        BotCommand("tasks", "📋 查看任务队列状态和进度"),
        BotCommand("login", "🔑 登录机器人"),
        BotCommand("setbot", "🧸 添加处理文件的机器人"),
        BotCommand("rembot", "🤨 移除您的自定义机器人"),
        BotCommand("logout", "🚪 退出机器人"),
        BotCommand("status", "📊 查看您的状态"),
        BotCommand("myplan", "📋 查看您的会员套餐"),
        BotCommand("transfer", "💘 将会员转赠他人"),
        BotCommand("add", "➕ 添加用户为会员"),
        BotCommand("rem", "➖ 移除会员"),
        BotCommand("settings", "⚙️ 个性化设置"),
        BotCommand("dl", "🎬 下载视频（getav 加 -sub 烧录中文字幕）"),
        BotCommand("adl", "🎵 提取音频"),
        BotCommand("plan", "🗓️ 查看会员方案"),
        BotCommand("pay", "💎 开通/续费会员"),
        BotCommand("terms", "🥺 条款和条件"),
        BotCommand("help", "❓ 新手也能看懂！"),
        BotCommand("cancel", "🚫 取消登录/批量/设置流程"),
        BotCommand("stop", "🚫 取消批量提取流程")
    ])
 
    await message.reply("✅ 命令配置成功！")
 
 
 
 
help_pages = [
    (
        "📝 **机器人命令概览（1/2）**：\n\n"
        "🔑 **账号与登录**\n"
        "• **/login** — 登录以访问受限内容\n"
        "• **/logout** — 退出登录\n"
        "• **/setbot** — 添加自定义处理机器人\n"
        "• **/rembot** — 移除自定义机器人\n\n"
        "📥 **内容提取**\n"
        "• **/batch** — 批量提取帖子（登录后使用）\n"
        "• **/single** — 单条提取\n"
        "• **/merge** — 合并多条链接为一条消息/相册\n"
        "• **/dl** — 下载视频（YouTube/Instagram 等 yt-dlp 站点，及 missav.ai / getav.net 视频页）\n"
        "• **/dl -sub <getav链接>** — 同上，并把 getav 中文字幕烧录进画面（约 40 分钟重编码，标志可在链接前后）\n"
        "• **/adl** — 提取音频\n"
        "• **/cancel** / **/stop** — 取消进行中的任务\n"
        "• **/tasks** — 查看任务队列状态和进度（含 /dl /adl，每 5 秒自动刷新）\n"
        "• **/batch**、**/single**、**/merge** 均可加自定义文字：`/merge 我的标题`（替换原消息文字）\n\n"
        "⚙️ **个性化**\n"
        "• **/settings** — 重命名标签 / 标题 / 缩略图 / 会话等设置\n"
    ),
    (
        "📝 **机器人命令概览（2/2）**：\n\n"
        "💎 **会员**\n"
        "• **/status** — 查看登录与会员状态\n"
        "• **/myplan** — 查看您的会员套餐\n"
        "• **/plan** — 查看会员方案\n"
        "• **/pay** — 开通 / 续费会员\n"
        "• **/transfer** — 将会员转赠他人（仅高级会员）\n\n"
        "ℹ️ **其他**\n"
        "• **/help** — 查看本帮助\n"
        "• **/terms** — 条款和条件\n"
        "• **/add** — 添加会员（仅管理员）\n"
        "• **/rem** — 移除会员（仅管理员）\n\n"
        "**__由 Team SPY 提供支持__**"
    )
]
 
 
async def send_or_edit_help_page(_, message, page_number):
    if page_number < 0 or page_number >= len(help_pages):
        return
 
     
    prev_button = InlineKeyboardButton("◀️ 上一页", callback_data=f"help_prev_{page_number}")
    next_button = InlineKeyboardButton("下一页 ▶️", callback_data=f"help_next_{page_number}")
 
     
    buttons = []
    if page_number > 0:
        buttons.append(prev_button)
    if page_number < len(help_pages) - 1:
        buttons.append(next_button)
 
     
    keyboard = InlineKeyboardMarkup([buttons])
 
     
    await message.delete()
 
     
    await message.reply(
        help_pages[page_number],
        reply_markup=keyboard
    )
 
 
@app.on_message(filters.command("help"))
async def help(client, message):
    join = await subscribe(client, message)
    if join == 1:
        return
     
    await send_or_edit_help_page(client, message, 0)
 
 
@app.on_callback_query(filters.regex(r"help_(prev|next)_(\d+)"))
async def on_help_navigation(client, callback_query):
    action, page_number = callback_query.data.split("_")[1], int(callback_query.data.split("_")[2])
 
    if action == "prev":
        page_number -= 1
    elif action == "next":
        page_number += 1

    await send_or_edit_help_page(client, callback_query.message, page_number)
     
    await callback_query.answer()

 
@app.on_message(filters.command("terms") & filters.private)
async def terms(client, message):
    terms_text = (
        "> 📜 **条款和条件** 📜\n\n"
        "✨ 我们不对用户的行为负责，也不推广受版权保护的内容。如有用户从事此类活动，责任由其自行承担。\n"
        "✨ 购买后，我们不保证方案的在线时间、停机时间或有效性。__用户的授权和封禁由我们自行决定；我们保留随时封禁或授权用户的权利。__\n"
    )
    buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 联系开通", url=ADMIN_CONTACT)],
        ]
    )
    await message.reply_text(terms_text, reply_markup=buttons)


@app.on_message(filters.command("plan") & filters.private)
async def plan(client, message):
    await message.reply_text(PAY_NOTICE)
 
 
