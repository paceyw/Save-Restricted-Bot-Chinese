# Copyright (c) 2025 devgagan : https://github.com/devgaganin.
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

"""missav.ai / getav.net downloaders (issue #13 + getav follow-up).

yt-dlp ships no missav/getav extractor, so this module implements the
pipelines directly:

    missav: video page (Cloudflare) -> packed JS -> m3u8 -> HLS segments -> mp4
    getav:  JSON movie API -> media playlist (AES-128) -> HLS segments -> mp4

Key behaviours (validated against the reference implementation in
Alos21750/JableTV-MissAV-Downloader-GUI-2026 and live getav.net traffic,
2026-08-16):

* missav hides the m3u8 URL inside a Dean Edwards ``p,a,c,k,e,d`` eval
  block that must be unpacked before extraction. getav instead serves a
  JSON API (``/api/movies/<slug>``) whose ``videoSources`` entries are
  signed media playlists (disguised as ``index.txt``/``.woff2`` assets
  on static.worldstatic.com) — no page scraping needed.
* Both sit behind Cloudflare: we impersonate Chrome via curl_cffi when
  available (plain requests fallback) and rotate across mirror hosts
  (missav.ai / .ws / .live / missav123.com; getav.net).
* HLS AES-128: a playlist key without an explicit IV uses the segment's
  media-sequence number (``EXT-X-MEDIA-SEQUENCE`` + playlist index) as a
  16-byte big-endian IV. Each segment gets a fresh cipher object because
  cipher objects are not thread-safe.
* Segments are stored under their playlist INDEX, not the URL basename:
  distinct segment URLs can share a basename and would silently corrupt
  the merged output.

All network I/O funnels through :func:`_http_get` so tests can substitute
fixtures without touching the network.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from html import unescape as html_unescape
import tempfile as _tempfile
from urllib.parse import urljoin, urlparse, urlunparse

from config import (BURN_CONCURRENCY, BURN_CRF, BURN_PRESET, BURN_TIMEOUT_S,
                    FFMPEG_BURN_THREADS)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

# ─── constants ─────────────────────────────────────────────────────────────────

DEFAULT_MIRRORS = ("missav.ai", "missav.ws", "missav.live", "missav123.com", "avsea.site")
GETAV_DEFAULT_MIRRORS = ("getav.net",)

_LANG_PREFIXES = ("cn", "en", "ja", "ko", "ms", "th")
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SEGMENT_RETRIES = 3        # attempts per segment before failing the job
SEGMENT_RETRIES_404 = 4    # 段 404 档：CDN 瞬断/限流伪装 404（实测分钟级窗口）
MAX_PLAYLIST_REFRESH = 3   # 404 驱动的播放列表刷新上限，间隔退避 30/60/90s
SEGMENT_RETRIES_RATE_LIMITED = 6   # extended budget while the CDN 429s us
# 502/503/504 gateway brownouts are sustained ~1-2 minutes (edge/origin
# outage observed live on worldstatic 2026-08-16): the deepest budget, so
# a 40-minute job is not killed mid-stream by a transient CDN blip
SEGMENT_RETRIES_SERVER_ERROR = 8
PAGE_TIMEOUT = 20          # seconds, page/playlist/key fetch
SEGMENT_TIMEOUT = 60       # seconds, per TS segment
SEGMENT_CONCURRENCY = 8    # parallel segment downloads
MAX_SEGMENTS = 20_000      # sanity ceiling (~16h of 3s segments)
MAX_SEGMENT_BYTES = 32 * 1024 * 1024   # a legit TS segment is a few MB
MAX_TOTAL_BYTES = 20 * 1024 * 1024 * 1024  # per-job cumulative budget
MAX_DURATION_S = 8 * 3600  # per-job total duration ceiling
MIN_FREE_DISK = 10 * 1024 * 1024 * 1024    # refuse jobs below this free space
PAGE_MAX_BYTES = 8 * 1024 * 1024   # page/playlist body cap
KEY_MAX_BYTES = 64                 # an AES-128 key is exactly 16 bytes
MAX_PLAYLIST_HOPS = 3      # variant -> media playlist depth
PAGE_RETRIES = 4           # attempts for playlist/key/json on transient fails
PAGE_RETRY_BACKOFF = (3, 8, 15)   # seconds between page-level attempts

BLOCKED_MSG = (
    "所有 missav 镜像均被 Cloudflare 拦截或不可达，"
    "请稍后重试或更换网络/代理后重试"
)


class MissAVError(Exception):
    """Generic missav download failure (page layout change, missing video…)."""




class _SegmentStale(MissAVError):
    """分段 404：同 URL 重试无意义，交由 _download_hls_core 刷新播放列表。

    getav/missav 的 m3u8 常带时敏令牌：媒体清单过期后段 URL 集体失效
    （生产实案 ADN-538 段 156 集体 404）。刷新清单换新 URL 才是正解。
    """

    def __init__(self, index, message=""):
        super().__init__(message or f"片段 {index}: HTTP 404")
        self.index = index


class MissAVBlockedError(MissAVError):
    """Every mirror was Cloudflare-blocked or unreachable."""


# ─── URL recognition (pure) ────────────────────────────────────────────────────

def parse_missav_url(url, hosts=DEFAULT_MIRRORS):
    """Return {'host','lang','slug'} for a missav VIDEO page, else None.

    Accepts ``https://missav.ai/cn/sone-543-chinese-subtitle``,
    ``https://missav.ai/sone-543``, ``https://missav.ai/dm1151/092014_887``
    and the mirror domains. Rejects category/listing pages
    (``/dm278/chinese-subtitle``, ``/cn/dm278``, ``/search?q=…``): a video
    slug always contains a digit, and a bare (language-less) slug always
    carries an id separator like ``sone-543`` / ``092014_887``.
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
    if parts and re.fullmatch(r"dm\d+", parts[0]):
        parts = parts[1:]
    lang = None
    if parts and parts[0].lower() in _LANG_PREFIXES:
        lang = parts[0].lower()
        parts = parts[1:]
    # a bare ``dm<digits>`` left after prefix stripping is a category page
    if not parts or re.fullmatch(r"dm\d+", parts[0]):
        return None

    slug = parts[0]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9\-_]*", slug):
        return None
    if not any(ch.isdigit() for ch in slug):
        return None  # "new", "actresses", "chinese-subtitle" …
    if lang is None and not re.search(r"[-_]\d", slug):
        return None  # language-less slugs must look like a video id

    return {"host": host, "lang": lang, "slug": slug}


def is_missav_url(url, hosts=DEFAULT_MIRRORS):
    return parse_missav_url(url, hosts) is not None


def mirror_candidates(url, hosts=DEFAULT_MIRRORS):
    """Same URL on the original host first, then the remaining mirrors."""
    parsed = urlparse(url.strip())
    original = (parsed.hostname or "").lower()
    if original.startswith("www."):
        original = original[4:]
    ordered = [original] + [h for h in hosts if h.lower() != original]

    # Mirrors are 443-only: drop any user-supplied port (probe surface +
    # urlparse().port raises ValueError on out-of-range ports).
    try:
        parsed = parsed._replace(netloc=parsed.hostname or "")
    except ValueError:
        return []

    out = []
    for host in ordered:
        if not host:
            continue
        replaced = parsed._replace(scheme="https", netloc=host)
        out.append(urlunparse(replaced))
    return out


# ─── variant slugs (sister-version detection, issue #17, pure) ─────────────────

# Longest tail first: "-uncensored-leak" must win over "-leak".
_CN_SLUG_TAILS = ("chinese-subtitle", "ch-sub", "c")
_UC_SLUG_TAILS = ("uncensored-leak", "uncensored", "leak")


def _strip_slug_tail(slug, tails):
    """``slug`` minus its first matching ``-<tail>`` (longest first), else None."""
    for tail in tails:
        if slug.endswith("-" + tail):
            return slug[: -(len(tail) + 1)]
    return None


def missav_slug_family(slug):
    """'cn' for a Chinese-subtitled slug, 'uc' for an uncensored one, else None.

    长尾优先 within each tail group. The combined
    ``<base>-uncensored-leak-chinese-subtitle`` counts as 'cn' here; use
    :func:`_slug_variant` when the uncensored half matters too.
    """
    s = (slug or "").lower()
    if _strip_slug_tail(s, _CN_SLUG_TAILS) is not None:
        return "cn"
    if _strip_slug_tail(s, _UC_SLUG_TAILS) is not None:
        return "uc"
    return None


def _slug_variant(slug):
    """Four-state variant of a slug: 'raw' | 'cn' | 'uc' | 'uc-cn'."""
    s = (slug or "").lower()
    without_cn = _strip_slug_tail(s, _CN_SLUG_TAILS)
    if without_cn is not None:
        return "uc-cn" if _strip_slug_tail(without_cn, _UC_SLUG_TAILS) is not None else "cn"
    return "uc" if _strip_slug_tail(s, _UC_SLUG_TAILS) is not None else "raw"


def missav_base_slug(slug):
    """Strip every variant tail (loop, longest first) back to the bare id slug."""
    s = slug or ""
    while True:
        stripped = _strip_slug_tail(s, _CN_SLUG_TAILS)
        if stripped is None:
            stripped = _strip_slug_tail(s, _UC_SLUG_TAILS)
        if stripped is None:
            return s
        s = stripped


def _same_site_variant_url(url, new_slug):
    """Same URL with only the slug segment replaced; dm/lang prefixes kept."""
    parsed = urlparse(url.strip())
    parts = [seg for seg in parsed.path.split("/") if seg]
    idx = 0
    if parts and re.fullmatch(r"dm\d+", parts[0]):
        idx = 1
    if len(parts) > idx and parts[idx].lower() in _LANG_PREFIXES:
        idx += 1
    return urlunparse(parsed._replace(path="/" + "/".join(parts[:idx] + [new_slug])))


# Constructed sister pages, recommended-first (combined > cn > uc).
_VARIANT_BUILDERS = (
    ("cn", "{base}-chinese-subtitle"),
    ("uc", "{base}-uncensored-leak"),
    ("uc-cn", "{base}-uncensored-leak-chinese-subtitle"),
)


def missav_variant_candidates(url, hosts=DEFAULT_MIRRORS):
    """[(variant, url)] to probe around a missav video page.

    First entry is the page itself; the rest are same-host siblings built
    from the base slug. The variant the page already is gets skipped —
    it is covered by the first entry. Empty for non-missav URLs.
    """
    info = parse_missav_url(url, hosts)
    if not info:
        return []
    current = _slug_variant(info["slug"])
    base = missav_base_slug(info["slug"])
    out = [(current, url)]
    for variant, template in _VARIANT_BUILDERS:
        if variant != current:
            out.append((variant, _same_site_variant_url(url, template.format(base=base))))
    return out


# ─── getav URL recognition (pure) ──────────────────────────────────────────────

_GETAV_LOCALE_RE = re.compile(r"[a-z]{2}(?:-[a-z0-9]{2,8})?", re.IGNORECASE)


def parse_getav_url(url, hosts=GETAV_DEFAULT_MIRRORS):
    """Return {'host','lang','slug'} for a getav VIDEO page, else None.

    getav video pages are ``https://getav.net/[<locale>/]videos/<slug>``
    (e.g. ``/zh/videos/cjod-159``, ``/en/videos/fc2-ppv-1234567``; the
    locale-less form 302s to ``/en/...``). The literal ``videos`` path
    segment is the discriminator, so any locale getav ships is accepted;
    listing pages (``/zh/hot``, ``/zh/videos``) and missav-style slugs
    (``/zh/cjod-159``) are rejected. A video slug always contains a digit.
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
    lang = None
    if parts and _GETAV_LOCALE_RE.fullmatch(parts[0]) and len(parts) > 1:
        # a lone /<locale> is the site root; /videos alone is a listing
        lang = parts[0].lower()
        parts = parts[1:]
    if len(parts) != 2 or parts[0].lower() != "videos":
        return None

    slug = parts[1]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9\-_]*", slug):
        return None
    if not any(ch.isdigit() for ch in slug):
        return None

    return {"host": host, "lang": lang, "slug": slug}


def is_getav_url(url, hosts=GETAV_DEFAULT_MIRRORS):
    return parse_getav_url(url, hosts) is not None


def getav_api_candidates(url, hosts=GETAV_DEFAULT_MIRRORS):
    """Movie-API URLs to try: ``https://<mirror>/api/movies/<slug>``.

    Same rotation contract as :func:`mirror_candidates` — original host
    first, user ports dropped (443-only), scheme forced to https.
    """
    info = parse_getav_url(url, hosts)
    if not info:
        return []
    out = []
    for host in dict.fromkeys([info["host"]] + [h.lower() for h in hosts]):
        if host:
            out.append(f"https://{host}/api/movies/{info['slug']}")
    return out


# ─── packed JS unpacking (pure) ────────────────────────────────────────────────

_PACKER_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',\s*(\d+),\s*(\d+),\s*'([^']*)'\s*\.split\('\|'\)",
    re.DOTALL,
)


def unpack_packed_js(script_text):
    """Decode a Dean Edwards p,a,c,k,e,d block; None when absent/malformed.

    Guards mirror the reference implementation: base<=1 makes the radix
    loop diverge and an absurd ``c`` would allocate an unbounded lookup.
    """
    if not script_text or len(script_text) > 4 * 1024 * 1024:
        return None
    match = _PACKER_RE.search(script_text)
    if not match:
        return None
    packed, base, count, keys_str = (
        match.group(1), int(match.group(2)), int(match.group(3)), match.group(4).split("|")
    )
    if base <= 1 or count < 0 or count > 200_000:
        return None
    # Amplification guard: one multi-MB dictionary key substituted at ~1M
    # word positions would blow memory; real packer dictionaries are tiny.
    if len(match.group(4)) > 64 * 1024:
        return None

    digits = "0123456789abcdefghijklmnopqrstuvwxyz"

    def to_base(n, b):
        if n == 0:
            return "0"
        s = ""
        while n:
            s = digits[n % b] + s
            n //= b
        return s

    lookup = {
        to_base(i, base): (keys_str[i] if i < len(keys_str) and keys_str[i] else to_base(i, base))
        for i in range(count)
    }
    out = re.sub(r"\b(\w+)\b", lambda m: lookup.get(m.group(0), m.group(0)), packed)
    return None if len(out) > 8 * 1024 * 1024 else out


# ─── page parsing (pure) ───────────────────────────────────────────────────────

def _meta_content(html, key):
    # property="og:title" content="…" and the reversed attribute order
    for pattern in (
        rf'{key}["\']\s+content=["\']([^"\']*)',
        rf'content=["\']([^"\']*)["\']\s+{key}',
    ):
        m = re.search(pattern, html)
        if m:
            return m.group(1).strip()
    return None


def extract_page_info(html):
    title = _meta_content(html, r'property=["\']og:title')
    thumbnail = _meta_content(html, r'property=["\']og:image')
    return {"title": title or "", "thumbnail": thumbnail}


_PANEL_LABELS = {
    "code": r"番号|番號|Code",
    "orig_title": r"标题|標題|Title",
    "actresses": r"女优|女優|Actress(?:es)?",
    "genres": r"类型|類型|Genre|Tag",
    "release_date": r"发行日期|發行日期|Release",
}


def _panel_section(html, label_key):
    """Return the labelled info-panel section's inner HTML, or ''.

    The video page renders labelled rows like
    ``<span>番号:</span> <span class=…>DASS-629</span>`` and
    ``<span>女优:</span> <a …>百永さりな</a>, …`` inside one div.
    """
    label = _PANEL_LABELS[label_key]
    m = re.search(
        rf"<span>\s*(?:{label})\s*:\s*</span>(.*?)</div>", html, re.DOTALL
    )
    return m.group(1) if m else ""


def _panel_plain(section):
    return re.sub(r"<[^>]+>", "", section).strip(" ,\n\t")


def _panel_links(section):
    out = []
    for m in re.finditer(r"<a\b[^>]*>([^<]+)</a>", section):
        text = html_unescape(m.group(1)).strip()
        if text and text not in out:
            out.append(text)
    return out


_CODE_FROM_SLUG = re.compile(r"^([a-z]{2,7})-?(\d{2,5})", re.IGNORECASE)
_SLUG_BADGES = (
    ("uncensored-leak", "无码破解"),
    ("chinese-subtitle", "中文字幕"),
    ("ch-sub", "中文字幕"),
    ("uncensored-leaked", "无码流出"),
    ("uncensored", "无码"),
    ("leaked", "流出"),
)


GENRES_MAX = 6    # D4 拍板「抓主要标签」：类型 hashtag 截前 6 个
_PANEL_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def extract_video_details(page_html, url):
    """Parse the labelled info panel + og meta into caption ingredients.

    Returns {'code','title','actresses','actresses_cn','genres','badges',
    'studio','release_date'} — every field degrades independently (page
    layout changes must not break downloads). studio is never on the
    missav panel; it starts empty and is filled by the JavBus enrichment.
    actresses keeps the panel's source-language names; actresses_cn
    (契约 v2 中文名) starts empty here — javbus/词库 fills it.
    """
    details = {"code": "", "title": "", "actresses": [], "actresses_cn": [],
               "genres": [], "badges": [], "studio": "", "release_date": ""}

    code = _panel_plain(_panel_section(page_html, "code"))
    # 变体页的面板 code 带版本尾巴（如 ROYD-159-UNCENSORED-LEAK）：剥到纯番号
    code = re.sub(r"(?:-(?:uncensored-leak|chinese-subtitle|ch-sub|uncensored|leaked|leak))+$",
                  "", code, flags=re.IGNORECASE)
    details["code"] = code.upper() if code else ""

    og_title = _meta_content(page_html, r'property=["\']og:title') or ""
    orig_title = _panel_plain(_panel_section(page_html, "orig_title"))

    # Localized intro line: og:title minus the leading code and the
    # trailing " - actress" segment, e.g.
    # "DASS-629 你愿意当我的宠物吗？… - 百永さりな" -> "你愿意当我的宠物吗？…"
    intro = og_title
    if details["code"] and intro.upper().startswith(details["code"]):
        intro = intro[len(details["code"]):].strip()
    intro = re.sub(r"\s*-\s*[^-]+$", "", intro).strip(" -")
    if not intro:
        intro = orig_title
    details["title"] = intro

    details["actresses"] = _panel_links(_panel_section(page_html, "actresses"))
    details["genres"] = _panel_links(_panel_section(page_html, "genres"))[:GENRES_MAX]
    date_m = _PANEL_DATE_RE.search(
        _panel_plain(_panel_section(page_html, "release_date")))
    details["release_date"] = date_m.group(0) if date_m else ""

    slug = (parse_missav_url(url) or {}).get("slug", "") or ""
    lowered = slug.lower()
    badges = [label for token, label in _SLUG_BADGES if token in lowered]
    # keep order but drop the weaker badge when a combined one covers it:
    # "无码破解" (uncensored-leak) subsumes 无码/流出/无码流出,
    # "无码流出" (uncensored-leaked) subsumes 无码/流出
    if "无码破解" in badges:
        badges = [b for b in badges if b not in ("无码", "流出", "无码流出")]
    elif "无码流出" in badges:
        badges = [b for b in badges if b not in ("无码", "流出")]
        badges.append("无码流出")
    details["badges"] = badges

    if not details["code"]:
        m = _CODE_FROM_SLUG.match(slug)
        if m:
            details["code"] = f"{m.group(1).upper()}-{m.group(2)}"
    return details


def _hashtag(text):
    """'#' + text with characters that break Telegram hashtags mapped away.

    Markdown-active characters are mapped too: caption text derived from
    page-controlled titles must not inject formatting into the album.
    """
    cleaned = re.sub(r"[\s#\n\[\]~`*|<>{}]+", "_", html_unescape(text).strip())
    cleaned = cleaned.strip("_")
    return f"#{cleaned}" if cleaned else ""


def build_caption(details, max_len=1024):
    """Caption 骨架（用户定稿 v3）：

        DASS-629\n\n<intro>\n\n演员：#中文名 #日文名…\n标签：#…\n类别：#无码破解 #中文字幕…

    三行 hashtag 行合成一个块（单换行），与简介之间空一行。骨架恒定
    渲染：缺数据的行留空占位，便于手动补全。演员行 = 中文名 + 日文名
    同行（同名去重）；标签行 = 内容标签（enrich 阶段按词库频率取
    top-20）；类别行 = 有无码/字幕等版本标识（badges，经词库黑名单
    过滤）。hashtag lines are trimmed from the tail when the whole
    caption would exceed Telegram's 1024 limit.
    """
    def _uniq(seq):
        out = []
        for item in seq:
            if item and item not in out:
                out.append(item)
        return out

    code = (details.get("code") or "").strip()
    intro = (details.get("title") or "").strip()
    # 演员行：中文名 + 日文名同行（同名去重；中文名在前）
    actress_names = _uniq(
        n.strip() for n in
        [*(details.get("actresses_cn") or []), *(details.get("actresses") or [])]
        if isinstance(n, str))
    actresses = _uniq(t for t in (_hashtag(x) for x in actress_names) if t)
    genres = _uniq(t for t in (_hashtag(x) for x in details.get("genres") or []) if t)
    badges = _uniq(t for t in (_hashtag(x) for x in details.get("badges") or []) if t)

    if not code and not intro:
        return ""  # nothing to show: caller falls back to a bold title

    blocks = []
    if code:
        blocks.append(code)
    if intro:
        blocks.append(intro)

    # 骨架留空（用户定稿）：三行结构恒定渲染，缺数据的行留空占位，
    # 便于手动补全（与 /single /batch /merge 的自动排版骨架一致）。
    # 演员 = 中文名 + 日文名同行；类别 = 有无码/字幕等版本标识（badges）；
    # 标签 = 内容标签（enrich 阶段已按词库频率取 top-20）。
    tag_lines = [
        "演员：" + " ".join(actresses),
        "标签：" + " ".join(genres),
        "类别：" + " ".join(badges),
    ]
    blocks.append("\n".join(tag_lines))

    def render(parts):
        return "\n\n".join(parts)

    # hashtag lines carry a 「label：」prefix; trim their tails (keep the
    # label + one tag) until the caption fits Telegram's limit
    def is_tag_line(line):
        return line.startswith(("演员：", "标签：", "类别：",
                                "演员:", "标签:", "类别:"))

    while len(render(blocks)) > max_len:
        tag_block_idx = next(
            (i for i in reversed(range(len(blocks))) if "\n" in blocks[i]), None
        )
        if tag_block_idx is None:
            break
        tag_lines = blocks[tag_block_idx].split("\n")
        trimmable = next(
            (j for j in reversed(range(len(tag_lines)))
             if is_tag_line(tag_lines[j]) and len(tag_lines[j].split(" ")) > 2),
            None,
        )
        if trimmable is None:
            break
        tag_lines[trimmable] = " ".join(tag_lines[trimmable].split(" ")[:-1])
        blocks[tag_block_idx] = "\n".join(tag_lines)
    out = render(blocks)
    if len(out) > max_len and len(blocks) >= 2:
        # 兜底：标签行已全部收到「label + 单 tag」仍超长（如单个超长人名）
        # 时压缩简介块，四行骨架的 label 恒定保留（定稿约定）。
        tag_i = next((i for i, b in enumerate(blocks) if "\n" in b), None)
        if tag_i:  # 简介（或番号）块位于 tag 块之前才可压
            others = len(render([b for i, b in enumerate(blocks)
                                 if i != tag_i and i != tag_i - 1]))
            budget = max(max_len - others - 2 * (len(blocks) - 1) - 1, 1)
            blocks[tag_i - 1] = blocks[tag_i - 1][:budget].rstrip() + "…"
            out = render(blocks)
    return out[:max_len]


def extract_m3u8_url(html):
    """Unpack every packed script and pull the primary m3u8 URL.

    Primary match is ``source=…``; fallback is any m3u8 URL in the block.
    """
    for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE):
        if "eval(function" not in script or "m3u8" not in script:
            continue
        unpacked = unpack_packed_js(script)
        if not unpacked:
            continue
        # unpacked text keeps JS escapes: source=\'https://….m3u8\'
        m = re.search(r"source\s*=\s*[\\']*(https?://[^'\\;\s]+\.m3u8)", unpacked)
        if m:
            return m.group(1)
        m = re.search(r"(https?://[^'\\;\s]+\.m3u8)", unpacked)
        if m:
            return m.group(1)
    return None


# ─── HLS playlist handling (pure) ──────────────────────────────────────────────

def _absolute(base_url, uri):
    return urljoin(base_url, uri) if uri else uri


def select_variant_uri(playlist):
    """For a variant playlist return the highest-resolution entry, else None."""
    variants = playlist.playlists or []
    if not variants:
        return None

    def rank(v):
        info = v.stream_info
        return (getattr(info, "height", None) or 0, getattr(info, "bandwidth", None) or 0)

    return max(variants, key=rank).uri


def select_subtitle_media(playlist):
    """Best EXT-X-MEDIA subtitle track URI from a master playlist, or None.

    Preference: a zh/chinese-named track > the default=YES track > the
    first track. Entries without a URI are skipped.
    """
    entries = [
        media for media in (playlist.media or [])
        if str(getattr(media, "type", "") or "").upper() == "SUBTITLES"
        and getattr(media, "uri", None)
    ]
    if not entries:
        return None

    def _is_zh(media):
        text = f"{getattr(media, 'name', '') or ''} {getattr(media, 'language', '') or ''}".lower()
        return "zh" in text or "chinese" in text or "中文" in text

    for media in entries:
        if _is_zh(media):
            return media.uri
    for media in entries:
        if str(getattr(media, "default", "") or "").upper() == "YES":
            return media.uri
    return entries[0].uri


def playlist_encryption(playlist):
    """Return {'method','uri','iv'} for the first AES key, or None."""
    for key in playlist.keys or []:
        if not key or not key.uri:
            continue
        method = (getattr(key, "method", "") or "").upper()
        if method == "SAMPLE-AES":
            raise MissAVError("不支持的加密方式 SAMPLE-AES")
        if method == "AES-128":
            return {"method": method, "uri": key.uri, "iv": getattr(key, "iv", None)}
    return None


# ─── WebVTT track merging (issue #18, pure) ────────────────────────────────────

_VTT_TS = r"\d{1,3}:\d{1,2}(?::\d{1,2})?[.,]\d{1,3}"
_VTT_TIMING_RE = re.compile(
    rf"(?P<start>{_VTT_TS})\s*-->\s*(?P<end>{_VTT_TS})", re.IGNORECASE)
_VTT_TS_RE = re.compile(rf"^({ _VTT_TS })$")
_VTT_MAP_RE = re.compile(
    r"X-TIMESTAMP-MAP\s*=\s*LOCAL:([^,\s]+)\s*,\s*MPEGTS:(-?\d+)", re.IGNORECASE)


def _parse_vtt_ts(text):
    """VTT timestamp (MM:SS.mmm or HH:MM:SS.mmm) -> seconds, else None."""
    m = _VTT_TS_RE.match(text.strip())
    if not m:
        return None
    parts = m.group(1).replace(",", ".").split(":")
    h, mm, ss = (["0"] * (3 - len(parts))) + parts
    return int(h) * 3600 + int(mm) * 60 + float(ss)


def _vtt_segment_offset(body):
    """Per-segment shift (seconds) from the X-TIMESTAMP-MAP header, else 0.

    HLS WebVTT cues are segment-relative: absolute = cue + MPEGTS/90000
    - LOCAL. The header sits in the segment's first lines.
    """
    m = _VTT_MAP_RE.search(body[:4096])
    if not m:
        return 0.0
    local = _parse_vtt_ts(m.group(1))
    if local is None:
        return 0.0
    return int(m.group(2)) / 90000.0 - local


def _iter_vtt_cues(body):
    """Yield (start_s, end_s, payload) for every cue block in a VTT body."""
    lines = body.splitlines()
    i, total = 0, len(lines)
    while i < total:
        m = _VTT_TIMING_RE.search(lines[i])
        if not m:
            i += 1
            continue
        start, end = _parse_vtt_ts(m.group("start")), _parse_vtt_ts(m.group("end"))
        if start is None or end is None:
            i += 1
            continue
        i += 1
        payload = []
        while i < total and lines[i].strip():
            payload.append(lines[i])
            i += 1
        yield start, end, "\n".join(payload)


def _fmt_vtt_ts(seconds):
    """Seconds -> HH:MM:SS.mmm (clamped at zero)."""
    total_ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(total_ms, 3_600_000)
    mm, rem = divmod(rem, 60_000)
    ss, ms = divmod(rem, 1000)
    return f"{h:02d}:{mm:02d}:{ss:02d}.{ms:03d}"


def merge_vtt_segments(bodies):
    """Concatenate HLS WebVTT segments into one WEBVTT document.

    Each segment's ``X-TIMESTAMP-MAP`` is honoured (MPEGTS/90000 - LOCAL
    shifts that segment's cues onto the absolute timeline) and cues that
    repeat EXACTLY across segment boundaries are emitted once. Output
    keeps first-seen order and normalizes timestamps to HH:MM:SS.mmm.
    """
    out = ["WEBVTT"]
    seen = set()
    for body in bodies:
        if not body or not body.strip():
            continue
        offset = _vtt_segment_offset(body)
        for start, end, payload in _iter_vtt_cues(body):
            a = max(0.0, start + offset)
            b = max(0.0, end + offset)
            key = (round(a * 1000), round(b * 1000), payload)
            if key in seen:
                continue
            seen.add(key)
            out.extend(["", f"{_fmt_vtt_ts(a)} --> {_fmt_vtt_ts(b)}", payload])
    out.append("")
    return "\n".join(out)


def segment_iv(key_iv, index, media_sequence):
    """Explicit hex IV, or the HLS implicit IV: media-sequence + index."""
    if key_iv:
        hexstr = key_iv.replace("0x", "").replace("0X", "")
        return bytes.fromhex(hexstr.zfill(32))
    return (index + (media_sequence or 0)).to_bytes(16, "big")


def decrypt_segment(data, key, iv):
    """AES-128-CBC decrypt one segment and strip valid PKCS7 padding."""
    if len(data) % 16 != 0:
        raise MissAVError(f"加密片段长度未按 16 字节对齐: {len(data)}")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    if plain:
        pad = plain[-1]
        if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
    return plain


# ─── network layer (single test seam) ──────────────────────────────────────────

_SESSION = None


def _get_session():
    """curl_cffi Chrome-impersonating session; plain requests fallback."""
    global _SESSION
    if _SESSION is None:
        try:
            from curl_cffi import requests as cffi_requests
            _SESSION = cffi_requests.Session(impersonate="chrome")
        except ImportError:
            import requests
            _SESSION = requests.Session()
            _SESSION.headers.update(
                {"User-Agent": _CHROME_UA, "Accept-Language": "en-US,en;q=0.9"}
            )
    return _SESSION


class _BodyTooLarge(Exception):
    pass


class _BufferedResponse:
    """Minimal response facade built from a streamed body."""

    def __init__(self, status_code, raw, url, headers=None):
        self.status_code = status_code
        self.content = raw
        self.url = url
        self.headers = headers or {}
        self.text = raw.decode("utf-8", errors="replace")


def _http_get(url, headers=None, timeout=PAGE_TIMEOUT, max_bytes=PAGE_MAX_BYTES):
    """Streaming GET with a hard body cap -> (response|None, error|None).

    The body is read incrementally and the transfer is aborted as soon as
    ``max_bytes`` is exceeded, so a hostile endpoint cannot exhaust memory
    with an oversized body. Never raises.
    """
    try:
        resp = _get_session().get(url, headers=headers, timeout=timeout, stream=True)
        try:
            chunks = []
            received = 0
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                received += len(chunk)
                if max_bytes is not None and received > max_bytes:
                    raise _BodyTooLarge(f"{received} > {max_bytes}")
                chunks.append(chunk)
            return (
                _BufferedResponse(
                    resp.status_code, b"".join(chunks), getattr(resp, "url", url),
                    getattr(resp, "headers", None),
                ),
                None,
            )
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
    except _BodyTooLarge as exc:
        return None, f"body too large: {exc}"
    except Exception as exc:  # network/timeout/TLS
        return None, str(exc)

def _http_get_retry(url, headers=None, timeout=PAGE_TIMEOUT, max_bytes=PAGE_MAX_BYTES):
    """_http_get with transient-failure retries (gateway brownout / stall).

    Retries ONLY a dead transport (resp None — timeout, curl 28-style
    stall) or a gateway error (502/503/504): the CDN brownout class
    observed live on worldstatic 2026-08-16. A Cloudflare 403/404 or any
    parsed response returns on the first attempt — retrying a block is
    noise. Returns the LAST (response|None, error|None) pair.
    """
    result = (None, None)
    for attempt in range(PAGE_RETRIES):
        result = _http_get(url, headers, timeout, max_bytes)
        resp, err = result
        transient = resp is None or getattr(resp, "status_code", None) in (502, 503, 504)
        if not transient:
            return result
        if attempt + 1 < PAGE_RETRIES:
            logger.info(
                "page fetch transient fail (%s attempt %d/%d): %s",
                urlparse(url).hostname, attempt + 1, PAGE_RETRIES,
                err or getattr(resp, "status_code", "?"))
            time.sleep(PAGE_RETRY_BACKOFF[min(attempt, len(PAGE_RETRY_BACKOFF) - 1)])
    return result


# Hosts we never dial, even if a mirror/CDN response points at them
# (second-order SSRF hardening: cloud metadata, loopback, private nets).
_PRIVATE_HOST_RE = re.compile(
    r"^(?:localhost|.*\.local|.*\.internal"
    r"|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|169\.254\.\d+\.\d+|0\.0\.0\.0"
    r"|\[?::1\]?$)", re.IGNORECASE)


def _registered_domain(host):
    """Rough registrable-domain suffix: the last two DNS labels."""
    host = (host or "").lower().strip(".")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _host_allowed(url, pinned_domain):
    """http(s) scheme, non-private host, and under the pinned domain."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if _PRIVATE_HOST_RE.match(host):
        return False
    if host == pinned_domain or host.endswith("." + pinned_domain):
        return True
    return _registered_domain(host) == pinned_domain


def _response_host_allowed(resp, requested_url, pinned_domain):
    """Re-validate the FINAL host after redirects.

    ``_http_get`` follows redirects transparently and exposes the final
    URL as ``resp.url``; a request-time ``_host_allowed`` check alone
    does not pin the actual endpoint (review finding: subtitle/getav
    subtitle paths). Missing ``url`` (older fakes) falls back to the
    requested URL.
    """
    final = getattr(resp, "url", None) or requested_url
    return _host_allowed(final, pinned_domain)


# Page-provided cover URLs (og:image / movie-JSON covers) may live on the
# mirror itself or the known video-CDN families; anything else drops the
# cover and the delivery falls back to a screenshot. Same discipline the
# HLS pipeline applies to playlists/segments (review: unpinned cover GET).
_COVER_EXTRA_DOMAINS = (
    "surrit.com", "nineyu.com", "fourhoi.com", "worldstatic.com", "getav.net",
)


def cover_url_allowed(url, hosts=DEFAULT_MIRRORS):
    """Scheme + private-host + registered-domain allowlist for cover URLs."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if _PRIVATE_HOST_RE.match(host):
        return False
    domain = _registered_domain(host)
    allowed = {h.lower() for h in hosts} | set(_COVER_EXTRA_DOMAINS)
    return domain in allowed


def _looks_blocked(resp, text):
    if resp.status_code in (403, 429, 503):
        return True
    headers = getattr(resp, "headers", None)
    if headers and (headers.get("cf-mitigated") or headers.get("cf-mitigated-header")):
        return True
    marker = text[:4096]
    return "just a moment" in marker.lower() or "cf-challenge" in marker.lower()


def _is_video_page(text):
    return "og:title" in text and ("m3u8" in text or "eval(function" in text)


def fetch_video_page(url, hosts=DEFAULT_MIRRORS):
    """Rotate mirrors until one serves a valid video page.

    Returns (html, host). Raises MissAVBlockedError when every mirror is
    blocked/unreachable, MissAVError when the video is gone or the layout
    changed.
    """
    mirror_domains = {_registered_domain(h.lower()) for h in hosts}
    saw_content = False
    saw_404 = False
    for candidate in mirror_candidates(url, hosts):
        resp, err = _http_get(
            candidate, headers={"User-Agent": _CHROME_UA}, timeout=PAGE_TIMEOUT,
            max_bytes=PAGE_MAX_BYTES,
        )
        if resp is None:
            logger.info("missav mirror unreachable %s: %s", candidate, err)
            continue
        # Redirects are followed by the client; re-validate the FINAL host
        # against the mirror set so a 30x cannot retarget us elsewhere.
        final = urlparse(getattr(resp, "url", candidate) or candidate).hostname or ""
        if _registered_domain(final) not in mirror_domains:
            logger.info("missav mirror redirected off-site %s -> %s", candidate, final)
            continue
        text = resp.text or ""
        if resp.status_code == 404:
            saw_404 = True
            continue
        if _looks_blocked(resp, text):
            logger.info("missav mirror blocked %s (status %s)", candidate, resp.status_code)
            continue
        saw_content = True
        if _is_video_page(text):
            return text, final or urlparse(candidate).hostname

    if saw_content:
        raise MissAVError(f"页面解析失败（视频不存在或版面改版）: {url}")
    if saw_404:
        raise MissAVError(f"视频不存在或已删除: {url}")
    raise MissAVBlockedError(BLOCKED_MSG)


# ─── sister-version probing (issue #17) ────────────────────────────────────────

_MISSAV_VARIANT_ORDER = {"raw": 0, "cn": 1, "uc": 2, "uc-cn": 3}
_MISSAV_VARIANT_LABELS = {
    "raw": "原版",
    "cn": "中文字幕",
    "uc": "无码破解",
    "uc-cn": "无码破解·中文字幕",
}


def _m3u8_stream_key(m3u8_url):
    """Stream fingerprint: the surrit per-video UUID path segment when
    present, else the full URL."""
    m = re.search(
        r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/",
        m3u8_url or "", re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return (m3u8_url or "").strip()


def _probe_missav_page(url):
    """One lightweight GET: (valid, m3u8_url) for a missav video page.

    Same Chrome UA as :func:`fetch_video_page`, same page-shaped guard;
    every failure (blocked, 404, listing page, timeout) is (False, None)
    — a probe must never raise into the caller. The extracted stream URL
    is the fingerprint discovery uses to reject phantom alias pages
    (missav answers unknown slug suffixes with a playable page for the
    BASE video — observed live on FC2-PPV-2761664).
    """
    try:
        resp, err = _http_get(
            url, headers={"User-Agent": _CHROME_UA}, timeout=PAGE_TIMEOUT,
            max_bytes=PAGE_MAX_BYTES,
        )
    except Exception:  # _http_get never raises by contract; belt and braces
        return False, None
    if resp is None or resp.status_code != 200:
        logger.info("missav variant probe failed %s: %s",
                    url, err or getattr(resp, "status_code", "?"))
        return False, None
    text = resp.text or ""
    if _looks_blocked(resp, text):
        logger.info("missav variant probe blocked %s (status %s)", url, resp.status_code)
        return False, None
    if not _is_video_page(text):
        return False, None
    return True, extract_m3u8_url(text)


async def discover_missav_variants(url, hosts=DEFAULT_MIRRORS):
    """Probe the sister versions of a missav page (one GET each).

    Returns [(variant, url, label)] ordered raw < cn < uc < uc-cn. A
    candidate only counts when it serves a DIFFERENT stream than the
    page itself: missav answers slug suffixes it does not know with a
    playable page for the BASE video (phantom alias — observed live on
    FC2-PPV-2761664, which the site lists as one unlabeled version).
    When the base page cannot be probed, discovery degrades to the
    current page alone rather than risk phantom cards; non-missav URLs
    return [].
    """
    candidates = missav_variant_candidates(url, hosts)
    if not candidates:
        return []
    probes = await asyncio.gather(*(
        asyncio.to_thread(_probe_missav_page, candidate) for _, candidate in candidates
    ))
    base_ok, base_m3u8 = probes[0]          # first candidate = the page itself
    base_key = _m3u8_stream_key(base_m3u8) if base_ok and base_m3u8 else None
    base_variant = candidates[0][0]
    found = []
    for (variant, candidate), (ok, m3u8) in zip(candidates, probes):
        if not ok:
            continue
        if variant != base_variant and _m3u8_stream_key(m3u8) == base_key:
            logger.info("missav variant %s is a phantom alias (same stream)", candidate)
            continue
        found.append((variant, candidate, _MISSAV_VARIANT_LABELS[variant]))
    if not found:
        variant, candidate = candidates[0]
        return [(variant, candidate, _MISSAV_VARIANT_LABELS[variant])]
    found.sort(key=lambda item: _MISSAV_VARIANT_ORDER[item[0]])
    return found

# ─── getav movie API (fetch + parse, pure where possible) ─────────────────────

_GETAV_STATIC_HOST = "https://static.worldstatic.com"


def fetch_getav_movie(url, hosts=GETAV_DEFAULT_MIRRORS):
    """Rotate getav mirrors until one serves the movie JSON.

    Returns ``(data, host)`` where ``data`` is the validated movie dict
    (``success`` + ``data`` + non-empty ``videoSources``). Raises with
    the same error contract as :func:`fetch_video_page` so the bot shows
    consistent messages across sites.
    """
    mirror_domains = {_registered_domain(h.lower()) for h in hosts}
    saw_404 = False
    saw_content = False
    for candidate in getav_api_candidates(url, hosts):
        resp, err = _http_get_retry(
            candidate, headers={"User-Agent": _CHROME_UA}, timeout=PAGE_TIMEOUT,
            max_bytes=PAGE_MAX_BYTES,
        )
        if resp is None:
            logger.info("getav mirror unreachable %s: %s", candidate, err)
            continue
        final = urlparse(getattr(resp, "url", candidate) or candidate).hostname or ""
        if _registered_domain(final) not in mirror_domains:
            logger.info("getav mirror redirected off-site %s -> %s", candidate, final)
            continue
        if resp.status_code == 404:
            saw_404 = True
            continue
        if _looks_blocked(resp, resp.text or ""):
            logger.info("getav mirror blocked %s (status %s)", candidate, resp.status_code)
            continue
        saw_content = True
        data = _parse_getav_json(resp.text or "")
        if data is not None:
            host = final or urlparse(candidate).hostname
            _augment_getav_zh(data, url, hosts, host)
            return data, host

    if saw_content:
        raise MissAVError(f"影片数据解析失败（视频不存在或接口改版）: {url}")
    if saw_404:
        raise MissAVError(f"视频不存在或已删除: {url}")
    raise MissAVBlockedError(BLOCKED_MSG)


# P0 收紧：劈分截断符从「。<」扩到「。，,；;」——简介里
# 「主演：A、B，讲述…」以前会把整个叙述段抓进来，劈分出假演员。
# 注：契约截断符列表里的「、」保留为劈分符而非截断符——截在、会把
# 「主演：A、B」的 B 弄丢（多演员列表是主流格式）。
_GETAV_ZH_STARS_RE = re.compile(r"主演[：:]([^<。，,；;]{2,60})")
_GETAV_ZH_TITLE_RE = re.compile(
    r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_GETAV_ZH_DESC_RE = re.compile(
    r'<meta\s+name="description"\s+content="([^"]*)"', re.IGNORECASE)
# 白名单校验（第二道防线）：劈分后的每段必须像真实人名才收录。
# 无数字/拉丁标点由纯 CJK 字符类一并排除；中隔点 · 是译名的一部分。
_NARRATIVE_WORDS = ("讲述", "描写", "为您", "带来", "片中", "剧情", "作品",
                    "出演", "是一位", "的", "了", "在", "和", "与", "及", "其")
_CJK_NAME_RE = re.compile(r"^[\u3400-\u9fff·・]+$")


def _looks_like_name(seg):
    """True when a 主演 fragment plausibly is a person name.

    strip 后 2–15 字、纯 CJK（允许中隔点 ·/・）、不含叙述词
    （讲述/描写/为您/带来/片中/剧情/作品/出演/是一位/的/了/在/和/与/及/其）。
    尽力而为的启发式：宁可漏收，绝不产出「讲述…」这类叙述残句。
    """
    name = (seg or "").strip().strip("·・").strip()
    if not 2 <= len(name) <= 15:
        return False
    if not _CJK_NAME_RE.match(name):
        return False
    return not any(w in name for w in _NARRATIVE_WORDS)


def _augment_getav_zh(data, url, hosts, api_host):
    """Overlay the /zh page's Chinese title/actresses onto the movie JSON.

    The movie API is locale-fixed (title stays Japanese regardless of
    Accept-Language/cookies; the localized data lives only in the /zh
    page's <title> + meta description, live 2026-08-16). Best-effort: any
    failure leaves the API fields untouched — Chinese intro text is a
    display upgrade, never a download prerequisite.
    """
    try:
        info = parse_getav_url(url, hosts) or {}
        host = api_host or info.get("host") or "getav.net"
        zh_url = f"https://{host}/zh/videos/{info.get('slug', '')}"
        resp, err = _http_get_retry(
            zh_url, headers={"User-Agent": _CHROME_UA}, timeout=PAGE_TIMEOUT,
            max_bytes=PAGE_MAX_BYTES,
        )
        if resp is None or resp.status_code != 200 or _looks_blocked(resp, resp.text or ""):
            logger.info("getav zh-page unavailable (status %s): %s",
                        getattr(resp, "status_code", "?"), err)
            return
        html = resp.text or ""
        m = _GETAV_ZH_TITLE_RE.search(html)
        title = html_unescape(m.group(1)).strip() if m else ""
        if title.endswith("| GetAV"):
            title = title[: -len("| GetAV")].strip()
        m = _GETAV_ZH_DESC_RE.search(html)
        desc = html_unescape(m.group(1)).strip() if m else ""
        if title:
            data["titleZh"] = title
        if desc:
            data["descriptionZh"] = desc
        m = _GETAV_ZH_STARS_RE.search(desc)
        if m:
            # 劈分（含空白，处理「A、B 讲述…」式粘连）后逐段过白名单；
            # 过滤后非空才写入 starsZh
            stars = [n for n in (s.strip() for s in re.split(r"[、,，/\s]+", m.group(1)))
                     if _looks_like_name(n)]
            if stars:
                data["starsZh"] = stars
    except Exception:
        logger.info("getav zh-page overlay failed", exc_info=True)


def _parse_getav_json(text):
    """{'success':True,'data':{…,'videoSources':[…]}} -> data dict, else None."""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not payload.get("success"):
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("videoSources"), list):
        return None
    if not data["videoSources"]:
        return None
    return data


_GETAV_FAMILY_RANK = {"cn": 3, "uc": 2, "raw": 1}
_GETAV_FAMILY_LABELS = {"cn": "中文字幕版", "uc": "无码版", "raw": "原版"}
_GETAV_TYPE_RE = re.compile(r"^(?P<fam>[a-z0-9]+)_(?P<res>\d{3,4})p$", re.IGNORECASE)


def _getav_source_rank(source):
    """Sort key: family (cn > uc > raw), then resolution, then priority.

    ``cn`` = hardcoded Chinese-sub release, ``uc`` = uncensored — the
    site's own player groups sources the same way and ranks uc above raw
    (live 2026-08-16). Unknown families still rank by resolution/priority.
    """
    stype = (source.get("type") or "").lower()
    m = _GETAV_TYPE_RE.match(stype)
    fam = _GETAV_FAMILY_RANK.get(m.group("fam"), 0) if m else 0
    res = int(m.group("res")) if m else 0
    try:
        prio = int(source.get("priority") or 0)
    except (TypeError, ValueError):
        prio = 0
    return (fam, res, prio)

def select_getav_source(video_sources):
    """Best playable source: (url, family) or (None, None).

    Only http(s) URLs count — a hostile/renamed source entry must never
    be dialled blindly.
    """
    best = None
    for source in video_sources:
        if not isinstance(source, dict):
            continue
        url = source.get("url")
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https"):
            continue
        if _getav_source_rank(source) > _getav_source_rank(best or {}):
            best = source
    if not best:
        return None, None
    stype = (best.get("type") or "").lower()
    m = _GETAV_TYPE_RE.match(stype)
    return best["url"], (m.group("fam") if m else None)

def list_getav_sources(video_sources):
    """Playable sources best-first: [(url, family, label), ...].

    Same admission rules as select_getav_source (only http(s) URLs), same
    ranking (family > resolution > priority). Labels are the user-facing
    version names for the /dl selection card, e.g. ``中文字幕版 1080p``.
    """
    entries = [
        s for s in video_sources
        if isinstance(s, dict)
        and isinstance(s.get("url"), str)
        and urlparse(s["url"]).scheme in ("http", "https")
    ]
    entries.sort(key=_getav_source_rank, reverse=True)
    listed = []
    for source in entries:
        stype = (source.get("type") or "").lower()
        m = _GETAV_TYPE_RE.match(stype)
        if m:
            fam = m.group("fam")
            label = f"{_GETAV_FAMILY_LABELS.get(fam, fam)} {m.group('res')}p"
        else:
            fam = None
            label = stype or "其他版本"
        listed.append((source["url"], fam, label))
    return listed


def _getav_family_by_url(video_sources, url):
    """Family of a pinned source URL ('cn'/'uc'/'raw'/None)."""
    for source in video_sources:
        if isinstance(source, dict) and source.get("url") == url:
            m = _GETAV_TYPE_RE.match((source.get("type") or "").lower())
            return m.group("fam") if m else None
    return None


def getav_cover_url(data):
    """Absolute cover URL from the movie dict, or ''.

    ``localImg`` is site-relative (served from static.worldstatic.com;
    the page's own og:image is a generic site banner, useless as a
    cover). Best-effort: the album builder falls back to a screenshot.
    """
    img = data.get("localImg")
    if not isinstance(img, str) or not img.strip():
        return ""
    if img.startswith(("http://", "https://")):
        return img
    if img.startswith("/"):
        return _GETAV_STATIC_HOST + img
    return ""


def getav_has_zh_subtitle(data):
    """getav 影片 JSON 是否带中文外挂字幕（语言码 zh*）。"""
    if not isinstance(data, dict):
        return False
    subs = data.get("subtitles")
    return isinstance(subs, list) and any(
        isinstance(s, dict) and str(s.get("language") or "").lower().startswith("zh")
        for s in subs
    )



def extract_getav_details(data, url, family=None):
    """Movie JSON -> caption ingredients (details dict v2 契约字段表).

    ``family`` is the chosen source family ('cn'/'uc'/'raw'/None) and
    only badges the actually downloaded stream. ``titleZh`` takes
    precedence over the locale-fixed Japanese API title. v2 字段分离：
    ``starsZh``（/zh 页中文演员）→ ``actresses_cn``；``stars``（API 源
    语言名，日文为主）→ ``actresses``——不再混入/拼接。
    """
    details = {"code": "", "title": "", "actresses": [], "actresses_cn": [],
               "genres": [], "badges": []}

    code = data.get("id") or (parse_getav_url(url) or {}).get("slug", "") or ""
    details["code"] = str(code).upper()

    # Chinese intro (from the /zh page overlay) beats the locale-fixed
    # Japanese API title; "[无码/中文字幕]" badges and the "| GetAV"
    # suffix are stripped before use.
    title = str(data.get("titleZh") or data.get("title") or "")
    if title.endswith("| GetAV"):
        title = title[: -len("| GetAV")].strip()
    title = re.sub(r"^\s*(?:\[[^\]]*\]\s*)+", "", title)
    if details["code"] and title.upper().startswith(details["code"]):
        title = title[len(details["code"]):]
    details["title"] = title.strip()

    names = []
    stars = data.get("stars")
    if isinstance(stars, list):
        for star in stars:
            name = star.get("name") if isinstance(star, dict) else None
            name = str(name).strip() if name else ""
            if name and name not in names:
                names.append(name)
    details["actresses"] = names

    names_cn = []
    stars_zh = data.get("starsZh")
    if isinstance(stars_zh, list):
        for name in stars_zh:
            name = str(name).strip()
            if name and name not in names_cn:
                names_cn.append(name)
    details["actresses_cn"] = names_cn

    genres = data.get("genres")
    if isinstance(genres, list):
        seen = []
        for genre in genres:
            name = genre.get("name") if isinstance(genre, dict) else genre
            name = str(name).strip() if name else ""
            if name and name not in seen:
                seen.append(name)
        details["genres"] = seen

    badges = []
    if family == "cn" or getav_has_zh_subtitle(data):
        badges.append("中文字幕")
    if family == "uc" or data.get("uc") == 1:
        badges.append("无码")
    details["badges"] = badges
    return details


# ─── ffmpeg remux ──────────────────────────────────────────────────────────────

async def _run_ffmpeg(args, timeout_s=None, env=None):
    """Await ffmpeg; thin seam for tests. Raises MissAVError on failure.

    The child runs demoted (`nice -n 19`, plus `ionice -c3` when
    available) so a 20-40 minute burn always yields CPU/IO to downloads
    and uploads — the "slow lane" contract. Missing wrappers degrade to
    the raw command. ``timeout_s`` (when > 0) bounds the wall-clock run:
    on expiry — and on task cancellation — the child is killed and
    reaped so no orphan ffmpeg keeps holding memory after the coroutine
    gives up. The raised MissAVError carries ``exit_code`` for
    structured logging by callers.
    """
    cmd = []
    if shutil.which("nice"):
        cmd += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        cmd += ["ionice", "-c3"]
    cmd += args
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        if timeout_s and timeout_s > 0:
            _, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        else:
            _, err = await proc.communicate()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise MissAVError(f"ffmpeg 超时 (>{timeout_s}s) 已终止: {args[:3]}")
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    err_text = err.decode(errors="replace")
    if proc.returncode != 0:
        exc = MissAVError(f"ffmpeg remux 失败: {err_text[-300:]}")
        exc.exit_code = proc.returncode
        raise exc
    # 字体环境告警必须可见：libass 在「无缓存目录/字体缺失」时静默
    # 渲染 0 条字幕（exit 0，ADN-538 实案——烧录 31 分钟全片无字）
    for marker in ("No writable cache directories", "Fontconfig error",
                   "fontselect"):
        hits = [ln for ln in err_text.splitlines() if marker in ln]
        if hits:
            logger.warning("ffmpeg 字体环境告警: %s", hits[:2])
            break


def _is_concat_list(src):
    """True when src is the ffconcat list the HLS core writes (sniff)."""
    try:
        with open(src, "rb") as fh:
            return fh.read(8).lstrip() == b"ffconcat"
    except OSError:
        return False


async def remux_to_mp4(src, dst):
    """Copy-remux -> MP4 (+faststart). Zero re-encode, seconds.

    ``src`` is the HLS core's ffconcat segment list (absolute paths, so
    merged.ts never hits the disk — the single biggest per-job IO lever)
    or, for direct callers, a plain media file: the input flags follow
    the sniffed kind.
    """
    if not shutil.which("ffmpeg"):
        raise MissAVError("服务器缺少 ffmpeg，无法封装 MP4")
    input_flags = (
        ["-f", "concat", "-safe", "0", "-fflags", "+genpts"]
        if _is_concat_list(src) else []
    )
    await _run_ffmpeg(
        ["ffmpeg", "-y", "-loglevel", "error", *input_flags, "-i", src,
         "-c", "copy", "-bsf:a", "aac_adtstoasc",
         "-movflags", "+faststart", dst]
    )
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise MissAVError("ffmpeg 未产出有效 MP4")


# Fansub-style rendering: white glyphs, black outline + soft shadow,
# bottom-center, bold CJK sans.
#
# Sizing is height-RELATIVE by construction: ffmpeg's VTT→ASS header pins
# PlayResY=288 and libass renders every style unit as unit/288 of the
# frame HEIGHT — so the constants below are the same fraction of the
# picture at 480p, 1080p and 4K, no per-video probing needed.
#   FontSize 13 ≈ 4.5% of height  (52 before ≈ 18%: unreadably huge,
#                                  two-line cues filled a third of the
#                                  screen — reworked 2026-08-16)
#   Outline   1 ≈ 0.35%           (≈ 3.7px at 1080p)
#   Shadow  0.5 ≈ 0.17%           (≈ 1.9px at 1080p, soft)
#   MarginV 10 ≈ 3.5%             (≈ 38px above the bottom edge at 1080p)
# Multi-line pitch follows the font's own metrics (ASS styles carry no
# line-height field); at 4.5% glyphs the site's two-line cues render with
# standard streaming-service spacing.
_SUBTITLE_FORCE_STYLE = (
    "FontName=Noto Sans CJK SC,Bold=1,FontSize=13,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "BorderStyle=1,Outline=1,Shadow=0.5,MarginV=10,Alignment=2"
)


def _escape_filter_path(path):
    """Escape a path for use inside a filtergraph argument."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _burn_threads():
    """Encode threads: explicit FFMPEG_BURN_THREADS wins, else leave one core
    for the bot itself (clamped 2..8, pre-config behavior)."""
    if FFMPEG_BURN_THREADS > 0:
        return FFMPEG_BURN_THREADS
    return max(2, min(8, (os.cpu_count() or 3) - 1))


# Independent budget for the libx264 re-encode: MISSAV_MAX_JOBS bounds the
# whole HLS pipeline (download+merge+burn+upload), so with the default 2 jobs
# two burns could still run back-to-back and blow the 512M cgroup. This
# semaphore serializes the re-encode itself (plan Phase 1 §4.2).
_burn_slots = asyncio.Semaphore(BURN_CONCURRENCY)


def _burn_env():
    """烧录子进程 env：XDG 缓存目录不可写时切到可写临时目录。

    fontconfig 拿不到可写缓存时 libass 静默渲染 0 条字幕（exit 0）。
    返回 None 表示继承当前环境即可。
    """
    xdg = os.environ.get("XDG_CACHE_HOME") or ""
    if xdg and os.path.isdir(xdg) and os.access(xdg, os.W_OK):
        return None
    env = dict(os.environ)
    env["XDG_CACHE_HOME"] = _tempfile.mkdtemp(prefix="fc_burn_")
    return env


async def burn_subtitles_to_mp4(src, dst, subtitle_path, task_id=None):
    """Re-encode TS -> MP4 with Chinese subtitles rendered INTO the frame.

    Full libx264 re-encode (the only way to burn subs), config-tuned via
    BURN_PRESET (default superfast, §8 定稿) / BURN_CRF (default 19) and
    FFMPEG_BURN_THREADS; peak RSS ~0.5GB. The subtitles filter matches
    cues against the 0-based frame timeline, which is exactly how
    getav's player authors its VTTs, so no offset correction is needed.
    ``src`` is the HLS core's ffconcat list or a plain media file (the
    input flags follow the sniffed kind).

    Concurrency is bounded by BURN_CONCURRENCY; every run logs start/end
    with sizes, duration and exit status (task ids only — never URLs,
    sessions, tokens or user content).
    """
    if not shutil.which("ffmpeg"):
        raise MissAVError("服务器缺少 ffmpeg，无法烧录字幕")
    vf = (
        f"subtitles={_escape_filter_path(subtitle_path)}"
        f":force_style='{_SUBTITLE_FORCE_STYLE}'"
    )
    threads = _burn_threads()
    input_bytes = os.path.getsize(src) if os.path.isfile(src) else 0
    started = time.monotonic()
    logger.info(
        "burn.start task=%s input_bytes=%d threads=%d preset=%s crf=%d",
        task_id, input_bytes, threads, BURN_PRESET, BURN_CRF,
    )
    try:
        async with _burn_slots:
            await _run_ffmpeg(env=_burn_env(), args=[
                "ffmpeg", "-y", "-loglevel", "error",
                *(["-f", "concat", "-safe", "0", "-fflags", "+genpts"]
                  if _is_concat_list(src) else []),
                "-i", src,
                "-vf", vf,
                "-c:v", "libx264", "-preset", BURN_PRESET, "-crf", str(BURN_CRF),
                "-threads", str(threads),
                "-c:a", "copy", "-movflags", "+faststart", dst,
            ], timeout_s=BURN_TIMEOUT_S)
    except MissAVError as exc:
        logger.warning(
            "burn.fail task=%s exit=%s input_bytes=%d duration_s=%.1f",
            task_id, getattr(exc, "exit_code", "timeout"), input_bytes,
            time.monotonic() - started,
        )
        raise
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise MissAVError("ffmpeg 未产出有效 MP4")
    logger.info(
        "burn.done task=%s input_bytes=%d output_bytes=%d duration_s=%.1f",
        task_id, input_bytes, os.path.getsize(dst), time.monotonic() - started,
    )


_GETAV_SUB_LANG_RANK = {"zh": 3, "zh-hans": 3, "zhtw": 2, "zh-hant": 2}


def select_getav_subtitle(data):
    """Best Chinese subtitle entry from the movie JSON, or None.

    getav ships site-polished VTTs (the player's 精校字幕): simplified
    ``zh`` beats ``zhtw``, then verified > qualityScore. Only vtt/srt
    formats with an http(s) filePath qualify.
    """
    subs = data.get("subtitles")
    if not isinstance(subs, list):
        return None
    best = None
    best_key = None
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        lang = str(sub.get("language") or "").lower()
        rank = _GETAV_SUB_LANG_RANK.get(lang)
        if rank is None:
            continue
        fmt = str(sub.get("format") or "").lower()
        if fmt not in ("vtt", "srt"):
            continue
        url = sub.get("filePath")
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https"):
            continue
        try:
            quality = int(sub.get("qualityScore") or 0)
        except (TypeError, ValueError):
            quality = 0
        key = (rank, bool(sub.get("isVerified")), quality)
        if best_key is None or key > best_key:
            best, best_key = sub, key
    return best


def _fetch_getav_subtitle(sub_entry, pinned_domain, dest_dir):
    """Download + validate the chosen VTT to a temp file; None on any problem.

    Best-effort by design: subtitle problems must never fail the video.
    Host-pinned to the same CDN domain as the video playlist; body is
    capped and must look like a real WebVTT/SRT.
    """
    url = sub_entry.get("filePath")
    if not _host_allowed(url, pinned_domain):
        logger.info("getav subtitle host rejected: %s", urlparse(url).hostname)
        return None
    resp, err = _http_get(url, headers={"Referer": f"https://getav.net/"},
                          timeout=PAGE_TIMEOUT, max_bytes=PAGE_MAX_BYTES)
    if resp is None or resp.status_code != 200 or not resp.content:
        logger.info("getav subtitle fetch failed: %s", err or getattr(resp, "status_code", "?"))
        return None
    if not _response_host_allowed(resp, url, pinned_domain):
        logger.info("getav subtitle redirected off-domain: %s",
                    urlparse(getattr(resp, "url", url) or url).hostname)
        return None
    text = resp.text or ""
    if "-->" not in text:
        logger.info("getav subtitle has no cues, skipped")
        return None
    fd, path = _tempfile.mkstemp(prefix="getav_sub_", suffix=".vtt", dir=dest_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return path


def _find_getav_movie_for_code(code):
    """Locate a getav movie record by bare code; ``data`` dict or None.

    Boundary-aware match (review: "ABC-12" must not accept "ABC-123"):
    the record id must normalize to exactly the wanted code, or the
    title's own CODE-123 token must normalize to it exactly. Single
    attempt per mirror candidate, never raises.
    """
    code = (code or "").strip()
    if not code:
        return None
    for api_url in getav_api_candidates(f"https://getav.net/zh/videos/{code}"):
        try:
            resp, err = _http_get(
                api_url, headers={"User-Agent": _CHROME_UA},
                timeout=PAGE_TIMEOUT, max_bytes=PAGE_MAX_BYTES,
            )
        except Exception:
            return None
        if resp is None or resp.status_code != 200:
            logger.info("getav code api miss %s: %s",
                        api_url, err or getattr(resp, "status_code", "?"))
            continue
        data = _parse_getav_json(resp.text or "")
        if data is None:
            continue
        haystack = f"{data.get('id') or ''} {data.get('title') or ''}".upper()
        want = re.sub(r"[^A-Z0-9]", "", code.upper())
        ident = re.sub(r"[^A-Z0-9]", "", str(data.get("id") or "").upper())
        title_token = re.search(r"[A-Z][A-Z0-9]*-\d+", str(data.get("title") or "").upper())
        token_code = re.sub(r"[^A-Z0-9]", "", title_token.group(0)) if title_token else ""
        if want != ident and want != token_code:
            logger.info("getav code mismatch: %s vs %s", code, haystack[:80])
            continue
        return data
    return None


def find_getav_subtitle_for_code(code, dest_dir):
    """Best-effort official getav VTT for a missav video code; None on any miss.

    When the missav HLS master carries no subtitle track, getav.net often
    hosts the same release with a site-polished Chinese VTT. Accept the
    payload ONLY on a strong code match (never burn another title's
    subs), then reuse the getav subtitle pipeline. Silent: subtitles are
    never a download prerequisite.
    """
    data = _find_getav_movie_for_code(code)
    if not data:
        return None
    sub = select_getav_subtitle(data)
    if not sub:
        return None
    # pin like download_getav: subtitle lives on the video CDN's domain
    source_url, _fam = select_getav_source(data.get("videoSources") or [])
    if not source_url:
        return None
    pinned = _registered_domain(urlparse(source_url).hostname or "")
    return _fetch_getav_subtitle(sub, pinned, dest_dir)


def find_getav_details_for_code(code):
    """Best-effort getav caption ingredients for a missav code; None on miss.

    FC2 and other codes missav pages leave sparse (no actress/genre
    panel) often exist on getav with full Chinese metadata. Applies the
    same /zh overlay as the getav pipeline (Chinese title/actresses
    when the page is reachable) and returns the standard details dict.
    Silent by design.
    """
    data = _find_getav_movie_for_code(code)
    if not data:
        return None
    page_url = f"https://getav.net/zh/videos/{(code or '').strip()}"
    try:
        _augment_getav_zh(data, page_url, (GETAV_DEFAULT_MIRRORS[0],),
                          urlparse(page_url).hostname)
    except Exception:
        logger.info("getav zh overlay failed for %s", code, exc_info=True)
    try:
        return extract_getav_details(data, page_url)
    except Exception:
        logger.info("getav details extract failed for %s", code, exc_info=True)
        return None


async def _fetch_subtitle_track(subtitle_uri, headers, pinned_domain, dest_dir):
    """Download + merge an HLS WebVTT subtitle track into one .vtt file.

    Returns the temp file path, or None on ANY problem — subtitles are
    strictly best-effort and must never fail the video. The track stays
    host-pinned like playlists/keys/segments and every body is
    byte-capped at the page budget.
    """
    try:
        url = subtitle_uri
        vtt_bodies = None
        for _ in range(MAX_PLAYLIST_HOPS):
            if not _host_allowed(url, pinned_domain):
                logger.info("subtitle track host rejected: %s", urlparse(url).hostname)
                return None
            resp, err = await asyncio.to_thread(
                _http_get, url, headers, PAGE_TIMEOUT, PAGE_MAX_BYTES
            )
            if resp is None or resp.status_code != 200 or not (resp.text or "").strip():
                logger.info("subtitle playlist fetch failed: %s",
                            err or getattr(resp, "status_code", "?"))
                return None
            # redirects are followed transparently: re-pin the FINAL host
            if not _response_host_allowed(resp, url, pinned_domain):
                logger.info("subtitle playlist redirected off-domain: %s",
                            urlparse(getattr(resp, "url", url) or url).hostname)
                return None
            text = resp.text or ""
            if text.lstrip().startswith("WEBVTT") and "-->" in text:
                vtt_bodies = [text]  # track URI pointed straight at a VTT body
                break
            import m3u8
            playlist = m3u8.loads(text)
            if playlist.segments:
                if len(playlist.segments) > MAX_SEGMENTS:
                    return None
                vtt_bodies = []
                spent = 0
                for seg in playlist.segments:
                    seg_url = _absolute(url, seg.uri)
                    if not _host_allowed(seg_url, pinned_domain):
                        return None
                    seg_resp, seg_err = await asyncio.to_thread(
                        _http_get, seg_url, headers, PAGE_TIMEOUT, PAGE_MAX_BYTES
                    )
                    if seg_resp is None or seg_resp.status_code != 200:
                        logger.info("subtitle segment failed (%s), dropping track",
                                    seg_err or getattr(seg_resp, "status_code", "?"))
                        return None
                    if not _response_host_allowed(seg_resp, seg_url, pinned_domain):
                        logger.info("subtitle segment redirected off-domain: %s",
                                    urlparse(getattr(seg_resp, "url", seg_url) or seg_url).hostname)
                        return None
                    body = seg_resp.text or ""
                    # budget on encoded BYTES, not decoded chars (CJK VTT
                    # is up to 3 bytes/char — review finding)
                    spent += len(seg_resp.content or b"")
                    if spent > PAGE_MAX_BYTES:
                        return None
                    vtt_bodies.append(body)
                break
            nxt = select_variant_uri(playlist)
            if not nxt:
                return None
            url = _absolute(url, nxt)
        if not vtt_bodies:
            return None
        merged = merge_vtt_segments(vtt_bodies)
        if "-->" not in merged:
            return None
        fd, path = _tempfile.mkstemp(prefix="missav_sub_", suffix=".vtt", dir=dest_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(merged)
        except BaseException:
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        return path
    except Exception:
        logger.info("subtitle track fetch failed", exc_info=True)
        return None


# ─── orchestration ─────────────────────────────────────────────────────────────

async def _resolve_media_playlist(m3u8_url, headers, pinned_domain):
    """Follow variant playlists (bounded, host-pinned) to the media playlist.

    Returns ``(playlist, playlist_url, master_url, subtitle_uri)``:
    ``master_url`` is the variant-level playlist a subtitle track was
    read from (None when the first load was already a media playlist)
    and ``subtitle_uri`` the selected ``EXT-X-MEDIA`` subtitles URI made
    absolute (None when the master declares no usable track).
    """
    url = m3u8_url
    master_url = None
    subtitle_uri = None
    for _ in range(MAX_PLAYLIST_HOPS):
        if not _host_allowed(url, pinned_domain):
            raise MissAVError(f"m3u8 地址域校验失败: {urlparse(url).hostname}")
        resp, err = await asyncio.to_thread(
            _http_get_retry, url, headers, PAGE_TIMEOUT, PAGE_MAX_BYTES
        )
        if resp is None or resp.status_code != 200:
            raise MissAVError(f"m3u8 获取失败: {err or getattr(resp, 'status_code', '?')}")
        # m3u8 loads lazily (plan §5.4): only the missav/getav HLS path needs
        # it, keeping idle import weight off every other bot command.
        import m3u8
        playlist = m3u8.loads(resp.text or "")
        sub_uri = select_subtitle_media(playlist)
        if sub_uri:
            master_url = url
            subtitle_uri = _absolute(url, sub_uri)
        variant_uri = select_variant_uri(playlist)
        if not variant_uri:
            if not playlist.segments:
                raise MissAVError("m3u8 无有效片段（可能被拦截或改版）")
            return playlist, url, master_url, subtitle_uri
        url = _absolute(url, variant_uri)
    raise MissAVError("m3u8 嵌套层级过深，疑似改版")

async def _download_one_segment(index, seg_url, temp_dir, key, iv_factory,
                                headers, pinned_domain, budget):
    """Fetch (and decrypt) one TS segment to an index-named temp file.

    ``budget`` is a one-element list holding cumulative downloaded bytes
    across the job; exceeding MAX_TOTAL_BYTES aborts the whole download.
    """
    last_err = None
    last_status = None
    max_attempts = SEGMENT_RETRIES
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        if not _host_allowed(seg_url, pinned_domain):
            raise MissAVError(f"片段 {index} 地址域校验失败: {urlparse(seg_url).hostname}")
        resp, err = await asyncio.to_thread(
            _http_get, seg_url, headers, SEGMENT_TIMEOUT, MAX_SEGMENT_BYTES
        )
        status = getattr(resp, "status_code", None)
        if resp is not None and status == 200:
            data = resp.content
            if not data:
                last_err = f"segment {index}: empty body"
            elif len(data) > MAX_SEGMENT_BYTES:
                raise MissAVError(f"片段 {index} 异常巨大 ({len(data)} bytes)，中止")
            else:
                # reserve BEFORE the decrypt await: with several workers in
                # flight, a check that only precedes the await lets them all
                # pass on the same stale budget (review finding)
                if budget[0] + len(data) > MAX_TOTAL_BYTES:
                    raise MissAVError(
                        f"累计下载量超预算 ({budget[0] + len(data)} > {MAX_TOTAL_BYTES} bytes)，中止")
                budget[0] += len(data)
                if key is not None:
                    data = await asyncio.to_thread(
                        decrypt_segment, data, key, iv_factory(index)
                    )
                path = os.path.join(temp_dir, f"{index:06d}.ts")
                with open(path, "wb") as fh:
                    fh.write(data)
                return path
        else:
            last_status = status
            last_err = f"segment {index}: HTTP {status if status is not None else err}"
            if status == 404:
                # worldstatic 的 404 有两种形态（ADN-538 连续三案实锤）：
                # ① 瞬态 404（限流/微窗口）——同 URL 短退避即愈；
                # ② Cloudflare 边缘粘性 404：源站曾瞬时 404 一次，被 CF
                #    按 cache-control: max-age=1y 缓存（cf-cache-status:
                #    HIT, age 天级, body 空），该 URL 从此永远 404——
                #    换令牌/刷新清单均无效，唯一解是加随机 query 穿透
                #    缓存键直回源站（实测 cf=MISS 200）。
                # 第 2 次尝试起启用穿透；bust 值每次唯一（防 busted URL
                # 自身又被缓存）。仍失败则上抛核心刷新播放列表兜底。
                max_attempts = max(max_attempts, SEGMENT_RETRIES_404)
                if attempt >= 2:
                    seg_url = (
                        f"{seg_url}{'&' if '?' in seg_url else '?'}"
                        f"_cfbust={time.monotonic_ns()}"
                    )
            elif status == 429:
                # a CDN rate-limit is sustained, not transient: switch to
                # the long-backoff budget instead of failing the whole job
                max_attempts = max(max_attempts, SEGMENT_RETRIES_RATE_LIMITED)
            elif status in (502, 503, 504):
                # gateway brownout: ride it out with the deepest budget
                # (exponential backoff capped at 60s spans the outage)
                max_attempts = max(max_attempts, SEGMENT_RETRIES_SERVER_ERROR)
        if attempt < max_attempts:
            cap = 60 if status in (429, 502, 503, 504) else (
                20 if status == 404 else 8)
            await asyncio.sleep(min(2 ** attempt, cap))
    if last_status == 404:
        # 404 档耗尽：交由核心刷新播放列表换新 URL 再试
        raise _SegmentStale(index, last_err)
    raise MissAVError(f"片段 {index} 下载失败: {last_err}")


async def _download_hls_core(m3u8_url, dest_path, referer_host, info, details,
                             concurrency=SEGMENT_CONCURRENCY, progress=None,
                             subtitle_path=None, want_site_subtitle=False,
                             task_id=None):
    """Shared missav/getav tail: m3u8 -> guarded segments -> merged mp4.

    ``info`` is {'title','thumbnail'}, ``details`` the caption
    ingredients (already parsed by the caller). Returns
    {'title','thumbnail','segments','host','details'}.

    Resource guards: segment count, cumulative bytes, total duration and
    free disk are all checked before/while downloading, so a hostile
    playlist cannot exhaust the host.

    ``want_site_subtitle`` (missav ``-sub``) pulls the master's
    EXT-X-MEDIA subtitle track and burns it; with no track in the HLS it
    falls back to the official getav VTT matched by code. External
    ``subtitle_path`` (getav flow) always wins and skips both.
    """
    async def _report(done, total, stage):
        if progress:
            await progress(done, total, stage)

    dest_dir = os.path.dirname(os.path.abspath(dest_path))
    if shutil.disk_usage(dest_dir).free < MIN_FREE_DISK:
        raise MissAVError("磁盘剩余空间不足，请稍后重试或联系管理员清理")

    # Referer only: real HLS players send it for hotlink-protected CDNs,
    # while a browser only sends Origin on CORS requests — worldstatic's
    # signed per-segment paths 404 when an Origin header rides along
    # (observed live 2026-08-16: seg-N 404 with Origin, 200 without).
    headers = {"Referer": f"https://{referer_host}/"}

    # Pin the CDN domain: every later hop (variant/key/segment) must stay
    # on the registered domain the playlist URL itself declared.
    m3u8_host = urlparse(m3u8_url).hostname or ""
    pinned_domain = _registered_domain(m3u8_host)
    if not _host_allowed(m3u8_url, pinned_domain):
        raise MissAVError(f"m3u8 地址非法: {m3u8_host}")

    playlist, playlist_url, master_url, subtitle_uri = await _resolve_media_playlist(
        m3u8_url, headers, pinned_domain
    )
    subtitle_track_path = None
    if subtitle_path is None and want_site_subtitle:
        if subtitle_uri:
            subtitle_track_path = await _fetch_subtitle_track(
                subtitle_uri, headers, pinned_domain, dest_dir
            )
        if subtitle_track_path is None:
            # no (usable) HLS track: try the official getav VTT by code
            code = (details or {}).get("code") or ""
            if code:
                subtitle_track_path = await asyncio.to_thread(
                    find_getav_subtitle_for_code, code, dest_dir
                )
    segments = playlist.segments
    if len(segments) > MAX_SEGMENTS:
        raise MissAVError(f"片段数超上限 ({len(segments)} > {MAX_SEGMENTS})，疑似异常数据")

    total_duration = sum(getattr(s, "duration", None) or 0 for s in segments)
    if total_duration > MAX_DURATION_S:
        hours = MAX_DURATION_S // 3600
        raise MissAVError(f"视频总时长超上限（>{hours} 小时），拒绝下载")

    media_sequence = getattr(playlist, "media_sequence", 0) or 0
    enc = playlist_encryption(playlist)

    key_bytes = None
    iv_factory = None
    if enc:
        key_url = _absolute(playlist_url, enc["uri"])
        if not _host_allowed(key_url, pinned_domain):
            raise MissAVError(f"AES 密钥地址域校验失败: {urlparse(key_url).hostname}")
        key_resp, kerr = await asyncio.to_thread(
            _http_get_retry, key_url, headers, PAGE_TIMEOUT, KEY_MAX_BYTES
        )
        if key_resp is None or key_resp.status_code != 200 or not key_resp.content:
            raise MissAVError(f"AES 密钥获取失败: {kerr or getattr(key_resp, 'status_code', '?')}")
        if len(key_resp.content) != 16:
            raise MissAVError(f"AES 密钥长度异常 ({len(key_resp.content)} bytes)")
        key_bytes = key_resp.content
        if enc["iv"]:
            iv_factory = lambda i: segment_iv(enc["iv"], i, media_sequence)
        else:
            iv_factory = lambda i: segment_iv(None, i, media_sequence)

    temp_dir = _tempfile.mkdtemp(prefix="missav_parts_", dir=dest_dir)
    try:
        # 分段阶段：404-stale（时敏令牌过期）驱动播放列表刷新，只补缺失
        # 分片；AES 密钥或 media_sequence 轮换时废弃旧解密分片全部重下。
        results = None
        total = 0
        old_media_sequence = media_sequence
        budget = [0]  # cumulative downloaded bytes (single-threaded loop)
        stale_refreshes = 0

        while True:
            seg_urls = [_absolute(playlist_url, s.uri) for s in segments]
            if results is None:
                total = len(seg_urls)
                results = [None] * total
            else:
                # 刷新后按序号对位：已下载分片直接复用
                new_total = len(seg_urls)
                if new_total < total:
                    raise MissAVError(
                        f"刷新播放列表后片段数变少 ({total} -> {new_total})，"
                        "源站内容已变更，中止")
                results.extend([None] * (new_total - total))
                total = new_total
            missing = [i for i in range(total) if results[i] is None]
            if not missing:
                break

            failures = []
            done = total - len(missing)
            queue = asyncio.Queue()
            for i in missing:
                queue.put_nowait((i, seg_urls[i]))

            async def _worker():
                nonlocal done
                while True:
                    try:
                        i, seg_url = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        results[i] = await _download_one_segment(
                            i, seg_url, temp_dir, key_bytes, iv_factory,
                            headers, pinned_domain, budget,
                        )
                    except _SegmentStale as e:
                        failures.append((i, e))
                        continue
                    done += 1
                    await _report(done, total, "segments")

            workers = [asyncio.create_task(_worker())
                       for _ in range(max(1, concurrency))]
            try:
                await asyncio.gather(*workers)
            except BaseException:
                for t in workers:
                    t.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                raise

            if not failures:
                break
            if stale_refreshes >= MAX_PLAYLIST_REFRESH:
                idx = failures[0][0]
                raise MissAVError(
                    f"片段 {idx} 刷新播放列表后仍 404（源站分片缺失或清单已整体过期），"
                    f"已刷新 {stale_refreshes} 次")
            stale_refreshes += 1
            # 分钟级 404 窗口（worldstatic brownout）需要真实间隔，
            # 否则 3 轮刷新几秒内连发毫无意义
            await asyncio.sleep(min(30 * stale_refreshes, 90))
            await _report(total - len(failures), total, "refresh")

            playlist, playlist_url, master_url, subtitle_uri = await _resolve_media_playlist(
                m3u8_url, headers, pinned_domain
            )
            segments = playlist.segments
            if len(segments) > MAX_SEGMENTS:
                raise MissAVError(f"片段数超上限 ({len(segments)} > {MAX_SEGMENTS})，疑似异常数据")
            media_sequence = getattr(playlist, "media_sequence", 0) or 0
            enc = playlist_encryption(playlist)
            key_rotated = False
            if enc:
                key_url = _absolute(playlist_url, enc["uri"])
                if not _host_allowed(key_url, pinned_domain):
                    raise MissAVError(f"AES 密钥地址域校验失败: {urlparse(key_url).hostname}")
                key_resp, kerr = await asyncio.to_thread(
                    _http_get_retry, key_url, headers, PAGE_TIMEOUT, KEY_MAX_BYTES
                )
                if key_resp is None or key_resp.status_code != 200 or not key_resp.content:
                    raise MissAVError(f"AES 密钥获取失败: {kerr or getattr(key_resp, 'status_code', '?')}")
                if len(key_resp.content) != 16:
                    raise MissAVError(f"AES 密钥长度异常 ({len(key_resp.content)} bytes)")
                new_key = key_resp.content
                key_rotated = (key_bytes is not None and new_key != key_bytes) or (
                    media_sequence != old_media_sequence)
                key_bytes = new_key
                if enc["iv"]:
                    iv_factory = lambda i: segment_iv(enc["iv"], i, media_sequence)
                else:
                    iv_factory = lambda i: segment_iv(None, i, media_sequence)
            else:
                key_bytes = None
                iv_factory = None
            old_media_sequence = media_sequence
            if key_rotated:
                # 密钥/序列轮换：旧解密分片与新密钥不可混用，全部作废重下
                for r in results:
                    if r and os.path.exists(r):
                        try:
                            os.remove(r)
                        except OSError:
                            pass
                results = None  # 下一轮按新列表全量重建

        await _report(total, total, "merge")
        # review F1: the job slot frees at burn time, so concurrent tasks
        # may hold several GB by now — re-check before writing the output
        if shutil.disk_usage(dest_dir).free < MIN_FREE_DISK:
            raise MissAVError("磁盘剩余空间不足（封装前复查），请稍后重试")
        # no merged.ts: remux/burn read the decrypted parts straight from
        # this ffconcat list (-4..8 GB of copy IO per 2-4 GB job)
        concat_list = os.path.join(temp_dir, "list.txt")
        with open(concat_list, "w", encoding="utf-8") as fh:
            fh.write("ffconcat version 1.0\n")
            for path in results:
                fh.write("file '" + path.replace("'", "'\\''") + "'\n")

        burn_sub = subtitle_path or subtitle_track_path
        if burn_sub:
            await _report(0, 1, "burn")
            try:
                await burn_subtitles_to_mp4(concat_list, dest_path, burn_sub, task_id=task_id)
            except MissAVError:
                # a broken subtitle/font must never lose the video itself
                logger.warning("字幕烧录失败，回退无字幕封装", exc_info=True)
                await remux_to_mp4(concat_list, dest_path)
        else:
            await remux_to_mp4(concat_list, dest_path)
        return {
            "title": info.get("title") or "",
            "thumbnail": info.get("thumbnail") or "",
            "segments": total,
            "host": referer_host,
            "details": details,
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if subtitle_track_path:
            try:
                os.remove(subtitle_track_path)
            except OSError:
                pass


async def probe_missav_subtitle(url, hosts=DEFAULT_MIRRORS):
    """探测 missav 页视频是否带 HLS 外挂字幕轨（下载前询问用，尽力而为）。

    页面 packed-JS → m3u8 → 主清单 EXT-X-MEDIA SUBTITLES。返回
    True=检测到字幕轨；False=无/探测失败（下载期 -sub 仍有运行时兜底）。
    """
    try:
        page_html, host = await asyncio.to_thread(
            fetch_video_page, url, tuple(hosts))
        m3u8_url = extract_m3u8_url(page_html)
        if not m3u8_url:
            return False
        m3u8_host = urlparse(m3u8_url).hostname or ""
        pinned_domain = _registered_domain(m3u8_host)
        if not _host_allowed(m3u8_url, pinned_domain):
            return False
        headers = {"Referer": f"https://{host}/"}
        playlist, playlist_url, master_url, subtitle_uri = await _resolve_media_playlist(
            m3u8_url, headers, pinned_domain
        )
        return bool(subtitle_uri)
    except Exception:
        logger.debug("missav subtitle probe failed %s", url, exc_info=True)
        return False



async def download_missav(url, dest_path, *, hosts=DEFAULT_MIRRORS,
                          concurrency=SEGMENT_CONCURRENCY, progress=None,
                          want_subtitle=False, task_id=None):
    """Download a missav video page to ``dest_path`` (.mp4).
    ``progress`` is an optional async callable ``(done, total, stage)``
    invoked after the page fetch, per segment and at merge time. Returns
    page metadata {'title','thumbnail','segments','host','details'}.

    ``want_subtitle`` (the bot's ``/dl -sub`` flag) opts IN to burning
    the page's HLS subtitle track — or, when the HLS carries none, the
    official getav VTT matched by code. A full libx264 re-encode, so the
    default is a plain fast remux; subtitle problems degrade to a plain
    video, never a failed download.
    """
    page_html, host = await asyncio.to_thread(fetch_video_page, url, tuple(hosts))
    if progress:
        await progress(0, 1, "page")

    info = extract_page_info(page_html)
    m3u8_url = extract_m3u8_url(page_html)
    if not m3u8_url:
        raise MissAVError("未找到 m3u8 地址（页面改版或视频不存在）")
    return await _download_hls_core(
        m3u8_url, dest_path, host, info,
        extract_video_details(page_html, url),
        concurrency=concurrency, progress=progress,
        want_site_subtitle=want_subtitle, task_id=task_id,
    )

async def download_getav(url, dest_path, *, hosts=GETAV_DEFAULT_MIRRORS,
                         concurrency=SEGMENT_CONCURRENCY, progress=None,
                         want_subtitle=False, source_url=None, task_id=None):
    """Download a getav.net video page to ``dest_path`` (.mp4).

    Same contract as :func:`download_missav`: the movie JSON API is
    fetched instead of an HTML page and the shared guarded HLS core does
    the rest. By default the best ``videoSources`` entry wins (cn > uc >
    raw family, then resolution); pass the source URL chosen on the /dl
    version card as ``source_url`` to pin a specific version.

    ``want_subtitle`` (the bot's ``/dl -sub`` flag) opts IN to burning
    the site's polished Chinese VTT into the frame — a full libx264
    re-encode (~40 min of CPU for a feature film), so the default is a
    plain fast remux. The ``cn`` source family already carries burned-in
    subs and never re-burns. Subtitle problems degrade to a plain
    video, never a failed download.
    """
    data, host = await asyncio.to_thread(fetch_getav_movie, url, tuple(hosts))
    if progress:
        await progress(0, 1, "page")

    sources = data.get("videoSources") or []
    if source_url:
        # user picked a version on the /dl selection card: honour it
        # verbatim (family still derived for badges/subtitle decisions)
        family = _getav_family_by_url(sources, source_url)
    else:
        source_url, family = select_getav_source(sources)
    if not source_url:
        raise MissAVError("视频没有可用的播放源")
    info = {
        "title": str(data.get("title") or ""),
        "thumbnail": getav_cover_url(data),
    }

    subtitle_path = None
    sub_entry = None
    if want_subtitle and family != "cn":
        sub_entry = select_getav_subtitle(data)
    if sub_entry:
        # pin subtitles to the video CDN's registered domain (same rule
        # the HLS core applies to playlists/keys/segments)
        pinned = _registered_domain(urlparse(source_url).hostname or "")
        subtitle_path = await asyncio.to_thread(
            _fetch_getav_subtitle, sub_entry, pinned,
            os.path.dirname(os.path.abspath(dest_path)),
        )
    try:
        return await _download_hls_core(
            source_url, dest_path, host, info,
            extract_getav_details(data, url, family),
            concurrency=concurrency, progress=progress,
            subtitle_path=subtitle_path,
            task_id=task_id,
        )
    finally:
        if subtitle_path:
            try:
                os.remove(subtitle_path)
            except OSError:
                pass
