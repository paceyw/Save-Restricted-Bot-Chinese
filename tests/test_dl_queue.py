"""Queue integration tests for /dl and /adl (issue: dl/adl join /tasks).

Loads the REAL plugins.tasks module (queue, worker, dispatch, runners)
against a stubbed plugins.ytdl, proving:
  - enqueue → worker → _run_dl/_run_adl → ytdl.run_dl/run_adl handoff with
    the live task_id,
  - pipeline progress calls surface in TASKS while the task runs,
  - cancellation before start short-circuits without touching the pipeline,
  - pipeline errors land in task['result'] instead of killing the worker,
  - the /tasks command renders dl/adl with friendly labels and live
    refreshes until every task is terminal.
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]


class _FakeUser:
    id = 42

class _FakeChat:
    id = -100

class _FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.chat = _FakeChat()
        self.from_user = _FakeUser()
        self.replies = []
        self.sent = []  # the editable message objects returned by reply_text

    async def reply_text(self, text, *a, **kw):
        editable = _FakeEditable(text)
        self.replies.append(text)
        self.sent.append(editable)
        return editable


class _FakeEditable:
    def __init__(self, text):
        self.text = text
        self.edits = []

    async def edit_text(self, text, *a, **kw):
        self.edits.append(text)

    async def delete(self):
        pass


@pytest.fixture
def queue_env(monkeypatch):
    """Real plugins.tasks + real plugins.batch against stubbed everything else."""
    for name in ("plugins.tasks", "plugins.ytdl", "plugins.batch",
                 "plugins.fetch", "plugins.start"):
        sys.modules.pop(name, None)

    pyrogram = types.ModuleType("pyrogram")

    class FloodWait(Exception):
        def __init__(self, value=0):
            super().__init__(value)
            self.value = value

    class _Filter:
        def __and__(self, other):
            return self

        def __invert__(self):
            return self

    class _Filters:
        @staticmethod
        def command(*args, **kwargs):
            return _Filter()

        text = _Filter()
        private = _Filter()

    pyrogram.filters = _Filters
    pyrogram_errors = types.ModuleType("pyrogram.errors")
    pyrogram_errors.FloodWait = FloodWait
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)
    monkeypatch.setitem(sys.modules, "pyrogram.errors", pyrogram_errors)

    config = types.ModuleType("config")
    config.BATCH_INTERVAL = 0.01
    config.BATCH_MIN_INTERVAL = 0.01
    config.CHANNEL_INTERVAL = 0.01
    config.MERGE_INTERVAL = 0.01
    config.FREEMIUM_LIMIT = 5
    config.PREMIUM_LIMIT = 10
    monkeypatch.setitem(sys.modules, "config", config)

    shared_client = types.ModuleType("shared_client")

    class _FakeBot:
        def on_message(self, *args, **kwargs):
            def decorator(fn):
                return fn

            return decorator

    shared_client.app = _FakeBot()
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    utils = types.ModuleType("utils")
    utils.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils)
    func = types.ModuleType("utils.func")
    func.get_user_data = lambda _uid: None
    func.filter_settings = lambda doc: dict(doc or {})
    func.cred_epoch = lambda _uid: 0
    func.prune_cred_epochs = lambda _active: None
    func.get_user_data_key = lambda _uid, _key, _default=None: _default
    func.is_premium_user = lambda _uid: False
    func.parse_link = lambda _text: (None, None, None, None)
    monkeypatch.setitem(sys.modules, "utils.func", func)

    custom_filters = types.ModuleType("utils.custom_filters")
    custom_filters.login_in_progress = _Filter()
    monkeypatch.setitem(sys.modules, "utils.custom_filters", custom_filters)

    plugins = types.ModuleType("plugins")
    plugins.__path__ = [str(SRC / "plugins")]
    monkeypatch.setitem(sys.modules, "plugins", plugins)

    fetch = types.ModuleType("plugins.fetch")
    fetch.get_ubot = None
    fetch.get_uclient = None
    fetch.get_msg = None
    fetch.resolve_linked_chat = None

    class _NoopLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    fetch._client_lock = lambda _uid: _NoopLock()
    monkeypatch.setitem(sys.modules, "plugins.fetch", fetch)
    plugins.fetch = fetch

    # ytdl stub: records the handoff and can simulate progress/errors/sleep.
    ytdl_stub = types.ModuleType("plugins.ytdl")
    state = {"calls": [], "run_dl": None, "run_adl": None}

    async def default_run_dl(message, url, want_subtitle=False, task_id=None,
                             source_url=None):
        from plugins import tasks as tasks_mod
        tasks_mod.task_update(task_id, progress_msg="下载中 42%")
        state["calls"].append(("dl", message, url, want_subtitle, task_id, source_url))

    async def default_run_adl(message, url, task_id=None):
        state["calls"].append(("adl", message, url, task_id))

    ytdl_stub.run_dl = default_run_dl
    ytdl_stub.run_adl = default_run_adl
    ytdl_stub.discard_getav_prompts = lambda uid=None: 0
    monkeypatch.setitem(sys.modules, "plugins.ytdl", ytdl_stub)
    plugins.ytdl = ytdl_stub

    tasks = importlib.import_module("plugins.tasks")
    plugins.tasks = tasks

    start = types.ModuleType("plugins.start")

    async def _subscribe(_c, _m):
        return 0

    start.subscribe = _subscribe
    monkeypatch.setitem(sys.modules, "plugins.start", start)
    plugins.start = start

    batch = importlib.import_module("plugins.batch")
    plugins.batch = batch

    yield state, tasks, batch

    for worker in list(tasks.USER_WORKERS.values()):
        worker.cancel()
    tasks.USER_WORKERS.clear()
    tasks.USER_QUEUES.clear()
    tasks.TASKS.clear()


def _wait_terminal(task, timeout=5.0):
    async def _run():
        deadline = asyncio.get_event_loop().time() + timeout
        while task["status"] not in ("done", "failed", "cancelled"):
            if asyncio.get_event_loop().time() > deadline:
                return
            await asyncio.sleep(0.01)

    asyncio.run(_run())


def _run(coro):
    return asyncio.run(coro)


def test_dl_task_dispatches_to_ytdl_with_task_id(queue_env):
    state, tasks, _batch = queue_env
    msg = _FakeMessage("/dl https://example.com/video")
    task = tasks.create_task(42, "dl", 1, url="https://example.com/video",
                             want_subtitle=False, message=msg)
    assert _run(tasks.enqueue_task(42, task)) is True
    _wait_terminal(task)
    assert task["status"] == "done"
    assert task["current"] == 1
    assert task["result"] == "✅ 下载完成"
    assert state["calls"] == [("dl", msg, "https://example.com/video", False, task["id"], None)]


def test_dl_task_pins_user_chosen_source(queue_env):
    """A version picked on the getav card rides the task to the pipeline."""
    state, tasks, _batch = queue_env
    msg = _FakeMessage("/dl https://getav.net/zh/videos/cjod-159")
    task = tasks.create_task(
        42, "dl", 1, url="https://getav.net/zh/videos/cjod-159",
        want_subtitle=False, message=msg,
        source_url="https://cdn/uc720")
    assert _run(tasks.enqueue_task(42, task)) is True
    _wait_terminal(task)
    assert task["status"] == "done"
    assert state["calls"][0][5] == "https://cdn/uc720"


def test_pipeline_progress_surfaces_in_tasks_entry(queue_env):
    state, tasks, _batch = queue_env

    async def slow_dl(message, url, want_subtitle=False, task_id=None, source_url=None):
        tasks.task_update(task_id, progress_msg="下载中 42%")
        await asyncio.sleep(0.2)

    state["calls"].clear()
    import plugins.ytdl as ytdl_stub
    ytdl_stub.run_dl = slow_dl

    async def scenario():
        msg = _FakeMessage("/dl https://example.com/video")
        task = tasks.create_task(42, "dl", 1, url="u", want_subtitle=False, message=msg)
        await tasks.enqueue_task(42, task)
        await asyncio.sleep(0.1)
        # mid-flight: the worker pushed the pipeline's progress line
        assert tasks.TASKS[task["id"]]["progress_msg"] == "下载中 42%"
        assert tasks.TASKS[task["id"]]["status"] == "running"
        deadline = asyncio.get_event_loop().time() + 5
        while task["status"] != "done":
            await asyncio.sleep(0.02)
            assert asyncio.get_event_loop().time() < deadline

    asyncio.run(scenario())


def test_adl_task_dispatches_to_ytdl(queue_env):
    state, tasks, _batch = queue_env
    msg = _FakeMessage("/adl https://youtube.com/watch?v=x")
    task = tasks.create_task(42, "adl", 1, url="https://youtube.com/watch?v=x", message=msg)
    assert _run(tasks.enqueue_task(42, task)) is True
    _wait_terminal(task)
    assert task["status"] == "done"
    assert task["result"] == "✅ 音频提取完成"
    assert state["calls"][0][0] == "adl"
    assert state["calls"][0][2] == "https://youtube.com/watch?v=x"


def test_cancelled_before_start_skips_pipeline(queue_env):
    state, tasks, _batch = queue_env
    msg = _FakeMessage("/dl https://example.com/video")
    task = tasks.create_task(42, "dl", 1, url="https://example.com/video",
                             want_subtitle=False, message=msg)
    task["cancel_requested"] = True
    assert _run(tasks.enqueue_task(42, task)) is True
    _wait_terminal(task)
    assert task["status"] == "cancelled"
    assert state["calls"] == []


def test_pipeline_error_lands_in_result_not_worker(queue_env):
    state, tasks, _batch = queue_env

    async def boom_dl(message, url, want_subtitle=False, task_id=None, source_url=None):
        raise ValueError("boom")

    import plugins.ytdl as ytdl_stub
    ytdl_stub.run_dl = boom_dl

    async def scenario():
        msg = _FakeMessage("/dl https://example.com/video")
        task = tasks.create_task(42, "dl", 1, url="u", want_subtitle=False, message=msg)
        await tasks.enqueue_task(42, task)
        deadline = asyncio.get_event_loop().time() + 5
        while task["status"] != "done":
            await asyncio.sleep(0.02)
            assert asyncio.get_event_loop().time() < deadline
        assert "❌ 错误：boom" in task["result"]

    asyncio.run(scenario())


def test_tasks_view_renders_dl_labels_and_live_refresh(queue_env):
    _state, tasks, batch = queue_env
    batch._TASKS_REFRESH_INTERVAL = 0.05

    async def slow_dl(message, url, want_subtitle=False, task_id=None, source_url=None):
        await asyncio.sleep(0.25)
        tasks.task_update(task_id, progress_msg="", result="✅ 上传完成")

    import plugins.ytdl as ytdl_stub
    ytdl_stub.run_dl = slow_dl

    async def scenario():
        msg = _FakeMessage("/dl u")
        task = tasks.create_task(42, "dl", 1, url="u", want_subtitle=False, message=msg)
        await tasks.enqueue_task(42, task)
        await asyncio.sleep(0.05)  # let the worker mark it running

        view_msg = _FakeMessage("/tasks")
        await batch.tasks_cmd(None, view_msg)
        live = view_msg.sent[-1]

        assert "视频下载" in live.edits[0] or "视频下载" in live.text
        assert len(live.edits) >= 2                       # live refresh happened
        assert "✅ 上传完成" in live.edits[-1]            # final state rendered
        assert batch._has_active_tasks([]) is False
        assert batch._has_active_tasks([{"status": "queued"}]) is True

        deadline = asyncio.get_event_loop().time() + 5
        while task["status"] != "done":
            await asyncio.sleep(0.02)
            assert asyncio.get_event_loop().time() < deadline

    asyncio.run(scenario())


def test_tasks_view_labels_adl(queue_env):
    _state, tasks, batch = queue_env

    async def scenario():
        task = tasks.create_task(42, "adl", 1, url="u", message=_FakeMessage())
        tasks.TASKS[task["id"]] = task
        task["status"] = "done"
        task["result"] = "✅ 音频上传完成"
        text = batch._render_tasks_view(42)
        assert "音频提取" in text
        assert "✅ 音频上传完成" in text

    asyncio.run(scenario())
