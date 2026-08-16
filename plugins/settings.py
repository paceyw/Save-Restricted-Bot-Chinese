# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton as IK, InlineKeyboardMarkup as IKM
import asyncio
import re
import os
import string
import random
import time
from shared_client import app, _WORKDIR
from utils.func import get_user_data_key, save_user_data, bump_cred_epoch
try:
    from utils.encrypt import ecs, dcs
except ImportError:
    ecs = None
    dcs = None

VIDEO_EXTENSIONS = {
    'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'webm',
    'mpeg', 'mpg', '3gp'
}
SET_PIC = 'settings.jpg'
MESS = '自定义文件设置...\n\n提示：任务开始执行后修改设置，对进行中的任务不生效。\n视频下载选项为命令级开关：getav 中文字幕烧录请在下载命令加 `-sub`（如 `/dl -sub <链接>`），无需在此设置。'

active_conversations = {}
_ACTIVE_CONVERSATION_TTL = 900
def active_conversation_filter(_, __, message):
    return (
        message.from_user is not None
        and message.from_user.id in active_conversations
    )

active_conversation = filters.create(active_conversation_filter)


def _ensure_settings_sweeper():
    """Start the shared sweeper without importing tasks during module load."""
    try:
        from plugins.tasks import _ensure_sweeper
    except Exception:
        return
    try:
        _ensure_sweeper()
    except Exception:
        pass



def settings_menu():
    return IKM([
        [IK('📝 设置聊天 ID', callback_data='setchat'),
         IK('🏷️ 设置重命名标签', callback_data='setrename')],
        [IK('📋 设置标题', callback_data='setcaption'),
         IK('🔄 替换词语', callback_data='setreplacement')],
        [IK('🗑️ 删除词语', callback_data='delete'),
         IK('🔄 重置设置', callback_data='reset')],
        [IK('🔑 会话登录', callback_data='addsession'),
         IK('🚪 退出登录', callback_data='logout')],
        [IK('🖼️ 设置缩略图', callback_data='setthumb'),
         IK('❌ 移除缩略图', callback_data='remthumb')],
        [IK('🆘 报告错误', url='https://t.me/team_spy_pro')]
    ])


@app.on_message(filters.command("settings"))
async def settings_command(client, message):
    _ensure_settings_sweeper()
    user_id = message.from_user.id
    await app.send_message(message.chat.id, MESS, reply_markup=settings_menu())


async def send_settings_message(chat_id, user_id):
    await app.send_message(chat_id, MESS, reply_markup=settings_menu())


@app.on_callback_query(filters.regex(
    r'^(setchat|setrename|setcaption|setreplacement|addsession|delete|setthumb|logout|reset|remthumb)$'))
async def callback_query_handler(client, query):
    user_id = query.from_user.id
    data = query.data

    callback_actions = {
        'setchat': {
            'type': 'setchat',
            'message': """请向我发送该聊天的 ID（带 -100 前缀）： 
__👉 **注意：** 如果您使用自定义机器人，您的机器人必须是该聊天的管理员，否则本机器人必须是管理员。__
👉 __如果您想上传到群组中的话题以及特定话题，请按 **-100CHANNELID/TOPIC_ID** 这种格式传入聊天 ID，例如：**-1004783898/12**__"""
        },
        'setrename': {
            'type': 'setrename',
            'message': '请向我发送重命名标签：'
        },
        'setcaption': {
            'type': 'setcaption',
            'message': '请向我发送标题：'
        },
        'setreplacement': {
            'type': 'setreplacement',
            'message': "请按以下格式发送替换词：'WORD(s)' 'REPLACEWORD'"
        },
        'addsession': {
            'type': 'addsession',
            'message': '请发送 Pyrogram V2 会话字符串：'
        },
        'delete': {
            'type': 'deleteword',
            'message': '请用空格分隔发送要从标题/文件名中删除的词语...'
        },
        'setthumb': {
            'type': 'setthumb',
            'message': '请发送要设置为缩略图的照片。'
        }
    }

    if data in callback_actions:
        action = callback_actions[data]
        await start_conversation(query, user_id, action['type'], action['message'])
    elif data == 'logout':
        await _do_logout(user_id, query)
    elif data == 'reset':
        await _do_reset(user_id, query)
    elif data == 'remthumb':
        await _do_remthumb(user_id, query)

    await query.answer()
async def _stop_cached_user_client(user_id):
    from plugins.fetch import user_clients, premium_userbot, _client_lock, _UC_EPOCH
    async with _client_lock(user_id):
        old_client = user_clients.pop(user_id, None)
        _UC_EPOCH.pop(user_id, None)
        if old_client is not None and old_client is not premium_userbot:
            try:
                await old_client.stop()
            except Exception:
                pass


async def _do_logout(user_id, query):
    from utils.func import users_collection
    result = await users_collection.update_one(
        {'user_id': user_id},
        {'$unset': {'session_string': ''}}
    )
    bump_cred_epoch(user_id)
    await _stop_cached_user_client(user_id)
    if result.modified_count > 0:
        await query.message.reply_text('已成功退出登录并删除会话。')
    else:
        await query.message.reply_text('您尚未登录。')


async def _do_reset(user_id, query):
    from utils.func import users_collection
    try:
        await users_collection.update_one(
            {'user_id': user_id},
            {'$unset': {
                'delete_words': '',
                'replacement_words': '',
                'rename_tag': '',
                'caption': '',
                'chat_id': ''
            }}
        )
        thumbnail_path = os.path.join(_WORKDIR, f'{user_id}.jpg')
        if os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)
        await query.message.reply_text('✅ 所有设置已成功重置。如需退出登录，请点击 /logout')
    except Exception as e:
        await query.message.reply_text(f'重置设置时出错：{e}')


async def _do_remthumb(user_id, query):
    try:
        os.remove(f'{user_id}.jpg')
        await query.message.reply_text('缩略图已成功移除！')
    except FileNotFoundError:
        await query.message.reply_text('未找到可移除的缩略图。')


async def start_conversation(query, user_id, conv_type, prompt_message):
    _ensure_settings_sweeper()
    if user_id in active_conversations:
        await query.message.reply_text('上一次对话已取消，开始新的对话。')

    msg = await query.message.reply_text(f'{prompt_message}\n\n（发送 /cancel 取消此操作）')
    active_conversations[user_id] = {
        'type': conv_type,
        'message_id': msg.id,
        'ts': time.time(),
    }


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_conversation(client, message):
    user_id = message.from_user.id
    if user_id in active_conversations:
        await message.reply_text('已取消。')
        del active_conversations[user_id]


@app.on_message(filters.private & active_conversation & ~filters.command([
    'start', 'batch', 'cancel', 'login', 'logout', 'stop', 'set',
    'pay', 'redeem', 'gencode', 'single', 'generate', 'keyinfo', 'encrypt', 'decrypt', 'keys',
    'setbot', 'rembot', 'status', 'myplan', 'transfer', 'rem', 'add', 'plan', 'terms', 'help',
    'settings', 'dl', 'adl']))
async def handle_conversation_input(client, message):
    user_id = message.from_user.id
    if user_id not in active_conversations:
        return
    if message.text and message.text.startswith('/'):
        return

    active_conversations[user_id]['ts'] = time.time()
    conv_type = active_conversations[user_id]['type']

    handlers = {
        'setchat': handle_setchat,
        'setrename': handle_setrename,
        'setcaption': handle_setcaption,
        'setreplacement': handle_setreplacement,
        'addsession': handle_addsession,
        'deleteword': handle_deleteword,
        'setthumb': handle_setthumb
    }

    if conv_type in handlers:
        await handlers[conv_type](message, user_id)

    if user_id in active_conversations:
        del active_conversations[user_id]


async def handle_setchat(message, user_id):
    try:
        chat_id = message.text.strip()
        try:
            chat_id_int = int(chat_id)
        except ValueError:
            await message.reply_text('❌ 无效的聊天 ID，请输入数字 ID（频道/群组通常为 -100 开头的负数）。')
            return
        if chat_id_int >= 0:
            await message.reply_text('❌ 聊天 ID 必须为负数（频道/群组 ID），例如 -1001234567890。')
            return
        await save_user_data(user_id, 'chat_id', chat_id)
        await message.reply_text('✅ 聊天 ID 设置成功！')
    except Exception as e:
        await message.reply_text(f'❌ 设置聊天 ID 时出错：{e}')

async def handle_setrename(message, user_id):
    rename_tag = message.text.strip()
    await save_user_data(user_id, 'rename_tag', rename_tag)
    await message.reply_text(f'✅ 重命名标签已设置为：{rename_tag}')

async def handle_setcaption(message, user_id):
    caption = message.text
    await save_user_data(user_id, 'caption', caption)
    await message.reply_text('✅ 标题设置成功！')

async def handle_setreplacement(message, user_id):
    match = re.match(r"^'([^']+)'\s+'([^']+)'$", message.text)
    if not match:
        await message.reply_text("❌ 格式无效。用法：'WORD(s)' 'REPLACEWORD'")
    else:
        word, replace_word = match.groups()
        delete_words = await get_user_data_key(user_id, 'delete_words', [])
        if word in delete_words:
            await message.reply_text(f"❌ 词语 '{word}' 在删除列表中，无法替换。")
        else:
            replacements = await get_user_data_key(user_id, 'replacement_words', {})
            replacements[word] = replace_word
            await save_user_data(user_id, 'replacement_words', replacements)
            await message.reply_text(f"✅ 已保存替换规则：'{word}' 将替换为 '{replace_word}'")

def _prepare_session_string(session_string):
    encrypt_session = ecs
    decrypt_session = dcs
    if encrypt_session is None or decrypt_session is None:
        from utils.encrypt import ecs as encrypt_session, dcs as decrypt_session
    try:
        if ':' in decrypt_session(session_string):
            return session_string
    except Exception:
        pass
    return encrypt_session(session_string)


async def handle_addsession(message, user_id):
    session_string = message.text.strip()
    encrypted_session = _prepare_session_string(session_string)
    await save_user_data(user_id, 'session_string', encrypted_session)
    await message.reply_text('✅ 会话字符串添加成功！')


async def handle_deleteword(message, user_id):
    words_to_delete = message.text.split()
    delete_words = await get_user_data_key(user_id, 'delete_words', [])
    delete_words = list(set(delete_words + words_to_delete))
    await save_user_data(user_id, 'delete_words', delete_words)
    await message.reply_text(f"✅ 已添加到删除列表的词语：{', '.join(words_to_delete)}")

async def handle_setthumb(message, user_id):
    if message.photo:
        # Absolute path under the writable volume; pyrofork would otherwise
        # resolve "downloads/" against PARENT_DIR (/app, read-only).
        download_path = os.path.join(_WORKDIR, 'downloads', f'thumb_{user_id}.jpg')
        temp_path = await app.download_media(message, file_name=download_path)
        if not temp_path:
            await message.reply_text('❌ 下载缩略图失败，请重试。')
            return
        try:
            # Must match utils.func.thumbnail(): absolute path under the
            # writable volume - cwd is /app (read-only) in the container.
            thumb_path = os.path.join(_WORKDIR, f'{user_id}.jpg')
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
            os.rename(temp_path, thumb_path)
            await message.reply_text('✅ 缩略图保存成功！')
        except Exception as e:
            await message.reply_text(f'❌ 保存缩略图时出错：{e}')
    else:
        await message.reply_text('❌ 请发送照片，操作已取消。')

def generate_random_name(length=7):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))


async def rename_file(file, sender, edit, settings):
    try:
        delete_words = settings.get('delete_words', [])
        custom_rename_tag = settings.get('rename_tag', '')
        replacements = settings.get('replacement_words', {})
        
        last_dot_index = str(file).rfind('.')
        if last_dot_index != -1 and last_dot_index != 0:
            ggn_ext = str(file)[last_dot_index + 1:]
            if ggn_ext.isalpha() and len(ggn_ext) <= 9:
                if ggn_ext.lower() in VIDEO_EXTENSIONS:
                    original_file_name = str(file)[:last_dot_index]
                    file_extension = 'mp4'
                else:
                    original_file_name = str(file)[:last_dot_index]
                    file_extension = ggn_ext
            else:
                original_file_name = str(file)[:last_dot_index]
                file_extension = 'mp4'
        else:
            original_file_name = str(file)
            file_extension = 'mp4'

        for word in delete_words:
            original_file_name = original_file_name.replace(word, '')
        for word, replace_word in replacements.items():
            original_file_name = original_file_name.replace(word, replace_word)
        new_file_name = f'{original_file_name} {custom_rename_tag}.{file_extension}'
        os.rename(file, new_file_name)
        return new_file_name
    except Exception as e:
        print(f"Rename error: {e}")
        return file

async def _sweep_active_conversations():
    now = time.time()
    for user_id, state in list(active_conversations.items()):
        if now - state.get('ts', now) > _ACTIVE_CONVERSATION_TTL:
            active_conversations.pop(user_id, None)



