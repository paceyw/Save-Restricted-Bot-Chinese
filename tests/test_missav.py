"""Offline tests for the missav downloader (issue #13).

No network: ``_http_get`` is monkeypatched with fixture-serving fakes.
No ffmpeg required: the merge path is verified byte-for-byte with a
stubbed remux, and the missing-ffmpeg guard is exercised for real.
"""

import asyncio
import importlib.util
import os
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
            return FakeResp(text=_page_html("https://surrit.com/a/playlist.m3u8")), None
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

    async def fake_remux(src, dst):
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            fo.write(fi.read())

    monkeypatch.setattr(missav, "remux_to_mp4", fake_remux)

    events = []

    async def progress(done, total, stage):
        events.append((stage, done, total))

    dest = tmp_path / "out.mp4"
    meta = asyncio.run(
        missav.download_missav("https://missav.ai/sone-543", str(dest), progress=progress)
    )

    assert dest.read_bytes() == b"".join(parts)  # byte-exact decrypt + ordered merge
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

    def fake_get(url, headers=None, timeout=None, max_bytes=None):
        if url.endswith("sone-543"):
            return FakeResp(text=_page_html("https://surrit.com/vid/master.m3u8")), None
        if url.endswith("master.m3u8"):
            return FakeResp(text=MASTER), None
        if url.endswith("prog.m3u8"):
            return FakeResp(text=media_pl), None
        if url.endswith("seg-0.ts"):
            calls["n"] += 1
            return FakeResp(status=502), None  # always fails -> 3 retries then error
        if url.endswith("enc.key"):
            return FakeResp(content=b"k" * 16), None
        return FakeResp(content=b"x" * 32), None

    monkeypatch.setattr(missav, "_http_get", fake_get)
    monkeypatch.setattr(
        missav, "remux_to_mp4",
        lambda src, dst: asyncio.sleep(0, result=None),
    )

    dest = tmp_path / "out.mp4"
    with pytest.raises(missav.MissAVError, match="片段 0 下载失败"):
        asyncio.run(missav.download_missav("https://missav.ai/sone-543", str(dest)))
    assert calls["n"] == missav.SEGMENT_RETRIES  # retry budget respected
    assert not dest.exists()
    # failure path must not leak segment temp dirs next to the output
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == []


def test_remux_missing_ffmpeg_reports_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(missav.shutil, "which", lambda name: None)
    with pytest.raises(missav.MissAVError, match="ffmpeg"):
        asyncio.run(missav.remux_to_mp4(str(tmp_path / "a.ts"), str(tmp_path / "a.mp4")))


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


def test_extract_video_details_uncensored_leaked_slug():
    d = missav.extract_video_details(
        "<html></html>", "https://missav.ai/cn/stars-804-uncensored-leaked")
    assert d["badges"] == ["无码流出"]


def test_build_caption_full_format():
    d = {
        "code": "DASS-629",
        "title": "想不想被我饲养？实录",
        "actresses": ["百永さりな", "千石もなか"],
        "genres": ["女同性恋", "潮吹", "多人运动"],
        "badges": ["中文字幕"],
    }
    cap = missav.build_caption(d)
    assert cap == (
        "DASS-629\n\n"
        "想不想被我饲养？实录\n\n"
        "演员：#百永さりな #千石もなか\n"
        "标签：#女同性恋 #潮吹 #多人运动\n"
        "类别：#中文字幕"
    )


def test_build_caption_sanitizes_and_skips_empty_blocks():
    d = {
        "code": "ABP-1",
        "title": "",
        "actresses": ["Sarina Momonaga"],   # space -> underscore in hashtag
        "genres": [],
        "badges": ["无码"],
    }
    cap = missav.build_caption(d)
    assert cap == "ABP-1\n\n演员：#Sarina_Momonaga\n类别：#无码"


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
