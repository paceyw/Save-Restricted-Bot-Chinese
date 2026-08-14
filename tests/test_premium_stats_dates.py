"""Date-formatting coverage for premium.py / stats.py strftime sites.

Phase 6 regression history: a global rename turned strftime '%Y' into
'%premium_userbot' in these files (no test covered the paths, so it shipped).
These tests pin the exact rendered strings with hardcoded expectations —
no strftime in the assertions, so a mangled format string cannot pass.
"""
import asyncio
import sys
import types
from datetime import datetime

import pytest

SRC = __import__('pathlib').Path(__file__).resolve().parent.parent


class _Filter:
    def __and__(self, other):
        return self

    def __or__(self, other):
        return self


class _Filters:
    text = _Filter()
    private = _Filter()

    @staticmethod
    def command(*args, **kwargs):
        return _Filter()


class _FakeApp:
    def __init__(self):
        self.sent = []

    def on_message(self, *args, **kwargs):
        def decorator(function):
            return function

        return decorator

    async def get_users(self, user_id):
        return types.SimpleNamespace(id=user_id, first_name='Target', username=None)

    async def send_message(self, user_id, text):
        self.sent.append((user_id, text))


def _fake_message(user_id, text):
    replies = []

    class FakeMessage:
        def __init__(self):
            self.from_user = types.SimpleNamespace(
                id=user_id, first_name='Sender', username=None,
            )
            self.text = text

        async def reply_text(self, t, **kwargs):
            replies.append(t)

    return FakeMessage(), replies


@pytest.fixture
def premium_stats_modules(monkeypatch):
    pyrogram = types.ModuleType('pyrogram')
    pyrogram.Client = object
    pyrogram.filters = _Filters()
    monkeypatch.setitem(sys.modules, 'pyrogram', pyrogram)

    app = _FakeApp()
    shared_client = types.ModuleType('shared_client')
    shared_client.app = app
    shared_client.userbot = None
    shared_client._WORKDIR = '/persistent'
    monkeypatch.setitem(sys.modules, 'shared_client', shared_client)

    config = types.ModuleType('config')
    config.OWNER_ID = [7]
    config.PAY_NOTICE = 'PAY-NOTICE-PLACEHOLDER'
    monkeypatch.setitem(sys.modules, 'config', config)

    func = types.ModuleType('utils.func')
    # 2026-09-01 12:00 UTC + 5:30 IST == 2026-09-01 17:30 IST.
    expiry = datetime(2026, 9, 1, 12, 0, 0)
    state = {
        'expiry': expiry,
        'premium': True,
        'collection': None,
    }

    async def add_premium_user(user_id, value, unit):
        return True, state['expiry']

    async def get_premium_details(user_id):
        if not state['premium']:
            return None
        return {'subscription_end': state['expiry']}

    async def get_user_data(user_id):
        return None

    async def is_premium_user(user_id):
        # Only the sender (42) holds the plan; the transfer target must not.
        return state['premium'] and user_id == 42

    def get_display_name(entity):
        return entity.first_name

    class FakeCollection:
        async def delete_one(self, query):
            return types.SimpleNamespace(deleted_count=1)

        async def update_one(self, *args, **kwargs):
            return None

        async def insert_one(self, doc):
            return None

    func.add_premium_user = add_premium_user
    func.get_premium_details = get_premium_details
    func.get_user_data = get_user_data
    func.is_premium_user = is_premium_user
    func.get_display_name = get_display_name
    func.premium_users_collection = FakeCollection()
    utils_pkg = types.ModuleType('utils')
    utils_pkg.__path__ = [str(SRC / 'utils')]
    utils_pkg.func = func
    monkeypatch.setitem(sys.modules, 'utils', utils_pkg)
    monkeypatch.setitem(sys.modules, 'utils.func', func)

    start_stub = types.ModuleType('plugins.start')
    start_stub.subscribe = lambda *a, **k: None
    plugins_pkg = types.ModuleType('plugins')
    plugins_pkg.__path__ = [str(SRC / 'plugins')]
    plugins_pkg.start = start_stub
    monkeypatch.setitem(sys.modules, 'plugins', plugins_pkg)
    monkeypatch.setitem(sys.modules, 'plugins.start', start_stub)

    for name in ('plugins.premium', 'plugins.stats'):
        sys.modules.pop(name, None)
    import importlib
    premium = importlib.import_module('plugins.premium')
    stats = importlib.import_module('plugins.stats')
    return premium, stats, app, state


def test_add_premium_renders_expiry_date(premium_stats_modules):
    premium, _, app, _ = premium_stats_modules
    message, replies = _fake_message(7, '/add 123456 1 days')

    asyncio.run(premium.add_premium_handler(None, message))

    assert len(replies) == 1
    assert '01-Sep-2026 05:30:00 PM (IST)' in replies[0]
    # The owner notification to the target renders the same timestamp.
    assert app.sent == [(123456, app.sent[0][1])]
    assert '01-Sep-2026 05:30:00 PM (IST)' in app.sent[0][1]


def test_status_renders_expiry_date(premium_stats_modules):
    _, stats, _, _ = premium_stats_modules
    message, replies = _fake_message(42, '/status')

    asyncio.run(stats.status_handler(None, message))

    assert len(replies) == 1
    assert '高级会员有效期至 01-Sep-2026 05:30:00 PM (IST)' in replies[0]


def test_myplan_renders_expiry_date(premium_stats_modules):
    _, stats, _, _ = premium_stats_modules
    message, replies = _fake_message(42, '/myplan')

    asyncio.run(stats.myplan_handler(None, message))

    assert len(replies) == 1
    assert '有效期至：01-Sep-2026 05:30:00 PM (IST)' in replies[0]


def test_myplan_without_plan_shows_pay_notice(premium_stats_modules):
    _, stats, _, state = premium_stats_modules
    state['premium'] = False
    message, replies = _fake_message(42, '/myplan')

    asyncio.run(stats.myplan_handler(None, message))

    assert len(replies) == 1
    assert 'PAY-NOTICE-PLACEHOLDER' in replies[0]


def test_transfer_gift_message_renders_expiry_date(premium_stats_modules):
    _, stats, app, _ = premium_stats_modules
    message, replies = _fake_message(42, '/transfer 123456')

    asyncio.run(stats.transfer_premium_handler(None, message))

    assert len(replies) == 1
    assert '已成功转赠' in replies[0]
    # Two notifications: the gift to the target (IST-suffixed) and the owner
    # audit message (same timestamp, no suffix) — both strftime sites pinned.
    assert len(app.sent) == 2
    assert app.sent[0][0] == 123456
    assert '01-Sep-2026 05:30:00 PM (IST)' in app.sent[0][1]
    assert app.sent[1][0] == 7
    assert '到期时间：01-Sep-2026 05:30:00 PM' in app.sent[1][1]
