import asyncio
import re as _re
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

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

def _settings(**overrides):
    result = {
        "caption": "",
        "chat_id": None,
        "replacement_words": {},
        "delete_words": [],
        "rename_tag": "",
        "bot_token": None,
    }
    result.update(overrides)
    return result

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
    config.BATCH_INTERVAL = 0.01
    config.MERGE_INTERVAL = 0.01
    config.CHANNEL_INTERVAL = 0.01
    config.UPLOAD_INTERVAL = 0.01
    config.MAX_FLOOD_RETRIES = 1
    monkeypatch.setitem(sys.modules, "config", config)

    utils = types.ModuleType("utils")
    utils.__path__ = [str(SRC / "utils")]
    monkeypatch.setitem(sys.modules, "utils", utils)
    func = types.ModuleType("utils.func")
    func.get_user_data = lambda uid: None
    func.screenshot = None
    func.thumbnail = None
    func.get_video_metadata = None
    func.touch_file = lambda *_a, **_k: None
    func.ensure_audio_track = None
    func.VIDEO_EXTENSIONS = set()
    func.AUDIO_EXTENSIONS = set()
    func.get_user_data_key = None
    func.process_text_with_rules = None
    func.is_premium_user = None
    async def get_user_settings(_uid):
        return {
            "caption": "",
            "chat_id": None,
            "replacement_words": {},
            "delete_words": [],
            "rename_tag": "",
            "bot_token": None,
        }

    func.get_user_settings = get_user_settings

    def filter_settings(doc):
        result = {
            "caption": "",
            "chat_id": None,
            "replacement_words": {},
            "delete_words": [],
            "rename_tag": "",
            "bot_token": None,
        }
        for key in result:
            if doc and key in doc:
                result[key] = doc[key]
        return result

    func.filter_settings = filter_settings
    func.cred_epoch = lambda _uid: 0
    func.prune_cred_epochs = lambda _active: None
    func.apply_text_rules = lambda text, _replacements, _delete_words: text

    def _parse_link(L):
        private_match = _re.match(r'https://t\.me/c/(\d+)/(?:\d+/)?(\d+)', L)
        public_match = _re.match(r'https://t\.me/([^/]+)/(?:\d+/)?(\d+)', L)
        comment_match = _re.search(r'[?&]comment=(\d+)', L)
        comment_id = int(comment_match.group(1)) if comment_match else None
        if private_match:
            return f'-100{private_match.group(1)}', int(private_match.group(2)), 'private', comment_id
        if public_match:
            return public_match.group(1), int(public_match.group(2)), 'public', comment_id
        return None, None, None, None

    func.parse_link = _parse_link
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

    for name in ("plugins.fetch", "plugins.tasks", "plugins.deliver", "plugins.batch"):
        sys.modules.pop(name, None)
    import importlib
    fetch_module = importlib.import_module("plugins.fetch")
    tasks_module = importlib.import_module("plugins.tasks")
    deliver_module = importlib.import_module("plugins.deliver")
    batch_module = importlib.import_module("plugins.batch")

    for worker in list(tasks_module.USER_WORKERS.values()):
        worker.cancel()
    fetch_module.user_bots.clear()
    fetch_module.user_clients.clear()
    fetch_module.fetch_origin.clear()
    fetch_module._CLIENT_LAST_USED.clear()
    fetch_module._PEER_CACHE.clear()
    fetch_module._LINKED_CHAT.clear()
    fetch_module._UB_EPOCH.clear()
    fetch_module._UC_EPOCH.clear()
    fetch_module._UB_UC_LOCKS.clear()
    deliver_module.progress_state.clear()
    batch_module.pending_flows.clear()
    batch_module._Z_TS.clear()
    tasks_module.TASKS.clear()
    tasks_module.USER_QUEUES.clear()
    tasks_module.USER_WORKERS.clear()

    class ModuleBundle:
        def __init__(self):
            object.__setattr__(self, "_modules", (
                fetch_module, tasks_module, deliver_module, batch_module
            ))

        def _owner(self, name):
            for candidate in self._modules:
                if hasattr(candidate, name):
                    return candidate
            return deliver_module

        def __getattr__(self, name):
            return getattr(self._owner(name), name)

        def __setattr__(self, name, value):
            if name == "_modules":
                object.__setattr__(self, name, value)
                return
            shared = {
                "get_user_data", "get_user_data_key", "get_ubot", "get_uclient",
                "get_msg", "resolve_linked_chat", "cred_epoch", "process_msg",
                "process_merged", "process_album", "process_one_link",
                "_ok", "_flood_secs",
            }
            if name in shared:
                for candidate in self._modules:
                    if hasattr(candidate, name):
                        setattr(candidate, name, value)
                return
            setattr(self._owner(name), name, value)

    return ModuleBundle(), FakeClient


def test_get_ubot_uses_persistent_workdir_and_saved_token(batch_module):
    module, fake_client = batch_module
    module.user_bots.clear()

    async def get_key(uid, key, default=None):
        assert (uid, key) == (42, "bot_token")
        return " 123456:token-value "

    module.get_user_data_key = get_key

    bot = asyncio.run(module.get_ubot(42))

    assert bot is fake_client.instances[-1]
    assert bot.kwargs["bot_token"] == "123456:token-value"
    assert bot.kwargs["workdir"] == "/persistent"
    assert module.user_bots[42] is bot


def test_single_reports_start_failure_as_start_failure_not_missing_token(batch_module):
    module, _ = batch_module
    module.user_bots.clear()

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
    module.fetch_origin.clear()

    class UserClient:
        async def get_messages(self, chat, message_id):
            raise RuntimeError("user session unavailable")

    class BotClient:
        async def get_messages(self, chat, message_id):
            return types.SimpleNamespace(empty=False, media=False)

    message = asyncio.run(
        module.get_msg(BotClient(), UserClient(), "public_channel", 7, "public", 42)
    )

    assert message is not None
    assert module.fetch_origin[(42, "public_channel")] is False


def test_get_msg_public_uses_bot_directly_without_user_client(batch_module):
    module, _ = batch_module
    module.fetch_origin.clear()

    class BotClient:
        async def get_messages(self, chat, message_id):
            return types.SimpleNamespace(empty=False, media=False)

    message = asyncio.run(
        module.get_msg(BotClient(), None, "public_channel", 7, "public", 42)
    )

    assert message is not None
    assert module.fetch_origin[(42, "public_channel")] is False

def test_get_msg_public_marks_user_source_for_download(batch_module):
    module, _ = batch_module
    module.fetch_origin.clear()

    class UserClient:
        async def get_messages(self, chat, message_id):
            return types.SimpleNamespace(empty=False, media=False)

    message = asyncio.run(
        module.get_msg(None, UserClient(), "public_channel", 7, "public", 42)
    )

    assert message is not None
    assert module.fetch_origin[(42, "public_channel")] is True


def test_process_msg_does_not_report_direct_send_success_on_error(batch_module):
    module, _ = batch_module
    module.fetch_origin[(42, "public_channel")] = False

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
            settings=_settings(),
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

    tcid, rtmid, via_bot = asyncio.run(module.resolve_delivery("42", _settings(chat_id="-100111")))

    assert tcid == -100111
    assert rtmid is None
    assert via_bot is True


def test_resolve_delivery_falls_back_to_log_group(batch_module):
    module, _ = batch_module

    async def get_key(uid, key, default=None):
        return default

    module.get_user_data_key = get_key
    module.LOG_GROUP = -100999

    tcid, rtmid, via_bot = asyncio.run(module.resolve_delivery("42", _settings()))

    assert tcid == -100999
    assert via_bot is True


def test_resolve_delivery_defaults_to_user_chat(batch_module):
    module, _ = batch_module

    async def get_key(uid, key, default=None):
        return default

    module.get_user_data_key = get_key
    module.LOG_GROUP = 0

    tcid, rtmid, via_bot = asyncio.run(module.resolve_delivery("42", _settings()))

    assert tcid == 42
    assert isinstance(tcid, int)
    assert via_bot is False


def test_parse_link_lines_single_line_is_range(batch_module):
    module, _ = batch_module

    mode, payload = module.parse_link_lines('https://t.me/fancha103/7823?single')

    assert mode == 'range'
    assert payload == ('fancha103', 7823, 'public', None)


def test_parse_link_lines_multi_lines(batch_module):
    module, _ = batch_module

    mode, payload = module.parse_link_lines(
        'https://t.me/fancha103/7823\n\n  https://t.me/c/1234567/88 \nhttps://t.me/other/9?single\n'
    )

    assert mode == 'multi'
    assert payload == [
        ('fancha103', 7823, 'public', None),
        ('-1001234567', 88, 'private', None),
        ('other', 9, 'public', None),
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


def _stub_progress_app(module):
    class FakeX:
        async def send_message(self, did, text):
            return types.SimpleNamespace(id=1)

        async def edit_message_text(self, *args, **kwargs):
            pass

        async def delete_messages(self, *args, **kwargs):
            pass

    async def get_key(user_id, key, default=None):
        return default

    async def process_text(user_id, text):
        return text

    module.get_user_data_key = get_key
    module.process_text_with_rules = process_text
    module.main_bot = FakeX()
    module.LOG_GROUP = 0


def test_process_msg_cleans_downloaded_file_on_processing_error(batch_module, tmp_path):
    module, _ = batch_module
    module.fetch_origin[(42, "public_channel")] = True
    _stub_progress_app(module)
    # fixture's plugins.settings.rename_file is None -> the rename step raises,
    # exercising the mid-processing cleanup path.

    downloaded = tmp_path / "42_doc.bin"

    class UserClient:
        async def download_media(self, m, file_name=None, progress=None, progress_args=None):
            downloaded.write_bytes(b"data")
            return str(downloaded)

    message = types.SimpleNamespace(
        media=True, caption=None, video=None, video_note=None, voice=None,
        sticker=None, audio=None, photo=None,
        document=types.SimpleNamespace(file_name="doc.bin"),
    )

    result = asyncio.run(
        module.process_msg(
            UserClient(), UserClient(), message, "42", "public", 42,
            "public_channel", settings=_settings()
        )
    )

    assert result.startswith("Error:")
    assert not downloaded.exists()


def test_process_msg_sends_sticker_from_downloaded_file(batch_module, tmp_path):
    module, _ = batch_module
    module.fetch_origin[(42, "public_channel")] = True
    _stub_progress_app(module)
    module.thumbnail = lambda d: None

    downloaded = tmp_path / "42_sticker"
    sent = {}

    class UserClient:
        async def download_media(self, m, file_name=None, progress=None, progress_args=None):
            downloaded.write_bytes(b"webp")
            return str(downloaded)

        async def send_sticker(self, tcid, sticker, reply_to_message_id=None):
            sent["sticker"] = sticker

    message = types.SimpleNamespace(
        media=True, caption=None, video=None, video_note=None, voice=None,
        sticker=types.SimpleNamespace(file_id="sticker-file-id"),
        audio=None, photo=None, document=None,
    )

    result = asyncio.run(
        module.process_msg(
            UserClient(), UserClient(), message, "42", "public", 42,
            "public_channel", settings=_settings()
        )
    )

    assert result == "Done."
    assert sent["sticker"] == str(downloaded)
    assert not downloaded.exists()

# ---------------------------------------------------------------------------
# /merge — process_merged + _download_media_item


def _stub_input_media(module):
    """InputMedia constructors returning mutable namespaces (caption settable)."""
    module.InputMediaPhoto = lambda f: types.SimpleNamespace(kind='photo', media=f, caption=None)
    module.InputMediaVideo = lambda f, **kw: types.SimpleNamespace(kind='video', media=f, caption=None)
    module.InputMediaAudio = lambda f, **kw: types.SimpleNamespace(kind='audio', media=f, caption=None)
    module.InputMediaDocument = lambda f: types.SimpleNamespace(kind='doc', media=f, caption=None)


async def _identity_ensure_audio(fp):
    return fp


def _photo_msg(caption=None):
    return types.SimpleNamespace(
        media=True,
        photo=types.SimpleNamespace(),
        video=None, audio=None, document=None,
        caption=types.SimpleNamespace(markdown=caption) if caption else None,
    )


def _text_msg(text):
    return types.SimpleNamespace(
        media=None,
        photo=None, video=None, audio=None, document=None,
        text=types.SimpleNamespace(markdown=text),
        caption=None,
    )


class _MergeClient:
    """Fake client backing both the download (u) and delivery (sender) roles."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self._n = 0
        self.groups = []
        self.messages = []

    async def download_media(self, m, file_name=None, progress=None, progress_args=None):
        self._n += 1
        p = self.tmp_path / f"f_{self._n}"
        p.write_bytes(b"x")
        return str(p)

    async def send_media_group(self, tcid, media, reply_to_message_id=None):
        self.groups.append(list(media))

    async def send_message(self, tcid, text=None, reply_to_message_id=None):
        self.messages.append(text)


def test_download_media_item_skips_non_media(batch_module):
    module, _ = batch_module

    class Client:
        async def download_media(self, *a, **k):
            raise AssertionError("should not download a media-less message")

    msg = types.SimpleNamespace(photo=None, video=None, document=None, audio=None)
    im, files = asyncio.run(
        module._download_media_item(Client(), msg, 42, 0, 'merge', None, 42, 1, 0)
    )
    assert im is None
    assert files == []


def test_process_merged_combines_text_only(batch_module):
    module, _ = batch_module
    _stub_progress_app(module)

    sent = []

    class Client:
        async def send_message(self, tcid, text=None, reply_to_message_id=None):
            sent.append(text)

    msgs = [_text_msg('Hello'), _text_msg('World')]
    result = asyncio.run(module.process_merged(Client(), Client(), msgs, "42", 42, settings=_settings()))

    assert result == '✅ 文字已合并发送'
    assert sent == ['Hello\n\nWorld']


def test_process_merged_no_content(batch_module):
    module, _ = batch_module
    _stub_progress_app(module)

    class Client:
        async def send_message(self, *a, **k):
            raise AssertionError("should not send when there is no content")

    msg = types.SimpleNamespace(
        media=None, photo=None, video=None, audio=None, document=None,
        text=None, caption=None,
    )
    result = asyncio.run(module.process_merged(Client(), Client(), [msg], "42", 42, settings=_settings()))
    assert result == '❌ 没有可合并的内容'


def test_process_merged_builds_album_with_combined_caption(batch_module, tmp_path):
    module, _ = batch_module
    _stub_progress_app(module)
    _stub_input_media(module)
    module.ensure_audio_track = _identity_ensure_audio

    client = _MergeClient(tmp_path)
    msgs = [_photo_msg('Pic 1'), _photo_msg('Pic 2')]
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42, settings=_settings()))

    assert result.startswith('✅')
    assert len(client.groups) == 1
    assert len(client.groups[0]) == 2
    # Combined caption lands on the first item only.
    assert client.groups[0][0].caption == 'Pic 1\n\nPic 2'
    assert client.groups[0][1].caption is None


def test_process_merged_chunks_over_ten_media(batch_module, tmp_path):
    module, _ = batch_module
    _stub_progress_app(module)
    _stub_input_media(module)
    module.ensure_audio_track = _identity_ensure_audio

    client = _MergeClient(tmp_path)
    msgs = [_photo_msg() for _ in range(12)]
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42, settings=_settings()))

    assert result.startswith('✅')
    # 12 items split into groups of 10 + 2 (Telegram media-group limit).
    assert [len(g) for g in client.groups] == [10, 2]

def test_process_merged_oc_chunks_each_get_caption_with_marker(batch_module, tmp_path):
    """oc set + >10 media: each chunk's first item gets the SAME text + (n/N)."""
    module, _ = batch_module
    _stub_progress_app(module)
    _stub_input_media(module)
    module.ensure_audio_track = _identity_ensure_audio

    client = _MergeClient(tmp_path)
    msgs = [_photo_msg() for _ in range(12)]
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42, oc='我的合集', settings=_settings()))

    assert result.startswith('✅')
    assert [len(g) for g in client.groups] == [10, 2]
    # Both chunks carry the same caption text + the progress marker.
    assert client.groups[0][0].caption.startswith('我的合集')
    assert '(1/2)' in client.groups[0][0].caption
    assert client.groups[1][0].caption.startswith('我的合集')
    assert '(2/2)' in client.groups[1][0].caption


def test_process_merged_no_oc_single_chunk_no_marker(batch_module, tmp_path):
    """No oc + ≤10 media: no marker, caption only on first item (unchanged)."""
    module, _ = batch_module
    _stub_progress_app(module)
    _stub_input_media(module)
    module.ensure_audio_track = _identity_ensure_audio

    client = _MergeClient(tmp_path)
    msgs = [_photo_msg('Cap A'), _photo_msg('Cap B')]
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42, settings=_settings()))

    assert result.startswith('✅')
    assert client.groups[0][0].caption == 'Cap A\n\nCap B'
    assert client.groups[0][1].caption is None
    # No marker when oc is None.
    assert '(' not in client.groups[0][0].caption


def test_process_merged_long_text_sent_as_standalone(batch_module, tmp_path):
    module, _ = batch_module
    _stub_progress_app(module)
    _stub_input_media(module)
    module.ensure_audio_track = _identity_ensure_audio

    client = _MergeClient(tmp_path)
    long_caption = 'A' * 1100
    msgs = [_photo_msg(long_caption)]
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42, settings=_settings()))

    assert result.startswith('✅')
    # Caption too long for an album (>1024) → sent separately, not on the item.
    assert client.groups[0][0].caption is None
    assert client.messages == [long_caption]


def test_process_merged_mixed_text_and_media(batch_module, tmp_path):
    module, _ = batch_module
    _stub_progress_app(module)
    _stub_input_media(module)
    module.ensure_audio_track = _identity_ensure_audio

    client = _MergeClient(tmp_path)
    msgs = [_text_msg('Intro'), _photo_msg('Photo caption')]
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42, settings=_settings()))

    assert result.startswith('✅')
    assert len(client.groups) == 1
    # Both the standalone text and the photo caption are merged into one caption.
    assert client.groups[0][0].caption == 'Intro\n\nPhoto caption'


# ---------------------------------------------------------------------------
# Override caption (oc param) — replaces original message text


def test_process_merged_oc_replaces_original_text(batch_module, tmp_path):
    module, _ = batch_module
    _stub_progress_app(module)
    _stub_input_media(module)
    module.ensure_audio_track = _identity_ensure_audio

    client = _MergeClient(tmp_path)
    msgs = [_photo_msg('Original caption A'), _photo_msg('Original caption B')]
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42, oc='My title', settings=_settings()))

    assert result.startswith('✅')
    # oc replaces the combined original captions entirely.
    assert client.groups[0][0].caption == 'My title'
    assert client.groups[0][1].caption is None


def test_process_merged_oc_replaces_text_only_messages(batch_module):
    module, _ = batch_module
    _stub_progress_app(module)

    sent = []

    class Client:
        async def send_message(self, tcid, text=None, reply_to_message_id=None):
            sent.append(text)

    msgs = [_text_msg('Original 1'), _text_msg('Original 2')]
    # oc replaces the merged original text.
    result = asyncio.run(module.process_merged(Client(), Client(), msgs, "42", 42, oc='Custom', settings=_settings()))

    assert result == '✅ 文字已合并发送'
    assert sent == ['Custom']


def test_process_msg_oc_replaces_media_caption(batch_module, tmp_path):
    module, _ = batch_module
    module.fetch_origin[(42, "public_channel")] = True
    _stub_progress_app(module)
    module.thumbnail = lambda d: None

    sent_caps = []

    class UserClient:
        async def download_media(self, m, file_name=None, progress=None, progress_args=None):
            p = tmp_path / "dl.jpg"
            p.write_bytes(b"x")
            return str(p)

        async def send_photo(self, tcid, photo=None, caption=None, **kw):
            sent_caps.append(caption)

    message = types.SimpleNamespace(
        media=True,
        caption=types.SimpleNamespace(markdown='Original media caption'),
        video=None, video_note=None, voice=None, sticker=None,
        audio=None, document=None, photo=types.SimpleNamespace(file_id="p"),
    )
    result = asyncio.run(
        module.process_msg(
            UserClient(), UserClient(), message, "42", "public", 42,
            "public_channel", oc='Override', settings=_settings()
        )
    )

    assert result == "Done."
    assert sent_caps == ['Override']

def test_process_msg_oc_replaces_text_message(batch_module):
    module, _ = batch_module
    _stub_progress_app(module)

    sent = []

    class Client:
        async def send_message(self, tcid, text=None, reply_to_message_id=None):
            sent.append(text)

    message = types.SimpleNamespace(
        media=None,
        text=types.SimpleNamespace(markdown='Original text'),
        caption=None,
    )
    result = asyncio.run(
        module.process_msg(
            Client(), Client(), message, "42", "public", 42,
            "public_channel", oc='Replaced', settings=_settings()
        )
    )

    assert result == "Sent."
    assert sent == ['Replaced']


def test_process_merged_oc_none_preserves_original_text(batch_module, tmp_path):
    """oc=None is the default — original behavior must be unchanged."""
    module, _ = batch_module
    _stub_progress_app(module)
    _stub_input_media(module)
    module.ensure_audio_track = _identity_ensure_audio

    client = _MergeClient(tmp_path)
    msgs = [_photo_msg('Keep me')]
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42, settings=_settings()))

    assert result.startswith('✅')
    assert client.groups[0][0].caption == 'Keep me'


def test_process_album_oc_replaces_caption(batch_module, tmp_path):
    module, _ = batch_module
    _stub_progress_app(module)
    _stub_input_media(module)
    module.ensure_audio_track = _identity_ensure_audio

    msgs = [
        types.SimpleNamespace(
            media=True, video=types.SimpleNamespace(
                file_name="v.mp4", duration=1, width=1, height=1, thumbs=None),
            photo=None, audio=None, document=None,
            caption=types.SimpleNamespace(markdown='Original album cap'),
        ),
    ]

    class FakeClient:
        async def download_media(self, m, file_name=None, progress=None, progress_args=None):
            p = tmp_path / file_name.split('/')[-1]
            p.write_bytes(b"x")
            return str(p)

        async def send_media_group(self, tcid, media, reply_to_message_id=None):
            self.sent_group = media

    client = FakeClient()
    result = asyncio.run(
        module.process_album(
            client, client, msgs, "42", "private", 42, "chan",
            oc='Album override', settings=_settings()
        )
    )

    assert result.startswith('✅')
    assert client.sent_group[0].caption == 'Album override'


# ---------------------------------------------------------------------------
# Comment links (?comment=N) — discussion-group reply resolution


def test_parse_link_lines_extracts_comment_id(batch_module):
    module, _ = batch_module

    mode, payload = module.parse_link_lines(
        'https://t.me/abbbbaabba/681?single&comment=686'
    )

    assert mode == 'range'
    assert payload == ('abbbbaabba', 681, 'public', 686)


def test_parse_link_lines_comment_in_multi(batch_module):
    module, _ = batch_module

    mode, payload = module.parse_link_lines(
        'https://t.me/abbbbaabba/681?comment=686\nhttps://t.me/other/9'
    )

    assert mode == 'multi'
    assert payload[0] == ('abbbbaabba', 681, 'public', 686)
    assert payload[1] == ('other', 9, 'public', None)


def test_get_msg_resolves_comment_via_linked_chat(batch_module):
    """comment_id present -> fetch from linked discussion group, not the channel."""
    module, _ = batch_module

    fetched = {}

    class FakeChat:
        linked_chat = types.SimpleNamespace(id=-100999)

    class FakeClient:
        async def get_chat(self, chat_ref):
            return FakeChat()

        async def get_messages(self, chat_id, msg_id):
            fetched['chat_id'] = chat_id
            fetched['msg_id'] = msg_id
            return types.SimpleNamespace(
                empty=False,
                chat=types.SimpleNamespace(id=chat_id),
                id=msg_id,
            )

    client = FakeClient()
    result = asyncio.run(
        module.get_msg(client, client, 'abbbbaabba', 681, 'public', 42, comment_id=686)
    )

    # Must fetch from the discussion group (-100999), not the channel.
    assert fetched['chat_id'] == -100999
    assert fetched['msg_id'] == 686
    assert result is not None
    assert module.fetch_origin[(42, -100999)] is True


def test_get_msg_comment_without_linked_chat_returns_none(batch_module):
    """No discussion group -> can't resolve comment -> None."""
    module, _ = batch_module

    class FakeChat:
        linked_chat = None

    class FakeClient:
        async def get_chat(self, chat_ref):
            return FakeChat()

    result = asyncio.run(
        module.get_msg(FakeClient(), None, 'abbbbaabba', 681, 'public', 42, comment_id=686)
    )

    assert result is None


def test_get_msg_no_comment_id_uses_normal_path(batch_module):
    """comment_id=None (default) -> normal channel fetch, no discussion-group logic."""
    module, _ = batch_module

    get_chat_calls = []

    class FakeClient:
        async def get_chat(self, chat_ref):
            get_chat_calls.append(chat_ref)
            raise AssertionError('get_chat should not be called without comment_id')

        async def get_messages(self, chat_id, msg_id):
            return types.SimpleNamespace(empty=False, chat=None, id=msg_id)

    client = FakeClient()
    # No comment_id -> enters the normal 'public' path, tries get_messages directly.
    asyncio.run(
        module.get_msg(client, None, 'abbbbaabba', 681, 'public', 42)
    )
    assert get_chat_calls == []  # get_chat (discussion resolution) never called


# ---------------------------------------------------------------------------
# Rate control — with_flood_retry


def test_with_flood_retry_succeeds_first_try(batch_module):
    module, _ = batch_module
    calls = []

    async def coro():
        calls.append(1)
        return 'ok'

    result = asyncio.run(module.with_flood_retry(coro, 'test', max_retries=3))
    assert result == 'ok'
    assert len(calls) == 1


def test_with_flood_retry_retries_on_flood_then_succeeds(batch_module):
    module, _ = batch_module
    calls = []

    async def coro():
        calls.append(1)
        if len(calls) < 2:
            raise module.FloodWait()  # patched from pyrogram.errors
        return 'recovered'

    result = asyncio.run(module.with_flood_retry(coro, 'test', max_retries=3))
    assert result == 'recovered'
    assert len(calls) == 2


def test_with_flood_retry_exhausts_retries_and_raises(batch_module):
    module, _ = batch_module
    calls = []

    async def coro():
        calls.append(1)
        raise module.FloodWait()

    # Should raise after max_retries attempts.
    try:
        asyncio.run(module.with_flood_retry(coro, 'test', max_retries=2))
        assert False, 'should have raised'
    except Exception:
        pass
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Task queue system


def test_create_task_registers_and_defaults(batch_module):
    module, _ = batch_module
    t = module.create_task(42, 'batch_links', 5, links=[], caption='hi', chat_id='42')
    assert t['id'].startswith('task_42_')
    assert t['status'] == 'queued'
    assert t['total'] == 5
    assert t['current'] == 0
    assert t['success'] == 0
    assert t['uid'] == 42
    assert module.TASKS[t['id']] is t


def test_task_update_modifies_fields(batch_module):
    module, _ = batch_module
    t = module.create_task(42, 'merge', 3, links=[], caption=None, chat_id='42')
    module.task_update(t['id'], current=2, success=1, progress_msg='downloading')
    assert t['current'] == 2
    assert t['success'] == 1
    assert t['progress_msg'] == 'downloading'


def test_task_should_cancel(batch_module):
    module, _ = batch_module
    t = module.create_task(42, 'single', 1, link_info=(1, 2, 'public', None), caption=None, chat_id='42')
    assert module.task_should_cancel(t['id']) is False
    t['cancel_requested'] = True
    assert module.task_should_cancel(t['id']) is True


def test_request_cancel_tasks_only_apects_user(batch_module):
    module, _ = batch_module
    t1 = module.create_task(42, 'merge', 2, links=[], caption=None, chat_id='42')
    t2 = module.create_task(99, 'merge', 2, links=[], caption=None, chat_id='99')
    count = module.request_cancel_tasks(42)
    assert count == 1
    assert t1['cancel_requested'] is True
    assert t2['cancel_requested'] is False


def test_get_user_tasks_newest_first(batch_module):
    module, _ = batch_module
    t1 = module.create_task(42, 'single', 1, link_info=(1, 1, 'public', None), caption=None, chat_id='42')
    t2 = module.create_task(42, 'single', 1, link_info=(2, 2, 'public', None), caption=None, chat_id='42')
    t3 = module.create_task(99, 'single', 1, link_info=(3, 3, 'public', None), caption=None, chat_id='99')
    result = module.get_user_tasks(42)
    assert len(result) == 2
    assert result[0]['id'] == t2['id']  # newest first
    assert result[1]['id'] == t1['id']


def test_get_queue_size_empty(batch_module):
    module, _ = batch_module
    assert module.get_queue_size(42) == 0


def test_has_running_task_false_when_queued(batch_module):
    module, _ = batch_module
    module.create_task(42, 'merge', 2, links=[], caption=None, chat_id='42')
    assert module.has_running_task(42) is False
    # Manually set one to running
    module.TASKS[list(module.TASKS.keys())[0]]['status'] = 'running'
    assert module.has_running_task(42) is True


async def _identity_coro():
    pass


def test_task_worker_processes_queued_tasks(batch_module):
    """Integration: enqueue a task, worker picks it up and runs it."""
    module, _ = batch_module
    # _run_single will fail since process_one_link doesn't exist in test, but
    # the worker should still mark status (failed) and not hang.
    t = module.create_task(42, 'single', 1, link_info=(1, 1, 'public', None),
                           caption=None, chat_id='42')

    async def _test():
        await module.enqueue_task(42, t)
        # Let the worker process
        await asyncio.sleep(0.05)
        # Worker should have attempted and set a terminal status
        assert t['status'] in ('done', 'failed')
        assert t['finished_at'] is not None

    asyncio.run(_test())

def test_dispatch_snapshots_settings_once_for_single_message_chain(batch_module):
    module, _ = batch_module
    module.LOG_GROUP = 0
    snapshots = []
    sent = []

    async def get_data(uid):
        snapshots.append(uid)
        return {'caption': 'snapshot caption'}

    class Client:
        async def send_message(self, tcid, text=None, reply_to_message_id=None):
            sent.append((tcid, text))

    client = Client()
    module.get_user_data = get_data
    module.get_ubot = lambda _uid, **_k: asyncio.sleep(0, result=client)
    module.get_uclient = lambda _uid, **_k: asyncio.sleep(0, result=None)
    message = types.SimpleNamespace(
        media=None,
        text=types.SimpleNamespace(markdown='hello'),
        caption=None,
    )

    async def process_one_link(ubot, uc, i, s, lt, d, uid, oc=None, comment_id=None, *, settings):
        return await module.process_msg(
            ubot, uc, message, d, lt, uid, i, oc, settings=settings
        )

    module.process_one_link = process_one_link
    task = module.create_task(
        42,
        'single',
        1,
        link_info=('public_channel', 7, 'public', None),
        caption=None,
        chat_id='42',
    )

    asyncio.run(module._dispatch_task(42, task))

    assert snapshots == [42]
    assert task['settings']['caption'] == 'snapshot caption'
    assert sent == [(42, 'hello')]


# ─── Phase 4 adversarial-review regression tests ─────────────────────────────


def test_dispatch_chain_fails_loudly_on_any_extra_users_read(batch_module):
    """P1 regression: the task chain must not touch user-data accessors beyond
    the single dispatch-time snapshot. Any stray get_user_data/get_user_data_key
    call raises instead of being silently mocked away."""
    module, _ = batch_module
    module.LOG_GROUP = 0
    reads = []
    sent = []

    async def get_data(uid):
        # The dispatch-time document fetch is the ONE allowed users read.
        reads.append(uid)
        return None

    async def fail_users_read(*_a, **_k):
        raise AssertionError("unexpected users-collection read in task chain")

    class Client:
        async def send_message(self, tcid, text=None, reply_to_message_id=None):
            sent.append((tcid, text))

    module.get_user_data = get_data
    module.get_user_data_key = fail_users_read
    module.get_ubot = lambda _uid, **_k: asyncio.sleep(0, result=Client())
    module.get_uclient = lambda _uid, **_k: asyncio.sleep(0, result=None)
    message = types.SimpleNamespace(
        media=None,
        text=types.SimpleNamespace(markdown='hello'),
        caption=None,
    )

    async def process_one_link(ubot, uc, i, s, lt, d, uid, oc=None, comment_id=None, *, settings):
        return await module.process_msg(
            ubot, uc, message, d, lt, uid, i, oc, settings=settings
        )

    module.process_one_link = process_one_link
    task = module.create_task(
        42,
        'single',
        1,
        link_info=('public_channel', 7, 'public', None),
        caption=None,
        chat_id='42',
    )

    asyncio.run(module._dispatch_task(42, task))

    assert reads == [42]
    assert sent == [(42, 'hello')]


def test_get_uclient_cache_hit_performs_no_db_reads(batch_module):
    """P1 regression: a warm user_clients entry short-circuits before get_user_data and
    before get_ubot — steady-state tasks issue zero extra users queries."""
    module, _ = batch_module
    cached = object()
    module.user_clients[42] = cached
    module._UC_EPOCH[42] = 0  # matches fixture cred_epoch == 0

    async def fail_read(*_a, **_k):
        raise AssertionError("users read on warm get_uclient path")

    module.get_user_data = fail_read
    module.get_user_data_key = fail_read
    module.get_ubot = fail_read

    assert asyncio.run(module.get_uclient(42)) is cached


def test_process_msg_builds_caption_from_snapshot_rules(batch_module, monkeypatch):
    """The media path must apply the snapshot's replacement/delete words and
    append the snapshot caption — wired through the PRODUCTION apply_text_rules
    (loaded from utils/func.py), not a test double."""
    module, _ = batch_module
    module.LOG_GROUP = 0
    sent = []

    # Load the real utils.func to exercise production rule semantics. The
    # batch_module fixture already stubbed config/utils.encrypt; func.py
    # additionally needs config.MONGO_DB/DB_NAME, encrypt.ecs and motor.
    monkeypatch.setattr(sys.modules['config'], 'MONGO_DB', 'mongodb://unused', raising=False)
    monkeypatch.setattr(sys.modules['config'], 'DB_NAME', 'test', raising=False)
    monkeypatch.setattr(sys.modules['utils.encrypt'], 'ecs', lambda value: value, raising=False)
    motor = types.ModuleType('motor')
    motor_asyncio = types.ModuleType('motor.motor_asyncio')

    class FakeMotorClient:
        def __init__(self, *_a, **_k):
            pass

        def __getitem__(self, _name):
            return self

        def __getattr__(self, _name):
            return self

    motor_asyncio.AsyncIOMotorClient = FakeMotorClient
    motor.motor_asyncio = motor_asyncio
    monkeypatch.setitem(sys.modules, 'motor', motor)
    monkeypatch.setitem(sys.modules, 'motor.motor_asyncio', motor_asyncio)
    spec = importlib.util.spec_from_file_location('real_func_for_rules', SRC / 'utils' / 'func.py')
    real_func = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real_func)
    module.apply_text_rules = real_func.apply_text_rules

    class Client:
        async def send_photo(self, tcid, file_id, caption=None, reply_to_message_id=None):
            sent.append((tcid, caption))

    message = types.SimpleNamespace(
        media=types.SimpleNamespace(),
        photo=types.SimpleNamespace(file_id='photo-file'),
        video=None, video_note=None, voice=None, sticker=None,
        audio=None, document=None,
        caption=types.SimpleNamespace(markdown='alpha beta'),
        text=None,
    )
    settings = _settings(
        caption='snap cap',
        replacement_words={'alpha': 'A'},
        delete_words=['beta'],
    )

    result = asyncio.run(
        module.process_msg(
            Client(), Client(), message, '42', 'public', 42, 'chan', None,
            settings=settings,
        )
    )

    assert result == 'Sent directly.'
    # Production semantics: substring replace ('alpha'->'A'), then whole-word
    # delete removes 'beta' and re-joins -> 'A' (no stray whitespace).
    assert sent == [(42, 'A\n\nsnap cap')]


def test_run_batch_count_forwards_snapshot_to_process_msg(batch_module):
    """Runner coverage: _run_batch_count threads the dispatch snapshot through
    every iteration (batch_links/single covered elsewhere)."""
    module, _ = batch_module
    seen = []

    module.get_ubot = lambda _uid, **_k: asyncio.sleep(0, result=None)
    module.get_uclient = lambda _uid, **_k: asyncio.sleep(0, result=None)
    module.get_msg = lambda *_a: asyncio.sleep(0, result=object())

    async def process_msg(c, u, m, d, lt, uid, i, oc=None, *, settings):
        seen.append(settings)
        return 'Done.'

    module.process_msg = process_msg
    task = module.create_task(
        42, 'batch_count', 2, cid='chan', sid=5, lt='public', num=2,
        caption=None, chat_id='42',
    )
    task['settings'] = _settings(caption='fwd')

    asyncio.run(module._run_batch_count(42, task, {}, 0))

    assert len(seen) == 2
    assert all(s['caption'] == 'fwd' for s in seen)


def test_get_uclient_miss_reads_user_data_exactly_once(batch_module):
    """Cold-path contract: user_clients miss performs exactly one session read
    (establishment-class, documented residual), then falls back to the
    custom bot when no session_string exists."""
    module, _ = batch_module
    calls = []

    async def get_user_data(uid):
        calls.append(uid)
        return None

    module.get_user_data = get_user_data
    module.get_ubot = lambda _uid, **_k: asyncio.sleep(0, result='bot')

    result = asyncio.run(module.get_uclient(42))

    assert result == 'bot'
    assert calls == [42]


def test_task_chain_performs_exactly_one_real_find_one(batch_module, monkeypatch):
    """Acceptance: a full dispatched task — including COLD client establishment
    through the prefetched document — performs exactly one real
    users_collection.find_one (real func accessors, counted collection)."""
    module, _ = batch_module
    module.LOG_GROUP = 0

    monkeypatch.setattr(sys.modules['config'], 'MONGO_DB', 'mongodb://unused', raising=False)
    monkeypatch.setattr(sys.modules['config'], 'DB_NAME', 'test', raising=False)
    monkeypatch.setattr(sys.modules['utils.encrypt'], 'ecs', lambda value: value, raising=False)
    motor = types.ModuleType('motor')
    motor_asyncio = types.ModuleType('motor.motor_asyncio')

    class FakeMotorClient:
        def __init__(self, *_a, **_k):
            pass

        def __getitem__(self, _name):
            return self

        def __getattr__(self, _name):
            return self

    motor_asyncio.AsyncIOMotorClient = FakeMotorClient
    motor.motor_asyncio = motor_asyncio
    monkeypatch.setitem(sys.modules, 'motor', motor)
    monkeypatch.setitem(sys.modules, 'motor.motor_asyncio', motor_asyncio)
    spec = importlib.util.spec_from_file_location('real_func_for_count', SRC / 'utils' / 'func.py')
    real_func = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real_func)

    find_one = AsyncMock(return_value={
        'user_id': 42,
        'caption': 'real cap',
        'session_string': 'enc-session',
        'bot_token': 'enc-token',
    })
    real_func.users_collection = types.SimpleNamespace(find_one=find_one)
    module.get_user_data = real_func.get_user_data
    module.filter_settings = real_func.filter_settings
    # Real get_ubot/get_uclient run against the prefetched doc; fixture
    # pyrogram.Client is a fake whose start() never touches the network, and
    # encrypt.dcs is the identity stub. The chain continues through the REAL
    # process_msg (only the network fetch get_msg is bypassed).
    sent = []

    class DeliveryClient:
        async def send_message(self, tcid, text=None, reply_to_message_id=None):
            sent.append((tcid, text))

    message = types.SimpleNamespace(
        media=None,
        text=types.SimpleNamespace(markdown='hello'),
        caption=None,
    )

    async def process_one_link(ubot, uc, i, s, lt, d, uid, oc=None, comment_id=None, *, settings):
        return await module.process_msg(
            DeliveryClient(), DeliveryClient(), message, d, lt, uid, i, oc,
            settings=settings,
        )

    module.process_one_link = process_one_link

    task = module.create_task(
        42, 'single', 1, link_info=('public_channel', 7, 'public', None),
        caption=None, chat_id='42',
    )

    asyncio.run(module._dispatch_task(42, task))

    assert find_one.await_count == 1
    assert find_one.await_args_list[0].args == ({'user_id': 42},)
    assert task['settings']['caption'] == 'real cap'
    assert 'session_string' not in task['settings']
    # Cold clients were established from the prefetched doc, no re-query.
    assert 42 in module.user_bots and 42 in module.user_clients
    # The real process_msg delivered through the snapshot settings.
    assert sent == [(42, 'hello')]


def test_get_ubot_uses_matching_prefetch_without_query(batch_module):
    """Epoch-matched prefetch: zero users queries, client built from doc."""
    module, FakeClient = batch_module

    async def fail_read(*_a, **_k):
        raise AssertionError("users read despite matching epoch")

    module.get_user_data_key = fail_read
    module.cred_epoch = lambda _uid: 5

    bot = asyncio.run(
        module.get_ubot(42, prefetched={'bot_token': 'tok'}, prefetched_epoch=5)
    )

    assert bot is not None
    assert bot.kwargs.get('bot_token') == 'tok'
    assert module.user_bots[42] is bot


def test_get_ubot_discards_stale_prefetch_and_rereads_under_lock(batch_module):
    """Rotation race: /setbot-/rembot bump the epoch after dispatch; the stale
    prefetched token must be discarded for a fresh locked read."""
    module, FakeClient = batch_module
    calls = []

    async def get_key(uid, key, default=None):
        calls.append((uid, key))
        return 'fresh-token'

    module.get_user_data_key = get_key
    module.cred_epoch = lambda _uid: 1  # rotation happened after dispatch read

    bot = asyncio.run(
        module.get_ubot(42, prefetched={'bot_token': 'old-token'}, prefetched_epoch=0)
    )

    assert calls == [(42, 'bot_token')]
    assert bot.kwargs.get('bot_token') == 'fresh-token'


def test_get_ubot_rotation_during_start_never_caches_stale(batch_module, monkeypatch):
    """TOCTOU regression: a credential rotation landing while Client.start()
    is in flight must discard the freshly started client, never cache it."""
    module, FakeClient = batch_module
    state = {'epoch': 0}
    module.cred_epoch = lambda _uid: state['epoch']

    async def rotating_start(self):
        state['epoch'] = 1  # /rembot lands mid-start

    monkeypatch.setattr(FakeClient, 'start', rotating_start)

    bot = asyncio.run(
        module.get_ubot(42, prefetched={'bot_token': 'tok'}, prefetched_epoch=0)
    )

    assert bot is None
    assert 42 not in module.user_bots


def test_get_ubot_evicts_cached_client_after_rotation(batch_module):
    """Cache-hit epoch check: a client built before rotation is evicted under
    the lock and rebuilt from a fresh locked read (addsession/rembot path)."""
    module, FakeClient = batch_module
    stale = FakeClient('user_42')
    module.user_bots[42] = stale
    calls = []

    async def get_key(uid, key, default=None):
        calls.append((uid, key))
        return 'fresh-token'

    module.get_user_data_key = get_key
    module.cred_epoch = lambda _uid: 7

    bot = asyncio.run(
        module.get_ubot(42, prefetched={'bot_token': 'old-token'}, prefetched_epoch=3)
    )

    assert stale.stopped is True
    assert calls == [(42, 'bot_token')]
    assert bot is not stale
    assert bot.kwargs.get('bot_token') == 'fresh-token'
    assert module.user_bots[42] is bot


def test_get_uclient_evicts_cached_session_client_after_rotation(batch_module):
    """addsession bumps the epoch without stopping user_clients; the next get_uclient
    with a stale prefetch must evict the old session client and rebuild from
    a freshly read document."""
    module, FakeClient = batch_module
    stale = FakeClient('42_client')
    module.user_clients[42] = stale
    reads = []

    async def get_data(uid):
        reads.append(uid)
        return {'session_string': 'new-session'}

    async def no_token(uid, key, default=None):
        return None

    module.get_user_data = get_data
    module.get_user_data_key = no_token
    module.cred_epoch = lambda _uid: 2

    client = asyncio.run(
        module.get_uclient(
            42,
            prefetched={'session_string': 'old-session', 'bot_token': None},
            prefetched_epoch=1,
        )
    )

    assert stale.stopped is True
    assert client is not stale
    assert module.user_clients[42] is client
    assert reads == [42]


def test_get_ubot_prefetched_plaintext_token_survives_self_migration(batch_module):
    """A prefetched legacy PLAINTEXT token migrates itself; the migration's own
    epoch bump must not trip the post-start guard (data re-synchronized)."""
    module, FakeClient = batch_module
    state = {'epoch': 5}
    module.cred_epoch = lambda _uid: state['epoch']
    module.dcs = lambda _v: (_ for _ in ()).throw(ValueError('not ciphertext'))

    async def migrate(uid, plaintext):
        state['epoch'] += 1  # real migrate_user_bot_token bumps on success
        return True

    module.migrate_user_bot_token = migrate
    plaintext = '123456789:ABCDEFGHIJklmnopqrstuvwxyz0123456789'

    bot = asyncio.run(
        module.get_ubot(42, prefetched={'bot_token': plaintext}, prefetched_epoch=5)
    )

    assert bot is not None
    assert bot.kwargs.get('bot_token') == plaintext
    assert module.user_bots[42] is bot


def test_dispatch_captures_epoch_before_document_read(batch_module):
    """Pairing invariant: epoch is taken BEFORE the doc read, so a rotation
    completing mid-read yields a conservative mismatch, never stale acceptance."""
    module, _ = batch_module
    order = []

    def epoch(_uid):
        order.append('epoch')
        return 0

    async def get_data(uid):
        order.append('read')
        return None

    module.cred_epoch = epoch
    module.get_user_data = get_data
    module.get_ubot = lambda _uid, **_k: asyncio.sleep(0, result=None)
    module.get_uclient = lambda _uid, **_k: asyncio.sleep(0, result=None)
    module.process_one_link = lambda *_a, **_k: asyncio.sleep(0, result='Done.')

    task = module.create_task(
        42, 'single', 1, link_info=('ch', 7, 'public', None),
        caption=None, chat_id='42',
    )
    asyncio.run(module._dispatch_task(42, task))

    assert order == ['epoch', 'read']


def test_get_uclient_rotation_during_upd_dlg_never_caches_stale(batch_module, monkeypatch):
    """The final epoch re-check sits between upd_dlg and the cache insert;
    a rotation during upd_dlg discards the client (bot fallback)."""
    module, FakeClient = batch_module
    state = {'epoch': 3}
    module.cred_epoch = lambda _uid: state['epoch']

    async def rotating_upd_dlg(_client):
        state['epoch'] = 4  # login/logout lands during dialog warm-up
        return True

    monkeypatch.setattr(module, 'upd_dlg', rotating_upd_dlg)

    client = asyncio.run(
        module.get_uclient(
            42,
            prefetched={'session_string': 'sess', 'bot_token': 'tok'},
            prefetched_epoch=3,
        )
    )

    assert client is not None
    assert client.kwargs.get('bot_token') == 'tok'  # fell back to the bot client
    assert 42 not in module.user_clients
