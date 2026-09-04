"""Public-link fetch order regression tests (plan Phase 2 §5.1).

The bot client now fetches public messages FIRST so delivery takes the
zero-byte file_id direct path. Guards keep the user client as fetcher when
ITS bytes are required downstream:
  - albums (copy_media_group fallback re-downloads via the user client),
  - media > 2 GiB (bot cannot re-download if direct send is rejected).

Harness mirrors test_custom_bot_flow.py: plugins.fetch imported for real
against stubbed pyrogram/config/shared_client/utils.
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1]

import os
os.environ.setdefault("MASTER_KEY", "fetch-test-master")
os.environ.setdefault("IV_KEY", "fetch-test-iv")

_GIB = 1024 * 1024 * 1024


class _FakeClient:
    def __init__(self, name, messages=None, fail=False):
        self.name = name
        self.calls = []
        self.messages = messages or {}
        self.fail = fail

    async def get_messages(self, chat, msg_id):
        self.calls.append((chat, msg_id))
        if self.fail:
            raise RuntimeError("cannot resolve")
        return self.messages.get(msg_id)


def _video_msg(size, group_id=None, chat_id=-100123):
    return SimpleNamespace(
        video=SimpleNamespace(file_size=size, file_name="v.mp4"),
        media_group_id=group_id,
        chat=SimpleNamespace(id=chat_id),
        empty=False,
    )


@pytest.fixture()
def fetch_module(monkeypatch):
    pyrogram = types.ModuleType("pyrogram")
    pyrogram.Client = object
    pyrogram.filters = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)

    pyrogram_errors = types.ModuleType("pyrogram.errors")
    pyrogram_errors.FloodWait = type("FloodWait", (Exception,), {})
    pyrogram_errors.UserNotParticipant = type("UserNotParticipant", (Exception,), {})
    monkeypatch.setitem(sys.modules, "pyrogram.errors", pyrogram_errors)

    config = types.ModuleType("config")
    config.API_ID = 1
    config.API_HASH = "h"
    config.STRING = None
    monkeypatch.setitem(sys.modules, "config", config)

    shared_client = types.ModuleType("shared_client")
    shared_client._WORKDIR = "/tmp/fetch-test"
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    encrypt = types.ModuleType("utils.encrypt")
    encrypt.dcs = lambda value: value
    monkeypatch.setitem(sys.modules, "utils.encrypt", encrypt)

    utils = types.ModuleType("utils")
    utils.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils)

    func = types.ModuleType("utils.func")
    func.get_user_data = lambda uid: None
    func.get_user_data_key = lambda uid, key: None
    func.cred_epoch = lambda uid: 0
    monkeypatch.setitem(sys.modules, "utils.func", func)

    plugins = types.ModuleType("plugins")
    plugins.__path__ = [str(SRC / "plugins")]
    monkeypatch.setitem(sys.modules, "plugins", plugins)

    sys.modules.pop("plugins.fetch", None)
    module = importlib.import_module("plugins.fetch")
    module.fetch_origin.clear()
    yield module
    sys.modules.pop("plugins.fetch", None)


def _run(coro):
    return asyncio.run(coro)


def test_public_single_media_prefers_bot_client(fetch_module):
    bot = _FakeClient("bot", {5: _video_msg(500 * 1024 * 1024)})
    user = _FakeClient("user", {5: _video_msg(500 * 1024 * 1024)})

    msg = _run(fetch_module.get_msg(bot, user, "somechannel", 5, "public", 42))

    assert msg is bot.messages[5]
    assert bot.calls and not user.calls
    # bot fetch => fetch_origin False => downstream picks direct send
    assert fetch_module.fetch_origin[(42, "somechannel")] is False
    assert fetch_module.fetch_origin[(42, -100123)] is False


def test_public_oversized_media_keeps_user_client(fetch_module):
    bot = _FakeClient("bot", {5: _video_msg(3 * _GIB)})
    user = _FakeClient("user", {5: _video_msg(3 * _GIB)})

    msg = _run(fetch_module.get_msg(bot, user, "somechannel", 5, "public", 42))

    assert msg is user.messages[5]
    assert bot.calls and user.calls  # bot tried first, result skipped
    assert fetch_module.fetch_origin[(42, -100123)] is True  # download path


def test_public_album_keeps_user_client(fetch_module):
    bot = _FakeClient("bot", {5: _video_msg(100, group_id="g1")})
    user = _FakeClient("user", {5: _video_msg(100, group_id="g1")})

    msg = _run(fetch_module.get_msg(bot, user, "somechannel", 5, "public", 42))

    assert msg is user.messages[5]
    assert fetch_module.fetch_origin[(42, -100123)] is True


def test_public_bot_failure_falls_back_to_user(fetch_module):
    bot = _FakeClient("bot", fail=True)
    user = _FakeClient("user", {7: _video_msg(10)})

    msg = _run(fetch_module.get_msg(bot, user, "somechannel", 7, "public", 42))

    assert msg is user.messages[7]
    assert fetch_module.fetch_origin[(42, -100123)] is True


def test_public_without_user_client_bot_fetches_everything(fetch_module):
    bot = _FakeClient("bot", {5: _video_msg(3 * _GIB, group_id="g1")})

    msg = _run(fetch_module.get_msg(bot, None, "somechannel", 5, "public", 42))

    assert msg is bot.messages[5]
    # bot is the only client: direct-send attempt happens (same as before)
    assert fetch_module.fetch_origin[(42, -100123)] is False


def test_needs_user_fetch_boundaries(fetch_module):
    assert fetch_module._needs_user_fetch(_video_msg(2 * _GIB)) is False
    assert fetch_module._needs_user_fetch(_video_msg(2 * _GIB + 1)) is True
    assert fetch_module._needs_user_fetch(_video_msg(1, group_id="a")) is True
    # photo-only messages are small by construction -> bot can serve
    photo_only = SimpleNamespace(
        photo=SimpleNamespace(file_size=100), media_group_id=None
    )
    assert fetch_module._needs_user_fetch(photo_only) is False
