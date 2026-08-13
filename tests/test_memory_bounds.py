import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1]


class _Filter:
    def __and__(self, other):
        return self

    def __or__(self, other):
        return self

    def __invert__(self):
        return self


class _Filters:
    text = _Filter()
    private = _Filter()

    @staticmethod
    def command(*args, **kwargs):
        return _Filter()


class _FakeApp:
    def on_message(self, *args, **kwargs):
        def decorator(function):
            return function

        return decorator


@pytest.fixture
def batch_module(monkeypatch):
    pyrogram = types.ModuleType("pyrogram")

    class FakeClient:
        instances = []
        fail_start = False

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.stopped = False
            self.started = False
            FakeClient.instances.append(self)

        async def start(self):
            if self.fail_start:
                raise RuntimeError("start failed")
            self.started = True

        async def stop(self):
            self.stopped = True

    pyrogram.Client = FakeClient
    pyrogram.filters = _Filters()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)

    pyrogram_types = types.ModuleType("pyrogram.types")
    for name in (
        "Message",
        "InputMediaPhoto",
        "InputMediaVideo",
        "InputMediaDocument",
        "InputMediaAudio",
    ):
        setattr(pyrogram_types, name, object)
    monkeypatch.setitem(sys.modules, "pyrogram.types", pyrogram_types)

    pyrogram_errors = types.ModuleType("pyrogram.errors")
    pyrogram_errors.UserNotParticipant = type("UserNotParticipant", (Exception,), {})
    pyrogram_errors.FloodWait = type("FloodWait", (Exception,), {})
    monkeypatch.setitem(sys.modules, "pyrogram.errors", pyrogram_errors)

    config = types.ModuleType("config")
    config.API_ID = 123
    config.API_HASH = "hash"
    config.LOG_GROUP = 0
    config.STRING = None
    config.FORCE_SUB = 0
    config.FREEMIUM_LIMIT = 1
    config.PREMIUM_LIMIT = 10
    config.BATCH_INTERVAL = 0
    config.MERGE_INTERVAL = 0
    config.CHANNEL_INTERVAL = 0
    config.UPLOAD_INTERVAL = 0
    config.MAX_FLOOD_RETRIES = 1
    monkeypatch.setitem(sys.modules, "config", config)

    utils = types.ModuleType("utils")
    utils.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils)
    func = types.ModuleType("utils.func")

    async def get_user_data(uid):
        return None

    async def get_user_data_key(uid, key, default=None):
        return default

    func.get_user_data = get_user_data
    func.get_user_data_key = get_user_data_key
    func.screenshot = None
    func.thumbnail = None
    func.get_video_metadata = None
    func.ensure_audio_track = None
    func.VIDEO_EXTENSIONS = set()
    func.AUDIO_EXTENSIONS = set()
    func.touch_file = lambda *_a, **_k: None
    func.process_text_with_rules = None
    func.is_premium_user = None
    func.parse_link = lambda value: (None, None, None, None)
    async def get_user_settings(_uid):
        return {}

    func.get_user_settings = get_user_settings

    def filter_settings(doc):
        result = {
            "caption": "",
            "chat_id": None,
            "replacement_words": {},
            "delete_words": [],
            "rename_tag": "",
            "bot_token": None,
        }
        for key in result:
            if doc and key in doc:
                result[key] = doc[key]
        return result

    func.filter_settings = filter_settings
    func.cred_epoch = lambda _uid: 0
    func.prune_cred_epochs = lambda _active: None
    func.apply_text_rules = lambda text, _replacements, _delete_words: text
    monkeypatch.setitem(sys.modules, "utils.func", func)

    custom_filters = types.ModuleType("utils.custom_filters")
    custom_filters.login_in_progress = _Filter()
    monkeypatch.setitem(sys.modules, "utils.custom_filters", custom_filters)

    encrypt = types.ModuleType("utils.encrypt")
    encrypt.dcs = lambda value: value
    monkeypatch.setitem(sys.modules, "utils.encrypt", encrypt)

    shared_client = types.ModuleType("shared_client")
    shared_client.app = _FakeApp()
    shared_client.userbot = None
    shared_client._WORKDIR = "/persistent"
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    plugins = types.ModuleType("plugins")
    plugins.__path__ = [str(SRC / "plugins")]
    monkeypatch.setitem(sys.modules, "plugins", plugins)
    settings = types.ModuleType("plugins.settings")
    settings.rename_file = None

    async def _sweep_active_conversations():
        return None

    settings._sweep_active_conversations = _sweep_active_conversations
    monkeypatch.setitem(sys.modules, "plugins.settings", settings)
    start = types.ModuleType("plugins.start")

    async def subscribe(*args, **kwargs):
        return 0

    start.subscribe = subscribe
    monkeypatch.setitem(sys.modules, "plugins.start", start)

    for name in ("plugins.fetch", "plugins.tasks", "plugins.deliver", "plugins.batch"):
        sys.modules.pop(name, None)
    import importlib
    fetch_module = importlib.import_module("plugins.fetch")
    tasks_module = importlib.import_module("plugins.tasks")
    deliver_module = importlib.import_module("plugins.deliver")
    batch_module = importlib.import_module("plugins.batch")
    for worker in list(tasks_module.USER_WORKERS.values()):
        worker.cancel()
    fetch_module.user_bots.clear()
    fetch_module.user_clients.clear()
    fetch_module.fetch_origin.clear()
    fetch_module._CLIENT_LAST_USED.clear()
    fetch_module._PEER_CACHE.clear()
    fetch_module._LINKED_CHAT.clear()
    fetch_module._UB_EPOCH.clear()
    fetch_module._UC_EPOCH.clear()
    fetch_module._UB_UC_LOCKS.clear()
    deliver_module.progress_state.clear()
    batch_module.pending_flows.clear()
    batch_module._Z_TS.clear()
    tasks_module.TASKS.clear()
    tasks_module.USER_QUEUES.clear()
    tasks_module.USER_WORKERS.clear()

    class ModuleBundle:
        def __init__(self):
            object.__setattr__(self, "_modules", (
                fetch_module, tasks_module, deliver_module, batch_module
            ))

        def _owner(self, name):
            for candidate in self._modules:
                if hasattr(candidate, name):
                    return candidate
            return deliver_module

        def __getattr__(self, name):
            return getattr(self._owner(name), name)

        def __setattr__(self, name, value):
            if name == "_modules":
                object.__setattr__(self, name, value)
                return
            shared = {
                "get_user_data", "get_user_data_key", "get_ubot", "get_uclient",
                "get_msg", "resolve_linked_chat", "cred_epoch", "process_msg",
                "process_merged", "process_album", "process_one_link",
                "_ok", "_flood_secs",
            }
            if name in shared:
                for candidate in self._modules:
                    if hasattr(candidate, name):
                        setattr(candidate, name, value)
                return
            setattr(self._owner(name), name, value)

    return ModuleBundle(), FakeClient, func

@pytest.fixture
def login_module(monkeypatch):
    pyrogram = types.ModuleType("pyrogram")

    class FakeClient:
        instances = []

        def __init__(self, *args, **kwargs):
            self.disconnected = False
            self.args = args
            self.kwargs = kwargs
            FakeClient.instances.append(self)

        async def disconnect(self):
            self.disconnected = True

    pyrogram.Client = FakeClient
    pyrogram.filters = _Filters()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)

    pyrogram_types = types.ModuleType("pyrogram.types")
    pyrogram_types.Message = object
    monkeypatch.setitem(sys.modules, "pyrogram.types", pyrogram_types)
    pyrogram_errors = types.ModuleType("pyrogram.errors")
    for name in (
        "BadRequest",
        "SessionPasswordNeeded",
        "PhoneCodeInvalid",
        "PhoneCodeExpired",
        "MessageNotModified",
    ):
        setattr(pyrogram_errors, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "pyrogram.errors", pyrogram_errors)

    config = types.ModuleType("config")
    config.API_ID = 123
    config.API_HASH = "hash"
    monkeypatch.setitem(sys.modules, "config", config)
    shared_client = types.ModuleType("shared_client")
    shared_client.app = _FakeApp()
    shared_client._WORKDIR = "/tmp"
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    utils = types.ModuleType("utils")
    utils.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils)
    func = types.ModuleType("utils.func")

    async def ok(*args, **kwargs):
        return True

    func.save_user_session = ok
    func.get_user_data = lambda user_id: None
    func.remove_user_session = ok
    func.save_user_bot = ok
    func.remove_user_bot = ok
    monkeypatch.setitem(sys.modules, "utils.func", func)
    encrypt = types.ModuleType("utils.encrypt")
    encrypt.ecs = lambda value: value
    encrypt.dcs = lambda value: value
    monkeypatch.setitem(sys.modules, "utils.encrypt", encrypt)

    fetch = types.ModuleType("plugins.fetch")
    fetch.user_bots = {}
    fetch.user_clients = {}
    fetch.premium_userbot = None
    fetch._LOGIN_LOCKS = {}

    def _client_lock(user_id):
        lock = fetch._LOGIN_LOCKS.get(user_id)
        if lock is None:
            lock = fetch._LOGIN_LOCKS[user_id] = asyncio.Lock()
        return lock

    fetch._client_lock = _client_lock
    monkeypatch.setitem(sys.modules, "plugins.fetch", fetch)
    tasks = types.ModuleType("plugins.tasks")
    tasks._ensure_sweeper = lambda: None
    tasks.register_sweep_hook = lambda _hook: None
    monkeypatch.setitem(sys.modules, "plugins.tasks", tasks)

    steps = {}
    custom_filters = types.ModuleType("utils.custom_filters")
    custom_filters.login_in_progress = _Filter()

    def set_user_step(user_id, step=None):
        if step is None:
            steps.pop(user_id, None)
        else:
            steps[user_id] = step

    custom_filters.set_user_step = set_user_step
    custom_filters.get_user_step = lambda user_id: steps.get(user_id)
    custom_filters.user_steps = steps
    monkeypatch.setitem(sys.modules, "utils.custom_filters", custom_filters)
    import importlib
    sys.modules.pop("plugins.login", None)
    module = importlib.import_module("plugins.login")
    return module, steps, fetch, FakeClient


class _StoppedClient:
    def __init__(self):
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1


def _seed_idle_clients(module, count, now):
    clients = []
    for uid in range(count):
        bot = _StoppedClient()
        user_client = _StoppedClient()
        clients.extend((bot, user_client))
        module.user_bots[uid] = bot
        module.user_clients[uid] = user_client
        module._CLIENT_LAST_USED[uid] = now - 1900
    return clients


def test_sweep_evicts_idle_clients_and_stops_all(batch_module):
    module, _, _ = batch_module
    now = time.time()
    clients = _seed_idle_clients(module, 50, now)

    asyncio.run(module._sweep_once(now=now))

    assert not module.user_bots
    assert not module.user_clients
    assert not module._CLIENT_LAST_USED
    assert all(client.stop_calls == 1 for client in clients)


def test_evicted_bot_is_rebuilt_on_next_get(batch_module):
    module, fake_client, func = batch_module
    uid = 7
    old = _StoppedClient()
    module.user_bots[uid] = old
    now = time.time()
    module._CLIENT_LAST_USED[uid] = now - 1900

    asyncio.run(module._sweep_once(now=now))

    async def token(uid, key, default=None):
        return "12345:token-value-long-enough-123456"

    module.get_user_data_key = token
    rebuilt = asyncio.run(module.get_ubot(uid))
    assert rebuilt is not old
    assert module.user_bots[uid] is rebuilt
    assert old.stop_calls == 1
    assert fake_client.instances[-1].started


def test_running_task_prevents_client_eviction(batch_module):
    module, _, _ = batch_module
    uid = 11
    module.user_bots[uid] = _StoppedClient()
    module.user_clients[uid] = _StoppedClient()
    now = time.time()
    module._CLIENT_LAST_USED[uid] = now - 1900
    module.TASKS["running"] = {"uid": uid, "status": "running", "finished_at": None}

    asyncio.run(module._sweep_once(now=now))

    assert uid in module.user_bots and uid in module.user_clients


def test_queued_task_prevents_client_eviction(batch_module):
    module, _, _ = batch_module
    uid = 12
    module.user_bots[uid] = _StoppedClient()
    module.user_clients[uid] = _StoppedClient()
    module._CLIENT_LAST_USED[uid] = time.time() - 1900
    queue = asyncio.Queue()
    queue.put_nowait(object())
    module.USER_QUEUES[uid] = queue

    asyncio.run(module._sweep_once(now=time.time()))

    assert uid in module.user_bots and uid in module.user_clients


def test_eviction_cancels_worker_and_removes_queue(batch_module):
    module, _, _ = batch_module
    uid = 13
    module.user_bots[uid] = _StoppedClient()
    module._CLIENT_LAST_USED[uid] = time.time() - 1900

    async def scenario():
        worker = asyncio.create_task(asyncio.sleep(3600))
        module.USER_WORKERS[uid] = worker
        module.USER_QUEUES[uid] = asyncio.Queue()
        await module._sweep_once(now=time.time())
        return worker

    worker = asyncio.run(scenario())
    assert worker.cancelled()
    assert uid not in module.USER_WORKERS
    assert uid not in module.USER_QUEUES


def test_sweep_expires_only_old_finished_tasks(batch_module):
    module, _, _ = batch_module
    now = time.time()
    module.TASKS.update(
        expired={"uid": 1, "status": "done", "finished_at": now - 601},
        fresh={"uid": 1, "status": "failed", "finished_at": now - 599},
        queued={"uid": 1, "status": "queued", "finished_at": None},
        running={"uid": 1, "status": "running", "finished_at": None},
    )

    asyncio.run(module._sweep_once(now=now))

    assert "expired" not in module.TASKS
    assert {"fresh", "queued", "running"} <= module.TASKS.keys()


def test_create_task_caps_completed_history_per_user(batch_module):
    module, _, _ = batch_module
    module._MAX_TASKS_PER_USER = 20
    created = []
    for index in range(25):
        task = module.create_task(99, "single", 1)
        task["status"] = "done"
        task["finished_at"] = index
        task["created_at"] = index
        created.append(task["id"])

    remaining = [task for task in module.TASKS.values() if task["uid"] == 99]
    assert len(remaining) == 20
    assert {task["id"] for task in remaining} == set(created[5:])


def test_bounded_lru_evicts_oldest_and_refreshes_hits(batch_module):
    module, _, _ = batch_module
    for index in range(1000):
        module.fetch_origin[index] = index
    assert module.fetch_origin[0] == 0
    module.fetch_origin[1000] = 1000

    assert len(module.fetch_origin) == 1000
    assert 1 not in module.fetch_origin
    assert 0 in module.fetch_origin
    assert module.fetch_origin.get(0) == 0


def test_progress_sweep_expires_old_tuple_only(batch_module):
    module, _, _ = batch_module
    now = time.time()
    module.progress_state["old"] = (20, now - 3601)
    module.progress_state["fresh"] = (40, now - 3599)

    asyncio.run(module._sweep_once(now=now))

    assert "old" not in module.progress_state
    assert module.progress_state["fresh"] == (40, now - 3599)


def test_login_sweep_cleans_expired_cache_and_skips_locked(login_module):
    module, steps, _, fake_client = login_module
    now = time.monotonic()
    temp_client = fake_client("temp")
    module.login_cache[1] = {"created_at": now - module.LOGIN_TTL - 1, "temp_client": temp_client}
    locked = asyncio.Lock()
    module.login_locks[2] = locked
    module.login_cache[2] = {"created_at": now - module.LOGIN_TTL - 1}
    module.login_locks[3] = asyncio.Lock()

    async def scenario():
        await locked.acquire()
        await module._sweep_login_state()
        locked.release()

    asyncio.run(scenario())

    assert 1 not in module.login_cache
    assert temp_client.disconnected
    assert 2 in module.login_cache
    assert 3 not in module.login_locks
    assert 1 not in steps


def test_cached_client_access_touches_last_used(batch_module):
    module, _, _ = batch_module
    module._ensure_sweeper = lambda: None
    bot = object()
    user_client = object()
    module.user_bots[1] = bot
    module.user_clients[2] = user_client
    module._UB_EPOCH[1] = 0  # build-epoch tags match fixture cred_epoch == 0
    module._UC_EPOCH[2] = 0
    module._CLIENT_LAST_USED[1] = 0
    module._CLIENT_LAST_USED[2] = 0

    async def scenario():
        assert await module.get_ubot(1) is bot
        assert await module.get_uclient(2) is user_client

    asyncio.run(scenario())

    assert module._CLIENT_LAST_USED[1] > 0
    assert module._CLIENT_LAST_USED[2] > 0


def test_enqueue_and_sweep_race_keeps_queue_consistent(batch_module):
    module, _, _ = batch_module
    uid = 21
    module.user_bots[uid] = _StoppedClient()
    module._CLIENT_LAST_USED[uid] = time.time() - 1900

    async def parked_worker(user_id):
        await asyncio.sleep(3600)

    module._task_worker = parked_worker
    task = module.create_task(uid, "single", 1)

    async def scenario():
        lock = module._client_lock(uid)
        await lock.acquire()
        enqueue = asyncio.create_task(module.enqueue_task(uid, task))
        await asyncio.sleep(0)
        sweep = asyncio.create_task(module._sweep_once(now=time.time()))
        lock.release()
        await asyncio.gather(enqueue, sweep)

    asyncio.run(scenario())
    assert uid in module.USER_QUEUES
    assert task in list(module.USER_QUEUES[uid]._queue)
    worker = module.USER_WORKERS.pop(uid)
    worker.cancel()


def test_get_uclient_rebuilds_bot_after_eviction(batch_module):
    module, _, func = batch_module
    uid = 22
    old_bot = _StoppedClient()
    old_user_client = _StoppedClient()
    module.user_bots[uid] = old_bot
    module.user_clients[uid] = old_user_client
    now = time.time()
    module._CLIENT_LAST_USED[uid] = now - 1900

    async def data(user_id):
        return {"session_string": "session"}

    async def token(user_id, key, default=None):
        return "12345:token-value-long-enough-123456"

    module.get_user_data = data
    module.get_user_data_key = token
    asyncio.run(module._sweep_once(now=now))
    rebuilt = asyncio.run(module.get_uclient(uid))

    assert rebuilt is module.user_clients[uid]
    assert module.user_bots[uid] is not old_bot
    assert module.user_clients[uid] is not old_user_client


def test_orphan_client_timestamp_is_removed(batch_module):
    module, _, _ = batch_module
    uid = 23
    module._CLIENT_LAST_USED[uid] = time.time() - 1900

    asyncio.run(module._sweep_once(now=time.time()))

    assert uid not in module._CLIENT_LAST_USED


def test_orphan_client_lock_is_removed(batch_module):
    module, _, _ = batch_module
    uid = 24
    module._UB_UC_LOCKS[uid] = asyncio.Lock()

    asyncio.run(module._sweep_once(now=time.time()))

    assert uid not in module._UB_UC_LOCKS


def test_pending_flow_ttl_removes_stale_z_entry(batch_module):
    module, _, _ = batch_module
    now = time.time()
    module.pending_flows[25] = {"step": "start"}
    module._Z_TS[25] = now - module._Z_IDLE_TTL - 1

    asyncio.run(module._sweep_once(now=now))

    assert 25 not in module.pending_flows
    assert 25 not in module._Z_TS


def test_client_stop_timeout_does_not_block_sweep(batch_module, monkeypatch):
    module, _, _ = batch_module
    uid = 26
    started = asyncio.Event()

    class HungClient:
        async def stop(self):
            started.set()
            await asyncio.sleep(3600)

    module.user_bots[uid] = HungClient()
    module._CLIENT_LAST_USED[uid] = time.time() - 1900
    original_wait_for = module.asyncio.wait_for

    async def short_wait(awaitable, timeout):
        return await original_wait_for(awaitable, timeout=0.001)

    monkeypatch.setattr(module.asyncio, "wait_for", short_wait)

    async def scenario():
        await module._sweep_once(now=time.time())
        return started.is_set()

    assert asyncio.run(scenario())
    assert uid not in module.user_bots


def test_login_sweep_cleans_stale_user_step(login_module):
    module, steps, _, _ = login_module
    uid = 31
    steps[uid] = module.STEP_CODE
    module.login_step_times[uid] = time.monotonic() - module.LOGIN_TTL - 1

    asyncio.run(module._sweep_login_state())

    assert uid not in steps
    assert uid not in module.login_step_times


def test_login_sweep_cleans_fallback_client_lock(login_module):
    module, _, _, _ = login_module
    uid = 32
    module.login_locks[uid] = asyncio.Lock()

    asyncio.run(module._sweep_login_state())

    assert uid not in module.login_locks


def test_queued_status_also_prevents_client_eviction(batch_module):
    module, _, _ = batch_module
    uid = 40
    module.user_bots[uid] = _StoppedClient()
    module._CLIENT_LAST_USED[uid] = time.time() - 1900
    module.TASKS["queued-active"] = {
        "uid": uid,
        "status": "queued",
        "finished_at": None,
    }

    asyncio.run(module._sweep_once(now=time.time()))

    assert uid in module.user_bots


def test_sweep_prunes_completed_history_to_cap(batch_module):
    module, _, _ = batch_module
    module._MAX_TASKS_PER_USER = 2
    now = time.time()
    module.TASKS.update(
        first={"uid": 41, "status": "done", "created_at": 1, "finished_at": now},
        second={"uid": 41, "status": "failed", "created_at": 2, "finished_at": now},
        third={"uid": 41, "status": "cancelled", "created_at": 3, "finished_at": now},
    )

    asyncio.run(module._sweep_once(now=now))

    assert len([task for task in module.TASKS.values() if task["uid"] == 41]) == 2
    assert "first" not in module.TASKS


def test_enqueue_rejects_full_queue_and_removes_task(batch_module):
    module, _, _ = batch_module
    uid = 42
    module._MAX_QUEUE = 1
    queue = asyncio.Queue()
    queue.put_nowait(object())
    module.USER_QUEUES[uid] = queue
    task = module.create_task(uid, "single", 1)

    accepted = asyncio.run(module.enqueue_task(uid, task))

    assert accepted is False
    assert task["id"] not in module.TASKS
    assert queue.qsize() == 1


def test_login_entry_starts_batch_sweeper_defensively(login_module, monkeypatch):
    module, _, _, _ = login_module
    calls = []
    monkeypatch.setattr(module, "_ensure_sweeper", lambda: calls.append(True))

    class Message:
        from_user = types.SimpleNamespace(id=43)

        async def delete(self):
            return None

        async def reply(self, text):
            return types.SimpleNamespace()

    asyncio.run(module.login_command(None, Message()))

    assert calls


def test_register_sweep_hook_deduplicates_callbacks(batch_module):
    module, _, _ = batch_module

    async def hook():
        return None

    module._SWEEP_HOOKS.clear()
    module.register_sweep_hook(hook)
    module.register_sweep_hook(hook)

    assert module._SWEEP_HOOKS == [hook]


def test_concurrent_sweeps_stop_client_once(batch_module):
    module, _, _ = batch_module
    uid = 44

    class Client:
        def __init__(self):
            self.stop_calls = 0

        async def stop(self):
            self.stop_calls += 1
            await asyncio.sleep(0)

    client = Client()
    module.user_bots[uid] = client
    module._CLIENT_LAST_USED[uid] = time.time() - 1900

    async def scenario():
        now = time.time()
        await asyncio.gather(
            module._sweep_once(now=now),
            module._sweep_once(now=now),
        )

    asyncio.run(scenario())

    assert client.stop_calls == 1


def test_failed_client_lookup_does_not_create_last_used_orphan(batch_module):
    module, _, _ = batch_module
    module._ensure_sweeper = lambda: None

    assert asyncio.run(module.get_ubot(45)) is None
    assert asyncio.run(module.get_uclient(46)) is None
    assert 45 not in module._CLIENT_LAST_USED
    assert 46 not in module._CLIENT_LAST_USED


def test_orphan_worker_and_queue_without_clients_are_reclaimed(batch_module):
    module, _, _ = batch_module
    uid = 47

    async def scenario():
        worker = asyncio.create_task(asyncio.sleep(3600))
        module.USER_WORKERS[uid] = worker
        module.USER_QUEUES[uid] = asyncio.Queue()
        await module._sweep_once(now=time.time())
        return worker

    worker = asyncio.run(scenario())

    assert worker.cancelled()
    assert uid not in module.USER_WORKERS


def test_login_sweep_cleans_custom_filter_orphan_step(login_module):
    module, steps, _, _ = login_module
    uid = 48
    steps[uid] = module.STEP_CODE

    asyncio.run(module._sweep_login_state())

    assert uid not in steps


def test_active_conversation_sweeper_and_timestamp_refresh(monkeypatch):
    from tests.test_settings_routing import _load_settings_module

    module = _load_settings_module(monkeypatch)
    now = time.time()
    module.active_conversations[1] = {"type": "setchat", "message_id": 1, "ts": now - 901}
    module.active_conversations[2] = {"type": "setchat", "message_id": 2, "ts": now - 10}

    asyncio.run(module._sweep_active_conversations())

    assert 1 not in module.active_conversations
    assert 2 in module.active_conversations
    assert module.active_conversations[2]["ts"] == now - 10


def test_batch_registers_settings_sweeper_hook(batch_module):
    module, _, _ = batch_module
    assert any(
        getattr(hook, '__name__', None) == '_sweep_active_conversations'
        for hook in module._SWEEP_HOOKS
    )


def test_settings_conversation_starts_shared_sweeper(monkeypatch):
    from tests.test_settings_routing import _load_settings_module

    module = _load_settings_module(monkeypatch)
    calls = []
    monkeypatch.setattr(module, '_ensure_settings_sweeper', lambda: calls.append(True))

    class Message:
        id = 1

        async def reply_text(self, text):
            return self

    query = types.SimpleNamespace(message=Message())
    asyncio.run(module.start_conversation(query, 55, 'setchat', 'prompt'))

    assert calls == [True]
