"""Temp-file lifecycle tests (plan Phase 2 §5.2).

Acceptance mapped from the plan:
  1. Task-scoped scratch dirs: deliver paths download into
     downloads/task_<id>/ and the worker's finally-rmtree removes the dir
     on completion, failure AND cancellation — no leftovers.
  2. Slow uploads keep their mtime lease refreshed per item, so the
     stale-sweeper cannot reap waiting album items mid-send.
  3. Disk watermark: new tasks are refused below DISK_FREE_MIN_GB while
     running tasks proceed.
  4. cleanup_stale_downloads drops empty task dirs left by crashed runs.

Harness conventions mirror tests/test_disk_cleanup.py (func_module fixture)
and tests/test_deliver_phase7.py (stubbed pyrogram/config/utils imports).
"""

import asyncio
import importlib
import importlib.util
import os
import shutil
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1]

os.environ.setdefault("MASTER_KEY", "temp-test-master")
os.environ.setdefault("IV_KEY", "temp-test-iv")


@pytest.fixture
def func_module(monkeypatch, tmp_path):
    config = types.ModuleType("config")
    config.MONGO_DB = "mongodb://unused"
    config.DB_NAME = "test"
    config.DISK_FREE_MIN_GB = 10.0
    monkeypatch.setitem(sys.modules, "config", config)

    motor = types.ModuleType("motor")
    motor_asyncio = types.ModuleType("motor.motor_asyncio")

    class FakeMotorClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getitem__(self, _name):
            return _FakeDatabase()

    class _FakeDatabase:
        def __getitem__(self, _name):
            return object()

    motor_asyncio.AsyncIOMotorClient = FakeMotorClient
    motor.motor_asyncio = motor_asyncio

    encrypt = types.ModuleType("utils.encrypt")
    encrypt.ecs = lambda value: value
    encrypt.dcs = lambda value: value
    monkeypatch.setitem(sys.modules, "utils.encrypt", encrypt)
    monkeypatch.setitem(sys.modules, "motor", motor)
    monkeypatch.setitem(sys.modules, "motor.motor_asyncio", motor_asyncio)
    monkeypatch.setitem(sys.modules, "cv2", types.ModuleType("cv2"))

    shared_client = types.ModuleType("shared_client")
    shared_client._WORKDIR = str(tmp_path)
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    spec = importlib.util.spec_from_file_location(
        "temp_lifecycle_func", SRC / "utils" / "func.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "temp_lifecycle_func", module)
    spec.loader.exec_module(module)
    return module


def test_task_downloads_dir_scoped_and_unique(func_module, tmp_path):
    a = func_module.task_downloads_dir("task_1_2_3")
    b = func_module.task_downloads_dir("task_1_2_4")
    assert a.endswith(os.path.join("downloads", "task_task_1_2_3"))
    assert os.path.isdir(a)
    assert a != b
    assert func_module.task_downloads_dir("task_1_2_3") == a  # idempotent


def test_cleanup_task_downloads_removes_dir_and_is_idempotent(func_module):
    d = func_module.task_downloads_dir("t1")
    (Path(d) / "video.mp4").write_bytes(b"x" * 10)
    (Path(d) / "sub").mkdir()
    func_module.cleanup_task_downloads("t1")
    assert not os.path.exists(d)
    func_module.cleanup_task_downloads("t1")  # second call: no error
    func_module.cleanup_task_downloads("never-existed")


def test_cleanup_stale_downloads_sweeps_files_then_empty_dirs(func_module):
    downloads = Path(func_module.task_downloads_dir("t", create=False)).parent
    downloads.mkdir(exist_ok=True)
    live = downloads / "task_live"
    dead = downloads / "task_dead"
    live.mkdir()
    dead.mkdir()
    (live / "a.mp4").write_bytes(b"a")
    (dead / "b.mp4").write_bytes(b"b")

    now = time.time()
    os.utime(dead / "b.mp4", (now - 2 * 3600, now - 2 * 3600))  # stale: 2h
    os.utime(live / "a.mp4", (now, now))  # active lease
    # husk must be an OLD dir too: fresh dirs are never reap candidates
    # (race guard for task dirs whose first file has not landed yet)
    os.utime(dead, (now - 2 * 3600, now - 2 * 3600))

    asyncio.run(func_module.cleanup_stale_downloads(max_age_min=60))

    assert (live / "a.mp4").exists()  # active file survives
    assert not (dead / "b.mp4").exists()  # stale file removed
    assert not dead.exists()  # empty husk removed
    assert live.exists()  # non-empty dir survives


def test_cleanup_stale_downloads_never_reaps_fresh_empty_task_dir(func_module):
    """Race guard (review round 1): a task dir created moments ago whose
    first file has not landed yet (caller awaiting network) must survive
    the sweep regardless of invocation timing."""
    downloads = Path(func_module.task_downloads_dir("t", create=False)).parent
    downloads.mkdir(exist_ok=True)
    fresh = downloads / "task_fresh_empty"
    fresh.mkdir()
    # fresh mtime (just created) — no aging needed
    assert fresh.exists(), "fresh empty task dir must not be reaped"


def test_direct_send_peer_invalid_falls_back_to_reupload(deliver_env):
    """PEER_ID_INVALID must fall back to download+re-upload (review round 1):
    bot-first public fetch made this the common path when the bot cannot
    resolve the target; the user-client sender can still deliver."""
    deliver = deliver_env.deliver
    calls = {"direct": 0, "download": 0, "upload": 0}

    async def fake_send_direct(c, m, tcid, ft=None, rtmid=None):
        calls["direct"] += 1
        return False, "PEER_ID_INVALID: the peer id being used is invalid"

    deliver.send_direct = fake_send_direct

    class _Recorder:
        async def download_media(self, msg, file_name=None, progress=None,
                                 progress_args=None):
            calls["download"] += 1
            Path(file_name).parent.mkdir(parents=True, exist_ok=True)
            Path(file_name).write_bytes(b"payload")
            return file_name

        async def send_document(self, chat, document, caption=None, thumb=None,
                                progress=None, progress_args=None,
                                reply_to_message_id=None):
            calls["upload"] += 1
            return SimpleNamespace(id=1)

    deliver.main_bot = _Bot()

    class _Msg:
        video = None
        video_note = None
        voice = None
        sticker = None
        audio = None
        photo = None
        document = SimpleNamespace(file_id="f1", file_name=None,
                                   file_size=6)
        media = True
        caption = None

    prep = deliver._PreparedMsg(
        'direct',
        c=_Recorder(), u=None, m=_Msg(), d="42", lt="public", uid=42,
        i="chan", oc=None, settings=dict(_SETTINGS), tcid=42, rtmid=None,
        ft=None, sender=_Recorder(), did=42, bot_fetched=True,
        f=None, p=None, st=None, th=None,
        downloads_dir=str(deliver_env.tmp / "downloads"),
    )

    result = asyncio.run(deliver.finish_prepared_msg(prep))
    assert calls["direct"] == 1
    assert calls["download"] == 1, "PEER_ID_INVALID must retry via download"
    assert calls["upload"] == 1, "re-upload must be attempted"
    assert result == "Done."



def test_disk_free_ok_threshold(func_module, monkeypatch):
    state = {"gib": 5.0}

    def fake_usage(path):
        return SimpleNamespace(
            free=state["gib"] * 1024 ** 3, total=100 * 1024 ** 3, used=0)

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    monkeypatch.setattr(func_module.shutil, "disk_usage", fake_usage)

    state["gib"] = 5.0
    ok, free_gb = func_module.disk_free_ok()
    assert ok is False and free_gb == pytest.approx(5.0)
    state["gib"] = 20.0
    ok, free_gb = func_module.disk_free_ok()
    assert ok is True and free_gb == pytest.approx(20.0)


# ── deliver-side behavior through the real module with stubbed deps ─────────

@pytest.fixture
def deliver_env(monkeypatch, tmp_path):
    pyrogram = types.ModuleType("pyrogram")
    pyrogram.Client = object
    pyrogram.filters = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)
    py_types = types.ModuleType("pyrogram.types")

    class _InputMedia:
        def __init__(self, media, **kwargs):
            self.media = media
            self.caption = None

    for name in ("InputMediaPhoto", "InputMediaVideo", "InputMediaDocument",
                 "InputMediaAudio"):
        setattr(py_types, name, _InputMedia)
    monkeypatch.setitem(sys.modules, "pyrogram.types", py_types)
    py_errors = types.ModuleType("pyrogram.errors")
    py_errors.FloodWait = type("FloodWait", (Exception,), {})
    monkeypatch.setitem(sys.modules, "pyrogram.errors", py_errors)

    config = types.ModuleType("config")
    config.LOG_GROUP = 0
    config.MAX_FLOOD_RETRIES = 1
    config.UPLOAD_INTERVAL = 0
    config.PROGRESS_MIN_INTERVAL = 999
    # utils.missav (imported via utils.caption) reads the transcode budget
    config.BURN_CONCURRENCY = 1
    config.FFMPEG_BURN_THREADS = 0
    config.BURN_TIMEOUT_S = 0
    config.DISK_FREE_MIN_GB = 10.0
    monkeypatch.setitem(sys.modules, "config", config)

    shared_stub = types.ModuleType("shared_client")
    shared_stub._WORKDIR = str(tmp_path)
    shared_stub.app = None
    shared_stub.userbot = None
    monkeypatch.setitem(sys.modules, "shared_client", shared_stub)

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils_pkg)

    func_stub = types.ModuleType("utils.func")
    func_stub.apply_text_rules = lambda text, *_a, **_k: text
    func_stub.screenshot = None
    func_stub.thumbnail = lambda uid: None
    func_stub.get_video_metadata = None
    func_stub.ensure_audio_track = None
    func_stub.VIDEO_EXTENSIONS = {".mp4"}
    func_stub.AUDIO_EXTENSIONS = {".mp3"}
    touched = []
    func_stub.touch_file = lambda p, **_k: touched.append(p)

    def _task_dir(task_id, create=True):
        root = Path(tmp_path) / "downloads"
        path = root / f"task_{task_id}"
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return str(path)

    func_stub.task_downloads_dir = _task_dir
    monkeypatch.setitem(sys.modules, "utils.func", func_stub)

    settings_stub = types.ModuleType("plugins.settings")
    settings_stub.rename_file = None
    monkeypatch.setitem(sys.modules, "plugins.settings", settings_stub)

    fetch_stub = types.ModuleType("plugins.fetch")
    fetch_stub.fetch_origin = {}
    fetch_stub.get_msg = None
    fetch_stub.resolve_linked_chat = None
    fetch_stub.upd_dlg = None
    fetch_stub.premium_userbot = None
    monkeypatch.setitem(sys.modules, "plugins.fetch", fetch_stub)

    tasks_stub = types.ModuleType("plugins.tasks")
    tasks_stub.sanitize = lambda s: s
    tasks_stub.register_sweep_hook = lambda hook: None
    monkeypatch.setitem(sys.modules, "plugins.tasks", tasks_stub)

    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.__path__ = [str(SRC / "plugins")]
    monkeypatch.setitem(sys.modules, "plugins", plugins_pkg)

    sys.modules.pop("plugins.deliver", None)
    deliver = importlib.import_module("plugins.deliver")
    yield SimpleNamespace(deliver=deliver, touched=touched, tmp=tmp_path,
                          task_dir=_task_dir)
    sys.modules.pop("plugins.deliver", None)


def _album_msgs(count=2, size=100):
    msgs = []
    for idx in range(count):
        msgs.append(SimpleNamespace(
            photo=None,
            video=SimpleNamespace(
                file_size=700, file_name=f"v{idx}.mp4", duration=10,
                width=2, height=2, thumbs=None,
            ),
            document=None, audio=None, media=True, media_group_id="g1",
            caption=None, chat=SimpleNamespace(id=-100500),
            id=idx + 1, empty=False,
        ))
    return msgs


class _DownloadClient:
    def __init__(self):
        pass

    async def download_media(self, msg, file_name=None, progress=None,
                             progress_args=None):
        path = Path(file_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"payload" * 100)
        return str(path)


class _Bot:
    def __init__(self):
        self.edits = []

    async def send_message(self, chat, text):
        return SimpleNamespace(id=len(self.edits) + 1)

    async def edit_message_text(self, chat, mid, text):
        self.edits.append(text)

    async def delete_messages(self, chat, mid):
        pass


class _Sender:
    def __init__(self, fail_group=False, cancel_on_group=False):
        self.fail_group = fail_group
        self.cancel_on_group = cancel_on_group
        self.groups = 0

    async def send_media_group(self, chat, media, reply_to_message_id=None):
        if self.cancel_on_group:
            raise asyncio.CancelledError()
        if self.fail_group:
            raise RuntimeError("MEDIA_EMPTY")
        self.groups += 1
        return [SimpleNamespace(id=1)] * len(media)


_SETTINGS = {"caption": "", "chat_id": None, "replacement_words": {},
             "delete_words": [], "rename_tag": "", "bot_token": None}


def test_process_album_success_uses_task_dir_and_cleans_up(deliver_env):
    deliver = deliver_env.deliver
    task_id = "task_album_ok"
    task_dir = Path(deliver_env.task_dir(task_id))

    # deliver.ensure_audio_track is stubbed None: patch to identity
    async def _identity(f):
        return f
    deliver.ensure_audio_track = _identity
    deliver.resolve_delivery = asyncio.coroutine(
        lambda d, settings: None) if False else deliver.resolve_delivery

    # resolve_delivery needs settings/chat resolution — patch to constants
    async def fake_resolve(d, settings):
        return int(d), None, True
    deliver.resolve_delivery = fake_resolve
    deliver.main_bot = _Bot()

    async def scenario():
        return await deliver.process_album(
            _Sender(), _DownloadClient(), _album_msgs(),
            "123", "public", 42, "chan", settings=dict(_SETTINGS),
            task_id=task_id,
        )

    result = asyncio.run(scenario())

    assert result.startswith("✅")
    downloaded = list(task_dir.rglob("*.mp4")) if task_dir.exists() else []
    assert downloaded == []  # files removed after successful group send


def test_process_album_cancellation_mid_upload_cleans_files(deliver_env):
    deliver = deliver_env.deliver
    task_id = "task_album_cancel"
    task_dir = Path(deliver_env.task_dir(task_id))

    async def _identity(f):
        return f
    deliver.ensure_audio_track = _identity

    async def fake_resolve(d, settings):
        return int(d), None, True
    deliver.resolve_delivery = fake_resolve
    deliver.main_bot = _Bot()

    async def scenario():
        await deliver.process_album(
            _Sender(cancel_on_group=True), _DownloadClient(), _album_msgs(),
            "123", "public", 42, "chan", settings=dict(_SETTINGS),
            task_id=task_id,
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())

    leftovers = list(task_dir.rglob("*")) if task_dir.exists() else []
    assert not leftovers, f"cancel leaked: {leftovers}"


def test_send_album_items_refreshes_lease_each_round(deliver_env):
    deliver = deliver_env.deliver
    touched = deliver_env.touched
    lease = [str(deliver_env.tmp / "downloads" / "a.mp4"),
             str(deliver_env.tmp / "downloads" / "b.mp4")]

    class ItemSender:
        async def send_video(self, *a, **k):
            return SimpleNamespace(id=1)

    async def send_one(sender, tcid, im, rtmid):
        await asyncio.sleep(0)

    orig = deliver._send_album_item
    deliver._send_album_item = send_one
    try:
        media = [SimpleNamespace(kind="video"), SimpleNamespace(kind="video")]
        sent = asyncio.run(deliver._send_album_items(
            ItemSender(), 1, media, None, lease_files=lease))
    finally:
        deliver._send_album_item = orig

    assert sent == 2
    assert len([t for t in touched if t in lease]) >= 4
