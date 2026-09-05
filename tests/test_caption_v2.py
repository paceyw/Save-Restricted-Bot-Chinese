"""Offline tests for the av-dict Caption v2 contract (契约 Caption v2 节).

Covers the C integration points:
- P0: getav zh 简介 主演 劈分不再产出「讲述…」假演员
      （_GETAV_ZH_STARS_RE 收紧 + _looks_like_name 白名单）。
- details dict v2 字段分离：starsZh → actresses_cn，stars → actresses。
- build_caption 四行骨架（演员/原名/标签/类别=badges+categories），
  缺数据留空占位。
- parse_details 与 build_caption 的 round-trip 稳定。

No network: ``_http_get`` is monkeypatched with fixture-serving fakes.
"""

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]

# utils.missav imports config (transcode budget knobs); config hard-requires
# these keys at import time, so default them for hermetic test runs.
os.environ.setdefault("MASTER_KEY", "missav-test-master")
os.environ.setdefault("IV_KEY", "missav-test-iv")
sys.path.insert(0, str(SRC))

spec = importlib.util.spec_from_file_location("missav_mod", SRC / "utils" / "missav.py")
missav = importlib.util.module_from_spec(spec)
sys.modules["missav_mod"] = missav
spec.loader.exec_module(missav)

caption = importlib.import_module("utils.caption")


class FakeResp:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.url = None


def augment_with_desc(monkeypatch, desc, slug="cjod-159"):
    """Run _augment_getav_zh against a stubbed /zh page description."""
    served = {
        f"https://getav.net/zh/videos/{slug}": FakeResp(text=(
            f"<title>[无码/中文字幕] CJOD-159 测试 | GetAV</title>"
            f'<meta name="description" content="{desc}"/>'
        )),
    }
    monkeypatch.setattr(
        missav, "_http_get",
        lambda url, headers=None, timeout=None, max_bytes=None:
            (served.get(url) or FakeResp(status=404), None))
    data = {}
    missav._augment_getav_zh(data, f"https://getav.net/zh/videos/{slug}",
                             ("getav.net",), "getav.net")
    return data


# ─── P0: 假演员劈分修复 ────────────────────────────────────────────────────────

def test_zh_stars_regex_stops_at_sentence_punctuation():
    # 截断符收紧：。，,；; 处停止（、保留为劈分符）
    m = missav._GETAV_ZH_STARS_RE.search(
        "剧情介绍。主演：妃月琉衣、小明，为您带来精彩剧情。支持手机播放")
    assert m.group(1) == "妃月琉衣、小明"
    m = missav._GETAV_ZH_STARS_RE.search("主演：妃月琉衣;小明:支持手机播放")
    assert m.group(1) == "妃月琉衣"
    m = missav._GETAV_ZH_STARS_RE.search("主演：妃月琉衣。更多介绍")
    assert m.group(1) == "妃月琉衣"


def test_looks_like_name_whitelist():
    # 像人名：2-15 字、纯 CJK（允许中隔点 ·）
    for name in ("妃月琉衣", "小明", "玛丽亚·小林", "田中一郎"):
        assert missav._looks_like_name(name) is True, name
    # 不像人名：叙述词 / 数字 / 拉丁字母标点 / 假名 / 长度出界
    for bad in (
        "小明 讲述了一段禁忌之恋",      # 误劈残句：叙述词
        "本片讲述了",                   # 纯叙述
        "为您带来精彩剧情",             # 叙述词
        "出演过多部作品的女优",         # 叙述词
        "是一位人妻",                   # 是一位
        "3D双穴女神",                   # 数字
        "AI机器人",                     # 拉丁字母
        "ソープランド",                 # 假名非 CJK
        "和",                           # 单字
        "这是一个超过了十五个字符上限的过长片段某某",  # 16 字
    ):
        assert missav._looks_like_name(bad) is False, bad


def test_augment_no_fake_actress_from_narrative(monkeypatch):
    """P0 误劈样例：主演：A、B 讲述… 不产生「讲述…」假演员。"""
    data = augment_with_desc(
        monkeypatch, "CJOD-159 剧情介绍。主演：妃月琉衣、小明 讲述了一段禁忌之恋")
    assert data["starsZh"] == ["妃月琉衣", "小明"]

    # 逗号后紧跟叙述段：截断符直接拦住，白名单再兜底
    data = augment_with_desc(
        monkeypatch, "主演：妃月琉衣、小明，为您带来最精彩的演出")
    assert data["starsZh"] == ["妃月琉衣", "小明"]

    # 全是叙述：过滤后非空才设 starsZh
    data = augment_with_desc(monkeypatch, "主演：本片讲述了一个故事")
    assert "starsZh" not in data

    # 正常多演员（、分隔）不受影响
    data = augment_with_desc(monkeypatch, "主演：妃月琉衣、小明、田中一郎。支持手机播放")
    assert data["starsZh"] == ["妃月琉衣", "小明", "田中一郎"]


# ─── details dict v2: 字段分离 ────────────────────────────────────────────────

def test_getav_starszh_goes_to_actresses_cn():
    data = {
        "id": "cjod-159",
        "stars": [{"name": "妃月るい"}, {"name": "妃月るい"}, {"name": ""}],
        "starsZh": ["妃月琉衣", "小明"],
    }
    d = missav.extract_getav_details(data, "https://getav.net/zh/videos/cjod-159")
    assert d["actresses"] == ["妃月るい"]          # 源语言名（日文）
    assert d["actresses_cn"] == ["妃月琉衣", "小明"]  # 中文名，不再混入 actresses


def test_missav_panel_actresses_cn_starts_empty():
    d = missav.extract_video_details("<html></html>", "https://missav.ai/sone-543")
    assert d["actresses"] == [] and d["actresses_cn"] == []


def test_build_caption_category_merges_badges_and_categories():
    """类别 = badges + categories 合并去重，badges 在前。"""
    d = {
        "code": "GVH-690",
        "title": "t",
        "badges": ["中文字幕", "无码"],
        "categories": ["巨乳系", "中文字幕"],   # 中文字幕重复：去重保前
    }
    cap = missav.build_caption(d)
    assert cap.endswith("类别：#中文字幕 #无码 #巨乳系")
    # 无 categories 时类别行退化为纯 badges
    assert missav.build_caption({"code": "A-1", "badges": ["中文字幕"]}).endswith(
        "类别：#中文字幕")


# ─── Caption v2 骨架：四行恒定渲染 ────────────────────────────────────────────

def test_build_caption_four_lines_with_blank_slots():
    d = {
        "code": "GVH-690",
        "title": "intro",
        "actresses_cn": ["百永纱里奈"],
        "actresses": ["百永さりな"],
        "genres": ["巨乳", "中出"],
        "badges": ["中文字幕"],
    }
    assert missav.build_caption(d) == (
        "GVH-690\n\nintro\n\n"
        "演员：#百永纱里奈\n"
        "原名：#百永さりな\n"
        "标签：#巨乳 #中出\n"
        "类别：#中文字幕"
    )
    # 缺数据留空占位：结构恒定
    sparse = missav.build_caption({"code": "FC2-PPV-1", "badges": ["无码"]})
    assert sparse.split("\n\n")[-1].split("\n") == [
        "演员：", "原名：", "标签：", "类别：#无码"]


# ─── parse_details v2 标签映射 + round-trip ──────────────────────────────────

def test_parse_details_v2_label_mapping():
    det = caption.parse_details(
        "GVH-690\n\n简介\n\n演员：#小美 #小林\n原名：#Mei #Rin\n"
        "标签：#巨乳\n类别：#中文字幕")
    assert det["actresses_cn"] == ["小美", "小林"]
    assert det["actresses"] == ["Mei", "Rin"]
    assert det["genres"] == ["巨乳"]
    assert det["badges"] == ["中文字幕"]


def test_parse_details_accepts_plain_label_values():
    # 非 hashtag 的裸值 + 分隔符劈分同样按字段归位
    det = caption.parse_details("GVH-690\n\n演员： 小美、小林\n原名：Mei\n类别： #高潮")
    assert det["actresses_cn"] == ["小美", "小林"]
    assert det["actresses"] == ["Mei"]
    assert det["badges"] == ["高潮"]
    # 中隔点 · 是译名一部分，不再劈分
    det = caption.parse_details("GVH-690\n\n演员：玛丽亚·小林")
    assert det["actresses_cn"] == ["玛丽亚·小林"]


def test_round_trip_build_parse_build_stable():
    """build_caption → parse_details → build_caption 恒等（round-trip 稳定）。"""
    d = {
        "code": "GVH-690",
        "title": "标题简介",
        "actresses_cn": ["百永纱里奈", "千石桃香"],
        "actresses": ["百永さりな", "千石もなか"],
        "genres": ["巨乳", "中出"],
        "badges": ["中文字幕"],
    }
    once = missav.build_caption(d)
    assert caption.restructure_caption(once) == once


def test_round_trip_forwarded_v2_text_stable():
    """转发别人手写的 v2 格式文本：原样回流，不 mangling。"""
    text = ("GVH-690\n\n标题简介\n\n演员：#小美\n原名：#Mei\n"
            "标签：#巨乳\n类别：#中文字幕")
    assert caption.restructure_caption(text) == text
