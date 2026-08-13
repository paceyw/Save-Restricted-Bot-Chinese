# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import BadRequest, SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, MessageNotModified
import asyncio
import logging
import os
import re
import time
import unicodedata
from config import API_HASH, API_ID
from shared_client import app as bot, _WORKDIR
from utils.func import save_user_session, get_user_data, remove_user_session, save_user_bot, remove_user_bot
from utils.encrypt import ecs, dcs
from plugins.batch import UB, UC
try:
    from plugins.batch import Y, _client_lock
except ImportError:
    Y = None
    _LOGIN_LOCKS = {}

    def _client_lock(user_id):
        lock = _LOGIN_LOCKS.get(user_id)
        if lock is None:
            lock = _LOGIN_LOCKS[user_id] = asyncio.Lock()
        return lock
try:
    from plugins.batch import _ensure_sweeper
except Exception:
    def _ensure_sweeper():
        return None
from utils.custom_filters import login_in_progress, set_user_step, get_user_step
try:
    from utils.custom_filters import user_steps
except ImportError:
    user_steps = None
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
model = "v3saver Team SPY"

STEP_PHONE = 1
STEP_CODE = 2
STEP_PASSWORD = 3
LOGIN_TTL = 10 * 60
login_cache = {}
login_step_times = {}
login_locks = {}


def _get_login_lock(user_id):
    _ensure_sweeper()
    lock = login_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        login_locks[user_id] = lock
    return lock


def _set_login_step(user_id, step=None):
    set_user_step(user_id, step)
    if step is None:
        login_step_times.pop(user_id, None)
    else:
        login_step_times[user_id] = time.monotonic()


def _login_state_expired(user_id):
    now = time.monotonic()
    cache = login_cache.get(user_id)
    step = get_user_step(user_id)
    if cache is None and step is not None:
        return True
    timestamps = []
    if cache is not None:
        created_at = cache.get('created_at')
        if created_at is None:
            created_at = now
            cache['created_at'] = created_at
        timestamps.append(created_at)
    step_started_at = login_step_times.get(user_id)
    if step is not None and step_started_at is None:
        step_started_at = cache.get('created_at', now) if cache else now
        login_step_times[user_id] = step_started_at
    if step_started_at is not None:
        timestamps.append(step_started_at)
    return any(now - timestamp > LOGIN_TTL for timestamp in timestamps)


async def _clear_login_state(user_id, temp_client=None):
    await cleanup_temp_login(user_id, temp_client)
    login_cache.pop(user_id, None)
    _set_login_step(user_id, None)


async def _stop_cached_user_client_locked(user_id):
    old_client = UC.pop(user_id, None)
    if old_client is not None and old_client is not Y:
        try:
            await old_client.stop()
        except Exception as e:
            logger.warning(f'Error stopping cached user client for {user_id}: {e}')


async def _stop_cached_user_client(user_id):
    async with _client_lock(user_id):
        await _stop_cached_user_client_locked(user_id)


async def _complete_login(user_id, status_msg, temp_client, session_string):
    encrypted_session = ecs(session_string)
    if not await save_user_session(user_id, encrypted_session):
        await cleanup_temp_login(user_id, temp_client)
        login_cache.pop(user_id, None)
        _set_login_step(user_id, None)
        await edit_message_safely(status_msg, '登录状态保存失败,请重试')
        return False

    await cleanup_temp_login(user_id, temp_client)
    await _stop_cached_user_client(user_id)
    temp_status_msg = login_cache.get(user_id, {}).get('status_msg', status_msg)
    login_cache.pop(user_id, None)
    login_cache[user_id] = {
        'status_msg': temp_status_msg,
        'created_at': time.monotonic(),
    }
    await edit_message_safely(status_msg, '✅ 登录成功！！')
    _set_login_step(user_id, None)
    return True


def _mask_phone(phone):
    """Mask a phone number for logs: keep country prefix shape and last 2 digits."""
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) <= 4:
        return '***'
    return f'+{digits[:2]}***{digits[-2:]}'


def normalize_login_code(text):
    """Extract digit characters from various user inputs.
    
    Accepts formats like: 12345, 1 2 3 4 5, 1-2-3-4-5, s12345, etc.
    Telegram immediately invalidates codes sent in plain text, so users must obfuscate.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    digits = []
    for char in normalized:
        if char.isdecimal():
            digits.append(str(unicodedata.digit(char)))
        elif char.isspace() or char in "-_.,;:/*\\|=+()[]{}~`!@#$%^&<>?":
            continue
        else:
            # Any other letter/symbol makes the input invalid — user must use pure digits + separators
            return ""
    return "".join(digits)


def extract_digits(text):
    """Extract only decimal digits from any input (letters, symbols, whitespace ignored).
    
    Used as a robust fallback when users obfuscate codes like 's12345' or '1a2a3a4a5'.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(
        str(unicodedata.digit(char))
        for char in normalized
        if char.isdecimal()
    )


async def cleanup_temp_login(user_id, temp_client=None):
    if temp_client is None:
        temp_client = login_cache.get(user_id, {}).get("temp_client")
    if temp_client is not None:
        try:
            await temp_client.disconnect()
        except Exception:
            pass
    for ext in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(os.path.join(_WORKDIR, f"temp_{user_id}.session{ext}"))
        except OSError:
            pass
async def _sweep_login_state():
    """Expire idle login flows and discard their lock-only state."""
    now = time.monotonic()

    for user_id in list(login_cache):
        cache = login_cache.get(user_id)
        if cache is None:
            continue
        created_at = cache.get('created_at')
        if created_at is None:
            cache['created_at'] = now
            continue
        if now - created_at <= LOGIN_TTL:
            continue

        lock = login_locks.get(user_id)
        if lock is not None and lock.locked():
            continue
        if lock is None:
            lock = _get_login_lock(user_id)
        async with lock:
            cache = login_cache.get(user_id)
            if cache is None:
                continue
            created_at = cache.get('created_at')
            if created_at is None:
                cache['created_at'] = now
                continue
            if now - created_at <= LOGIN_TTL:
                continue
            try:
                await cleanup_temp_login(user_id)
            except Exception as exc:
                logger.warning("Error cleaning expired login state for %s: %s", user_id, exc)
            login_cache.pop(user_id, None)
            login_step_times.pop(user_id, None)
            set_user_step(user_id, None)

    for user_id, timestamp in list(login_step_times.items()):
        if user_id in login_cache:
            continue
        lock = login_locks.get(user_id)
        if lock is not None and lock.locked():
            continue
        if lock is None:
            lock = _get_login_lock(user_id)
        async with lock:
            if user_id in login_cache:
                continue
            step = get_user_step(user_id)
            if step is None:
                login_step_times.pop(user_id, None)
                continue
            if now - timestamp > LOGIN_TTL:
                set_user_step(user_id, None)
                login_step_times.pop(user_id, None)

    if user_steps is not None:
        for user_id in list(user_steps):
            if user_id in login_cache or user_id in login_step_times:
                continue
            lock = login_locks.get(user_id)
            if lock is not None and lock.locked():
                continue
            if lock is None:
                lock = _get_login_lock(user_id)
            async with lock:
                if user_id in login_cache or user_id in login_step_times:
                    continue
                set_user_step(user_id, None)

    for lock_map in (login_locks, globals().get('_LOGIN_LOCKS', {})):
        for user_id, lock in list(lock_map.items()):
            if (
                user_id not in login_cache
                and get_user_step(user_id) is None
                and not lock.locked()
            ):
                lock_map.pop(user_id, None)

async def request_login_code(user_id, temp_client, phone):
    sent_code = await temp_client.send_code(phone)
    login_cache[user_id].setdefault('created_at', time.monotonic())
    login_cache[user_id]['phone'] = phone
    login_cache[user_id]['phone_code_hash'] = sent_code.phone_code_hash
    login_cache[user_id]['temp_client'] = temp_client
    login_cache[user_id]['code_sent_at'] = time.monotonic()
    login_cache[user_id]['dc_id'] = await temp_client.storage.dc_id()
    return sent_code

@bot.on_message(filters.command('login'))
async def login_command(client, message):
    _ensure_sweeper()
    user_id = message.from_user.id
    async with _get_login_lock(user_id):
        await _clear_login_state(user_id)
        _set_login_step(user_id, STEP_PHONE)
        await message.delete()
        status_msg = await message.reply(
            """请发送带国家区号的手机号码
示例：`+12345678900`"""
            )
        login_cache[user_id] = {
            'status_msg': status_msg,
            'created_at': time.monotonic(),
        }
    
    
@bot.on_message(filters.command("setbot"))
async def set_bot_token(C, m):
    user_id = m.from_user.id
    command_text = (m.text or "").strip()
    args = command_text.split(maxsplit=1)

    if len(args) < 2 or not args[1].strip():
        await m.reply_text("⚠️ 请提供机器人令牌。用法：`/setbot token`", quote=True)
        return

    bot_token = args[1].strip()
    if not await save_user_bot(user_id, bot_token):
        await m.reply_text("❌ 机器人令牌保存失败，请稍后重试。", quote=True)
        return

    session_path = os.path.join(_WORKDIR, f"user_{user_id}.session")
    async with _client_lock(user_id):
        if user_id in UB:
            try:
                await UB[user_id].stop()
                print(f"Stopped old bot for user {user_id}")
            except Exception as e:
                print(f"Error stopping old bot for user {user_id}: {e}")
            finally:
                UB.pop(user_id, None)

        try:
            if os.path.exists(session_path):
                os.remove(session_path)
        except Exception as e:
            print(f"Error removing bot session for user {user_id}: {e}")

    await m.reply_text("✅ 机器人令牌保存成功。", quote=True)


@bot.on_message(filters.command("rembot"))
async def rem_bot_token(C, m):
    user_id = m.from_user.id
    await remove_user_bot(user_id)

    session_path = os.path.join(_WORKDIR, f"user_{user_id}.session")
    async with _client_lock(user_id):
        if user_id in UB:
            try:
                await UB[user_id].stop()
                print(f"Stopped old bot for user {user_id}")
            except Exception as e:
                print(f"Error stopping old bot for user {user_id}: {e}")
            finally:
                UB.pop(user_id, None)

        try:
            if os.path.exists(session_path):
                os.remove(session_path)
        except Exception as e:
            print(f"Error removing bot session for user {user_id}: {e}")

    await m.reply_text("✅ 机器人令牌已成功移除。", quote=True)



    
@bot.on_message(login_in_progress & filters.text & filters.private & ~filters.command([
    'start', 'batch', 'cancel', 'login', 'logout', 'stop', 'set', 'pay',
    'redeem', 'gencode', 'generate', 'keyinfo', 'encrypt', 'decrypt', 'keys', 'setbot', 'rembot']))
async def handle_login_steps(client, message):
    _ensure_sweeper()
    user_id = message.from_user.id
    async with _get_login_lock(user_id):
        status_msg = login_cache.get(user_id, {}).get('status_msg')
        if _login_state_expired(user_id):
            await _clear_login_state(user_id)
            try:
                await message.delete()
            except Exception:
                pass
            if status_msg:
                await edit_message_safely(
                    status_msg,
                    '❌ 登录流程已过期，请使用 /login 重新开始。',
                )
            else:
                await message.reply('❌ 登录流程已过期，请使用 /login 重新开始。')
            return
        await _handle_login_steps(client, message)


async def _handle_login_steps(client, message):
    user_id = message.from_user.id
    text = (message.text or "")
    if get_user_step(user_id) != STEP_PASSWORD:
        text = text.strip()
    step = get_user_step(user_id)

    try:
        await message.delete()
    except Exception as e:
        logger.warning(f'Could not delete message: {e}')

    status_msg = login_cache[user_id].get('status_msg')
    if not status_msg:
        status_msg = await message.reply('处理中...')
        login_cache[user_id]['status_msg'] = status_msg

    temp_client = None
    try:
        if step == STEP_PHONE:
            if not text.startswith('+'):
                await edit_message_safely(
                    status_msg,
                    '❌ 请提供以 + 开头的有效手机号码',
                )
                return

            await edit_message_safely(status_msg, '🔄 正在处理手机号码...')
            temp_client = Client(
                f'temp_{user_id}',
                api_id=API_ID,
                api_hash=API_HASH,
                device_model=model,
                workdir=_WORKDIR,
                in_memory=True,
            )
            try:
                await temp_client.connect()
                sent_code = await request_login_code(user_id, temp_client, text)
                logger.info(
                    f'LOGIN send_code OK phone={_mask_phone(text)} type={sent_code.type} '
                    f'dc={login_cache[user_id]["dc_id"]}'
                )
                _set_login_step(user_id, STEP_CODE)
                await edit_message_safely(
                    status_msg,
                    """✅ 验证码已发送到您的 Telegram 账户。

⚠️ **请勿直接发送原始验证码**（Telegram 会将其立即失效）。
请按以下任一格式输入（Bot 会自动提取数字）：
• `1 2 3 4 5`
• `s12345`
• `1-2-3-4-5`""",
                )
            except BadRequest as e:
                await edit_message_safely(
                    status_msg,
                    f"""❌ 错误：{str(e)}
请使用 /login 重试。""",
                )
                await cleanup_temp_login(user_id, temp_client)
                _set_login_step(user_id, None)

        elif step == STEP_CODE:
            # Extract digits from any format (plain, spaced, letter-mixed)
            code = extract_digits(text)
            if not code:
                # Fallback to strict validation for truly invalid inputs
                code = normalize_login_code(text)
                if not code:
                    await edit_message_safely(
                        status_msg,
                        '❌ 无法识别验证码。请使用混淆格式输入（如 `1 2 3 4 5` 或 `s12345`）：',
                    )
                    return

            phone = login_cache[user_id]['phone']
            phone_code_hash = login_cache[user_id]['phone_code_hash']
            temp_client = login_cache[user_id]['temp_client']
            code_age = time.monotonic() - login_cache[user_id].get(
                'code_sent_at',
                time.monotonic(),
            )
            logger.info(
                f'LOGIN sign_in: phone={_mask_phone(phone)} '
                f'raw_length={len(text)} '
                f'whitespace_count={sum(char.isspace() for char in text)} '
                f'code_length={len(code)} '
                f'code_age={code_age:.1f}s '
                f'dc={login_cache[user_id].get("dc_id", "?")}'
            )

            try:
                await edit_message_safely(status_msg, '🔄 正在验证验证码...')
                await temp_client.sign_in(phone, phone_code_hash, code)
                session_string = await temp_client.export_session_string()
                await _complete_login(
                    user_id,
                    status_msg,
                    temp_client,
                    session_string,
                )
            except PhoneCodeExpired:
                logger.warning(
                    f'LOGIN PhoneCodeExpired after {code_age:.1f}s; '
                    'requesting a fresh code'
                )
                try:
                    sent_code = await request_login_code(
                        user_id,
                        temp_client,
                        phone,
                    )
                    _set_login_step(user_id, STEP_CODE)
                    await edit_message_safely(
                        status_msg,
                        """❌ 上一个验证码已失效。
已重新发送验证码，**请勿直接发送原始验证码**，请用混淆格式输入（如 `1 2 3 4 5` 或 `s12345`）：""",
                    )
                    logger.info(
                        f'LOGIN resend_code OK type={sent_code.type} '
                        f'dc={login_cache[user_id]["dc_id"]}'
                    )
                except Exception as resend_error:
                    logger.error(
                        f'LOGIN resend_code failed: '
                        f'{type(resend_error).__name__}: {resend_error}'
                    )
                    await cleanup_temp_login(user_id, temp_client)
                    login_cache.pop(user_id, None)
                    _set_login_step(user_id, None)
                    await edit_message_safely(
                        status_msg,
                        '❌ 验证码已失效且重新发送失败，请重新发送 /login。',
                    )
            except SessionPasswordNeeded:
                _set_login_step(user_id, STEP_PASSWORD)
                await edit_message_safely(
                    status_msg,
                    """🔒 已启用两步验证。
请输入您的密码：""",
                )
            except PhoneCodeInvalid as e:
                logger.warning('LOGIN PhoneCodeInvalid; keeping session for retry')
                await edit_message_safely(
                    status_msg,
                    f'❌ 验证码错误：{str(e)}。请重新输入当前验证码。',
                )
            except Exception as e:
                logger.error(
                    f'LOGIN sign_in failed: {type(e).__name__}: {e}'
                )
                await edit_message_safely(
                    status_msg,
                    f'❌ {str(e)}。请使用 /login 重试。',
                )
                await cleanup_temp_login(user_id, temp_client)
                login_cache.pop(user_id, None)
                _set_login_step(user_id, None)

        elif step == STEP_PASSWORD:
            temp_client = login_cache[user_id]['temp_client']
            try:
                await edit_message_safely(status_msg, '🔄 正在验证密码...')
                await temp_client.check_password(text)
                session_string = await temp_client.export_session_string()
                await _complete_login(
                    user_id,
                    status_msg,
                    temp_client,
                    session_string,
                )
            except BadRequest as e:
                await edit_message_safely(
                    status_msg,
                    f"""❌ 密码错误：{str(e)}
请重试：""",
                )
    except Exception as e:
        logger.error(f'Error in login flow: {str(e)}')
        await edit_message_safely(
            status_msg,
            f"""❌ 发生错误：{str(e)}
请使用 /login 重试。""",
        )
        await cleanup_temp_login(user_id, temp_client)
        login_cache.pop(user_id, None)
        _set_login_step(user_id, None)
async def edit_message_safely(message, text):
    """Helper function to edit message and handle errors"""
    try:
        await message.edit(text)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f'Error editing message: {e}')
        
@bot.on_message(filters.command('cancel'))
async def cancel_command(client, message):
    user_id = message.from_user.id
    await message.delete()
    if get_user_step(user_id):
        status_msg = login_cache.get(user_id, {}).get('status_msg')
        await _clear_login_state(user_id)
        if status_msg:
            await edit_message_safely(status_msg,
                '✅ 登录流程已取消。使用 /login 重新开始。')
        else:
            temp_msg = await message.reply(
                '✅ 登录流程已取消。使用 /login 重新开始。')
    else:
        temp_msg = await message.reply('没有正在进行的登录流程可取消。')
        await temp_msg.delete(5)
        
@bot.on_message(filters.command('logout'))
async def logout_command(client, message):
    user_id = message.from_user.id
    await message.delete()
    status_msg = await message.reply('🔄 正在处理退出登录请求...')
    try:
        session_data = await get_user_data(user_id)
        
        if not session_data or 'session_string' not in session_data:
            await _stop_cached_user_client(user_id)
            await edit_message_safely(status_msg,
                '❌ 未找到您账户的有效会话。')


            return
        encss = session_data['session_string']
        session_string = dcs(encss)
        temp_client = Client(f'temp_logout_{user_id}', api_id=API_ID,
            api_hash=API_HASH, session_string=session_string)
        try:
            await temp_client.connect()
            await temp_client.log_out()
            await edit_message_safely(status_msg,
                '✅ Telegram 会话已成功终止。正在从数据库中移除...'
                )
        except Exception as e:
            logger.error(f'Error terminating session: {str(e)}')
            await edit_message_safely(status_msg,
                f"""⚠️ 终止 Telegram 会话时出错：{str(e)}
仍将从数据库中移除..."""
                )
        finally:
            await temp_client.disconnect()
        await remove_user_session(user_id)
        await edit_message_safely(status_msg,
            '✅ 已成功退出登录！！')
        try:
            if os.path.exists(f"{user_id}_client.session"):
                os.remove(f"{user_id}_client.session")
        except Exception:
            pass
        await _stop_cached_user_client(user_id)
    except Exception as e:
        logger.error(f'Error in logout command: {str(e)}')
        try:
            await remove_user_session(user_id)
        except Exception:
            pass
        await _stop_cached_user_client(user_id)
        await edit_message_safely(status_msg,
            f'❌ 退出登录时发生错误：{str(e)}')
        try:
            if os.path.exists(f"{user_id}_client.session"):
                os.remove(f"{user_id}_client.session")
        except Exception:
            pass

try:
    from plugins.batch import register_sweep_hook
    register_sweep_hook(_sweep_login_state)
except Exception:
    pass
