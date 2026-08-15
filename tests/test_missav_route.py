"""Routing tests for the /dl handler's missav branch (issue #13).

Loads ``plugins.ytdl`` offline with stubbed heavy deps (yt_dlp, mutagen,
pyrogram, …) — exactly the convention of test_settings_routing.py — then
drives ``dl_handler`` with a fake message to prove missav URLs reach
``process_missav`` and non-missav URLs keep their existing routing.
"""

import asyncio
import importlib
import os
import shutil
import subprocess
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

def test_segment_times_targets_part_size(ytdl):
    # 3.6GB / 7200s -> 0.5MB/s -> 1.8GB target => 2 parts, split at 3600s
    t = ytdl._segment_times(int(3.6 * 1024**3), 7200, int(1.8 * 1024**3), 9)
    assert t == [3600.0]


def test_segment_times_caps_part_count(ytdl):
    # absurdly large file with tiny max_parts: evenly divided, no overflow
    t = ytdl._segment_times(int(36 * 1024**3), 72000, int(1.8 * 1024**3), 4)
    assert len(t) == 3 and all(d > 0 for d in t)


def test_segment_times_degenerate_inputs(ytdl):
    assert ytdl._segment_times(0, 100, 1, 9) == []
    assert ytdl._segment_times(100, 0, 1, 9) == []
    assert ytdl._segment_times(100, 100, 10 * 1024**3, 9) == []  # fits one part


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_split_video_parts_produces_playable_parts(ytdl, monkeypatch, tmp_path):
    monkeypatch.setattr(ytdl, "_WORKDIR", str(tmp_path))
    monkeypatch.setattr(ytdl, "touch_file", lambda *a, **k: None)
    src = tmp_path / "video.mp4"
    # 12s 320x240 test clip with audio, ~350kbps
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=12:size=320x240:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
         "-c:a", "aac", str(src)],
        check=True)

    # force ~4s parts: size/12 * 4 bytes
    part_size = max(int(os.path.getsize(src) / 12 * 4), 1)
    parts = asyncio.run(ytdl._split_video_parts(str(src), 12, part_size=part_size))

    assert 2 <= len(parts) <= 4
    assert [os.path.basename(p) for p in parts][0] == "part000.mp4"
    assert parts[0].startswith(str(tmp_path / "tmp"))
    # every part is a self-contained playable mp4: ffprobe returns a duration
    total = 0.0
    for p in parts:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", p], capture_output=True, text=True, check=True)
        d = float(probe.stdout.strip())
        assert d > 0
        total += d
    assert abs(total - 12) < 2.0  # keyframe snapping tolerance


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

    g = ytdl._build_album_group("cover.jpg", [("v1.mp4", 100)], "CAP", 640, 360)
    assert [(x.kind, x.caption) for x in g] == [("photo", "CAP"), ("video", None)]
    assert g[1].kw["supports_streaming"] is True
    assert g[1].kw["width"] == 640 and g[1].kw["duration"] == 100

    captured.clear()
    g2 = ytdl._build_album_group(
        None, [("v1.mp4", 100), ("v2.mp4", 98), ("v3.mp4", 97)], "CAP", 640, 360)
    # no cover: caption rides the FIRST video; each part carries its OWN
    # duration (ffmpeg segments are self-contained); dims only on part 0
    assert [(x.kind, x.caption) for x in g2] == [
        ("video", "CAP"), ("video", None), ("video", None)]
    assert g2[0].kw["width"] == 640 and g2[0].kw["duration"] == 100
    assert g2[1].kw["width"] == 0 and g2[2].kw["duration"] == 97


def test_upload_album_small_file_sends_only_album(ytdl, monkeypatch, tmp_path):
    sent = {"album": [], "notices": []}

    class FakeSender:
        async def send_media_group(self, chat, group, progress=None, progress_args=()):
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
    monkeypatch.setattr(
        ytdl, "_build_album_group",
        lambda cover, parts, cap, w, h: ["GROUP"])

    async def fake_probe(path):
        return {"duration": 3, "width": 1, "height": 1}

    monkeypatch.setattr(ytdl, "get_video_metadata", fake_probe)

    asyncio.run(ytdl._upload_missav_album(
        FakeSender(), -100, 42, str(video), "cover.jpg", "CAP", 1, 2, 3))

    assert sent["album"] == [(-100, ["GROUP"])]
    # only upload notices in the PRIVATE chat; channel got nothing textual
    assert sent["notices"] == [(42, "**__开始上传相册（1 项）...__**")]


def test_upload_album_big_file_splits_with_private_notice(ytdl, monkeypatch, tmp_path):
    sent = {"album": [], "notices": []}

    class FakeSender:
        async def send_media_group(self, chat, group, progress=None, progress_args=()):
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

    async def fake_split(path, duration):
        return part_paths

    async def fake_probe(path):
        return {"duration": 123, "width": 1, "height": 1}

    monkeypatch.setattr(ytdl, "_split_video_parts", fake_split)
    monkeypatch.setattr(ytdl, "get_video_metadata", fake_probe)
    monkeypatch.setattr(
        ytdl, "_build_album_group",
        lambda cover, parts, cap, w, h: ["GROUP"] * len(parts))

    asyncio.run(ytdl._upload_missav_album(
        FakeSender(), -100, 42, str(video), "cover.jpg", "CAP", 1, 2, 3))

    # every notice went to the PRIVATE chat only; album to the channel
    assert sent["notices"] == [
        (42, "**__文件超过 2GB，ffmpeg 关键帧分片中...__**"),
        (42, "**__开始上传相册（2 项）...__**"),
    ]
    assert sent["album"] == [(-100, ["GROUP", "GROUP"])]
    # split temp dir cleaned up
    assert not parts_dir.exists()
