import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


SRC = Path(__file__).resolve().parents[1]


@pytest.fixture
def func_module(monkeypatch):
    config = types.ModuleType("config")
    config.MONGO_DB = "mongodb://unused"
    config.DB_NAME = "test"
    monkeypatch.setitem(sys.modules, "config", config)

    motor = types.ModuleType("motor")
    motor_asyncio = types.ModuleType("motor.motor_asyncio")

    class FakeMotorClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getitem__(self, _name):
            return self

        def __getattr__(self, _name):
            return self

    motor_asyncio.AsyncIOMotorClient = FakeMotorClient
    motor.motor_asyncio = motor_asyncio
    monkeypatch.setitem(sys.modules, "motor", motor)
    monkeypatch.setitem(sys.modules, "motor.motor_asyncio", motor_asyncio)

    encrypt = types.ModuleType("utils.encrypt")
    encrypt.ecs = lambda value: value
    monkeypatch.setitem(sys.modules, "utils.encrypt", encrypt)

    module_name = "phase4_settings_func"
    spec = importlib.util.spec_from_file_location(module_name, SRC / "utils" / "func.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_apply_text_rules_replaces_deletes_and_handles_empty_text(func_module):
    assert func_module.apply_text_rules(
        "hello world world", {"world": "there"}, ["hello"]
    ) == "there there"
    assert func_module.apply_text_rules("", {"hello": "bye"}, ["hello"]) == ""
    assert func_module.apply_text_rules(None, {}, []) == ""


def test_get_user_settings_merges_defaults_without_sharing_mutable_values(func_module):
    collection = types.SimpleNamespace(find_one=AsyncMock(side_effect=[
        {
            "user_id": 42,
            "caption": "saved caption",
            "replacement_words": {"old": "new"},
        },
        {
            "user_id": 7,
            "caption": "saved caption",
            "replacement_words": {"old": "new"},
        },
        None,
    ]))
    func_module.users_collection = collection

    settings = asyncio.run(func_module.get_user_settings("42"))
    assert collection.find_one.await_args_list[0].args == ({"user_id": 42},)
    assert settings["caption"] == "saved caption"
    assert settings["chat_id"] is None
    assert settings["replacement_words"] == {"old": "new"}
    assert settings["delete_words"] == []

    settings["delete_words"].append("local-only")
    settings["replacement_words"]["local"] = "only"
    fresh = asyncio.run(func_module.get_user_settings(7))
    assert fresh["delete_words"] == []
    assert fresh["replacement_words"] == {"old": "new"}

    missing = asyncio.run(func_module.get_user_settings("9"))
    assert missing == func_module.SETTINGS_DEFAULTS
    assert missing is not func_module.SETTINGS_DEFAULTS
    assert missing["delete_words"] is not func_module.SETTINGS_DEFAULTS["delete_words"]


def test_get_user_settings_filters_undeclared_document_fields(func_module):
    """Security/memory: session_string, _id and bookkeeping fields must not be
    carried into the per-task snapshot retained in TASKS history."""
    collection = types.SimpleNamespace(find_one=AsyncMock(return_value={
        "user_id": 42,
        "caption": "cap",
        "session_string": "encrypted-secret",
        "_id": "mongo-object-id",
        "updated_at": "2026-08-13",
        "unknown_field": "x",
    }))
    func_module.users_collection = collection

    settings = asyncio.run(func_module.get_user_settings(42))

    assert set(settings) == set(func_module.SETTINGS_DEFAULTS)
    assert settings["caption"] == "cap"
    assert "session_string" not in settings
    assert "_id" not in settings


def test_credential_mutations_bump_epoch(func_module):
    collection = types.SimpleNamespace(
        update_one=AsyncMock(return_value=types.SimpleNamespace(matched_count=1))
    )
    func_module.users_collection = collection

    base = func_module.cred_epoch(42)
    assert asyncio.run(func_module.save_user_bot(42, 'tok')) is True
    assert asyncio.run(func_module.save_user_session(42, 'sess')) is True
    assert asyncio.run(func_module.remove_user_bot(42)) is True
    assert asyncio.run(func_module.remove_user_session(42)) is True
    assert asyncio.run(func_module.migrate_user_bot_token(42, 'plain')) is True
    assert func_module.cred_epoch(42) == base + 5
    # Other users are unaffected.
    assert func_module.cred_epoch(7) == 0


def test_generic_setter_bumps_epoch_for_credential_keys(func_module):
    """handle_addsession writes session_string through save_user_data; the
    generic setter must still invalidate dispatch-time prefetches."""
    collection = types.SimpleNamespace(update_one=AsyncMock())
    func_module.users_collection = collection

    base = func_module.cred_epoch(42)
    asyncio.run(func_module.save_user_data(42, 'session_string', 'enc'))
    asyncio.run(func_module.save_user_data(42, 'bot_token', 'enc'))
    assert func_module.cred_epoch(42) == base + 2
    # Non-credential settings writes do not bump (snapshot semantics cover them).
    asyncio.run(func_module.save_user_data(42, 'caption', 'x'))
    assert func_module.cred_epoch(42) == base + 2


def test_prune_cred_epochs_drops_inactive_users(func_module):
    func_module.bump_cred_epoch(1)
    func_module.bump_cred_epoch(2)
    func_module.prune_cred_epochs({2})
    assert func_module.cred_epoch(1) == 0
    assert func_module.cred_epoch(2) == 1
