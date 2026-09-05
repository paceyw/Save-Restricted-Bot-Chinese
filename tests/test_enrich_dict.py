"""Offline integration tests: javbus enrich × av 词库（契约集成点 B）。

No network, no mongo: ``javbus._http_get`` is stubbed per the
test_javbus convention; avdict 的两个集合 seam（``_cols`` /
``_code_col``）注入 dict-backed FakeCol（与 test_avdict 同款）。
"""

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

# config hard-requires these keys at import time — same defaults as test_missav.
os.environ.setdefault("MASTER_KEY", "enrich-dict-test-master")
os.environ.setdefault("IV_KEY", "enrich-dict-test-iv")

from utils import avdict  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "javbus_enrichdict", SRC / "utils" / "javbus.py")
javbus = importlib.util.module_from_spec(spec)
sys.modules["javbus_enrichdict"] = javbus
spec.loader.exec_module(javbus)


class _Result:
    matched_count = 1
    modified_count = 1
    upserted_id = None


class _FakeCursor:
    """find() 链式游标最小面：sort/skip/limit + 迭代。"""

    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction):
        self._docs.sort(key=lambda d: d.get(key) or 0, reverse=direction < 0)
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeCol:
    """dict-backed 最小 mongo collection：find_one / update_one。"""

    def __init__(self, rows=None, fail=False):
        self.rows = dict(rows or {})  # _id -> doc
        self.fail = fail
        self.calls = []

    def _check(self):
        if self.fail:
            raise RuntimeError("mongo down")

    def find_one(self, query):
        self._check()
        self.calls.append(("find_one", dict(query)))
        if "_id" in query:
            return self.rows.get(query["_id"])
        # 数组字段命中（如 {"aliases": key}）
        for field, value in query.items():
            for doc in self.rows.values():
                v = doc.get(field)
                if isinstance(v, list) and value in v:
                    return doc
        return None

    def find(self, query):
        self._check()
        self.calls.append(("find", dict(query)))
        return _FakeCursor(list(self.rows.values()))

    def count_documents(self, query):
        self._check()
        return len(self.rows)

    def update_one(self, query, update, upsert=False):
        self._check()
        self.calls.append(("update_one", dict(query), update, upsert))
        _id = query.get("_id")
        doc = self.rows.setdefault(_id, {"_id": _id})
        for field, value in update.get("$set", {}).items():
            doc[field] = value
        for field, value in update.get("$setOnInsert", {}).items():
            doc.setdefault(field, value)
        for field, delta in update.get("$inc", {}).items():
            doc[field] = doc.get(field, 0) + delta
        for field, value in update.get("$addToSet", {}).items():
            lst = doc.setdefault(field, [])
            if value not in lst:
                lst.append(value)
        return _Result()


class FakeResp:
    def __init__(self, status=200, text="", url=None):
        self.status_code = status
        self.text = text
        self.url = url


# javbus zh-CN 页：star 名为中文译名（无译名的保持假名）；genre 带 JP 别名
JAVBUS_CN_PAGE = """
<html><head><title>DASS-629 テスト - JavBus</title></head><body>
<div class="container"><div class="movie">
 <a class="bigImage" href="https://www.javbus.com/pics/cover/dass-629.jpg"><img src="n.jpg"></a>
</div>
<h3>DASS-629 テスト</h3>
<div class="col-md-3 info">
 <p><span class="header">片商:</span> <a href="https://www.javbus.com/studio/1">SOD create</a></p>
 <p><span class="header">類別:</span>
  <a href="https://www.javbus.com/genre/g1">中出し</a>
  <a href="https://www.javbus.com/genre/g2">巨乳</a>
 </p>
 <p><span class="header">演員:</span>
  <a href="https://www.javbus.com/star/a">百永纱里奈</a>
  <a href="https://www.javbus.com/star/b">桃乃木かな</a>
 </p>
</div></div></body></html>
"""


def _serve_javbus(monkeypatch):
    """javbus 域名给中文页，其余（javlibrary 兜底源）一律 403。"""

    def fake_get(url, timeout=None, max_bytes=None):
        if "javbus.com" in url:
            return FakeResp(200, JAVBUS_CN_PAGE), None
        return FakeResp(403, "Just a moment..."), None

    monkeypatch.setattr(javbus, "_http_get", fake_get)


def _all_fail(monkeypatch):
    """所有网络源（javbus/javlibrary/missav-cn 探测）全部失败。"""

    def fake_get(url, timeout=None, max_bytes=None):
        return FakeResp(403, "Just a moment..."), None

    monkeypatch.setattr(javbus, "_http_get", fake_get)


def _details(**over):
    d = {
        "code": "DASS-629",
        "title": "",
        "actresses": ["百永さりな"],
        "actresses_cn": [],
        "genres": [],
        "badges": [],
        "studio": "",
        "release_date": "",
    }
    d.update(over)
    return d


@pytest.fixture(autouse=True)
def _clean_cache():
    javbus._cache_clear()
    yield
    javbus._cache_clear()


@pytest.fixture
def cols(monkeypatch):
    """注入 (actress, tag, code) 三个 FakeCol 并接好词库的 mongo seam。"""
    actress, tag, code = FakeCol(), FakeCol(), FakeCol()
    monkeypatch.setattr(avdict, "_cols", lambda: (actress, tag))
    monkeypatch.setattr(avdict, "_code_col", lambda: code)
    return actress, tag, code


# ─── actresses / actresses_cn 分离 ─────────────────────────────────────────────

def test_javbus_cn_names_go_to_actresses_cn_without_pairing(cols, monkeypatch):
    _serve_javbus(monkeypatch)
    d = _details(title="已有标题")
    out = javbus.enrich_details(d, "https://missav.ai/cn/dass-629")
    assert out is d
    assert d["actresses"] == ["百永さりな"]  # 源 JP 名不拼接、不覆盖
    assert d["actresses_cn"] == ["百永纱里奈", "桃乃木かな"]
    assert d["title"] == "已有标题"  # 只补缺不覆盖


def test_existing_actresses_cn_merged_with_javbus_first(cols, monkeypatch):
    """getav starsZh 已填 actresses_cn 时与 javbus CN 名合并去重（javbus 优先序）。"""
    _serve_javbus(monkeypatch)
    d = _details(actresses_cn=["桃乃木香奈"])
    javbus.enrich_details(d, None)
    assert d["actresses_cn"] == ["百永纱里奈", "桃乃木かな", "桃乃木香奈"]


# ─── 词库学习（jp/cn 序位对回写）───────────────────────────────────────────────

def test_positional_pairs_learned_into_dict(cols, monkeypatch):
    _serve_javbus(monkeypatch)
    d = _details(actresses=["百永さりな", "桃乃木かな"])
    javbus.enrich_details(d, None)
    row = actress_row = cols[0].rows["百永さりな"]
    assert actress_row["cn"] == "百永纱里奈"
    assert "javbus" in actress_row["sources"]
    assert actress_row["hits"] == 1
    # 假名与中文名同形（桃乃木かな）：无对照价值，不入库
    assert "桃乃木かな" not in cols[0].rows
    assert row is actress_row  # noqa: F841 — 别名仅保证上面断言读的是同一行


# ─── genres 归一（词库选择）────────────────────────────────────────────────────

def test_genres_normalized_and_categories_derived(cols, monkeypatch):
    _all_fail(monkeypatch)
    d = _details(title="t", genres=["中出し", "巨乳", "未知标签"])
    javbus.enrich_details(d, None)
    assert d["genres"] == ["中出", "巨乳", "未知标签"]  # 别名归一，unknown 原样（同频保序）


# ─── code_meta 快照：成功存 / 全失败补缺 ───────────────────────────────────────

def test_successful_enrich_saves_code_meta(cols, monkeypatch):
    _serve_javbus(monkeypatch)
    javbus.enrich_details(_details(), None)
    snap = avdict.code_meta_load("DASS-629")
    assert snap is not None
    assert snap["actresses_cn"] == ["百永纱里奈", "桃乃木かな"]
    assert snap["studio"] == "SOD create"
    assert snap["genres"] == ["中出", "巨乳"]


def test_all_sources_fail_replays_snapshot_fill_only(cols, monkeypatch):
    cols[2].rows["DASS-629"] = {
        "_id": "DASS-629",
        "updated_at": datetime.now(timezone.utc),
        "details": {
            "title": "快照标题",
            "studio": "プレステージ",
            "release_date": "2025-05-09",
            "genres": ["中出し"],
        },
    }
    _all_fail(monkeypatch)
    d = _details(title="自己的标题", studio="已有片商")
    javbus.enrich_details(d, None)
    assert d["title"] == "自己的标题"  # 只补缺不覆盖
    assert d["studio"] == "已有片商"
    assert d["release_date"] == "2025-05-09"
    assert d["genres"] == ["中出"]  # 补到的 genres 也过 select_tags


# ─── 库故障不响下载 ─────────────────────────────────────────────────────────────

def test_dict_layer_failure_does_not_break_enrich(monkeypatch):
    boom = FakeCol(fail=True)
    monkeypatch.setattr(avdict, "_cols", lambda: (boom, boom))
    monkeypatch.setattr(avdict, "_code_col", lambda: boom)
    _serve_javbus(monkeypatch)
    d = _details()
    out = javbus.enrich_details(d, None)
    assert out is d  # 不抛出，网络 enrich 照常产出
    assert d["actresses_cn"] == ["百永纱里奈", "桃乃木かな"]
    assert d["genres"] == ["中出", "巨乳"]  # 种子映射兜底，不依赖库
    assert d["studio"] == "SOD create"
    assert boom.rows == {}  # 学习/快照写入静默失败，无脏数据


# ─── 修复回归：学习守卫 / 词库读路径 / hit 语义 ────────────────────────────────

def test_unequal_lengths_skip_learning(cols, monkeypatch):
    """两侧数量不一致（跨站排序不保证）时不得按位学习，防错误映射永久入库。"""
    page = JAVBUS_CN_PAGE  # 2 个 star 名
    html = page.replace('<a href="https://www.javbus.com/star/b">桃乃木かな</a>', "")
    def fake_get(url, timeout=None, max_bytes=None):
        if "javbus.com" in url:
            return FakeResp(200, html), None
        return FakeResp(403, "Just a moment..."), None
    monkeypatch.setattr(javbus, "_http_get", fake_get)
    d = _details(actresses=["百永さりな", "桃乃木かな"])  # 2 JP vs 1 CN
    javbus.enrich_details(d, None)
    assert d["actresses_cn"] == ["百永纱里奈"]  # 展示照常
    assert cols[0].rows == {}                   # 词库零写入（守卫生效）


def test_library_lookup_fills_actresses_cn(cols, monkeypatch):
    """词库读路径：源 JP 名在库内有对照时补进 actresses_cn（生产主消费点）。"""
    cols[0].rows["百永さりな"] = {"_id": "百永さりな", "cn": "百永纱里奈", "aliases": ["百永さりな"]}
    _all_fail(monkeypatch)
    d = _details(title="t")
    javbus.enrich_details(d, None)
    assert d["actresses_cn"] == ["百永纱里奈"]


def test_cover_only_hit_still_replays_snapshot(cols, monkeypatch):
    """仅封面的解析结果不算命中：不抑制 code_meta 快照回放。"""
    cover_only = '''
<html><body>
<div class="container"><a class="bigImage" href="https://www.javbus.com/pics/x.jpg"><img src="n.jpg"></a></div>
</body></html>'''
    def fake_get(url, timeout=None, max_bytes=None):
        if "javbus.com" in url:
            return FakeResp(200, cover_only), None
        return FakeResp(403, "Just a moment..."), None
    monkeypatch.setattr(javbus, "_http_get", fake_get)
    cols[2].rows["DASS-629"] = {
        "_id": "DASS-629",
        "details": {"title": "快照标题", "studio": "快照片商"},
        "updated_at": datetime.now(timezone.utc),
    }
    d = _details()
    javbus.enrich_details(d, None)
    assert d["title"] == "快照标题"
    assert d["studio"] == "快照片商"
