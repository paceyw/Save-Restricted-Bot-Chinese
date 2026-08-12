# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

import os, re, time, asyncio, json, asyncio 
from pyrogram import Client, filters
from pyrogram.types import Message, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from pyrogram.errors import UserNotParticipant, FloodWait
from config import API_ID, API_HASH, LOG_GROUP, STRING, FORCE_SUB, FREEMIUM_LIMIT, PREMIUM_LIMIT
from utils.func import get_user_data, screenshot, thumbnail, get_video_metadata
from utils.func import get_user_data_key, process_text_with_rules, is_premium_user, E
from shared_client import app as X, _WORKDIR
from plugins.settings import rename_file
from plugins.start import subscribe as sub
from utils.custom_filters import login_in_progress
from utils.encrypt import dcs
from typing import Dict, Any, Optional


Y = None if not STRING else __import__('shared_client').userbot
Z, P, UB, UC, emp = {}, {}, {}, {}, {}

ACTIVE_USERS = {}
ACTIVE_USERS_FILE = "active_users.json"

# fixed directory file_name problems 
def sanitize(filename):
    return re.sub(r'[<>:"/\\|?*\']', '_', filename).strip(" .")[:255]

def load_active_users():
    try:
        if os.path.exists(ACTIVE_USERS_FILE):
            with open(ACTIVE_USERS_FILE, 'r') as f:
                return json.load(f)
        return {}
    except Exception:
        return {}

async def save_active_users_to_file():
    try:
        with open(ACTIVE_USERS_FILE, 'w') as f:
            json.dump(ACTIVE_USERS, f)
    except Exception as e:
        print(f"Error saving active users: {e}")

async def add_active_batch(user_id: int, batch_info: Dict[str, Any]):
    ACTIVE_USERS[str(user_id)] = batch_info
    await save_active_users_to_file()

def is_user_active(user_id: int) -> bool:
    return str(user_id) in ACTIVE_USERS

async def update_batch_progress(user_id: int, current: int, success: int):
    if str(user_id) in ACTIVE_USERS:
        ACTIVE_USERS[str(user_id)]["current"] = current
        ACTIVE_USERS[str(user_id)]["success"] = success
        await save_active_users_to_file()

async def request_batch_cancel(user_id: int):
    if str(user_id) in ACTIVE_USERS:
        ACTIVE_USERS[str(user_id)]["cancel_requested"] = True
        await save_active_users_to_file()
        return True
    return False

def should_cancel(user_id: int) -> bool:
    user_str = str(user_id)
    return user_str in ACTIVE_USERS and ACTIVE_USERS[user_str].get("cancel_requested", False)

async def remove_active_batch(user_id: int):
    if str(user_id) in ACTIVE_USERS:
        del ACTIVE_USERS[str(user_id)]
        await save_active_users_to_file()

def get_batch_info(user_id: int) -> Optional[Dict[str, Any]]:
    return ACTIVE_USERS.get(str(user_id))

ACTIVE_USERS = load_active_users()

async def upd_dlg(c):
    try:
        async for _ in c.get_dialogs(limit=100): pass
        return True
    except Exception as e:
        print(f'Failed to update dialogs: {e}')
        return False

# fixed the old group of 2021-2022 extraction 🌝 (buy krne ka fayda nhi ab old group) ✅ 
async def get_msg(c, u, i, d, lt):
    try:
        if lt == 'public':
            clients = []
            if u:
                clients.append(('user', u, False))
            if c and c is not u:
                clients.append(('bot', c, True))

            for label, client, fetched_by_bot in clients:
                try:
                    xm = await client.get_messages(i, d)
                except Exception as e:
                    print(f'Error fetching public message with {label} client: {e}')
                    continue

                if xm and not getattr(xm, 'empty', False):
                    emp[i] = not fetched_by_bot
                    print(f'Fetched public message with {label} client')
                    return xm

            if u:
                try:
                    await u.join_chat(i)
                    chat = await u.get_chat(f'@{i}')
                    xm = await u.get_messages(chat.id, d)
                    if xm and not getattr(xm, 'empty', False):
                        emp[i] = True
                        return xm
                except Exception as e:
                    print(f'Error joining public chat {i}: {e}')

            return None

        if not u:
            return None

        try:
            async for _ in u.get_dialogs(limit=50):
                pass

            # Try with -100 prefix first
            if str(i).startswith('-100'):
                chat_id_100 = i
                base_id = str(i)[4:]
                chat_id_dash = f"-{base_id}"
            elif i.isdigit():
                chat_id_100 = f"-100{i}"
                chat_id_dash = f"-{i}"
            else:
                chat_id_100 = i
                chat_id_dash = i

            try:
                result = await u.get_messages(chat_id_100, d)
                if result and not getattr(result, "empty", False):
                    return result
            except Exception:
                pass

            try:
                result = await u.get_messages(chat_id_dash, d)
                if result and not getattr(result, "empty", False):
                    return result
            except Exception:
                pass

            try:
                async for _ in u.get_dialogs(limit=200):
                    pass
                result = await u.get_messages(i, d)
                if result and not getattr(result, "empty", False):
                    return result
            except Exception:
                pass

            return None
        except Exception as e:
            print(f'Private channel error: {e}')
            return None
    except Exception as e:
        print(f'Error fetching message: {e}')
        return None


async def get_ubot(uid):
    bt = await get_user_data_key(uid, "bot_token", None)
    if isinstance(bt, str):
        bt = bt.strip()
    if not bt:
        return None
    if uid in UB:
        return UB.get(uid)

    bot = None
    try:
        bot = Client(
            f"user_{uid}",
            bot_token=bt,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=_WORKDIR,
        )
        await bot.start()
        UB[uid] = bot
        return bot
    except Exception as e:
        if bot is not None:
            try:
                await bot.stop()
            except Exception:
                pass
        print(f"Error starting bot for user {uid}: {e}")
        return None

async def get_uclient(uid):
    ud = await get_user_data(uid)
    ubot = UB.get(uid)
    cl = UC.get(uid)
    if cl: return cl
    if not ud: return ubot if ubot else None
    xxx = ud.get('session_string')
    if xxx:
        try:
            ss = dcs(xxx)
            gg = Client(f'{uid}_client', api_id=API_ID, api_hash=API_HASH, device_model="v3saver", session_string=ss, workdir=_WORKDIR)
            await gg.start()
            await upd_dlg(gg)
            UC[uid] = gg
            return gg
        except Exception as e:
            print(f'User client error: {e}')
            return None
    return Y

async def prog(c, t, C, h, m, st):
    global P
    p = c / t * 100
    interval = 10 if t >= 100 * 1024 * 1024 else 20 if t >= 50 * 1024 * 1024 else 30 if t >= 10 * 1024 * 1024 else 50
    step = int(p // interval) * interval
    if m not in P or P[m] != step or p >= 100:
        P[m] = step
        c_mb = c / (1024 * 1024)
        t_mb = t / (1024 * 1024)
        bar = '🟢' * int(p / 10) + '🔴' * (10 - int(p / 10))
        speed = c / (time.time() - st) / (1024 * 1024) if time.time() > st else 0
        eta = time.strftime('%M:%S', time.gmtime((t - c) / (speed * 1024 * 1024))) if speed > 0 else '00:00'
        await C.edit_message_text(h, m, f"__**Pyro 处理器...**__\n\n{bar}\n\n⚡**__已完成__**：{c_mb:.2f} MB / {t_mb:.2f} MB\n📊 **__完成度__**：{p:.2f}%\n🚀 **__速度__**：{speed:.2f} MB/s\n⏳ **__预计剩余时间__**：{eta}\n\n**__由 Team SPY 提供支持__**")
        if p >= 100: P.pop(m, None)

async def send_direct(c, m, tcid, ft=None, rtmid=None):
    try:
        if m.video:
            await c.send_video(
                tcid,
                m.video.file_id,
                caption=ft,
                duration=m.video.duration,
                width=m.video.width,
                height=m.video.height,
                reply_to_message_id=rtmid,
            )
        elif m.video_note:
            await c.send_video_note(
                tcid,
                m.video_note.file_id,
                reply_to_message_id=rtmid,
            )
        elif m.voice:
            await c.send_voice(
                tcid,
                m.voice.file_id,
                reply_to_message_id=rtmid,
            )
        elif m.sticker:
            await c.send_sticker(
                tcid,
                m.sticker.file_id,
                reply_to_message_id=rtmid,
            )
        elif m.audio:
            await c.send_audio(
                tcid,
                m.audio.file_id,
                caption=ft,
                duration=m.audio.duration,
                performer=m.audio.performer,
                title=m.audio.title,
                reply_to_message_id=rtmid,
            )
        elif m.photo:
            photo_id = (
                m.photo.file_id
                if hasattr(m.photo, 'file_id')
                else m.photo[-1].file_id
            )
            await c.send_photo(
                tcid,
                photo_id,
                caption=ft,
                reply_to_message_id=rtmid,
            )
        elif m.document:
            await c.send_document(
                tcid,
                m.document.file_id,
                caption=ft,
                file_name=m.document.file_name,
                reply_to_message_id=rtmid,
            )
        else:
            return False, '消息没有可直接发送的媒体'
        return True, None
    except Exception as e:
        error = str(e)
        print(f'Direct send error: {error}')
        return False, error

async def resolve_delivery(d):
    """Resolve the delivery target for user chat ``d``.

    Priority:
    1. /settings chat_id (per-user, custom bot must be admin there)
    2. LOG_GROUP from .env (deployment-level channel, custom bot must be a member)
    3. the user's own chat (fallback, delivered via the user client)

    Returns (tcid, rtmid, deliver_via_bot).
    """
    cfg_chat = await get_user_data_key(d, 'chat_id', None)
    if cfg_chat is not None:
        cfg_chat = str(cfg_chat).strip()
    tcid = d
    rtmid = None
    deliver_via_bot = False
    if cfg_chat:
        if '/' in cfg_chat:
            parts = cfg_chat.split('/', 1)
            tcid = int(parts[0])
            rtmid = int(parts[1]) if len(parts) > 1 else None
        else:
            tcid = int(cfg_chat)
        deliver_via_bot = True
    elif LOG_GROUP:
        tcid = LOG_GROUP
        deliver_via_bot = True
    elif isinstance(tcid, str):
        try:
            tcid = int(tcid)
        except ValueError:
            pass
    return tcid, rtmid, deliver_via_bot


async def _send_album_item(sender, tcid, im, rtmid):
    """Send one InputMedia item individually (fallback when SendMultiMedia rejects the group)."""
    cap = getattr(im, 'caption', None)
    if isinstance(im, InputMediaPhoto):
        return await sender.send_photo(tcid, im.media, caption=cap, reply_to_message_id=rtmid)
    if isinstance(im, InputMediaVideo):
        return await sender.send_video(
            tcid, im.media, caption=cap, duration=im.duration,
            width=im.width, height=im.height, thumb=im.thumb,
            reply_to_message_id=rtmid,
        )
    if isinstance(im, InputMediaAudio):
        return await sender.send_audio(tcid, im.media, caption=cap, duration=im.duration,
                                       reply_to_message_id=rtmid)
    return await sender.send_document(tcid, im.media, caption=cap, reply_to_message_id=rtmid)


def _flood_secs(e):
    return getattr(e, 'value', getattr(e, 'x', 10))


async def process_album(c, u, msgs, d, lt, uid, i):
    """Forward an album 1:1 — grouping, order, caption and tags preserved.

    Fast path: server-side copy_media_group (works for unrestricted chats).
    Fallback: download every item with the user client and re-upload as ONE
    media group (works for restricted content). Progress reports go to the
    user's chat with the main bot, never to the target channel.
    """
    tcid, rtmid, deliver_via_bot = await resolve_delivery(d)
    sender = c if deliver_via_bot else (u or c)
    did = int(d)
    p = await X.send_message(did, f'正在处理相册（{len(msgs)} 项）...')

    orig_caption = next((one.caption.markdown for one in msgs if one.caption), '')
    proc_text = await process_text_with_rules(d, orig_caption)
    user_cap = await get_user_data_key(d, 'caption', '')
    ft = f'{proc_text}\n\n{user_cap}' if proc_text and user_cap else user_cap if user_cap else proc_text

    if deliver_via_bot:
        try:
            await sender.copy_media_group(tcid, msgs[0].chat.id, msgs[0].id)
            await X.delete_messages(did, p.id)
            return f'✅ 相册已一比一转发（{len(msgs)} 项）'
        except Exception as e:
            print(f'copy_media_group failed, falling back to re-upload: {e}')

    st = time.time()
    media = []
    files = []
    for idx, one in enumerate(msgs):
        if not (one.photo or one.video or one.document or one.audio):
            continue
        await X.edit_message_text(did, p.id, f'正在下载 {idx + 1}/{len(msgs)}...')
        # Telegram's SendMultiMedia validates uploads by file extension
        # (PHOTO_EXT_INVALID otherwise), so the temp name must carry one.
        if one.photo:
            ext = '.jpg'
        elif one.video:
            ext = os.path.splitext(one.video.file_name or '')[1] or '.mp4'
        elif one.audio:
            ext = os.path.splitext(one.audio.file_name or '')[1] or '.mp3'
        else:
            ext = os.path.splitext(one.document.file_name or '')[1]
        f = await u.download_media(
            one,
            file_name=os.path.join(_WORKDIR, 'downloads', f'album_{int(time.time())}_{idx}{ext}'),
            progress=prog, progress_args=(X, did, p.id, st),
        )
        if not f:
            print(f'Album item {idx + 1} download failed, skipping')
            continue
        files.append(f)
        if one.photo:
            media.append(InputMediaPhoto(f))
        elif one.video:
            # Keep the source channel's thumbnail; without one Telegram shows
            # the first frame, which is often black.
            thumb_path = None
            if one.video.thumbs:
                try:
                    thumb_path = await u.download_media(
                        one.video.thumbs[-1].file_id,
                        file_name=os.path.join(
                            _WORKDIR, 'downloads',
                            f'album_thumb_{int(time.time())}_{idx}.jpg',
                        ),
                    )
                except Exception as e:
                    print(f'Thumb download failed for album item {idx + 1}: {e}')
            if thumb_path:
                files.append(thumb_path)
            media.append(InputMediaVideo(
                f, duration=one.video.duration,
                width=one.video.width, height=one.video.height,
                thumb=thumb_path,
            ))
        elif one.audio:
            media.append(InputMediaAudio(f, duration=one.audio.duration))
        else:
            media.append(InputMediaDocument(f))

    if not media:
        await X.edit_message_text(did, p.id, '相册下载失败')
        return '❌ 相册下载失败'

    if ft:
        media[0].caption = ft

    await X.edit_message_text(did, p.id, f'正在上传相册（{len(media)} 项）...')
    upload_error = None
    try:
        await sender.send_media_group(tcid, media, reply_to_message_id=rtmid)
    except TypeError as e:
        if 'keyword-only argument' in str(e):
            # pyrofork 2.3.69 breaks parsing the SendMultiMedia response AFTER
            # the RPC already succeeded — the album is already delivered.
            # Treat the parse bug as success.
            print(f'send_media_group response parse bug (treating as success): {e}')
        else:
            upload_error = str(e)
    except Exception as e:
        upload_error = str(e)

    if upload_error:
        err = upload_error
        # Telegram rejects some groups (e.g. MEDIA_EMPTY when a no-audio-track
        # video is treated as an animation and mixed into an album). Sending the
        # items individually still delivers the good ones — partial success
        # beats total failure.
        print(f'send_media_group failed ({err}), falling back to per-item sends')
        sent = 0
        for im in media:
            try:
                await _send_album_item(sender, tcid, im, rtmid)
                sent += 1
            except FloodWait as e:
                await asyncio.sleep(_flood_secs(e))
                try:
                    await _send_album_item(sender, tcid, im, rtmid)
                    sent += 1
                except Exception as e2:
                    print(f'Per-item send failed after flood wait: {e2}')
            except Exception as e2:
                print(f'Per-item send failed: {e2}')
            await asyncio.sleep(2)
        for f in files:
            if os.path.exists(f):
                os.remove(f)
        if sent:
            await X.delete_messages(did, p.id)
            return f'⚠️ 整组发送被拒，已逐条发送 {sent}/{len(media)} 项（{err[:40]}）'
        if 'PEER_ID_INVALID' in err or 'CHAT_WRITE_FORBIDDEN' in err or 'ADMIN' in err.upper():
            hint = '请将 /setbot 的机器人加入目标频道并授予发帖权限。'
        else:
            hint = ''
        await X.edit_message_text(did, p.id, f'相册上传失败：{err[:60]} {hint}')
        return f'❌ 相册上传失败：{err[:60]}'

    for f in files:
        if os.path.exists(f):
            os.remove(f)
    await X.delete_messages(did, p.id)
    return f'✅ 相册已发送（{len(media)} 项）'


async def process_msg(c, u, m, d, lt, uid, i):
    try:
        tcid, rtmid, deliver_via_bot = await resolve_delivery(d)
        did = int(d)

        if m.media:
            orig_text = m.caption.markdown if m.caption else ''
            proc_text = await process_text_with_rules(d, orig_text)
            user_cap = await get_user_data_key(d, 'caption', '')
            ft = f'{proc_text}\n\n{user_cap}' if proc_text and user_cap else user_cap if user_cap else proc_text
            
            if lt == 'public' and not emp.get(i, False):
                # Direct file reference send requires the file reference holder's client.
                sent, error = await send_direct(c, m, tcid, ft, rtmid)
                if sent:
                    return 'Sent directly.'
                if error and 'PEER_ID_INVALID' in error:
                    return (
                        '发送失败：目标聊天不可用。请在 /settings 设置正确的 '
                        '-100... 聊天 ID，并将 /setbot 机器人加入该频道且设为管理员。'
                    )
                return f'发送失败：{error[:80] if error else "未知错误"}'
            
            # Sender selection: a custom bot CANNOT message a user who never
            # started it (PEER_ID_INVALID on resolve_peer). When delivering to
            # a bot-managed target (configured chat or LOG_GROUP), use the
            # custom bot — it must be a member there. When falling back to the
            # user's own chat, use the user client (messaging self always works).
            # Progress reports go through the main bot (X) to the user's bot
            # chat, so channels are never spammed and the client always edits
            # its own messages.
            sender = c if deliver_via_bot else (u or c)
            st = time.time()
            p = await X.send_message(did, '正在下载...')

            c_name = f"{time.time()}"
            if m.video:
                file_name = m.video.file_name
                if not file_name:
                    file_name = f"{time.time()}.mp4"
                    c_name = sanitize(file_name)
            elif m.audio:
                file_name = m.audio.file_name
                if not file_name:
                    file_name = f"{time.time()}.mp3"
                    c_name = sanitize(file_name)
            elif m.document:
                file_name = m.document.file_name
                if not file_name:
                    file_name = f"{time.time()}"
                else:
                    c_name = sanitize(file_name)
            elif m.photo:
                file_name = f"{time.time()}.jpg"
                c_name = sanitize(file_name)
    
            # pyrofork download_media resolves relative names against PARENT_DIR
            # (Path(sys.argv[0]).parent = /app, read-only image layer), ignoring the
            # client workdir. Pass an absolute path under the writable volume.
            download_path = os.path.join(_WORKDIR, 'downloads', c_name)
            f = await u.download_media(m, file_name=download_path, progress=prog, progress_args=(X, did, p.id, st))
            
            if not f:
                await X.edit_message_text(did, p.id, '失败。')
                return 'Failed.'
            
            await X.edit_message_text(did, p.id, '正在重命名...')
            if (
                (m.video and m.video.file_name) or
                (m.audio and m.audio.file_name) or
                (m.document and m.document.file_name)
            ):
                f = await rename_file(f, d, p)
            
            fsize = os.path.getsize(f) / (1024 * 1024 * 1024)
            th = thumbnail(d)
            
            if fsize > 2 and Y:
                st = time.time()
                await X.edit_message_text(did, p.id, '文件大于 2GB，正在使用备用方法...')
                await upd_dlg(Y)
                mtd = await get_video_metadata(f)
                dur, h, w = mtd['duration'], mtd['width'], mtd['height']
                th = await screenshot(f, dur, d)
                
                send_funcs = {'video': Y.send_video, 'video_note': Y.send_video_note, 
                            'voice': Y.send_voice, 'audio': Y.send_audio, 
                            'photo': Y.send_photo, 'document': Y.send_document}
                
                for mtype, func in send_funcs.items():
                    if f.endswith('.mp4'): mtype = 'video'
                    if getattr(m, mtype, None):
                        sent = await func(LOG_GROUP, f, thumb=th if mtype == 'video' else None, 
                                        duration=dur if mtype == 'video' else None,
                                        height=h if mtype == 'video' else None,
                                        width=w if mtype == 'video' else None,
                                        caption=ft if m.caption and mtype not in ['video_note', 'voice'] else None, 
                                        reply_to_message_id=rtmid, progress=prog, progress_args=(X, did, p.id, st))
                        break
                else:
                    sent = await Y.send_document(LOG_GROUP, f, thumb=th, caption=ft if m.caption else None,
                                                reply_to_message_id=rtmid, progress=prog, progress_args=(X, did, p.id, st))
                
                await sender.copy_message(tcid, LOG_GROUP, sent.id)
                os.remove(f)
                await X.delete_messages(did, p.id)
                
                return 'Done (Large file).'
            
            await X.edit_message_text(did, p.id, '正在上传...')
            st = time.time()

            try:
                video_extensions = ['.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.3gp', '.ogv']
                audio_extensions = ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a', '.opus', '.aiff', '.ac3']
                file_ext = os.path.splitext(f)[1].lower()
                if m.video or (m.document and file_ext in video_extensions):
                    mtd = await get_video_metadata(f)
                    dur, h, w = mtd['duration'], mtd['width'], mtd['height']
                    th = await screenshot(f, dur, d)
                    await sender.send_video(tcid, video=f, caption=ft if m.caption else None, 
                                    thumb=th, width=w, height=h, duration=dur, 
                                    progress=prog, progress_args=(X, did, p.id, st), 
                                    reply_to_message_id=rtmid)
                elif m.video_note:
                    await sender.send_video_note(tcid, video_note=f, progress=prog, 
                                        progress_args=(X, did, p.id, st), reply_to_message_id=rtmid)
                elif m.voice:
                    await sender.send_voice(tcid, f, progress=prog, progress_args=(X, did, p.id, st), 
                                    reply_to_message_id=rtmid)
                elif m.sticker:
                    await sender.send_sticker(tcid, m.sticker.file_id, reply_to_message_id=rtmid)
                elif m.audio or (m.document and file_ext in audio_extensions):
                    await sender.send_audio(tcid, audio=f, caption=ft if m.caption else None, 
                                    thumb=th, progress=prog, progress_args=(X, did, p.id, st), 
                                    reply_to_message_id=rtmid)
                elif m.photo:
                    await sender.send_photo(tcid, photo=f, caption=ft if m.caption else None, 
                                    progress=prog, progress_args=(X, did, p.id, st), 
                                    reply_to_message_id=rtmid)
                elif m.document:
                    await sender.send_document(tcid, document=f, caption=ft if m.caption else None, 
                                        progress=prog, progress_args=(X, did, p.id, st), 
                                        reply_to_message_id=rtmid)
                else:
                    await sender.send_document(tcid, document=f, caption=ft if m.caption else None, 
                                        progress=prog, progress_args=(X, did, p.id, st), 
                                        reply_to_message_id=rtmid)
            except Exception as e:
                err = str(e)
                if 'PEER_ID_INVALID' in err or 'CHAT_WRITE_FORBIDDEN' in err or 'ADMIN' in err.upper():
                    hint = '请将 /setbot 的机器人加入目标频道并授予发帖权限。'
                else:
                    hint = ''
                try:
                    await X.edit_message_text(did, p.id, f'上传失败：{err[:60]} {hint}')
                except Exception:
                    pass
                if os.path.exists(f): os.remove(f)
                return f'上传失败：{err[:60]} {hint}'.strip()
            
            os.remove(f)
            await X.delete_messages(did, p.id)
            
            return 'Done.'
            
        elif m.text:
            sender = c if deliver_via_bot else (u or c)
            await sender.send_message(tcid, text=m.text.markdown, reply_to_message_id=rtmid)
            return 'Sent.'
    except Exception as e:
        return f'Error: {str(e)[:50]}'
        
def parse_link_lines(text):
    """Parse /batch input.

    One non-empty line  -> ('range', (cid, sid, lt))   # start link + count flow
    Multiple lines      -> ('multi', [(cid, sid, lt), ...])
    Any unparsable line -> ('invalid', (line_no, line_text))
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    parsed = []
    for idx, ln in enumerate(lines, 1):
        ci, di, lti = E(ln)
        if not ci or not di:
            return 'invalid', (idx, ln)
        parsed.append((ci, di, lti))
    if not parsed:
        return 'invalid', (1, text.strip()[:50])
    if len(parsed) == 1:
        return 'range', parsed[0]
    return 'multi', parsed


def _ok(res):
    # Success strings are either process_msg's English markers or the
    # emoji-prefixed album results (✅ full, ⚠️ partial per-item fallback).
    return (res.startswith(('✅', '⚠️'))
            or 'Done' in res or 'Copied' in res or 'Sent' in res)


async def process_one_link(ubot, uc, i, s, lt, d, uid):
    """Fetch and deliver one t.me link (expanding albums), with one FloodWait retry."""
    try:
        return await _process_one_link(ubot, uc, i, s, lt, d, uid)
    except FloodWait as e:
        secs = _flood_secs(e)
        print(f'FloodWait {secs}s on {i}/{s}, waiting and retrying once')
        await asyncio.sleep(secs)
        return await _process_one_link(ubot, uc, i, s, lt, d, uid)


async def _process_one_link(ubot, uc, i, s, lt, d, uid):
    """Fetch and deliver one t.me link (expanding albums). Returns a status string."""
    if not uc and lt != 'public':
        return '用户会话无效或未登录，请先使用 /login。'
    msg = await get_msg(ubot, uc, i, s, lt)
    if not msg:
        return '未找到消息'
    msgs = [msg]
    if getattr(msg, 'media_group_id', None):
        fetch_client = uc if (uc and (lt == 'private' or emp.get(i, False))) else ubot
        try:
            group = await fetch_client.get_media_group(i, s)
            if group:
                msgs = group
        except Exception as e:
            print(f'Media group fetch failed, falling back to single: {e}')
    if len(msgs) > 1:
        return await process_album(ubot, uc, msgs, d, lt, uid, i)
    return await process_msg(ubot, uc, msgs[0], d, lt, uid, i)


@X.on_message(filters.command(['batch', 'single']))
async def process_cmd(c, m):
    uid = m.from_user.id
    cmd = m.command[0]
    
    if FREEMIUM_LIMIT == 0 and not await is_premium_user(uid):
        await m.reply_text("此机器人不提供免费服务，请向管理员订阅")
        return
    
    if await sub(c, m) == 1: return
    pro = await m.reply_text('正在进行检查，请稍候...')

    if is_user_active(uid):
        await pro.edit('您有一个正在进行的任务。使用 /stop 取消。')
        return

    bot_token = await get_user_data_key(uid, "bot_token", None)
    if isinstance(bot_token, str):
        bot_token = bot_token.strip()

    ubot = await get_ubot(uid)
    if not ubot:
        if bot_token:
            await pro.edit('已保存机器人令牌，但机器人启动失败。请检查令牌后重新使用 /setbot。')
        else:
            await pro.edit('请先使用 /setbot 添加您的机器人')
        return
    
    Z[uid] = {'step': 'start' if cmd == 'batch' else 'start_single'}
    if cmd == 'batch':
        await pro.edit('发送起始链接（连续下载指定数量），或多条链接（每行一条，逐个下载）。')
    else:
        await pro.edit('发送要处理的链接。')

@X.on_message(filters.command(['cancel', 'stop']))
async def cancel_cmd(c, m):
    uid = m.from_user.id
    if is_user_active(uid):
        if await request_batch_cancel(uid):
            await m.reply_text('已请求取消。当前批量提取将在本次下载完成后停止。')
        else:
            await m.reply_text('请求取消失败，请重试。')
    else:
        await m.reply_text('未找到正在进行的批量提取。')

@X.on_message(filters.text & filters.private & ~login_in_progress & ~filters.command([
    'start', 'batch', 'cancel', 'login', 'logout', 'stop', 'set', 
    'pay', 'redeem', 'gencode', 'single', 'generate', 'keyinfo', 'encrypt', 'decrypt', 'keys', 'setbot', 'rembot']))
async def text_handler(c, m):
    uid = m.from_user.id
    if uid not in Z: return
    s = Z[uid].get('step')
    x = await get_ubot(uid)
    if not x:
        Z.pop(uid, None)
        bot_token = await get_user_data_key(uid, "bot_token", None)
        if isinstance(bot_token, str):
            bot_token = bot_token.strip()
        if bot_token:
            await m.reply_text('已保存机器人令牌，但机器人启动失败。请检查令牌后重新使用 /setbot。')
        else:
            await m.reply_text("请先使用 /setbot 添加您的机器人")
        return

    if s == 'start':
        mode, payload = parse_link_lines(m.text)
        if mode == 'invalid':
            idx, line = payload
            await m.reply_text(f'第 {idx} 行链接格式无效：{line[:50]}')
            Z.pop(uid, None)
            return
        if mode == 'range':
            i, d, lt = payload
            Z[uid].update({'step': 'count', 'cid': i, 'sid': d, 'lt': lt})
            await m.reply_text('要处理多少条消息？')
            return

        links = payload
        n = len(links)
        maxlimit = PREMIUM_LIMIT if await is_premium_user(uid) else FREEMIUM_LIMIT
        if n > maxlimit:
            await m.reply_text(f'一次最多 {maxlimit} 条链接，你发送了 {n} 条。')
            Z.pop(uid, None)
            return

        pt = await m.reply_text(f'开始批量提取 {n} 条链接...')
        uc = await get_uclient(uid)
        ubot = UB.get(uid)
        if not ubot:
            await pt.edit('请先使用 /setbot 添加机器人')
            Z.pop(uid, None)
            return
        if is_user_active(uid):
            await pt.edit('存在正在进行的任务。请先使用 /stop。')
            Z.pop(uid, None)
            return

        await add_active_batch(uid, {
            "total": n,
            "current": 0,
            "success": 0,
            "cancel_requested": False,
            "progress_message_id": pt.id
            })

        success = 0
        try:
            for j, (ci, di, lti) in enumerate(links):
                if should_cancel(uid):
                    await pt.edit(f'已在 {j}/{n} 处取消。成功：{success}')
                    break
                await update_batch_progress(uid, j, success)
                try:
                    res = await process_one_link(ubot, uc, ci, di, lti, str(m.chat.id), uid)
                    if _ok(res):
                        success += 1
                except Exception as e:
                    try: await pt.edit(f'{j+1}/{n}：错误 - {str(e)[:30]}')
                    except: pass
                await asyncio.sleep(10)
            if j + 1 == n:
                await m.reply_text(f'批量提取完成 ✅ 成功：{success}/{n}')
        finally:
            await remove_active_batch(uid)
            Z.pop(uid, None)

    elif s == 'start_single':
        L = m.text
        i, d, lt = E(L)
        if not i or not d:
            await m.reply_text('链接格式无效。')
            Z.pop(uid, None)
            return

        Z[uid].update({'step': 'process_single', 'cid': i, 'sid': d, 'lt': lt})
        i, s, lt = Z[uid]['cid'], Z[uid]['sid'], Z[uid]['lt']
        pt = await m.reply_text('处理中...')
        
        ubot = UB.get(uid)
        if not ubot:
            await pt.edit('请先使用 /setbot 添加机器人')
            Z.pop(uid, None)
            return
        
        uc = await get_uclient(uid)
        if is_user_active(uid):
            await pt.edit('存在正在进行的任务。请先使用 /stop。')
            Z.pop(uid, None)
            return

        try:
            res = await process_one_link(ubot, uc, i, s, lt, str(m.chat.id), uid)
            await pt.edit(res)
        except Exception as e:
            await pt.edit(f'错误：{str(e)[:50]}')
        finally:
            Z.pop(uid, None)

    elif s == 'count':
        if not m.text.isdigit():
            await m.reply_text('请输入有效数字。')
            return
        
        count = int(m.text)
        maxlimit = PREMIUM_LIMIT if await is_premium_user(uid) else FREEMIUM_LIMIT

        if count > maxlimit:
            await m.reply_text(f'最大限制为 {maxlimit}。')
            return

        Z[uid].update({'step': 'process', 'did': str(m.chat.id), 'num': count})
        i, s, n, lt = Z[uid]['cid'], Z[uid]['sid'], Z[uid]['num'], Z[uid]['lt']
        success = 0

        pt = await m.reply_text('正在进行批量提取...')
        uc = await get_uclient(uid)
        ubot = UB.get(uid)
        
        if not uc or not ubot:
            await pt.edit('客户端配置缺失')
            Z.pop(uid, None)
            return
            
        if is_user_active(uid):
            await pt.edit('存在正在进行的任务')
            Z.pop(uid, None)
            return
        
        await add_active_batch(uid, {
            "total": n,
            "current": 0,
            "success": 0,
            "cancel_requested": False,
            "progress_message_id": pt.id
            })
        
        try:
            for j in range(n):
                
                if should_cancel(uid):
                    await pt.edit(f'已在 {j}/{n} 处取消。成功：{success}')
                    break
                
                await update_batch_progress(uid, j, success)
                
                mid = int(s) + j
                
                try:
                    msg = await get_msg(ubot, uc, i, mid, lt)
                    if msg:
                        res = await process_msg(ubot, uc, msg, str(m.chat.id), lt, uid, i)
                        if 'Done' in res or 'Copied' in res or 'Sent' in res:
                            success += 1
                    else:
                        pass
                except Exception as e:
                    try: await pt.edit(f'{j+1}/{n}：错误 - {str(e)[:30]}')
                    except: pass
                
                await asyncio.sleep(10)
            
            if j+1 == n:
                await m.reply_text(f'批量提取完成 ✅ 成功：{success}/{n}')
        
        finally:
            await remove_active_batch(uid)
            Z.pop(uid, None)



