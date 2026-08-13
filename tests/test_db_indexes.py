import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest


class OperationFailure(Exception):
    pass


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
            return FakeDatabase()

    class FakeDatabase:
        def __getitem__(self, _name):
            return object()

    motor_asyncio.AsyncIOMotorClient = FakeMotorClient
    motor.motor_asyncio = motor_asyncio
    monkeypatch.setitem(sys.modules, "motor", motor)
    monkeypatch.setitem(sys.modules, "motor.motor_asyncio", motor_asyncio)

    encrypt = types.ModuleType("utils.encrypt")
    encrypt.ecs = lambda value: value
    monkeypatch.setitem(sys.modules, "utils.encrypt", encrypt)

    module_name = "phase4_db_indexes_func"
    spec = importlib.util.spec_from_file_location(module_name, SRC / "utils" / "func.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _collections():
    users = MagicMock()
    premium = MagicMock()
    users.create_index = AsyncMock()
    premium.create_index = AsyncMock()
    return users, premium


def test_init_db_indexes_creates_all_indexes(func_module, monkeypatch):
    users, premium = _collections()
    monkeypatch.setattr(func_module, "users_collection", users)
    monkeypatch.setattr(func_module, "premium_users_collection", premium)

    asyncio.run(func_module.init_db_indexes())

    assert users.create_index.await_args_list == [call("user_id", unique=True)]
    assert premium.create_index.await_args_list == [
        call("user_id", unique=True),
        call("expireAt", expireAfterSeconds=0),
    ]
    assert users.create_index.await_count + premium.create_index.await_count == 3


def test_init_db_indexes_continues_after_operation_failure(func_module, monkeypatch):
    users, premium = _collections()
    premium.create_index.side_effect = [OperationFailure("duplicate key", 11000), None]
    monkeypatch.setattr(func_module, "users_collection", users)
    monkeypatch.setattr(func_module, "premium_users_collection", premium)

    asyncio.run(func_module.init_db_indexes())

    assert users.create_index.await_args_list == [call("user_id", unique=True)]
    assert premium.create_index.await_args_list == [
        call("user_id", unique=True),
        call("expireAt", expireAfterSeconds=0),
    ]


def test_init_db_indexes_is_safe_to_call_repeatedly(func_module, monkeypatch):
    users, premium = _collections()
    monkeypatch.setattr(func_module, "users_collection", users)
    monkeypatch.setattr(func_module, "premium_users_collection", premium)

    asyncio.run(func_module.init_db_indexes())
    asyncio.run(func_module.init_db_indexes())

    assert users.create_index.await_count == 2
    assert premium.create_index.await_count == 4


def test_add_premium_user_does_not_create_indexes(func_module, monkeypatch):
    premium = MagicMock()
    premium.update_one = AsyncMock()
    premium.create_index = AsyncMock()
    monkeypatch.setattr(func_module, "premium_users_collection", premium)

    ok, _expiry = asyncio.run(func_module.add_premium_user(42, 1, "days"))

    assert ok is True
    premium.update_one.assert_awaited_once()
    premium.create_index.assert_not_called()
