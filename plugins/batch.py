# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

import os, re, time, asyncio, json, asyncio 
from pyrogram import Client, filters
from pyrogram.types import Message, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from pyrogram.errors import UserNotParticipant, FloodWait
from config import API_ID, API_HASH, LOG_GROUP, STRING, FORCE_SUB, FREEMIUM_LIMIT, PREMIUM_LIMIT, BATCH_INTERVAL, MERGE_INTERVAL, CHANNEL_INTERVAL, UPLOAD_INTERVAL, MAX_FLOOD_RETRIES
from utils.func import get_user_data, screenshot, thumbnail, get_video_metadata, ensure_audio_track
from utils.func import get_user_data_key, process_text_with_rules, is_premium_user, E
from shared_client import app as X, _WORKDIR
from plugins.settings import rename_file
from plugins.start import subscribe as sub
from utils.custom_filters import login_in_progress
from utils.encrypt import dcs
from typing import Dict, Any, Optional


Y = None if not STRING else __import__('shared_client').userbot
Z, P, UB, UC, emp, _LINKED_CHAT = {}, {}, {}, {}, {}, {}

# ─── Task queue system ─────────────────────────────────────────────────────────
# Per-user serial task queue: multiple tasks queue up, each runs in a
# background worker. Users can /tasks to check status, /stop to cancel.
# FloodWait inside a task no longer blocks the user's ability to interact.

TASKS = {}            # task_id -> task dict
_TASK_SEQ = 0         # monotonic counter for unique task IDs
USER_QUEUES = {}      # uid -> asyncio.Queue
USER_WORKERS = {}     # uid -> asyncio.Task (persistent worker coroutine)
_MAX_QUEUE = 3        # max queued tasks per user

# fixed directory file_name problems
def sanitize(filename):
    return re.sub(r'[<>:"/\\|?*\']', '_', filename).strip(" .")[:255]

def create_task(uid, task_type, total, **params):
    """Create a task descriptor and register it in TASKS."""
    global _TASK_SEQ
    _TASK_SEQ += 1
    tid = f'task_{uid}_{int(time.time())}_{_TASK_SEQ}'
    task = {
        'id': tid,
        'uid': uid,
        'type': task_type,         # 'batch_links' | 'single' | 'merge' | 'batch_count'
        'status': 'queued',        # 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
        'total': total,
        'current': 0,
        'success': 0,
        'cancel_requested': False,
        'created_at': time.time(),
        'finished_at': None,
        'result': '',
        'progress_msg': '',
    }
    task.update(params)
    TASKS[tid] = task
    return task

async def enqueue_task(uid, task):
    """Enqueue a task for a user, starting the worker if needed."""
    if uid not in USER_QUEUES:
        USER_QUEUES[uid] = asyncio.Queue()
        USER_WORKERS[uid] = asyncio.create_task(_task_worker(uid))
    await USER_QUEUES[uid].put(task)

async def _task_worker(uid):
    """Per-user persistent worker: processes tasks serially."""
    queue = USER_QUEUES[uid]
    while True:
        task = await queue.get()
        task['status'] = 'running'
        try:
            await _dispatch_task(uid, task)
            if task['status'] == 'running':
                task['status'] = 'done'
        except asyncio.CancelledError:
            task['status'] = 'cancelled'
            raise
        except Exception as e:
            task['status'] = 'failed'
            task['result'] = f'❌ 任务失败：{str(e)[:100]}'
            print(f'Task {task["id"]} failed: {e}')
        finally:
            task['finished_at'] = time.time()
            queue.task_done()

async def _dispatch_task(uid, task):
    """Route a task to its execution function based on type."""
    if task['type'] == 'batch_links':
        await _run_batch_links(uid, task)
    elif task['type'] == 'single':
        await _run_single(uid, task)
    elif task['type'] == 'merge':
        await _run_merge(uid, task)
    elif task['type'] == 'batch_count':
        await _run_batch_count(uid, task)

def task_should_cancel(task_id):
    t = TASKS.get(task_id)
    return t is not None and t.get('cancel_requested', False)

def task_update(task_id, current=None, success=None, progress_msg=None):
    t = TASKS.get(task_id)
    if t is None:
        return
    if current is not None:
        t['current'] = current
    if success is not None:
        t['success'] = success
    if progress_msg is not None:
        t['progress_msg'] = progress_msg

def request_cancel_tasks(uid):
    """Cancel all queued/running tasks for a user. Returns count."""
    count = 0
    for t in TASKS.values():
        if t['uid'] == uid and t['status'] in ('queued', 'running'):
            t['cancel_requested'] = True
            count += 1
    return count

def get_user_tasks(uid):
    """Return all tasks for a user, newest first."""
    return sorted(
        [t for t in TASKS.values() if t['uid'] == uid],
        key=lambda t: t['created_at'], reverse=True
    )

def get_queue_size(uid):
    q = USER_QUEUES.get(uid)
    return q.qsize() if q else 0

def has_running_task(uid):
    """True if the user has a task currently executing (not just queued)."""
    return any(t['uid'] == uid and t['status'] == 'running' for t in TASKS.values())



# ─── Task execution functions ───────────────────────────────────────────────────
# Each takes (uid, task) and runs to completion. Cancellation is checked via
# task_should_cancel(task['id']). Progress is reported via task_update().

async def _run_batch_links(uid, task):
    """Execute multi-link batch extraction."""
    links = task['links']
    oc = task.get('caption')
    chat_id = task['chat_id']
    n = len(links)
    ubot = UB.get(uid)
    uc = await get_uclient(uid)
    success = 0
    cancelled = False
    task_update(task['id'], progress_msg=f'批量提取 {n} 条链接...')
    for j, (ci, di, lti, comment_id) in enumerate(links):
        if task_should_cancel(task['id']):
            cancelled = True
            task_update(task['id'], current=j, success=success, progress_msg=f'已取消（{j}/{n}）')
            break
        task_update(task['id'], current=j, success=success, progress_msg=f'正在提取 {j+1}/{n}...')
        try:
            res = await process_one_link(ubot, uc, ci, di, lti, chat_id, uid, oc, comment_id)
            if _ok(res):
                success += 1
        except Exception as e:
            print(f'Batch link {j+1}/{n} error: {e}')
        await asyncio.sleep(BATCH_INTERVAL)
    if not cancelled:
        task_update(task['id'], current=n, success=success)
        task['result'] = f'✅ 批量提取完成：成功 {success}/{n}'
    else:
        task['result'] = f'已取消。成功：{success}/{n}'

async def _run_single(uid, task):
    """Execute single-link extraction."""
    ci, di, lt, comment_id = task['link_info']
    oc = task.get('caption')
    chat_id = task['chat_id']
    ubot = UB.get(uid)
    uc = await get_uclient(uid)
    task_update(task['id'], progress_msg='处理中...')
    try:
        res = await process_one_link(ubot, uc, ci, di, lt, chat_id, uid, oc, comment_id)
        task['result'] = res
    except FloodWait as e:
        secs = _flood_secs(e)
        task_update(task['id'], progress_msg=f'Telegram 限流，等待 {int(secs)}s...')
        await asyncio.sleep(secs)
        res = await process_one_link(ubot, uc, ci, di, lt, chat_id, uid, oc, comment_id)
        task['result'] = res
    except Exception as e:
        task['result'] = f'❌ 错误：{str(e)[:100]}'

async def _run_merge(uid, task):
    """Execute merge extraction and delivery."""
    links = task['links']
    oc = task.get('caption')
    chat_id = task['chat_id']
    ubot = UB.get(uid)
    uc = await get_uclient(uid)
    n = len(links)

    # Phase 1: batch-fetch comment links grouped by channel
    comment_groups = {}
    for ci, di, lti, comment_id in links:
        if comment_id:
            comment_groups.setdefault(ci, []).append(comment_id)
    batch_msgs = {}
    if comment_groups:
        batch_fetch_client = uc or ubot
        for channel, cids in comment_groups.items():
            try:
                linked = await resolve_linked_chat(batch_fetch_client, channel)
            except FloodWait as e:
                await asyncio.sleep(_flood_secs(e))
                linked = await resolve_linked_chat(batch_fetch_client, channel)
            if not linked:
                continue
            try:
                results = await batch_fetch_client.get_messages(linked.id, cids)
                if not isinstance(results, list):
                    results = [results]
                for msg in results:
                    if msg and not getattr(msg, 'empty', False):
                        batch_msgs[msg.id] = msg
                emp[(uid, linked.id)] = True
            except FloodWait as e:
                await asyncio.sleep(_flood_secs(e))
                try:
                    results = await batch_fetch_client.get_messages(linked.id, cids)
                    if not isinstance(results, list):
                        results = [results]
                    for msg in results:
                        if msg and not getattr(msg, 'empty', False):
                            batch_msgs[msg.id] = msg
                    emp[(uid, linked.id)] = True
                except Exception as e2:
                    print(f'Retry batch fetch failed for {channel}: {e2}')
            except Exception as e:
                print(f'Batch comment fetch failed for {channel}: {e}')
            await asyncio.sleep(CHANNEL_INTERVAL)

    # Phase 2: assemble messages in original link order
    all_msgs = []
    for j, (ci, di, lti, comment_id) in enumerate(links):
        if task_should_cancel(task['id']):
            task['result'] = f'已取消（{j}/{n}）'
            return
        task_update(task['id'], current=j, progress_msg=f'正在获取 {j+1}/{n}...')
        if comment_id:
            msg = batch_msgs.get(comment_id)
        else:
            if not uc and lti != 'public':
                continue
            try:
                msg = await get_msg(ubot, uc, ci, di, lti, uid)
            except FloodWait as e:
                await asyncio.sleep(_flood_secs(e))
                try:
                    msg = await get_msg(ubot, uc, ci, di, lti, uid)
                except Exception:
                    msg = None
        if not msg:
            continue
        if getattr(msg, 'media_group_id', None):
            src_chat = msg.chat.id if getattr(msg, 'chat', None) else ci
            src_lt = 'private' if comment_id else lti
            fetch_client = uc if (uc and (src_lt == 'private' or emp.get((uid, src_chat), False))) else ubot
            try:
                group = await fetch_client.get_media_group(src_chat, msg.id)
            except FloodWait as e:
                await asyncio.sleep(_flood_secs(e))
                try:
                    group = await fetch_client.get_media_group(src_chat, msg.id)
                except Exception:
                    group = None
            except Exception:
                group = None
            all_msgs.extend(group or [msg])
        else:
            all_msgs.append(msg)
        await asyncio.sleep(MERGE_INTERVAL)

    if not all_msgs:
        task['result'] = '❌ 合并失败：未获取到任何消息。'
        return
    task_update(task['id'], progress_msg=f'正在合并 {len(all_msgs)} 条消息...')
    try:
        res = await process_merged(ubot, uc, all_msgs, chat_id, uid, oc)
    except FloodWait as e:
        await asyncio.sleep(_flood_secs(e))
        res = await process_merged(ubot, uc, all_msgs, chat_id, uid, oc)
    except Exception as e:
        res = f'❌ 合并失败：{str(e)[:100]}'
    task['result'] = res

async def _run_batch_count(uid, task):
    """Execute sequential batch extraction (start link + count)."""
    ci = task['cid']
    sid = task['sid']
    lt = task['lt']
    n = task['num']
    oc = task.get('caption')
    chat_id = task['chat_id']
    ubot = UB.get(uid)
    uc = await get_uclient(uid)
    success = 0
    cancelled = False
    task_update(task['id'], progress_msg=f'批量提取 {n} 条...')
    for j in range(n):
        if task_should_cancel(task['id']):
            cancelled = True
            task_update(task['id'], current=j, success=success, progress_msg=f'已取消（{j}/{n}）')
            break
        task_update(task['id'], current=j, success=success, progress_msg=f'正在提取 {j+1}/{n}...')
        mid = int(sid) + j
        try:
            msg = await get_msg(ubot, uc, ci, mid, lt, uid)
            if msg:
                res = await process_msg(ubot, uc, msg, chat_id, lt, uid, ci, oc)
                if _ok(res):
                    success += 1
        except Exception as e:
            print(f'Count batch {j+1}/{n} error: {e}')
        await asyncio.sleep(BATCH_INTERVAL)
    if not cancelled:
        task_update(task['id'], current=n, success=success)
        task['result'] = f'✅ 批量提取完成：成功 {success}/{n}'
    else:
        task['result'] = f'已取消。成功：{success}/{n}'
async def upd_dlg(c):
    try:
        async for _ in c.get_dialogs(limit=100): pass
        return True
    except Exception as e:
        print(f'Failed to update dialogs: {e}')
        return False

# fixed the old group of 2021-2022 extraction 🌝 (buy krne ka fayda nhi ab old group) ✅ 
async def resolve_linked_chat(client, channel):
    """Resolve a channel's linked discussion group Chat, cached per channel.

    ``get_chat`` triggers a cross-DC auth-key round-trip. Calling it per-link
    (common when merging multiple comment links from one channel) causes
    FLOOD_WAIT on the auth subsystem. The cache ensures one resolution per
    channel, process-wide.
    """
    if channel in _LINKED_CHAT:
        return _LINKED_CHAT[channel]
    try:
        chat = await client.get_chat(channel)
        linked = getattr(chat, 'linked_chat', None)
    except FloodWait:
        raise
    except Exception as e:
        print(f'Failed to resolve linked chat for {channel}: {e}')
        linked = None
    _LINKED_CHAT[channel] = linked
    return linked


async def get_msg(c, u, i, d, lt, uid, comment_id=None):
    # emp is keyed per (uid, channel): concurrent users fetching the same
    # channel must not overwrite each other's source-client marker.
    try:
        if comment_id:
            # Comment link (?comment=N): the message lives in the channel's
            # linked discussion group, NOT the channel itself. Resolve it,
            # then fetch the comment message from there.
            fetch_client = u or c
            if not fetch_client:
                return None
            linked = await resolve_linked_chat(fetch_client, i)
            if not linked:
                print(f'Channel {i} has no linked discussion group')
                return None
            try:
                xm = await fetch_client.get_messages(linked.id, comment_id)
            except FloodWait:
                raise
            except Exception as e:
                print(f'Failed to fetch comment {comment_id} from discussion group: {e}')
                return None
            if xm and not getattr(xm, 'empty', False):
                emp[(uid, linked.id)] = True
                return xm
            return None

        if lt == 'public':
            clients = []
            if u:
                clients.append(('user', u, False))
            if c and c is not u:
                clients.append(('bot', c, True))

            for label, client, fetched_by_bot in clients:
                try:
                    xm = await client.get_messages(i, d)
                except FloodWait:
                    raise  # let process_one_link's retry wrapper see it
                except Exception as e:
                    print(f'Error fetching public message with {label} client: {e}')
                    continue

                if xm and not getattr(xm, 'empty', False):
                    # emp is looked up downstream by numeric chat id
                    # (msg.chat.id) — record both keys: for public links
                    # ``i`` is the username, which would never match.
                    emp[(uid, i)] = not fetched_by_bot
                    if getattr(xm, 'chat', None):
                        emp[(uid, xm.chat.id)] = not fetched_by_bot
                    print(f'Fetched public message with {label} client')
                    return xm

            if u:
                try:
                    await u.join_chat(i)
                    chat = await u.get_chat(f'@{i}')
                    xm = await u.get_messages(chat.id, d)
                    if xm and not getattr(xm, 'empty', False):
                        emp[(uid, i)] = True
                        emp[(uid, chat.id)] = True
                        return xm
                except FloodWait:
                    raise
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
            except FloodWait:
                raise
            except Exception:
                pass

            try:
                result = await u.get_messages(chat_id_dash, d)
                if result and not getattr(result, "empty", False):
                    return result
            except FloodWait:
                raise
            except Exception:
                pass

            try:
                async for _ in u.get_dialogs(limit=200):
                    pass
                result = await u.get_messages(i, d)
                if result and not getattr(result, "empty", False):
                    return result
            except FloodWait:
                raise
            except Exception:
                pass

            return None
        except FloodWait:
            raise
        except Exception as e:
            print(f'Private channel error: {e}')
            return None
    except FloodWait:
        raise
    except Exception as e:
        print(f'Error fetching message: {e}')
        return None


_UB_UC_LOCKS = {}

def _client_lock(uid):
    # Per-user lock serializing UB/UC creation: concurrent updates must not
    # start two clients sharing one session file.
    lock = _UB_UC_LOCKS.get(uid)
    if lock is None:
        lock = _UB_UC_LOCKS[uid] = asyncio.Lock()
    return lock


async def get_ubot(uid):
    bt = await get_user_data_key(uid, "bot_token", None)
    if isinstance(bt, str):
        bt = bt.strip()
    if not bt:
        return None
    if uid in UB:
        return UB.get(uid)

    async with _client_lock(uid):
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
        async with _client_lock(uid):
            if uid in UC:
                return UC.get(uid)
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


async def with_flood_retry(coro_fn, context='', max_retries=None):
    """Call ``coro_fn()`` (a zero-arg async factory), retrying on FloodWait.

    Waits the server-requested seconds, then retries. After MAX_FLOOD_RETRIES
    attempts the FloodWait re-raises so the caller can handle or surface it.
    """
    retries = max_retries if max_retries is not None else MAX_FLOOD_RETRIES
    last_exc = None
    for attempt in range(retries):
        try:
            return await coro_fn()
        except FloodWait as e:
            last_exc = e
            secs = _flood_secs(e)
            if attempt < retries - 1:
                print(f'FloodWait {secs}s on {context} (attempt {attempt + 1}/{retries}), waiting')
                await asyncio.sleep(secs)
            else:
                print(f'FloodWait {secs}s on {context}: retries exhausted')
    raise last_exc

async def _safe_cleanup(coro):
    """Post-delivery cleanup must never propagate (esp. FloodWait): a raised
    error here would make the caller retry an already-delivered send and
    duplicate the content."""
    try:
        await coro
    except Exception as e:
        print(f'cleanup failed (delivery already done): {e}')


async def _download_media_item(u, one, uid, idx, tag, X, did, p_id, st):
    """Download one message's media and wrap it as an InputMedia for grouping.

    Returns (input_media, [local_files_to_cleanup]); (None, []) when the
    message has no usable media or its download fails. ``tag`` ('album'/
    'merge') namespaces temp filenames so concurrent flows never collide.
    Shared by process_album and process_merged.
    """
    if not (one.photo or one.video or one.document or one.audio):
        return None, []
    # SendMultiMedia validates uploads by file extension (PHOTO_EXT_INVALID
    # otherwise), so the temp name must carry one.
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
        file_name=os.path.join(_WORKDIR, 'downloads', f'{tag}_{uid}_{int(time.time())}_{idx}{ext}'),
        progress=prog, progress_args=(X, did, p_id, st),
    )
    if not f:
        print(f'{tag} item {idx + 1} download failed, skipping')
        return None, []
    files = [f]
    if one.video:
        # Videos without an audio track are treated as animations by Telegram;
        # mixed into SendMultiMedia they fail the whole group (MEDIA_EMPTY).
        f = await ensure_audio_track(f)
        files = [f]
        # Keep the source channel's thumbnail; without one Telegram shows the
        # first frame, which is often black.
        thumb_path = None
        if one.video.thumbs:
            try:
                thumb_path = await u.download_media(
                    one.video.thumbs[-1].file_id,
                    file_name=os.path.join(
                        _WORKDIR, 'downloads',
                        f'{tag}_thumb_{uid}_{int(time.time())}_{idx}.jpg',
                    ),
                )
            except Exception as e:
                print(f'Thumb download failed for {tag} item {idx + 1}: {e}')
        if thumb_path:
            files.append(thumb_path)
        return InputMediaVideo(
            f, duration=one.video.duration,
            width=one.video.width, height=one.video.height,
            thumb=thumb_path,
        ), files
    if one.photo:
        return InputMediaPhoto(f), files
    if one.audio:
        return InputMediaAudio(f, duration=one.audio.duration), files
    return InputMediaDocument(f), files

async def process_album(c, u, msgs, d, lt, uid, i, oc=None):
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

    # ``oc`` (override caption) replaces the original text entirely; the
    # /settings default caption (user_cap) is still appended below.
    if oc is not None:
        proc_text = oc
    else:
        orig_caption = next((one.caption.markdown for one in msgs if one.caption), '')
        proc_text = await process_text_with_rules(d, orig_caption)
    user_cap = await get_user_data_key(d, 'caption', '')
    ft = f'{proc_text}\n\n{user_cap}' if proc_text and user_cap else user_cap if user_cap else proc_text

    # Fast server-side copy preserves the ORIGINAL caption — skip it whenever
    # text rules or a user caption apply, so both paths produce the same text.
    if deliver_via_bot and not ft:
        try:
            await sender.copy_media_group(tcid, msgs[0].chat.id, msgs[0].id)
            await _safe_cleanup(X.delete_messages(did, p.id))
            return f'✅ 相册已一比一转发（{len(msgs)} 项）'
        except Exception as e:
            print(f'copy_media_group failed, falling back to re-upload: {e}')

    st = time.time()
    media = []
    files = []
    try:
        for idx, one in enumerate(msgs):
            await X.edit_message_text(did, p.id, f'正在下载 {idx + 1}/{len(msgs)}...')
            im, ifiles = await _download_media_item(u, one, uid, idx, 'album', X, did, p.id, st)
            if im is None:
                continue
            media.append(im)
            files.extend(ifiles)
    except Exception:
        # A failed download/progress step must not leak already-downloaded
        # files. FloodWait propagates too — the retry re-downloads cleanly.
        for ff in files:
            if os.path.exists(ff):
                os.remove(ff)
        raise

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
            await asyncio.sleep(UPLOAD_INTERVAL)
        for f in files:
            if os.path.exists(f):
                os.remove(f)
        if sent:
            await _safe_cleanup(X.delete_messages(did, p.id))
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
    await _safe_cleanup(X.delete_messages(did, p.id))
    return f'✅ 相册已发送（{len(media)} 项）'

async def process_merged(c, u, msgs, d, uid, oc=None):
    """Merge multiple fetched messages into ONE delivery.

    All media (photo/video/audio/document) across every message is re-uploaded
    as a single album — chunked into groups of <= 10 (Telegram's media-group
    limit). All text (standalone text messages + media captions) is combined
    into one block: used as the album caption when it fits (<= 1024 chars),
    otherwise sent as a standalone message after the album.  When ``oc`` is
    provided it replaces the combined original text entirely.
    """
    tcid, rtmid, deliver_via_bot = await resolve_delivery(d)
    sender = c if deliver_via_bot else (u or c)
    did = int(d)
    p = await X.send_message(did, f'正在合并 {len(msgs)} 条消息...')

    # Partition into media items and text pieces; media captions count as text.
    media_msgs = []
    text_pieces = []
    for one in msgs:
        if one.media and (one.photo or one.video or one.document or one.audio):
            media_msgs.append(one)
            if one.caption:
                text_pieces.append(one.caption.markdown)
        elif one.text:
            text_pieces.append(one.text.markdown)

    if oc is not None:
        proc_text = oc
    else:
        combined = '\n\n'.join(tp for tp in text_pieces if tp)
        proc_text = await process_text_with_rules(d, combined)
    user_cap = await get_user_data_key(d, 'caption', '')
    ft = f'{proc_text}\n\n{user_cap}' if proc_text and user_cap else user_cap if user_cap else proc_text

    # No media: send the combined text as one (or chunked) message(s).
    if not media_msgs:
        if not ft:
            await X.edit_message_text(did, p.id, '没有可合并的内容。')
            return '❌ 没有可合并的内容'
        for i in range(0, len(ft), 4096):
            await sender.send_message(tcid, text=ft[i:i + 4096], reply_to_message_id=rtmid)
        await _safe_cleanup(X.delete_messages(did, p.id))
        return '✅ 文字已合并发送'

    # Download every media item.
    st = time.time()
    media = []
    files = []
    try:
        for idx, one in enumerate(media_msgs):
            await X.edit_message_text(did, p.id, f'正在下载 {idx + 1}/{len(media_msgs)}...')
            try:
                im, ifiles = await _download_media_item(u, one, uid, idx, 'merge', X, did, p.id, st)
            except FloodWait as e:
                secs = _flood_secs(e)
                print(f'FloodWait {secs}s downloading merge item {idx + 1}, waiting')
                await X.edit_message_text(did, p.id, f'Telegram 限流，等待 {secs}s 后重试...')
                await asyncio.sleep(secs)
                try:
                    im, ifiles = await _download_media_item(u, one, uid, idx, 'merge', X, did, p.id, st)
                except Exception as e2:
                    print(f'Retry download failed for merge item {idx + 1}: {e2}')
                    im = None
                    ifiles = []
            if im is None:
                continue
            media.append(im)
            files.extend(ifiles)
    except Exception:
        for ff in files:
            if os.path.exists(ff):
                os.remove(ff)
        raise
    if not media:
        await X.edit_message_text(did, p.id, '媒体下载全部失败')
        return '❌ 媒体下载全部失败'

    # Caption distribution: when oc is set and media needs >1 chunk, each
    # chunk's album carries the SAME text plus a (n/N) progress marker so the
    # recipient knows which part is which. Without oc, the original behavior
    # is preserved (caption only on the first item, or standalone if >1024).
    num_chunks = (len(media) + 9) // 10
    standalone_text = None
    if ft:
        if oc is not None and num_chunks > 1:
            marker_tmpl = '\n\n({}/{})'
            for ci in range(num_chunks):
                chunk_start = ci * 10
                chunk_media = media[chunk_start:chunk_start + 10]
                marker = marker_tmpl.format(ci + 1, num_chunks)
                # Leave room for the marker within Telegram's 1024-char cap.
                max_cap = 1024 - len(marker)
                chunk_media[0].caption = ft[:max_cap] + marker
        elif len(ft) <= 1024:
            media[0].caption = ft
        else:
            standalone_text = ft

    await X.edit_message_text(did, p.id, f'正在上传（{len(media)} 项）...')
    sent_items = 0
    for start in range(0, len(media), 10):
        chunk = media[start:start + 10]
        try:
            await sender.send_media_group(tcid, chunk, reply_to_message_id=rtmid)
            sent_items += len(chunk)
            continue
        except TypeError as e:
            if 'keyword-only argument' in str(e):
                # pyrofork parse bug: the RPC already succeeded — the album is
                # already delivered, only response parsing failed.
                sent_items += len(chunk)
                continue
            print(f'send_media_group failed on chunk {start}: {e}')
        except FloodWait as e:
            await asyncio.sleep(_flood_secs(e))
            try:
                await sender.send_media_group(tcid, chunk, reply_to_message_id=rtmid)
                sent_items += len(chunk)
                continue
            except Exception as e2:
                print(f'Retry send_media_group failed on chunk {start}: {e2}')
        except Exception as e:
            print(f'send_media_group failed on chunk {start} ({e}), falling back to per-item')
        # Per-item fallback for whatever the group attempt above could not send.
        for im in chunk:
            try:
                await _send_album_item(sender, tcid, im, rtmid)
                sent_items += 1
            except FloodWait as e:
                await asyncio.sleep(_flood_secs(e))
                try:
                    await _send_album_item(sender, tcid, im, rtmid)
                    sent_items += 1
                except Exception as e2:
                    print(f'Per-item send failed after flood wait: {e2}')
            except Exception as e2:
                print(f'Per-item send failed: {e2}')
            await asyncio.sleep(UPLOAD_INTERVAL)

    for ff in files:
        if os.path.exists(ff):
            os.remove(ff)

    if standalone_text:
        try:
            await sender.send_message(tcid, text=standalone_text, reply_to_message_id=rtmid)
        except Exception as e:
            print(f'Standalone text send failed: {e}')

    if sent_items:
        await _safe_cleanup(X.delete_messages(did, p.id))
        return f'✅ 已合并发送（{sent_items} 项媒体）'
    await X.edit_message_text(did, p.id, '合并上传失败')
    return '❌ 合并上传失败'


def _cleanup_downloaded_thumbnail(th, downloads_dir):
    if not th:
        return
    try:
        if os.path.dirname(os.path.abspath(th)) == downloads_dir and os.path.exists(th):
            os.remove(th)
    except Exception:
        pass

async def process_msg(c, u, m, d, lt, uid, i, oc=None):
    f = None  # downloaded temp file; the finally below guarantees cleanup
    th = None
    downloads_dir = os.path.abspath(os.path.join(_WORKDIR, 'downloads'))
    try:
        tcid, rtmid, deliver_via_bot = await resolve_delivery(d)
        did = int(d)

        if m.media:
            if oc is not None:
                proc_text = oc
            else:
                orig_text = m.caption.markdown if m.caption else ''
                proc_text = await process_text_with_rules(d, orig_text)
            user_cap = await get_user_data_key(d, 'caption', '')
            ft = f'{proc_text}\n\n{user_cap}' if proc_text and user_cap else user_cap if user_cap else proc_text
            
            if lt == 'public' and not emp.get((uid, i), False):
                # Direct file reference send requires the file reference holder's client.
                sent, error = await send_direct(c, m, tcid, ft, rtmid)
                if sent:
                    return 'Sent directly.'
                if error and 'PEER_ID_INVALID' in error:
                    return (
                        '发送失败：目标聊天不可用。请在 /settings 设置正确的 '
                        '-100... 聊天 ID，并将 /setbot 机器人加入该频道且设为管理员。'
                    )
                # Stale or cross-client file references (MEDIA_EMPTY) are
                # recoverable: fall through to download + re-upload instead
                # of failing the task.
                print(f'Direct send failed ({error}), falling back to re-upload')
            
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

            # Temp names carry uid + timestamp: concurrent users must never
            # share a downloads/ path (overwrite / cross-delivery / premature
            # cleanup).
            c_name = f"{uid}_{time.time()}"
            if m.video:
                file_name = m.video.file_name
                if not file_name:
                    file_name = f"{time.time()}.mp4"
                    c_name = sanitize(f"{uid}_{file_name}")
            elif m.audio:
                file_name = m.audio.file_name
                if not file_name:
                    file_name = f"{time.time()}.mp3"
                    c_name = sanitize(f"{uid}_{file_name}")
            elif m.document:
                file_name = m.document.file_name
                if not file_name:
                    file_name = f"{time.time()}"
                else:
                    c_name = sanitize(f"{uid}_{int(time.time())}_{file_name}")
            elif m.photo:
                file_name = f"{time.time()}.jpg"
                c_name = sanitize(f"{uid}_{file_name}")
    
            # pyrofork download_media resolves relative names against PARENT_DIR
            # (Path(sys.argv[0]).parent = /app, read-only image layer), ignoring the
            # client workdir. Pass an absolute path under the writable volume.
            download_path = os.path.join(_WORKDIR, 'downloads', c_name)
            # Download with the client that fetched the message: emp False on a
            # public link means the bot fetched it (user client may be absent
            # or not a member); otherwise the user client holds access.
            dl_client = (c or u) if (lt == 'public' and not emp.get((uid, i), False)) else (u or c)
            f = await dl_client.download_media(m, file_name=download_path, progress=prog, progress_args=(X, did, p.id, st))
            
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
                await _safe_cleanup(X.delete_messages(did, p.id))
                
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
                    await sender.send_sticker(tcid, f, reply_to_message_id=rtmid)
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
            await _safe_cleanup(X.delete_messages(did, p.id))
            
            return 'Done.'
            
        elif m.text:
            sender = c if deliver_via_bot else (u or c)
            await sender.send_message(tcid, text=oc if oc is not None else m.text.markdown, reply_to_message_id=rtmid)
            return 'Sent.'
    except Exception as e:
        return f'Error: {str(e)[:50]}'
    finally:
        # Any mid-processing exception (rename, metadata, upload setup) would
        # otherwise strand the downloaded file in downloads/ forever.
        if f and isinstance(f, str) and os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
        _cleanup_downloaded_thumbnail(th, downloads_dir)
        
def parse_link_lines(text):
    """Parse /batch input.

    One non-empty line  -> ('range', (cid, sid, lt, comment_id))
    Multiple lines      -> ('multi', [(cid, sid, lt, comment_id), ...])
    Any unparsable line -> ('invalid', (line_no, line_text))
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    parsed = []
    for idx, ln in enumerate(lines, 1):
        ci, di, lti, comment_id = E(ln)
        if not ci or not di:
            return 'invalid', (idx, ln)
        parsed.append((ci, di, lti, comment_id))
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


async def process_one_link(ubot, uc, i, s, lt, d, uid, oc=None, comment_id=None):
    """Fetch and deliver one t.me link (expanding albums), with one FloodWait retry."""
    try:
        return await _process_one_link(ubot, uc, i, s, lt, d, uid, oc, comment_id)
    except FloodWait as e:
        secs = _flood_secs(e)
        print(f'FloodWait {secs}s on {i}/{s}, waiting and retrying once')
        await asyncio.sleep(secs)
        return await _process_one_link(ubot, uc, i, s, lt, d, uid, oc, comment_id)


async def _process_one_link(ubot, uc, i, s, lt, d, uid, oc=None, comment_id=None):
    """Fetch and deliver one t.me link (expanding albums). Returns a status string."""
    if not uc and lt != 'public':
        return '用户会话无效或未登录，请先使用 /login。'
    msg = await get_msg(ubot, uc, i, s, lt, uid, comment_id)
    if not msg:
        return '未找到消息'
    msgs = [msg]
    # Comment links resolve to a discussion-group message whose chat differs
    # from the URL's channel. Use the message's own chat for media-group
    # expansion so we fetch the right group.
    src_chat = msg.chat.id if getattr(msg, 'chat', None) else i
    src_lt = 'private' if comment_id else lt
    if getattr(msg, 'media_group_id', None):
        fetch_client = uc if (uc and (src_lt == 'private' or emp.get((uid, src_chat), False))) else ubot
        try:
            group = await fetch_client.get_media_group(src_chat, msg.id)
            if group:
                msgs = group
        except FloodWait:
            raise
        except Exception as e:
            print(f'Media group fetch failed, falling back to single: {e}')
    if len(msgs) > 1:
        return await process_album(ubot, uc, msgs, d, src_lt, uid, src_chat, oc)
    return await process_msg(ubot, uc, msgs[0], d, src_lt, uid, src_chat, oc)


@X.on_message(filters.command(['batch', 'single', 'merge']))
async def process_cmd(c, m):
    uid = m.from_user.id
    cmd = m.command[0]
    
    if FREEMIUM_LIMIT == 0 and not await is_premium_user(uid):
        await m.reply_text("此机器人不提供免费服务，请向管理员订阅")
        return
    
    if await sub(c, m) == 1: return
    pro = await m.reply_text('正在进行检查，请稍候...')

    qsize = get_queue_size(uid)
    if qsize >= _MAX_QUEUE:
        await pro.edit(f'队列已满（{_MAX_QUEUE} 个任务排队中）。请使用 /tasks 查看，或 /stop 取消。')
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
    
    # Custom caption: text after the command name on the FIRST line only
    # (e.g. "/merge my title"). m.command holds the parsed first-line tokens;
    # using it (not m.text) avoids two problems: (1) m.text on bot messages
    # is a Text object without .split, and (2) anything after a newline in
    # the command message (e.g. an accidentally pasted link) would otherwise
    # be swallowed into the caption.
    oc = ' '.join(m.command[1:]).strip() or None
    Z[uid] = {'step': {'batch': 'start', 'single': 'start_single', 'merge': 'start_merge'}[cmd], 'oc': oc}
    oc_note = f'\n📝 自定义说明（替换原文字）：{oc}' if oc else ''
    if cmd == 'batch':
        await pro.edit(f'发送起始链接（连续下载指定数量），或多条链接（每行一条，逐个下载）。{oc_note}')
    elif cmd == 'merge':
        await pro.edit(f'发送要合并的链接（每行一条）。所有内容将合并为一条消息发送（多媒体合并为相册，文字合并到一起）。{oc_note}')
    else:
        await pro.edit(f'发送要处理的链接。{oc_note}')

@X.on_message(filters.command(['cancel', 'stop']))
async def cancel_cmd(c, m):
    uid = m.from_user.id
    cancelled = request_cancel_tasks(uid)
    had_state = Z.pop(uid, None) is not None
    if cancelled:
        await m.reply_text(f'已请求取消 {cancelled} 个任务。进行中的将在当前步骤完成后停止。')
    elif had_state:
        await m.reply_text('已取消。')
    else:
        await m.reply_text('没有正在进行的任务。')

@X.on_message(filters.command('tasks'))
async def tasks_cmd(c, m):
    uid = m.from_user.id
    user_tasks = get_user_tasks(uid)
    if not user_tasks:
        await m.reply_text('📋 没有任务记录。')
        return
    # Show last 5 tasks
    lines = ['📋 **任务列表**（最近 5 个）\n']
    status_icons = {
        'queued': '⏳', 'running': '🔄', 'done': '✅',
        'failed': '❌', 'cancelled': '🚫',
    }
    for t in user_tasks[:5]:
        icon = status_icons.get(t['status'], '❓')
        elapsed = ''
        if t['finished_at']:
            elapsed = f' · 用时 {int(t["finished_at"] - t["created_at"])}s'
        elif t['status'] == 'running':
            elapsed = f' · 已运行 {int(time.time() - t["created_at"])}s'
        progress = f'{t["current"]}/{t["total"]}'
        if t['progress_msg']:
            progress += f' · {t["progress_msg"]}'
        lines.append(f'{icon} **{t["type"]}** {progress}{elapsed}')
        if t['result']:
            lines.append(f'   └ {t["result"][:80]}')
    lines.append(f'\n队列：{get_queue_size(uid)} 个等待')
    await m.reply_text('\n'.join(lines))

@X.on_message(filters.text & filters.private & ~login_in_progress & ~filters.command([
    'start', 'batch', 'cancel', 'login', 'logout', 'stop', 'set', 
    'pay', 'redeem', 'gencode', 'single', 'generate', 'keyinfo', 'encrypt', 'decrypt', 'keys', 'setbot', 'rembot', 'merge', 'tasks']))
async def text_handler(c, m):
    uid = m.from_user.id
    if uid not in Z: return
    s = Z[uid].get('step')
    oc = Z[uid].get('oc')
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
            i, d, lt, _comment = payload
            Z[uid].update({'step': 'count', 'cid': i, 'sid': d, 'lt': lt})
            try:
                await m.reply_text('要处理多少条消息？')
            except Exception:
                Z.pop(uid, None)
                raise
            return

        links = payload
        n = len(links)
        maxlimit = PREMIUM_LIMIT if await is_premium_user(uid) else FREEMIUM_LIMIT
        if n > maxlimit:
            await m.reply_text(f'一次最多 {maxlimit} 条链接，你发送了 {n} 条。')
            Z.pop(uid, None)
            return
        ubot = UB.get(uid)
        if not ubot:
            await m.reply_text('请先使用 /setbot 添加机器人')
            Z.pop(uid, None)
            return
        task = create_task(uid, 'batch_links', n, links=links, caption=oc, chat_id=str(m.chat.id))
        await enqueue_task(uid, task)
        qpos = get_queue_size(uid)
        await m.reply_text(f'📦 批量提取任务已加入队列（{n} 条链接）。\n位置：{"执行中" if qpos <= 1 else f"队列第 {qpos-1} 位"}\n使用 /tasks 查看进度。')
        Z.pop(uid, None)

    elif s == 'start_single':
        L = m.text
        i, d, lt, comment_id = E(L)
        if not i or not d:
            await m.reply_text('链接格式无效。')
            Z.pop(uid, None)
            return
        ubot = UB.get(uid)
        if not ubot:
            await m.reply_text('请先使用 /setbot 添加机器人')
            Z.pop(uid, None)
            return
        task = create_task(uid, 'single', 1, link_info=(i, d, lt, comment_id), caption=oc, chat_id=str(m.chat.id))
        await enqueue_task(uid, task)
        qpos = get_queue_size(uid)
        await m.reply_text(f'📦 单条提取任务已加入队列。\n位置：{"执行中" if qpos <= 1 else f"队列第 {qpos-1} 位"}\n使用 /tasks 查看进度。')
        Z.pop(uid, None)

    elif s == 'start_merge':
        mode, payload = parse_link_lines(m.text)
        if mode == 'invalid':
            idx, line = payload
            await m.reply_text(f'第 {idx} 行链接格式无效：{line[:50]}')
            Z.pop(uid, None)
            return
        links = [payload] if mode == 'range' else payload
        n = len(links)
        maxlimit = PREMIUM_LIMIT if await is_premium_user(uid) else FREMIUM_LIMIT
        if n > maxlimit:
            await m.reply_text(f'一次最多 {maxlimit} 条链接，你发送了 {n} 条。')
            Z.pop(uid, None)
            return
        ubot = UB.get(uid)
        if not ubot:
            await m.reply_text('请先使用 /setbot 添加机器人')
            Z.pop(uid, None)
            return
        task = create_task(uid, 'merge', n, links=links, caption=oc, chat_id=str(m.chat.id))
        await enqueue_task(uid, task)
        qpos = get_queue_size(uid)
        await m.reply_text(f'📦 合并任务已加入队列（{n} 条链接）。\n位置：{"执行中" if qpos <= 1 else f"队列第 {qpos-1} 位"}\n使用 /tasks 查看进度。')
        Z.pop(uid, None)


    elif s == 'count':
        if not m.text.isdigit():
            await m.reply_text('请输入有效数字。')
            return
        count = int(m.text)
        if count < 1:
            await m.reply_text('数量至少为 1。')
            return
        maxlimit = PREMIUM_LIMIT if await is_premium_user(uid) else FREMIUM_LIMIT
        if count > maxlimit:
            await m.reply_text(f'最大限制为 {maxlimit}。')
            return
        cid = Z[uid]['cid']
        sid = Z[uid]['sid']
        lt = Z[uid]['lt']
        ubot = UB.get(uid)
        if not ubot:
            await m.reply_text('请先使用 /setbot 添加机器人')
            Z.pop(uid, None)
            return
        task = create_task(uid, 'batch_count', count, cid=cid, sid=sid, lt=lt, num=count, caption=oc, chat_id=str(m.chat.id))
        await enqueue_task(uid, task)
        qpos = get_queue_size(uid)
        await m.reply_text(f'📦 批量提取任务已加入队列（{count} 条）。\n位置：{"执行中" if qpos <= 1 else f"队列第 {qpos-1} 位"}\n使用 /tasks 查看进度。')
        Z.pop(uid, None)



