import asyncio
import importlib.util
import sys
import types
from pathlib import Path


SRC = Path(__file__).resolve().parents[1]


class _Filter:
    def __and__(self, other):
        return self

    def __invert__(self):
        return self


class _Filters:
    private = _Filter()

    @staticmethod
    def command(*args, **kwargs):
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


def _load_settings_module(monkeypatch):
    pyrogram = types.ModuleType("pyrogram")
    pyrogram.filters = _Filters()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)

    pyrogram_types = types.ModuleType("pyrogram.types")
    pyrogram_types.InlineKeyboardButton = object
    pyrogram_types.InlineKeyboardMarkup = object
    monkeypatch.setitem(sys.modules, "pyrogram.types", pyrogram_types)

    shared_client = types.ModuleType("shared_client")
    shared_client.app = _FakeApp()
    shared_client._WORKDIR = "/tmp"
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    utils = types.ModuleType("utils")
    utils.__path__ = []
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
    import importlib
    module = importlib.import_module("plugins.settings")
    return module


def test_active_conversation_filter_only_claims_active_user(monkeypatch):
    module = _load_settings_module(monkeypatch)

    inactive_message = types.SimpleNamespace(
        from_user=types.SimpleNamespace(id=42)
    )
    assert module.active_conversation_filter(None, None, inactive_message) is False

    module.active_conversations[42] = {"type": "setchat"}
    assert module.active_conversation_filter(None, None, inactive_message) is True

    other_user_message = types.SimpleNamespace(
        from_user=types.SimpleNamespace(id=43)
    )
    assert module.active_conversation_filter(None, None, other_user_message) is False
    assert module.active_conversation_filter(
        None, None, types.SimpleNamespace(from_user=None)
    ) is False

def test_rename_file_uses_snapshot_settings(monkeypatch, tmp_path):
    module = _load_settings_module(monkeypatch)
    source = tmp_path / "oldfoo.mp4"
    source.write_bytes(b"data")
    renamed = []
    monkeypatch.setattr(module.os, "rename", lambda old, new: renamed.append((old, new)))

    settings = {
        "delete_words": ["old"],
        "rename_tag": "TAG",
        "replacement_words": {"foo": "bar"},
    }
    result = asyncio.run(module.rename_file(str(source), 42, None, settings))

    assert result.endswith("/bar TAG.mp4")
    assert renamed == [(str(source), result)]
