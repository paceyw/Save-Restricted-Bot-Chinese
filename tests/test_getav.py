"""Offline tests for the getav.net downloader (same pipeline as missav).

No network: ``_http_get`` is monkeypatched with fixture-serving fakes.
No ffmpeg required: the merge path is verified byte-for-byte with a
stubbed remux, mirroring test_missav.py's conventions.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("missav_mod", SRC / "utils" / "missav.py")
missav = importlib.util.module_from_spec(spec)
sys.modules["missav_mod"] = missav
spec.loader.exec_module(missav)

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


# ─── URL recognition ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://getav.net/zh/videos/cjod-159",
    "https://getav.net/videos/cjod-159",               # locale-less (302s to /en)
    "https://www.getav.net/en/videos/cjod-159",
    "https://getav.net/ja/videos/cjod-159",
    "https://getav.net/zh-TW/videos/cjod-159",         # compound locale
    "https://getav.net/en/videos/fc2-ppv-1234567",     # multi-dash slug
    "https://getav.net/zh/videos/092014_887",          # underscore id
    "http://getav.net/zh/videos/cjod-159",
])
def test_video_urls_recognized(url):
    assert missav.is_getav_url(url) is True


@pytest.mark.parametrize("url", [
    "https://getav.net/zh/videos",          # listing
    "https://getav.net/zh/hot",             # listing
    "https://getav.net/zh",                 # locale root
    "https://getav.net",                    # bare host
    "https://getav.net/zh/videos/abcd",     # slug without a digit
    "https://getav.net/zh/cjod-159",        # missav-style path, no /videos
    "https://getav.net/videos/cjod-159/extra",  # trailing path junk
    "https://missav.ai/zh/videos/cjod-159", # foreign (missav) host
    "https://evil-getav.net/zh/videos/cjod-159",
    "ftp://getav.net/zh/videos/cjod-159",
])
def test_non_video_urls_rejected(url):
    assert missav.is_getav_url(url) is False


def test_parse_returns_components():
    info = missav.parse_getav_url("https://www.getav.net/zh/videos/cjod-159")
    assert info == {"host": "getav.net", "lang": "zh", "slug": "cjod-159"}
    info = missav.parse_getav_url("https://getav.net/videos/cjod-159")
    assert info == {"host": "getav.net", "lang": None, "slug": "cjod-159"}


def test_getav_api_candidates_order_and_scheme():
    cands = missav.getav_api_candidates("https://getav.net/zh/videos/cjod-159")
    assert cands == ["https://getav.net/api/movies/cjod-159"]
    # custom mirrors rotate after the original host, ports dropped
    cands = missav.getav_api_candidates(
        "https://getav.net:8443/zh/videos/cjod-159", ("getav.net", "getav2.net"))
    assert cands == [
        "https://getav.net/api/movies/cjod-159",
        "https://getav2.net/api/movies/cjod-159",
    ]
    assert missav.getav_api_candidates("https://missav.ai/sone-543") == []


# ─── movie JSON parsing ────────────────────────────────────────────────────────

def movie_json(sources=(("raw_1080p", "https://cdn.example/a/index.txt", 25),),
               **extra):
    data = {
        "id": "CJOD-159",
        "title": "[中文字幕] CJOD-159 アナルとマ○コ 妃月るい",
        "videoSources": [
            {"id": f"s{i}", "movieId": "CJOD-159", "source": "tiktok",
             "type": t, "url": u, "priority": p}
            for i, (t, u, p) in enumerate(sources)
        ],
    }
    data.update(extra)
    return json.dumps({"success": True, "data": data})


def test_parse_getav_json_accepts_valid():
    data = missav._parse_getav_json(movie_json())
    assert data["id"] == "CJOD-159"
    assert len(data["videoSources"]) == 1


@pytest.mark.parametrize("payload", [
    "not json",
    json.dumps({"success": False, "data": {}}),
    json.dumps({"success": True}),                      # no data
    json.dumps({"success": True, "data": {"id": "X"}}), # no videoSources
    json.dumps({"success": True, "data": {"videoSources": []}}),
])
def test_parse_getav_json_rejects_bad(payload):
    assert missav._parse_getav_json(payload) is None


# ─── source selection ──────────────────────────────────────────────────────────

def test_select_getav_source_prefers_family_then_resolution():
    sources = [
        {"type": "raw_1080p", "url": "https://cdn/raw1080", "priority": 25},
        {"type": "uc_1080p", "url": "https://cdn/uc1080", "priority": 45},
        {"type": "uc_720p", "url": "https://cdn/uc720", "priority": 40},
        {"type": "cn_480p", "url": "https://cdn/cn480", "priority": 30},
        {"type": "raw_720p", "url": "https://cdn/raw720", "priority": 23},
    ]
    url, family = missav.select_getav_source(sources)
    # cn beats uc/raw even at lower resolution; among uc, higher res wins
    assert url == "https://cdn/cn480"
    assert family == "cn"

    sources_wo_cn = [s for s in sources if not s["type"].startswith("cn_")]
    url, family = missav.select_getav_source(sources_wo_cn)
    assert url == "https://cdn/uc1080"
    assert family == "uc"

    url, family = missav.select_getav_source(
        [s for s in sources if s["type"].startswith("raw_")])
    assert url == "https://cdn/raw1080"
    assert family == "raw"


def test_select_getav_source_rejects_non_http_and_unknown_shapes():
    sources = [
        {"type": "raw_1080p", "url": "javascript:alert(1)"},
        {"type": "raw_1080p", "url": "ftp://x/y"},
        "not-a-dict",
        {"type": None, "url": None},
    ]
    assert missav.select_getav_source(sources) == (None, None)
    assert missav.select_getav_source([]) == (None, None)


def test_select_getav_source_unknown_type_ranks_by_priority():
    sources = [
        {"type": "weird", "url": "https://cdn/lo", "priority": 1},
        {"type": "weird", "url": "https://cdn/hi", "priority": 9},
    ]
    url, family = missav.select_getav_source(sources)
    assert url == "https://cdn/hi"
    assert family is None


# ─── cover + details ───────────────────────────────────────────────────────────

def test_getav_cover_url():
    assert missav.getav_cover_url({"localImg": "/images/cover/x.jpg"}) == \
        "https://static.worldstatic.com/images/cover/x.jpg"
    assert missav.getav_cover_url(
        {"localImg": "https://other/pic.jpg"}) == "https://other/pic.jpg"
    assert missav.getav_cover_url({"localImg": ""}) == ""
    assert missav.getav_cover_url({"localImg": "images/x.jpg"}) == ""  # relative, unusable
    assert missav.getav_cover_url({}) == ""


def details_movie(**extra):
    return {
        "id": "cjod-159",
        "title": "[中文字幕] CJOD-159 アナルとマ○コの両穴中出しOK 妃月るい",
        "stars": [{"name": "妃月るい"}, {"name": "妃月るい"}, {"name": ""}],
        "genres": ["肛交", {"name": "中出"}, "肛交"],
        "subtitles": [{"language": "ja"}, {"language": "zh"}],
        "uc": 0,
        **extra,
    }


def test_extract_getav_details_full():
    d = missav.extract_getav_details(
        details_movie(), "https://getav.net/zh/videos/cjod-159", family="raw")
    assert d["code"] == "CJOD-159"
    assert d["title"] == "アナルとマ○コの両穴中出しOK 妃月るい"
    assert d["actresses"] == ["妃月るい"]        # deduped, blanks dropped
    assert d["genres"] == ["肛交", "中出"]       # dict + str mix, deduped
    assert d["badges"] == ["中文字幕"]           # zh subtitle track present


def test_extract_getav_details_family_badges():
    d = missav.extract_getav_details(details_movie(subtitles=[]),
                                     "https://getav.net/zh/videos/cjod-159",
                                     family="cn")
    assert d["badges"] == ["中文字幕"]
    d = missav.extract_getav_details(details_movie(subtitles=[]),
                                     "https://getav.net/zh/videos/cjod-159",
                                     family="uc")
    assert d["badges"] == ["无码"]
    d = missav.extract_getav_details(details_movie(subtitles=[], uc=1),
                                     "https://getav.net/zh/videos/cjod-159",
                                     family="raw")
    assert d["badges"] == ["无码"]


def test_extract_getav_details_prefers_zh_overlay():
    d = missav.extract_getav_details(
        details_movie(
            titleZh="[无码/中文字幕] CJOD-159 肛门和蜜穴双穴中出OK 贪求快感的淫乱女仆 妃月琉衣 | GetAV",
            starsZh=["妃月琉衣"]),
        "https://getav.net/zh/videos/cjod-159", family="cn")
    assert d["title"] == "肛门和蜜穴双穴中出OK 贪求快感的淫乱女仆 妃月琉衣"
    # zh actress first, Japanese star kept as secondary
    assert d["actresses"] == ["妃月琉衣", "妃月るい"]


def test_augment_getav_zh_parses_page(monkeypatch):
    served = {
        "https://getav.net/zh/videos/cjod-159": FakeResp(text=(
            "<title>[无码/中文字幕] CJOD-159 肛门和蜜穴 妃月琉衣 | GetAV</title>"
            '<meta name="description" content="CJOD-159 肛门和蜜穴。主演：妃月琉衣、小明。支持手机播放"/>'
        )),
    }
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None:
            (served.get(url) or FakeResp(404), None))
    data = details_movie()
    missav._augment_getav_zh(data, "https://getav.net/zh/videos/cjod-159",
                             ("getav.net",), "getav.net")
    assert data["titleZh"] == "[无码/中文字幕] CJOD-159 肛门和蜜穴 妃月琉衣"
    assert data["starsZh"] == ["妃月琉衣", "小明"]
    assert "肛门和蜜穴" in data["descriptionZh"]


def test_augment_getav_zh_best_effort(monkeypatch):
    # page down / blocked: API fields stay untouched, no exception
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (None, "timeout"))
    data = details_movie()
    missav._augment_getav_zh(data, "https://getav.net/zh/videos/cjod-159",
                             ("getav.net",), "getav.net")
    assert "titleZh" not in data and "starsZh" not in data


def test_extract_getav_details_degrades():
    d = missav.extract_getav_details({}, "https://getav.net/zh/videos/cjod-159")
    assert d["code"] == "CJOD-159"               # slug fallback, uppercased
    assert d["title"] == ""
    assert d["actresses"] == [] and d["genres"] == [] and d["badges"] == []


def test_getav_details_feed_build_caption():
    d = missav.extract_getav_details(
        details_movie(), "https://getav.net/zh/videos/cjod-159", family="raw")
    cap = missav.build_caption(d)
    assert cap.startswith("CJOD-159")
    assert "演员：#妃月るい" in cap
    assert "标签：#肛交 #中出" in cap
    assert "类别：#中文字幕" in cap


# ─── movie API fetch (fake HTTP layer) ─────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, text="", content=b""):
        self.status_code = status
        self.text = text
        self.content = content if content else text.encode()
        self.url = None


def test_fetch_getav_movie_success(monkeypatch):
    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        assert url == "https://getav.net/api/movies/cjod-159"
        return FakeResp(text=movie_json()), None
    monkeypatch.setattr(missav, "_http_get", fake_get)
    data, host = missav.fetch_getav_movie("https://getav.net/zh/videos/cjod-159")
    assert data["id"] == "CJOD-159"
    assert host == "getav.net"


def test_fetch_getav_movie_404(monkeypatch):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (FakeResp(404), None))
    with pytest.raises(missav.MissAVError, match="不存在"):
        missav.fetch_getav_movie("https://getav.net/zh/videos/cjod-159")


def test_fetch_getav_movie_all_blocked(monkeypatch):
    sleeps = []
    monkeypatch.setattr(missav.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (None, "conn reset"))
    with pytest.raises(missav.MissAVBlockedError):
        missav.fetch_getav_movie("https://getav.net/zh/videos/cjod-159")
    # conn-reset is transient: the retry budget applies before giving up
    assert sleeps == list(missav.PAGE_RETRY_BACKOFF)


def test_fetch_getav_movie_rotates_blocked_mirror(monkeypatch):
    sleeps = []
    monkeypatch.setattr(missav.time, "sleep", lambda s: sleeps.append(s))

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if "getav.net" in url:
            return FakeResp(status=503, text="just a moment"), None
        return FakeResp(text=movie_json()), None
    monkeypatch.setattr(missav, "_http_get", fake_get)
    data, host = missav.fetch_getav_movie(
        "https://getav.net/zh/videos/cjod-159", ("getav.net", "getav2.net"))
    assert host == "getav2.net"
    # 503 is transient: retried to exhaustion on mirror 1, then rotation
    assert sleeps == list(missav.PAGE_RETRY_BACKOFF)


def test_fetch_getav_movie_redirect_offsite_rejected(monkeypatch):
    resp = FakeResp(text=movie_json())
    resp.url = "https://evil.example/api/movies/cjod-159"
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (resp, None))
    with pytest.raises(missav.MissAVBlockedError):
        missav.fetch_getav_movie(
            "https://getav.net/zh/videos/cjod-159", ("getav.net", "getav2.net"))


def test_fetch_getav_movie_garbage_json(monkeypatch):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (FakeResp(text="<html>"), None))
    with pytest.raises(missav.MissAVError, match="解析失败"):
        missav.fetch_getav_movie("https://getav.net/zh/videos/cjod-159")


# ─── end-to-end download (stubbed remux, byte-exact merge assertion) ──────────

def _aes_crypt(data, key, iv, encrypt=True):
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    op = cipher.encryptor() if encrypt else cipher.decryptor()
    return op.update(data) + op.finalize()


def _pkcs7(data):
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad


MEDIA_PL = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-KEY:METHOD=AES-128,URI="glyph.key",IV=0x0e92570270b04c4e4f0efc6eae7db5f2
#EXTINF:6.000000,
seg-0.woff2
#EXTINF:6.000000,
seg-1.woff2
#EXT-X-ENDLIST
"""


def test_download_getav_roundtrip(monkeypatch, tmp_path):
    key = b"k" * 16
    iv = bytes.fromhex("0e92570270b04c4e4f0efc6eae7db5f2")
    parts = [os.urandom(188 * 40 + 11 * (i + 1)) for i in range(2)]

    def enc_part(i, data):
        return _aes_crypt(_pkcs7(data), key, iv)

    served = {
        "https://getav.net/api/movies/cjod-159": FakeResp(
            text=movie_json(
                sources=(("uc_1080p", "https://static.worldstatic.com/cdn/a/index.txt", 45),),
                localImg="/images/cover/CJOD-159.jpg",
            ),
        ),
        "https://static.worldstatic.com/cdn/a/index.txt": FakeResp(text=MEDIA_PL),
        "https://static.worldstatic.com/cdn/a/glyph.key": FakeResp(content=key),
        "https://static.worldstatic.com/cdn/a/seg-0.woff2": FakeResp(content=enc_part(0, parts[0])),
        "https://static.worldstatic.com/cdn/a/seg-1.woff2": FakeResp(content=enc_part(1, parts[1])),
    }

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        resp = served.get(url)
        if resp is None:
            return FakeResp(status=404), None
        if "worldstatic.com" in url:
            assert headers and headers.get("Referer") == "https://getav.net/"
        return resp, None

    monkeypatch.setattr(missav, "_http_get", fake_get)

    async def fake_remux(src, dst):
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            fo.write(fi.read())
    monkeypatch.setattr(missav, "remux_to_mp4", fake_remux)

    events = []

    async def progress(done, total, stage):
        events.append((stage, done, total))

    dest = tmp_path / "out.mp4"
    meta = asyncio.run(missav.download_getav(
        "https://getav.net/zh/videos/cjod-159", str(dest), progress=progress))

    assert dest.read_bytes() == b"".join(parts)  # explicit-IV decrypt + ordered merge
    assert meta["title"] == "[中文字幕] CJOD-159 アナルとマ○コ 妃月るい"
    assert meta["thumbnail"] == "https://static.worldstatic.com/images/cover/CJOD-159.jpg"
    assert meta["segments"] == 2
    assert meta["host"] == "getav.net"
    assert meta["details"]["code"] == "CJOD-159"
    assert events[0] == ("page", 0, 1)
    assert events[-1] == ("merge", 2, 2)
    assert [p.name for p in tmp_path.iterdir()] == ["out.mp4"]  # no leaked temp dirs


def test_download_getav_no_playable_source(monkeypatch, tmp_path):
    served = {
        "https://getav.net/api/movies/cjod-159": FakeResp(
            text=movie_json(sources=(("raw_1080p", "javascript:alert(1)", 25),))),
    }
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (served.get(url) or FakeResp(404), None))
    with pytest.raises(missav.MissAVError, match="播放源"):
        asyncio.run(missav.download_getav(
            "https://getav.net/zh/videos/cjod-159", str(tmp_path / "o.mp4")))


def test_download_getav_rejects_private_playlist(monkeypatch, tmp_path):
    served = {
        "https://getav.net/api/movies/cjod-159": FakeResp(
            text=movie_json(sources=(("raw_1080p", "http://169.254.169.254/x.txt", 25),))),
    }
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (served.get(url) or FakeResp(404), None))
    with pytest.raises(missav.MissAVError, match="m3u8 地址非法"):
        asyncio.run(missav.download_getav(
            "https://getav.net/zh/videos/cjod-159", str(tmp_path / "o.mp4")))


def test_download_getav_rejects_host_hopping_segments(monkeypatch, tmp_path):
    evil_pl = MEDIA_PL.replace("seg-0.woff2", "https://evil.com/seg-0.woff2")
    served = {
        "https://getav.net/api/movies/cjod-159": FakeResp(
            text=movie_json(sources=(("raw_1080p", "https://static.worldstatic.com/cdn/a/index.txt", 25),))),
        "https://static.worldstatic.com/cdn/a/index.txt": FakeResp(text=evil_pl),
        "https://static.worldstatic.com/cdn/a/glyph.key": FakeResp(content=b"k" * 16),
    }
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (served.get(url) or FakeResp(404), None))
    with pytest.raises(missav.MissAVError, match="域校验失败"):
        asyncio.run(missav.download_getav(
            "https://getav.net/zh/videos/cjod-159", str(tmp_path / "o.mp4")))


# ─── Chinese subtitle embedding (site-polished VTT -> mov_text) ────────────────

SUB_ZH = {"language": "zh", "format": "vtt", "qualityScore": 84, "isVerified": False,
          "filePath": "https://static.worldstatic.com/subtitles/CJOD-159/zh.vtt"}
SUB_ZHTW = {"language": "zhtw", "format": "vtt", "qualityScore": 99, "isVerified": True,
            "filePath": "https://static.worldstatic.com/subtitles/CJOD-159/zhtw.vtt"}


def test_select_getav_subtitle_prefers_simplified_zh():
    # zh wins over zhtw even when zhtw has higher quality/verified flags
    sub = missav.select_getav_subtitle({"subtitles": [SUB_ZHTW, SUB_ZH]})
    assert sub["language"] == "zh"


def test_select_getav_subtitle_ranks_quality_and_verified():
    strong = dict(SUB_ZH, qualityScore=95, isVerified=True)
    weak = dict(SUB_ZH, qualityScore=10)
    assert missav.select_getav_subtitle({"subtitles": [weak, strong]}) is strong
    verified = dict(SUB_ZH, qualityScore=50, isVerified=True)
    unverified = dict(SUB_ZH, qualityScore=51, isVerified=False)
    assert missav.select_getav_subtitle({"subtitles": [unverified, verified]}) is verified


@pytest.mark.parametrize("data", [
    {},                                   # no subtitles key
    {"subtitles": []},
    {"subtitles": [{"language": "ja"}]},  # no Chinese track
    {"subtitles": [{"language": "zh", "format": "ass",
                    "filePath": "https://static.worldstatic.com/s.ass"}]},  # bad format
    {"subtitles": [{"language": "zh", "format": "vtt",
                    "filePath": "ftp://x/zh.vtt"}]},                        # bad scheme
    {"subtitles": ["not-a-dict"]},
])
def test_select_getav_subtitle_rejects(data):
    assert missav.select_getav_subtitle(data) is None


VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n早安主人\n"


def test_fetch_getav_subtitle_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (FakeResp(text=VTT), None))
    path = missav._fetch_getav_subtitle(
        SUB_ZH, "worldstatic.com", str(tmp_path))
    assert path and os.path.isfile(path)
    assert open(path, encoding="utf-8").read() == VTT
    os.remove(path)


def test_fetch_getav_subtitle_host_pinned(monkeypatch, tmp_path):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (FakeResp(text=VTT), None))
    assert missav._fetch_getav_subtitle(SUB_ZH, "surrit.com", str(tmp_path)) is None


@pytest.mark.parametrize("resp", [
    None,
    FakeResp(status=404),
    FakeResp(text=""),        # empty body
    FakeResp(text="no cues"), # not a subtitle
])
def test_fetch_getav_subtitle_degrades(monkeypatch, tmp_path, resp):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (resp, "err"))
    assert missav._fetch_getav_subtitle(SUB_ZH, "worldstatic.com", str(tmp_path)) is None
    assert list(tmp_path.iterdir()) == []  # nothing written


def test_burn_subtitles_args(monkeypatch, tmp_path):
    calls = []

    async def fake_run(args):
        calls.append(args)

    sub = str(tmp_path / "zh.vtt")
    monkeypatch.setattr(missav.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(missav, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(missav.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(missav.os.path, "getsize", lambda p: 10)

    asyncio.run(missav.burn_subtitles_to_mp4("a.ts", "a.mp4", sub))
    args = calls[0]
    vf = args[args.index("-vf") + 1]
    assert vf.startswith(f"subtitles={sub}:force_style=")
    assert "mov_text" not in " ".join(args)          # burn, not a text track
    assert "libx264" in args and "veryfast" in args  # full re-encode
    assert "-c:a" in args and args[args.index("-c:a") + 1] == "copy"
    assert "+faststart" in args and str(missav.BURN_CRF) in args
    style = vf.split("force_style='")[1]
    assert "Noto Sans CJK SC" in style            # fansub look
    assert "FontSize=13" in style                 # ≈4.5% of frame height
    assert "Outline=1" in style and "Shadow=0.5" in style
    assert "MarginV=10" in style                  # ≈3.5% above bottom


def test_burn_subtitles_escapes_filter_path(monkeypatch, tmp_path):
    captured = {}

    async def fake_run(args):
        captured["vf"] = args[args.index("-vf") + 1]

    monkeypatch.setattr(missav.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(missav, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(missav.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(missav.os.path, "getsize", lambda p: 10)
    asyncio.run(missav.burn_subtitles_to_mp4(
        "a.ts", "a.mp4", str(tmp_path / "we:ird'name.vtt")))
    assert captured["vf"].startswith("subtitles=")
    assert "\\:" in captured["vf"] or ":" not in captured["vf"].split(":force")[0]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_burn_subtitles_real_smoke(tmp_path):
    # 1s synthetic clip + one cue; fontconfig falls back to any system
    # font, so this only proves the encode chain, not glyph rendering.
    import subprocess
    src = tmp_path / "in.ts"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=duration=1:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=duration=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         str(src)], check=True)
    sub = tmp_path / "s.vtt"
    sub.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhi\n")
    dst = tmp_path / "out.mp4"
    asyncio.run(missav.burn_subtitles_to_mp4(str(src), str(dst), str(sub)))
    assert dst.exists() and dst.stat().st_size > 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("height", [360, 720])
def test_burn_subtitle_size_scales_with_resolution(tmp_path, height):
    """The style must render as a fixed FRACTION of frame height.

    Burns a two-line cue onto a plain black clip (so any bright pixel is
    subtitle ink), extracts the mid-cue frame as raw gray, and measures
    the row bands of white pixels:
      - exactly 2 bands → the two cue lines are visibly separated
        (line spacing, not glued/overlapping),
      - band height ≈ 4.5% of the frame height (was 18% with FontSize=52),
      - band height halves at 360p vs 720p → resolution-proportional.
    Pairs with test_burn_subtitle_proportionality_ratio below.
    """
    import subprocess
    width = height * 16 // 9
    src = tmp_path / f"in_{height}.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"color=c=black:s={width}x{height}:d=1:r=10",
         "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    sub = tmp_path / "two.vtt"
    sub.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nLINE ONE\nLINE TWO\n")
    dst = tmp_path / f"out_{height}.mp4"
    asyncio.run(missav.burn_subtitles_to_mp4(str(src), str(dst), str(sub)))

    frame = tmp_path / f"frame_{height}.gray"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.5", "-i", str(dst),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", str(frame)],
        check=True)
    rows = frame.read_bytes()
    bands = []
    start = None
    for y in range(height):
        bright = sum(1 for x in range(width) if rows[y * width + x] > 200)
        if bright >= 3 and start is None:
            start = y
        elif bright < 3 and start is not None:
            bands.append((start, y - start))
            start = None
    if start is not None:
        bands.append((start, height - start))

    assert len(bands) == 2, f"two cue lines must be separate bands, got {bands}"
    band_h = max(h for _y, h in bands)
    gap = bands[1][0] - (bands[0][0] + bands[0][1])
    assert gap >= 1, "no visible gap between the two subtitle lines"
    # glyph em ≈ 4.5% of height; caps/latin ink ≈ 0.55-0.85 em
    assert height * 0.02 <= band_h <= height * 0.07, \
        f"band {band_h}px is not ~4.5% of {height}px frame"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_burn_subtitle_proportionality_ratio(tmp_path):
    """Glyph pixels must scale with resolution: 720p band ≈ 2x the 360p one."""
    import subprocess
    sizes = {}
    for height in (360, 720):
        width = height * 16 // 9
        src = tmp_path / f"r_in_{height}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", f"color=c=black:s={width}x{height}:d=1:r=10",
             "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
        sub = tmp_path / "r.vtt"
        sub.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nLINE ONE\n")
        dst = tmp_path / f"r_out_{height}.mp4"
        asyncio.run(missav.burn_subtitles_to_mp4(str(src), str(dst), str(sub)))
        frame = tmp_path / f"r_frame_{height}.gray"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.5", "-i", str(dst),
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", str(frame)],
            check=True)
        rows = frame.read_bytes()
        band_h = 0
        run_start = None
        for y in range(height):
            bright = sum(1 for x in range(width) if rows[y * width + x] > 200)
            if bright >= 3 and run_start is None:
                run_start = y
            elif bright < 3 and run_start is not None:
                band_h = max(band_h, y - run_start)
                run_start = None
        if run_start is not None:
            band_h = max(band_h, height - run_start)
        assert band_h > 0
        sizes[height] = band_h
    ratio = sizes[720] / sizes[360]
    assert 1.6 <= ratio <= 2.4, \
        f"subtitle pixels must scale with resolution, got {sizes} (ratio {ratio:.2f})"


def _hls_fixture(with_subtitle, tmp_path, monkeypatch):
    """Shared wiring for the burn roundtrips: serves a 2-segment uc
    playlist plus an optional zh VTT; records (kind, subtitle_path)
    calls — kind is 'burn' or 'remux'."""
    key = b"k" * 16
    iv = bytes.fromhex("0e92570270b04c4e4f0efc6eae7db5f2")
    parts = [os.urandom(188 * 40), os.urandom(188 * 40)]

    def enc_part(i, data):
        return _aes_crypt(_pkcs7(data), key, iv)

    movie = movie_json(
        sources=(("uc_1080p", "https://static.worldstatic.com/cdn/a/index.txt", 45),),
    )
    if with_subtitle:
        data = json.loads(movie)["data"]
        data["subtitles"] = [SUB_ZH]
        movie = json.dumps({"success": True, "data": data})

    served = {
        "https://getav.net/api/movies/cjod-159": FakeResp(text=movie),
        "https://static.worldstatic.com/cdn/a/index.txt": FakeResp(text=MEDIA_PL),
        "https://static.worldstatic.com/cdn/a/glyph.key": FakeResp(content=key),
        "https://static.worldstatic.com/cdn/a/seg-0.woff2": FakeResp(content=enc_part(0, parts[0])),
        "https://static.worldstatic.com/cdn/a/seg-1.woff2": FakeResp(content=enc_part(1, parts[1])),
        SUB_ZH["filePath"]: FakeResp(text=VTT),
    }
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (served.get(url) or FakeResp(404), None))

    calls = []

    async def _copy(src, dst):
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            fo.write(fi.read())

    async def fake_burn(src, dst, subtitle_path):
        calls.append(("burn", subtitle_path))
        await _copy(src, dst)

    async def fake_remux(src, dst):
        calls.append(("remux", None))
        await _copy(src, dst)

    monkeypatch.setattr(missav, "burn_subtitles_to_mp4", fake_burn)
    monkeypatch.setattr(missav, "remux_to_mp4", fake_remux)
    return parts, calls


async def _copy_remux(src, dst):
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fo.write(fi.read())


def test_download_getav_burns_subtitle(monkeypatch, tmp_path):
    parts, calls = _hls_fixture(True, tmp_path, monkeypatch)
    events = []

    async def progress(done, total, stage):
        events.append((stage, done, total))

    dest = tmp_path / "out.mp4"
    meta = asyncio.run(missav.download_getav(
        "https://getav.net/zh/videos/cjod-159", str(dest),
        want_subtitle=True, progress=progress))
    assert dest.read_bytes() == b"".join(parts)
    assert calls == [("burn", calls[0][1])]
    assert calls[0][1] and calls[0][1].endswith(".vtt")
    assert not os.path.exists(calls[0][1])          # temp subtitle cleaned up
    assert [p.name for p in tmp_path.iterdir()] == ["out.mp4"]
    assert meta["segments"] == 2
    assert ("burn", 0, 1) in events                  # burn stage surfaced


def test_download_getav_no_subtitle_when_track_absent(monkeypatch, tmp_path):
    parts, calls = _hls_fixture(False, tmp_path, monkeypatch)
    dest = tmp_path / "out.mp4"
    asyncio.run(missav.download_getav(
        "https://getav.net/zh/videos/cjod-159", str(dest)))
    assert calls == [("remux", None)]


def test_download_getav_subtitle_fetch_failure_degrades(monkeypatch, tmp_path):
    # subtitle URL 404s -> download continues without the burn
    parts, calls = _hls_fixture(True, tmp_path, monkeypatch)
    real_get = missav._http_get

    def failing_sub(url, headers=None, timeout=None, max_bytes=None):
        if url == SUB_ZH["filePath"]:
            return FakeResp(status=404), None
        return real_get(url, headers=headers, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(missav, "_http_get", failing_sub)
    dest = tmp_path / "out.mp4"
    asyncio.run(missav.download_getav(
        "https://getav.net/zh/videos/cjod-159", str(dest), want_subtitle=True))
    assert calls == [("remux", None)]
    assert dest.exists()


def test_download_getav_failed_burn_retries_plain(monkeypatch, tmp_path):
    # burn fails -> core falls back to the plain remux and still delivers
    parts, calls = _hls_fixture(True, tmp_path, monkeypatch)

    async def failing_burn(src, dst, subtitle_path):
        calls.append(("burn", subtitle_path))
        raise missav.MissAVError("ffmpeg 烧录失败: boom")

    monkeypatch.setattr(missav, "burn_subtitles_to_mp4", failing_burn)
    dest = tmp_path / "out.mp4"
    meta = asyncio.run(missav.download_getav(
        "https://getav.net/zh/videos/cjod-159", str(dest), want_subtitle=True))
    assert calls == [("burn", calls[0][1]), ("remux", None)]
    assert dest.exists() and meta["segments"] == 2


def test_download_getav_default_skips_subtitle(monkeypatch, tmp_path):
    """Default /dl never fetches or burns the subtitle track."""
    parts, calls = _hls_fixture(True, tmp_path, monkeypatch)  # zh track IS served
    real_get = missav._http_get

    def spying_get(url, headers=None, timeout=None, max_bytes=None):
        assert url != SUB_ZH["filePath"], "subtitle must not be fetched by default"
        return real_get(url, headers=headers, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(missav, "_http_get", spying_get)
    dest = tmp_path / "out.mp4"
    meta = asyncio.run(missav.download_getav(
        "https://getav.net/zh/videos/cjod-159", str(dest)))
    assert calls == [("remux", None)]
    assert dest.exists() and meta["segments"] == 2


def test_download_getav_cn_family_skips_subtitle(monkeypatch, tmp_path):
    # cn release already has burned-in subs: no track fetched, no burn
    key = b"k" * 16
    iv = bytes.fromhex("0e92570270b04c4e4f0efc6eae7db5f2")
    parts = [os.urandom(188 * 40)]
    movie = movie_json(
        sources=(("cn_1080p", "https://static.worldstatic.com/cdn/a/index.txt", 99),),
    )
    data = json.loads(movie)["data"]
    data["subtitles"] = [SUB_ZH]
    served = {
        "https://getav.net/api/movies/cjod-159": FakeResp(
            text=json.dumps({"success": True, "data": data})),
        "https://static.worldstatic.com/cdn/a/index.txt": FakeResp(
            text=MEDIA_PL.replace("#EXTINF:6.000000,\nseg-0.woff2\n#EXTINF:6.000000,\nseg-1.woff2\n",
                                  "#EXTINF:6.000000,\nseg-0.woff2\n")),
        "https://static.worldstatic.com/cdn/a/glyph.key": FakeResp(content=key),
        "https://static.worldstatic.com/cdn/a/seg-0.woff2": FakeResp(
            content=_aes_crypt(_pkcs7(parts[0]), key, iv)),
    }
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (served.get(url) or FakeResp(404), None))
    calls = []

    async def fake_burn(src, dst, subtitle_path):
        calls.append(("burn", subtitle_path))

    async def fake_remux(src, dst):
        calls.append(("remux", None))
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            fo.write(fi.read())

    monkeypatch.setattr(missav, "burn_subtitles_to_mp4", fake_burn)
    monkeypatch.setattr(missav, "remux_to_mp4", fake_remux)
    asyncio.run(missav.download_getav(
        "https://getav.net/zh/videos/cjod-159", str(tmp_path / "out.mp4")))
    assert calls == [("remux", None)]


def test_list_getav_sources_labels_and_order():
    sources = [
        {"type": "raw_1080p", "url": "https://cdn/raw1080", "priority": 25},
        {"type": "uc_720p", "url": "https://cdn/uc720", "priority": 40},
        {"type": "cn_1080p", "url": "https://cdn/cn1080", "priority": 30},
        {"type": "broken", "url": "javascript:x"},
    ]
    listed = missav.list_getav_sources(sources)
    assert listed == [
        ("https://cdn/cn1080", "cn", "中文字幕版 1080p"),
        ("https://cdn/uc720", "uc", "无码版 720p"),
        ("https://cdn/raw1080", "raw", "原版 1080p"),
    ]
    assert missav.list_getav_sources([]) == []


def test_download_getav_pins_chosen_source(monkeypatch, tmp_path):
    """source_url (the /dl card pick) must override auto-best selection."""
    key = b"k" * 16
    iv = bytes.fromhex("0e92570270b04c4e4f0efc6eae7db5f2")
    parts = [os.urandom(188 * 40)]
    # TWO sources: auto-best would be cn_1080 @ /cdn/best/, the user pins
    # the raw_480p @ /cdn/a/ playlist (the only one actually served).
    movie = movie_json(sources=(
        ("cn_1080p", "https://static.worldstatic.com/cdn/best/index.txt", 99),
        ("raw_480p", "https://static.worldstatic.com/cdn/a/index.txt", 10),
    ))
    served = {
        "https://getav.net/api/movies/cjod-159": FakeResp(text=movie),
        "https://static.worldstatic.com/cdn/a/index.txt": FakeResp(
            text=MEDIA_PL.replace("#EXTINF:6.000000,\nseg-0.woff2\n#EXTINF:6.000000,\nseg-1.woff2\n",
                                  "#EXTINF:6.000000,\nseg-0.woff2\n")),
        "https://static.worldstatic.com/cdn/a/glyph.key": FakeResp(content=key),
        "https://static.worldstatic.com/cdn/a/seg-0.woff2": FakeResp(
            content=_aes_crypt(_pkcs7(parts[0]), key, iv)),
    }
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (served.get(url) or FakeResp(404), None))

    async def fake_remux(src, dst):
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            fo.write(fi.read())

    monkeypatch.setattr(missav, "remux_to_mp4", fake_remux)
    dest = tmp_path / "out.mp4"
    asyncio.run(missav.download_getav(
        "https://getav.net/zh/videos/cjod-159", str(dest),
        source_url="https://static.worldstatic.com/cdn/a/index.txt"))
    assert dest.read_bytes() == parts[0]        # pinned playlist downloaded,
    # not the auto-best one (which would 404 and fail the whole download)
