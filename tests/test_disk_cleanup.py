import asyncio
import importlib.util
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1]
DOCKER_DIR = SRC / "docker"


@pytest.fixture
def func_module(monkeypatch, tmp_path):
    config = types.ModuleType("config")
    config.MONGO_DB = "mongodb://unused"
    config.DB_NAME = "test"
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

    module_name = "phase1_disk_func"
    spec = importlib.util.spec_from_file_location(module_name, SRC / "utils" / "func.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_screenshot_writes_to_downloads_and_uses_unique_names(func_module, tmp_path, monkeypatch):
    class FakeProcess:
        async def communicate(self):
            return b"", b""

    async def fake_exec(*cmd, **_kwargs):
        Path(cmd[-2]).touch()
        return FakeProcess()

    monkeypatch.setattr(func_module.asyncio, "create_subprocess_exec", fake_exec)

    result = asyncio.run(func_module.screenshot("video.mp4", 10, "123"))
    result2 = asyncio.run(func_module.screenshot("video.mp4", 10, "123"))

    downloads = (tmp_path / "downloads").resolve()
    assert downloads.is_dir()
    assert Path(result).is_absolute()
    assert Path(result).parent == downloads
    assert Path(result).is_file()
    assert Path(result2).is_file()
    assert result2 != result
    assert "_123_" in Path(result).name
    assert "_123_" in Path(result2).name


def test_screenshot_keeps_existing_custom_thumbnail(func_module, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "123.jpg"
    existing.touch()
    downloads = tmp_path / "downloads"

    result = asyncio.run(func_module.screenshot("video.mp4", 10, "123"))

    assert result == "123.jpg"
    assert not downloads.exists()


def _set_age(path, age_minutes):
    timestamp = time.time() - age_minutes * 60
    os.utime(path, (timestamp, timestamp))


def test_cleanup_stale_downloads_removes_old_files_and_keeps_fresh(func_module, tmp_path):
    downloads = tmp_path / "downloads"
    nested = downloads / "nested"
    nested.mkdir(parents=True)
    old = downloads / "old.mp4"
    old_nested = nested / "old.part"
    fresh = downloads / "fresh.mp3"
    old.touch()
    old_nested.touch()
    fresh.touch()
    _set_age(old, 120)
    _set_age(old_nested, 120)

    asyncio.run(func_module.cleanup_stale_downloads(max_age_min=60))

    assert not old.exists()
    assert not old_nested.exists()
    assert fresh.exists()


def test_cleanup_stale_downloads_skips_symlinked_directory(func_module, tmp_path):
    target = tmp_path / "real-downloads"
    target.mkdir()
    old = target / "old.mp4"
    old.touch()
    _set_age(old, 120)
    (tmp_path / "downloads").symlink_to(target, target_is_directory=True)

    asyncio.run(func_module.cleanup_stale_downloads(max_age_min=60))

    assert old.exists()


def test_cleanup_stale_downloads_noops_when_directory_is_missing(func_module, tmp_path):
    asyncio.run(func_module.cleanup_stale_downloads(max_age_min=60))


@pytest.fixture
def batch_module(monkeypatch, tmp_path):
    class _Filter:
        def __and__(self, _other):
            return self

        def __or__(self, _other):
            return self

        def __invert__(self):
            return self

    class _Filters:
        text = _Filter()
        private = _Filter()

        @staticmethod
        def command(*_args, **_kwargs):
            return _Filter()

        @staticmethod
        def regex(_pattern):
            return _Filter()

    class _FakeApp:
        def on_message(self, *_args, **_kwargs):
            def decorator(function):
                return function

            return decorator

    pyrogram = types.ModuleType("pyrogram")
    pyrogram.Client = object
    pyrogram.filters = _Filters()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)

    pyrogram_types = types.ModuleType("pyrogram.types")
    pyrogram_types.Message = object
    pyrogram_types.InputMediaPhoto = object
    pyrogram_types.InputMediaVideo = object
    pyrogram_types.InputMediaDocument = object
    pyrogram_types.InputMediaAudio = object
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
    config.BATCH_INTERVAL = 0.01
    config.MERGE_INTERVAL = 0.01
    config.CHANNEL_INTERVAL = 0.01
    config.UPLOAD_INTERVAL = 0.01
    config.MAX_FLOOD_RETRIES = 1
    monkeypatch.setitem(sys.modules, "config", config)

    utils = types.ModuleType("utils")
    utils.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils)
    func = types.ModuleType("utils.func")
    func.get_user_data = None
    func.screenshot = None
    func.thumbnail = None
    func.get_video_metadata = None
    func.ensure_audio_track = None
    func.VIDEO_EXTENSIONS = set()
    func.AUDIO_EXTENSIONS = set()
    func.touch_file = lambda *_a, **_k: None
    func.get_user_data_key = None
    func.process_text_with_rules = None
    func.is_premium_user = None
    func.parse_link = None
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
    shared_client._WORKDIR = str(tmp_path)
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    plugins = types.ModuleType("plugins")
    plugins.__path__ = [str(SRC / "plugins")]
    monkeypatch.setitem(sys.modules, "plugins", plugins)
    settings = types.ModuleType("plugins.settings")
    settings.rename_file = None
    monkeypatch.setitem(sys.modules, "plugins.settings", settings)
    start = types.ModuleType("plugins.start")

    async def subscribe(*_args, **_kwargs):
        return 0

    start.subscribe = subscribe
    monkeypatch.setitem(sys.modules, "plugins.start", start)

    for name in ("plugins.fetch", "plugins.tasks", "plugins.deliver", "plugins.batch"):
        sys.modules.pop(name, None)
    import importlib
    importlib.import_module("plugins.fetch")
    importlib.import_module("plugins.tasks")
    deliver_module = importlib.import_module("plugins.deliver")
    importlib.import_module("plugins.batch")
    deliver_module.progress_state.clear()
    return deliver_module


def test_process_msg_thumbnail_cleanup_preserves_user_thumbnail(batch_module, tmp_path):
    downloads = (tmp_path / "downloads").resolve()
    downloads.mkdir()
    ephemeral = downloads / "2026-08-13_00-00-00.jpg"
    persistent = tmp_path / "123.jpg"
    ephemeral.touch()
    persistent.touch()

    batch_module._cleanup_downloaded_thumbnail(str(ephemeral), str(downloads))
    batch_module._cleanup_downloaded_thumbnail(str(persistent), str(downloads))

    assert not ephemeral.exists()
    assert persistent.exists()


def test_cleanup_runtime_script_handles_downloads_and_user_thumbnail(tmp_path):
    runtime = tmp_path / "runtime"
    downloads = runtime / "downloads" / "nested"
    downloads.mkdir(parents=True)

    old_media = downloads / "foo.mp4"
    fresh_media = downloads / "bar.mp4"
    old_temp = downloads / "x.temp"
    old_part = downloads / "y.part123"
    old_album_thumb = downloads / "album_thumb_42_1700000000_0.jpg"
    old_unknown = downloads / "orphan.bin"
    old_media.touch()
    fresh_media.touch()
    old_temp.touch()
    old_part.touch()
    old_album_thumb.touch()
    old_unknown.touch()
    for path in (old_media, old_temp, old_part, old_album_thumb, old_unknown):
        _set_age(path, 300)

    user_thumbnail = runtime / "123456.jpg"
    old_root_screenshot = runtime / "2026-01-01_00-00-00.jpg"
    user_thumbnail.touch()
    old_root_screenshot.touch()
    _set_age(user_thumbnail, 300)
    _set_age(old_root_screenshot, 11000)

    result = subprocess.run(
        ["bash", str(DOCKER_DIR / "cleanup-runtime.sh"), str(runtime)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0

    assert not old_media.exists()
    assert fresh_media.exists()
    assert not old_temp.exists()
    assert not old_part.exists()
    assert not old_album_thumb.exists()
    assert not old_unknown.exists()
    assert user_thumbnail.exists()
    assert not old_root_screenshot.exists()


def test_touch_file_refreshes_mtime_and_throttles(func_module, tmp_path):
    target = tmp_path / "big.mp4"
    target.write_bytes(b"x")
    _set_age(target, 300)
    before = os.path.getmtime(target)

    func_module._touch_last.clear()
    func_module.touch_file(str(target))
    assert os.path.getmtime(target) > before

    # Throttled: an immediate second call must not refresh again.
    _set_age(target, 300)
    aged = os.path.getmtime(target)
    func_module.touch_file(str(target))
    assert os.path.getmtime(target) == aged


def test_touch_file_tolerates_missing_and_empty(func_module):
    func_module._touch_last.clear()
    func_module.touch_file(None)
    func_module.touch_file("")
    func_module.touch_file("/nonexistent/dir/x.mp4")


def test_prog_heartbeat_touches_upload_source(batch_module, monkeypatch):
    module = batch_module
    touched = []
    monkeypatch.setattr(module, "touch_file", touched.append)
    module.progress_state.clear()

    class _FakeClient:
        async def edit_message_text(self, *_args, **_kwargs):
            return None

    asyncio.run(module.prog(50, 100, _FakeClient(), 1, 999, time.time(), fp="/data/downloads/f.mp4"))
    assert touched == ["/data/downloads/f.mp4"]

    # fp=None (download path) must not touch anything.
    asyncio.run(module.prog(50, 100, _FakeClient(), 1, 998, time.time()))
    assert touched == ["/data/downloads/f.mp4"]
