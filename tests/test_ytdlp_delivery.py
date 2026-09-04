"""Xvideos/generic-site delivery regression tests (2026-09-04 incident).

Contract under test:
  1. _finalize_and_upload routes the video to the RESOLVED delivery target
     via the resolved sender (settings channel -> LOG_GROUP -> chat), not
     straight to the requesting private chat.
  2. Progress messages stay in the requesting chat via the main bot.
  3. The >2GB split path receives (sender, target) too, with progress
     routed to the user's chat.
  4. build_ytdlp_caption renders the five-block layout from yt-dlp
     metadata: tags/categories as hashtags (deduped, capped), uploader/
     resolution/duration/size info line, graceful fallbacks, 1024 clamp.

Harness style follows tests/test_temp_lifecycle.py (stubbed deps, real
plugins.ytdl module under test).
"""

import asyncio
import importlib
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1]

os.environ.setdefault("MASTER_KEY", "ytdlp-test-master")
os.environ.setdefault("IV_KEY", "ytdlp-test-iv")


@pytest.fixture
def ytdl_env(monkeypatch, tmp_path):
    pyrogram = types.ModuleType("pyrogram")
    pyrogram.Client = object

    class _Filters:
        def command(self, *a, **k):
            return None

        def regex(self, *a, **k):
            return None

        def __getattr__(self, name):
            return None

    pyrogram.filters = _Filters()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)
    py_types = types.ModuleType("pyrogram.types")
    for name in ("InlineKeyboardButton", "InlineKeyboardMarkup",
                 "InputMediaPhoto", "InputMediaVideo"):
        setattr(py_types, name, type(name, (), {}))
    monkeypatch.setitem(sys.modules, "pyrogram.types", py_types)
    py_errors = types.ModuleType("pyrogram.errors")
    py_errors.FloodWait = type("FloodWait", (Exception,), {})
    monkeypatch.setitem(sys.modules, "pyrogram.errors", py_errors)

    config = types.ModuleType("config")
    config.INSTA_COOKIES = ""
    config.YT_COOKIES = ""
    config.MISSAV_MIRRORS = None
    config.GETAV_MIRRORS = None
    config.MISSAV_MAX_JOBS = 2
    config.MISSAV_SEGMENT_CONCURRENCY = 8
    config.PROGRESS_MIN_INTERVAL = 999
    config.LOG_GROUP = 0
    config.UPLOAD_INTERVAL = 0
    config.MERGE_INTERVAL = 0
    config.CHANNEL_INTERVAL = 0
    config.BATCH_INTERVAL = 0
    config.BATCH_MIN_INTERVAL = 0
    config.MAX_FLOOD_RETRIES = 1
    config.FREEMIUM_LIMIT = 0
    config.PREMIUM_LIMIT = 500
    config.BURN_CONCURRENCY = 1
    config.FFMPEG_BURN_THREADS = 0
    config.BURN_TIMEOUT_S = 0
    config.BURN_PRESET = "superfast"
    config.BURN_CRF = 19
    config.DISK_FREE_MIN_GB = 10.0
    monkeypatch.setitem(sys.modules, "config", config)

    shared_client = types.ModuleType("shared_client")
    shared_client.app = SimpleNamespace(name="main-bot")
    shared_client._WORKDIR = str(tmp_path)
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    utils_pkg = types.ModuleType("utils")
    utils_pkg.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils_pkg)

    func_stub = types.ModuleType("utils.func")

    def _task_dir(task_id, create=True):
        path = os.path.join(str(tmp_path), "downloads", f"task_{task_id}")
        if create:
            os.makedirs(path, exist_ok=True)
        return path

    func_stub.task_downloads_dir = _task_dir
    func_stub.get_video_metadata = None
    func_stub.screenshot = None
    func_stub.touch_file = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "utils.func", func_stub)

    shared_client = types.ModuleType("shared_client")

    class _FakeApp:
        name = "main-bot"

        def on_message(self, *args, **kwargs):
            def decorator(function):
                return function
            return decorator

        def on_callback_query(self, *args, **kwargs):
            def decorator(function):
                return function
            return decorator

    shared_client.app = _FakeApp()
    shared_client._WORKDIR = str(tmp_path)
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    # real missav module (build_ytdlp_caption needs _hashtag from it)
    sys.modules.pop("utils.missav", None)
    import importlib.util as ilu
    spec = ilu.spec_from_file_location("utils.missav", SRC / "utils" / "missav.py")
    missav = ilu.module_from_spec(spec)
    sys.modules["utils.missav"] = missav
    spec.loader.exec_module(missav)

    tasks_stub = types.ModuleType("plugins.tasks")
    tasks_stub.task_update = lambda *a, **k: None
    tasks_stub.register_sweep_hook = lambda hook: None
    monkeypatch.setitem(sys.modules, "plugins.tasks", tasks_stub)

    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.__path__ = [str(SRC / "plugins")]
    monkeypatch.setitem(sys.modules, "plugins", plugins_pkg)

    sys.modules.pop("plugins.ytdl", None)
    ytdl = importlib.import_module("plugins.ytdl")
    yield SimpleNamespace(ytdl=ytdl, tmp=tmp_path, main_bot=shared_client.app)
    sys.modules.pop("plugins.ytdl", None)
    sys.modules.pop("utils.missav", None)


# ── build_ytdlp_caption ──────────────────────────────────────────────────────

def test_caption_five_blocks_from_metadata(ytdl_env):
    cap = ytdl_env.ytdl.build_ytdlp_caption(
        {"uploader": "StudioX",
         "tags": ["amateur", "hd", "1080p", "amateur", "big tits"],
         "categories": ["teen"]},
        "My Video", height=1080, duration=754, filesize=129 * 1024 * 1024)
    lines = cap.split("\n")
    assert lines[0] == "**My Video**"
    assert lines[2] == "StudioX | 1080p | 12:34 | 129 MB"
    assert lines[4].startswith("标签：#amateur #hd #1080p #big_tits")
    assert lines[5] == "类别：#teen"


def test_caption_tag_cap_and_dedupe(ytdl_env):
    cap = ytdl_env.ytdl.build_ytdlp_caption(
        {"tags": [f"t{i}" for i in range(30)] + ["t1"]}, "T")
    tag_line = next(l for l in cap.split("\n") if l.startswith("标签："))
    tags = tag_line[len("标签："):].split()
    assert len(tags) == 10, cap
    assert len(set(tags)) == 10


def test_caption_falls_back_to_bold_title(ytdl_env):
    assert ytdl_env.ytdl.build_ytdlp_caption({}, "Solo") == "**Solo**"
    assert ytdl_env.ytdl.build_ytdlp_caption(None, "Solo") == "**Solo**"
    # long titles clamp at 500 like the legacy behavior
    cap = ytdl_env.ytdl.build_ytdlp_caption({}, "x" * 800)
    assert cap == "**" + "x" * 500 + "**"


def test_caption_clamps_to_telegram_limit(ytdl_env):
    cap = ytdl_env.ytdl.build_ytdlp_caption(
        {"tags": [f"tag{i}" for i in range(30)],
         "categories": [f"cat{i}" for i in range(30)]},
        "T" * 600)
    assert len(cap) <= 1024
    assert cap.startswith("**T")


# ── delivery routing ─────────────────────────────────────────────────────────

class _Recorder:
    def __init__(self, name="client"):
        self.name = name
        self.videos = []
        self.documents = []
        self.messages = []

    async def send_video(self, chat, **kwargs):
        self.videos.append((chat, kwargs))

    async def send_document(self, chat, **kwargs):
        self.documents.append((chat, kwargs))

    async def send_message(self, chat, text, **kwargs):
        self.messages.append((chat, text))

        class _Msg:
            async def delete(self):
                return None

        return _Msg()


def _message(user_id=42, chat_id=42):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=chat_id),
    )


def test_finalize_uploads_to_resolved_target_not_request_chat(ytdl_env, tmp_path):
    ytdl = ytdl_env.ytdl
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v" * 100)

    main_bot = _Recorder("main-bot")
    custom = _Recorder("custom-bot")

    async def fake_resolve(message):
        return -100999, custom

    async def fake_metadata(path):
        return {"duration": 61, "width": 1280, "height": 720}

    ytdl._resolve_delivery = fake_resolve
    ytdl.get_video_metadata = fake_metadata
    ytdl.app = main_bot
    ytdl.thumbnail = lambda uid: None
    ytdl.screenshot = None
    # no thumbnail url -> screenshot path: stub it out by giving thumbnail_url
    message = _message()

    async def scenario():
        await ytdl._finalize_and_upload(
            message, str(video), "Title",
            "http://example/t.jpg", None,
            extra_meta={"uploader": "U", "tags": ["a"]},
            task_id="t1",
        )

    def fake_dl_thumb(url, path):
        Path(path).write_bytes(b"j")
        return path

    ytdl.d_thumbnail = fake_dl_thumb
    asyncio.run(scenario())

    # video went to the resolved channel via the custom bot
    assert custom.videos and custom.videos[0][0] == -100999
    cap = custom.videos[0][1]["caption"]
    assert cap.startswith("**Title**") and "标签：#a" in cap
    # progress stayed in the requesting chat via the main bot
    assert any(c == 42 for c, _ in main_bot.messages)
    assert not main_bot.videos


def test_finalize_without_channel_target_behaves_as_before(ytdl_env, tmp_path):
    ytdl = ytdl_env.ytdl
    video = tmp_path / "v.mp4"
    video.write_bytes(b"v" * 100)
    main_bot = _Recorder("main-bot")

    async def fake_resolve(message):
        return message.chat.id, main_bot

    async def fake_metadata(path):
        return {"duration": 1, "width": 2, "height": 3}

    ytdl._resolve_delivery = fake_resolve
    ytdl.get_video_metadata = fake_metadata
    ytdl.app = main_bot

    async def fake_screenshot(path, duration, uid):
        return None

    ytdl.screenshot = fake_screenshot  # thumbnail_url None -> screenshot path
    ytdl.d_thumbnail = None

    async def scenario():
        await ytdl._finalize_and_upload(
            _message(), str(video), "Plain", None, None, task_id="t2")

    asyncio.run(scenario())
    assert main_bot.videos and main_bot.videos[0][0] == 42


def test_split_upload_routes_parts_and_progress_separately(ytdl_env, tmp_path):
    ytdl = ytdl_env.ytdl
    big = tmp_path / "big.mp4"
    big.write_bytes(b"x" * 10)

    uploader = _Recorder("uploader")
    progress = _Recorder("progress")

    async def fake_split_file(client, chat, path, caption):
        # emulate one part round
        edit = await progress.send_message(chat, "part...")
        await client.send_document(chat, document=path, caption=caption)

    # call the real function but stub the file-splitting? Simpler: verify
    # parameter routing by driving split_and_upload_file with a tiny file.
    async def scenario():
        await ytdl.split_and_upload_file(
            uploader, -100999, str(big), "cap",
            progress_client=progress, progress_chat=42,
        )

    asyncio.run(scenario())
    # part document went to the channel via the upload client
    assert uploader.documents and uploader.documents[0][0] == -100999
    # progress messages went to the user chat via the progress client
    assert progress.messages and progress.messages[0][0] == 42
    assert not progress.documents
