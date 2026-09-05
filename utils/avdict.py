# Copyright (c) 2025 devgagan : https://github.com/devgaganin.
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

"""av 词库（av_dict）：演员中日名互查、标签别名归一、类别派生、番号快照。

同步 pymongo（enrich 在 to_thread 里跑，不进事件循环），模块级惰性单例
``MongoClient(config.MONGO_DB, serverSelectionTimeoutMS=2000,
connectTimeoutMS=2000, maxPoolSize=5)``。三个集合：

- ``av_actress``：{_id: 日文名, aliases: [变体], cn, hits, sources, updated_at}
- ``av_tag``    : {_id: 规范 CN 标签, aliases: [变体], category, hits, sources, updated_at}
- ``av_code``   : {_id: 番号, details: dict, updated_at}

**全部公开函数 try/except 吞异常（logger.info 带 exc_info）并返回安全
默认值**——pymongo 缺失、库连不上、数据坏，词库整体静默降级，绝不影响
下载管线。种子映射只做冷启动兜底，运行时以库为准。

测试约定：mongo 一律 monkeypatch——

    monkeypatch.setattr(avdict, "_cols", lambda: (FakeCol(), FakeCol()))

code_meta 走独立 seam :func:`_code_col`（同一 client），同样可整体替换。
"""

import logging
import re
import time
from datetime import datetime, timezone

import config
from utils.avdict_seed import (
    SEED_CATEGORY_ORDER,
    SEED_TAG_ALIASES,
    SEED_TAG_CATEGORIES,
)

logger = logging.getLogger(__name__)

_client = None  # pymongo.MongoClient 惰性单例（进程内共享）
_down_until = 0.0  # 熔断：mongo 连接类故障后 60s 内直接跳过（保护 to_thread 工作线程）

_CONN_ERR_NAMES = {
    "ServerSelectionTimeoutError", "AutoReconnect", "NetworkTimeout",
    "ConnectionFailure", "ConfigurationError", "InvalidClient",
}


def _trip_down() -> None:
    """熔断跳闸：60s 内所有词库操作直接降级返回。"""
    global _down_until
    _down_until = time.monotonic() + 60.0
    logger.warning("avdict: mongo 连接类故障，词库降级 60s")


def _down() -> bool:
    return time.monotonic() < _down_until


def _on_err(exc: Exception) -> None:
    """连接类异常触发熔断；其余仅 DEBUG（不打爆日志）。"""
    if type(exc).__name__ in _CONN_ERR_NAMES or isinstance(exc, ImportError):
        _trip_down()
    else:
        logger.debug("avdict 操作失败: %s", exc, exc_info=True)

# 种子派生（冷启动兜底；运行时以库为准）
_SEED_TAG2CAT = {
    tag: cat for cat, tags in SEED_TAG_CATEGORIES.items() for tag in tags
}
_SEED_ALIASES = dict(SEED_TAG_ALIASES)
_ORDER_INDEX = {cat: i for i, cat in enumerate(SEED_CATEGORY_ORDER)}

# 全角 ASCII（U+FF01–U+FF5E）→ 半角；表意空格另在 _norm_key 处理
_FULLWIDTH_TABLE = {cp: cp - 0xFEE0 for cp in range(0xFF01, 0xFF5F)}


# ---------------------------------------------------------------------------
# 惰性单例与集合
# ---------------------------------------------------------------------------

def _get_client():
    """惰性建立同步 MongoClient；pymongo 缺失/配置为空由调用方兜底。"""
    global _client
    if _client is None:
        import pymongo  # 延迟导入：环境无 pymongo 时词库整体静默降级
        _client = pymongo.MongoClient(
            config.MONGO_DB,
            serverSelectionTimeoutMS=2000,
            connectTimeoutMS=2000,
            maxPoolSize=5,
        )
    return _client


def _cols():
    """-> (av_actress, av_tag)；测试可整体 monkeypatch。"""
    db = _get_client()[config.DB_NAME]
    return db["av_actress"], db["av_tag"]


def _code_col():
    """av_code 集合（与 _cols 同一 client）；测试可整体 monkeypatch。"""
    return _get_client()[config.DB_NAME]["av_code"]


# ---------------------------------------------------------------------------
# 键归一
# ---------------------------------------------------------------------------

def _norm_key(name) -> str:
    """键归一：strip；全角→半角；连续空白折叠为单空格。非字符串返回 ""。"""
    if not isinstance(name, str):
        return ""
    s = name.translate(_FULLWIDTH_TABLE).replace("\u3000", " ").strip()
    return " ".join(s.split())[:100]  # 防 mongo 索引键超限/页面垃圾串


# ---------------------------------------------------------------------------
# 演员
# ---------------------------------------------------------------------------

def actress_cn(jp: str) -> str:
    """日文名查中文名；查不到返回 ""。优先 _id，其次 aliases 变体命中。"""
    if _down():
        return ""
    try:
        key = _norm_key(jp)
        if not key:
            return ""
        col, _ = _cols()
        row = col.find_one({"_id": key})
        if isinstance(row, dict) and row.get("cn"):
            return str(row["cn"])
        row = col.find_one({"aliases": key})
        if isinstance(row, dict) and row.get("cn"):
            return str(row["cn"])
        return ""
    except Exception as e:
        _on_err(e)
        return ""


def record_actress(jp: str, cn: str = "", source: str = "") -> None:
    """学习演员对照：upsert {_id, aliases, cn 仅空时填, hits++, sources, updated_at}。

    竞态安全：cn 走 $setOnInsert（首个写入者占位，服务器端原子）；已存在但
    cn 为空的行用服务端条件更新补填（无 read-modify-write 窗口）。已有非空
    cn 一律不覆盖；已知对照直接跳过（零写放大）；任何失败静默。
    """
    if _down():
        return
    try:
        key = _norm_key(jp)
        cn = (cn or "").strip()
        if not key or (cn and actress_cn(key) == cn):
            return  # 已知对照：不重复写（hits 不为此自增）
        col, _ = _cols()
        update = {
            "$set": {"updated_at": datetime.now(timezone.utc)},
            "$inc": {"hits": 1},
            "$addToSet": {"aliases": key},
        }
        if cn:
            update["$setOnInsert"] = {"cn": cn}
        if source:
            update["$addToSet"]["sources"] = source
        col.update_one({"_id": key}, update, upsert=True)
        if cn:
            # 既有行 cn 为空的场景：服务端条件补填（无竞态窗口）
            col.update_one(
                {"_id": key, "cn": {"$in": ["", None]}},
                {"$set": {"cn": cn}},
            )
    except Exception as e:
        _on_err(e)


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------

def normalize_tag(name: str) -> str:
    """别名→规范 CN 名（库优先，种子兜底）；unknown 返回归一化原名。"""
    try:
        key = _norm_key(name)
        if not key:
            return ""
        # ① 库（运行时以库为准）：_id 即规范名且带 aliases；或 aliases 命中取 _id
        try:
            _, tag_col = _cols()
            row = tag_col.find_one({"_id": key})
            if isinstance(row, dict) and row.get("aliases"):
                return str(row.get("_id") or key)
            row = tag_col.find_one({"aliases": key})
            if isinstance(row, dict) and row.get("_id"):
                return _norm_key(row["_id"]) or key
        except Exception:
            pass
        # ② 种子别名；unknown 原样（保留归一化结果）
        return _SEED_ALIASES.get(key, key)
    except Exception:
        logger.info("avdict.normalize_tag 失败 name=%r", name, exc_info=True)
        return _norm_key(name)


def record_tag(name: str, source: str = "") -> None:
    """unknown tag 学习：upsert {category: None}（已有 category 不覆盖）。"""
    try:
        key = _norm_key(name)
        if not key:
            return
        _, tag_col = _cols()
        update = {
            "$setOnInsert": {"category": None},
            "$set": {"updated_at": datetime.now(timezone.utc)},
            "$inc": {"hits": 1},
        }
        if source:
            update["$addToSet"] = {"sources": source}
        tag_col.update_one({"_id": key}, update, upsert=True)
    except Exception:
        logger.info("avdict.record_tag 写入失败 name=%r", name, exc_info=True)


def _category_of(tag: str) -> str | None:
    """tag → category：db 有 category 值则优先，否则查种子；都没有为 None。"""
    try:
        _, tag_col = _cols()
        row = tag_col.find_one({"_id": tag})
        if isinstance(row, dict) and row.get("category"):
            return str(row["category"])
    except Exception:
        pass
    return _SEED_TAG2CAT.get(tag)


def classify(genres: list[str]) -> tuple[list[str], list[str]]:
    """-> (规范化 tags 保序去重, categories 按 SEED_CATEGORY_ORDER 排序去重)。

    逐个 normalize_tag 后查类别（db 覆盖优先）；不在 SEED_CATEGORY_ORDER
    里的类别值不产出。
    """
    try:
        tags, cats = [], []
        for raw in genres or []:
            tag = normalize_tag(raw)
            if not tag or tag in tags:
                continue
            tags.append(tag)
            cat = _category_of(tag)
            if cat and cat not in cats:
                cats.append(cat)
        known = sorted(
            (c for c in cats if c in _ORDER_INDEX), key=_ORDER_INDEX.__getitem__
        )
        return tags, known
    except Exception:
        logger.info("avdict.classify 失败 genres=%r", genres, exc_info=True)
        return [], []


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def enhance(details: dict, source: str = "") -> dict:
    """词库总编排：演员补 cn、genres 归一、派生 categories、unknown 回写库。

    原地修改并返回 ``details``：

    - ``actresses`` 日文名 → 词库查 cn 补 ``actresses_cn``（已有 cn 不覆盖；
      位置对应，javbus/getav 先填的序位优先）；查不到的日名回写 av_actress。
    - ``genres`` 全量 normalize；unknown tag（种子与库都无类别）回写 av_tag。
    - 派生 ``categories``（有产出才写 key）。
    """
    try:
        if not isinstance(details, dict):
            return details

        # ① 演员：日名 → 中文名（序位对齐，已有 cn 不覆盖）
        jps = [
            n for n in (details.get("actresses") or [])
            if isinstance(n, str) and n.strip()
        ]
        old_cn = [
            c for c in (details.get("actresses_cn") or []) if isinstance(c, str)
        ]
        cn_names = []
        for i, jp in enumerate(jps):
            jp = jp.strip()
            existing = old_cn[i].strip() if i < len(old_cn) else ""
            cn = existing or actress_cn(jp)
            if cn:
                cn_names.append(cn)
            else:
                record_actress(jp, source=source)  # unknown 日名回写学习
        for extra in old_cn[len(jps):]:
            extra = extra.strip()
            if extra and extra not in cn_names:
                cn_names.append(extra)  # 多出的既有 cn 保留
        if cn_names:
            details["actresses_cn"] = cn_names

        # ② 标签：全量归一 + 派生类别 + unknown 回写
        genres = [g for g in (details.get("genres") or []) if isinstance(g, str)]
        tags, cats = classify(genres)
        if tags:
            details["genres"] = tags
        for tag in tags:
            if _category_of(tag) is None:
                record_tag(tag, source=source)
        if genres:
            # 派生类别恒定跟随当前 genres 重算：归一后无类可派也要清掉
            # 旧值，否则残留与 genres 不再对应的陈旧 categories
            details["categories"] = cats
        return details
    except Exception as e:
        _on_err(e)
        return details


# ---------------------------------------------------------------------------
# 番号快照
# ---------------------------------------------------------------------------

_SNAPSHOT_KEYS = {
    "title", "studio", "release_date",
    "actresses", "actresses_cn", "genres", "categories", "badges",
}
_CODE_KEY_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")


def _valid_code_key(code: str) -> bool:
    """快照键守卫：大写字母/数字/连字符且含数字，防面板垃圾串制造键碎片。"""
    return bool(code) and bool(_CODE_KEY_RE.fullmatch(code)) and any(c.isdigit() for c in code)


def code_meta_save(code: str, details: dict) -> None:
    """番号快照落库：av_code {_id: code, details: v2 白名单字段, updated_at}。

    仅存 v2 白名单内有内容的字段（空列表/空串/None 丢弃）；与既有快照
    顶层合并——部分补齐的 enrich 不会抹掉早前快照里更全的字段；键不合法
    （垃圾 code）直接跳过；失败静默。
    """
    if _down():
        return
    try:
        code = str(code or "").strip().upper()
        if not _valid_code_key(code) or not isinstance(details, dict):
            return
        slim = {k: v for k, v in details.items() if k in _SNAPSHOT_KEYS and v}
        if not slim:
            return
        col = _code_col()
        row = col.find_one({"_id": code}) or {}
        merged = dict(row.get("details") or {})
        merged.update(slim)  # 新值覆盖旧值；旧有新无的键保留
        col.update_one(
            {"_id": code},
            {"$set": {"details": merged, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except Exception as e:
        _on_err(e)


def code_meta_load(code: str, max_age_days: int = 30) -> dict | None:
    """读番号快照；键不合法 / 超龄 / 未来时间戳 / details 非空校验不过 → None。"""
    if _down():
        return None
    try:
        code = str(code or "").strip().upper()
        if not _valid_code_key(code):
            return None
        row = _code_col().find_one({"_id": code})
        if not isinstance(row, dict):
            return None
        details = row.get("details")
        if not isinstance(details, dict) or not details:
            return None
        updated = _parse_dt(row.get("updated_at"))
        if updated is None:
            return None
        age = datetime.now(timezone.utc) - updated
        if age.total_seconds() > max_age_days * 86400 or age.total_seconds() < -300:
            return None
        return dict(details)
    except Exception as e:
        _on_err(e)
        return None


def _parse_dt(value) -> datetime | None:
    """兼容 pymongo 的 naive UTC datetime 与 ISO 字符串；解析不了为 None。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None
