# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.
import asyncio
import logging
import re
import time
import inspect
from config import BATCH_INTERVAL, CHANNEL_INTERVAL, MERGE_INTERVAL
try:
    from config import BATCH_MIN_INTERVAL
except ImportError:
    # Legacy config shims (test fixtures) predate the adaptive floor: pin the
    # floor to the ceiling so the limiter degenerates to the old fixed sleep.
    BATCH_MIN_INTERVAL = BATCH_INTERVAL
from utils.func import get_user_data, filter_settings, cred_epoch, prune_cred_epochs
from utils.ratelimit import RateLimiter
from plugins import fetch as fetch_module
from plugins.fetch import (
    get_ubot, get_uclient, get_msg, resolve_linked_chat, _client_lock,
)
from pyrogram.errors import FloodWait

logger = logging.getLogger(__name__)
_TASK_RESULT_TTL = 600
_MAX_TASKS_PER_USER = 20
_SWEEP_INTERVAL = 60
_SWEEPER_TASK = None
_SWEEP_LOCK = None
_SWEEP_HOOKS = []

def register_sweep_hook(fn):
    """Register an async callback invoked by the periodic state sweeper."""
    if fn not in _SWEEP_HOOKS:
        _SWEEP_HOOKS.append(fn)

def _ensure_sweeper():
    global _SWEEPER_TASK
    if _SWEEPER_TASK is not None and not _SWEEPER_TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _SWEEPER_TASK = loop.create_task(_sweeper_loop())

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


def _prune_task_history(uid):
    user_tasks = [
        (task_id, candidate) for task_id, candidate in TASKS.items()
        if candidate.get('uid') == uid
    ]
    excess = len(user_tasks) - _MAX_TASKS_PER_USER
    if excess <= 0:
        return
    completed = sorted(
        (
            (task_id, candidate) for task_id, candidate in user_tasks
            if candidate.get('status') in ('done', 'failed', 'cancelled')
        ),
        key=lambda item: item[1].get('created_at', 0),
    )
    for task_id, _ in completed[:excess]:
        TASKS.pop(task_id, None)


def _has_active_task(uid):
    return any(
        task.get('uid') == uid and task.get('status') in ('queued', 'running')
        for task in TASKS.values()
    )



def create_task(uid, task_type, total, **params):
    """Create a task descriptor and register it in TASKS."""
    global _TASK_SEQ
    _TASK_SEQ += 1
    tid = f'task_{uid}_{int(time.time())}_{_TASK_SEQ}'
    task = {
        'id': tid,
        'uid': uid,
        'type': task_type,         # 'batch_links' | 'single' | 'merge' | 'batch_count' | 'dl' | 'adl'
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
    _prune_task_history(uid)
    return task

async def enqueue_task(uid, task):
    """Enqueue a task for a user, starting the worker if needed."""
    _ensure_sweeper()
    async with _client_lock(uid):
        queue = USER_QUEUES.get(uid)
        if queue is not None and queue.qsize() >= _MAX_QUEUE:
            TASKS.pop(task.get('id'), None)
            return False
        if queue is None:
            queue = USER_QUEUES[uid] = asyncio.Queue()
            USER_WORKERS[uid] = asyncio.create_task(_task_worker(uid))
        await queue.put(task)
        return True


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
            _prune_task_history(uid)
            queue.task_done()

async def _dispatch_task(uid, task):
    """Snapshot settings when execution starts, then route the task.

    This find_one is the ONLY users-collection read of the whole task. The
    full document is forwarded as a call argument — never stored in TASKS, so
    session_string is not retained in task history — letting client
    establishment (get_ubot/get_uclient) reuse it instead of re-querying."""
    # Epoch is captured BEFORE the read it validates: a rotation completing
    # during the await then yields (old doc, old epoch) — a conservative
    # mismatch the helpers discard for a fresh read, never stale acceptance.
    # dl/adl run no userbot and read no settings: skip the snapshot read.
    if task['type'] == 'dl':
        await _run_dl(uid, task)
        return
    if task['type'] == 'adl':
        await _run_adl(uid, task)
        return
    epoch = cred_epoch(uid)
    doc = await get_user_data(uid) or {}
    task['settings'] = filter_settings(doc)
    if task['type'] == 'batch_links':
        await _run_batch_links(uid, task, doc, epoch)
    elif task['type'] == 'single':
        await _run_single(uid, task, doc, epoch)
    elif task['type'] == 'merge':
        await _run_merge(uid, task, doc, epoch)
    elif task['type'] == 'batch_count':
        await _run_batch_count(uid, task, doc, epoch)

def task_should_cancel(task_id):
    t = TASKS.get(task_id)
    return t is not None and t.get('cancel_requested', False)

def task_update(task_id, current=None, success=None, progress_msg=None, result=None):
    t = TASKS.get(task_id)
    if t is None:
        return
    if current is not None:
        t['current'] = current
    if success is not None:
        t['success'] = success
    if progress_msg is not None:
        t['progress_msg'] = progress_msg
    if result is not None:
        t['result'] = result

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




async def _sweep_once(now=None):
    global _SWEEP_LOCK
    if _SWEEP_LOCK is None:
        _SWEEP_LOCK = asyncio.Lock()
    async with _SWEEP_LOCK:
        await _sweep_once_impl(now)


async def _sweep_once_impl(now=None):
    """Run one bounded-state cleanup pass."""
    if now is None:
        now = time.time()
    fetch = fetch_module

    for task_id, task in list(TASKS.items()):
        finished_at = task.get('finished_at')
        if finished_at is not None and now - finished_at > _TASK_RESULT_TTL:
            TASKS.pop(task_id, None)
    for uid in {task.get('uid') for task in TASKS.values()}:
        if uid is not None:
            _prune_task_history(uid)

    for uid, peers in list(fetch._PEER_CACHE.items()):
        for key, entry in list(peers.items()):
            if entry[1] <= now:
                peers.pop(key, None)
        if not peers:
            fetch._PEER_CACHE.pop(uid, None)

    for uid in set(fetch.user_bots) | set(fetch.user_clients) | set(USER_QUEUES) | set(USER_WORKERS):
        has_clients = uid in fetch.user_bots or uid in fetch.user_clients
        if has_clients:
            last_used = fetch._CLIENT_LAST_USED.get(uid)
            if last_used is None:
                fetch._CLIENT_LAST_USED[uid] = now
                continue
            if now - last_used <= fetch._CLIENT_IDLE_TTL:
                continue
        if _has_active_task(uid) or get_queue_size(uid) > 0:
            continue

        async with fetch._client_lock(uid):
            has_clients = uid in fetch.user_bots or uid in fetch.user_clients
            if has_clients:
                last_used = fetch._CLIENT_LAST_USED.get(uid)
                if last_used is None:
                    fetch._CLIENT_LAST_USED[uid] = now
                    continue
                if now - last_used <= fetch._CLIENT_IDLE_TTL:
                    continue
            if _has_active_task(uid) or get_queue_size(uid) > 0:
                continue

            clients = (
                fetch.user_bots.pop(uid, None),
                fetch.user_clients.pop(uid, None),
            )
            fetch._UB_EPOCH.pop(uid, None)
            fetch._UC_EPOCH.pop(uid, None)
            for client in clients:
                if client is None or client is fetch.premium_userbot:
                    continue
                try:
                    await asyncio.wait_for(client.stop(), timeout=10)
                except asyncio.TimeoutError:
                    logger.warning("Timed out stopping idle client for %s", uid)
                except Exception as exc:
                    logger.warning("Error stopping idle client for %s: %s", uid, exc)
            fetch._CLIENT_LAST_USED.pop(uid, None)
            fetch._PEER_CACHE.pop(uid, None)

            worker = USER_WORKERS.pop(uid, None)
            if worker is not None:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning("Error stopping idle worker for %s: %s", uid, exc)
            USER_QUEUES.pop(uid, None)

    for uid in list(fetch._CLIENT_LAST_USED):
        if uid not in fetch.user_bots and uid not in fetch.user_clients and not _has_active_task(uid):
            fetch._CLIENT_LAST_USED.pop(uid, None)
            fetch._PEER_CACHE.pop(uid, None)

    for uid, lock in list(fetch._UB_UC_LOCKS.items()):
        if (
            uid not in fetch.user_bots
            and uid not in fetch.user_clients
            and not lock.locked()
            and not _has_active_task(uid)
        ):
            fetch._UB_UC_LOCKS.pop(uid, None)

    for uid in list(fetch._UB_EPOCH):
        if uid not in fetch.user_bots:
            fetch._UB_EPOCH.pop(uid, None)
    for uid in list(fetch._UC_EPOCH):
        if uid not in fetch.user_clients:
            fetch._UC_EPOCH.pop(uid, None)

    # Credential epochs live in utils.func; bound them to users with any live
    # per-user state (same criteria as the lock cleanup above).
    prune_cred_epochs(
        set(fetch.user_bots) | set(fetch.user_clients) | set(USER_QUEUES) | set(USER_WORKERS)
        | set(fetch._UB_UC_LOCKS)
        | {task.get('uid') for task in TASKS.values()}
    )

    for hook in list(_SWEEP_HOOKS):
        try:
            parameters = inspect.signature(hook).parameters.values()
            accepts_now = any(
                parameter.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.VAR_POSITIONAL,
                )
                for parameter in parameters
            )
            if accepts_now:
                await hook(now)
            else:
                await hook()
        except Exception as exc:
            logger.warning("Sweep hook failed: %s", exc)


async def _sweeper_loop():
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL)
        try:
            await _sweep_once()
        except Exception as exc:
            logger.warning("State sweep failed: %s", exc)



# ─── Task execution functions ───────────────────────────────────────────────────
# Each takes (uid, task) and runs to completion. Cancellation is checked via
# task_should_cancel(task['id']). Progress is reported via task_update().

async def _drain_prefetch(prefetch):
    """Cancel an armed prefetch and return its result payload if it had
    already completed (None otherwise).

    Uses asyncio.wait instead of directly awaiting the cancelled task: a
    direct ``await`` would conflate our own ``prefetch.cancel()`` with an
    external cancellation of this runner (both surface as CancelledError at
    the same point). asyncio.wait never cancels the tasks it waits on, so a
    CancelledError raised inside is unambiguously the runner's own and must
    propagate.
    """
    if prefetch is None:
        return None
    if not prefetch.done():
        prefetch.cancel()
        try:
            while not prefetch.done():
                await asyncio.wait({prefetch})
        except asyncio.CancelledError:
            # External cancellation of the runner itself. The prefetch is
            # already cancelling (its prepare self-cleans); do not swallow.
            raise
    if prefetch.cancelled():
        return None
    try:
        _, prepared = prefetch.result()
    except Exception:
        return None
    return prepared


async def _run_batch_links(uid, task, doc, epoch):
    """Execute multi-link batch extraction.

    Phase 7: pipeline window=1 — prepare (fetch + download) of link j+1
    overlaps the finish (rename/upload) of link j. Delivery order is
    preserved because prepare performs no content sends; every send happens
    in finish, strictly inside this loop. The fixed BATCH_INTERVAL sleep is
    replaced by an AIMD RateLimiter (floor BATCH_MIN_INTERVAL, ceiling
    BATCH_INTERVAL): FloodWait backs off, quiet links tighten.
    """
    settings = task['settings']
    links = task['links']
    oc = task.get('caption')
    chat_id = task['chat_id']
    n = len(links)
    ubot = await get_ubot(uid, prefetched=doc, prefetched_epoch=epoch)
    uc = await get_uclient(uid, prefetched=doc, prefetched_epoch=epoch)
    success = 0
    cancelled = False
    limiter = RateLimiter(base=BATCH_MIN_INTERVAL, ceiling=BATCH_INTERVAL)
    started_at = time.time()
    task_update(task['id'], progress_msg=f'批量提取 {n} 条链接...')

    async def prepare(idx):
        ci, di, lti, comment_id = links[idx]
        return await with_flood_retry(
            lambda: prepare_one_link(
                ubot, uc, ci, di, lti, chat_id, uid, oc, comment_id,
                settings=settings,
            ),
            context=f'{ci}/{di}',
            max_retries=2,
            on_flood=limiter.report_flood,
        )

    prefetch = None
    try:
        for j in range(n):
            if task_should_cancel(task['id']):
                cancelled = True
                task_update(task['id'], current=j, success=success, progress_msg=f'已取消（{j}/{n}）')
                break
            if prefetch is None:
                prefetch = asyncio.create_task(prepare(j))
            task_update(task['id'], current=j, success=success, progress_msg=f'正在提取 {j+1}/{n}...')
            current, prefetch = prefetch, None
            try:
                result, prepared_link = await current
                if j + 1 < n:
                    # Arm the next preparation before finishing this link so its
                    # fetch+download overlaps the upload below.
                    prefetch = asyncio.create_task(prepare(j + 1))
                if prepared_link is not None:
                    res = await with_flood_retry(
                        lambda: finish_one_link(prepared_link),
                        context=f'finish {j + 1}/{n}',
                        max_retries=2,
                        on_flood=limiter.report_flood,
                    )
                else:
                    res = result
                # An unsupported message yields a None result: not a success,
                # but also not the error the old _ok(None) crash logged.
                if res is not None and _ok(res):
                    success += 1
                limiter.report_success()
            except FloodWait as e:
                limiter.report_flood(_flood_secs(e))
                print(f'Batch link {j+1}/{n} error: {e}')
            except Exception as e:
                print(f'Batch link {j+1}/{n} error: {e}')
            await limiter.wait()
    except asyncio.CancelledError:
        # External worker cancellation: an armed prefetch must not outlive
        # the runner (its downloaded file / progress message would leak).
        prepared_link = await _drain_prefetch(prefetch)
        if prepared_link is not None and prepared_link.kind == 'single':
            await abort_prepared_msg(prepared_link.prepared)
        raise
    if cancelled and prefetch is not None:
        # The armed preparation may hold a downloaded file + progress
        # message. In-flight preparation self-cleans on CancelledError; a
        # completed one is aborted explicitly. Album preparations allocate
        # nothing, so only single-downloaded ones need the abort.
        prepared_link = await _drain_prefetch(prefetch)
        if prepared_link is not None and prepared_link.kind == 'single':
            await abort_prepared_msg(prepared_link.prepared)
    # Throughput-analysis signal: per-task wall time + terminal interval.
    logger.info(
        'batch_links done uid=%s links=%d success=%d cancelled=%s wall=%.1fs final_interval=%.1fs',
        uid, n, success, cancelled, time.time() - started_at, limiter.current,
    )
    if not cancelled:
        task_update(task['id'], current=n, success=success)
        task['result'] = f'✅ 批量提取完成：成功 {success}/{n}'
    else:
        task['result'] = f'已取消。成功：{success}/{n}'

async def _run_single(uid, task, doc, epoch):
    """Execute single-link extraction."""
    settings = task['settings']
    ci, di, lt, comment_id = task['link_info']
    oc = task.get('caption')
    chat_id = task['chat_id']
    ubot = await get_ubot(uid, prefetched=doc, prefetched_epoch=epoch)
    uc = await get_uclient(uid, prefetched=doc, prefetched_epoch=epoch)
    task_update(task['id'], progress_msg='处理中...')
    flood_seen = False

    async def process_single():
        nonlocal flood_seen
        try:
            return await process_one_link(
                ubot, uc, ci, di, lt, chat_id, uid, oc, comment_id,
                settings=settings,
            )
        except FloodWait as e:
            flood_seen = True
            secs = _flood_secs(e)
            task_update(task['id'], progress_msg=f'Telegram 限流，等待 {int(secs)}s...')
            raise

    try:
        task['result'] = await with_flood_retry(
            process_single,
            context=f'{ci}/{di}',
            max_retries=2,
        )
    except Exception as e:
        if flood_seen:
            raise
        task['result'] = f'❌ 错误：{str(e)[:100]}'

async def _run_merge(uid, task, doc, epoch):
    """Execute merge extraction and delivery."""
    settings = task['settings']
    links = task['links']
    oc = task.get('caption')
    chat_id = task['chat_id']
    ubot = await get_ubot(uid, prefetched=doc, prefetched_epoch=epoch)
    uc = await get_uclient(uid, prefetched=doc, prefetched_epoch=epoch)
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
            linked = await with_flood_retry(
                lambda: resolve_linked_chat(batch_fetch_client, channel),
                context=f'resolve linked chat {channel}',
                max_retries=2,
            )
            if not linked:
                continue
            flood_seen = False

            async def fetch_comments():
                nonlocal flood_seen
                try:
                    return await batch_fetch_client.get_messages(linked.id, cids)
                except FloodWait:
                    flood_seen = True
                    raise

            try:
                results = await with_flood_retry(
                    fetch_comments,
                    context=f'comment fetch {channel}',
                    max_retries=2,
                )
                if not isinstance(results, list):
                    results = [results]
                for msg in results:
                    if msg and not getattr(msg, 'empty', False):
                        batch_msgs[msg.id] = msg
                fetch_module.fetch_origin[(uid, linked.id)] = True
            except Exception as e:
                if flood_seen:
                    print(f'Retry batch fetch failed for {channel}: {e}')
                else:
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
            flood_seen = False

            async def fetch_message():
                nonlocal flood_seen
                try:
                    return await get_msg(ubot, uc, ci, di, lti, uid)
                except FloodWait:
                    flood_seen = True
                    raise

            try:
                msg = await with_flood_retry(
                    fetch_message,
                    context=f'get message {ci}/{di}',
                    max_retries=2,
                )
            except Exception:
                if not flood_seen:
                    raise
                msg = None
        if not msg:
            continue
        if getattr(msg, 'media_group_id', None):
            src_chat = msg.chat.id if getattr(msg, 'chat', None) else ci
            src_lt = 'private' if comment_id else lti
            fetch_client = (
                uc if (
                    uc and (
                        src_lt == 'private'
                        or fetch_module.fetch_origin.get((uid, src_chat), False)
                    )
                ) else ubot
            )
            flood_seen = False

            async def fetch_group():
                nonlocal flood_seen
                try:
                    return await fetch_client.get_media_group(src_chat, msg.id)
                except FloodWait:
                    flood_seen = True
                    raise

            try:
                group = await with_flood_retry(
                    fetch_group,
                    context=f'get media group {src_chat}/{msg.id}',
                    max_retries=2,
                )
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
    flood_seen = False

    async def merge_delivery():
        nonlocal flood_seen
        try:
            return await process_merged(
                ubot, uc, all_msgs, chat_id, uid, oc,
                settings=settings,
            )
        except FloodWait:
            flood_seen = True
            raise

    try:
        res = await with_flood_retry(
            merge_delivery,
            context=f'merge delivery {chat_id}',
            max_retries=2,
        )
    except Exception as e:
        if flood_seen:
            raise
        res = f'❌ 合并失败：{str(e)[:100]}'
    task['result'] = res

async def _run_batch_count(uid, task, doc, epoch):
    """Execute sequential batch extraction (start link + count).

    Phase 7: same pipeline window=1 + adaptive interval as
    ``_run_batch_links``; unlike link batches there is no album expansion
    and no FloodWait retry wrapper (preserved), a flood simply fails the
    item after the limiter records it.
    """
    settings = task['settings']
    ci = task['cid']
    sid = task['sid']
    lt = task['lt']
    n = task['num']
    oc = task.get('caption')
    chat_id = task['chat_id']
    ubot = await get_ubot(uid, prefetched=doc, prefetched_epoch=epoch)
    uc = await get_uclient(uid, prefetched=doc, prefetched_epoch=epoch)
    success = 0
    cancelled = False
    limiter = RateLimiter(base=BATCH_MIN_INTERVAL, ceiling=BATCH_INTERVAL)
    started_at = time.time()
    task_update(task['id'], progress_msg=f'批量提取 {n} 条...')

    async def prepare(idx):
        msg = await get_msg(ubot, uc, ci, int(sid) + idx, lt, uid)
        if not msg:
            return None, None
        return await prepare_msg(
            ubot, uc, msg, chat_id, lt, uid, ci, oc, settings=settings,
        )

    prefetch = None
    try:
        for j in range(n):
            if task_should_cancel(task['id']):
                cancelled = True
                task_update(task['id'], current=j, success=success, progress_msg=f'已取消（{j}/{n}）')
                break
            if prefetch is None:
                prefetch = asyncio.create_task(prepare(j))
            task_update(task['id'], current=j, success=success, progress_msg=f'正在提取 {j+1}/{n}...')
            current, prefetch = prefetch, None
            try:
                result, prepared = await current
                if j + 1 < n:
                    prefetch = asyncio.create_task(prepare(j + 1))
                if prepared is not None:
                    res = await finish_prepared_msg(prepared)
                else:
                    res = result
                if res is not None and _ok(res):
                    success += 1
                limiter.report_success()
            except FloodWait as e:
                limiter.report_flood(_flood_secs(e))
                print(f'Count batch {j+1}/{n} error: {e}')
            except Exception as e:
                print(f'Count batch {j+1}/{n} error: {e}')
            await limiter.wait()
    except asyncio.CancelledError:
        # External worker cancellation: drain the armed prefetch so its
        # downloaded file / progress message cannot outlive the runner.
        prepared = await _drain_prefetch(prefetch)
        if prepared is not None:
            await abort_prepared_msg(prepared)
        raise
    if cancelled and prefetch is not None:
        # In-flight preparation self-cleans on CancelledError; a completed
        # one is aborted explicitly.
        prepared = await _drain_prefetch(prefetch)
        if prepared is not None:
            await abort_prepared_msg(prepared)
    # Throughput-analysis signal: per-task wall time + terminal interval.
    logger.info(
        'batch_count done uid=%s links=%d success=%d cancelled=%s wall=%.1fs final_interval=%.1fs',
        uid, n, success, cancelled, time.time() - started_at, limiter.current,
    )
    if not cancelled:
        task_update(task['id'], current=n, success=success)
        task['result'] = f'✅ 批量提取完成：成功 {success}/{n}'
    else:
        task['result'] = f'已取消。成功：{success}/{n}'

async def _run_dl(uid, task):
    """Execute a queued /dl video download (yt-dlp / missav / getav).

    Routing and delivery live in plugins.ytdl.run_dl; this wrapper only
    owns queue lifecycle: cancellation before start, live progress, and a
    default result when the pipeline reported none (errors are already
    replied to the user inside the pipeline, which re-raises nothing)."""
    from plugins.ytdl import run_dl
    if task_should_cancel(task['id']):
        task['status'] = 'cancelled'
        task['result'] = '已取消。'
        return
    task_update(task['id'], progress_msg='准备下载...')
    try:
        await run_dl(task['message'], task['url'], bool(task.get('want_subtitle')),
                     task_id=task['id'], source_url=task.get('source_url'))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        task['result'] = f'❌ 错误：{str(e)[:100]}'
    if not task.get('result'):
        task['result'] = '✅ 下载完成'
    task_update(task['id'], current=1)

async def _run_adl(uid, task):
    """Execute a queued /adl audio extraction (same lifecycle as _run_dl)."""
    from plugins.ytdl import run_adl
    if task_should_cancel(task['id']):
        task['status'] = 'cancelled'
        task['result'] = '已取消。'
        return
    task_update(task['id'], progress_msg='准备提取音频...')
    try:
        await run_adl(task['message'], task['url'], task_id=task['id'])
    except asyncio.CancelledError:
        raise
    except Exception as e:
        task['result'] = f'❌ 错误：{str(e)[:100]}'
    if not task.get('result'):
        task['result'] = '✅ 音频提取完成'
    task_update(task['id'], current=1)

async def process_one_link(*args, **kwargs):
    from plugins.deliver import process_one_link as deliver_process_one_link
    return await deliver_process_one_link(*args, **kwargs)

async def prepare_one_link(*args, **kwargs):
    from plugins.deliver import prepare_one_link as deliver_prepare_one_link
    return await deliver_prepare_one_link(*args, **kwargs)

async def finish_one_link(*args, **kwargs):
    from plugins.deliver import finish_one_link as deliver_finish_one_link
    return await deliver_finish_one_link(*args, **kwargs)

async def prepare_msg(*args, **kwargs):
    from plugins.deliver import prepare_msg as deliver_prepare_msg
    return await deliver_prepare_msg(*args, **kwargs)

async def finish_prepared_msg(*args, **kwargs):
    from plugins.deliver import finish_prepared_msg as deliver_finish_prepared_msg
    return await deliver_finish_prepared_msg(*args, **kwargs)

async def abort_prepared_msg(*args, **kwargs):
    from plugins.deliver import abort_prepared_msg as deliver_abort_prepared_msg
    return await deliver_abort_prepared_msg(*args, **kwargs)

async def process_merged(*args, **kwargs):
    from plugins.deliver import process_merged as deliver_process_merged
    return await deliver_process_merged(*args, **kwargs)

async def process_msg(*args, **kwargs):
    from plugins.deliver import process_msg as deliver_process_msg
    return await deliver_process_msg(*args, **kwargs)

def _ok(result):
    from plugins.deliver import _ok as deliver_ok
    return deliver_ok(result)

def _flood_secs(error):
    from plugins.deliver import _flood_secs as deliver_flood_secs
    return deliver_flood_secs(error)

async def with_flood_retry(*args, **kwargs):
    from plugins.deliver import with_flood_retry as deliver_with_flood_retry
    return await deliver_with_flood_retry(*args, **kwargs)
