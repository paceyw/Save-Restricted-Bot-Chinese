"""Offline tests for the JavBus metadata enrichment (issue #21 D4+D5).

No network: ``javbus._http_get`` is monkeypatched with fixture-serving
fakes, same convention as tests/test_missav.py. The missav /cn/ fallback
path goes through the real utils.missav parsers (pure), so only the HTTP
seam is substituted.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]

# utils.missav (lazily imported by the /cn/ fallback) pulls config, which
# hard-requires these keys at import time — same defaults as test_missav.
os.environ.setdefault("MASTER_KEY", "missav-test-master")
os.environ.setdefault("IV_KEY", "missav-test-iv")

spec = importlib.util.spec_from_file_location("javbus_mod", SRC / "utils" / "javbus.py")
javbus = importlib.util.module_from_spec(spec)
sys.modules["javbus_mod"] = javbus
spec.loader.exec_module(javbus)


class FakeResp:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text


@pytest.fixture(autouse=True)
def _clean_cache():
    javbus._cache_clear()
    yield
    javbus._cache_clear()


# trimmed/reshaped from a real www.javbus.com movie page (2026-09):
# labelled <p class="header"> rows + bigImage cover + /star/ actress links
JAVBUS_PAGE = """
<html><head><title>DASS-629 私に飼われてみない？ - JavBus</title></head><body>
<div class="container">
 <div class="movie">
  <a class="bigImage" href="https://www.javbus.com/pics/cover/dass-629.jpg"><img src="n.jpg"></a>
 </div>
 <h3>私に飼われてみない？ 実録。 DASS-629</h3>
 <div class="col-md-3 info">
  <p><span class="header">識別碼:</span> DASS-629</p>
  <p><span class="header">發行日期:</span> 2025-05-09</p>
  <p><span class="header">片商:</span> <a href="https://www.javbus.com/studio/167" target="_blank">プレステージ</a></p>
  <p><span class="header">類別:</span>
   <a href="https://www.javbus.com/genre/e1">苗条</a>
   <a href="https://www.javbus.com/genre/e2">女同性恋</a>
   <a href="https://www.javbus.com/genre/e3">潮吹</a>
   <a href="https://www.javbus.com/genre/e4">多人运动</a>
   <a href="https://www.javbus.com/genre/e5">单体作品</a>
   <a href="https://www.javbus.com/genre/e6">巨乳</a>
   <a href="https://www.javbus.com/genre/e7">中出</a>
   <a href="https://www.javbus.com/genre/e8">颜射</a>
  </p>
  <p><span class="header">演員:</span>
   <a href="https://www.javbus.com/star/abc"><img src="a.jpg">百永さりな</a>
   <a href="https://www.javbus.com/star/def">桃乃木かな</a>
  </p>
 </div>
</div>
</body></html>
"""

# missav /cn/ variant page: actresses panel carries the Chinese name
MISSAV_CN_PAGE = """
<html><head><meta property="og:title" content="DASS-629 テスト - 百永さりな"></head><body>
<div class="space-y-2">
 <div class="text-secondary"><span>女优:</span>
   <a href="https://missav.ai/dm71/cn/actresses/x" class="font-medium">桃永紗里奈</a>
 </div>
</div>
</body></html>
"""


def _serve_javbus_page(monkeypatch, page=None):
    served = []

    def fake_get(url, timeout=None, max_bytes=None):
        served.append(url)
        return FakeResp(200, page if page is not None else JAVBUS_PAGE), None

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    return served


# ─── fetch_javbus_meta: parse + degrade ────────────────────────────────────────

def test_fetch_javbus_meta_parses_all_fields(monkeypatch):
    served = _serve_javbus_page(monkeypatch)
    meta = javbus.fetch_javbus_meta("DASS-629")
    assert served == ["https://www.javbus.com/DASS-629"]
    assert meta["title"] == "私に飼われてみない？ 実録。 DASS-629"
    assert meta["studio"] == "プレステージ"
    assert meta["release_date"] == "2025-05-09"
    # D4 拍板「抓主要标签」：8 个类型截前 6 个
    assert meta["genres"] == ["苗条", "女同性恋", "潮吹", "多人运动", "单体作品", "巨乳"]
    assert meta["cover"] == "https://www.javbus.com/pics/cover/dass-629.jpg"
    # <img> inside the anchor must not leak into the name
    assert meta["actresses"] == ["百永さりな", "桃乃木かな"]


def test_fetch_javbus_meta_title_falls_back_to_title_tag(monkeypatch):
    page = JAVBUS_PAGE.replace(
        "<h3>私に飼われてみない？ 実録。 DASS-629</h3>", "")
    _serve_javbus_page(monkeypatch, page)
    meta = javbus.fetch_javbus_meta("DASS-629")
    assert meta["title"] == "DASS-629 私に飼われてみない？"


def test_fetch_javbus_meta_partial_page_degrades_independently(monkeypatch):
    page = ('<html><body><div class="info">'
            '<p><span class="header">片商:</span> '
            '<a href="/studio/1">SOD create</a></p></div></body></html>')
    _serve_javbus_page(monkeypatch, page)
    meta = javbus.fetch_javbus_meta("ABP-123")
    assert meta is not None
    assert meta["studio"] == "SOD create"
    assert meta["title"] == "" and meta["genres"] == [] and meta["actresses"] == []


def test_fetch_javbus_meta_unparseable_page_returns_none(monkeypatch):
    _serve_javbus_page(monkeypatch, "<html><body>plain page</body></html>")
    assert javbus.fetch_javbus_meta("DASS-629") is None


def test_fetch_javbus_meta_cf_blocked_returns_none(monkeypatch):
    def fake_get(url, timeout=None, max_bytes=None):
        return FakeResp(403, "Just a moment..."), None

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    assert javbus.fetch_javbus_meta("DASS-629") is None


def test_fetch_javbus_meta_timeout_returns_none(monkeypatch):
    monkeypatch.setattr(
        javbus, "_http_get",
        lambda url, timeout=None, max_bytes=None: (None, "timeout"))
    assert javbus.fetch_javbus_meta("DASS-629") is None


def test_fetch_javbus_meta_404_returns_none(monkeypatch):
    monkeypatch.setattr(
        javbus, "_http_get",
        lambda url, timeout=None, max_bytes=None: (FakeResp(404), None))
    assert javbus.fetch_javbus_meta("DASS-629") is None


def test_fetch_javbus_meta_never_raises_on_seam_violation(monkeypatch):
    def fake_get(url, timeout=None, max_bytes=None):
        raise RuntimeError("contract violation")

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    assert javbus.fetch_javbus_meta("DASS-629") is None


def test_fetch_javbus_meta_bad_code_never_hits_http(monkeypatch):
    def fake_get(url, timeout=None, max_bytes=None):
        raise AssertionError(f"no HTTP for a bad code: {url}")

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    assert javbus.fetch_javbus_meta("") is None
    assert javbus.fetch_javbus_meta(None) is None
    assert javbus.fetch_javbus_meta("../../etc/passwd") is None
    assert javbus.fetch_javbus_meta("has space") is None


# ─── LRU cache ─────────────────────────────────────────────────────────────────

def test_lru_cache_hit_avoids_second_fetch(monkeypatch):
    served = _serve_javbus_page(monkeypatch)
    first = javbus.fetch_javbus_meta("DASS-629")
    # lowercase input normalizes onto the same cached entry
    second = javbus.fetch_javbus_meta("dass-629")
    assert served == ["https://www.javbus.com/DASS-629"]  # one fetch only
    assert second is first


def test_lru_cache_evicts_oldest(monkeypatch):
    served = _serve_javbus_page(monkeypatch)
    monkeypatch.setattr(javbus, "_CACHE_MAX", 2)
    javbus.fetch_javbus_meta("AAA-001")
    javbus.fetch_javbus_meta("BBB-002")
    javbus.fetch_javbus_meta("CCC-003")     # evicts AAA-001
    javbus.fetch_javbus_meta("AAA-001")     # must refetch
    codes = [u.rsplit("/", 1)[-1] for u in served]
    assert codes == ["AAA-001", "BBB-002", "CCC-003", "AAA-001"]


def test_lru_cache_does_not_stick_failures(monkeypatch):
    state = {"ok": False}

    def fake_get(url, timeout=None, max_bytes=None):
        return (FakeResp(200, JAVBUS_PAGE), None) if state["ok"] \
            else (FakeResp(403, "Just a moment..."), None)

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    assert javbus.fetch_javbus_meta("DASS-629") is None   # blocked
    state["ok"] = True
    meta = javbus.fetch_javbus_meta("DASS-629")           # retried, not cached-None
    assert meta and meta["studio"] == "プレステージ"


# ─── D5: dual-name formatting ──────────────────────────────────────────────────

def test_pair_names_dual_single_and_shortfall():
    assert javbus._pair_names(["百永さりな"], ["桃永紗里奈"]) == ["桃永紗里奈 (百永さりな)"]
    # same name on both sites: keep the single original
    assert javbus._pair_names(["百永さりな"], ["百永さりな"]) == ["百永さりな"]
    # fewer CN names: leftover originals stay untouched
    assert javbus._pair_names(
        ["百永さりな", "千石もなか"], ["桃永紗里奈"]) == ["桃永紗里奈 (百永さりな)", "千石もなか"]


# ─── enrich_details: wiring-level behaviour ────────────────────────────────────

def _details(**over):
    d = {
        "code": "DASS-629",
        "title": "想不想被我饲养？实录",
        "actresses": ["百永さりな"],
        "genres": ["苗条"],
        "badges": [],
        "studio": "",
        "release_date": "",
    }
    d.update(over)
    return d


def test_enrich_details_fills_missing_fields_from_javbus(monkeypatch):
    _serve_javbus_page(monkeypatch)
    d = _details()
    out = javbus.enrich_details(d, "https://missav.ai/cn/dass-629")
    assert out is d
    assert d["studio"] == "プレステージ"
    assert d["release_date"] == "2025-05-09"
    # positional pairing: javbus name[0] equals the missav name -> single
    assert d["actresses"] == ["百永さりな"]
    # source-page genres are authoritative: never overwritten
    assert d["genres"] == ["苗条"]


def test_enrich_details_fills_empty_genres_and_title_from_javbus(monkeypatch):
    _serve_javbus_page(monkeypatch)
    d = _details(title="", genres=[], actresses=[])
    javbus.enrich_details(d, None)
    assert d["title"] == "私に飼われてみない？ 実録。 DASS-629"
    assert len(d["genres"]) == javbus.GENRES_MAX


def test_enrich_details_falls_back_to_missav_cn_page(monkeypatch):
    def fake_get(url, timeout=None, max_bytes=None):
        if "javbus.com" in url:
            return FakeResp(403, "Just a moment..."), None
        assert url == "https://missav.ai/cn/dass-629"  # lang swapped, slug kept
        return FakeResp(200, MISSAV_CN_PAGE), None

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    d = _details()
    javbus.enrich_details(d, "https://missav.ai/en/dass-629")
    assert d["actresses"] == ["桃永紗里奈 (百永さりな)"]
    assert d["studio"] == ""  # javbus blocked: no studio fill


def test_enrich_details_skips_cn_probe_for_cn_url(monkeypatch):
    def fake_get(url, timeout=None, max_bytes=None):
        if "javbus.com" not in url:
            raise AssertionError(f"cn page must not be re-probed: {url}")
        return FakeResp(403, "Just a moment..."), None

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    d = _details()
    javbus.enrich_details(d, "https://missav.ai/cn/dass-629")
    assert d["actresses"] == ["百永さりな"]


def test_enrich_details_silent_when_everything_fails(monkeypatch):
    def fake_get(url, timeout=None, max_bytes=None):
        return FakeResp(403, "Just a moment..."), None

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    d = _details()
    javbus.enrich_details(d, "https://youtube.com/watch?v=x")
    assert d == _details()


def test_enrich_details_no_code_no_http(monkeypatch):
    def fake_get(url, timeout=None, max_bytes=None):
        raise AssertionError(f"no HTTP without a code: {url}")

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    d = {"code": "", "title": "t", "actresses": [], "genres": [], "badges": []}
    javbus.enrich_details(d, None)
    assert d["title"] == "t"


def test_enrich_details_never_raises(monkeypatch):
    def fake_get(url, timeout=None, max_bytes=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    d = _details()
    assert javbus.enrich_details(d, "https://missav.ai/en/dass-629") is d
    assert javbus.enrich_details(None) is None
    assert javbus.enrich_details("not-a-dict") == "not-a-dict"


def test_fetch_javbus_meta_redirect_off_domain_rejected(monkeypatch):
    """重定向到 javbus.com 之外的最终响应必须整体拒绝（review）。"""
    class _R:
        status_code = 200
        text = "<html><title>x</title></html>"
        content = b"x"
        url = "https://evil.com/dass-629"

    def fake_get(url, timeout=None, max_bytes=None):
        page = javbus._Page(200, b"<html></html>", None, url="https://evil.com/dass-629")
        return page, None

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    assert javbus.fetch_javbus_meta("DASS-629") is None


def test_fetch_javbus_meta_same_host_redirect_accepted(monkeypatch):
    """最终 host 仍是 javbus.com（www 归一）时正常解析。"""
    page = javbus._Page(
        200,
        b"<html><title>DASS-629</title></html>",
        None,
        url="https://www.javbus.com/DASS-629",
    )

    def fake_get(url, timeout=None, max_bytes=None):
        return page, None

    monkeypatch.setattr(javbus, "_http_get", fake_get)
    meta = javbus.fetch_javbus_meta("DASS-629")
    assert meta is not None and meta["title"].startswith("DASS-629")


def test_enrich_fc2_code_skips_javbus_uses_getav(monkeypatch):
    """FC2：JavBus 必 404，直接走 getav 详情兜底（title/genres/演员填充）。

    getav 中文名与 missav 日文名按位配对成「中文名 (日文名)」。
    """
    import types

    missav_stub = types.ModuleType("utils.missav")

    def fake_getav(code):
        assert code == "FC2-PPV-2761664"
        return {"code": "FC2-PPV-2761664", "title": "FC2 中文标题",
                "actresses": ["中文演员"], "genres": ["素人"], "badges": []}

    missav_stub.find_getav_details_for_code = fake_getav
    monkeypatch.setitem(sys.modules, "utils.missav", missav_stub)

    javbus_calls = []

    def fake_javbus(code):
        javbus_calls.append(code)
        return None

    monkeypatch.setattr(javbus, "fetch_javbus_meta", fake_javbus)

    d = {"code": "FC2-PPV-2761664", "title": "", "actresses": ["JP Name"], "genres": []}
    out = javbus.enrich_details(
        d, "https://missav.ai/fc2-ppv-2761664-uncensored-leak-chinese-subtitle")
    assert javbus_calls == []                  # never hit javbus for FC2
    assert out["title"] == "FC2 中文标题"
    assert out["genres"] == ["素人"]
    assert out["actresses"] == ["中文演员 (JP Name)"]
