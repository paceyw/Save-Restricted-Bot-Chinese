# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

import time
from pyrogram import filters
from config import FREEMIUM_LIMIT, PREMIUM_LIMIT
from shared_client import app as main_bot
from utils.func import get_user_data_key, is_premium_user, parse_link
from plugins.fetch import get_ubot
from plugins.tasks import (
    _MAX_QUEUE, create_task, enqueue_task, get_queue_size, get_user_tasks,
    request_cancel_tasks,
)
from plugins.start import subscribe as sub
from utils.custom_filters import login_in_progress

pending_flows = {}
_Z_TS = {}
_Z_IDLE_TTL = 1800




        
def parse_link_lines(text):
    """Parse /batch input.

    One non-empty line  -> ('range', (cid, sid, lt, comment_id))
    Multiple lines      -> ('multi', [(cid, sid, lt, comment_id), ...])
    Any unparsable line -> ('invalid', (line_no, line_text))
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    parsed = []
    for idx, ln in enumerate(lines, 1):
        ci, di, lti, comment_id = parse_link(ln)
        if not ci or not di:
            return 'invalid', (idx, ln)
        parsed.append((ci, di, lti, comment_id))
    if not parsed:
        return 'invalid', (1, text.strip()[:50])
    if len(parsed) == 1:
        return 'range', parsed[0]
    return 'multi', parsed




@main_bot.on_message(filters.command(['batch', 'single', 'merge']))
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
    pending_flows[uid] = {'step': {'batch': 'start', 'single': 'start_single', 'merge': 'start_merge'}[cmd], 'oc': oc}
    _Z_TS[uid] = time.time()
    oc_note = f'\n📝 自定义说明（替换原文字）：{oc}' if oc else ''
    if cmd == 'batch':
        await pro.edit(f'发送起始链接（连续下载指定数量），或多条链接（每行一条，逐个下载）。{oc_note}')
    elif cmd == 'merge':
        await pro.edit(f'发送要合并的链接（每行一条）。所有内容将合并为一条消息发送（多媒体合并为相册，文字合并到一起）。{oc_note}')
    else:
        await pro.edit(f'发送要处理的链接。{oc_note}')

@main_bot.on_message(filters.command(['cancel', 'stop']))
async def cancel_cmd(c, m):
    uid = m.from_user.id
    cancelled = request_cancel_tasks(uid)
    had_state = pending_flows.pop(uid, None) is not None
    if cancelled:
        await m.reply_text(f'已请求取消 {cancelled} 个任务。进行中的将在当前步骤完成后停止。')
    elif had_state:
        await m.reply_text('已取消。')
    else:
        await m.reply_text('没有正在进行的任务。')

@main_bot.on_message(filters.command('tasks'))
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

@main_bot.on_message(filters.text & filters.private & ~login_in_progress & ~filters.command([
    'start', 'batch', 'cancel', 'login', 'logout', 'stop', 'set', 
    'pay', 'redeem', 'gencode', 'single', 'generate', 'keyinfo', 'encrypt', 'decrypt', 'keys', 'setbot', 'rembot', 'merge', 'tasks']))
async def text_handler(c, m):
    uid = m.from_user.id
    if uid not in pending_flows: return
    _Z_TS[uid] = time.time()
    s = pending_flows[uid].get('step')
    oc = pending_flows[uid].get('oc')
    x = await get_ubot(uid)
    if not x:
        pending_flows.pop(uid, None)
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
            pending_flows.pop(uid, None)
            return
        if mode == 'range':
            i, d, lt, _comment = payload
            pending_flows[uid].update({'step': 'count', 'cid': i, 'sid': d, 'lt': lt})
            try:
                await m.reply_text('要处理多少条消息？')
            except Exception:
                pending_flows.pop(uid, None)
                raise
            return

        links = payload
        n = len(links)
        maxlimit = PREMIUM_LIMIT if await is_premium_user(uid) else FREEMIUM_LIMIT
        if n > maxlimit:
            await m.reply_text(f'一次最多 {maxlimit} 条链接，你发送了 {n} 条。')
            pending_flows.pop(uid, None)
            return
        ubot = await get_ubot(uid)
        if not ubot:
            await m.reply_text('请先使用 /setbot 添加机器人')
            pending_flows.pop(uid, None)
            return
        task = create_task(uid, 'batch_links', n, links=links, caption=oc, chat_id=str(m.chat.id))
        if not await enqueue_task(uid, task):
            await m.reply_text(f'队列已满（{_MAX_QUEUE} 个任务排队中）。请使用 /tasks 查看，或 /stop 取消。')
            pending_flows.pop(uid, None)
            return
        qpos = get_queue_size(uid)
        await m.reply_text(f'📦 批量提取任务已加入队列（{n} 条链接）。\n位置：{"执行中" if qpos <= 1 else f"队列第 {qpos-1} 位"}\n使用 /tasks 查看进度。')
        pending_flows.pop(uid, None)

    elif s == 'start_single':
        L = m.text
        i, d, lt, comment_id = parse_link(L)
        if not i or not d:
            await m.reply_text('链接格式无效。')
            pending_flows.pop(uid, None)
            return
        ubot = await get_ubot(uid)
        if not ubot:
            await m.reply_text('请先使用 /setbot 添加机器人')
            pending_flows.pop(uid, None)
            return
        task = create_task(uid, 'single', 1, link_info=(i, d, lt, comment_id), caption=oc, chat_id=str(m.chat.id))
        if not await enqueue_task(uid, task):
            await m.reply_text(f'队列已满（{_MAX_QUEUE} 个任务排队中）。请使用 /tasks 查看，或 /stop 取消。')
            pending_flows.pop(uid, None)
            return
        qpos = get_queue_size(uid)
        await m.reply_text(f'📦 单条提取任务已加入队列。\n位置：{"执行中" if qpos <= 1 else f"队列第 {qpos-1} 位"}\n使用 /tasks 查看进度。')
        pending_flows.pop(uid, None)

    elif s == 'start_merge':
        mode, payload = parse_link_lines(m.text)
        if mode == 'invalid':
            idx, line = payload
            await m.reply_text(f'第 {idx} 行链接格式无效：{line[:50]}')
            pending_flows.pop(uid, None)
            return
        links = [payload] if mode == 'range' else payload
        n = len(links)
        maxlimit = PREMIUM_LIMIT if await is_premium_user(uid) else FREEMIUM_LIMIT
        if n > maxlimit:
            await m.reply_text(f'一次最多 {maxlimit} 条链接，你发送了 {n} 条。')
            pending_flows.pop(uid, None)
            return
        ubot = await get_ubot(uid)
        if not ubot:
            await m.reply_text('请先使用 /setbot 添加机器人')
            pending_flows.pop(uid, None)
            return
        task = create_task(uid, 'merge', n, links=links, caption=oc, chat_id=str(m.chat.id))
        if not await enqueue_task(uid, task):
            await m.reply_text(f'队列已满（{_MAX_QUEUE} 个任务排队中）。请使用 /tasks 查看，或 /stop 取消。')
            pending_flows.pop(uid, None)
            return
        qpos = get_queue_size(uid)
        await m.reply_text(f'📦 合并任务已加入队列（{n} 条链接）。\n位置：{"执行中" if qpos <= 1 else f"队列第 {qpos-1} 位"}\n使用 /tasks 查看进度。')
        pending_flows.pop(uid, None)


    elif s == 'count':
        if not m.text.isdigit():
            await m.reply_text('请输入有效数字。')
            return
        count = int(m.text)
        if count < 1:
            await m.reply_text('数量至少为 1。')
            return
        maxlimit = PREMIUM_LIMIT if await is_premium_user(uid) else FREEMIUM_LIMIT
        if count > maxlimit:
            await m.reply_text(f'最大限制为 {maxlimit}。')
            return
        cid = pending_flows[uid]['cid']
        sid = pending_flows[uid]['sid']
        lt = pending_flows[uid]['lt']
        ubot = await get_ubot(uid)
        if not ubot:
            await m.reply_text('请先使用 /setbot 添加机器人')
            pending_flows.pop(uid, None)
            return
        task = create_task(uid, 'batch_count', count, cid=cid, sid=sid, lt=lt, num=count, caption=oc, chat_id=str(m.chat.id))
        if not await enqueue_task(uid, task):
            await m.reply_text(f'队列已满（{_MAX_QUEUE} 个任务排队中）。请使用 /tasks 查看，或 /stop 取消。')
            pending_flows.pop(uid, None)
            return
        qpos = get_queue_size(uid)
        await m.reply_text(f'📦 批量提取任务已加入队列（{count} 条）。\n位置：{"执行中" if qpos <= 1 else f"队列第 {qpos-1} 位"}\n使用 /tasks 查看进度。')
        pending_flows.pop(uid, None)




async def _sweep_pending_flows(now=None):
    if now is None:
        now = time.time()
    for uid, timestamp in list(_Z_TS.items()):
        if uid not in pending_flows:
            _Z_TS.pop(uid, None)
        elif now - timestamp > _Z_IDLE_TTL:
            pending_flows.pop(uid, None)
            _Z_TS.pop(uid, None)
    for uid in pending_flows:
        _Z_TS.setdefault(uid, now)

from plugins import tasks as tasks_module
tasks_module.register_sweep_hook(_sweep_pending_flows)

try:
    from plugins.settings import _sweep_active_conversations
    tasks_module.register_sweep_hook(_sweep_active_conversations)
except Exception:
    pass
