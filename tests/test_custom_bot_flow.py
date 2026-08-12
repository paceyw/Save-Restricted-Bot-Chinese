import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


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


class _FakeReply:
    def __init__(self, text):
        self.text = text
        self.edits = []

    async def edit(self, text):
        self.edits.append(text)


class _FakeMessage:
    def __init__(self, command):
        self.command = [command]
        self.from_user = types.SimpleNamespace(id=42)
        self.replies = []

    async def reply_text(self, text):
        reply = _FakeReply(text)
        self.replies.append(reply)
        return reply


@pytest.fixture
def batch_module(monkeypatch):
    pyrogram = types.ModuleType("pyrogram")

    class FakeClient:
        instances = []
        fail_start = False

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.stopped = False
            FakeClient.instances.append(self)

        async def start(self):
            if self.fail_start:
                raise RuntimeError("start failed")

        async def stop(self):
            self.stopped = True

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
    monkeypatch.setitem(sys.modules, "config", config)

    utils = types.ModuleType("utils")
    utils.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils)
    func = types.ModuleType("utils.func")
    func.get_user_data = lambda uid: None
    func.screenshot = None
    func.thumbnail = None
    func.get_video_metadata = None
    func.get_user_data_key = None
    func.process_text_with_rules = None
    func.is_premium_user = None

    import re as _re

    def _E(L):
        private_match = _re.match(r'https://t\.me/c/(\d+)/(?:\d+/)?(\d+)', L)
        public_match = _re.match(r'https://t\.me/([^/]+)/(?:\d+/)?(\d+)', L)
        if private_match:
            return f'-100{private_match.group(1)}', int(private_match.group(2)), 'private'
        if public_match:
            return public_match.group(1), int(public_match.group(2)), 'public'
        return None, None, None

    func.E = _E
    monkeypatch.setitem(sys.modules, "utils.func", func)

    custom_filters = types.ModuleType("utils.custom_filters")
    custom_filters.login_in_progress = _Filter()
    monkeypatch.setitem(sys.modules, "utils.custom_filters", custom_filters)

    encrypt = types.ModuleType("utils.encrypt")
    encrypt.dcs = lambda value: value
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

    module_name = "test_batch_module"
    spec = importlib.util.spec_from_file_location(module_name, SRC / "plugins" / "batch.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module, FakeClient


def test_get_ubot_uses_persistent_workdir_and_saved_token(batch_module):
    module, fake_client = batch_module
    module.UB.clear()

    async def get_key(uid, key, default=None):
        assert (uid, key) == (42, "bot_token")
        return " 123456:token-value "

    module.get_user_data_key = get_key

    bot = asyncio.run(module.get_ubot(42))

    assert bot is fake_client.instances[-1]
    assert bot.kwargs["bot_token"] == "123456:token-value"
    assert bot.kwargs["workdir"] == "/persistent"
    assert module.UB[42] is bot


def test_single_reports_start_failure_as_start_failure_not_missing_token(batch_module):
    module, _ = batch_module
    module.UB.clear()

    async def get_key(uid, key, default=None):
        return "123456:token-value"

    async def no_bot(uid):
        return None

    module.get_user_data_key = get_key
    module.get_ubot = no_bot
    module.is_premium_user = lambda uid: asyncio.sleep(0, result=True)
    message = _FakeMessage("single")

    asyncio.run(module.process_cmd(None, message))

    assert message.replies[0].edits == [
        "已保存机器人令牌，但机器人启动失败。请检查令牌后重新使用 /setbot。"
    ]

def test_get_msg_public_falls_back_without_emp_keyerror(batch_module):
    module, _ = batch_module
    module.emp.clear()

    class UserClient:
        async def get_messages(self, chat, message_id):
            raise RuntimeError("user session unavailable")

    class BotClient:
        async def get_messages(self, chat, message_id):
            return types.SimpleNamespace(empty=False, media=False)

    message = asyncio.run(
        module.get_msg(BotClient(), UserClient(), "public_channel", 7, "public")
    )

    assert message is not None
    assert module.emp["public_channel"] is False


def test_get_msg_public_uses_bot_directly_without_user_client(batch_module):
    module, _ = batch_module
    module.emp.clear()

    class BotClient:
        async def get_messages(self, chat, message_id):
            return types.SimpleNamespace(empty=False, media=False)

    message = asyncio.run(
        module.get_msg(BotClient(), None, "public_channel", 7, "public")
    )

    assert message is not None
    assert module.emp["public_channel"] is False

def test_get_msg_public_marks_user_source_for_download(batch_module):
    module, _ = batch_module
    module.emp.clear()

    class UserClient:
        async def get_messages(self, chat, message_id):
            return types.SimpleNamespace(empty=False, media=False)

    message = asyncio.run(
        module.get_msg(None, UserClient(), "public_channel", 7, "public")
    )

    assert message is not None
    assert module.emp["public_channel"] is True


def test_process_msg_does_not_report_direct_send_success_on_error(batch_module):
    module, _ = batch_module
    module.emp["public_channel"] = False

    async def get_key(user_id, key, default=None):
        return default

    async def process_text(user_id, text):
        return text

    class FailingBot:
        async def send_photo(self, *args, **kwargs):
            raise RuntimeError("PEER_ID_INVALID")

    message = types.SimpleNamespace(
        media=True,
        caption=None,
        video=None,
        video_note=None,
        voice=None,
        sticker=None,
        audio=None,
        document=None,
        photo=types.SimpleNamespace(file_id="photo-id"),
    )
    module.get_user_data_key = get_key
    module.process_text_with_rules = process_text

    result = asyncio.run(
        module.process_msg(
            FailingBot(),
            None,
            message,
            "42",
            "public",
            42,
            "public_channel",
        )
    )

    assert result.startswith("发送失败：")
    assert "Sent directly" not in result


def test_resolve_delivery_prefers_settings_chat_id(batch_module):
    module, _ = batch_module

    async def get_key(uid, key, default=None):
        return "-100111" if key == "chat_id" else default

    module.get_user_data_key = get_key
    module.LOG_GROUP = -100999

    tcid, rtmid, via_bot = asyncio.run(module.resolve_delivery("42"))

    assert tcid == -100111
    assert rtmid is None
    assert via_bot is True


def test_resolve_delivery_falls_back_to_log_group(batch_module):
    module, _ = batch_module

    async def get_key(uid, key, default=None):
        return default

    module.get_user_data_key = get_key
    module.LOG_GROUP = -100999

    tcid, rtmid, via_bot = asyncio.run(module.resolve_delivery("42"))

    assert tcid == -100999
    assert via_bot is True


def test_resolve_delivery_defaults_to_user_chat(batch_module):
    module, _ = batch_module

    async def get_key(uid, key, default=None):
        return default

    module.get_user_data_key = get_key
    module.LOG_GROUP = 0

    tcid, rtmid, via_bot = asyncio.run(module.resolve_delivery("42"))

    assert tcid == 42
    assert isinstance(tcid, int)
    assert via_bot is False


def test_parse_link_lines_single_line_is_range(batch_module):
    module, _ = batch_module

    mode, payload = module.parse_link_lines('https://t.me/fancha103/7823?single')

    assert mode == 'range'
    assert payload == ('fancha103', 7823, 'public')


def test_parse_link_lines_multi_lines(batch_module):
    module, _ = batch_module

    mode, payload = module.parse_link_lines(
        'https://t.me/fancha103/7823\n\n  https://t.me/c/1234567/88 \nhttps://t.me/other/9?single\n'
    )

    assert mode == 'multi'
    assert payload == [
        ('fancha103', 7823, 'public'),
        ('-1001234567', 88, 'private'),
        ('other', 9, 'public'),
    ]


def test_parse_link_lines_reports_bad_line_number(batch_module):
    module, _ = batch_module

    mode, payload = module.parse_link_lines('https://t.me/fancha103/7823\nnot-a-link\nhttps://t.me/other/9')

    assert mode == 'invalid'
    assert payload[0] == 2
    assert 'not-a-link' in payload[1]


def test_parse_link_lines_rejects_empty_text(batch_module):
    module, _ = batch_module

    mode, _ = module.parse_link_lines('   \n  \n')

    assert mode == 'invalid'
