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

def test_classify_order_and_unknown_preserved(cols):
    _, _, _ = cols
    tags, cats = avdict.classify(["中出し", "巨乳", "未知の遊び", "巨乳", "　"])
    assert tags == ["中出", "巨乳", "未知の遊び"]  # 保序去重，unknown 原样
    assert cats == ["巨乳系", "中出·受孕"]  # SEED_CATEGORY_ORDER 出现序


def test_classify_db_category_override(cols):
    _, tag, _ = cols
    tag.rows["巨乳"] = {"_id": "巨乳", "category": "SM·束缚"}  # db 覆盖
    tag.rows["痴女"] = {"_id": "痴女", "category": "怪类别"}  # 不在 ORDER → 不产出
    tags, cats = avdict.classify(["痴女", "巨乳"])
    assert tags == ["痴女", "巨乳"]
    assert cats == ["SM·束缚"]


def test_classify_empty_input(cols):
    assert avdict.classify([]) == ([], [])
    assert avdict.classify(None) == ([], [])


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
    assert details["genres"] == ["中出", "巨乳", "未知タグ"]  # 归一 + 保序去重
    assert details["categories"] == ["巨乳系", "中出·受孕"]  # 派生类别
    assert details["badges"] == ["中文字幕"]  # 无关字段不动

    # unknown 日名回写 av_actress 学习
    assert "未知子" in actress.rows
    assert actress.rows["未知子"]["hits"] == 1
    assert actress.rows["未知子"]["sources"] == ["test"]
    # known 日名（已命中）不回写
    assert "百永サリナ" not in actress.rows
    # unknown tag 回写 av_tag；种子/库已知 tag 不回写
    assert set(tag.rows) == {"未知タグ"}
    assert tag.rows["未知タグ"]["category"] is None


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
    avdict.record_tag("未知タグ")  # 不抛

    tags, cats = avdict.classify(["中出し", "巨乳"])
    assert (tags, cats) == (["中出", "巨乳"], ["巨乳系", "中出·受孕"])

    details = {"actresses": ["未知子"], "genres": ["中出し", "未知タグ"]}
    out = avdict.enhance(details, source="x")
    assert out is details
    assert details["genres"] == ["中出", "未知タグ"]  # 本地归一不受库故障影响
    assert "categories" not in details or details["categories"] == ["中出·受孕"]

    assert avdict.code_meta_load("GVH-690") is None
    avdict.code_meta_save("GVH-690", {"code": "GVH-690"})  # 不抛


def test_enhance_db_failure_keeps_seed_categories(monkeypatch):
    boom = FakeCol(fail=True)
    monkeypatch.setattr(avdict, "_cols", lambda: (boom, boom))
    monkeypatch.setattr(avdict, "_code_col", lambda: boom)
    details = {"genres": ["巨乳", "痴女"]}
    avdict.enhance(details)
    assert details["genres"] == ["巨乳", "痴女"]
    assert details["categories"] == ["巨乳系", "痴女·荡妇"]


# ---------------------------------------------------------------------------
# 种子数据一致性自查
# ---------------------------------------------------------------------------

def _canonical_tags():
    return {t for tags in avdict_seed.SEED_TAG_CATEGORIES.values() for t in tags}


def test_seed_shape():
    cats = avdict_seed.SEED_TAG_CATEGORIES
    total = sum(len(v) for v in cats.values())
    assert 15 <= len(cats) <= 20
    assert 150 <= total <= 250
    # ORDER 与 SEED_TAG_CATEGORIES 键完全同序
    assert avdict_seed.SEED_CATEGORY_ORDER == list(cats.keys())


def test_seed_tag_single_category():
    seen = {}
    for cat, tags in avdict_seed.SEED_TAG_CATEGORIES.items():
        for tag in tags:
            assert tag not in seen, f"{tag!r} 同时属于 {seen.get(tag)!r} 与 {cat!r}"
            seen[tag] = cat


def test_seed_alias_targets_canonical():
    canonical = _canonical_tags()
    for alias, target in avdict_seed.SEED_TAG_ALIASES.items():
        assert target in canonical, f"别名 {alias!r} 指向非规范名 {target!r}"
        assert target != alias, f"别名 {alias!r} 是恒等映射"


def test_seed_alias_keys_not_canonical():
    canonical = _canonical_tags()
    for alias in avdict_seed.SEED_TAG_ALIASES:
        assert alias not in canonical, f"别名键 {alias!r} 与规范名冲突"


def test_seed_no_duplicate_literal_keys():
    """AST 层检查：dict/列表字面量无重复字符串键/元素（防静默覆盖）。"""
    src = (SRC / "utils" / "avdict_seed.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    lits = {
        "SEED_CATEGORY_ORDER": None,
        "SEED_TAG_CATEGORIES": None,
        "SEED_TAG_ALIASES": None,
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in lits:
                    lits[t.id] = node.value
    for name, value in lits.items():
        assert value is not None, f"{name} 缺失"

    def assert_no_dup_strings(items, where):
        strs = [k.value for k in items if isinstance(k, ast.Constant)
                and isinstance(k.value, str)]
        assert len(strs) == len(set(strs)), f"{where} 存在重复字面量"

    order = lits["SEED_CATEGORY_ORDER"]
    assert_no_dup_strings(order.elts, "SEED_CATEGORY_ORDER")
    cats = lits["SEED_TAG_CATEGORIES"]
    assert_no_dup_strings(cats.keys, "SEED_TAG_CATEGORIES")
    for k, v in zip(cats.keys, cats.values):
        assert_no_dup_strings(v.elts, f"类别 {k.value}")
    aliases = lits["SEED_TAG_ALIASES"]
    assert_no_dup_strings(aliases.keys, "SEED_TAG_ALIASES")

    # 运行时长度 == 字面量长度（重复键会被 Python 静默吞掉）
    assert len(aliases.keys) == len(avdict_seed.SEED_TAG_ALIASES)
    assert len(cats.keys) == len(avdict_seed.SEED_TAG_CATEGORIES)


def test_seed_end_to_end_derivation(cols):
    """种子映射可独立支撑 classify（冷启动路径）。"""
    _, _, _ = cols
    tags, cats = avdict.classify(["人妻", "中出し", "拘束", "单体作品"])
    assert tags == ["人妻", "中出", "拘束", "单体作品"]
    assert cats == ["人妻·熟女", "SM·束缚", "中出·受孕", "企划·综合"]


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


def test_enhance_clears_stale_categories(cols):
    """genres 归一后无类可派时，必须清掉与 genres 不再对应的旧 categories。"""
    d = {"genres": ["未知标签"], "categories": ["巨乳系"]}
    out = avdict.enhance(d)
    assert out["categories"] == []
