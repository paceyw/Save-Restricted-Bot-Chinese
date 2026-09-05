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
from utils.avdict_seed import SEED_TAG_ALIASES

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

# 种子别名（冷启动兜底；运行时以库为准）
_SEED_ALIASES = dict(SEED_TAG_ALIASES)

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


def record_tag_seen(name: str, source: str = "") -> None:
    """每个获取到的标签都计数沉淀（频率 = 跨影片出现次数，全局）。

    别名变体归并到规范名 _id 上计数（「中出し」与「中出」同条）；别名
    收进 aliases；失败静默。
    """
    if _down():
        return
    try:
        canonical = normalize_tag(name)
        if not canonical:
            return
        _, col = _cols()
        update = {
            "$set": {"updated_at": datetime.now(timezone.utc)},
            "$inc": {"hits": 1},
            "$addToSet": {"aliases": _norm_key(name)},
        }
        if source:
            update["$addToSet"]["sources"] = source
        col.update_one({"_id": canonical}, update, upsert=True)
    except Exception as e:
        _on_err(e)


def blacklist_tag(name: str) -> bool:
    """拉黑标签（词库黑名单/负面清单）；返回是否生效。失败静默 False。"""
    if _down():
        return False
    try:
        canonical = normalize_tag(name)
        if not canonical:
            return False
        _, col = _cols()
        col.update_one(
            {"_id": canonical},
            {"$set": {"blacklisted": True,
                      "updated_at": datetime.now(timezone.utc)},
             "$setOnInsert": {"hits": 0}},
            upsert=True)
        return True
    except Exception as e:
        _on_err(e)
        return False


def restore_tag(name: str) -> bool:
    """解除拉黑。失败静默 False。"""
    if _down():
        return False
    try:
        canonical = normalize_tag(name)
        if not canonical:
            return False
        _, col = _cols()
        col.update_one(
            {"_id": canonical},
            {"$set": {"blacklisted": False,
                      "updated_at": datetime.now(timezone.utc)}})
        return True
    except Exception as e:
        _on_err(e)
        return False


def _is_blacklisted(canonical: str) -> bool:
    """规范名是否在黑名单（库内标记；词库降级时按未拉黑处理）。"""
    try:
        _, col = _cols()
        row = col.find_one({"_id": canonical})
        return bool(isinstance(row, dict) and row.get("blacklisted"))
    except Exception as e:
        _on_err(e)
        return False


def list_tags(page: int = 0, per_page: int = 10):
    """词库分页（频率降序）：-> (rows, total)。row 含 hits/blacklisted。"""
    try:
        _, col = _cols()
        total = col.count_documents({})
        rows = list(col.find({}).sort("hits", -1).skip(max(page, 0) * per_page)
                    .limit(per_page))
        return rows, total
    except Exception as e:
        _on_err(e)
        return [], 0


def select_tags(genres, limit: int = 20) -> list[str]:
    """标签行产出：归一去重 → 拉黑过滤 → 全量频率沉淀 → 频率降序 top-N。

    - 归一合并相似标签（别名/分词变体同条计数，天然去重）；
    - 黑名单词永不进入影片信息；
    - 所有获取到的标签都入库计数（含最终被舍弃/拉黑的——拉黑前历史
      频率保留，解除拉黑后可恢复排序资格）；
    - 超过 limit 按频率从高到低舍弃（同频保持源顺序，稳定排序）。
    """
    try:
        cleaned, seen = [], set()
        for g in genres or []:
            if not isinstance(g, str):
                continue
            canonical = normalize_tag(g)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            if _is_blacklisted(canonical):
                continue
            cleaned.append(canonical)
        if not cleaned:
            return []
        freq = {}

        def _freq(tag):
            # 库故障降级为 0（全部同频 → 保持源顺序），绝不清空标签
            if tag not in freq:
                try:
                    _, col = _cols()
                    row = col.find_one({"_id": tag})
                    freq[tag] = int(row.get("hits") or 0) if isinstance(row, dict) else 0
                except Exception as e:
                    _on_err(e)
                    freq[tag] = 0
            return freq[tag]

        for tag in cleaned:
            record_tag_seen(tag)
        ranked = sorted(cleaned, key=_freq, reverse=True)
        return ranked[:max(limit, 0)]
    except Exception as e:
        _on_err(e)
        return []


def filter_badges(badges) -> list[str]:
    """类别行产出：badge 归一（别名表）+ 黑名单过滤 + 频率沉淀。"""
    try:
        out, seen = [], set()
        for b in badges or []:
            if not isinstance(b, str):
                continue
            canonical = normalize_tag(b)
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            if _is_blacklisted(canonical):
                continue
            record_tag_seen(canonical)
            out.append(canonical)
        return out
    except Exception as e:
        _on_err(e)
        return []


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def enhance(details: dict, source: str = "") -> dict:
    """词库总编排：演员补 cn、标签归一/黑名单/top-20、badges 过滤。

    原地修改并返回 ``details``：

    - ``actresses`` 日文名 → 词库查 cn 补 ``actresses_cn``（已有 cn 不覆盖；
      位置对应，javbus/getav 先填的序位优先）；查不到的日名回写 av_actress。
    - ``genres`` 归一去重 + 黑名单过滤 + 全量频率沉淀 + 频率降序 top-20。
    - ``badges``（类别行）归一 + 黑名单过滤 + 频率沉淀。
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

        # ② 标签：归一去重 + 黑名单过滤 + 全量频率沉淀 + top-20
        genres = [g for g in (details.get("genres") or []) if isinstance(g, str)]
        if genres:
            details["genres"] = select_tags(genres)
        # ③ 类别行（badges）：归一 + 黑名单过滤 + 频率沉淀
        badges = details.get("badges")
        if isinstance(badges, list) and badges:
            details["badges"] = filter_badges(badges)
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
