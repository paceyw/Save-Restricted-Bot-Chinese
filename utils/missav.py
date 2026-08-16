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
from html import unescape as html_unescape
import tempfile as _tempfile
from urllib.parse import urljoin, urlparse, urlunparse

import m3u8
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

# ─── constants ─────────────────────────────────────────────────────────────────

DEFAULT_MIRRORS = ("missav.ai", "missav.ws", "missav.live", "missav123.com")
GETAV_DEFAULT_MIRRORS = ("getav.net",)

_LANG_PREFIXES = ("cn", "en", "ja", "ko", "ms", "th")
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SEGMENT_RETRIES = 3        # attempts per segment before failing the job
SEGMENT_RETRIES_RATE_LIMITED = 6   # extended budget while the CDN 429s us
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

BLOCKED_MSG = (
    "所有 missav 镜像均被 Cloudflare 拦截或不可达，"
    "请稍后重试或更换网络/代理后重试"
)


class MissAVError(Exception):
    """Generic missav download failure (page layout change, missing video…)."""


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
    ("chinese-subtitle", "中文字幕"),
    ("uncensored", "无码"),
    ("uncensored-leaked", "无码流出"),
    ("leaked", "流出"),
)


def extract_video_details(page_html, url):
    """Parse the labelled info panel + og meta into caption ingredients.

    Returns {'code','title','actresses','genres','badges'} — every field
    degrades independently (page layout changes must not break downloads).
    """
    details = {"code": "", "title": "", "actresses": [], "genres": [], "badges": []}

    code = _panel_plain(_panel_section(page_html, "code"))
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
    details["genres"] = _panel_links(_panel_section(page_html, "genres"))

    slug = (parse_missav_url(url) or {}).get("slug", "") or ""
    lowered = slug.lower()
    badges = [label for token, label in _SLUG_BADGES if token in lowered]
    # keep order but drop the weaker "leaked"/"uncensored" when the
    # combined badge already covers them
    if "无码流出" in badges:
        badges = [b for b in badges if b not in ("无码", "流出", "无码流出")]
        badges.append("无码流出")
    details["badges"] = badges

    if not details["code"]:
        m = _CODE_FROM_SLUG.match(slug)
        if m:
            details["code"] = f"{m.group(1).upper()}-{m.group(2)}"
    return details


def _hashtag(text):
    """'#' + text with characters that break Telegram hashtags mapped away."""
    cleaned = re.sub(r"[\s#\n]+", "_", html_unescape(text).strip())
    cleaned = cleaned.strip("_")
    return f"#{cleaned}" if cleaned else ""


def build_caption(details, max_len=1024):
    """Five-block caption per issue #13 style:

        DASS-629\n\n<intro>\n\n演员：#…\n标签：#…\n类别：#…

    The three hashtag lines form ONE block (single newlines) separated
    from the intro by a blank line, matching the reference layout.
    Blocks with no data are omitted; hashtag lines are trimmed from the
    tail when the whole caption would exceed Telegram's 1024 limit.
    """
    code = (details.get("code") or "").strip()
    intro = (details.get("title") or "").strip()
    actresses = [t for t in (_hashtag(x) for x in details.get("actresses") or []) if t]
    genres = [t for t in (_hashtag(x) for x in details.get("genres") or []) if t]
    badges = [t for t in (_hashtag(x) for x in details.get("badges") or []) if t]

    blocks = []
    if code:
        blocks.append(code)
    if intro:
        blocks.append(intro)

    tag_lines = []
    if actresses:
        tag_lines.append("演员：" + " ".join(actresses))
    if genres:
        tag_lines.append("标签：" + " ".join(genres))
    if badges:
        tag_lines.append("类别：" + " ".join(badges))
    if tag_lines:
        blocks.append("\n".join(tag_lines))
    if not blocks:
        return ""

    def render(parts):
        return "\n\n".join(parts)

    # hashtag lines carry a 「label：」prefix; trim their tails (keep the
    # label + one tag) until the caption fits Telegram's limit
    def is_tag_line(line):
        return line.startswith(("演员：", "标签：", "类别：", "演员:", "标签:", "类别:"))

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
    return render(blocks)[:max_len]


def extract_m3u8_url(html):
    """Unpack every packed script and pull the primary m3u8 URL.

    Primary match is ``source=…``; fallback is any m3u8 URL in the block.
    """
    for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
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
        resp, err = _http_get(
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
            return data, final or urlparse(candidate).hostname

    if saw_content:
        raise MissAVError(f"影片数据解析失败（视频不存在或接口改版）: {url}")
    if saw_404:
        raise MissAVError(f"视频不存在或已删除: {url}")
    raise MissAVBlockedError(BLOCKED_MSG)


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


def extract_getav_details(data, url, family=None):
    """Movie JSON -> caption ingredients (same shape as missav details).

    ``family`` is the chosen source family ('cn'/'uc'/'raw'/None) and
    only badges the actually downloaded stream.
    """
    details = {"code": "", "title": "", "actresses": [], "genres": [], "badges": []}

    code = data.get("id") or (parse_getav_url(url) or {}).get("slug", "") or ""
    details["code"] = str(code).upper()

    # API title: "[中文字幕] CJOD-159 … 妃月るい" style — drop bracket
    # badges and the leading code, keep the original (actress-bearing)
    # title as the intro line.
    title = str(data.get("title") or "")
    title = re.sub(r"^\s*(?:\[[^\]]*\]\s*)+", "", title)
    if details["code"] and title.upper().startswith(details["code"]):
        title = title[len(details["code"]):]
    details["title"] = title.strip()

    stars = data.get("stars")
    if isinstance(stars, list):
        names = []
        for star in stars:
            name = star.get("name") if isinstance(star, dict) else None
            name = str(name).strip() if name else ""
            if name and name not in names:
                names.append(name)
        details["actresses"] = names

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
    subtitles = data.get("subtitles")
    has_zh_sub = isinstance(subtitles, list) and any(
        isinstance(s, dict) and str(s.get("language") or "").lower().startswith("zh")
        for s in subtitles
    )
    if family == "cn" or has_zh_sub:
        badges.append("中文字幕")
    if family == "uc" or data.get("uc") == 1:
        badges.append("无码")
    details["badges"] = badges
    return details


# ─── ffmpeg remux ──────────────────────────────────────────────────────────────

async def _run_ffmpeg(args):
    """Await ffmpeg; thin seam for tests. Raises MissAVError on failure."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise MissAVError(f"ffmpeg remux 失败: {err.decode(errors='replace')[-300:]}")


async def remux_to_mp4(src, dst):
    """Copy-remux TS -> MP4 (+faststart). Zero re-encode, seconds."""
    if not shutil.which("ffmpeg"):
        raise MissAVError("服务器缺少 ffmpeg，无法封装 MP4")
    await _run_ffmpeg(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-c", "copy", "-bsf:a", "aac_adtstoasc",
         "-movflags", "+faststart", dst]
    )
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise MissAVError("ffmpeg 未产出有效 MP4")


# Fansub-style rendering: white glyphs, black outline + soft shadow,
# bottom-center, bold CJK sans — validated visually 2026-08-16.
_SUBTITLE_FORCE_STYLE = (
    "FontName=Noto Sans CJK SC,Bold=1,FontSize=52,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=1,MarginV=36,Alignment=2"
)
BURN_PRESET = "veryfast"   # x264 speed preset: ~4x realtime on 3 vCPU
BURN_CRF = 19              # visually lossless tier for a re-encode


def _escape_filter_path(path):
    """Escape a path for use inside a filtergraph argument."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _burn_threads():
    """Encode threads: leave one core for the bot itself."""
    return max(2, min(8, (os.cpu_count() or 3) - 1))


async def burn_subtitles_to_mp4(src, dst, subtitle_path):
    """Re-encode TS -> MP4 with Chinese subtitles rendered INTO the frame.

    Full libx264 re-encode (the only way to burn subs): measured on the
    3-vCPU box, veryfast/CRF19 runs ~4x realtime (~36 min for a 2.5h
    movie), peak RSS ~0.5GB. The subtitles filter matches cues against
    the 0-based frame timeline, which is exactly how getav's player
    authors its VTTs, so no offset correction is needed.
    """
    if not shutil.which("ffmpeg"):
        raise MissAVError("服务器缺少 ffmpeg，无法烧录字幕")
    vf = (
        f"subtitles={_escape_filter_path(subtitle_path)}"
        f":force_style='{_SUBTITLE_FORCE_STYLE}'"
    )
    await _run_ffmpeg([
        "ffmpeg", "-y", "-loglevel", "error", "-i", src,
        "-vf", vf,
        "-c:v", "libx264", "-preset", BURN_PRESET, "-crf", str(BURN_CRF),
        "-threads", str(_burn_threads()),
        "-c:a", "copy", "-movflags", "+faststart", dst,
    ])
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise MissAVError("ffmpeg 未产出有效 MP4")


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


# ─── orchestration ─────────────────────────────────────────────────────────────

async def _resolve_media_playlist(m3u8_url, headers, pinned_domain):
    """Follow variant playlists (bounded, host-pinned) to the media playlist."""
    url = m3u8_url
    for _ in range(MAX_PLAYLIST_HOPS):
        if not _host_allowed(url, pinned_domain):
            raise MissAVError(f"m3u8 地址域校验失败: {urlparse(url).hostname}")
        resp, err = await asyncio.to_thread(
            _http_get, url, headers, PAGE_TIMEOUT, PAGE_MAX_BYTES
        )
        if resp is None or resp.status_code != 200:
            raise MissAVError(f"m3u8 获取失败: {err or getattr(resp, 'status_code', '?')}")
        playlist = m3u8.loads(resp.text or "")
        variant_uri = select_variant_uri(playlist)
        if not variant_uri:
            if not playlist.segments:
                raise MissAVError("m3u8 无有效片段（可能被拦截或改版）")
            return playlist, url
        url = _absolute(url, variant_uri)
    raise MissAVError("m3u8 嵌套层级过深，疑似改版")


async def _download_one_segment(index, seg_url, temp_dir, key, iv_factory, headers,
                                pinned_domain, budget):
    """Fetch (and decrypt) one TS segment to an index-named temp file.

    ``budget`` is a one-element list holding cumulative downloaded bytes
    across the job; exceeding MAX_TOTAL_BYTES aborts the whole download.
    """
    last_err = None
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
                if budget[0] + len(data) > MAX_TOTAL_BYTES:
                    raise MissAVError(
                        f"累计下载量超预算 ({budget[0] + len(data)} > {MAX_TOTAL_BYTES} bytes)，中止")
                if key is not None:
                    data = await asyncio.to_thread(
                        decrypt_segment, data, key, iv_factory(index)
                    )
                path = os.path.join(temp_dir, f"{index:06d}.ts")
                with open(path, "wb") as fh:
                    fh.write(data)
                budget[0] += len(data)
                return path
        else:
            last_err = f"segment {index}: HTTP {status if status is not None else err}"
            if status == 429 and max_attempts < SEGMENT_RETRIES_RATE_LIMITED:
                # a CDN rate-limit is sustained, not transient: switch to
                # the long-backoff budget instead of failing the whole job
                max_attempts = SEGMENT_RETRIES_RATE_LIMITED
        if attempt < max_attempts:
            cap = 60 if status == 429 else 8
            await asyncio.sleep(min(2 ** attempt, cap))
    raise MissAVError(f"片段 {index} 下载失败: {last_err}")


async def _download_hls_core(m3u8_url, dest_path, referer_host, info, details,
                             concurrency=SEGMENT_CONCURRENCY, progress=None,
                             subtitle_path=None):
    """Shared missav/getav tail: m3u8 -> guarded segments -> merged mp4.

    ``info`` is {'title','thumbnail'}, ``details`` the caption
    ingredients (already parsed by the caller). Returns
    {'title','thumbnail','segments','host','details'}.

    Resource guards: segment count, cumulative bytes, total duration and
    free disk are all checked before/while downloading, so a hostile
    playlist cannot exhaust the host.
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

    playlist, playlist_url = await _resolve_media_playlist(m3u8_url, headers, pinned_domain)
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
            _http_get, key_url, headers, PAGE_TIMEOUT, KEY_MAX_BYTES
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
        seg_urls = [_absolute(playlist_url, s.uri) for s in segments]
        total = len(seg_urls)
        budget = [0]  # cumulative downloaded bytes (single-threaded loop)
        results = [None] * total
        queue = asyncio.Queue()
        for i, u in enumerate(seg_urls):
            queue.put_nowait((i, u))
        done = 0

        async def worker():
            nonlocal done
            while True:
                try:
                    i, seg_url = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                path = await _download_one_segment(
                    i, seg_url, temp_dir, key_bytes, iv_factory, headers,
                    pinned_domain, budget,
                )
                results[i] = path
                done += 1
                await _report(done, total, "segments")

        workers = [asyncio.create_task(worker()) for _ in range(max(1, concurrency))]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for t in workers:
                t.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        await _report(total, total, "merge")
        merged = os.path.join(temp_dir, "merged.ts")
        with open(merged, "wb") as out:
            for path in results:
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)

        if subtitle_path:
            await _report(0, 1, "burn")
            try:
                await burn_subtitles_to_mp4(merged, dest_path, subtitle_path)
            except MissAVError:
                # a broken subtitle/font must never lose the video itself
                logger.warning("字幕烧录失败，回退无字幕封装", exc_info=True)
                await remux_to_mp4(merged, dest_path)
        else:
            await remux_to_mp4(merged, dest_path)
        return {
            "title": info.get("title") or "",
            "thumbnail": info.get("thumbnail") or "",
            "segments": total,
            "host": referer_host,
            "details": details,
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def download_missav(url, dest_path, *, hosts=DEFAULT_MIRRORS,
                          concurrency=SEGMENT_CONCURRENCY, progress=None):
    """Download a missav video page to ``dest_path`` (.mp4).

    ``progress`` is an optional async callable ``(done, total, stage)``
    invoked after the page fetch, per segment and at merge time. Returns
    page metadata {'title','thumbnail','segments','host','details'}.
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
    )


async def download_getav(url, dest_path, *, hosts=GETAV_DEFAULT_MIRRORS,
                         concurrency=SEGMENT_CONCURRENCY, progress=None,
                         want_subtitle=False):
    """Download a getav.net video page to ``dest_path`` (.mp4).

    Same contract as :func:`download_missav`: the movie JSON API is
    fetched instead of an HTML page, the best ``videoSources`` entry
    (cn > uc > raw family, then resolution) becomes the playlist, and
    the shared guarded HLS core does the rest.

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

    source_url, family = select_getav_source(data.get("videoSources") or [])
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
        )
    finally:
        if subtitle_path:
            try:
                os.remove(subtitle_path)
            except OSError:
                pass
