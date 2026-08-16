"""Routing + queue-enqueue tests for the /dl and /adl handlers.

Loads plugins.ytdl against stubbed heavy deps (yt_dlp, mutagen,
pyrogram, …) — exactly the convention of test_settings_routing.py — then
drives ``run_dl`` (the queue worker's site router) with a fake message to
prove missav/getav/generic URLs reach the right pipeline, and drives the
handlers themselves to prove /dl and /adl now land in the shared task
queue instead of running inline.
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

    @staticmethod
    def regex(pattern):
        import re as _re
        return _PatternFilter(_re.compile(pattern))

    text = _Filter()
    private = _Filter()


class _PatternFilter:
    def __init__(self, compiled):
        self.compiled = compiled

    def __and__(self, other):
        return self


class _Match:
    def __init__(self, groups):
        self._groups = groups

    def group(self, i):
        return self._groups[i]


class _FakeQuery:
    """Stands in for pyrogram CallbackQuery in callback tests."""
    def __init__(self, uid, data, pattern):
        m = _Match(["", *pattern.match(data).groups()]) if pattern.match(data) else None
        self.from_user = types.SimpleNamespace(id=uid)
        self.data = data
        self.matches = [m] if m else []
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_text(self, text, *a, **kw):
        self.edits.append(text)


class _FakeApp:
    def on_message(self, *args, **kwargs):
        def decorator(function):
            return function

        return decorator

    def on_callback_query(self, *args, **kwargs):
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
    pyrogram_types.InlineKeyboardButton = lambda text, callback_data=None: \
        types.SimpleNamespace(text=text, callback_data=callback_data)
    pyrogram_types.InlineKeyboardMarkup = lambda buttons: \
        types.SimpleNamespace(buttons=buttons)
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
    config.GETAV_MIRRORS = None
    config.MISSAV_SEGMENT_CONCURRENCY = 8
    config.PROGRESS_MIN_INTERVAL = 3
    config.MISSAV_MAX_JOBS = 2
    monkeypatch.setitem(sys.modules, "config", config)

    plugins = types.ModuleType("plugins")
    plugins.__path__ = [str(SRC / "plugins")]
    monkeypatch.setitem(sys.modules, "plugins", plugins)

    # Queue stub: dl/adl handlers import queue glue at call time; routing
    # tests never need the real per-user worker machinery.
    tasks_stub = types.ModuleType("plugins.tasks")
    tasks_stub._MAX_QUEUE = 3
    tasks_stub.task_update = lambda *_args, **_kwargs: None
    def _create_task(uid, task_type, total, **params):
        return {"id": f"task_{uid}", "uid": uid, "type": task_type,
                "total": total, "status": "queued", **params}
    tasks_stub.create_task = _create_task
    async def _enqueue_task(uid, task):
        return True
    tasks_stub.enqueue_task = _enqueue_task
    tasks_stub.get_queue_size = lambda uid: 0
    monkeypatch.setitem(sys.modules, "plugins.tasks", tasks_stub)

    sys.modules.pop("plugins.ytdl", None)
    return importlib.import_module("plugins.ytdl")


def _drive(ytdl, monkeypatch, text):
    """Drive run_dl (the queue worker's site router) with a /dl command text."""
    calls = {"missav": [], "getav": [], "video": []}

    async def fake_missav(message, url, hosts, task_id=None):
        calls["missav"].append(url)

    async def fake_getav(message, url, hosts, want_subtitle=False, task_id=None,
                         source_url=None):
        calls["getav"].append((url, want_subtitle))

    async def fake_video(message, url, cookies, check_duration_and_size=False, task_id=None):
        calls["video"].append(url)

    monkeypatch.setattr(ytdl, "process_missav", fake_missav)
    monkeypatch.setattr(ytdl, "process_getav", fake_getav)
    monkeypatch.setattr(ytdl, "process_video", fake_video)
    parts = text.split()
    want_sub = "-sub" in parts[1:]
    url = next((p for p in parts[1:] if p != "-sub"), "")
    msg = _FakeMessage(text)
    asyncio.run(ytdl.run_dl(msg, url, want_sub))
    return calls


def test_missav_url_routed_to_process_missav(ytdl, monkeypatch):
    calls = _drive(ytdl, monkeypatch, "/dl https://missav.ai/cn/sone-543-chinese-subtitle")
    assert calls["missav"] == ["https://missav.ai/cn/sone-543-chinese-subtitle"]
    assert calls["video"] == []


def test_missav_mirror_domain_routed(ytdl, monkeypatch):
    calls = _drive(ytdl, monkeypatch, "/dl https://missav.ws/sone-543")
    assert calls["missav"] == ["https://missav.ws/sone-543"]



def test_getav_url_routed_to_process_getav(ytdl, monkeypatch):
    calls = _drive(ytdl, monkeypatch, "/dl https://getav.net/zh/videos/cjod-159")
    assert calls["getav"] == [("https://getav.net/zh/videos/cjod-159", False)]
    assert calls["missav"] == []
    assert calls["video"] == []


def test_getav_sub_flag_opts_into_subtitle(ytdl, monkeypatch):
    # flag before the link
    calls = _drive(ytdl, monkeypatch, "/dl -sub https://getav.net/zh/videos/cjod-159")
    assert calls["getav"] == [("https://getav.net/zh/videos/cjod-159", True)]
    # flag after the link
    calls = _drive(ytdl, monkeypatch, "/dl https://getav.net/zh/videos/cjod-159 -sub")
    assert calls["getav"] == [("https://getav.net/zh/videos/cjod-159", True)]


def test_sub_flag_ignored_for_non_getav(ytdl, monkeypatch):
    calls = _drive(ytdl, monkeypatch, "/dl -sub https://youtube.com/watch?v=x")
    assert calls["getav"] == []
    assert calls["video"] == ["https://youtube.com/watch?v=x"]


def test_getav_locale_less_and_www_routed(ytdl, monkeypatch):
    calls = _drive(ytdl, monkeypatch, "/dl https://getav.net/videos/cjod-159")
    assert calls["getav"] == [("https://getav.net/videos/cjod-159", False)]
    calls = _drive(ytdl, monkeypatch, "/dl https://www.getav.net/en/videos/fc2-ppv-1234567")
    assert calls["getav"] == [("https://www.getav.net/en/videos/fc2-ppv-1234567", False)]


def test_getav_listing_urls_fall_through_to_generic(ytdl, monkeypatch):
    # /zh/videos is a listing; /zh/hot likewise; missav-style slug is not getav
    for url in ("https://getav.net/zh/videos", "https://getav.net/zh/hot",
                "https://getav.net/zh/cjod-159"):
        calls = _drive(ytdl, monkeypatch, f"/dl {url}")
        assert calls["getav"] == [], url
        assert calls["video"] == [url]


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


def _queue_state(monkeypatch):
    """Capture handler→queue interactions via the plugins.tasks stub."""
    stub = sys.modules["plugins.tasks"]
    state = {"created": [], "enqueued": []}

    def _create(uid, task_type, total, **params):
        task = {"id": f"task_{len(state['created'])}", "uid": uid, "type": task_type,
                "total": total, "status": "queued", **params}
        state["created"].append(task)
        return task

    async def _enqueue(uid, task):
        state["enqueued"].append(task)
        return True

    monkeypatch.setattr(stub, "create_task", _create)
    monkeypatch.setattr(stub, "enqueue_task", _enqueue)
    monkeypatch.setattr(stub, "get_queue_size", lambda uid: 0)
    return state


def _probe_stub(ytdl, monkeypatch, sources):
    """Stub the getav probe (fetch movie JSON + list versions)."""
    data = {"videoSources": sources}
    monkeypatch.setattr(ytdl, "fetch_getav_movie", lambda url, hosts: (data, "getav.net"))
    return data


def test_dl_handler_enqueues_task_with_params(ytdl, monkeypatch):
    state = _queue_state(monkeypatch)
    # single-version getav page: probe finds one source -> direct enqueue
    _probe_stub(ytdl, monkeypatch, [
        {"type": "cn_1080p", "url": "https://cdn/cn1080"}])
    msg = _FakeMessage("/dl -sub https://getav.net/zh/videos/cjod-159")
    asyncio.run(ytdl.dl_handler(None, msg))
    assert len(state["created"]) == 1
    task = state["enqueued"][0]
    assert task["type"] == "dl"
    assert task["url"] == "https://getav.net/zh/videos/cjod-159"
    assert task["want_subtitle"] is True
    assert task["uid"] == 42
    assert any("加入队列" in r for r in msg.replies)


def test_dl_handler_multiversion_getav_shows_card(ytdl, monkeypatch):
    state = _queue_state(monkeypatch)
    _probe_stub(ytdl, monkeypatch, [
        {"type": "cn_1080p", "url": "https://cdn/cn1080"},
        {"type": "uc_720p", "url": "https://cdn/uc720"},
        {"type": "raw_1080p", "url": "https://cdn/raw1080"},
    ])
    msg = _FakeMessage("/dl https://getav.net/zh/videos/cjod-159")
    asyncio.run(ytdl.dl_handler(None, msg))
    assert not state["created"]            # nothing enqueued until picked
    assert any("多个版本" in r for r in msg.replies)
    prompt = ytdl._GETAV_PROMPTS[42]
    assert len(prompt["versions"]) == 3
    # best-first order: cn 1080 > uc 720 > raw 1080
    assert [v[2] for v in prompt["versions"]] == [
        "中文字幕版 1080p", "无码版 720p", "原版 1080p"]
    assert prompt["want_subtitle"] is False


def test_getav_version_callback_enqueues_pinned_source(ytdl, monkeypatch):
    import re as _re
    state = _queue_state(monkeypatch)
    _probe_stub(ytdl, monkeypatch, [
        {"type": "cn_1080p", "url": "https://cdn/cn1080"},
        {"type": "uc_720p", "url": "https://cdn/uc720"},
    ])
    msg = _FakeMessage("/dl https://getav.net/zh/videos/cjod-159")
    asyncio.run(ytdl.dl_handler(None, msg))
    prompt = ytdl._GETAV_PROMPTS[42]
    token = prompt["token"]

    # user picks the SECOND button (uc 720p)
    query = _FakeQuery(42, f"gav:{token}:1", _re.compile(r"^gav:([0-9a-f]+):(\d+)$"))
    asyncio.run(ytdl.getav_version_callback(None, query))
    assert 42 not in ytdl._GETAV_PROMPTS      # prompt consumed
    task = state["enqueued"][0]
    assert task["source_url"] == "https://cdn/uc720"   # pinned, not auto-best
    assert any("已选择" in e for e in query.edits)
    assert any("加入队列" in r for r in msg.replies)


def test_getav_version_callback_rejects_expired(ytdl, monkeypatch):
    import re as _re
    import time as _time
    state = _queue_state(monkeypatch)
    _probe_stub(ytdl, monkeypatch, [
        {"type": "cn_1080p", "url": "https://cdn/cn1080"},
        {"type": "uc_720p", "url": "https://cdn/uc720"},
    ])
    msg = _FakeMessage("/dl https://getav.net/zh/videos/cjod-159")
    asyncio.run(ytdl.dl_handler(None, msg))
    prompt = ytdl._GETAV_PROMPTS[42]
    prompt["created_at"] = _time.time() - ytdl._GETAV_PROMPT_TTL - 1  # aged out

    query = _FakeQuery(42, f"gav:{prompt['token']}:0",
                       _re.compile(r"^gav:([0-9a-f]+):(\d+)$"))
    asyncio.run(ytdl.getav_version_callback(None, query))
    assert not state["created"]
    assert query.answers and "过期" in query.answers[0][0]


def test_getav_version_callback_ignores_foreign_user(ytdl, monkeypatch):
    import re as _re
    state = _queue_state(monkeypatch)
    _probe_stub(ytdl, monkeypatch, [
        {"type": "cn_1080p", "url": "https://cdn/cn1080"},
        {"type": "uc_720p", "url": "https://cdn/uc720"},
    ])
    asyncio.run(ytdl.dl_handler(
        None, _FakeMessage("/dl https://getav.net/zh/videos/cjod-159")))
    token = ytdl._GETAV_PROMPTS[42]["token"]

    # another user's callback: uid 7 has no prompt -> expired-style answer
    query = _FakeQuery(7, f"gav:{token}:0",
                       _re.compile(r"^gav:([0-9a-f]+):(\d+)$"))
    asyncio.run(ytdl.getav_version_callback(None, query))
    assert not state["created"]


def test_adl_handler_enqueues_task(ytdl, monkeypatch):
    state = _queue_state(monkeypatch)
    msg = _FakeMessage("/adl https://youtube.com/watch?v=x")
    asyncio.run(ytdl.adl_handler(None, msg))
    task = state["enqueued"][0]
    assert task["type"] == "adl"
    assert task["url"] == "https://youtube.com/watch?v=x"


def test_dl_handler_rejects_when_queue_full(ytdl, monkeypatch):
    state = _queue_state(monkeypatch)
    stub = sys.modules["plugins.tasks"]
    monkeypatch.setattr(stub, "get_queue_size", lambda uid: stub._MAX_QUEUE)
    msg = _FakeMessage("/dl https://missav.ai/sone-543")
    asyncio.run(ytdl.dl_handler(None, msg))
    assert not state["created"]
    assert any("队列已满" in r for r in msg.replies)


def test_dl_handler_usage_without_link(ytdl, monkeypatch):
    state = _queue_state(monkeypatch)
    msg = _FakeMessage("/dl")
    asyncio.run(ytdl.dl_handler(None, msg))
    assert not state["created"]
    assert any("用法" in r for r in msg.replies)


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
