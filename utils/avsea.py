# Copyright (c) 2025 devgagan : https://github.com/devgaganin.
# Licensed under the GNU General Public License v3.0.

"""avsea.site 管线：搜索解析、线路提取（Nuxt UrlHls）、下载（复用 missav HLS 核心）。

avsea 与 missav 同族但架构不同：Nuxt SSR + videojs/hls.js，播放数据在
``__NUXT_DATA__`` JSON 里（``{"type":"UrlHls","value":m3u8,"label":...}``），
缺失页是**硬 404**（无 missav 式幻影别名页）。版本由 slug 后缀区分：
``-uncensored`` = 无码破解（标题带「[无码破解]」）。
"""

import asyncio
import html as html_lib
import json
import logging
import re
from urllib.parse import quote, urljoin, urlparse

from utils.missav import (
    MissAVError,
    _CHROME_UA,
    _download_hls_core,
    _http_get,
    _looks_blocked,
)

logger = logging.getLogger(__name__)

AVSEA_HOST = "avsea.site"
AVSEA_BASE = f"https://{AVSEA_HOST}"
DEFAULT_HOSTS = (AVSEA_HOST,)

_OG_TITLE_RE = re.compile(r'property="og:title" content="([^"]+)"')
_OG_IMAGE_RE = re.compile(r'property="og:image" content="([^"]+)"')
_GENRE_RE = re.compile(r'href="/genre/[^"]*"[^>]*title="([^"]+)"')
_M3U8_RE = re.compile(r'(https?://[^\s"\'<>\\]+?\.m3u8[^\s"\'<>\\]*)')
_NUXT_RE = re.compile(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def parse_avsea_url(url, hosts=DEFAULT_HOSTS):
    """``https://avsea.site/movies/royd-159-uncensored`` -> {'host','slug'}。

    非 avsea 域名、非 /movies/ 路径、空 slug 均返回 None。
    """
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except (ValueError, TypeError):
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {h.lower() for h in hosts}:
        return None
    parts = [seg for seg in parsed.path.split("/") if seg]
    if parts and parts[0] == "movies":
        parts = parts[1:]
    if not parts:
        return None
    slug = parts[0].lower()
    if not _SLUG_RE.match(slug):
        return None
    return {"host": host, "slug": slug}


def is_avsea_url(url):
    return parse_avsea_url(url) is not None


def _page(url):
    """单次 GET 页面文本；失败/被拦返回 None（永不抛出）。"""
    resp, err = _http_get(url, headers={"User-Agent": _CHROME_UA})
    if resp is None or resp.status_code != 200:
        logger.info("avsea page unavailable %s: %s",
                    url, err or getattr(resp, "status_code", "?"))
        return None
    text = resp.text or ""
    if _looks_blocked(resp, text):
        logger.info("avsea page blocked %s (status %s)", url, resp.status_code)
        return None
    return text


def _nuxt_hls_entries(html):
    """``__NUXT_DATA__`` 里的 UrlHls 线路条目 -> [{'label','value'}]。"""
    m = _NUXT_RE.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    out = []

    def walk(o):
        if isinstance(o, dict):
            t = str(o.get("type") or "")
            if t == "UrlHls" and isinstance(o.get("value"), str) and o["value"]:
                out.append({"label": str(o.get("label") or ""), "value": o["value"]})
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return out


def _regex_hls(html):
    """兜底：全文扫 m3u8 URL（去重），默认线路名。"""
    seen = []
    for m in _M3U8_RE.finditer(html):
        u = m.group(1)
        if u not in seen:
            seen.append(u)
    return [{"label": f"线路{i + 1}", "value": u} for i, u in enumerate(seen)]


def extract_lines(html):
    """播放线路列表 [{'label','value'}]：NUXT 结构优先，正则兜底。"""
    entries = _nuxt_hls_entries(html)
    if entries:
        return entries
    return _regex_hls(html)


def _og_title(html):
    m = _OG_TITLE_RE.search(html or "")
    return html_lib.unescape(m.group(1)).strip() if m else ""


def extract_details(html, url):
    """影片页 -> caption 原料 {'code','title','actresses','genres','badges',...}。

    og:title 形如 ``ROYD-159 无码免费在线看, 森日向子, <简介>``：首段为
    番号+站点标记，第二段为演员，末段为简介。badges 由 slug 与标题
    （``[无码破解]``）推导。字段独立降级。
    """
    info = parse_avsea_url(url) or {}
    slug = info.get("slug", "")
    og = _og_title(html)
    segments = [p.strip() for p in og.split(",")] if og else []

    head = segments[0] if segments else ""
    code = (head.split()[0].upper() if head.split() else slug.upper())
    title = " ".join(segments[2:]) if len(segments) > 2 else head
    title = title.replace("[无码破解]", "").strip()
    title = re.sub(rf"^{re.escape(code)}\s*", "", title).strip()
    title = re.sub(r"\s*-\s*(?:avsea\.one|avs.*)$", "", title).strip()

    badges = []
    if "-uncensored" in slug or "无码" in head or "无码" in title:
        badges.append("无码破解")
    if "中文字幕" in og:
        badges.append("中文字幕")

    actress = segments[1] if len(segments) > 1 else ""
    genres = []
    seen = []
    for g in _GENRE_RE.findall(html or ""):
        g = g.strip()
        if g and g not in seen:
            seen.append(g)
            genres.append(g)

    img_m = _OG_IMAGE_RE.search(html or "")
    return {
        "code": code,
        "title": title,
        "actresses": [actress] if actress else [],
        "genres": genres[:6],
        "badges": badges,
        "studio": "avsea",
        "release_date": "",
        "thumbnail": html_lib.unescape(img_m.group(1)) if img_m else "",
    }


def fetch_avsea_movie(url):
    """影片页 -> {'lines','details','thumbnail'}；错误契约同 fetch_video_page。"""
    text = _page(url)
    if text is None:
        raise MissAVError(f"avsea 页面获取失败（被拦或不存在）: {url}")
    lines = extract_lines(text)
    if not lines:
        raise MissAVError("未找到播放线路（页面改版）")
    details = extract_details(text, url)
    img_m = _OG_IMAGE_RE.search(text)
    thumbnail = html_lib.unescape(img_m.group(1)) if img_m else ""
    return {"lines": lines, "details": details, "thumbnail": thumbnail,
            "url": url}


def discover_avsea_variants(url):
    """avsea 版本探测：当前页 与 -uncensored 姊妹页。

    avsea 缺失页是硬 404（无幻影别名），存在性即真实性。返回
    [(variant, url, label)]，排序 原版 < 无码破解；非 avsea URL 返回 []。
    """
    info = parse_avsea_url(url)
    if not info:
        return []
    slug = info["slug"]
    base_slug = slug[:-len("-uncensored")] if slug.endswith("-uncensored") else slug
    candidates = []
    if slug == base_slug:
        candidates.append(("raw", f"{AVSEA_BASE}/movies/{base_slug}"))
        candidates.append(("uc", f"{AVSEA_BASE}/movies/{base_slug}-uncensored"))
    else:
        candidates.append(("uc", url))
        candidates.append(("raw", f"{AVSEA_BASE}/movies/{base_slug}"))
    labels = {"raw": "原版", "uc": "无码破解"}
    found = []
    for variant, candidate in candidates:
        page = _page(candidate)
        if page is None:
            continue
        found.append((variant, candidate, labels[variant]))
    order = {"raw": 0, "uc": 1}
    found.sort(key=lambda item: order[item[0]])
    return found


async def download_avsea(url, dest_path, *, hosts=DEFAULT_HOSTS,
                         concurrency=8, progress=None, task_id=None):
    """avsea 影片页 -> 线路提取 -> 复用通用 HLS 核心下载到 ``dest_path``。

    契约与 ``download_missav`` 一致（并发/进度/守卫全部复用）；线路取
    页面第一条（多线路站方同源多 CDN，内容一致）。
    """
    movie = await asyncio.to_thread(fetch_avsea_movie, url)
    if progress:
        await progress(0, 1, "page")
    line = movie["lines"][0]
    referer = urlparse(url).hostname or AVSEA_HOST
    info = {"title": movie["details"].get("title") or movie["details"].get("code") or "",
            "thumbnail": movie.get("thumbnail") or ""}
    logger.info("avsea download task=%s line=%s m3u8=%s",
                task_id, line.get("label") or "1", line["value"][:80])
    return await _download_hls_core(
        line["value"], dest_path, referer, info, movie["details"],
        concurrency=concurrency, progress=progress, task_id=task_id)
