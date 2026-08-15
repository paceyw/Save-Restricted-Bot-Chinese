"""Routing tests for the /dl handler's missav branch (issue #13).

Loads ``plugins.ytdl`` offline with stubbed heavy deps (yt_dlp, mutagen,
pyrogram, …) — exactly the convention of test_settings_routing.py — then
drives ``dl_handler`` with a fake message to prove missav URLs reach
``process_missav`` and non-missav URLs keep their existing routing.
"""

import asyncio
import importlib
import os
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
    if importlib.util.find_spec("aiofiles") is not None:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    else:
        for name in ("aiohttp", "aiofiles"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))



    pyrogram = types.ModuleType("pyrogram")
    pyrogram.filters = _Filters()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)

    pyrogram_types = types.ModuleType("pyrogram.types")
    pyrogram_types.InputMediaPhoto = object
    pyrogram_types.InputMediaVideo = object
    monkeypatch.setitem(sys.modules, "pyrogram.types", pyrogram_types)


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


# ─── album upload flow (issue #13: channel album + private-only notices) ──────

def test_split_video_parts_chunks_correctly(ytdl, monkeypatch, tmp_path):
    monkeypatch.setattr(ytdl, "_WORKDIR", str(tmp_path))
    monkeypatch.setattr(ytdl, "touch_file", lambda *a, **k: None)
    payload = os.urandom(10 * 1024 * 1024)
    src = tmp_path / "video.mp4"
    src.write_bytes(payload)

    parts = asyncio.run(ytdl._split_video_parts(str(src), part_size=4 * 1024 * 1024))

    assert [os.path.basename(p) for p in parts] == [
        "part000.mp4", "part001.mp4", "part002.mp4"]
    # byte-exact reassembly in order
    joined = b"".join(Path(p).read_bytes() for p in parts)
    assert joined == payload
    # parts live under _WORKDIR/tmp (sweeper-immune), never in downloads/
    parts_root = os.path.commonpath(parts)
    assert parts_root.startswith(str(tmp_path / "tmp"))
    assert "downloads" not in parts[0]


def test_build_album_group_caption_and_dims(ytdl, monkeypatch):
    captured = []

    class FakePhoto:
        def __init__(self, media, caption=None):
            self.kind, self.media, self.caption = "photo", media, caption
            captured.append(self)

    class FakeVideo:
        def __init__(self, media, caption=None, **kw):
            self.kind, self.media, self.caption, self.kw = "video", media, caption, kw
            captured.append(self)

    monkeypatch.setattr(ytdl, "InputMediaPhoto", FakePhoto)
    monkeypatch.setattr(ytdl, "InputMediaVideo", FakeVideo)

    g = ytdl._build_album_group("cover.jpg", ["v1.mp4"], "CAP", 100, 640, 360)
    assert [(x.kind, x.caption) for x in g] == [("photo", "CAP"), ("video", None)]
    assert g[1].kw["supports_streaming"] is True
    assert g[1].kw["width"] == 640 and g[1].kw["duration"] == 100

    captured.clear()
    g2 = ytdl._build_album_group(None, ["v1.mp4", "v2.mp4", "v3.mp4"], "CAP", 100, 640, 360)
    # no cover: caption rides the FIRST video; later parts carry no dims
    assert [(x.kind, x.caption) for x in g2] == [
        ("video", "CAP"), ("video", None), ("video", None)]
    assert g2[1].kw["width"] is None and g2[2].kw["duration"] is None


def test_upload_album_small_file_sends_only_album(ytdl, monkeypatch, tmp_path):
    sent = {"album": [], "notices": []}

    class FakeSender:
        async def send_media_group(self, chat, group):
            sent["album"].append((chat, group))

    class FakeApp:
        async def send_message(self, chat, text):
            sent["notices"].append((chat, text))

    monkeypatch.setattr(ytdl, "app", FakeApp())
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 2048)
    monkeypatch.setattr(
        ytdl, "_build_album_group",
        lambda cover, paths, cap, d, w, h: ["GROUP"])

    asyncio.run(ytdl._upload_missav_album(
        FakeSender(), -100, 42, str(video), "cover.jpg", "CAP", 1, 2, 3))

    assert sent["album"] == [(-100, ["GROUP"])]
    assert sent["notices"] == []  # nothing but the album reaches the channel


def test_upload_album_big_file_splits_with_private_notice(ytdl, monkeypatch, tmp_path):
    sent = {"album": [], "notices": []}

    class FakeSender:
        async def send_media_group(self, chat, group):
            sent["album"].append((chat, group))

    class FakeMsg:
        async def delete(self):
            pass

    class FakeApp:
        async def send_message(self, chat, text):
            sent["notices"].append((chat, text))
            return FakeMsg()

    monkeypatch.setattr(ytdl, "app", FakeApp())
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 2048)

    real_getsize = os.path.getsize
    monkeypatch.setattr(
        os.path, "getsize",
        lambda p: 3 * 1024 ** 3 if p == str(video) else real_getsize(p))

    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    part_paths = []
    for name in ("p0.mp4", "p1.mp4"):
        p = parts_dir / name
        p.write_bytes(b"")
        part_paths.append(str(p))

    async def fake_split(path):
        return part_paths

    monkeypatch.setattr(ytdl, "_split_video_parts", fake_split)
    monkeypatch.setattr(
        ytdl, "_build_album_group",
        lambda cover, paths, cap, d, w, h: ["GROUP"] * len(paths))

    asyncio.run(ytdl._upload_missav_album(
        FakeSender(), -100, 42, str(video), "cover.jpg", "CAP", 1, 2, 3))

    # notice went to the PRIVATE chat only, album to the channel
    assert sent["notices"] == [(42, "**__文件超过 2GB，正在分片...__**")]
    assert sent["album"] == [(-100, ["GROUP", "GROUP"])]
    # split temp dir cleaned up
    assert not parts_dir.exists()
