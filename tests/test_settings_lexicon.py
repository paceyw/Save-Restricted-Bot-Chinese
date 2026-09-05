"""Offline tests: 设置页词库管理视图（/settings → 📚 词库管理）。

复用 test_settings_routing 的 stub 装载器思路：pyrogram/shared_client 全
替身；avdict 以 FakeCol 注入（与 test_avdict 同款），网络零调用。
"""

import asyncio
import importlib
import os
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

os.environ.setdefault("MASTER_KEY", "settings-lexicon-test-master")
os.environ.setdefault("IV_KEY", "settings-lexicon-test-iv")

from utils import avdict  # noqa: E402


class _Filter:
    def __and__(self, other):
        return self

    def __rand__(self, other):
        return self

    def __invert__(self):
        return self

    @staticmethod
    def __call__(*args, **kwargs):
        return True


class _Filters:
    private = _Filter()

    @staticmethod
    def command(*args, **kwargs):
        return _Filter()

    @staticmethod
    def regex(pattern):
        return _Filter()

    @staticmethod
    def create(callback):
        return _Filter()


class _FakeApp:
    def on_message(self, *args, **kwargs):
        def decorator(function):
            return function
        return decorator

    def on_callback_query(self, *args, **kwargs):
        return self.on_message(*args, **kwargs)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction):
        self._docs.sort(key=lambda d: d.get(key) or 0, reverse=direction < 0)
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeCol:
    def __init__(self, rows=None, fail=False):
        self.rows = dict(rows or {})
        self.fail = fail

    def _check(self):
        if self.fail:
            raise RuntimeError("mongo down")

    def find_one(self, query):
        self._check()
        return self.rows.get(query.get("_id"))

    def find(self, query):
        self._check()
        return _FakeCursor(list(self.rows.values()))

    def count_documents(self, query):
        self._check()
        return len(self.rows)

    def update_one(self, query, update, upsert=False):
        self._check()
        _id = query.get("_id")
        doc = self.rows.setdefault(_id, {"_id": _id})
        for field, value in update.get("$set", {}).items():
            doc[field] = value
        for field, value in update.get("$setOnInsert", {}).items():
            doc.setdefault(field, value)
        for field, delta in update.get("$inc", {}).items():
            doc[field] = doc.get(field, 0) + delta
        for field, value in update.get("$addToSet", {}).items():
            lst = doc.setdefault(field, [])
            if value not in lst:
                lst.append(value)


class _FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


class _FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = _FakeMessage()
        self.answered = 0

    async def answer(self):
        self.answered += 1


def _load_settings_module(monkeypatch):
    pyrogram = types.ModuleType("pyrogram")
    pyrogram.filters = _Filters()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)
    pyrogram_types = types.ModuleType("pyrogram.types")

    class _FakeIK:
        def __init__(self, text=None, callback_data=None, url=None):
            self.text = text
            self.callback_data = callback_data
            self.url = url

    class _FakeIKM:
        def __init__(self, keyboard):
            self.keyboard = keyboard

    pyrogram_types.InlineKeyboardButton = _FakeIK
    pyrogram_types.InlineKeyboardMarkup = _FakeIKM
    monkeypatch.setitem(sys.modules, "pyrogram.types", pyrogram_types)
    shared_client = types.ModuleType("shared_client")
    shared_client.app = _FakeApp()
    shared_client._WORKDIR = "/tmp"
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)
    utils = types.ModuleType("utils")
    utils.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils)
    func = types.ModuleType("utils.func")
    func.get_user_data_key = None
    func.save_user_data = None
    func.bump_cred_epoch = lambda _uid: None
    monkeypatch.setitem(sys.modules, "utils.func", func)
    plugins = types.ModuleType("plugins")
    plugins.__path__ = [str(SRC / "plugins")]
    monkeypatch.setitem(sys.modules, "plugins", plugins)
    sys.modules.pop("plugins.settings", None)
    return importlib.import_module("plugins.settings")


@pytest.fixture
def cols(monkeypatch):
    monkeypatch.setattr(avdict, "_down_until", 0.0)
    actress, tag, code = FakeCol(), FakeCol(), FakeCol()
    monkeypatch.setattr(avdict, "_cols", lambda: (actress, tag))
    monkeypatch.setattr(avdict, "_code_col", lambda: code)
    return actress, tag, code


def test_lexicon_view_lists_by_frequency(cols, monkeypatch):
    module = _load_settings_module(monkeypatch)
    _, tag, _ = cols
    for name, hits in [("中出", 30), ("巨乳", 99), ("痴女", 5)]:
        tag.rows[name] = {"_id": name, "hits": hits}
    query = _FakeQuery("lexicon")
    asyncio.run(module.lexicon_callback(None, query))
    text, kb = query.message.edits[0]
    assert "共 3 条" in text and "1/1" in text
    buttons = str(kb)
    order = [buttons.find(x) for x in ("巨乳", "中出", "痴女")]
    assert order == sorted(order)  # 频率降序
    assert query.answered == 1


def test_lexicon_toggle_blacklist_then_restore(cols, monkeypatch):
    module = _load_settings_module(monkeypatch)
    _, tag, _ = cols
    tag.rows["巨乳"] = {"_id": "巨乳", "hits": 7}
    # 点击第 0 条 → 拉黑
    asyncio.run(module.lexicon_callback(None, _FakeQuery("lexb:0:0")))
    assert tag.rows["巨乳"]["blacklisted"] is True
    assert tag.rows["巨乳"]["hits"] == 7  # 拉黑不清频
    # 再点击 → 恢复
    asyncio.run(module.lexicon_callback(None, _FakeQuery("lexb:0:0")))
    assert tag.rows["巨乳"]["blacklisted"] is False


def test_blacklisted_tag_excluded_from_select_tags(cols, monkeypatch):
    """UI 拉黑 → 下载管线同源生效：select_tags 不再产出该词。"""
    module = _load_settings_module(monkeypatch)
    _, tag, _ = cols
    tag.rows["巨乳"] = {"_id": "巨乳", "hits": 7}
    asyncio.run(module.lexicon_callback(None, _FakeQuery("lexb:0:0")))
    assert avdict.select_tags(["巨乳", "中出"]) == ["中出"]


def test_lexicon_back_returns_settings_menu(cols, monkeypatch):
    module = _load_settings_module(monkeypatch)
    query = _FakeQuery("lexback")
    asyncio.run(module.lexicon_callback(None, query))
    text, kb = query.message.edits[0]
    assert "自定义文件设置" in text
    assert query.answered == 1


def test_lexicon_empty_db_shows_placeholder(cols, monkeypatch):
    module = _load_settings_module(monkeypatch)
    query = _FakeQuery("lexicon")
    asyncio.run(module.lexicon_callback(None, query))
    text, _ = query.message.edits[0]
    assert "词库为空" in text


def test_main_menu_has_lexicon_entry(cols, monkeypatch):
    module = _load_settings_module(monkeypatch)
    kb = module.settings_menu()
    buttons = [b.callback_data for row in kb.keyboard for b in row]
    texts = [b.text for row in kb.keyboard for b in row]
    assert "lexicon" in buttons and "📚 词库管理" in texts
