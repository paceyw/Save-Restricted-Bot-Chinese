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
    func.ensure_audio_track = None
    func.touch_file = lambda *_a, **_k: None
    func.get_user_data_key = None
    func.process_text_with_rules = None
    func.is_premium_user = None

    import re as _re

    def _E(L):
        private_match = _re.match(r'https://t\.me/c/(\d+)/(?:\d+/)?(\d+)', L)
        public_match = _re.match(r'https://t\.me/([^/]+)/(?:\d+/)?(\d+)', L)
        comment_match = _re.search(r'[?&]comment=(\d+)', L)
        comment_id = int(comment_match.group(1)) if comment_match else None
        if private_match:
            return f'-100{private_match.group(1)}', int(private_match.group(2)), 'private', comment_id
        if public_match:
            return public_match.group(1), int(public_match.group(2)), 'public', comment_id
        return None, None, None, None

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
        module.get_msg(BotClient(), UserClient(), "public_channel", 7, "public", 42)
    )

    assert message is not None
    assert module.emp[(42, "public_channel")] is False


def test_get_msg_public_uses_bot_directly_without_user_client(batch_module):
    module, _ = batch_module
    module.emp.clear()

    class BotClient:
        async def get_messages(self, chat, message_id):
            return types.SimpleNamespace(empty=False, media=False)

    message = asyncio.run(
        module.get_msg(BotClient(), None, "public_channel", 7, "public", 42)
    )

    assert message is not None
    assert module.emp[(42, "public_channel")] is False

def test_get_msg_public_marks_user_source_for_download(batch_module):
    module, _ = batch_module
    module.emp.clear()

    class UserClient:
        async def get_messages(self, chat, message_id):
            return types.SimpleNamespace(empty=False, media=False)

    message = asyncio.run(
        module.get_msg(None, UserClient(), "public_channel", 7, "public", 42)
    )

    assert message is not None
    assert module.emp[(42, "public_channel")] is True


def test_process_msg_does_not_report_direct_send_success_on_error(batch_module):
    module, _ = batch_module
    module.emp[(42, "public_channel")] = False

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
    module.X = FakeX()
    module.LOG_GROUP = 0


def test_process_msg_cleans_downloaded_file_on_processing_error(batch_module, tmp_path):
    module, _ = batch_module
    module.emp[(42, "public_channel")] = True
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
        module.process_msg(UserClient(), UserClient(), message, "42", "public", 42, "public_channel")
    )

    assert result.startswith("Error:")
    assert not downloaded.exists()


def test_process_msg_sends_sticker_from_downloaded_file(batch_module, tmp_path):
    module, _ = batch_module
    module.emp[(42, "public_channel")] = True
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
        module.process_msg(UserClient(), UserClient(), message, "42", "public", 42, "public_channel")
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
    result = asyncio.run(module.process_merged(Client(), Client(), msgs, "42", 42))

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
    result = asyncio.run(module.process_merged(Client(), Client(), [msg], "42", 42))
    assert result == '❌ 没有可合并的内容'


def test_process_merged_builds_album_with_combined_caption(batch_module, tmp_path):
    module, _ = batch_module
    _stub_progress_app(module)
    _stub_input_media(module)
    module.ensure_audio_track = _identity_ensure_audio

    client = _MergeClient(tmp_path)
    msgs = [_photo_msg('Pic 1'), _photo_msg('Pic 2')]
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42))

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
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42))

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
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42, oc='我的合集'))

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
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42))

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
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42))

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
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42))

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
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42, oc='My title'))

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
    result = asyncio.run(module.process_merged(Client(), Client(), msgs, "42", 42, oc='Custom'))

    assert result == '✅ 文字已合并发送'
    assert sent == ['Custom']


def test_process_msg_oc_replaces_media_caption(batch_module, tmp_path):
    module, _ = batch_module
    module.emp[(42, "public_channel")] = True
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
        module.process_msg(UserClient(), UserClient(), message, "42", "public", 42, "public_channel", oc='Override')
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
        module.process_msg(Client(), Client(), message, "42", "public", 42, "public_channel", oc='Replaced')
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
    result = asyncio.run(module.process_merged(client, client, msgs, "42", 42))

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
        module.process_album(client, client, msgs, "42", "private", 42, "chan", oc='Album override')
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
    assert module.emp[(42, -100999)] is True


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
