"""Structured five-block caption reformatting for forwarded messages.

When a /single /batch /merge source text already carries catalog-ish
elements (JAV 番号, labelled 演员/标签/类别 lines, bare hashtags), reformat
it into the missav five-block layout

    <番号>

    <原文剩余内容作为简介>

    演员：#… 
    标签：#… 
    类别：#…

so the user can fill the missing ones in by hand (per the 2026-08-15
request: 内容我可以手动编辑). Rendering reuses missav's ``_hashtag``.

``restructure_caption`` returns ``None`` when nothing structured is
found — callers keep the original text untouched in that case. A user
``oc`` override always wins and never passes through here.
"""

import re

from utils.missav import _hashtag

# JAV 番号: 2-7 letters, dash, 2-5 digits — "GVH-690", "DASS-629".
# The dash is REQUIRED: dashless forms ("gvh690") produce far too many
# false positives in ordinary prose. Lookarounds keep the match off the
# edge of longer alphanumeric tokens (filenames, URLs).
_CODE_RE = re.compile(r'(?<![A-Za-z0-9-])([A-Za-z]{2,7})-(\d{2,5})(?![0-9A-Za-z])')
# FC2 uses a wider numeric range and an optional PPV infix.
_FC2_RE = re.compile(r'(?<![A-Za-z0-9])FC2[-_ ]?(?:PPV[-_ ]?)?(\d{4,9})(?![0-9])', re.IGNORECASE)
# Tech words shaped like codes ("COVID-19") must not become 番号.
_CODE_BLACKLIST = {
    'COVID', 'GB', 'MB', 'KB', 'TB', 'HD', 'SD', 'FHD', 'UHD', 'CD', 'DVD',
    'EP', 'TV', 'PC', 'VR', 'NO', 'ID', 'ISBN', 'PART', 'VOL', 'TOP', 'H264',
    'H265', 'X264', 'X265', 'HEVC', 'AVC', 'AAC', 'FLAC', 'MP3',
}

_ACTRESS_LABELS = ('女主角', '演员', '演員', '出演', '主演', '女优', '女優', '主役', '女主')
_TAG_LABELS = ('标签', '標籤', 'tags', 'tag')
_CAT_LABELS = ('类别', '類別', '分类', '分類', '类型', '類型', '题材', '題材', 'categories', 'category')

def _label_re(labels):
    # longest-first so "女主角" wins over "女主"
    alts = '|'.join(sorted(labels, key=len, reverse=True))
    return re.compile(r'^\s*(?:' + alts + r')\s*[：:=＝]\s*(.*)$', re.IGNORECASE)

_ACTRESS_RE = _label_re(_ACTRESS_LABELS)
_TAG_RE = _label_re(_TAG_LABELS)
_CAT_RE = _label_re(_CAT_LABELS)

# bare hashtags in the leftover text: keep chars that Telegram hashtags
# tolerate inside a word, stop at CJK/ASCII punctuation and whitespace
_HASHTAG_RE = re.compile(r'#([^\s#,，.。:：;；!！?？~“”"\'()（）\[\]【】{}<>《》|/\\]+)')
_VALUE_SPLIT_RE = re.compile(r'[\s、，,／/|·]+')

# 简介 cap: leaves ~400 chars of budget for the three hashtag lines
# inside Telegram's 1024 caption limit.
_INTRO_MAX = 600


def _split_values(raw):
    return [v for v in (x.strip().lstrip('#').strip() for x in _VALUE_SPLIT_RE.split(raw or '')) if v]


def _dedup(seq):
    seen = set()
    out = []
    for x in seq:
        key = x.casefold()
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


def _find_code(text):
    """Best-effort 番号 from the whole text, with the blacklist applied."""
    m = _FC2_RE.search(text)
    if m:
        return f'FC2-PPV-{m.group(1)}'
    for m in _CODE_RE.finditer(text):
        prefix = m.group(1).upper()
        if prefix not in _CODE_BLACKLIST:
            return f'{prefix}-{m.group(2)}'
    return ''


def parse_details(text):
    """Extract caption ingredients from arbitrary forwarded text.

    Returns the ``build_caption`` details dict, or ``None`` when nothing
    structured is present (callers then keep the original text).
    """
    if not text or not text.strip():
        return None

    actresses, genres, badges = [], [], []
    labelled = False
    kept_lines = []
    for line in text.splitlines():
        m = _ACTRESS_RE.match(line)
        if m:
            actresses.extend(_split_values(m.group(1)))
            labelled = True
            continue
        m = _TAG_RE.match(line)
        if m:
            genres.extend(_split_values(m.group(1)))
            labelled = True
            continue
        m = _CAT_RE.match(line)
        if m:
            badges.extend(_split_values(m.group(1)))
            labelled = True
            continue
        kept_lines.append(line)
    remainder = '\n'.join(kept_lines)

    code = _find_code(text)
    if code:
        # drop the 番号 token itself so the intro does not repeat it —
        # match the original spacing/case variants ("gvh-690", "FC2_PPV_1")
        tok = re.escape(code).replace(r'\-', '[-_\\ ]?')
        remainder = re.sub(tok, '', remainder, count=1, flags=re.IGNORECASE)

    bare_tags = _HASHTAG_RE.findall(remainder)
    for tag in bare_tags:
        remainder = remainder.replace(f'#{tag}', '', 1)
    genres.extend(t for t in (x.strip() for x in bare_tags) if t)

    details = {
        'code': code,
        'title': '',
        'actresses': _dedup(actresses),
        'genres': _dedup(genres),
        'badges': _dedup(badges),
    }

    # trigger: a real 番号, any labelled line, or a hashtag run. A lone
    # hashtag in ordinary prose is not worth reformatting the text.
    if not (code or labelled or len(bare_tags) >= 2):
        return None

    intro = re.sub(r'\n{3,}', '\n\n', remainder).strip(' \n\t·-—')
    if len(intro) > _INTRO_MAX:
        intro = intro[:_INTRO_MAX].rstrip() + '…'
    details['title'] = intro
    return details


def _hashtag_line(label, values):
    return f'{label}：' + ' '.join(t for t in (_hashtag(x) for x in values) if t)


def _render_skeleton(details, max_len=1024):
    """Fixed skeleton — FIVE content lines with TWO blank separators:

        <番号>

        <简介>

        演员：#…
        标签：#…
        类别：#…

    Missing items stay as empty lines / bare labels so the user can fill
    them in by hand; positions never shift between forwards.
    """
    code = (details.get('code') or '').strip()
    intro = (details.get('title') or '').strip()
    tail = '\n'.join([
        _hashtag_line('演员', details.get('actresses') or []),
        _hashtag_line('标签', details.get('genres') or []),
        _hashtag_line('类别', details.get('badges') or []),
    ])
    budget = max_len - len(tail) - 4  # two blank separators

    def render(intro_text):
        # keep every block slot (a missing 简介 stays as a blank line the
        # user can fill in); only LEADING empty blocks are dropped so a
        # missing 番号 never leaves blank lines at the top
        blocks = [code, intro_text, tail]
        while blocks and blocks[0] == '':
            blocks.pop(0)
        return '\n\n'.join(blocks)

    if len(intro) > budget:
        intro = intro[:max(0, budget - 1)].rstrip() + '…'
    return render(intro)[:max_len]


def restructure_caption(text):
    """Five-line skeleton caption for ``text``, or ``None`` to keep the
    original text (nothing structured detected / user oc override)."""
    details = parse_details(text)
    if details is None:
        return None
    return _render_skeleton(details) or None
