# Copyright (c) 2025 devgagan : https://github.com/devgaganin.
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

"""JavBus 资料补全（issue #21 D4）+ 演员 CN/JP 双名（D5）。

:func:`fetch_javbus_meta` 抓取 ``https://www.javbus.com/{code}`` 影片页，
尽力而为地解析标题、片商、发行日期、类型标签、封面大图与演员名。
Cloudflare 拦截、404、超时、解析失败一律返回 ``None``——单次尝试、
短超时、不轮换不重试：补全是锦上添花，绝不为它冒险。进程内 LRU
缓存（只缓存成功解析）让同一番号的重复下载不再吃第二次 CF。

:func:`enrich_details` 是下载管线的唯一入口：用 javbus 元数据补全
caption 原料（studio / release_date / title / genres 只补缺不覆盖），
并把演员列表升级为「中文名 (日文名)」。javbus 未命中演员名时最多
再探测一次 missav ``/cn/`` 页（复用 :func:`utils.missav.extract_video_details`
的既有解析）。任何异常都被吞掉并原样返回 ``details``——补全永不致
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
    """Minimal response facade: status_code / text / headers."""

    __slots__ = ("status_code", "text", "headers")

    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self.text = body.decode("utf-8", errors="replace")
        self.headers = headers or {}


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
                      getattr(resp, "headers", None)),
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
    """caption 原料的最小侵入升级：javbus 补全 + 演员双名（永不抛出）。

    D4: studio / release_date / title / genres 只补缺，不覆盖来源页
    自己的解析结果。D5: 演员列表元素升级为「中文名 (日文名)」——
    javbus 演员名优先；未命中且 ``url`` 非 cn 语言时再探测一次 missav
    ``/cn/`` 页。失败静默返回原 ``details``。
    """
    try:
        return _enrich_details(details, url)
    except Exception:
        logger.debug("javbus enrich failed", exc_info=True)
        return details


def _enrich_details(details, url):
    if not isinstance(details, dict):
        return details

    cn_names = []
    meta = fetch_javbus_meta(details["code"]) if details.get("code") else None
    if meta:
        if not details.get("studio") and meta.get("studio"):
            details["studio"] = meta["studio"]
        if not details.get("release_date") and meta.get("release_date"):
            details["release_date"] = meta["release_date"]
        if not details.get("title") and meta.get("title"):
            details["title"] = meta["title"]
        if not details.get("genres") and meta.get("genres"):
            details["genres"] = list(meta["genres"][:GENRES_MAX])
        cn_names = list(meta.get("actresses") or [])

    if not cn_names and url:
        cn_names = _missav_cn_actresses(url)

    actresses = details.get("actresses") or []
    if cn_names and actresses:
        details["actresses"] = _pair_names(actresses, cn_names)
    return details


def _pair_names(original, cn_names):
    """['日文名'] + ['中文名'] -> ['中文名 (日文名)']；仅有单名保持原名。

    Positional pairing: same code = same film, both sites bill actresses
    in the same order. Extra names on either side keep their original.
    """
    out = []
    for i, name in enumerate(original):
        cn = cn_names[i] if i < len(cn_names) else ""
        out.append(f"{cn} ({name})" if cn and cn != name else name)
    return out


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
