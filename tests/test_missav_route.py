"""Routing tests for the /dl handler's missav branch (issue #13).

Loads ``plugins.ytdl`` offline with stubbed heavy deps (yt_dlp, mutagen,
pyrogram, …) — exactly the convention of test_settings_routing.py — then
drives ``dl_handler`` with a fake message to prove missav URLs reach
``process_missav`` and non-missav URLs keep their existing routing.
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]


class _Filter:
    def __and__(self, other):
        return self

    def __invert__(self):
        return not self


class _Filters:
    @staticmethod
    def command(*args, **kwargs):
        return _Filter()


class _FakeApp:
    def on_message(self, *args, **kwargs):
        def decorator(function):
            return function

        return decorator


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.chat = types.SimpleNamespace(id=-100)
        self.from_user = types.SimpleNamespace(id=42)
        self.replies = []

    async def reply_text(self, text, *a, **kw):
        self.replies.append(text)
        return types.SimpleNamespace(deleted=False)

    async def delete(self):
        self.deleted = True


@pytest.fixture
def ytdl(monkeypatch):
    # stub external deps not needed for routing
    for name in ("yt_dlp", "mutagen", "mutagen.id3", "mutagen.mp3"):
        mod = types.ModuleType(name)
        if name == "mutagen.id3":
            for attr in ("ID3", "TIT2", "TPE1", "COMM", "APIC"):
                setattr(mod, attr, object)
        if name == "mutagen.mp3":
            mod.MP3 = object
        monkeypatch.setitem(sys.modules, name, mod)
    for name in ("aiohttp", "aiofiles"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))



    pyrogram = types.ModuleType("pyrogram")
    pyrogram.filters = _Filters()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)

    shared_client = types.ModuleType("shared_client")
    shared_client.app = _FakeApp()
    shared_client._WORKDIR = str(SRC)
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    utils = types.ModuleType("utils")
    utils.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils)
    func = types.ModuleType("utils.func")
    func.get_video_metadata = None
    func.screenshot = None
    func.touch_file = None
    monkeypatch.setitem(sys.modules, "utils.func", func)

    config = types.ModuleType("config")
    config.INSTA_COOKIES = ""
    config.YT_COOKIES = ""
    config.MISSAV_MIRRORS = None
    config.MISSAV_SEGMENT_CONCURRENCY = 8
    config.PROGRESS_MIN_INTERVAL = 3
    config.MISSAV_MAX_JOBS = 2
    monkeypatch.setitem(sys.modules, "config", config)

    plugins = types.ModuleType("plugins")
    plugins.__path__ = [str(SRC / "plugins")]
    monkeypatch.setitem(sys.modules, "plugins", plugins)

    sys.modules.pop("plugins.ytdl", None)
    return importlib.import_module("plugins.ytdl")


def _drive(ytdl, monkeypatch, text):
    calls = {"missav": [], "video": []}

    async def fake_missav(message, url, hosts):
        calls["missav"].append(url)

    async def fake_video(message, url, cookies, check_duration_and_size=False):
        calls["video"].append(url)

    monkeypatch.setattr(ytdl, "process_missav", fake_missav)
    monkeypatch.setattr(ytdl, "process_video", fake_video)
    msg = _FakeMessage(text)
    asyncio.run(ytdl.dl_handler(None, msg))
    return calls


def test_missav_url_routed_to_process_missav(ytdl, monkeypatch):
    calls = _drive(ytdl, monkeypatch, "/dl https://missav.ai/cn/sone-543-chinese-subtitle")
    assert calls["missav"] == ["https://missav.ai/cn/sone-543-chinese-subtitle"]
    assert calls["video"] == []


def test_missav_mirror_domain_routed(ytdl, monkeypatch):
    calls = _drive(ytdl, monkeypatch, "/dl https://missav.ws/sone-543")
    assert calls["missav"] == ["https://missav.ws/sone-543"]


def test_youtube_and_generic_routing_unchanged(ytdl, monkeypatch):
    calls = _drive(ytdl, monkeypatch, "/dl https://youtube.com/watch?v=x")
    assert calls["video"] == ["https://youtube.com/watch?v=x"]
    assert calls["missav"] == []
    calls = _drive(ytdl, monkeypatch, "/dl https://example.com/video")
    assert calls["video"] == ["https://example.com/video"]


def test_missav_category_url_falls_through_to_generic(ytdl, monkeypatch):
    # dm278/chinese-subtitle is a listing, not a video — must NOT hit missav
    calls = _drive(ytdl, monkeypatch, "/dl https://missav.ai/dm278/chinese-subtitle")
    assert calls["missav"] == []
    assert calls["video"] == ["https://missav.ai/dm278/chinese-subtitle"]


def test_concurrent_guard_fires_before_routing(ytdl, monkeypatch):
    ytdl.ongoing_downloads[42] = True
    try:
        msg = _FakeMessage("/dl https://missav.ai/sone-543")
        asyncio.run(ytdl.dl_handler(None, msg))
        assert any("正在进行" in r for r in msg.replies)
    finally:
        ytdl.ongoing_downloads.pop(42, None)
