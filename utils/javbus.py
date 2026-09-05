# Copyright (c) 2025 devgagan : https://github.com/devgaganin.
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

"""JavBus 资料补全（issue #21 D4）+ 演员 CN/JP 分离与词库集成（v2）。

:func:`fetch_javbus_meta` 抓取 ``https://www.javbus.com/{code}`` 影片页，
尽力而为地解析标题、片商、发行日期、类型标签、封面大图与演员名。
Cloudflare 拦截、404、超时、解析失败一律返回 ``None``——单次尝试、
短超时、不轮换不重试：补全是锦上添花，绝不为它冒险。进程内 LRU
缓存（只缓存成功解析）让同一番号的重复下载不再吃第二次 CF。

:func:`enrich_details` 是下载管线的唯一入口：用 javbus 元数据补全
caption 原料（studio / release_date / title / genres 只补缺不覆盖），
中文演员名写入独立的 ``actresses_cn``（javbus CN star 名优先，missav
``/cn/`` 页探测兜底）；``actresses`` 保留源 JP 名，不再拼接。序位对应
的 jp/cn 对回写词库学习（:mod:`utils.avdict`），genres 归一为 CN 规范
名并派生 ``categories``。成功补全后落 av_code 番号快照；网络源全失败
时回放快照补缺。任何异常都被吞掉并原样返回 ``details``——补全永不致
失败下载。

测试约定与 ``utils/missav`` 相同：全部网络经由 :func:`_http_get`，
monkeypatch 替换后即可离线覆盖；``_cache_clear()`` 清 LRU。
"""

import logging
import re
from collections import OrderedDict
from html import unescape as html_unescape

logger = logging.getLogger(__name__)

JAVBUS_HOST = "www.javbus.com"
PAGE_TIMEOUT = 10               # 短超时：补全不拖慢下载管线
PAGE_MAX_BYTES = 2 * 1024 * 1024

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _CHROME_UA,
    # javbus.com 本身中文化：中文 Accept-Language 拿中文片商/类型名
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

GENRES_MAX = 6      # D4 拍板「抓主要标签」：类型截前 6 个
_CACHE_MAX = 256    # LRU 容量：进程生命周期内同一番号只抓一次


# ─── network layer (single test seam) ──────────────────────────────────────────

_SESSION = None


def _get_session():
    """curl_cffi Chrome-impersonating session; plain requests fallback.

    Independent from utils.missav's session on purpose: JavBus is a
    best-effort side channel whose failures must never touch the
    download pipeline's connection pool state.
    """
    global _SESSION
    if _SESSION is None:
        try:
            from curl_cffi import requests as cffi_requests
            _SESSION = cffi_requests.Session(impersonate="chrome")
        except ImportError:
            import requests
            _SESSION = requests.Session()
    return _SESSION


class _Page:
    """Minimal response facade: status_code / text / headers / final url."""

    __slots__ = ("status_code", "text", "headers", "url")

    def __init__(self, status_code, body, headers=None, url=None):
        self.status_code = status_code
        self.text = body.decode("utf-8", errors="replace")
        self.headers = headers or {}
        self.url = url


def _redirected_off_domain(resp, requested_url):
    """True when the FINAL response host left javbus.com (review: the
    session follows redirects, so the request-time URL is not enough)."""
    from urllib.parse import urlparse
    final = urlparse(getattr(resp, "url", None) or requested_url).hostname or ""
    host = final.lower().removeprefix("www.")
    return host != JAVBUS_HOST.removeprefix("www.")


def _http_get(url, timeout=PAGE_TIMEOUT, max_bytes=PAGE_MAX_BYTES):
    """Streaming GET with a hard body cap -> (page|None, error|None).

    Same contract as ``utils.missav._http_get`` but on the JavBus
    session/headers; never raises.
    """
    try:
        resp = _get_session().get(
            url, headers=_HEADERS, timeout=timeout, stream=True)
        try:
            chunks = []
            received = 0
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                received += len(chunk)
                if max_bytes is not None and received > max_bytes:
                    return None, f"body too large: {received} > {max_bytes}"
                chunks.append(chunk)
            return (
                _Page(resp.status_code, b"".join(chunks),
                      getattr(resp, "headers", None),
                      getattr(resp, "url", url)),
                None,
            )
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
    except Exception as exc:  # network/timeout/TLS
        return None, str(exc)


def _page_html(page):
    """200 + 非 Cloudflare 拦截页 -> 正文，否则 None。"""
    if page is None or page.status_code != 200:
        return None
    text = page.text or ""
    head = text[:4096].lower()
    if "just a moment" in head or "cf-challenge" in head:
        return None
    return text


# ─── page parsing (pure) ───────────────────────────────────────────────────────

_ROW_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.DOTALL | re.IGNORECASE)
_ROW_LABEL_RE = re.compile(
    r'<span\s+class="header"[^>]*>(.*?)</span>', re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_H3_RE = re.compile(r"<h3\b[^>]*>(.*?)</h3>", re.DOTALL | re.IGNORECASE)
_TITLE_TAG_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_BIG_IMAGE_RE = re.compile(
    r'<a\b[^>]*class="[^"]*\bbigImage\b[^"]*"[^>]*?href="([^"]+)"',
    re.IGNORECASE)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _clean(fragment):
    return html_unescape(_TAG_RE.sub("", fragment)).strip()


def _labelled_rows(html):
    """{label: row body after the label} for javbus info-panel rows.

    Rows render like ``<p><span class="header">片商:</span> <a …>X</a></p>``.
    """
    rows = {}
    for m in _ROW_RE.finditer(html):
        inner = m.group(1)
        lm = _ROW_LABEL_RE.search(inner)
        if not lm:
            continue
        label = _clean(lm.group(1))
        if label:
            rows[label] = inner[lm.end():]
    return rows


def _find_label(rows, *keywords):
    """First labelled row whose label contains any keyword ('發行日期'…)."""
    for label, body in rows.items():
        if any(keyword in label for keyword in keywords):
            return body
    return ""


def parse_javbus_page(html):
    """影片页 HTML -> 元数据 dict；一个字段都读不出 -> None（解析失败）。

    Returns {'title','studio','release_date','genres','cover','actresses'}
    — every field degrades independently (layout changes must not turn a
    usable page into a hard failure).
    """
    rows = _labelled_rows(html)

    title = ""
    m = _H3_RE.search(html)
    if m:
        title = _clean(m.group(1))
    if not title:
        m = _TITLE_TAG_RE.search(html)
        if m:
            title = re.sub(
                r"\s*[-–|]\s*JavBus\s*$", "",
                _clean(m.group(1)), flags=re.IGNORECASE).strip()

    studio = ""
    studio_body = _find_label(rows, "片商", "製作", "制作")
    if studio_body:
        m = _ANCHOR_RE.search(studio_body)
        studio = _clean(m.group(2)) if m else _clean(studio_body)

    release_date = ""
    m = _DATE_RE.search(
        _clean(_find_label(rows, "發行日期", "发行日期", "Release")))
    if m:
        release_date = m.group(0)

    genres = []
    for m in _ANCHOR_RE.finditer(_find_label(rows, "類別", "类别", "Genre")):
        genre = _clean(m.group(2))
        if genre and genre not in genres:
            genres.append(genre)
    genres = genres[:GENRES_MAX]

    actresses = []
    for m in _ANCHOR_RE.finditer(html):
        if "/star/" not in m.group(1):
            continue
        name = _clean(m.group(2))
        if name and name not in actresses:
            actresses.append(name)

    cover = ""
    m = _BIG_IMAGE_RE.search(html)
    if m:
        cover = html_unescape(m.group(1)).strip()

    if not any((title, studio, release_date, cover, genres, actresses)):
        return None
    return {
        "title": title,
        "studio": studio,
        "release_date": release_date,
        "genres": genres,
        "cover": cover,
        "actresses": actresses,
    }


# ─── in-process LRU (successes only; misses must not stick forever) ────────────

_cache = OrderedDict()


def _cache_clear():
    """Test seam: wipe the metadata LRU."""
    _cache.clear()


# ─── public entry points ───────────────────────────────────────────────────────

_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,30}$")


def fetch_javbus_meta(code):
    """番号 -> javbus 影片元数据 dict；失败一律 None（永不抛出）。

    单次 GET、短超时、不轮换不重试；LRU 只缓存成功解析，命中后同一
    番号不再吃一次 Cloudflare。
    """
    code = str(code or "").strip().upper()
    if not _CODE_RE.match(code):
        return None
    meta = _cache.get(code)
    if meta is not None:
        _cache.move_to_end(code)
        return meta

    try:
        page, err = _http_get(f"https://{JAVBUS_HOST}/{code}")
        if page is not None and _redirected_off_domain(
                page, f"https://{JAVBUS_HOST}/{code}"):
            logger.info("javbus redirected off-domain for %s", code)
            return None
        html = _page_html(page)
        meta = parse_javbus_page(html) if html else None
    except Exception:  # a broken seam must never surface into the pipeline
        logger.info("javbus meta fetch failed %s", code, exc_info=True)
        return None
    if meta is None:
        logger.info("javbus meta unavailable %s: %s",
                    code, err or getattr(page, "status_code", "unparseable"))
        return None

    _cache[code] = meta
    _cache.move_to_end(code)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return meta


def enrich_details(details, url=None):
    """caption 原料的最小侵入升级：javbus 补全 + 词库集成（永不抛出）。

    D4: studio / release_date / title / genres 只补缺，不覆盖来源页
    自己的解析结果。v2: 中文演员名独立写入 ``actresses_cn``（javbus
    演员名优先；未命中且 ``url`` 非 cn 语言时再探测一次 missav
    ``/cn/`` 页），``actresses`` 保持源 JP 名。学习/归一/快照细节见
    :func:`_enrich_details`。失败静默返回原 ``details``。
    """
    try:
        return _enrich_details(details, url)
    except Exception:
        logger.debug("javbus enrich failed", exc_info=True)
        return details


# ─── JavLibrary CN（补充源：FC2 有收录；JavBus 404/被拦时的兜底） ────────────────

JAVLIBRARY_BASE = "https://www.javlibrary.com/cn"
_JL_CACHE = OrderedDict()
_JL_CACHE_MAX = _CACHE_MAX

_JL_TITLE_RE = re.compile(
    r'<h1[^>]*id="video_title"[^>]*>\s*<a[^>]*>(.*?)</a>', re.S | re.I)
_JL_DATE_RE = re.compile(r'id="video_date"[^>]*>\s*([0-9]{4}-[0-9]{2}-[0-9]{2})', re.I)
_JL_MAKER_RE = re.compile(r'id="video_maker".*?<a[^>]*>(.*?)</a>', re.S | re.I)
_JL_GENRES_BLOCK = re.compile(r'id="video_genres"(.*?)</div>', re.S | re.I)
_JL_CAST_BLOCK = re.compile(r'id="video_cast"(.*?)</div>', re.S | re.I)
_JL_ANCHOR_RE = re.compile(r'<a[^>]*>(.*?)</a>', re.S | re.I)
_JL_COVER_RE = re.compile(r'id="video_jacket"[^>]*src="([^"]+)"', re.I)
_JL_SEARCH_ANCHOR_RE = re.compile(
    r'<a[^>]*href="([^"]*\?v=[a-z0-9]+[^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
_JL_CODE_TOKEN_RE = re.compile(r"[A-Z][A-Z0-9]*-\d+")


def _strip_tags(text):
    return html_unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _jl_off_domain(resp, requested_url):
    from urllib.parse import urlparse
    final = urlparse(getattr(resp, "url", None) or requested_url).hostname or ""
    host = final.lower().removeprefix("www.")
    return host != "javlibrary.com"


def parse_javlibrary_page(html):
    """影片页 HTML -> 元数据 dict；字段独立降级，全空返回 None。"""
    if not html:
        return None
    title_m = _JL_TITLE_RE.search(html)
    if not title_m:
        return None
    title = _strip_tags(title_m.group(1))
    # 标题习惯以番号开头（"DASS-629 私に…"）：去掉番号前缀
    title = re.sub(r"^[A-Za-z0-9-]+\s*", "", title).strip()

    def block_names(block_re):
        m = block_re.search(html)
        if not m:
            return []
        seen, out = [], []
        for a in _JL_ANCHOR_RE.finditer(m.group(1)):
            name = _strip_tags(a.group(1))
            if name and name not in seen:
                seen.append(name)
                out.append(name)
        return out

    date_m = _JL_DATE_RE.search(html)
    cover_m = _JL_COVER_RE.search(html)
    meta = {
        "title": title,
        "actresses": block_names(_JL_CAST_BLOCK),
        "genres": block_names(_JL_GENRES_BLOCK)[:GENRES_MAX],
        "studio": _strip_tags(_JL_MAKER_RE.search(html).group(1)) if _JL_MAKER_RE.search(html) else "",
        "release_date": date_m.group(1) if date_m else "",
        "cover": cover_m.group(1) if cover_m else "",
    }
    if not any((meta["title"], meta["actresses"], meta["genres"])):
        return None
    return meta


def _jl_cache_get(code):
    meta = _JL_CACHE.get(code)
    if meta is not None:
        _JL_CACHE.move_to_end(code)
    return meta


def _jl_cache_put(code, meta):
    _JL_CACHE[code] = meta
    _JL_CACHE.move_to_end(code)
    while len(_JL_CACHE) > _JL_CACHE_MAX:
        _JL_CACHE.popitem(last=False)


def fetch_javlibrary_meta(code):
    """番号 -> JavLibrary CN 元数据 dict；失败一律 None（永不抛出）。

    流程：keyword 搜索（唯一命中时站点直接重定向到影片页）；多结果页
    则按番号 token 精确匹配挑候选再进影片页（防前缀误配，与 getav
    兜底同规则）。重定向最终 host 必须仍在 javlibrary.com。
    """
    code = str(code or "").strip().upper()
    if not _CODE_RE.match(code):
        return None
    cached = _jl_cache_get(code)
    if cached is not None:
        return cached

    from urllib.parse import quote, urljoin
    search_url = f"{JAVLIBRARY_BASE}/vl_searchbyid.php?keyword={quote(code)}"
    html = None
    try:
        page, err = _http_get(search_url)
        if page is not None and _jl_off_domain(page, search_url):
            logger.info("javlibrary redirected off-domain for %s", code)
            return None
        html = _page_html(page)
    except Exception:
        logger.info("javlibrary search failed %s", code, exc_info=True)
        return None
    if not html:
        return None

    if 'id="video_title"' not in html:
        # 多候选列表页：按标题内番号 token 精确匹配挑一个
        want = re.sub(r"[^A-Z0-9]", "", code)
        picked = None
        for m in _JL_SEARCH_ANCHOR_RE.finditer(html):
            text = _strip_tags(m.group(2)).upper()
            token = _JL_CODE_TOKEN_RE.search(text)
            if token and re.sub(r"[^A-Z0-9]", "", token.group(0)) == want:
                picked = m.group(1)
                break
        if not picked:
            logger.info("javlibrary no exact-code candidate for %s", code)
            return None
        detail_url = urljoin(JAVLIBRARY_BASE + "/", picked)
        try:
            page2, err2 = _http_get(detail_url)
            if page2 is not None and _jl_off_domain(page2, detail_url):
                logger.info("javlibrary detail redirected off-domain for %s", code)
                return None
            html = _page_html(page2)
        except Exception:
            logger.info("javlibrary detail failed %s", code, exc_info=True)
            return None
        if not html or 'id="video_title"' not in html:
            return None

    meta = parse_javlibrary_page(html)
    if meta is None:
        return None
    _jl_cache_put(code, meta)
    return meta


def _javbus_source_meta(code):
    return fetch_javbus_meta(code)


def _javlibrary_source_meta(code):
    return fetch_javlibrary_meta(code)


def _getav_source_meta(code):
    try:
        from utils.missav import find_getav_details_for_code
        return find_getav_details_for_code(code)
    except Exception:
        return None


def _enrich_details(details, url):

    if not isinstance(details, dict):
        return details

    from utils import avdict  # 延迟导入：与 utils.missav 同款，加载期零依赖

    code = str(details.get("code") or "")
    cn_names = []   # 网络源提供的中文演员名（保序去重）
    hit = False     # 任一网络源吐出 meta（区别于"全失败回放"路径）
    gained = False  # 本次 enrich 对 details 有实际增量
    if code.upper().startswith("FC2"):
        # JavBus does not catalog FC2: getav usually carries the release
        # with full Chinese metadata; JavLibrary CN lists FC2 too.
        sources = (_getav_source_meta, _javlibrary_source_meta)
    else:
        sources = (_javbus_source_meta, _javlibrary_source_meta)
    for fetch in sources:
        try:
            meta = fetch(code) if code else None
        except Exception:
            meta = None
        if not meta:
            continue
        contributed = False  # 本源真正补到东西才记为命中（仅封面页不算）
        if not details.get("title") and meta.get("title"):
            details["title"] = meta["title"]
            gained = contributed = True
        if not details.get("genres") and meta.get("genres"):
            details["genres"] = list(meta["genres"][:GENRES_MAX])
            gained = contributed = True
        if not details.get("studio") and meta.get("studio"):
            details["studio"] = meta["studio"]
            gained = contributed = True
        if not details.get("release_date") and meta.get("release_date"):
            details["release_date"] = meta["release_date"]
            gained = contributed = True
        for name in meta.get("actresses") or []:
            if name and name not in cn_names:
                cn_names.append(name)
                contributed = True
        if contributed:
            hit = True
        if details.get("genres") and details.get("actresses") and cn_names:
            break  # nothing left worth another network round

    if not cn_names and url:
        cn_names = _missav_cn_actresses(url)

    # v2：中文名独立成列，不再与 JP 名拼接。javbus 提供的名排前，
    # details 已有的 actresses_cn（getav starsZh 场景）去重并入尾部。
    existing_cn = details.get("actresses_cn")
    if not isinstance(existing_cn, list):
        existing_cn = []
    merged = []
    for name in list(cn_names) + list(existing_cn):
        name = name.strip() if isinstance(name, str) else ""
        if name and name not in merged:
            merged.append(name)
    if merged:
        details["actresses_cn"] = merged

    # 词库读路径（对照）：源 JP 名在合并后的 cn 列表里没有对应中文名时，
    # 查词库补——这是词库在生产里的主要消费点。
    jps = [j for j in (details.get("actresses") or []) if isinstance(j, str)]
    for jp in jps:
        jp = jp.strip()
        if not jp:
            continue
        cn = avdict.actress_cn(jp)
        if cn and cn not in merged and cn != jp:
            merged.append(cn)
    if merged:
        details["actresses_cn"] = merged

    # 词库学习（写路径）：仅当两侧数量一致时才按位配对回写——跨站演员
    # 排序不保证一致，长度不齐的 zip 会把错误映射永久写进词库；
    # 两边同名说明该站没有中文译名，不算对照、不入库。
    if cn_names and len(jps) == len(cn_names):
        for jp, cn in zip(jps, cn_names):
            jp = jp.strip()
            cn = cn.strip()
            if jp and cn and cn != jp:
                avdict.record_actress(jp, cn, source="javbus")

    # genres 归一为 CN 规范名 + 派生 categories
    genres = details.get("genres")
    if isinstance(genres, list) and genres:
        tags, cats = avdict.classify(genres)
        if tags:
            gained = gained or tags != genres
            details["genres"] = tags
        if cats:
            details["categories"] = cats

    if code and not hit:
        # 网络源全失败：回放番号快照，只补缺不覆盖；补到的 genres 也归一
        snapshot = avdict.code_meta_load(code)
        if snapshot:
            for key, value in snapshot.items():
                if not details.get(key):
                    details[key] = value
            replay = details.get("genres")
            if isinstance(replay, list) and replay:
                tags, cats = avdict.classify(replay)
                if tags:
                    details["genres"] = tags
                if cats:
                    details["categories"] = cats

    if code and (gained or merged or details.get("categories")):
        # 成功 enrich（有增量或 cn/分类非空）：落番号快照，供下次全失败回放
        avdict.code_meta_save(code, details)
    return details


def _missav_cn_actresses(url):
    """missav ``/cn/`` 页演员名（一次探测、静默失败）；[] 表示未命中。

    The user's URL keeps its slug — only the language segment is swapped
    for ``cn``. Already-cn pages are skipped: their panel already fed
    ``details``. Non-missav URLs never trigger a request.
    """
    try:
        from utils.missav import extract_video_details, parse_missav_url
        info = parse_missav_url(url)
        if not info or info.get("lang") == "cn":
            return []
        cn_url = f"https://{info['host']}/cn/{info['slug']}"
        page, err = _http_get(cn_url)
        html = _page_html(page)
        if not html:
            logger.info("missav cn page unavailable %s: %s",
                        cn_url, err or getattr(page, "status_code", "?"))
            return []
        return extract_video_details(html, cn_url).get("actresses") or []
    except Exception:
        logger.debug("missav cn actress probe failed", exc_info=True)
        return []
