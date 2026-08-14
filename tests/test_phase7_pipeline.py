import asyncio
import importlib
import sys
import time
import types
from pathlib import Path

import pytest

from tests.test_custom_bot_flow import batch_module


# Reference timing table for the headline acceptance test.  The runner also
# waits 0.01s after each link, so the measured pipelined run is expected to be
# roughly PIPELINED_REFERENCE_SECONDS + 0.10s while the old serial model is
# SERIAL_REFERENCE_SECONDS (before scheduler overhead).
PREP_T = 0.05
FIN_T = 0.05
PIPELINE_LINKS = 10
SERIAL_REFERENCE_SECONDS = PIPELINE_LINKS * (PREP_T + FIN_T)
PIPELINED_REFERENCE_SECONDS = PIPELINE_LINKS * max(PREP_T, FIN_T)


class FakePreparedLink:
    def __init__(self, marker, *, kind="single", prepared=None):
        self.kind = kind
        self.marker = marker
        self.prepared = prepared


class FakePrepared:
    def __init__(self, marker, *, kind="downloaded"):
        self.marker = marker
        self.kind = kind


def _links(count):
    """Use one-based link labels so failure/order assertions read naturally."""
    return [(index, index, "public", None) for index in range(1, count + 1)]


def _task(module, links, *, uid=42, task_id="phase7-task"):
    task = {
        "id": task_id,
        "uid": uid,
        "status": "running",
        "settings": {},
        "links": links,
        "chat_id": "destination",
        "caption": None,
        "current": 0,
        "success": 0,
        "cancel_requested": False,
        "result": "",
        "progress_msg": "",
    }
    module.TASKS[task_id] = task
    return task


def _patch_clients(module):
    async def no_op_client(*_args, **_kwargs):
        return None

    module.get_ubot = no_op_client
    module.get_uclient = no_op_client



class _PipelineMainBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.deleted = []
        self._next_id = 0

    async def send_message(self, chat_id, text=None):
        self._next_id += 1
        message = types.SimpleNamespace(id=self._next_id)
        self.sent.append((chat_id, text, message))
        return message

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    async def delete_messages(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _PipelineClient:
    def __init__(self, name):
        self.name = name
        self.downloads = []
        self.sends = []
        self.wait_for_download = None
        self.after_first_upload = None

    @staticmethod
    def _marker_from_path(path):
        try:
            return int(Path(path).read_text())
        except (FileNotFoundError, TypeError, ValueError):
            return None

    async def download_media(self, message, file_name=None, **_kwargs):
        started = time.monotonic()
        record = {
            "marker": message.marker,
            "path": str(file_name),
            "start": started,
            "end": None,
        }
        self.downloads.append(record)
        await asyncio.sleep(0.05)
        path = Path(file_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(message.marker))
        record["end"] = time.monotonic()
        if message.marker == 2 and self.wait_for_download is not None:
            self.wait_for_download.set()
        return str(path)

    async def _send_file(self, kind, path):
        marker = self._marker_from_path(path)
        started = time.monotonic()
        record = {
            "kind": kind,
            "marker": marker,
            "path": str(path),
            "start": started,
            "end": None,
        }
        self.sends.append(record)
        await asyncio.sleep(0.05)
        if marker == 1 and self.wait_for_download is not None:
            await self.wait_for_download.wait()
            # Let the completed preparation resume and finish before the
            # cancellation callback arms the runner's abort path.
            await asyncio.sleep(0)
        record["end"] = time.monotonic()
        if marker == 1 and self.after_first_upload is not None:
            self.after_first_upload()
        return types.SimpleNamespace(id=len(self.sends))

    async def send_photo(self, _chat_id, photo, **_kwargs):
        return await self._send_file("photo", photo)

    async def send_sticker(self, _chat_id, sticker, **_kwargs):
        return await self._send_file("sticker", sticker)

    async def send_video(self, _chat_id, video, **_kwargs):
        return await self._send_file("video", video)

    async def send_video_note(self, _chat_id, video_note, **_kwargs):
        return await self._send_file("video_note", video_note)

    async def send_voice(self, _chat_id, voice, **_kwargs):
        return await self._send_file("voice", voice)

    async def send_audio(self, _chat_id, audio, **_kwargs):
        return await self._send_file("audio", audio)

    async def send_document(self, _chat_id, document, **_kwargs):
        return await self._send_file("document", document)

    async def send_message(self, _chat_id, text=None, **_kwargs):
        now = time.monotonic()
        self.sends.append(
            {"kind": "message", "marker": text, "start": now, "end": now}
        )


def _pipeline_photo(marker, chat_id):
    return types.SimpleNamespace(
        marker=marker,
        id=marker,
        media=True,
        media_group_id=None,
        chat=types.SimpleNamespace(id=chat_id),
        caption=None,
        text=None,
        video=None,
        video_note=None,
        voice=None,
        sticker=None,
        audio=None,
        document=None,
        photo=types.SimpleNamespace(file_id=f"photo-{marker}"),
    )


def _pipeline_task(tasks, links, task_id):
    task = {
        "id": task_id,
        "uid": 42,
        "status": "running",
        "settings": {
            "caption": "",
            "chat_id": None,
            "replacement_words": {},
            "delete_words": [],
            "rename_tag": "",
        },
        "links": links,
        "chat_id": 42,
        "caption": None,
        "current": 0,
        "success": 0,
        "cancel_requested": False,
        "result": "",
        "progress_msg": "",
    }
    tasks.TASKS[task_id] = task
    return task


@pytest.fixture
def real_pipeline_env(monkeypatch, tmp_path):
    """Load deliver first, then tasks, so the lazy task wrappers share it."""
    for name in ("plugins.deliver", "plugins.tasks", "plugins.fetch"):
        sys.modules.pop(name, None)

    pyrogram = types.ModuleType("pyrogram")

    class FloodWait(Exception):
        def __init__(self, value=0):
            super().__init__(value)
            self.value = value

    pyrogram_errors = types.ModuleType("pyrogram.errors")
    pyrogram_errors.FloodWait = FloodWait
    pyrogram_types = types.ModuleType("pyrogram.types")
    pyrogram_types.InputMediaPhoto = type("InputMediaPhoto", (), {})
    pyrogram_types.InputMediaVideo = type("InputMediaVideo", (), {})
    pyrogram_types.InputMediaDocument = type("InputMediaDocument", (), {})
    pyrogram_types.InputMediaAudio = type("InputMediaAudio", (), {})
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)
    monkeypatch.setitem(sys.modules, "pyrogram.errors", pyrogram_errors)
    monkeypatch.setitem(sys.modules, "pyrogram.types", pyrogram_types)

    config = types.ModuleType("config")
    config.LOG_GROUP = 0
    config.MAX_FLOOD_RETRIES = 2
    config.UPLOAD_INTERVAL = 0
    config.PROGRESS_MIN_INTERVAL = 0.0
    config.BATCH_MIN_INTERVAL = 0.01
    config.BATCH_INTERVAL = 0.01
    config.CHANNEL_INTERVAL = 0.01
    config.MERGE_INTERVAL = 0.01
    monkeypatch.setitem(sys.modules, "config", config)

    workdir = tmp_path / "work"
    workdir.mkdir()
    main_bot = _PipelineMainBot()
    shared_client = types.ModuleType("shared_client")
    shared_client.app = main_bot
    shared_client._WORKDIR = str(workdir)
    shared_client.userbot = None
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    utils = types.ModuleType("utils")
    utils.__path__ = [str(Path(__file__).resolve().parents[1] / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils)
    func = types.ModuleType("utils.func")
    func.apply_text_rules = lambda text, _replacement, _delete: text
    func.screenshot = None
    func.thumbnail = lambda _destination: None
    func.get_video_metadata = None
    func.ensure_audio_track = None
    func.touch_file = lambda *_args, **_kwargs: None
    func.VIDEO_EXTENSIONS = set()
    func.AUDIO_EXTENSIONS = set()
    func.get_user_data = lambda _uid: None
    func.filter_settings = lambda doc: dict(doc or {})
    func.cred_epoch = lambda _uid: 0
    func.prune_cred_epochs = lambda _active: None
    monkeypatch.setitem(sys.modules, "utils.func", func)

    plugins = types.ModuleType("plugins")
    plugins.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins")]
    monkeypatch.setitem(sys.modules, "plugins", plugins)
    fetch = types.ModuleType("plugins.fetch")
    fetch.fetch_origin = {}
    fetch.get_msg = None
    fetch.get_ubot = None
    fetch.get_uclient = None
    fetch.resolve_linked_chat = None
    fetch.upd_dlg = None
    fetch.premium_userbot = None

    class _NoopLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    fetch._client_lock = lambda _uid: _NoopLock()
    monkeypatch.setitem(sys.modules, "plugins.fetch", fetch)
    plugins.fetch = fetch

    # deliver imports sanitize from plugins.tasks. Install the tiny import
    # stub first, then replace it with the real tasks module.
    tasks_stub = types.ModuleType("plugins.tasks")
    tasks_stub.sanitize = lambda value: value
    tasks_stub.register_sweep_hook = lambda _hook: None
    plugins.tasks = tasks_stub
    monkeypatch.setitem(sys.modules, "plugins.tasks", tasks_stub)
    deliver = importlib.import_module("plugins.deliver")
    sys.modules["plugins.deliver"] = deliver
    sys.modules.pop("plugins.tasks", None)
    if hasattr(plugins, "tasks"):
        del plugins.tasks
    tasks = importlib.import_module("plugins.tasks")
    plugins.tasks = tasks
    assert sys.modules["plugins.deliver"] is deliver


    ubot = _PipelineClient("ubot")
    uc = _PipelineClient("uc")

    async def get_ubot(*_args, **_kwargs):
        return ubot

    async def get_uclient(*_args, **_kwargs):
        return uc

    tasks.get_ubot = get_ubot
    tasks.get_uclient = get_uclient
    tasks.BATCH_MIN_INTERVAL = tasks.BATCH_INTERVAL = 0.01
    deliver.main_bot = main_bot
    deliver.thumbnail = lambda _destination: None
    deliver.progress_state.clear()
    tasks.TASKS.clear()
    deliver.fetch_origin.clear()

    try:
        yield types.SimpleNamespace(
            deliver=deliver,
            tasks=tasks,
            fetch=fetch,
            main_bot=main_bot,
            ubot=ubot,
            uc=uc,
            workdir=workdir,
        )
    finally:
        tasks.TASKS.clear()
        deliver.progress_state.clear()
        deliver.fetch_origin.clear()

def test_batch_links_pipeline_overlaps_and_beats_serial(batch_module):
    """Serial reference is 10*(0.05+0.05)=1.00s; pipelined reference is
    10*max(0.05, 0.05)=0.50s (plus ten 0.01s limiter waits).  The same fake
    prepare/finish operations are run strictly sequentially below as the old
    implementation reference, and both reference numbers are printed.
    """
    module, _fake_client = batch_module
    module.BATCH_MIN_INTERVAL = module.BATCH_INTERVAL = 0.01
    _patch_clients(module)

    prep_times = {}
    finish_times = {}

    async def fake_prepare(*args, **_kwargs):
        index = args[2]
        started = time.monotonic()
        await asyncio.sleep(PREP_T)
        ended = time.monotonic()
        prep_times[index] = (started, ended)
        return None, FakePreparedLink(index)

    async def fake_finish(prepared):
        started = time.monotonic()
        await asyncio.sleep(FIN_T)
        ended = time.monotonic()
        finish_times[prepared.marker] = (started, ended)
        return "Done."

    module.prepare_one_link = fake_prepare
    module.finish_one_link = fake_finish
    task = _task(module, _links(PIPELINE_LINKS))

    started = time.monotonic()
    asyncio.run(module._run_batch_links(42, task, {}, 0))
    pipeline_elapsed = time.monotonic() - started
    pipeline_prep_times = dict(prep_times)
    pipeline_finish_times = dict(finish_times)

    # This is the old runner's strictly serial prepare-then-finish model,
    # using exactly the same fakes and durations as the real runner above.
    async def serial_reference():
        for index in range(1, PIPELINE_LINKS + 1):
            _result, prepared = await fake_prepare(
                None,
                None,
                index,
                index,
                "public",
                "destination",
                42,
                None,
                None,
                settings={},
            )
            await fake_finish(prepared)

    started = time.monotonic()
    asyncio.run(serial_reference())
    serial_elapsed = time.monotonic() - started

    print(
        "phase7 timing reference: "
        f"serial={SERIAL_REFERENCE_SECONDS:.2f}s, "
        f"pipelined={PIPELINED_REFERENCE_SECONDS:.2f}s, "
        f"measured_pipeline={pipeline_elapsed:.3f}s, "
        f"measured_serial={serial_elapsed:.3f}s"
    )
    assert pipeline_elapsed < 0.85
    assert serial_elapsed > pipeline_elapsed + (
        SERIAL_REFERENCE_SECONDS - PIPELINED_REFERENCE_SECONDS
    ) * 0.20
    assert any(
        pipeline_prep_times[index + 1][0] < pipeline_finish_times[index][1]
        for index in range(1, PIPELINE_LINKS)
    )
    assert task["result"] == "✅ 批量提取完成：成功 10/10"


def test_batch_links_flood_wait_retries_and_recovers_limiter(batch_module):
    module, _fake_client = batch_module
    module.BATCH_MIN_INTERVAL = 0.001
    module.BATCH_INTERVAL = 0.05
    _patch_clients(module)
    attempts = {}
    limiter_events = {"flood": [], "success": []}
    real_limiter = module.RateLimiter

    class RecordingLimiter(real_limiter):
        def report_flood(self, seconds=None):
            before = self.current
            super().report_flood(seconds)
            limiter_events["flood"].append((seconds, before, self.current))

        def report_success(self):
            before = self.current
            super().report_success()
            limiter_events["success"].append((before, self.current))

    module.RateLimiter = RecordingLimiter

    def flood_wait():
        error = module.FloodWait()
        error.value = 0.01
        return error

    async def fake_prepare(*args, **_kwargs):
        index = args[2]
        attempts[index] = attempts.get(index, 0) + 1
        if index == 2 and attempts[index] == 1:
            raise flood_wait()
        return None, FakePreparedLink(index)

    async def fake_finish(prepared):
        return "Done."

    module.prepare_one_link = fake_prepare
    module.finish_one_link = fake_finish
    task = _task(module, _links(10), task_id="flood-retry")

    asyncio.run(module._run_batch_links(42, task, {}, 0))

    assert task["result"] == "✅ 批量提取完成：成功 10/10"
    assert attempts[2] == 2
    assert any(
        abs(seconds - 0.01) < 1e-9
        for seconds, _before, _after in limiter_events["flood"]
    )
    assert any(after > before for _seconds, before, after in limiter_events["flood"])
    assert any(
        after < before
        for before, after in limiter_events["success"]
        if before > module.BATCH_MIN_INTERVAL
    )
    assert limiter_events["success"]


def test_batch_links_flood_exhaustion_only_fails_one_link(batch_module):
    module, _fake_client = batch_module
    module.BATCH_MIN_INTERVAL = module.BATCH_INTERVAL = 0.01
    _patch_clients(module)
    finish_order = []
    flood_events = []
    real_limiter = module.RateLimiter

    class RecordingLimiter(real_limiter):
        def report_flood(self, seconds=None):
            flood_events.append(seconds)
            super().report_flood(seconds)

    module.RateLimiter = RecordingLimiter

    def flood_wait():
        error = module.FloodWait()
        error.value = 0.01
        return error

    async def fake_prepare(*args, **_kwargs):
        index = args[2]
        if index == 4:
            raise flood_wait()
        return None, FakePreparedLink(index)

    async def fake_finish(prepared):
        finish_order.append(prepared.marker)
        return "Done."

    module.prepare_one_link = fake_prepare
    module.finish_one_link = fake_finish
    task = _task(module, _links(5), task_id="flood-exhaustion")

    asyncio.run(module._run_batch_links(42, task, {}, 0))

    assert task["result"] == "✅ 批量提取完成：成功 4/5"
    assert flood_events
    assert finish_order == [1, 2, 3, 5]


def test_batch_links_cancel_aborts_completed_and_cancels_inflight_prefetch(
    batch_module,
):
    module, _fake_client = batch_module
    module.BATCH_MIN_INTERVAL = module.BATCH_INTERVAL = 0.01
    _patch_clients(module)
    aborted = []

    async def record_abort(prepared):
        aborted.append(prepared)

    module.abort_prepared_msg = record_abort

    async def completed_prepare(*args, **_kwargs):
        index = args[2]
        if index == 1:
            return None, FakePreparedLink(index, prepared=FakePrepared(index))
        return None, FakePreparedLink(index)

    async def completed_finish(prepared):
        if prepared.marker == 0:
            # Let the already-armed prefetch task finish before cancellation
            # is requested, exercising the explicit abort path.
            await asyncio.sleep(0)
            module.request_cancel_tasks(42)
        return "Done."

    module.prepare_one_link = completed_prepare
    module.finish_one_link = completed_finish
    completed_task = _task(
        module,
        [(index, index, "public", None) for index in range(5)],
        task_id="cancel-completed",
    )
    asyncio.run(module._run_batch_links(42, completed_task, {}, 0))

    assert completed_task["result"] == "已取消。成功：1/5"
    assert len(aborted) == 1
    assert aborted[0].marker == 1
    assert aborted[0].kind == "downloaded"

    blocked_started = None
    blocked_cancelled = False

    async def blocked_scenario():
        nonlocal blocked_started, blocked_cancelled
        blocked_started = asyncio.Event()
        blocked_never_fires = asyncio.Event()

        async def blocked_prepare(*args, **_kwargs):
            nonlocal blocked_cancelled
            index = args[2]
            if index == 1:
                blocked_started.set()
                try:
                    await blocked_never_fires.wait()
                except asyncio.CancelledError:
                    blocked_cancelled = True
                    raise
            return None, FakePreparedLink(index)

        async def blocked_finish(prepared):
            if prepared.marker == 0:
                await blocked_started.wait()
                module.request_cancel_tasks(42)
            return "Done."

        module.prepare_one_link = blocked_prepare
        module.finish_one_link = blocked_finish
        blocked_task = _task(
            module,
            [(index, index, "public", None) for index in range(5)],
            task_id="cancel-inflight",
        )
        await asyncio.wait_for(
            module._run_batch_links(42, blocked_task, {}, 0),
            timeout=1.0,
        )
        return blocked_task

    blocked_task = asyncio.run(blocked_scenario())
    assert blocked_cancelled
    assert blocked_task["result"] == "已取消。成功：1/5"


def test_batch_links_prepare_failure_rearms_next_link_serially(batch_module):
    module, _fake_client = batch_module
    module.BATCH_MIN_INTERVAL = module.BATCH_INTERVAL = 0.01
    _patch_clients(module)
    prepare_calls = []

    async def fake_prepare(*args, **_kwargs):
        index = args[2]
        prepare_calls.append(index)
        if index == 1:
            raise RuntimeError("prepare failed")
        return None, FakePreparedLink(index)

    async def fake_finish(_prepared):
        return "Done."

    module.prepare_one_link = fake_prepare
    module.finish_one_link = fake_finish
    task = _task(module, _links(2), task_id="prepare-failure")

    asyncio.run(module._run_batch_links(42, task, {}, 0))

    assert task["result"] == "✅ 批量提取完成：成功 1/2"
    assert prepare_calls == [1, 2]


def test_real_batch_links_pipeline_delivers_photos_and_cleans(real_pipeline_env):
    env = real_pipeline_env
    links = [
        (f"public-{index}", index, "public", None)
        for index in range(1, 3)
    ]
    messages = {
        index: _pipeline_photo(index, f"public-{index}")
        for index in range(1, 3)
    }

    async def fake_get_msg(_ubot, _uc, chat_id, message_id, _lt, uid, _comment_id=None):
        env.deliver.fetch_origin[(uid, chat_id)] = True
        return messages[int(message_id)]

    # prepare_one_link reads get_msg from deliver's namespace; tasks' copy is
    # patched too to make the wiring explicit for both lazy import surfaces.
    env.deliver.get_msg = fake_get_msg
    env.tasks.get_msg = fake_get_msg
    env.fetch.get_msg = fake_get_msg
    task = _pipeline_task(env.tasks, links, "real-pipeline")

    asyncio.run(env.tasks._run_batch_links(42, task, {}, 0))

    photo_sends = [send for send in env.uc.sends if send["kind"] == "photo"]
    assert [send["marker"] for send in photo_sends] == [1, 2]
    assert photo_sends[0]["start"] < photo_sends[1]["start"]
    downloads = {record["marker"]: record for record in env.uc.downloads}
    uploads = {record["marker"]: record for record in photo_sends}
    assert downloads[2]["start"] < uploads[1]["end"]
    assert task["result"] == "✅ 批量提取完成：成功 2/2"

    downloads_dir = env.workdir / "downloads"
    assert downloads_dir.exists()
    assert list(downloads_dir.iterdir()) == []
    sent_progress_ids = [message.id for _chat, _text, message in env.main_bot.sent]
    deleted_progress_ids = [message_id for _chat, message_id in env.main_bot.deleted]
    assert sent_progress_ids == deleted_progress_ids


def test_real_batch_links_cancel_aborts_completed_prefetch(real_pipeline_env):
    env = real_pipeline_env
    links = [
        (f"public-{index}", index, "public", None)
        for index in range(1, 3)
    ]
    messages = {
        index: _pipeline_photo(index, f"public-{index}")
        for index in range(1, 3)
    }

    async def fake_get_msg(_ubot, _uc, chat_id, message_id, _lt, uid, _comment_id=None):
        env.deliver.fetch_origin[(uid, chat_id)] = True
        return messages[int(message_id)]

    env.deliver.get_msg = fake_get_msg
    env.tasks.get_msg = fake_get_msg
    env.fetch.get_msg = fake_get_msg
    task = _pipeline_task(env.tasks, links, "real-pipeline-cancel")
    env.uc.after_first_upload = lambda: task.update(cancel_requested=True)

    async def run():
        env.uc.wait_for_download = asyncio.Event()
        await env.tasks._run_batch_links(42, task, {}, 0)

    asyncio.run(run())

    photo_sends = [send for send in env.uc.sends if send["kind"] == "photo"]
    assert [send["marker"] for send in photo_sends] == [1]
    assert task["result"].startswith("已取消")
    assert len(env.uc.downloads) == 2
    assert all(not Path(record["path"]).exists() for record in env.uc.downloads)
    downloads_dir = env.workdir / "downloads"
    assert downloads_dir.exists()
    assert list(downloads_dir.iterdir()) == []
    sent_progress_ids = [message.id for _chat, _text, message in env.main_bot.sent]
    deleted_progress_ids = [message_id for _chat, message_id in env.main_bot.deleted]
    assert sent_progress_ids == deleted_progress_ids
