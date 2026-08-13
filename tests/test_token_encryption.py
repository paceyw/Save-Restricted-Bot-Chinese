import base64
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("MASTER_KEY", "phase0-master-key")
os.environ.setdefault("IV_KEY", "phase0-iv-key")

from utils.encrypt import dcs, ecs


SRC = Path(__file__).resolve().parents[1]


class _Filter:
    def __and__(self, other):
        return self

    def __or__(self, other):
        return self

    def __invert__(self):
        return self


class _Filters:
    text = _Filter()
    private = _Filter()

    @staticmethod
    def command(*args, **kwargs):
        return _Filter()


class _FakeApp:
    def on_message(self, *args, **kwargs):
        def decorator(function):
            return function

        return decorator


@pytest.fixture
def batch_module(monkeypatch):
    class FakeClient:
        instances = []

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.stopped = False
            FakeClient.instances.append(self)

        async def start(self):
            return None

        async def stop(self):
            self.stopped = True

    pyrogram = types.ModuleType("pyrogram")
    pyrogram.Client = FakeClient
    pyrogram.filters = _Filters()
    monkeypatch.setitem(sys.modules, "pyrogram", pyrogram)

    pyrogram_types = types.ModuleType("pyrogram.types")
    pyrogram_types.Message = object
    pyrogram_types.InputMediaPhoto = object
    pyrogram_types.InputMediaVideo = object
    pyrogram_types.InputMediaDocument = object
    pyrogram_types.InputMediaAudio = object
    monkeypatch.setitem(sys.modules, "pyrogram.types", pyrogram_types)

    pyrogram_errors = types.ModuleType("pyrogram.errors")
    pyrogram_errors.UserNotParticipant = type("UserNotParticipant", (Exception,), {})
    pyrogram_errors.FloodWait = type("FloodWait", (Exception,), {})
    monkeypatch.setitem(sys.modules, "pyrogram.errors", pyrogram_errors)

    config = types.ModuleType("config")
    config.API_ID = 123
    config.API_HASH = "hash"
    config.LOG_GROUP = 0
    config.STRING = None
    config.FORCE_SUB = 0
    config.FREEMIUM_LIMIT = 1
    config.PREMIUM_LIMIT = 10
    config.BATCH_INTERVAL = 0.01
    config.MERGE_INTERVAL = 0.01
    config.CHANNEL_INTERVAL = 0.01
    config.UPLOAD_INTERVAL = 0.01
    config.MAX_FLOOD_RETRIES = 1
    monkeypatch.setitem(sys.modules, "config", config)

    utils = types.ModuleType("utils")
    utils.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils)

    saved_tokens = []
    func = types.ModuleType("utils.func")
    func.get_user_data = lambda uid: None
    func.screenshot = None
    func.thumbnail = None
    func.get_video_metadata = None
    func.ensure_audio_track = None
    func.get_user_data_key = None
    func.process_text_with_rules = None
    func.is_premium_user = None
    func.E = lambda value: (None, None, None, None)

    async def save_user_bot(user_id, bot_token):
        saved_tokens.append((user_id, bot_token))

    func.save_user_bot = save_user_bot
    func.migrate_user_bot_token = save_user_bot
    monkeypatch.setitem(sys.modules, "utils.func", func)

    custom_filters = types.ModuleType("utils.custom_filters")
    custom_filters.login_in_progress = _Filter()
    monkeypatch.setitem(sys.modules, "utils.custom_filters", custom_filters)

    encrypt = types.ModuleType("utils.encrypt")
    encrypt.dcs = dcs
    monkeypatch.setitem(sys.modules, "utils.encrypt", encrypt)

    shared_client = types.ModuleType("shared_client")
    shared_client.app = _FakeApp()
    shared_client.userbot = None
    shared_client._WORKDIR = "/persistent"
    monkeypatch.setitem(sys.modules, "shared_client", shared_client)

    plugins = types.ModuleType("plugins")
    plugins.__path__ = [str(SRC / "plugins")]
    monkeypatch.setitem(sys.modules, "plugins", plugins)

    settings = types.ModuleType("plugins.settings")
    settings.rename_file = None
    monkeypatch.setitem(sys.modules, "plugins.settings", settings)

    start = types.ModuleType("plugins.start")

    async def subscribe(*args, **kwargs):
        return 0

    start.subscribe = subscribe
    monkeypatch.setitem(sys.modules, "plugins.start", start)

    module_name = "test_token_batch_module"
    spec = importlib.util.spec_from_file_location(module_name, SRC / "plugins" / "batch.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module, FakeClient, saved_tokens


def test_get_ubot_decrypts_stored_token_before_client_creation(batch_module):
    module, fake_client, saved_tokens = batch_module
    module.UB.clear()
    encrypted = ecs(" 123456:token-value ")

    async def get_key(uid, key, default=None):
        assert (uid, key) == (42, "bot_token")
        return encrypted

    module.get_user_data_key = get_key

    bot = asyncio.run(module.get_ubot(42))

    assert bot is fake_client.instances[-1]
    assert bot.kwargs["bot_token"] == "123456:token-value"
    assert saved_tokens == []


def test_get_ubot_migrates_plaintext_token_and_uses_it(batch_module):
    module, fake_client, saved_tokens = batch_module
    module.UB.clear()
    plaintext = "123456:token-value-abcdefghijkl"

    async def get_key(uid, key, default=None):
        assert (uid, key) == (42, "bot_token")
        return plaintext

    module.get_user_data_key = get_key

    bot = asyncio.run(module.get_ubot(42))

    assert bot is fake_client.instances[-1]
    assert bot.kwargs["bot_token"] == plaintext
    assert saved_tokens == [(42, plaintext)]


def test_get_ubot_rejects_corrupted_ciphertext_without_migration(batch_module):
    module, fake_client, saved_tokens = batch_module
    module.UB.clear()
    encoded = bytearray(base64.b64decode(ecs("123456:token-value-abcdefghijkl")))
    encoded[-1] ^= 1
    corrupted = base64.b64encode(encoded).decode()

    async def get_key(uid, key, default=None):
        return corrupted

    module.get_user_data_key = get_key

    assert asyncio.run(module.get_ubot(42)) is None
    assert not fake_client.instances
    assert saved_tokens == []


@pytest.fixture
def real_func_module(monkeypatch):
    config = types.ModuleType("config")
    config.MONGO_DB = "mongodb://unused"
    config.DB_NAME = "test"
    monkeypatch.setitem(sys.modules, "config", config)

    monkeypatch.setitem(sys.modules, "cv2", types.ModuleType("cv2"))

    class FakeDatabase:
        def __getitem__(self, name):
            return object()

    class FakeMotorClient:
        def __init__(self, *args, **kwargs):
            pass

        def __getitem__(self, name):
            return FakeDatabase()

    motor = types.ModuleType("motor")
    motor.__path__ = []
    motor_asyncio = types.ModuleType("motor.motor_asyncio")
    motor_asyncio.AsyncIOMotorClient = FakeMotorClient
    monkeypatch.setitem(sys.modules, "motor", motor)
    monkeypatch.setitem(sys.modules, "motor.motor_asyncio", motor_asyncio)

    module_name = "test_real_func_module"
    spec = importlib.util.spec_from_file_location(module_name, SRC / "utils" / "func.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_save_user_bot_encrypts_token_before_persistence(real_func_module):
    class UpdateResult:
        matched_count = 1

    class Collection:
        def __init__(self):
            self.calls = []

        async def update_one(self, query, update, upsert=False):
            self.calls.append((query, update, upsert))
            return UpdateResult()

    collection = Collection()
    real_func_module.users_collection = collection
    plaintext = "123456:token-value-abcdefghijkl"

    assert asyncio.run(real_func_module.save_user_bot(42, plaintext))

    query, update, upsert = collection.calls[0]
    stored = update["$set"]["bot_token"]
    assert query == {"user_id": 42}
    assert upsert is True
    assert stored != plaintext
    assert dcs(stored) == plaintext


def test_migrate_user_bot_token_does_not_overwrite_competing_update(real_func_module):
    class UpdateResult:
        def __init__(self, matched_count):
            self.matched_count = matched_count

    class Collection:
        def __init__(self, current):
            self.current = current
            self.calls = []

        async def update_one(self, query, update, upsert=False):
            self.calls.append((query, update, upsert))
            if query["bot_token"] != self.current:
                return UpdateResult(0)
            self.current = update["$set"]["bot_token"]
            return UpdateResult(1)

    expected_plaintext = "123456:old-token-abcdefghijkl"
    competing_plaintext = "123456:new-token-abcdefghijkl"
    competing_ciphertext = ecs(competing_plaintext)
    collection = Collection(competing_ciphertext)
    real_func_module.users_collection = collection

    assert not asyncio.run(
        real_func_module.migrate_user_bot_token(42, expected_plaintext)
    )
    assert collection.current == competing_ciphertext
    assert collection.calls[0][0] == {
        "user_id": 42,
        "bot_token": expected_plaintext,
    }
