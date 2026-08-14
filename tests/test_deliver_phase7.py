import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1]


class _FakeMainBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.deleted = []

    async def send_message(self, chat_id, text):
        message = types.SimpleNamespace(id=len(self.sent) + 1)
        self.sent.append((chat_id, text, message))
        return message

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    async def delete_messages(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _Client:
    def __init__(self):
        self.download_paths = []
        self.sent = []

    async def download_media(self, message, file_name=None, **kwargs):
        path = Path(file_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'payload')
        self.download_paths.append(str(path))
        return str(path)

    async def send_message(self, chat_id, text=None, reply_to_message_id=None):
        self.sent.append(('message', chat_id, text, reply_to_message_id))

    async def send_sticker(self, chat_id, sticker, reply_to_message_id=None):
        self.sent.append(('sticker', chat_id, sticker, reply_to_message_id))

    async def send_photo(self, chat_id, photo, **kwargs):
        self.sent.append(('photo', chat_id, photo, kwargs))


@pytest.fixture
def deliver_env(monkeypatch, tmp_path):
    pyrogram = types.ModuleType('pyrogram')

    class FloodWait(Exception):
        def __init__(self, value):
            super().__init__(value)
            self.value = value

    pyrogram_errors = types.ModuleType('pyrogram.errors')
    pyrogram_errors.FloodWait = FloodWait
    monkeypatch.setitem(sys.modules, 'pyrogram', pyrogram)
    monkeypatch.setitem(sys.modules, 'pyrogram.errors', pyrogram_errors)

    pyrogram_types = types.ModuleType('pyrogram.types')
    pyrogram_types.InputMediaPhoto = type('InputMediaPhoto', (), {})
    pyrogram_types.InputMediaVideo = type('InputMediaVideo', (), {})
    pyrogram_types.InputMediaDocument = type('InputMediaDocument', (), {})
    pyrogram_types.InputMediaAudio = type('InputMediaAudio', (), {})
    monkeypatch.setitem(sys.modules, 'pyrogram.types', pyrogram_types)

    config = types.ModuleType('config')
    config.LOG_GROUP = 0
    config.MAX_FLOOD_RETRIES = 2
    config.UPLOAD_INTERVAL = 0
    config.PROGRESS_MIN_INTERVAL = 3.0
    monkeypatch.setitem(sys.modules, 'config', config)

    workdir = tmp_path / 'work'
    workdir.mkdir()
    main_bot = _FakeMainBot()
    shared_client = types.ModuleType('shared_client')
    shared_client.app = main_bot
    shared_client._WORKDIR = str(workdir)
    monkeypatch.setitem(sys.modules, 'shared_client', shared_client)

    utils = types.ModuleType('utils')
    utils.__path__ = [str(SRC / 'utils')]
    monkeypatch.setitem(sys.modules, 'utils', utils)
    func = types.ModuleType('utils.func')
    func.apply_text_rules = lambda text, _replacement, _delete: text
    func.screenshot = None
    func.thumbnail = lambda _d: None
    func.get_video_metadata = None
    func.ensure_audio_track = None
    func.touch_file = lambda *_args, **_kwargs: None
    func.VIDEO_EXTENSIONS = set()
    func.AUDIO_EXTENSIONS = set()
    monkeypatch.setitem(sys.modules, 'utils.func', func)

    plugins = types.ModuleType('plugins')
    plugins.__path__ = [str(SRC / 'plugins')]
    monkeypatch.setitem(sys.modules, 'plugins', plugins)
    fetch = types.ModuleType('plugins.fetch')
    fetch.fetch_origin = {}
    fetch.get_msg = None
    fetch.resolve_linked_chat = None
    fetch.upd_dlg = None
    fetch.premium_userbot = None
    monkeypatch.setitem(sys.modules, 'plugins.fetch', fetch)
    tasks = types.ModuleType('plugins.tasks')
    tasks.sanitize = lambda value: value
    tasks.register_sweep_hook = lambda _hook: None
    monkeypatch.setitem(sys.modules, 'plugins.tasks', tasks)

    sys.modules.pop('plugins.deliver', None)
    module = importlib.import_module('plugins.deliver')
    module.progress_state.clear()
    module.rename_file = None
    return module, main_bot, fetch


def _settings():
    return {
        'caption': '',
        'chat_id': None,
        'replacement_words': {},
        'delete_words': [],
        'rename_tag': '',
    }


def _message(*, text=None, photo=None, sticker=None, document=None):
    media = any(value is not None for value in (photo, sticker, document))
    return types.SimpleNamespace(
        media=media,
        caption=None,
        video=None,
        video_note=None,
        voice=None,
        sticker=sticker,
        audio=None,
        document=document,
        photo=photo,
        text=types.SimpleNamespace(markdown=text) if text is not None else None,
    )


def test_prog_throttles_by_time_and_keeps_heartbeat(deliver_env, monkeypatch):
    module, _main_bot, _fetch = deliver_env
    module.PROGRESS_MIN_INTERVAL = 3.0
    clock = [100.0]
    touches = []
    edits = []

    class Client:
        async def edit_message_text(self, *args):
            edits.append(args)

    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    monkeypatch.setattr(module, 'touch_file', lambda path: touches.append(path))

    async def run():
        for completed in range(5):
            await module.prog(completed + 1, 100, Client(), 42, 7, 0, fp='download')
        clock[0] = 104.0
        await module.prog(6, 100, Client(), 42, 7, 0, fp='download')
        await module.prog(100, 100, Client(), 42, 7, 0, fp='download')

    asyncio.run(run())

    assert len(edits) == 3
    assert len(touches) == 7
    assert 7 not in module.progress_state


def test_with_flood_retry_invokes_resilient_hook(deliver_env, monkeypatch):
    module, _main_bot, _fetch = deliver_env
    attempts = [0]
    seen = []

    async def no_sleep(_seconds):
        return None

    async def operation():
        attempts[0] += 1
        if attempts[0] == 1:
            raise module.FloodWait(4)
        return 'ok'

    monkeypatch.setattr(module.asyncio, 'sleep', no_sleep)
    result = asyncio.run(
        module.with_flood_retry(
            operation,
            context='test',
            max_retries=2,
            on_flood=lambda seconds: seen.append(seconds),
        )
    )
    assert result == 'ok'
    assert seen == [4]

    attempts[0] = 0

    def broken_hook(_seconds):
        raise RuntimeError('limiter bug')

    result = asyncio.run(
        module.with_flood_retry(
            operation,
            context='broken-hook',
            max_retries=2,
            on_flood=broken_hook,
        )
    )
    assert result == 'ok'


def test_prepare_text_defers_send_until_finish(deliver_env):
    module, _main_bot, _fetch = deliver_env
    client = _Client()
    message = _message(text='hello')

    result, prep = asyncio.run(
        module.prepare_msg(client, client, message, '42', 'private', 7, 'chat', settings=_settings())
    )
    assert result is None
    assert prep.kind == 'text'
    assert client.sent == []

    assert asyncio.run(module.finish_prepared_msg(prep)) == 'Sent.'
    assert client.sent == [('message', 42, 'hello', None)]


def test_prepare_direct_defers_direct_send_and_download(deliver_env, monkeypatch):
    module, _main_bot, fetch = deliver_env
    client = _Client()
    fetch.fetch_origin[(7, 'chat')] = False
    message = _message(photo=types.SimpleNamespace(file_id='photo-id'))
    direct_calls = []

    async def direct(*args, **kwargs):
        direct_calls.append((args, kwargs))
        return True, None

    monkeypatch.setattr(module, 'send_direct', direct)
    result, prep = asyncio.run(
        module.prepare_msg(client, client, message, '42', 'public', 7, 'chat', settings=_settings())
    )
    assert result is None
    assert prep.kind == 'direct'
    assert direct_calls == []
    assert client.download_paths == []

    assert asyncio.run(module.finish_prepared_msg(prep)) == 'Sent directly.'
    assert len(direct_calls) == 1


def test_process_msg_matches_prepare_finish_and_abort_cleanup(deliver_env):
    module, main_bot, fetch = deliver_env
    module.thumbnail = lambda _d: None
    fetch.fetch_origin[(7, 'chat')] = True
    client = _Client()
    message = _message(sticker=types.SimpleNamespace(file_id='sticker-id'))

    direct_result = asyncio.run(
        module.process_msg(client, client, message, '42', 'public', 7, 'chat', settings=_settings())
    )
    assert direct_result == 'Done.'

    prepared_client = _Client()
    result, prep = asyncio.run(
        module.prepare_msg(
            prepared_client,
            prepared_client,
            message,
            '42',
            'public',
            7,
            'chat',
            settings=_settings(),
        )
    )
    assert result is None
    assert prep.kind == 'downloaded'
    downloaded = prep.f
    assert Path(downloaded).exists()
    assert asyncio.run(module.finish_prepared_msg(prep)) == 'Done.'
    assert not Path(downloaded).exists()

    result, prep = asyncio.run(
        module.prepare_msg(
            prepared_client,
            prepared_client,
            message,
            '42',
            'public',
            7,
            'chat',
            settings=_settings(),
        )
    )
    assert result is None
    downloaded = prep.f
    assert Path(downloaded).exists()
    asyncio.run(module.abort_prepared_msg(prep))
    assert not Path(downloaded).exists()
    assert main_bot.deleted


def test_prepare_document_paths_are_unique_for_same_uid_and_name(deliver_env):
    module, _main_bot, fetch = deliver_env
    fetch.fetch_origin[(7, 'chat')] = True
    client = _Client()
    document = types.SimpleNamespace(file_name='same.bin')
    message = _message(document=document)

    _, first = asyncio.run(
        module.prepare_msg(client, client, message, '42', 'public', 7, 'chat', settings=_settings())
    )
    _, second = asyncio.run(
        module.prepare_msg(client, client, message, '42', 'public', 7, 'chat', settings=_settings())
    )
    assert first.f != second.f
    asyncio.run(module.abort_prepared_msg(first))
    asyncio.run(module.abort_prepared_msg(second))


def test_cancelled_prepare_cleans_file_and_progress_message(deliver_env):
    module, main_bot, fetch = deliver_env
    fetch.fetch_origin[(7, 'chat')] = True
    message = _message(sticker=types.SimpleNamespace(file_id='sticker-id'))

    async def run():
        started = asyncio.Event()
        client = _Client()

        async def blocked_download(message, file_name=None, **kwargs):
            path = Path(file_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'partial')
            client.download_paths.append(str(path))
            started.set()
            await asyncio.Event().wait()

        client.download_media = blocked_download
        task = asyncio.create_task(
            module.prepare_msg(
                client,
                client,
                message,
                '42',
                'public',
                7,
                'chat',
                settings=_settings(),
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return client.download_paths[0]

    downloaded = asyncio.run(run())
    assert not Path(downloaded).exists()
    assert main_bot.deleted
