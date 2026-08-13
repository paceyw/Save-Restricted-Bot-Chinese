import asyncio
import time
import types

import pytest

from tests.test_custom_bot_flow import batch_module


@pytest.fixture
def peer_batch_module(batch_module):
    module, _ = batch_module
    module._PEER_CACHE.clear()
    module.UB.clear()
    module.UC.clear()
    module._CLIENT_LAST_USED.clear()
    return module


class _PrivateClient:
    def __init__(self, success_forms=()):
        self.dialog_calls = []
        self.message_calls = []
        self.success_forms = set(success_forms)

    async def get_dialogs(self, limit):
        self.dialog_calls.append(limit)
        if False:
            yield None

    async def get_messages(self, chat_id, message_id):
        self.message_calls.append(chat_id)
        if chat_id in self.success_forms:
            return types.SimpleNamespace(empty=False, id=message_id)
        raise RuntimeError(f"peer unavailable: {chat_id}")


def test_private_cache_hit_skips_dialog_preheat(peer_batch_module):
    module = peer_batch_module
    uid = 101
    module._peer_cache_put(uid, ("123",), "-100123", time.time())
    client = _PrivateClient(success_forms={"-100123"})

    result = asyncio.run(module.get_msg(None, client, "123", 7, "private", uid))

    assert result is not None
    assert client.dialog_calls == []
    assert client.message_calls == ["-100123"]


def test_private_cache_miss_fills_then_hits(peer_batch_module):
    module = peer_batch_module
    uid = 102
    client = _PrivateClient(success_forms={"-100123"})

    first = asyncio.run(module.get_msg(None, client, "123", 7, "private", uid))
    assert first is not None
    assert client.dialog_calls == [50]
    assert set(module._PEER_CACHE[uid]) == {"123"}

    second = asyncio.run(module.get_msg(None, client, "123", 8, "private", uid))
    assert second is not None
    assert client.dialog_calls == [50]
    assert client.message_calls[-1] == "-100123"


def test_private_batch_ten_messages_preheats_at_most_once(peer_batch_module):
    module = peer_batch_module
    uid = 103
    client = _PrivateClient(success_forms={"-100123"})

    for message_id in range(10):
        result = asyncio.run(
            module.get_msg(None, client, "123", message_id, "private", uid)
        )
        assert result is not None

    assert len(client.dialog_calls) <= 1


def test_invalid_cached_peer_drops_and_uses_full_fallback(peer_batch_module):
    module = peer_batch_module
    uid = 104
    module._peer_cache_put(uid, ("123",), "stale-peer", time.time())
    client = _PrivateClient(success_forms={"123"})

    result = asyncio.run(module.get_msg(None, client, "123", 7, "private", uid))

    assert result is not None
    assert client.dialog_calls == [50, 200]
    assert client.message_calls == ["stale-peer", "-100123", "-123", "123"]
    assert module._PEER_CACHE[uid]["123"][0] == "123"


def test_expired_peer_is_ignored_and_replaced(peer_batch_module):
    module = peer_batch_module
    uid = 105
    now = time.time()
    module._PEER_CACHE[uid] = {"123": ("-100123", now - 1)}
    client = _PrivateClient(success_forms={"-100123"})

    result = asyncio.run(module.get_msg(None, client, "123", 7, "private", uid))

    assert result is not None
    assert client.dialog_calls == [50]
    assert module._PEER_CACHE[uid]["123"][0] == "-100123"
    assert module._PEER_CACHE[uid]["123"][1] > now


def test_sweeper_evicts_peer_cache_with_client_and_expires_entries(peer_batch_module):
    module = peer_batch_module
    now = time.time()
    evicted_uid = 106
    expired_uid = 107

    class Client:
        def __init__(self):
            self.stop_calls = 0

        async def stop(self):
            self.stop_calls += 1

    client = Client()
    module.UB[evicted_uid] = client
    module._CLIENT_LAST_USED[evicted_uid] = now - module._CLIENT_IDLE_TTL - 1
    module._PEER_CACHE[evicted_uid] = {"123": ("-100123", now + 100)}
    module._PEER_CACHE[expired_uid] = {"456": ("-100456", now - 1)}

    asyncio.run(module._sweep_once(now=now))

    assert evicted_uid not in module._PEER_CACHE
    assert expired_uid not in module._PEER_CACHE
    assert client.stop_calls == 1


def test_linked_chat_resolution_is_cached(peer_batch_module):
    module = peer_batch_module
    calls = []

    class Client:
        async def get_chat(self, channel):
            calls.append(channel)
            return types.SimpleNamespace(linked_chat=types.SimpleNamespace(id=-100999))

    async def scenario():
        first = await module.resolve_linked_chat(Client(), "channel")
        second = await module.resolve_linked_chat(Client(), "channel")
        return first, second

    first, second = asyncio.run(scenario())

    assert first.id == second.id == -100999
    assert calls == ["channel"]


def test_flood_wait_on_cached_peer_propagates(peer_batch_module):
    module = peer_batch_module
    uid = 108
    module._peer_cache_put(uid, ("123",), "-100123", time.time())

    class Client(_PrivateClient):
        async def get_messages(self, chat_id, message_id):
            self.message_calls.append(chat_id)
            raise module.FloodWait()

    client = Client()

    with pytest.raises(module.FloodWait):
        asyncio.run(module.get_msg(None, client, "123", 7, "private", uid))

    assert client.dialog_calls == []
    assert client.message_calls == ["-100123"]
