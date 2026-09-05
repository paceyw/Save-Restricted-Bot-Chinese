"""Offline tests for the missav downloader (issue #13).

No network: ``_http_get`` is monkeypatched with fixture-serving fakes.
No ffmpeg required: the merge path is verified byte-for-byte with a
stubbed remux, and the missing-ffmpeg guard is exercised for real.
"""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]

# utils.missav imports config (transcode budget knobs); config hard-requires
# these keys at import time, so default them for hermetic test runs.
os.environ.setdefault("MASTER_KEY", "missav-test-master")
os.environ.setdefault("IV_KEY", "missav-test-iv")
spec = importlib.util.spec_from_file_location("missav_mod", SRC / "utils" / "missav.py")
missav = importlib.util.module_from_spec(spec)
sys.modules["missav_mod"] = missav
spec.loader.exec_module(missav)

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


# ─── URL recognition ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://missav.ai/cn/sone-543-chinese-subtitle",
    "https://missav.ai/sone-543",
    "https://missav.ai/dm1151/092014_887",
    "https://missav.ai/dm464/081012-097",
    "https://www.missav.ws/cn/midv-911",
    "https://missav.live/en/starts-143",
    "https://missav123.com/sone-543",
    "http://missav.ai/sone-543",
])
def test_video_urls_recognized(url):
    assert missav.is_missav_url(url) is True


@pytest.mark.parametrize("url", [
    "https://missav.ai/dm278/chinese-subtitle",   # category listing
    "https://missav.ai/cn/dm278",                 # dm category under lang
    "https://missav.ai/search/abp",               # search page
    "https://missav.ai",                          # bare host
    "https://missav.ai/new",                      # listing slug, no digit
    "https://missav.ai/cn",                       # lang root
    "https://youtube.com/sone-543",               # foreign host
    "ftp://missav.ai/sone-543",                   # wrong scheme
    "https://evil.com/missav.ai/sone-543",        # path trickery
    None, 42, "", "not a url",
])
def test_non_video_urls_rejected(url):
    assert missav.is_missav_url(url) is False


def test_parse_returns_components():
    info = missav.parse_missav_url("https://www.missav.ai/cn/sone-543-chinese-subtitle")
    assert info == {"host": "missav.ai", "lang": "cn", "slug": "sone-543-chinese-subtitle"}
    info = missav.parse_missav_url("https://missav.ai/dm1151/092014_887")
    assert info == {"host": "missav.ai", "lang": None, "slug": "092014_887"}


def test_mirror_candidates_original_first():
    cands = missav.mirror_candidates("https://missav.ai/sone-543")
    assert cands[0] == "https://missav.ai/sone-543"
    assert len(cands) == len(missav.DEFAULT_MIRRORS)
    assert all(u.startswith("https://") for u in cands)
    # no duplicates and every mirror covered
    assert sorted(urlparse_host(u) for u in cands) == sorted(missav.DEFAULT_MIRRORS)


def urlparse_host(u):
    from urllib.parse import urlparse
    return urlparse(u).hostname

# ─── variant slugs / sister versions (issue #17) ──────────────────────────────

@pytest.mark.parametrize("slug,family", [
    ("sone-543", None),
    ("sone-543-chinese-subtitle", "cn"),
    ("sone-543-ch-sub", "cn"),
    ("sone-543-c", "cn"),
    ("cawd-629-uncensored-leak", "uc"),
    ("stars-804-uncensored", "uc"),
    ("stars-804-leak", "uc"),
    ("cawd-629-uncensored-leak-chinese-subtitle", "cn"),  # cn tail wins; _slug_variant splits
])
def test_missav_slug_family(slug, family):
    assert missav.missav_slug_family(slug) == family


@pytest.mark.parametrize("slug,variant", [
    ("sone-543", "raw"),
    ("sone-543-chinese-subtitle", "cn"),
    ("sone-543-ch-sub", "cn"),
    ("cawd-629-uncensored-leak", "uc"),
    ("cawd-629-uncensored-leak-chinese-subtitle", "uc-cn"),
    ("092014_887", "raw"),
])
def test_slug_variant_four_states(slug, variant):
    assert missav._slug_variant(slug) == variant


@pytest.mark.parametrize("slug,base", [
    ("sone-543", "sone-543"),
    ("sone-543-chinese-subtitle", "sone-543"),
    ("sone-543-ch-sub", "sone-543"),
    ("stars-804-uncensored-leak", "stars-804"),
    ("midv-911-uncensored", "midv-911"),
    ("sone-543-leak", "sone-543"),
    ("cawd-629-uncensored-leak-chinese-subtitle", "cawd-629"),
    ("092014_887", "092014_887"),
])
def test_missav_base_slug_strips_tails_repeatedly(slug, base):
    assert missav.missav_base_slug(slug) == base


def test_variant_candidates_from_raw_page():
    cands = missav.missav_variant_candidates("https://missav.ai/sone-543")
    assert cands[0] == ("raw", "https://missav.ai/sone-543")
    assert [v for v, _ in cands[1:]] == ["cn", "uc", "uc-cn"]
    urls = dict(cands[1:])
    assert urls["cn"] == "https://missav.ai/sone-543-chinese-subtitle"
    assert urls["uc"] == "https://missav.ai/sone-543-uncensored-leak"
    assert urls["uc-cn"] == "https://missav.ai/sone-543-uncensored-leak-chinese-subtitle"


def test_variant_candidates_skip_current_family():
    cands = missav.missav_variant_candidates(
        "https://missav.ai/cn/sone-543-chinese-subtitle")
    assert cands[0] == ("cn", "https://missav.ai/cn/sone-543-chinese-subtitle")
    assert [v for v, _ in cands[1:]] == ["uc", "uc-cn"]
    # a combined page still probes the plain cn + uc sisters
    cands = missav.missav_variant_candidates(
        "https://missav.ai/cawd-629-uncensored-leak-chinese-subtitle")
    assert cands[0] == (
        "uc-cn", "https://missav.ai/cawd-629-uncensored-leak-chinese-subtitle")
    assert [v for v, _ in cands[1:]] == ["cn", "uc"]


def test_variant_candidates_keep_dm_lang_prefix_and_host():
    # dm<digits> category prefix (dm BEFORE the slug: /cn/dm1151/... is a listing)
    cands = missav.missav_variant_candidates("https://www.missav.ws/dm1151/092014_887")
    assert cands[0] == ("raw", "https://www.missav.ws/dm1151/092014_887")
    for variant, u in cands[1:]:
        assert urlparse_host(u) == "www.missav.ws"
        assert u.startswith("https://www.missav.ws/dm1151/092014_887-")
    # language-prefixed video URL keeps the lang segment
    cands = missav.missav_variant_candidates("https://www.missav.ws/cn/sone-543")
    assert cands[0][1] == "https://www.missav.ws/cn/sone-543"
    for variant, u in cands[1:]:
        assert urlparse_host(u) == "www.missav.ws"
        assert u.startswith("https://www.missav.ws/cn/sone-543-")


def test_variant_candidates_non_missav_url():
    assert missav.missav_variant_candidates("https://youtube.com/watch?v=x") == []


def test_probe_missav_page_rejects_non_video(monkeypatch):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (
            FakeResp(text="<html>new releases</html>"), None))
    assert missav._probe_missav_page("https://missav.ai/sone-543") == (False, None)
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (FakeResp(status=404), None))
    assert missav._probe_missav_page("https://missav.ai/sone-543") == (False, None)
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (None, "timeout"))
    assert missav._probe_missav_page("https://missav.ai/sone-543") == (False, None)


def test_probe_missav_page_returns_stream_fingerprint(monkeypatch):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (
            FakeResp(text=_page_html("https://surrit.com/0a1b2c3d-0000-1111-2222-abcde0000001/playlist.m3u8")), None))
    ok, m3u8 = missav._probe_missav_page("https://missav.ai/sone-543")
    assert ok is True
    assert m3u8 == "https://surrit.com/0a1b2c3d-0000-1111-2222-abcde0000001/playlist.m3u8"
    assert missav._m3u8_stream_key(m3u8) == "0a1b2c3d-0000-1111-2222-abcde0000001"


def test_discover_missav_variants_partial_existence(monkeypatch):
    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if url.endswith("sone-543"):
            return FakeResp(text=_page_html("https://surrit.com/0a1b2c3d-0000-1111-2222-abcde0000001/playlist.m3u8")), None
        if "uncensored-leak-chinese-subtitle" in url:
            # a REAL combo page serves its own stream (different UUID)
            return FakeResp(text=_page_html("https://surrit.com/0a1b2c3d-0000-1111-2222-abcde0000002/playlist.m3u8")), None
        return FakeResp(status=404), None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    found = asyncio.run(
        missav.discover_missav_variants("https://missav.ai/sone-543"))
    assert found == [
        ("raw", "https://missav.ai/sone-543", "原版"),
        ("uc-cn", "https://missav.ai/sone-543-uncensored-leak-chinese-subtitle",
         "无码破解·中文字幕"),
    ]


def test_discover_missav_variants_phantom_alias_rejected(monkeypatch):
    """missav answers unknown slug suffixes with the BASE video's page:
    same m3u8 stream ⇒ phantom alias, never offered as a version."""
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (
            FakeResp(text=_page_html("https://surrit.com/0a1b2c3d-0000-1111-2222-abcde0000001/playlist.m3u8")), None))
    found = asyncio.run(
        missav.discover_missav_variants("https://missav.ai/fc2-ppv-2761664"))
    assert found == [("raw", "https://missav.ai/fc2-ppv-2761664", "原版")]


def test_discover_missav_variants_all_exist_sorted(monkeypatch):
    streams = {
        "sone-543": "https://surrit.com/0a1b2c3d-0000-1111-2222-abcde0000001/playlist.m3u8",
        "sone-543-chinese-subtitle": "https://surrit.com/0a1b2c3d-0000-1111-2222-abcde0000002/playlist.m3u8",
        "sone-543-uncensored-leak": "https://surrit.com/0a1b2c3d-0000-1111-2222-abcde0000003/playlist.m3u8",
        "sone-543-uncensored-leak-chinese-subtitle": "https://surrit.com/0a1b2c3d-0000-1111-2222-abcde0000004/playlist.m3u8",
    }

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        for tail, m3u8 in streams.items():
            if url.endswith(tail):
                return FakeResp(text=_page_html(m3u8)), None
        return FakeResp(status=404), None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    found = asyncio.run(
        missav.discover_missav_variants("https://missav.ai/sone-543"))
    assert [(v, u) for v, u, _ in found] == [
        ("raw", "https://missav.ai/sone-543"),
        ("cn", "https://missav.ai/sone-543-chinese-subtitle"),
        ("uc", "https://missav.ai/sone-543-uncensored-leak"),
        ("uc-cn", "https://missav.ai/sone-543-uncensored-leak-chinese-subtitle"),
    ]
    assert [lbl for _, _, lbl in found] == ["原版", "中文字幕", "无码破解", "无码破解·中文字幕"]


def test_discover_missav_variants_all_blocked_falls_back(monkeypatch):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (
            FakeResp(status=403, text="Just a moment..."), None))
    found = asyncio.run(missav.discover_missav_variants(
        "https://missav.ai/cn/sone-543-chinese-subtitle"))
    assert found == [
        ("cn", "https://missav.ai/cn/sone-543-chinese-subtitle", "中文字幕")]


def test_discover_missav_variants_unreachable_falls_back(monkeypatch):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (None, "conn reset"))
    found = asyncio.run(
        missav.discover_missav_variants("https://missav.ai/sone-543"))
    assert found == [("raw", "https://missav.ai/sone-543", "原版")]


# ─── subtitle track + VTT merge (issue #18) ────────────────────────────────────

MASTER_SUBS_ZH = """#EXTM3U
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",DEFAULT=YES,AUTOSELECT=YES,URI="subs/en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="中文",DEFAULT=NO,AUTOSELECT=YES,URI="subs/zh.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080,SUBTITLES="subs"
1080/prog.m3u8
"""


def test_select_subtitle_media_prefers_zh_name():
    pl = m3u8_load(MASTER_SUBS_ZH)
    assert missav.select_subtitle_media(pl) == "subs/zh.m3u8"


def test_select_subtitle_media_falls_back_to_default():
    text = MASTER_SUBS_ZH.replace(
        '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="中文",'
        'DEFAULT=NO,AUTOSELECT=YES,URI="subs/zh.m3u8"\n', "")
    pl = m3u8_load(text)
    assert missav.select_subtitle_media(pl) == "subs/en.m3u8"


def test_select_subtitle_media_any_when_unlabeled():
    text = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",URI="subs/en.m3u8"\n'
        '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Français",URI="subs/fr.m3u8"\n'
        "#EXT-X-STREAM-INF:BANDWIDTH=1,SUBTITLES=\"subs\"\nprog.m3u8\n"
    )
    pl = m3u8_load(text)
    assert missav.select_subtitle_media(pl) == "subs/en.m3u8"


def test_select_subtitle_media_skips_uri_less_entries():
    text = (
        '#EXTM3U\n'
        '#EXT-X-MEDIA:TYPE=SUBTITLES,NAME="bare"\n'
        '#EXT-X-MEDIA:TYPE=SUBTITLES,NAME="real",URI="subs/real.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=1,SUBTITLES="subs"\nprog.m3u8\n'
    )
    assert missav.select_subtitle_media(m3u8_load(text)) == "subs/real.m3u8"


def test_select_subtitle_media_none_on_plain_master():
    assert missav.select_subtitle_media(m3u8_load(MASTER)) is None


VTT_SEG_A = (
    "WEBVTT\n"
    "X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n"
    "\n"
    "00:00.000 --> 00:02.500\n"
    "こんにちは\n"
    "\n"
    "00:03.000 --> 00:04.000\n"
    "第二句\n"
)
VTT_SEG_B = (
    "WEBVTT\n"
    "X-TIMESTAMP-MAP=LOCAL:00:00.000,MPEGTS:900000\n"
    "\n"
    "00:00.000 --> 00:02.500\n"
    "こんにちは\n"
    "\n"
    "00:59.800 --> 01:00.200\n"
    "最後\n"
)


def test_merge_vtt_segments_applies_map_dedupes_and_formats():
    out = missav.merge_vtt_segments([VTT_SEG_A, VTT_SEG_B])
    assert out.startswith("WEBVTT")
    # MPEGTS 900000 = +10s shift onto the absolute timeline
    assert "00:00:10.000 --> 00:00:12.500\nこんにちは" in out
    assert "00:00:13.000 --> 00:00:14.000\n第二句" in out
    # MM:SS.mmm LOCAL values land on HH:MM:SS.mmm output
    assert "00:01:09.800 --> 00:01:10.200\n最後" in out
    # the exact boundary duplicate is emitted once
    assert out.count("こんにちは") == 1


def test_merge_vtt_segments_handles_hour_format_and_unmapped():
    a = "WEBVTT\n\n00:00:59.500 --> 00:01:01.000\nHello"
    b = "WEBVTT\n\n0:59.500 --> 1:01.000\nHello"  # 1-digit MM:SS.mmm, no map
    out = missav.merge_vtt_segments([a, b])
    assert out.count("Hello") == 1  # same absolute cue, deduped across formats
    assert "00:00:59.500 --> 00:01:01.000" in out


def _concat_aware_remux(src, dst):
    """Test double for the remux seam: concatenates ffconcat-listed parts,
    falls back to a byte copy for plain media files (pre-contract input)."""
    async def _run():
        with open(src, "rb") as fi:
            head = fi.read(8)
        if head.lstrip() == b"ffconcat":
            chunks = []
            with open(src, "r", encoding="utf-8") as fl:
                for line in fl:
                    line = line.strip()
                    if line.startswith("file '") and line.endswith("'"):
                        with open(line[6:-1], "rb") as fs:
                            chunks.append(fs.read())
            data = b"".join(chunks)
        else:
            with open(src, "rb") as fi:
                data = fi.read()
        with open(dst, "wb") as fo:
            fo.write(data)
    return _run()


MASTER_WITH_SUBS = """#EXTM3U
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="中文",DEFAULT=YES,AUTOSELECT=YES,URI="subs/zh.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080,SUBTITLES="subs"
1080/prog.m3u8
"""

SUBS_PLAYLIST = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXTINF:6.0,
zh-0.vtt
#EXTINF:6.0,
zh-1.vtt
#EXT-X-ENDLIST
"""


def test_download_missav_want_subtitle_burns_site_track(monkeypatch, tmp_path):
    key = os.urandom(16)
    media_sequence = 5
    parts = [os.urandom(188 * 40), os.urandom(188 * 40)]

    def enc_part(i, data):
        iv = (i + media_sequence).to_bytes(16, "big")
        return _aes_crypt(_pkcs7(data), key, iv, encrypt=True)

    served = {
        "https://missav.ai/sone-543": FakeResp(
            text=_page_html("https://surrit.com/vid/master.m3u8")),
        "https://surrit.com/vid/master.m3u8": FakeResp(text=MASTER_WITH_SUBS),
        "https://surrit.com/vid/1080/prog.m3u8": FakeResp(text=MEDIA),
        "https://surrit.com/vid/1080/enc.key": FakeResp(content=key),
        "https://surrit.com/vid/1080/seg-0.ts": FakeResp(content=enc_part(0, parts[0])),
        "https://surrit.com/vid/1080/seg-1.ts": FakeResp(content=enc_part(1, parts[1])),
        "https://surrit.com/vid/subs/zh.m3u8": FakeResp(text=SUBS_PLAYLIST),
        "https://surrit.com/vid/subs/zh-0.vtt": FakeResp(text=VTT_SEG_A),
        "https://surrit.com/vid/subs/zh-1.vtt": FakeResp(text=VTT_SEG_B),
    }

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        resp = served.get(url)
        return (resp, None) if resp is not None else (FakeResp(status=404), None)

    monkeypatch.setattr(missav, "_http_get", fake_get)

    burned = {}

    async def fake_burn(src, dst, subtitle_path, task_id=None):
        burned["vtt"] = Path(subtitle_path).read_text(encoding="utf-8")
        with open(dst, "wb") as fo:
            fo.write(b"burned")

    monkeypatch.setattr(missav, "burn_subtitles_to_mp4", fake_burn)
    monkeypatch.setattr(missav, "remux_to_mp4", _concat_aware_remux)

    dest = tmp_path / "out.mp4"
    meta = asyncio.run(missav.download_missav(
        "https://missav.ai/sone-543", str(dest), want_subtitle=True))
    assert dest.read_bytes() == b"burned"
    vtt = burned["vtt"]
    assert vtt.startswith("WEBVTT")
    assert "00:00:10.000 --> 00:00:12.500" in vtt
    assert "00:01:09.800 --> 00:01:10.200" in vtt
    assert vtt.count("こんにちは") == 1
    assert meta["segments"] == 2
    # subtitle temp file cleaned up next to the output
    assert [p.name for p in tmp_path.iterdir()] == ["out.mp4"]


def test_download_missav_want_subtitle_getav_fallback(monkeypatch, tmp_path):
    clear = MEDIA.replace('#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\n', "")
    official_vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n公式中字\n"
    served = {
        "https://missav.ai/sone-543": FakeResp(
            text=_page_html("https://surrit.com/vid/master.m3u8")),
        "https://surrit.com/vid/master.m3u8": FakeResp(text=MASTER),  # no track
        "https://surrit.com/vid/1080/prog.m3u8": FakeResp(text=clear),
        "https://surrit.com/vid/1080/seg-0.ts": FakeResp(content=b"a" * 90),
        "https://surrit.com/vid/1080/seg-1.ts": FakeResp(content=b"b" * 90),
        "https://getav.net/api/movies/SONE-543": FakeResp(text=json.dumps({
            "success": True,
            "data": {
                "id": "sone-543",
                "title": "SONE-543 すごい作品",
                "videoSources": [
                    {"type": "raw_1080p",
                     "url": "https://static.worldstatic.com/raw.m3u8"}],
                "subtitles": [
                    {"language": "zh", "format": "vtt",
                     "filePath": "https://static.worldstatic.com/sub.vtt"}],
            },
        })),
        "https://static.worldstatic.com/sub.vtt": FakeResp(text=official_vtt, content=official_vtt.encode()),
    }

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        resp = served.get(url)
        return (resp, None) if resp is not None else (FakeResp(status=404), None)

    monkeypatch.setattr(missav, "_http_get", fake_get)

    burned = {}

    async def fake_burn(src, dst, subtitle_path, task_id=None):
        burned["vtt"] = Path(subtitle_path).read_text(encoding="utf-8")
        with open(dst, "wb") as fo:
            fo.write(b"burned")

    monkeypatch.setattr(missav, "burn_subtitles_to_mp4", fake_burn)
    monkeypatch.setattr(missav, "remux_to_mp4", _concat_aware_remux)

    dest = tmp_path / "out.mp4"
    asyncio.run(missav.download_missav(
        "https://missav.ai/sone-543", str(dest), want_subtitle=True))
    assert dest.read_bytes() == b"burned"
    assert burned["vtt"] == official_vtt
    assert [p.name for p in tmp_path.iterdir()] == ["out.mp4"]


def test_download_missav_want_subtitle_getav_miss_remuxes_plain(monkeypatch, tmp_path):
    clear = MEDIA.replace('#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\n', "")
    parts = [b"a" * 90, b"b" * 90]
    served = {
        "https://missav.ai/sone-543": FakeResp(
            text=_page_html("https://surrit.com/vid/master.m3u8")),
        "https://surrit.com/vid/master.m3u8": FakeResp(text=MASTER),
        "https://surrit.com/vid/1080/prog.m3u8": FakeResp(text=clear),
        "https://surrit.com/vid/1080/seg-0.ts": FakeResp(content=parts[0]),
        "https://surrit.com/vid/1080/seg-1.ts": FakeResp(content=parts[1]),
        # getav API 404: no official subtitle either
    }
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (
            (served[url], None) if url in served else (FakeResp(status=404), None)))

    async def unexpected_burn(src, dst, subtitle_path, task_id=None):
        raise AssertionError("must not burn when nothing was found")

    monkeypatch.setattr(missav, "burn_subtitles_to_mp4", unexpected_burn)
    monkeypatch.setattr(missav, "remux_to_mp4", _concat_aware_remux)

    dest = tmp_path / "out.mp4"
    asyncio.run(missav.download_missav(
        "https://missav.ai/sone-543", str(dest), want_subtitle=True))
    assert dest.read_bytes() == b"".join(parts)


def test_find_getav_subtitle_hit(monkeypatch, tmp_path):
    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if url == "https://getav.net/api/movies/SONE-543":
            return FakeResp(text=json.dumps({
                "success": True,
                "data": {
                    "id": "sone-543", "title": "SONE-543 作品",
                    "videoSources": [{"type": "raw_1080p",
                                      "url": "https://static.worldstatic.com/v.m3u8"}],
                    "subtitles": [{"language": "zh", "format": "vtt",
                                   "filePath": "https://static.worldstatic.com/zh.vtt"}],
                }})), None
        if url == "https://static.worldstatic.com/zh.vtt":
                return FakeResp(text="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n公式\n",
                                content=b"WEBVTT cue body"), None
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(missav, "_http_get", fake_get)
    path = missav.find_getav_subtitle_for_code("SONE-543", str(tmp_path))
    assert path and "公式" in Path(path).read_text(encoding="utf-8")
    assert os.path.dirname(path) == str(tmp_path)


def test_find_getav_subtitle_api_miss(monkeypatch, tmp_path):
    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if url.endswith("/api/movies/SONE-543"):
            return FakeResp(status=404), None
        raise AssertionError(f"subtitle must not be fetched after API miss: {url}")

    monkeypatch.setattr(missav, "_http_get", fake_get)
    assert missav.find_getav_subtitle_for_code("SONE-543", str(tmp_path)) is None


def test_find_getav_subtitle_code_mismatch_rejected(monkeypatch, tmp_path):
    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if url.endswith("/api/movies/SONE-543"):
            return FakeResp(text=json.dumps({
                "success": True,
                "data": {"id": "stars-999", "title": "别的片",
                         "videoSources": [{"type": "raw_1080p",
                                           "url": "https://static.worldstatic.com/v.m3u8"}],
                         "subtitles": [{"language": "zh", "format": "vtt",
                                        "filePath": "https://static.worldstatic.com/zh.vtt"}]},
            })), None
        raise AssertionError(f"another title's subs must never be fetched: {url}")

    monkeypatch.setattr(missav, "_http_get", fake_get)
    assert missav.find_getav_subtitle_for_code("SONE-543", str(tmp_path)) is None


def test_find_getav_subtitle_bad_code_no_http(monkeypatch, tmp_path):
    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        raise AssertionError(f"no HTTP for a bad code: {url}")

    monkeypatch.setattr(missav, "_http_get", fake_get)
    assert missav.find_getav_subtitle_for_code("", str(tmp_path)) is None
    assert missav.find_getav_subtitle_for_code("../etc/passwd", str(tmp_path)) is None


# ─── packed JS ─────────────────────────────────────────────────────────────────

def _packed_block(packed, base, count, keys):
    keys_str = "|".join(keys)
    return (
        "eval(function(p,a,c,k,e,d){e=function(c){return c.toString(36)};"
        "if(!''.replace(/^/,String)){while(c--){d[e(c)]=k[c]||e(c)}k=[function(e){return d[e]}];"
        "e=function(){return'\\\\w+'};c=1};while(c--){if(k[c]){p=p.replace(new RegExp('\\\\b'+e(c)+'\\\\b','g'),k[c])}}return p}"
        f"('{packed}',{base},{count},'{keys_str}'.split('|'),0,{{}})"
    )


def test_unpack_packed_js_decodes_tokens():
    # base 36, 2 symbols: token '0' -> 'source', token '1' -> m3u8 url
    url = "https://surrit.com/vid/playlist.m3u8"
    block = _packed_block("var 0=\\'1\\';", 36, 2, ["source", url])
    out = missav.unpack_packed_js(block)
    assert out == "var source=\\'%s\\';" % url


def test_unpack_packed_js_rejects_malformed():
    assert missav.unpack_packed_js("no packer here") is None
    assert missav.unpack_packed_js("") is None
    # base 1 would loop forever
    bad = _packed_block("x", 1, 5, ["a"])
    assert missav.unpack_packed_js(bad) is None
    # absurd count
    bad = _packed_block("x", 36, 999999, ["a"])
    assert missav.unpack_packed_js(bad) is None


def test_extract_m3u8_url_prefers_source_assignment():
    url = "https://surrit.com/abc/playlist.m3u8"
    other = "https://surrit.com/zzz/other.m3u8"
    html = (
        '<html><meta property="og:title" content="T">'
        "<script>" + _packed_block("var q=\\\\'%s\\\\';var 0=\\\\'%s\\\\';" % (other, url), 36, 2, ["source", url]) +
        "</script></html>"
    )
    # NOTE: token 1 -> url, token 0 -> 'source'; packed uses '0' for source=…
    assert missav.extract_m3u8_url(html) == url


def test_extract_m3u8_url_falls_back_to_any_m3u8():
    url = "https://surrit.com/abc/playlist.m3u8"
    html = (
        '<html><script>'
        + _packed_block("var hls=\\\\'%s\\\\';" % url, 36, 1, [url])
        + "</script></html>"
    )
    assert missav.extract_m3u8_url(html) == url


def test_extract_page_info_reads_og_meta():
    html = (
        '<meta property="og:title" content="SONE-543 剧情">'
        '<meta property="og:image" content="https://cdn.example/pic.jpg">'
    )
    info = missav.extract_page_info(html)
    assert info["title"] == "SONE-543 剧情"
    assert info["thumbnail"] == "https://cdn.example/pic.jpg"


# ─── HLS crypto ────────────────────────────────────────────────────────────────

def _aes_crypt(data, key, iv, encrypt=True):
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    op = cipher.encryptor() if encrypt else cipher.decryptor()
    return op.update(data) + op.finalize()


def _pkcs7(data):
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad


def test_decrypt_segment_roundtrip():
    key = os.urandom(16)
    iv = os.urandom(16)
    plain = b"The quick brown fox jumps over the lazy dog" * 4
    cipher = _aes_crypt(_pkcs7(plain), key, iv, encrypt=True)
    assert missav.decrypt_segment(cipher, key, iv) == plain


def test_decrypt_segment_unpadded_passthrough():
    # Some streams ship un-padded aligned data; strip only when padding is valid
    key, iv = os.urandom(16), os.urandom(16)
    raw = os.urandom(64)  # last byte random: padding check almost surely fails
    out = missav.decrypt_segment(raw, key, iv)
    assert len(out) == 64


def test_decrypt_segment_rejects_misaligned():
    with pytest.raises(missav.MissAVError):
        missav.decrypt_segment(b"12345", os.urandom(16), os.urandom(16))


def test_segment_iv_explicit_hex():
    iv = missav.segment_iv("0x0011", 7, 100)
    assert iv == bytes.fromhex("0011".zfill(32))
    assert len(iv) == 16


def test_segment_iv_implicit_uses_media_sequence():
    iv = missav.segment_iv(None, 3, 5)
    assert iv == (8).to_bytes(16, "big")
    iv0 = missav.segment_iv(None, 0, 0)
    assert iv0 == bytes(16)


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360
360/prog.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080
1080/prog.m3u8
"""


def test_select_variant_uri_picks_highest():
    pl = m3u8_load(MASTER)
    assert missav.select_variant_uri(pl) == "1080/prog.m3u8"


def m3u8_load(text):
    import m3u8 as m3u8_lib
    return m3u8_lib.loads(text)


MEDIA = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:5
#EXT-X-TARGETDURATION:6
#EXT-X-KEY:METHOD=AES-128,URI="enc.key"
#EXTINF:5.0,
seg-0.ts
#EXTINF:5.0,
seg-1.ts
#EXT-X-ENDLIST
"""


def test_playlist_encryption_without_iv():
    pl = m3u8_load(MEDIA)
    enc = missav.playlist_encryption(pl)
    assert enc == {"method": "AES-128", "uri": "enc.key", "iv": None}


def test_playlist_encryption_with_iv_and_sample_aes():
    pl = m3u8_load(MEDIA.replace('URI="enc.key"', 'URI="enc.key",IV=0x1234'))
    assert missav.playlist_encryption(pl)["iv"] == "0x1234"
    bad = m3u8_load(MEDIA.replace("METHOD=AES-128", "METHOD=SAMPLE-AES"))
    with pytest.raises(missav.MissAVError):
        missav.playlist_encryption(bad)


def test_playlist_encryption_none_when_clear():
    pl = m3u8_load(MEDIA.replace('#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\n', ""))
    assert missav.playlist_encryption(pl) is None


# ─── fake HTTP layer ───────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, text="", content=b""):
        self.status_code = status
        self.text = text
        self.content = content
        self.headers = {}


def _page_html(m3u8_url, title="SONE-543"):
    script = _packed_block(
        "var 0=\\'1\\';", 36, 2, ["source", m3u8_url]
    )
    return (
        f'<html><head><meta property="og:title" content="{title}">'
        '<meta property="og:image" content="https://cdn.example/pic.jpg">'
        f"</head><body><script>{script}</script></body></html>"
    )


def test_fetch_video_page_rotates_blocked_mirrors(monkeypatch):
    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if "missav.ai" in url:
            return FakeResp(status=403, text="Just a moment..."), None
        if "missav.ws" in url:
            return FakeResp(text=_page_html("https://surrit.com/0a1b2c3d-0000-1111-2222-abcde0000001/playlist.m3u8")), None
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(missav, "_http_get", fake_get)
    html, host = missav.fetch_video_page("https://missav.ai/sone-543")
    assert host == "missav.ws"
    assert "og:title" in html


def test_fetch_video_page_all_blocked(monkeypatch):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (FakeResp(status=403, text="Just a moment..."), None),
    )
    with pytest.raises(missav.MissAVBlockedError):
        missav.fetch_video_page("https://missav.ai/sone-543")


def test_fetch_video_page_404_reports_missing(monkeypatch):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (FakeResp(status=404), None),
    )
    with pytest.raises(missav.MissAVError, match="不存在"):
        missav.fetch_video_page("https://missav.ai/sone-543")


def test_fetch_video_page_unreachable_reports_blocked(monkeypatch):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (None, "connection reset"),
    )
    with pytest.raises(missav.MissAVBlockedError):
        missav.fetch_video_page("https://missav.ai/sone-543")


def test_fetch_video_page_reachable_but_not_video(monkeypatch):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (FakeResp(text="<html>plain page</html>"), None),
    )
    with pytest.raises(missav.MissAVError, match="解析失败"):
        missav.fetch_video_page("https://missav.ai/sone-543")


# ─── end-to-end pipeline (stubbed remux, byte-exact merge assertion) ──────────

def test_download_missav_roundtrip(monkeypatch, tmp_path):
    key = os.urandom(16)
    media_sequence = 5
    parts = [os.urandom(188 * 40 + 11 * (i + 1)) for i in range(3)]  # TS-ish blobs

    def enc_part(i, data):
        iv = (i + media_sequence).to_bytes(16, "big")
        return _aes_crypt(_pkcs7(data), key, iv, encrypt=True)

    media_pl = MEDIA  # 2 segments … build a 3-segment playlist
    media_pl = media_pl.replace(
        '#EXTINF:5.0,\nseg-1.ts\n#EXT-X-ENDLIST',
        '#EXTINF:5.0,\nseg-1.ts\n#EXTINF:5.0,\nseg-2.ts\n#EXT-X-ENDLIST',
    )

    served = {
        "https://missav.ai/sone-543": FakeResp(text=_page_html("https://surrit.com/vid/master.m3u8")),
        "https://surrit.com/vid/master.m3u8": FakeResp(text=MASTER),
        "https://surrit.com/vid/1080/prog.m3u8": FakeResp(text=media_pl),
        "https://surrit.com/vid/1080/enc.key": FakeResp(content=key),
        "https://surrit.com/vid/1080/seg-0.ts": FakeResp(content=enc_part(0, parts[0])),
        "https://surrit.com/vid/1080/seg-1.ts": FakeResp(content=enc_part(1, parts[1])),
        "https://surrit.com/vid/1080/seg-2.ts": FakeResp(content=enc_part(2, parts[2])),
    }

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        resp = served.get(url)
        if resp is None:
            return FakeResp(status=404), None
        # every CDN request must carry the missav referer pair
        if "surrit.com" in url:
            assert headers and headers.get("Referer") == "https://missav.ai/"
        return resp, None

    monkeypatch.setattr(missav, "_http_get", fake_get)

    seen_src = {}

    async def fake_remux(src, dst):
        seen_src["src"] = os.path.basename(src)
        await _concat_aware_remux(src, dst)

    monkeypatch.setattr(missav, "remux_to_mp4", fake_remux)

    events = []

    async def progress(done, total, stage):
        events.append((stage, done, total))

    dest = tmp_path / "out.mp4"
    meta = asyncio.run(
        missav.download_missav("https://missav.ai/sone-543", str(dest), progress=progress)
    )

    assert dest.read_bytes() == b"".join(parts)  # byte-exact decrypt + ordered concat
    assert seen_src["src"] == "list.txt"  # issue #19: remux reads the ffconcat list, no merged.ts
    assert meta["title"] == "SONE-543"
    assert meta["thumbnail"] == "https://cdn.example/pic.jpg"
    assert meta["segments"] == 3
    assert meta["host"] == "missav.ai"
    assert meta["details"]["code"] == "SONE-543"  # slug fallback (fixture has no panel)
    assert events[0] == ("page", 0, 1)
    assert events[-1] == ("merge", 3, 3)
    seg_events = [e for e in events if e[0] == "segments"]
    assert [e[1] for e in seg_events] == [1, 2, 3]  # monotonically counted
    # no part files leaked next to the output
    assert [p.name for p in tmp_path.iterdir()] == ["out.mp4"]


def test_download_missav_failing_segment_cancels_siblings(monkeypatch, tmp_path):
    media_pl = MEDIA  # needs seg-0.ts + seg-1.ts
    calls = {"n": 0}

    async def fake_sleep(s):
        pass  # 502 escalates to the deep budget: no real backoff in tests

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if url.endswith("sone-543"):
            return FakeResp(text=_page_html("https://surrit.com/vid/master.m3u8")), None
        if url.endswith("master.m3u8"):
            return FakeResp(text=MASTER), None
        if url.endswith("prog.m3u8"):
            return FakeResp(text=media_pl), None
        if url.endswith("seg-0.ts"):
            calls["n"] += 1
            return FakeResp(status=502), None  # always fails -> deep budget
        if url.endswith("enc.key"):
            return FakeResp(content=b"k" * 16), None
        return FakeResp(content=b"x" * 32), None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    monkeypatch.setattr(missav.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        missav, "remux_to_mp4",
        lambda src, dst: asyncio.sleep(0, result=None),
    )

    dest = tmp_path / "out.mp4"
    with pytest.raises(missav.MissAVError, match="片段 0 下载失败"):
        asyncio.run(missav.download_missav("https://missav.ai/sone-543", str(dest)))
    # 502 is a gateway brownout: the deep 8-attempt budget applies
    assert calls["n"] == missav.SEGMENT_RETRIES_SERVER_ERROR
    assert not dest.exists()
    # failure path must not leak segment temp dirs next to the output
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == []


def test_remux_missing_ffmpeg_reports_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(missav.shutil, "which", lambda name: None)
    with pytest.raises(missav.MissAVError, match="ffmpeg"):
        asyncio.run(missav.remux_to_mp4(str(tmp_path / "a.ts"), str(tmp_path / "a.mp4")))


# ─── ffmpeg demotion / burn knobs / concat list (issue #19) ────────────────────

def test_run_ffmpeg_prefixes_nice_ionice(monkeypatch):
    captured = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(missav.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        missav.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in ("nice", "ionice", "ffmpeg") else None)
    asyncio.run(missav._run_ffmpeg(["ffmpeg", "-version"]))
    cmd = captured["cmd"]
    assert cmd[:3] == ("nice", "-n", "19")     # CPU demotion first
    assert cmd[3:5] == ("ionice", "-c3")       # IO idle class when available
    assert cmd[-2:] == ("ffmpeg", "-version")  # real command rides last
    # missing wrappers degrade to the raw command
    monkeypatch.setattr(missav.shutil, "which", lambda name: None)
    asyncio.run(missav._run_ffmpeg(["ffmpeg", "-version"]))
    assert captured["cmd"] == ("ffmpeg", "-version")


def test_remux_burn_sniff_concat_list_input(monkeypatch, tmp_path):
    captured = {}

    async def fake_run(args, timeout_s=None):
        captured["args"] = list(args)

    monkeypatch.setattr(missav, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(
        missav.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)

    lst = tmp_path / "list.txt"
    lst.write_text("ffconcat version 1.0\nfile '/tmp/x/000000.ts'\n", encoding="utf-8")
    dst = tmp_path / "o1.mp4"
    dst.write_bytes(b"x")
    asyncio.run(missav.remux_to_mp4(str(lst), str(dst)))
    args = captured["args"]
    idx = args.index("-i")
    assert args[idx - 6:idx] == ["-f", "concat", "-safe", "0", "-fflags", "+genpts"]
    assert args[idx + 1] == str(lst)
    # a plain media file keeps the legacy direct -i (no concat flags)
    plain = tmp_path / "in.ts"
    plain.write_bytes(b"\x00" * 32)
    dst2 = tmp_path / "o2.mp4"
    dst2.write_bytes(b"x")
    asyncio.run(missav.remux_to_mp4(str(plain), str(dst2)))
    args = captured["args"]
    assert args[args.index("-i") + 1] == str(plain)
    assert "concat" not in args  # legacy direct input, no concat flags


def test_is_concat_list_sniff(tmp_path):
    lst = tmp_path / "l.txt"
    lst.write_text("ffconcat version 1.0\n", encoding="utf-8")
    assert missav._is_concat_list(str(lst)) is True
    ts = tmp_path / "in.ts"
    ts.write_bytes(b"\x47\x40\x00\x10" * 8)
    assert missav._is_concat_list(str(ts)) is False
    assert missav._is_concat_list(str(tmp_path / "missing")) is False


def test_burn_uses_superfast_preset_and_concat_input(monkeypatch, tmp_path):
    captured = {}

    async def fake_run(args, timeout_s=None):
        captured["args"] = list(args)

    sub = tmp_path / "zh.vtt"
    sub.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhi\n", encoding="utf-8")
    src = tmp_path / "list.txt"
    src.write_text("ffconcat version 1.0\nfile '/tmp/x/000000.ts'\n", encoding="utf-8")
    dst = tmp_path / "out.mp4"
    dst.write_bytes(b"x")
    monkeypatch.setattr(missav, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(missav, "BURN_PRESET", "superfast")
    monkeypatch.setattr(missav, "BURN_CRF", 19)
    asyncio.run(missav.burn_subtitles_to_mp4(str(src), str(dst), str(sub)))
    args = captured["args"]
    assert args[args.index("-preset") + 1] == "superfast"
    assert args[args.index("-crf") + 1] == "19"
    idx = args.index("-i")
    assert args[idx - 6:idx] == ["-f", "concat", "-safe", "0", "-fflags", "+genpts"]
    assert "subtitles=" in args[args.index("-vf") + 1]


# ─── security hardening (post-review) ─────────────────────────────────────────

def test_host_allowed_blocks_private_and_pins_domain():
    # private / link-local / metadata hosts are never dialled
    for bad in (
        "http://127.0.0.1/x.m3u8",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/enc.key",
        "http://192.168.1.4/a.m3u8",
        "http://172.20.0.3/a.m3u8",
        "http://localhost/a.m3u8",
        "http://metadata.google.internal/x",
        "ftp://surrit.com/a.m3u8",
        "file:///etc/passwd",
    ):
        assert missav._host_allowed(bad, "surrit.com") is False, bad
    # pinned domain and its subdomains pass
    for good in (
        "https://surrit.com/a.m3u8",
        "https://cdn.surrit.com/a.m3u8",
        "https://x.y.surrit.com/seg.ts",
    ):
        assert missav._host_allowed(good, "surrit.com") is True, good
    # other public domains are rejected under the pin
    assert missav._host_allowed("https://evil.com/a.m3u8", "surrit.com") is False


def test_download_missav_rejects_host_hopping_segments(monkeypatch, tmp_path):
    # playlist on surrit.com declares a segment on evil.com -> must abort
    media_evil = MEDIA.replace("seg-0.ts", "https://evil.com/seg-0.ts")

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if url.endswith("sone-543"):
            return FakeResp(text=_page_html("https://surrit.com/vid/master.m3u8")), None
        if url.endswith("master.m3u8"):
            return FakeResp(text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nprog.m3u8\n"), None
        if url.endswith("prog.m3u8"):
            return FakeResp(text=media_evil), None
        if url.endswith("enc.key"):
            return FakeResp(content=b"k" * 16), None
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(missav, "_http_get", fake_get)
    with pytest.raises(missav.MissAVError, match="域校验失败"):
        asyncio.run(
            missav.download_missav("https://missav.ai/sone-543", str(tmp_path / "o.mp4"))
        )


def test_download_missav_rejects_private_m3u8(monkeypatch, tmp_path):
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (
            FakeResp(text=_page_html("http://169.254.169.254/x.m3u8")), None
        ),
    )
    with pytest.raises(missav.MissAVError, match="m3u8 地址非法"):
        asyncio.run(
            missav.download_missav("https://missav.ai/sone-543", str(tmp_path / "o.mp4"))
        )


def test_download_missav_enforces_duration_ceiling(monkeypatch, tmp_path):
    long_pl = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
        + "".join(f"#EXTINF:5.0,\nseg-{i:04d}.ts\n" for i in range(6000))
        + "#EXT-X-ENDLIST\n"
    )  # 30000s > 8h
    served = {
        "https://missav.ai/sone-543": FakeResp(text=_page_html("https://surrit.com/vid/prog.m3u8")),
        "https://surrit.com/vid/prog.m3u8": FakeResp(text=long_pl),
    }
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (served.get(url) or FakeResp(404), None),
    )
    with pytest.raises(missav.MissAVError, match="时长超上限"):
        asyncio.run(
            missav.download_missav("https://missav.ai/sone-543", str(tmp_path / "o.mp4"))
        )


def test_download_missav_enforces_byte_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(missav, "MAX_TOTAL_BYTES", 100)
    clear = MEDIA.replace('#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\n', "")
    served = {
        "https://missav.ai/sone-543": FakeResp(text=_page_html("https://surrit.com/vid/prog.m3u8")),
        "https://surrit.com/vid/prog.m3u8": FakeResp(text=clear),
        # seg-0 90 bytes fits; seg-1 90 more exceeds the 100-byte budget
        "https://surrit.com/vid/seg-0.ts": FakeResp(content=b"a" * 90),
        "https://surrit.com/vid/seg-1.ts": FakeResp(content=b"b" * 90),
    }
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None: (served.get(url) or FakeResp(404), None),
    )
    with pytest.raises(missav.MissAVError, match="预算"):
        asyncio.run(
            missav.download_missav("https://missav.ai/sone-543", str(tmp_path / "o.mp4"), concurrency=1)
        )


def test_http_get_aborts_oversized_stream(monkeypatch):

    class _SlowStream:
        def __init__(self, total):
            self.total = total
            self.pos = 0

        def iter_content(self, chunk_size=1):
            while self.pos < self.total:
                yield b"x" * min(chunk_size, self.total - self.pos)
                self.pos += chunk_size

    class _FakeStreamResp:
        status_code = 200
        url = "https://surrit.com/big.ts"
        headers = {}

        def __init__(self, total):
            self._s = _SlowStream(total)

        def iter_content(self, chunk_size=1):
            return self._s.iter_content(chunk_size)

    class _FakeSession:
        def get(self, url, headers=None, timeout=None, stream=False):
            return _FakeStreamResp(10 * 1024 * 1024)

    monkeypatch.setattr(missav, "_get_session", lambda: _FakeSession())
    resp, err = missav._http_get(
        "https://surrit.com/big.ts", max_bytes=1024 * 1024
    )
    assert resp is None
    assert "body too large" in err


def test_unpack_packed_js_rejects_oversized_dictionary():
    huge_key = "z" * (65 * 1024)
    block = _packed_block("var 0=1", 36, 2, ["source", huge_key])
    assert missav.unpack_packed_js(block) is None


def test_mirror_candidates_drops_port():
    cands = missav.mirror_candidates("https://missav.ai:9200/sone-543")
    assert cands[0] == "https://missav.ai/sone-543"
    assert all(":9200" not in c for c in cands)
    # out-of-range ports must not raise
    cands = missav.mirror_candidates("https://missav.ai:99999/sone-543")
    assert cands[0] == "https://missav.ai/sone-543"


# ─── video details + caption (issue #13 follow-up) ────────────────────────────

# trimmed from a real missav.ai/cn/dass-629 page (2026-08)
REAL_PAGE = """
<html><head>
<meta property="og:title" content="DASS-629 你愿意当我的宠物吗？真实的故事。饲养女同宠物的美女：桃永纱里奈、仙谷最中、松井日菜子 - 百永さりな">
<meta property="og:image" content="https://fourhoi.com/dass-629/cover-n.jpg">
</head><body>
<div class="space-y-2">
 <div class="text-secondary"><span>发行日期:</span> <time>2025-05-09</time></div>
 <div class="text-secondary"><span>番号:</span> <span class="font-medium">DASS-629</span></div>
 <div class="text-secondary"><span>标题:</span> <span class="font-medium">私に飼われてみない？ 実録。</span></div>
 <div class="text-secondary"><span>女优:</span>
   <a href="https://missav.live/dm71/cn/actresses/%E7%99%BE%E6%B0%B8" class="text-nord13 font-medium">百永さりな</a>,
   <a href="https://missav.live/dm30/cn/actresses/%E5%8D%83%E7%9F%B3" class="text-nord13 font-medium">千石もなか</a>,
   <a href="https://missav.live/dm5/cn/actresses/%E6%9D%BE%E4%BA%95" class="text-nord13 font-medium">松井日奈子</a>
 </div>
 <div class="text-secondary"><span>类型:</span>
   <a href="https://missav.live/dm757/cn/genres/%E8%8B%97%E6%9D%A1" class="text-nord13 font-medium">苗条</a>,
   <a href="https://missav.live/dm900/cn/genres/%E5%A5%B3%E5%90%8C" class="text-nord13 font-medium">女同性恋</a>,
   <a href="https://missav.live/dm906/cn/genres/%E6%BD%AE%E5%90%B" class="text-nord13 font-medium">潮吹</a>,
   <a href="https://missav.live/dm166/cn/genres/%E5%A4%9A%E4%BA" class="text-nord13 font-medium">多人运动</a>
 </div>
</div>
</body></html>
"""

REAL_PAGE_EN = """
<html><head><meta property="og:title" content="DASS-629 Would you be my pet? - Sarina Momonaga"></head><body>
<div class="text-secondary"><span>Code:</span> <span class="font-medium">DASS-629</span></div>
<div class="text-secondary"><span>Title:</span> <span class="font-medium">Watashi ni kawareteminai?</span></div>
<div class="text-secondary"><span>Actress:</span>
 <a href="https://missav.live/dm71/en/actresses/Sarina%20Momonaga" class="text-nord13 font-medium">Sarina Momonaga</a>
</div>
<div class="text-secondary"><span>Genre:</span>
 <a href="https://missav.live/dm757/en/genres/Slim" class="text-nord13 font-medium">Slim</a>,
 <a href="https://missav.live/dm900/en/genres/Lesbian" class="text-nord13 font-medium">Lesbian</a>
</div>
</body></html>
"""


def test_extract_video_details_cn_page():
    d = missav.extract_video_details(REAL_PAGE, "https://missav.ai/cn/dass-629")
    assert d["code"] == "DASS-629"
    assert d["actresses"] == ["百永さりな", "千石もなか", "松井日奈子"]
    assert d["actresses_cn"] == []  # missav 面板无中文名：javbus/词库补
    assert d["genres"] == ["苗条", "女同性恋", "潮吹", "多人运动"]
    assert d["badges"] == []
    # intro: og:title minus code prefix and trailing "- actress"
    assert d["title"].startswith("你愿意当我的宠物吗")
    assert "DASS-629" not in d["title"]
    assert not d["title"].endswith("百永さりな")


def test_extract_video_details_en_page_and_badges():
    d = missav.extract_video_details(REAL_PAGE_EN, "https://missav.ai/en/dass-629-chinese-subtitle")
    assert d["code"] == "DASS-629"
    assert d["actresses"] == ["Sarina Momonaga"]
    assert d["genres"] == ["Slim", "Lesbian"]
    assert d["badges"] == ["中文字幕"]
    assert d["title"].startswith("Would you be my pet?")


def test_extract_video_details_degrades_without_panel():
    # layout change / empty page: every field falls back independently
    d = missav.extract_video_details("<html></html>", "https://missav.ai/sone-543")
    assert d["actresses"] == [] and d["genres"] == []
    # slug fallback code: sone-543 -> SONE-543
    assert d["code"] == "SONE-543"
    d2 = missav.extract_video_details("<html></html>", "https://missav.ai/dm1151/092014_887")
    assert d2["code"] == ""  # slug starts with digits: no letters prefix, no fake code
    assert d2["badges"] == []


def test_extract_video_details_uncensored_leak_slug():
    # real missav suffix is "-uncensored-leak" (e.g. cawd-629-uncensored-leak);
    # "-uncensored-leaked" legacy spelling resolves to the same 无码破解 badge
    d = missav.extract_video_details(
        "<html></html>", "https://missav.ai/cn/stars-804-uncensored-leak")
    assert d["badges"] == ["无码破解"]
    d = missav.extract_video_details(
        "<html></html>", "https://missav.ai/cn/stars-804-uncensored-leaked")
    assert d["badges"] == ["无码破解"]


def test_build_caption_full_format():
    d = {
        "code": "DASS-629",
        "title": "想不想被我饲养？实录",
        "actresses_cn": ["百永纱里奈", "千石桃香"],
        "actresses": ["百永さりな", "千石もなか"],
        "genres": ["女同性恋", "潮吹", "多人运动"],
        "badges": ["中文字幕"],
    }
    cap = missav.build_caption(d)
    # caption v3：演员行 = 中文名 + 日文名同行（同名去重）
    assert cap == (
        "DASS-629\n\n"
        "想不想被我饲养？实录\n\n"
        "演员：#百永纱里奈 #千石桃香 #百永さりな #千石もなか\n"
        "标签：#女同性恋 #潮吹 #多人运动\n"
        "类别：#中文字幕"
    )


def test_build_caption_sanitizes_and_keeps_blank_skeleton():
    d = {
        "code": "ABP-1",
        "title": "",
        "actresses": ["Sarina Momonaga"],   # space -> underscore in hashtag
        "genres": [],
        "badges": ["无码"],
    }
    cap = missav.build_caption(d)
    assert cap == "ABP-1\n\n演员：#Sarina_Momonaga\n标签：\n类别：#无码"


def test_build_caption_trims_to_telegram_limit():
    d = {
        "code": "DASS-629",
        "title": "intro " * 5,
        "actresses": [f" Actress {i:03d} " for i in range(40)],
        "genres": [f" Genre {i:03d} " for i in range(40)],
        "badges": ["中文字幕"],
    }
    cap = missav.build_caption(d)
    assert len(cap) <= 1024
    assert cap.startswith("DASS-629")       # code line never trimmed
    assert cap.count("\n\n") == 2            # code / intro / tag-block layout kept
    assert "类别：" in cap                     # last tag line survives trimming


def test_build_caption_empty_details():
    assert missav.build_caption({}) == ""


def test_extract_video_details_release_date_and_genres_cap():
    d = missav.extract_video_details(REAL_PAGE, "https://missav.ai/cn/dass-629")
    assert d["release_date"] == "2025-05-09"
    assert d["studio"] == ""  # missav panel has no studio row: javbus fills it
    # D4 拍板「抓主要标签」: genres capped at the first 6
    page = REAL_PAGE.replace(
        '<a href="https://missav.live/dm166/cn/genres/%E5%A4%9A%E4%BA" class="text-nord13 font-medium">多人运动</a>',
        '<a href="https://missav.live/dm166/cn/genres/a" class="text-nord13 font-medium">多人运动</a>,\n'
        '   <a href="https://missav.live/dm167/cn/genres/b" class="text-nord13 font-medium">巨乳</a>,\n'
        '   <a href="https://missav.live/dm168/cn/genres/c" class="text-nord13 font-medium">中出</a>,\n'
        '   <a href="https://missav.live/dm169/cn/genres/d" class="text-nord13 font-medium">颜射</a>,\n'
        '   <a href="https://missav.live/dm170/cn/genres/e" class="text-nord13 font-medium">单体作品</a>')
    d2 = missav.extract_video_details(page, "https://missav.ai/cn/dass-629")
    assert d2["genres"] == ["苗条", "女同性恋", "潮吹", "多人运动", "巨乳", "中出"]
    assert "颜射" not in d2["genres"]  # beyond the cap: dropped


def test_build_caption_ignores_studio_and_release_date():
    # 用户定稿：caption 只保留 演员/标签/类别 三行结构，片商与发行日期不渲染
    # （javbus enrich 仍负责补中文名，studio/date 字段仅作数据保留）
    d = {
        "code": "DASS-629",
        "title": "想不想被我饲养？实录",
        "actresses_cn": ["桃永纱里奈"],
        "actresses": ["百永さりな"],
        "genres": ["苗条"],
        "studio": "プレステージ",
        "release_date": "2025-05-09",
        "badges": ["无码破解"],
    }
    cap = missav.build_caption(d)
    assert cap == (
        "DASS-629\n\n"
        "想不想被我饲养？实录\n\n"
        "演员：#桃永纱里奈 #百永さりな\n"
        "标签：#苗条\n"
        "类别：#无码破解"
    )
    assert "片商" not in cap
    d2 = {"code": "ABP-1", "title": "t", "release_date": "2025-05-09"}
    assert "片商" not in missav.build_caption(d2)


def test_segment_429_gets_extended_backoff(monkeypatch, tmp_path):
    """A CDN 429 must escalate to the long-backoff budget, not fail fast."""
    calls = {"n": 0}
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        calls["n"] += 1
        if calls["n"] <= 4:
            return FakeResp(429), None
        return FakeResp(content=b"x" * 188), None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    monkeypatch.setattr(missav.asyncio, "sleep", fake_sleep)
    path = asyncio.run(missav._download_one_segment(
        0, "https://surrit.com/seg-0.ts", str(tmp_path), None, None,
        {}, "surrit.com", [0]))


def test_segment_404_still_fails_fast(monkeypatch, tmp_path):
    calls = {"n": 0}

    async def fake_sleep(s):
        pass

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        calls["n"] += 1
        return FakeResp(404), None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    monkeypatch.setattr(missav.asyncio, "sleep", fake_sleep)
    with pytest.raises(missav.MissAVError, match="HTTP 404"):
        asyncio.run(missav._download_one_segment(
            0, "https://surrit.com/seg-0.ts", str(tmp_path), None, None,
            {}, "surrit.com", [0]))
    assert calls["n"] == missav.SEGMENT_RETRIES  # no escalation on 404


def test_segment_502_gets_deep_backoff_budget(monkeypatch, tmp_path):
    """A gateway brownout (502/503/504) must ride out with the deepest
    budget, not kill the job after 3 fast retries (live 2026-08-16:
    worldstatic 502s lasted 1-2 minutes mid-download)."""
    calls = {"n": 0}
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        calls["n"] += 1
        if calls["n"] <= 4:
            return FakeResp(502), None
        return FakeResp(content=b"x" * 188), None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    monkeypatch.setattr(missav.asyncio, "sleep", fake_sleep)
    path = asyncio.run(missav._download_one_segment(
        9, "https://static.worldstatic.com/seg-9.ts", str(tmp_path), None, None,
        {}, "static.worldstatic.com", [0]))
    assert path.endswith("000009.ts")
    assert calls["n"] == 5                       # 4x 502 then success
    assert sleeps == [2, 4, 8, 16]               # escalating, 502 cap 60s


def test_segment_502_exhausts_deep_budget(monkeypatch, tmp_path):
    calls = {"n": 0}

    async def fake_sleep(s):
        pass

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        calls["n"] += 1
        return FakeResp(502), None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    monkeypatch.setattr(missav.asyncio, "sleep", fake_sleep)
    with pytest.raises(missav.MissAVError, match="HTTP 502"):
        asyncio.run(missav._download_one_segment(
            0, "https://static.worldstatic.com/seg-0.ts", str(tmp_path), None, None,
            {}, "static.worldstatic.com", [0]))
    assert calls["n"] == missav.SEGMENT_RETRIES_SERVER_ERROR  # 8 attempts


def test_segment_connection_error_keeps_fast_budget(monkeypatch, tmp_path):
    """Network errors (status None) are not gateway brownouts: fast budget."""
    calls = {"n": 0}

    async def fake_sleep(s):
        pass

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        calls["n"] += 1
        return None, "connection reset"

    monkeypatch.setattr(missav, "_http_get", fake_get)
    monkeypatch.setattr(missav.asyncio, "sleep", fake_sleep)
    with pytest.raises(missav.MissAVError, match="connection reset"):
        asyncio.run(missav._download_one_segment(
            0, "https://surrit.com/seg-0.ts", str(tmp_path), None, None,
            {}, "surrit.com", [0]))
    assert calls["n"] == missav.SEGMENT_RETRIES  # fast budget, no escalation


# ─── page-level transient retries (curl-28 stall / gateway brownout) ─────────

def test_http_get_retry_transients_then_success(monkeypatch):
    """None (stall/timeout) and 502/503/504 retry; success returns as-is."""
    calls = {"n": 0}
    sleeps = []

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, "curl: (28) Operation too slow"
        if calls["n"] == 2:
            return FakeResp(502), None
        return FakeResp(content=b"ok"), None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    monkeypatch.setattr(missav.time, "sleep", lambda s: sleeps.append(s))
    resp, err = missav._http_get_retry("https://surrit.com/x")
    assert resp is not None and resp.content == b"ok" and err is None
    assert calls["n"] == 3
    assert sleeps == [3, 8]


def test_http_get_retry_exhausts_budget(monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        calls["n"] += 1
        return None, "curl: (28) stall"

    monkeypatch.setattr(missav, "_http_get", fake_get)
    monkeypatch.setattr(missav.time, "sleep", lambda s: sleeps.append(s))
    resp, err = missav._http_get_retry("https://surrit.com/x")
    assert resp is None and "curl" in err
    assert calls["n"] == missav.PAGE_RETRIES
    assert sleeps == list(missav.PAGE_RETRY_BACKOFF)


def test_http_get_retry_403_fails_fast(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        calls["n"] += 1
        return FakeResp(403), None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    monkeypatch.setattr(missav.time, "sleep", lambda s: pytest.fail("no sleep on 403"))
    resp, err = missav._http_get_retry("https://surrit.com/x")
    assert resp.status_code == 403
    assert calls["n"] == 1


def test_playlist_stall_retries_then_download_succeeds(monkeypatch, tmp_path):
    """Live regression (2026-08-16): a curl-28 stall on the m3u8 fetch used
    to kill the job instantly; it must ride the retry budget instead."""
    key = os.urandom(16)
    media_sequence = 5
    parts = [os.urandom(188 * 40), os.urandom(188 * 40)]

    def enc_part(i, data):
        iv = (i + media_sequence).to_bytes(16, "big")
        return _aes_crypt(_pkcs7(data), key, iv, encrypt=True)

    served = {
        "https://missav.ai/sone-543": FakeResp(text=_page_html("https://surrit.com/vid/master.m3u8")),
        "https://surrit.com/vid/master.m3u8": FakeResp(text=MASTER),
        "https://surrit.com/vid/1080/prog.m3u8": FakeResp(text=MEDIA),
        "https://surrit.com/vid/1080/enc.key": FakeResp(content=key),
        "https://surrit.com/vid/1080/seg-0.ts": FakeResp(content=enc_part(0, parts[0])),
        "https://surrit.com/vid/1080/seg-1.ts": FakeResp(content=enc_part(1, parts[1])),
    }
    master_stalls = {"n": 0}

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if url.endswith("master.m3u8"):
            master_stalls["n"] += 1
            if master_stalls["n"] <= 2:            # CDN brownout window
                return None, "curl: (28) Operation too slow"
        resp = served.get(url)
        return (resp, None) if resp else (FakeResp(404), None)

    monkeypatch.setattr(missav, "_http_get", fake_get)
    monkeypatch.setattr(missav.time, "sleep", lambda s: None)

    monkeypatch.setattr(missav, "remux_to_mp4", _concat_aware_remux)

    dest = tmp_path / "out.mp4"
    missav.asyncio.run(missav.download_missav("https://missav.ai/sone-543", str(dest)))
    assert dest.read_bytes() == b"".join(parts)
    assert master_stalls["n"] == 3               # stalled twice, third attempt OK


# ─── review hardening: redirect pin / code boundary / budget / disk ──────────

def test_subtitle_playlist_redirect_off_domain_rejected(monkeypatch, tmp_path):
    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        resp = FakeResp(text="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nx\n")
        resp.url = "https://evil.com/sub.vtt"  # redirect landed off-domain
        return resp, None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    assert asyncio.run(missav._fetch_subtitle_track(
        "https://surrit.com/sub.vtt", {"Referer": "https://missav.ai/"},
        "surrit.com", str(tmp_path))) is None


def test_subtitle_segment_redirect_off_domain_rejected(monkeypatch, tmp_path):
    playlist = ("#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                "#EXTINF:5.0,\nseg0.vtt\n#EXT-X-ENDLIST")

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        resp = FakeResp(text=playlist if url.endswith("track.m3u8")
                        else "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nx\n")
        if url.endswith("seg0.vtt"):
            resp.url = "https://evil.com/seg0.vtt"
        else:
            resp.url = url
        return resp, None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    assert asyncio.run(missav._fetch_subtitle_track(
        "https://surrit.com/track.m3u8", {"Referer": "https://missav.ai/"},
        "surrit.com", str(tmp_path))) is None


def test_getav_subtitle_redirect_off_domain_rejected(monkeypatch, tmp_path):
    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if url.endswith("/api/movies/SONE-543"):
            return FakeResp(text=json.dumps({
                "success": True,
                "data": {"id": "sone-543", "title": "SONE-543",
                         "videoSources": [{"type": "raw_1080p",
                                           "url": "https://static.worldstatic.com/v.m3u8"}],
                         "subtitles": [{"language": "zh", "format": "vtt",
                                        "filePath": "https://static.worldstatic.com/zh.vtt"}]}})), None
        resp = FakeResp(text="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nx\n",
                        content=b"WEBVTT cue body")
        resp.url = "https://evil.com/zh.vtt"
        return resp, None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    assert missav.find_getav_subtitle_for_code("SONE-543", str(tmp_path)) is None


def test_find_getav_subtitle_prefix_code_rejected(monkeypatch, tmp_path):
    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if url.endswith("/api/movies/SONE-54"):
            return FakeResp(text=json.dumps({
                "success": True,
                "data": {"id": "sone-543", "title": "SONE-543 作品",
                         "videoSources": [{"type": "raw_1080p",
                                           "url": "https://static.worldstatic.com/v.m3u8"}],
                         "subtitles": [{"language": "zh", "format": "vtt",
                                        "filePath": "https://static.worldstatic.com/zh.vtt"}]}})), None
        raise AssertionError(f"subtitle must not be fetched after mismatch: {url}")

    monkeypatch.setattr(missav, "_http_get", fake_get)
    assert missav.find_getav_subtitle_for_code("SONE-54", str(tmp_path)) is None


def test_subtitle_budget_counts_bytes_not_chars(monkeypatch, tmp_path):
    # 1500 CJK chars = 4500 UTF-8 bytes: over a 2000-byte budget even though
    # the char count (1500) stays under it
    monkeypatch.setattr(missav, "PAGE_MAX_BYTES", 2000)
    playlist = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:5.0,\nseg0.vtt\n#EXT-X-ENDLIST"

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if url.endswith("track.m3u8"):
            resp = FakeResp(text=playlist)
            resp.url = url
            return resp, None
        resp = FakeResp(text="あ" * 1500, content=("あ" * 1500).encode())
        resp.url = url
        return resp, None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    assert asyncio.run(missav._fetch_subtitle_track(
        "https://surrit.com/track.m3u8", {"Referer": "https://missav.ai/"},
        "surrit.com", str(tmp_path))) is None


def test_encrypted_segments_concurrent_budget_hard_stop(monkeypatch, tmp_path):
    # 2 workers x 6MB against a 10MB budget: with the check separated from
    # the reserve by the decrypt await, both workers passed and 12MB landed
    key = os.urandom(16)
    media_sequence = 0
    parts = [os.urandom(6 * 1024 * 1024) for _ in range(2)]

    def enc_part(i, data):
        iv = (i + media_sequence).to_bytes(16, "big")
        return _aes_crypt(_pkcs7(data), key, iv, encrypt=True)

    media_pl = MEDIA.replace(
        "seg-1.ts\n#EXT-X-ENDLIST",
        "seg-1.ts\n#EXTINF:5.0,\nseg-2.ts\n#EXT-X-ENDLIST")
    served = {
        "https://missav.ai/sone-543": FakeResp(text=_page_html("https://surrit.com/vid/master.m3u8")),
        "https://surrit.com/vid/master.m3u8": FakeResp(text=MASTER),
        "https://surrit.com/vid/1080/prog.m3u8": FakeResp(text=media_pl),
        "https://surrit.com/vid/1080/enc.key": FakeResp(content=key),
        "https://surrit.com/vid/1080/seg-0.ts": FakeResp(content=enc_part(0, parts[0])),
        "https://surrit.com/vid/1080/seg-1.ts": FakeResp(content=enc_part(1, parts[1])),
        "https://surrit.com/vid/1080/seg-2.ts": FakeResp(content=enc_part(2, parts[1])),
    }
    monkeypatch.setattr(missav, "_http_get",
                        lambda url, headers=None, timeout=None, max_bytes=None:
                        (served.get(url) or FakeResp(404), None))
    monkeypatch.setattr(missav, "MAX_TOTAL_BYTES", 10 * 1024 * 1024)

    async def fake_sleep(s):
        pass

    monkeypatch.setattr(missav.asyncio, "sleep", fake_sleep)
    with pytest.raises(missav.MissAVError, match="预算"):
        asyncio.run(missav.download_missav(
            "https://missav.ai/sone-543", str(tmp_path / "o.mp4"), concurrency=3))


def test_burn_stage_disk_recheck_aborts(monkeypatch, tmp_path):
    parts = [os.urandom(64) for _ in range(2)]
    served = {
        "https://missav.ai/sone-543": FakeResp(text=_page_html("https://surrit.com/vid/master.m3u8")),
        "https://surrit.com/vid/master.m3u8": FakeResp(text=MASTER),
        "https://surrit.com/vid/1080/prog.m3u8": FakeResp(text=MEDIA),
        "https://surrit.com/vid/1080/enc.key": FakeResp(content=os.urandom(16)),
        "https://surrit.com/vid/1080/seg-0.ts": FakeResp(content=os.urandom(64)),
        "https://surrit.com/vid/1080/seg-1.ts": FakeResp(content=os.urandom(64)),
    }
    calls = {"n": 0}

    class _Disk:
        free = 100 * 1024 ** 3

    def fake_disk(_):
        calls["n"] += 1
        if calls["n"] >= 2:  # admission passes, pre-merge recheck fails
            _Disk.free = 1 * 1024 ** 3
        return shutil.disk_usage("/", ) if False else _Disk

    monkeypatch.setattr(missav.shutil, "disk_usage", fake_disk)
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None:
        (served.get(url) or FakeResp(404), None))
    with pytest.raises(missav.MissAVError, match="磁盘剩余空间不足"):
        asyncio.run(missav.download_missav(
            "https://missav.ai/sone-543", str(tmp_path / "o.mp4")))
    assert calls["n"] >= 2


def test_cover_url_allowed_allowlist():
    assert missav.cover_url_allowed("https://missav.ai/pic.jpg")
    assert missav.cover_url_allowed("https://www.missav.ws/pic.jpg")
    assert missav.cover_url_allowed("https://surrit.com/x.jpg")
    assert missav.cover_url_allowed("https://static.worldstatic.com/c.jpg")
    assert missav.cover_url_allowed("https://getav.net/c.jpg")
    assert not missav.cover_url_allowed("https://evil.com/x.jpg")
    assert not missav.cover_url_allowed("http://169.254.169.254/latest/meta")
    assert not missav.cover_url_allowed("http://localhost/x.jpg")
    assert not missav.cover_url_allowed("ftp://missav.ai/x.jpg")
    assert not missav.cover_url_allowed(None)


def test_hashtag_maps_markdown_specials():
    assert missav._hashtag("a[b](c)`d`*e|f") == "#a_b_(c)_d_e_f"


def test_build_caption_blank_skeleton_from_sparse_details():
    """FC2 等无演员/标签面板的页面：骨架留空占位，结构恒定（用户定稿）。"""
    cap = missav.build_caption({
        "code": "FC2-PPV-2761664",
        "title": "【無码破解】FC2 作品标题 中文字幕",
        "badges": ["无码破解", "中文字幕"],
    })
    assert cap == (
        "FC2-PPV-2761664\n\n"
        "【無码破解】FC2 作品标题 中文字幕\n\n"
        "演员：\n"
        "标签：\n"
        "类别：#无码破解 #中文字幕"
    )


