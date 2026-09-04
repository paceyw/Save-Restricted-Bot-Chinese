"""missav 番号搜索卡片 tests (issue #16, 规格 §2 方案A + D2 定稿).

Covers:
  - 番号四层归一化（FC2 / HEYZO / 纯数字 / 字母-数字）与 URL 拒识,
  - _parse_missav_search_html 对手造 fixture 的解析（标题/href/缩略图、
    非视频页锚点过滤、slug 去重、≤10 条上限、badge 推断）,
  - 搜索被 CF 拦 / 无结果的明确中文提示,
  - 搜索卡片交互：唯一命中也出卡片（D2）、封面 photo 卡、分页（下一页）、
    选中回调 → discover_missav_variants → 版本卡片或直接入队,
  - batch.py 纯文本路径 route_code_search 的路由判定.

Harness convention mirrors tests/test_missav_route.py: stubbed heavy deps,
real plugins.ytdl module, no network anywhere (页面与搜索均为 monkeypatch).
"""

import asyncio
import re as _re

import pytest

from tests.test_missav_route import (  # noqa: F401
    _FakeMessage, _FakeQuery, _queue_state,
    ytdl,
)


# ─── 番号归一化（四层正则） ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("SSIS-405", "SSIS-405"),
    ("ssis405", "SSIS-405"),
    ("ssis 405", "SSIS-405"),
    ("ssis_405", "SSIS-405"),
    ("fc2-ppv-1234567", "FC2-PPV-1234567"),
    ("FC2PPV 1234567", "FC2-PPV-1234567"),
    ("fc2-1234567", "FC2-PPV-1234567"),
    ("HEYZO-1234", "HEYZO-1234"),
    ("heyzo1234", "HEYZO-1234"),
    ("092014_887", "092014-887"),
    ("092014-887", "092014-887"),
    ("1234", "1234"),
])
def test_normalize_video_code_layers(ytdl, raw, expected):
    assert ytdl._normalize_video_code(raw) == expected


@pytest.mark.parametrize("raw", [
    "https://missav.ai/sone-543",
    "missav.ai/sone-543",
    "https://example.com/video 123",
    "",
    None,
    "看这个",
    "https://t.me/fancha103/7823",
])
def test_normalize_video_code_rejects_urls_and_junk(ytdl, raw):
    assert ytdl._normalize_video_code(raw) is None


# ─── 搜索结果页解析（手造 fixture） ────────────────────────────────────────────

def _result_card(href, thumb, code, title):
    return (
        f'<a href="{href}" class="group">'
        f'<img src="{thumb}" loading="lazy" alt="cover">'
        f'<p class="text-secondary">{code}</p>'
        f'<h2 class="text-xs truncate">{title}</h2>'
        f'</a>'
    )


SEARCH_HTML = """
<html><head><title>「ssis-405」的搜索结果 - MissAV</title></head>
<body>
<div class="grid grid-cols-2 gap-4">
  """ + _result_card(
      "https://missav.ai/dm5/ssis-405-uncensored-leak-chinese-subtitle",
      "https://cdn.example/ssis405-uc-cn.jpg",
      "SSIS-405", "SSIS-405 無碼破解中文字幕 播放") + """
  """ + _result_card(
      "https://missav.ai/cn/ssis-405-chinese-subtitle",
      "https://cdn.example/ssis405-cn.jpg",
      "SSIS-405", "SSIS-405 中文字幕 播放") + """
  """ + _result_card(
      "https://missav.ai/ssis-405",
      "https://cdn.example/ssis405.jpg",
      "SSIS-405", "SSIS-405 原版 播放") + """
  <a href="https://missav.ai/dm278/chinese-subtitle" class="listing">中文分类</a>
  <a href="/actresses/sora" class="actress">actor</a>
  <a href="#top">Top</a>
</div>
</body></html>
"""


def test_parse_missav_search_html_filters_and_dedupes(ytdl):
    results = ytdl._parse_missav_search_html(SEARCH_HTML)
    # 分类/演员/锚点链接被过滤；同一部片的三个版本都保留（slug 不同）
    assert [r["href"] for r in results] == [
        "https://missav.ai/dm5/ssis-405-uncensored-leak-chinese-subtitle",
        "https://missav.ai/cn/ssis-405-chinese-subtitle",
        "https://missav.ai/ssis-405",
    ]
    assert all(r["thumb"].startswith("https://cdn.example/") for r in results)
    assert "SSIS-405" in results[0]["title"]
    # badge：组合页 = 无码破解·中文字幕；cn 页 = 中文字幕；原版无 badge
    assert results[0]["badges"] == "无码破解·中文字幕"
    assert results[1]["badges"] == "中文字幕"
    assert results[2]["badges"] == ""


def test_parse_missav_search_html_resolves_relative_links(ytdl):
    html = _result_card("/cn/ssis-405-chinese-subtitle", "/img/x.jpg",
                        "SSIS-405", "相对链接版本")
    results = ytdl._parse_missav_search_html(
        html, base="https://missav.ws/search/SSIS-405")
    assert len(results) == 1
    assert results[0]["href"] == "https://missav.ws/cn/ssis-405-chinese-subtitle"
    assert results[0]["thumb"] == "https://missav.ws/img/x.jpg"


def test_parse_missav_search_html_caps_at_ten(ytdl):
    html = "".join(
        _result_card(f"https://missav.ai/ssis-{n}", f"https://cdn/{n}.jpg",
                     f"SSIS-{n}", f"result {n}")
        for n in range(1, 16)
    )
    results = ytdl._parse_missav_search_html(html)
    assert len(results) == 10


def test_parse_missav_search_html_empty_and_garbage(ytdl):
    assert ytdl._parse_missav_search_html("") == []
    assert ytdl._parse_missav_search_html("<p>no anchors here</p>") == []


# ─── 搜索失败路径 ──────────────────────────────────────────────────────────────

class _Card(_FakeMessage):
    """Fake user message with editable notices, photo card and text cards."""

    def __init__(self, text=""):
        super().__init__(text)
        self.notices = []
        self.photos = []
        self.cards = []

    async def reply_text(self, text, *a, **kw):
        self.replies.append(text)
        notice = _Notice(text, reply_markup=kw.get("reply_markup"))
        self.notices.append(notice)
        return notice

    async def reply_photo(self, url, caption=None, reply_markup=None):
        self.photos.append((url, caption, reply_markup))
        card = _Notice(caption, reply_markup=reply_markup)
        self.cards.append(card)
        return card


class _Notice:
    def __init__(self, text="", reply_markup=None):
        self.text = text
        self.reply_markup = reply_markup
        self.edits = []
        self.markups = []
        self.deleted = False

    async def edit_text(self, text, *a, **kw):
        self.edits.append(text)
        self.text = text
        if "reply_markup" in kw:
            self.reply_markup = kw["reply_markup"]

    async def edit_reply_markup(self, reply_markup=None):
        self.markups.append(reply_markup)
        self.reply_markup = reply_markup

    async def edit_caption(self, text, *a, **kw):
        self.edits.append(text)
        self.text = text
        if "reply_markup" in kw:
            self.reply_markup = kw["reply_markup"]
        self.text = text

    async def delete(self):
        self.deleted = True


def test_search_blocked_message(ytdl, monkeypatch):
    def fake_search(code, m_hosts, g_hosts):
        return None, "**__搜索页被 Cloudflare 拦截，请稍后再试__**"

    monkeypatch.setattr(ytdl, "_search_both", fake_search)
    msg = _Card()
    asyncio.run(ytdl.start_missav_search(msg, "SSIS-405"))
    assert any("Cloudflare" in n.text for n in msg.notices)


def test_search_no_results_message(ytdl, monkeypatch):
    monkeypatch.setattr(ytdl, "_search_both", lambda code, m, g: ([], None))
    msg = _Card()
    asyncio.run(ytdl.start_missav_search(msg, "SSIS-405"))
    assert any("未找到" in n.text for n in msg.notices)
    assert not msg.photos


def test_search_unrecognized_code_message(ytdl):
    msg = _Card("hello")
    asyncio.run(ytdl.start_missav_search(msg, "看这个"))
    assert any("无法识别番号" in r for r in msg.replies)
    assert not msg.photos and not msg.cards


# ─── 卡片交互 ──────────────────────────────────────────────────────────────────

def _search_results(ytdl, code="SSIS-405", n=3):
    return [
        {"title": f"SSIS-405 版本{i}", "href": f"https://missav.ai/ssis-405{'-x' * i}",
         "thumb": f"https://cdn.example/{i}.jpg", "badges": "中文字幕" if i else ""}
        for i in range(n)
    ]


def test_search_card_shows_cover_and_rows_even_for_single_hit(ytdl, monkeypatch):
    monkeypatch.setattr(
        ytdl, "_missav_search", lambda code, hosts: (_search_results(ytdl, n=1), None))
    msg = _Card()
    asyncio.run(ytdl.start_missav_search(msg, "SSIS-405"))
    # D2：唯一命中也出卡片确认；封面 photo 卡先行
    assert len(msg.photos) == 1
    markup = msg.photos[0][2]
    assert len(markup.buttons) == 1        # one row per result
    assert "中文字幕" not in markup.buttons[0][0].text   # badge "" → 不加
    assert "SSIS-405" in markup.buttons[0][0].text
    prompt = ytdl._SEARCH_PROMPTS[42]
    assert prompt["is_photo"] is True and prompt["card"] is msg.cards[0]


def test_search_card_badge_shown_and_text_fallback_without_thumb(ytdl, monkeypatch):
    results = _search_results(ytdl, n=2)
    for r in results:
        r["thumb"] = ""                    # no covers at all → text-only card
    monkeypatch.setattr(ytdl, "_missav_search", lambda code, hosts: (results, None))
    msg = _Card()
    asyncio.run(ytdl.start_missav_search(msg, "SSIS-405"))
    assert not msg.photos                 # no cover → never a photo card
    assert len(msg.notices) >= 1
    markup = msg.notices[-1].reply_markup
    assert markup is not None
    assert "｜中文字幕" in markup.buttons[1][0].text


def test_search_pagination_next_page_footer(ytdl, monkeypatch):
    monkeypatch.setattr(
        ytdl, "_missav_search", lambda code, hosts: (_search_results(ytdl, n=8), None))
    msg = _Card()
    asyncio.run(ytdl.start_missav_search(msg, "SSIS-405"))
    markup = msg.photos[0][2]
    # page 0: 6 rows + footer nav
    assert len(markup.buttons) == 7
    assert "下一页" in markup.buttons[-1][0].text
    # turn the page via the footer callback
    token = ytdl._SEARCH_PROMPTS[42]["token"]
    query = _FakeQuery(42, f"srchpage:{token}:1",
                       _re.compile(r"^srchpage:([0-9a-f]+):(\d+)$"))
    asyncio.run(ytdl.search_page_callback(None, query))
    card = msg.cards[0]
    assert len(card.markups) == 1
    page2 = card.markups[0]
    assert len(page2.buttons) == 3         # 2 result rows + nav row
    assert "上一页" in page2.buttons[-1][0].text
    assert "下一页" not in page2.buttons[-1][0].text


def test_search_pick_single_variant_enqueues_directly(ytdl, monkeypatch):
    state = _queue_state(monkeypatch)

    async def fake_discover(url, hosts=None):
        return [("raw", url, "原版")]

    monkeypatch.setattr(
        ytdl, "_missav_search", lambda code, hosts: (_search_results(ytdl, n=1), None))
    monkeypatch.setattr(ytdl, "discover_missav_variants", fake_discover)
    msg = _Card()
    asyncio.run(ytdl.start_missav_search(msg, "SSIS-405"))
    prompt = ytdl._SEARCH_PROMPTS[42]
    token = prompt["token"]

    query = _FakeQuery(42, f"srch:{token}:0",
                       _re.compile(r"^srch:([0-9a-f]+):(\d+)$"))
    asyncio.run(ytdl.search_pick_callback(None, query))
    # 选中后先进操作卡片（预览/下载/返回），prompt 保留
    assert 42 in ytdl._SEARCH_PROMPTS
    assert prompt["picked"]["href"] == "https://missav.ai/ssis-405"
    assert "已选择" in msg.cards[0].edits[0]
    act_query = _FakeQuery(42, f"srchact:{token}:dl",
                           _re.compile(r"^srchact:([0-9a-f]+):(dl|back)$"))
    asyncio.run(ytdl.search_action_callback(None, act_query))
    # 下载确认后 prompt 消费，任务以选中 href 入队
    assert 42 not in ytdl._SEARCH_PROMPTS
    task = state["enqueued"][0]
    assert task["url"] == "https://missav.ai/ssis-405"
    assert task["want_subtitle"] is False
    assert any("加入队列" in r for r in msg.replies)


def test_search_pick_multi_variant_goes_to_mav_card(ytdl, monkeypatch):
    state = _queue_state(monkeypatch)

    async def fake_discover(url, hosts=None):
        return [
            ("raw", url, "原版"),
            ("cn", url + "-chinese-subtitle", "中文字幕"),
        ]

    monkeypatch.setattr(
        ytdl, "_missav_search", lambda code, hosts: (_search_results(ytdl, n=1), None))
    monkeypatch.setattr(ytdl, "discover_missav_variants", fake_discover)
    msg = _Card()
    asyncio.run(ytdl.start_missav_search(msg, "SSIS-405"))
    prompt = ytdl._SEARCH_PROMPTS[42]
    query = _FakeQuery(42, f"srch:{prompt['token']}:0",
                       _re.compile(r"^srch:([0-9a-f]+):(\d+)$"))
    asyncio.run(ytdl.search_pick_callback(None, query))
    act_query = _FakeQuery(42, f"srchact:{prompt['token']}:dl",
                           _re.compile(r"^srchact:([0-9a-f]+):(dl|back)$"))
    asyncio.run(ytdl.search_action_callback(None, act_query))
    # >1 版本 → 进 mav 版本卡片流程，不直接入队
    assert 42 in ytdl._MISSAV_PROMPTS
    assert state["enqueued"] == []
    assert ytdl._MISSAV_PROMPTS[42]["variants"][0][0] == "cn"


def test_search_pick_expired_prompt_rejected(ytdl, monkeypatch):
    import time as _time
    _queue_state(monkeypatch)
    monkeypatch.setattr(
        ytdl, "_missav_search", lambda code, hosts: (_search_results(ytdl, n=1), None))
    msg = _Card()
    asyncio.run(ytdl.start_missav_search(msg, "SSIS-405"))
    prompt = ytdl._SEARCH_PROMPTS[42]
    prompt["created_at"] = _time.time() - ytdl._GETAV_PROMPT_TTL - 1
    query = _FakeQuery(42, f"srch:{prompt['token']}:0",
                       _re.compile(r"^srch:([0-9a-f]+):(\d+)$"))
    asyncio.run(ytdl.search_pick_callback(None, query))
    assert query.answers and "过期" in query.answers[0][0]


def test_discard_search_prompts(ytdl, monkeypatch):
    monkeypatch.setattr(
        ytdl, "_missav_search", lambda code, hosts: (_search_results(ytdl, n=1), None))
    msg = _Card()
    asyncio.run(ytdl.start_missav_search(msg, "SSIS-405"))
    assert ytdl.discard_search_prompts(42) == 1
    assert ytdl.discard_search_prompts(42) == 0
    assert ytdl.discard_search_prompts() == 0


# ─── batch.py 纯文本路径 ───────────────────────────────────────────────────────

def test_route_code_search_routes_bare_code(ytdl, monkeypatch):
    seen = {}

    async def fake_start(message, raw_code):
        seen["message"] = message
        seen["code"] = raw_code

    monkeypatch.setattr(ytdl, "start_missav_search", fake_start)
    msg = _FakeMessage("ssis405")
    asyncio.run(ytdl.route_code_search(msg))
    assert seen["code"] == "SSIS-405"
    assert seen["message"] is msg


def test_route_code_search_ignores_urls_and_plain_text(ytdl, monkeypatch):
    called = []

    async def fake_start(message, raw_code):
        called.append(raw_code)

    monkeypatch.setattr(ytdl, "start_missav_search", fake_start)
    asyncio.run(ytdl.route_code_search(_FakeMessage("https://missav.ai/sone-543")))
    asyncio.run(ytdl.route_code_search(_FakeMessage("随便聊聊天")))
    asyncio.run(ytdl.route_code_search(_FakeMessage("")))
    assert called == []




# ─── 双源搜索（D2 追加）：missav + getav /zh/search?q= ─────────────────────────

def test_search_both_merges_and_dedupes(ytdl, monkeypatch):
    missav_results = [{"title": "A", "href": "https://missav.ai/ssis-405",
                       "thumb": "", "badges": "", "source": "missav"}]
    getav_results = [
        {"title": "A getav", "href": "https://getav.net/zh/videos/ssis-405",
         "thumb": "", "badges": "", "source": "getav"},          # 同番号 → 去重
        {"title": "B getav", "href": "https://getav.net/zh/videos/ssis-405-b",
         "thumb": "", "badges": "", "source": "getav"},
    ]
    seen = {}
    def fake_missav(code, hosts):
        return missav_results, None
    def fake_getav(code, hosts):
        return getav_results, None
    monkeypatch.setattr(ytdl, "_missav_search", fake_missav)
    monkeypatch.setattr(ytdl, "_getav_search", fake_getav)
    merged, err = ytdl._search_both("SSIS-405", ("missav.ai",), ("getav.net",))
    assert err is None
    assert [r["source"] for r in merged] == ["missav", "getav"]
    assert len(merged) == 2


def test_search_both_both_fail_yields_error(ytdl, monkeypatch):
    monkeypatch.setattr(ytdl, "_missav_search",
                        lambda c, h: (None, "**__搜索页被 Cloudflare 拦截，请稍后再试__**"))
    monkeypatch.setattr(ytdl, "_getav_search", lambda c, h: (None, None))
    merged, err = ytdl._search_both("SSIS-405", ("missav.ai",), ("getav.net",))
    assert merged is None and "Cloudflare" in err


def test_getav_search_parses_video_anchors(ytdl, monkeypatch):
    html = (
        '<div class="videos">'
        '<a href="/zh/videos/ssis-405"><img src="//getav.net/th/1.jpg" alt="标题一">标题一</a>'
        '<a href="/zh/videos/fc2-ppv-2761664"><img src="//getav.net/th/2.jpg" alt="FC2">FC2 作品</a>'
        "</div>")
    fake_page = __import__("types").SimpleNamespace(status_code=200, text=html)
    monkeypatch.setattr(
        ytdl, "_http_get", lambda url, headers=None: (fake_page, None))
    results, err = ytdl._getav_search("SSIS-405", ("getav.net",))
    assert err is None
    assert [r["href"] for r in results] == [
        "https://getav.net/zh/videos/ssis-405",
        "https://getav.net/zh/videos/fc2-ppv-2761664"]
    assert results[0]["source"] == "getav"


def test_search_pick_back_restores_results(ytdl, monkeypatch):
    """↩️ 返回：恢复结果卡片（markup 回结果按钮），prompt 保留可重选。"""
    _queue_state(monkeypatch)
    monkeypatch.setattr(
        ytdl, "_missav_search", lambda code, hosts: (_search_results(ytdl, n=3), None))
    msg = _Card()
    asyncio.run(ytdl.start_missav_search(msg, "SSIS-405"))
    prompt = ytdl._SEARCH_PROMPTS[42]
    import re as _re
    pick = _FakeQuery(42, f"srch:{prompt['token']}:1",
                      _re.compile(r"^srch:([0-9a-f]+):(\d+)$"))
    asyncio.run(ytdl.search_pick_callback(None, pick))
    assert prompt["picked"]["href"].endswith("ssis-405-x")
    back = _FakeQuery(42, f"srchact:{prompt['token']}:back",
                      _re.compile(r"^srchact:([0-9a-f]+):(dl|back)$"))
    asyncio.run(ytdl.search_action_callback(None, back))
    assert "picked" not in prompt
    assert prompt["card"].reply_markup is not None
    # 返回后可再次选择
    pick2 = _FakeQuery(42, f"srch:{prompt['token']}:0",
                       _re.compile(r"^srch:([0-9a-f]+):(\d+)$"))
    asyncio.run(ytdl.search_pick_callback(None, pick2))
    assert prompt["picked"]["href"].endswith("ssis-405")


def test_search_pick_action_card_has_preview_url(ytdl, monkeypatch):
    """操作卡片带 🌐 预览网页 的 URL 按钮（浏览器直开）。"""
    _queue_state(monkeypatch)
    monkeypatch.setattr(
        ytdl, "_missav_search", lambda code, hosts: (_search_results(ytdl, n=1), None))
    msg = _Card()
    asyncio.run(ytdl.start_missav_search(msg, "SSIS-405"))
    prompt = ytdl._SEARCH_PROMPTS[42]
    import re as _re
    pick = _FakeQuery(42, f"srch:{prompt['token']}:0",
                      _re.compile(r"^srch:([0-9a-f]+):(\d+)$"))
    asyncio.run(ytdl.search_pick_callback(None, pick))
    markup = prompt["card"].reply_markup
    url_buttons = [b for row in markup.buttons for b in row if b.url]
    assert any("https://missav.ai/ssis-405" == b.url for b in url_buttons)


def test_avsea_search_parses_results(ytdl, monkeypatch):
    """avsea（missav 同引擎克隆）搜索解析：host 限定 avsea.site。"""
    html = (
        '<a href="/ssis-405"><img src="//avsea.site/th/1.jpg" alt="标题">SSIS-405 标题</a>'
        '<a href="/tags/HD">HD</a>')
    fake_page = __import__("types").SimpleNamespace(status_code=200, text=html)
    monkeypatch.setattr(
        ytdl, "_http_get", lambda url, headers=None: (fake_page, None))
    results, err = ytdl._avsea_search("SSIS-405")
    assert err is None
    assert len(results) == 1
    assert results[0]["href"] == "https://avsea.site/ssis-405"
    assert results[0]["source"] == "avsea"    # _avsea_search 统一打来源标


def test_search_both_includes_avsea(ytdl, monkeypatch):
    monkeypatch.setattr(ytdl, "_missav_search", lambda c, h: ([], None))
    monkeypatch.setattr(ytdl, "_getav_search", lambda c, h: ([], None))
    monkeypatch.setattr(ytdl, "_avsea_search",
                        lambda c: ([{"title": "A", "href": "https://avsea.site/ssis-405",
                                     "thumb": "", "badges": "无码破解·中文字幕",
                                     "source": "missav"}], None))
    merged, err = ytdl._search_both("SSIS-405", ("missav.ai",), ("getav.net",))
    assert err is None and len(merged) == 1
