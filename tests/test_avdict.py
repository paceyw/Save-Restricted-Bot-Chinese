"""Offline tests for the av dict layer (utils/avdict.py + utils/avdict_seed.py).

No network, no mongo: both collection seams (`avdict._cols` /
`avdict._code_col`) are monkeypatched with dict-backed fakes per the
AVDICT_CONTRACT test convention. Seed-data consistency is asserted from
the module plus an AST pass (catches silent duplicate literal keys).
"""

import ast
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

# config hard-requires these keys at import time — same defaults as test_missav
os.environ.setdefault("MASTER_KEY", "avdict-test-master")
os.environ.setdefault("IV_KEY", "avdict-test-iv")

from utils import avdict, avdict_seed  # noqa: E402


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

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

    def _matches(self, doc, query):
        """评估非 _id 条件（$in / $exists / 等值）；真实 mongo 语义的最小面。"""
        for field, cond in query.items():
            if field == "_id":
                continue
            value = doc.get(field)
            if isinstance(cond, dict):
                if "$in" in cond and value not in cond["$in"]:
                    return False
                if "$exists" in cond and (value is not None) != cond["$exists"]:
                    return False
            elif value != cond:
                return False
        return True

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
        existing = self.rows.get(_id)
        if existing is not None and not self._matches(existing, query):
            return _Result()  # 条件不匹配：no-op（服务端原子语义的最小模拟）
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


@pytest.fixture(autouse=True)
def _reset_breaker(monkeypatch):
    """每个用例重置熔断器：fail 场景跳闸不外溢到后续测试。"""
    monkeypatch.setattr(avdict, "_down_until", 0.0)


@pytest.fixture
def cols(monkeypatch):
    """注入 (actress, tag, code) 三个 FakeCol 并接好词库的 mongo seam。"""
    actress, tag, code = FakeCol(), FakeCol(), FakeCol()
    monkeypatch.setattr(avdict, "_cols", lambda: (actress, tag))
    monkeypatch.setattr(avdict, "_code_col", lambda: code)
    return actress, tag, code


# ---------------------------------------------------------------------------
# actress_cn / record_actress
# ---------------------------------------------------------------------------

def test_actress_cn_hit_by_id_and_alias(cols):
    actress, _, _ = cols
    actress.rows["百永さりな"] = {
        "_id": "百永さりな", "aliases": ["百永サリナ"], "cn": "百永纱里奈",
    }
    assert avdict.actress_cn("百永さりな") == "百永纱里奈"
    assert avdict.actress_cn("百永サリナ") == "百永纱里奈"  # 变体命中
    assert avdict.actress_cn("  百永さりな ") == "百永纱里奈"  # 键归一后命中


def test_actress_cn_miss_and_empty(cols):
    actress, _, _ = cols
    assert avdict.actress_cn("无人子") == ""
    assert avdict.actress_cn("") == ""
    assert avdict.actress_cn(None) == ""  # 非字符串安全
    assert actress.rows == {}  # 只读查询不落库


def test_record_actress_fills_empty_cn_only(cols):
    actress, _, _ = cols
    avdict.record_actress("さおり", cn="纱织", source="javbus")
    row = actress.rows["さおり"]
    assert row["cn"] == "纱织"
    assert row["hits"] == 1
    assert row["sources"] == ["javbus"]
    assert "さおり" in row["aliases"]
    assert "updated_at" in row

    # 已知对照（同 jp→cn）再录：直接跳过，零写放大（hits 不变）
    avdict.record_actress("さおり", cn="纱织", source="javbus")
    assert actress.rows["さおり"]["hits"] == 1


def test_record_actress_no_overwrite_of_prefilled(cols):
    actress, _, _ = cols
    actress.rows["既存"] = {"_id": "既存", "cn": "旧名", "hits": 5}
    avdict.record_actress("既存", cn="新名")
    assert actress.rows["既存"]["cn"] == "旧名"
    assert actress.rows["既存"]["hits"] == 6


# ---------------------------------------------------------------------------
# normalize_tag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("中出し", "中出"),
        ("潮吹き", "潮吹"),
        ("単体作品", "单体作品"),
        ("処女", "处女"),
        ("ＯＬ", "OL"),          # 全角→半角后即规范名
        ("  巨乳  ", "巨乳"),     # strip
        ("巨　乳", "巨 乳"),      # 表意空格→半角空格并折叠（unknown 原样）
        ("未知の遊び", "未知の遊び"),  # unknown 原样保留
        ("", ""),
    ],
)
def test_normalize_tag_seed_and_keynorm(cols, raw, expect):
    assert avdict.normalize_tag(raw) == expect


def test_normalize_tag_db_alias_priority(cols):
    _, tag, _ = cols
    tag.rows["规范名"] = {"_id": "规范名", "aliases": ["变体X"], "category": None}
    assert avdict.normalize_tag("变体X") == "规范名"
    # 规范名自身（带 aliases 的行）直接返回
    assert avdict.normalize_tag("规范名") == "规范名"


def test_normalize_tag_plain_row_is_not_canonical(cols):
    """record_tag 造出的 {category: None} 行不得挡住种子别名归一。"""
    _, tag, _ = cols
    tag.rows["中出し"] = {"_id": "中出し", "category": None, "hits": 3}
    assert avdict.normalize_tag("中出し") == "中出"


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

def test_select_tags_normalizes_dedupes(cols):
    assert avdict.select_tags(
        ["中出し", "巨乳", "未知の遊び", "巨乳", "　"]) == ["中出", "巨乳", "未知の遊び"]
    # 全量频率沉淀：三个规范名都入库计数（含未知/被去重的不再重复计）
    _, tag, _ = cols
    assert tag.rows["中出"]["hits"] == 1
    assert tag.rows["巨乳"]["hits"] == 1
    assert tag.rows["未知の遊び"]["hits"] == 1


def test_select_tags_ranks_by_global_frequency(cols):
    """频率降序 top-N：先验高频排前；同频保持源顺序（稳定）。"""
    _, tag, _ = cols
    tag.rows["巨乳"] = {"_id": "巨乳", "hits": 10}
    tag.rows["中出"] = {"_id": "中出", "hits": 50}
    out = avdict.select_tags(["巨乳", "未知の遊び", "中出"])
    assert out == ["中出", "巨乳", "未知の遊び"]  # 50 > 10(record 后 11) > 1
    assert avdict.select_tags(["未知A", "巨乳"], limit=1) == ["巨乳"]


def test_select_tags_blacklist_and_restore(cols):
    """拉黑词永不进入影片信息；解除后恢复资格。"""
    assert avdict.blacklist_tag("巨乳") is True
    assert avdict.select_tags(["中出し", "巨乳"]) == ["中出"]
    assert avdict.restore_tag("巨乳") is True
    assert avdict.select_tags(["巨乳"]) == ["巨乳"]


def test_select_tags_empty_input(cols):
    assert avdict.select_tags([]) == []
    assert avdict.select_tags(None) == []


def test_list_tags_pages_by_frequency(cols):
    _, tag, _ = cols
    for i, name in enumerate(["甲", "乙", "丙"]):
        tag.rows[name] = {"_id": name, "hits": i + 1}
    rows, total = avdict.list_tags(page=0, per_page=2)
    assert total == 3
    assert [r["_id"] for r in rows] == ["丙", "乙"]  # 频率降序
    rows, _ = avdict.list_tags(page=1, per_page=2)
    assert [r["_id"] for r in rows] == ["甲"]


def test_blacklisted_word_keeps_frequency_for_restore(cols):
    """拉黑前历史频率保留：解除拉黑后可回到原排序位。"""
    _, tag, _ = cols
    tag.rows["巨乳"] = {"_id": "巨乳", "hits": 99}
    avdict.blacklist_tag("巨乳")
    assert tag.rows["巨乳"]["hits"] == 99      # 拉黑不清频
    avdict.restore_tag("巨乳")
    assert avdict.select_tags(["新词", "巨乳"]) == ["巨乳", "新词"]


# ---------------------------------------------------------------------------
# enhance 端到端
# ---------------------------------------------------------------------------

def test_enhance_end_to_end(cols):
    actress, tag, _ = cols
    actress.rows["百永さりな"] = {
        "_id": "百永さりな", "aliases": ["百永サリナ"], "cn": "百永纱里奈",
    }
    details = {
        "code": "GVH-690",
        "badges": ["中文字幕"],
        "actresses": ["百永サリナ", "未知子"],
        "actresses_cn": [],
        "genres": ["中出し", "巨乳", "未知タグ", "巨乳"],
    }
    out = avdict.enhance(details, source="test")

    assert out is details  # 原地修改并返回
    assert details["actresses_cn"] == ["百永纱里奈"]  # 日名补 cn；未知子无 cn 不产
    assert details["genres"] == ["中出", "巨乳", "未知タグ"]  # 归一 + 保序去重（首见同频）
    assert details["badges"] == ["中文字幕"]  # badges 过滤后原样（入 tag 库计数）

    # unknown 日名回写 av_actress 学习
    assert "未知子" in actress.rows
    assert actress.rows["未知子"]["hits"] == 1
    assert actress.rows["未知子"]["sources"] == ["test"]
    # known 日名（已命中）不回写
    assert "百永サリナ" not in actress.rows
    # 所有获取到的标签（含 badges）全量入库计数
    assert set(tag.rows) == {"中出", "巨乳", "未知タグ", "中文字幕"}
    assert all(r["hits"] == 1 for r in tag.rows.values())


def test_enhance_keeps_existing_cn_and_positions(cols):
    actress, _, _ = cols
    details = {
        "actresses": ["百永さりな", "未知子"],
        "actresses_cn": ["自定义名", ""],
    }
    avdict.enhance(details)
    assert details["actresses_cn"] == ["自定义名"]  # 已有 cn 不覆盖；空位查库无果不产
    # 既有 cn 覆盖的序位不触发回写；未知子 无 cn 回写一次
    assert set(actress.rows) == {"未知子"}


def test_enhance_extra_cn_and_mutation_safety(cols):
    _, _, _ = cols
    details = {"actresses": [], "actresses_cn": ["只有名", "只有名"]}
    avdict.enhance(details)
    assert details["actresses_cn"] == ["只有名"]  # 多出的既有 cn 保留并去重
    assert avdict.enhance(None) is None  # 非字典安全透传
    assert avdict.enhance({}) == {}


# ---------------------------------------------------------------------------
# code_meta
# ---------------------------------------------------------------------------

def test_code_meta_save_load_roundtrip(cols):
    _, _, code = cols
    details = {
        "code": "GVH-690",
        "title": "标题",
        "badges": [],  # 空值应被去冗
        "actresses": ["百永さりな"],
        "actresses_cn": [],
    }
    avdict.code_meta_save("GVH-690", details)
    stored = code.rows["GVH-690"]
    # v2 白名单：code 即 _id 不冗余入库；非白名单键（cover 等）拒收
    assert stored["details"] == {"title": "标题", "actresses": ["百永さりな"]}
    assert isinstance(stored["updated_at"], datetime)

    loaded = avdict.code_meta_load("GVH-690")
    assert loaded == {"title": "标题", "actresses": ["百永さりな"]}
    assert loaded is not stored["details"]  # 返回副本


def test_code_meta_save_merges_not_replaces(cols):
    """部分补齐的 enrich 不得抹掉早前快照更全的字段（顶层合并）。"""
    _, _, code = cols
    avdict.code_meta_save("DASS-629", {"title": "完整标题", "studio": "SOD", "genres": ["巨乳"]})
    avdict.code_meta_save("DASS-629", {"genres": ["中出"]})  # 部分保存
    assert avdict.code_meta_load("DASS-629") == {
        "title": "完整标题", "studio": "SOD", "genres": ["中出"]}


def test_code_meta_rejects_garbage_keys(cols):
    """面板垃圾串不得制造键碎片；合法形式（FC2 前缀/纯数字段）放行。"""
    _, _, code = cols
    avdict.code_meta_save("DASS-629 (蓝光) JUNK", {"title": "x"})
    avdict.code_meta_save("串时间", {"title": "x"})
    assert code.rows == {}
    avdict.code_meta_save("FC2-PPV-1234567", {"title": "x"})
    assert "FC2-PPV-1234567" in code.rows


def test_code_meta_load_expiry(cols):
    _, _, code = cols
    avdict.code_meta_save("GVH-690", {"title": "t"})

    # 边界内有效（29 天）
    code.rows["GVH-690"]["updated_at"] = datetime.now(timezone.utc) - timedelta(days=29)
    assert avdict.code_meta_load("GVH-690") == {"title": "t"}

    # 超龄失效（31 天）；max_age_days 参数生效
    code.rows["GVH-690"]["updated_at"] = datetime.now(timezone.utc) - timedelta(days=31)
    assert avdict.code_meta_load("GVH-690") is None
    code.rows["GVH-690"]["updated_at"] = datetime.now(timezone.utc) - timedelta(days=40)
    assert avdict.code_meta_load("GVH-690", max_age_days=50) == {"title": "t"}

    # 未来时间戳（时钟漂移/恶意）：>5min 偏差直接拒信
    code.rows["GVH-690"]["updated_at"] = datetime.now(timezone.utc) + timedelta(days=1)
    assert avdict.code_meta_load("GVH-690") is None


def test_code_meta_load_invalid_rejected(cols):
    _, _, code = cols
    assert avdict.code_meta_load("不存在") is None
    assert avdict.code_meta_load("") is None

    # details 非空 dict 才返回
    code.rows["空档"] = {"_id": "空档", "details": {}, "updated_at": datetime.now(timezone.utc)}
    assert avdict.code_meta_load("空档") is None

    # 无有效时间戳不可信
    code.rows["没时间"] = {"_id": "没时间", "details": {"code": "X"}}
    assert avdict.code_meta_load("没时间") is None
    code.rows["坏时间"] = {"_id": "坏时间", "details": {"code": "X"}, "updated_at": "not-a-date"}
    assert avdict.code_meta_load("坏时间") is None

    # ISO 字符串时间戳也认（naive 按 UTC）
    fresh = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    code.rows["GVH-690"] = {"_id": "GVH-690", "details": {"title": "t"}, "updated_at": fresh}
    assert avdict.code_meta_load("GVH-690") == {"title": "t"}

    # 空 details / 空 code 的 save 静默跳过
    avdict.code_meta_save("", {"code": "X"})
    avdict.code_meta_save("EMPTY", {})
    avdict.code_meta_save("EMPTY", {"code": ""})
    assert "EMPTY" not in code.rows


# ---------------------------------------------------------------------------
# 库故障全吞（安全默认值）
# ---------------------------------------------------------------------------

def test_all_public_funcs_swallow_db_failure(monkeypatch):
    boom = FakeCol(fail=True)
    monkeypatch.setattr(avdict, "_cols", lambda: (boom, boom))
    monkeypatch.setattr(avdict, "_code_col", lambda: boom)

    assert avdict.actress_cn("百永さりな") == ""
    avdict.record_actress("百永さりな", cn="名")  # 不抛
    assert avdict.normalize_tag("中出し") == "中出"  # 种子兜底仍可用
    assert avdict.normalize_tag("巨乳") == "巨乳"
    avdict.record_tag_seen("未知タグ")  # 不抛

    # 库故障 → 频率全 0 → 保持源顺序，绝不清空标签
    assert avdict.select_tags(["中出し", "巨乳"]) == ["中出", "巨乳"]
    assert avdict.filter_badges(["中文字幕", "无码破解"]) == ["中文字幕", "无码破解"]

    details = {"actresses": ["未知子"], "genres": ["中出し", "未知タグ"],
               "badges": ["中文字幕"]}
    out = avdict.enhance(details, source="x")
    assert out is details
    assert details["genres"] == ["中出", "未知タグ"]  # 本地归一不受库故障影响
    assert details["badges"] == ["中文字幕"]

    assert avdict.code_meta_load("GVH-690") is None
    avdict.code_meta_save("GVH-690", {"code": "GVH-690"})  # 不抛


def test_enhance_db_failure_keeps_local_normalization(monkeypatch):
    boom = FakeCol(fail=True)
    monkeypatch.setattr(avdict, "_cols", lambda: (boom, boom))
    monkeypatch.setattr(avdict, "_code_col", lambda: boom)
    details = {"genres": ["中出し", "巨乳"]}
    avdict.enhance(details)
    assert details["genres"] == ["中出", "巨乳"]


# ---------------------------------------------------------------------------
# 种子数据一致性自查（别名表）
# ---------------------------------------------------------------------------

def test_seed_alias_targets_differ_and_no_dup():
    for alias, target in avdict_seed.SEED_TAG_ALIASES.items():
        assert isinstance(target, str) and target, f"别名 {alias!r} 目标为空"
        assert target != alias, f"别名 {alias!r} 是恒等映射"


def test_seed_no_duplicate_literal_keys():
    """AST 层检查：别名 dict 字面量无重复键（防静默覆盖）。"""
    src = (SRC / "utils" / "avdict_seed.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    aliases_value = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SEED_TAG_ALIASES":
                    aliases_value = node.value
    assert aliases_value is not None, "SEED_TAG_ALIASES 缺失"
    keys = [k.value for k in aliases_value.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    assert len(keys) == len(set(keys)), "别名表存在重复字面量键"
    assert len(keys) == len(avdict_seed.SEED_TAG_ALIASES)


# ---------------------------------------------------------------------------
# 修复回归：键长上限 / 熔断器 / 陈旧类别
# ---------------------------------------------------------------------------

def test_norm_key_capped_at_100():
    assert len(avdict._norm_key("あ" * 500)) == 100


def test_circuit_breaker_trips_and_blocks(cols, monkeypatch):
    """连接类故障跳闸：60s 内后续操作不触库（保护 to_thread 工作线程）。"""
    calls = []

    def flaky():
        raise RuntimeError("boom")

    class DownCol:
        def find_one(self, q):
            calls.append(1)
            raise __import__("pymongo.errors", fromlist=["ServerSelectionTimeoutError"]).ServerSelectionTimeoutError()

    monkeypatch.setattr(avdict, "_cols", lambda: (DownCol(), DownCol()))
    assert avdict.actress_cn(" whoever ") == ""   # 触发跳闸
    assert calls and avdict._down()
    n = len(calls)
    assert avdict.actress_cn("再查") == ""        # 熔断期直接降级
    assert len(calls) == n                        # 未触库


def test_import_surface_has_no_category_apis():
    """类别派生机制已按用户裁决移除：不留死代码。"""
    assert not hasattr(avdict, "classify")
    assert not hasattr(avdict, "record_tag")
    assert not hasattr(avdict_seed, "SEED_TAG_CATEGORIES")
    assert not hasattr(avdict_seed, "SEED_CATEGORY_ORDER")
