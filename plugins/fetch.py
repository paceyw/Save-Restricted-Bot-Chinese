# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.
import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from pyrogram import Client
from pyrogram.errors import FloodWait
from config import API_ID, API_HASH, STRING
from shared_client import _WORKDIR
from utils.encrypt import dcs
from utils.func import get_user_data, get_user_data_key, cred_epoch
try:
    from utils.func import migrate_user_bot_token
except ImportError:
    migrate_user_bot_token = None

logger = logging.getLogger(__name__)
_PLAINTEXT_BOT_TOKEN_PATTERN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
_CLIENT_IDLE_TTL = 1800
_LRU_MAXSIZE = 1000
_PEER_CACHE_TTL = 24 * 3600
_PEER_CACHE_MAX = 500
class _BoundedLRU(OrderedDict):
    """Ordered mapping with a hard upper bound and access-order refresh."""

    def __init__(self, maxsize):
        super().__init__()
        self.maxsize = maxsize

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.maxsize:
            self.popitem(last=False)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


    def setdefault(self, key, default=None):
        if key in self:
            return self[key]
        self[key] = default
        return default

    def update(self, *args, **kwargs):
        if len(args) > 1:
            raise TypeError(f'update expected at most 1 argument, got {len(args)}')
        if args:
            other = args[0]
            if hasattr(other, 'keys'):
                for key in other:
                    self[key] = other[key]
            else:
                for key, value in other:
                    self[key] = value
        for key, value in kwargs.items():
            self[key] = value

premium_userbot = None if not STRING else __import__('shared_client').userbot
user_bots, user_clients = {}, {}
# Build-epoch tags: a cached client is only served while its build epoch still
# matches the current credential epoch (see utils.func._CRED_EPOCH). Any
# credential mutation invalidates the entry on the next touch, even if the
# mutation committed while its writer was suspended mid-await.
_UB_EPOCH, _UC_EPOCH = {}, {}
fetch_origin = _BoundedLRU(_LRU_MAXSIZE)
_LINKED_CHAT = _BoundedLRU(_LRU_MAXSIZE)
_CLIENT_LAST_USED = {}
_PEER_CACHE: dict[int, dict[str, tuple[str, float]]] = {}


def _peer_cache_get(uid, candidate_keys, now):
    """Return the first live ``(key, form)`` candidate and prune stale keys."""
    peers = _PEER_CACHE.get(uid)
    if not peers:
        return None

    for candidate in candidate_keys:
        key = str(candidate)
        entry = peers.get(key)
        if entry is None:
            continue
        form, expiry = entry
        if expiry <= now:
            peers.pop(key, None)
            continue
        return key, str(form)

    if not peers:
        _PEER_CACHE.pop(uid, None)
    return None


def _peer_cache_put(uid, keys, form, now):
    """Store aliases for a successful peer form, enforcing the per-user cap."""
    peers = _PEER_CACHE.setdefault(uid, {})
    value = (str(form), now + _PEER_CACHE_TTL)
    for key in keys:
        peers[str(key)] = value

    while len(peers) > _PEER_CACHE_MAX:
        oldest_key = min(peers, key=lambda candidate: peers[candidate][1])
        peers.pop(oldest_key, None)
    if not peers:
        _PEER_CACHE.pop(uid, None)


def _peer_cache_drop(uid, key):
    """Drop a stale peer form and all aliases that point to it."""
    peers = _PEER_CACHE.get(uid)
    if not peers:
        return
    entry = peers.pop(str(key), None)
    if entry is not None:
        stale_form = str(entry[0])
        for alias, candidate in list(peers.items()):
            if str(candidate[0]) == stale_form:
                peers.pop(alias, None)
    if not peers:
        _PEER_CACHE.pop(uid, None)


def _int_if_numeric(form):
    """Return ``form`` as an int when it is a numeric chat reference.

    pyrofork's peer resolver only runs its ``channels.GetChannels``
    (access_hash=0) fallback for INT ids; numeric STRINGS take the
    phone-number path and raise PeerIdInvalid when the session has no cached
    access hash (channel not among recent dialogs) — turning a fetchable
    message into 未找到消息.
    """
    try:
        return int(form)
    except (TypeError, ValueError):
        return form



 
async def upd_dlg(c):
    try:
        async for _ in c.get_dialogs(limit=100): pass
        return True
    except Exception as e:
        print(f'Failed to update dialogs: {e}')
        return False

# fixed the old group of 2021-2022 extraction 🌝 (buy krne ka fayda nhi ab old group) ✅ 
async def resolve_linked_chat(client, channel):
    """Resolve a channel's linked discussion group Chat, cached per channel.

    ``get_chat`` triggers a cross-DC auth-key round-trip. Calling it per-link
    (common when merging multiple comment links from one channel) causes
    FLOOD_WAIT on the auth subsystem. The cache ensures one resolution per
    channel, process-wide.
    """
    if channel in _LINKED_CHAT:
        return _LINKED_CHAT[channel]
    try:
        chat = await client.get_chat(channel)
        linked = getattr(chat, 'linked_chat', None)
    except FloodWait:
        raise
    except Exception as e:
        print(f'Failed to resolve linked chat for {channel}: {e}')
        linked = None
    _LINKED_CHAT[channel] = linked
    return linked


async def get_msg(c, u, i, d, lt, uid, comment_id=None):
    # fetch_origin is keyed per (uid, channel): concurrent users fetching the same
    # channel must not overwrite each other's source-client marker.
    try:
        if comment_id:
            # Comment link (?comment=N): the message lives in the channel's
            # linked discussion group, NOT the channel itself. Resolve it,
            # then fetch the comment message from there.
            fetch_client = u or c
            if not fetch_client:
                return None
            linked = await resolve_linked_chat(fetch_client, i)
            if not linked:
                print(f'Channel {i} has no linked discussion group')
                return None
            try:
                xm = await fetch_client.get_messages(linked.id, comment_id)
            except FloodWait:
                raise
            except Exception as e:
                print(f'Failed to fetch comment {comment_id} from discussion group: {e}')
                return None
            if xm and not getattr(xm, 'empty', False):
                fetch_origin[(uid, linked.id)] = True
                return xm
            return None

        if lt == 'public':
            clients = []
            if u:
                clients.append(('user', u, False))
            if c and c is not u:
                clients.append(('bot', c, True))

            for label, client, fetched_by_bot in clients:
                try:
                    xm = await client.get_messages(i, d)
                except FloodWait:
                    raise  # let process_one_link's retry wrapper see it
                except Exception as e:
                    print(f'Error fetching public message with {label} client: {e}')
                    continue

                if xm and not getattr(xm, 'empty', False):
                    # fetch_origin is looked up downstream by numeric chat id
                    # (msg.chat.id) — record both keys: for public links
                    # ``i`` is the username, which would never match.
                    fetch_origin[(uid, i)] = not fetched_by_bot
                    if getattr(xm, 'chat', None):
                        fetch_origin[(uid, xm.chat.id)] = not fetched_by_bot
                    print(f'Fetched public message with {label} client')
                    return xm

            if u:
                try:
                    await u.join_chat(i)
                    chat = await u.get_chat(f'@{i}')
                    xm = await u.get_messages(chat.id, d)
                    if xm and not getattr(xm, 'empty', False):
                        fetch_origin[(uid, i)] = True
                        fetch_origin[(uid, chat.id)] = True
                        return xm
                except FloodWait:
                    raise
                except Exception as e:
                    print(f'Error joining public chat {i}: {e}')

            return None

        if not u:
            return None

        try:
            i_key = str(i)
            if i_key.startswith('-100'):
                chat_id_100 = i_key
                base_id = i_key[4:]
                chat_id_dash = f"-{base_id}"
            elif i_key.isdigit():
                chat_id_100 = f"-100{i_key}"
                chat_id_dash = f"-{i_key}"
            else:
                chat_id_100 = i_key
                chat_id_dash = i_key

            cache_hit = _peer_cache_get(
                uid, (i_key, chat_id_100, chat_id_dash), time.time()
            )
            if cache_hit:
                cache_key, cached_form = cache_hit
                try:
                    result = await u.get_messages(_int_if_numeric(cached_form), d)
                    if result and not getattr(result, "empty", False):
                        _peer_cache_put(
                            uid, (i_key,), cached_form, time.time()
                        )
                        return result
                except FloodWait:
                    raise
                except Exception:
                    pass
                _peer_cache_drop(uid, cache_key)

            async for _ in u.get_dialogs(limit=50):
                pass

            # Numeric forms must reach pyrogram as ints (see _int_if_numeric):
            # string ids never trigger the channel-resolution fallback.
            chat_id_100 = _int_if_numeric(chat_id_100)
            chat_id_dash = _int_if_numeric(chat_id_dash)

            # Try with -100 prefix first
            try:
                result = await u.get_messages(chat_id_100, d)
                if result and not getattr(result, "empty", False):
                    _peer_cache_put(uid, (i_key,), chat_id_100, time.time())
                    return result
            except FloodWait:
                raise
            except Exception:
                pass

            try:
                result = await u.get_messages(chat_id_dash, d)
                if result and not getattr(result, "empty", False):
                    _peer_cache_put(uid, (i_key,), chat_id_dash, time.time())
                    return result
            except FloodWait:
                raise
            except Exception:
                pass

            try:
                async for _ in u.get_dialogs(limit=200):
                    pass
                result = await u.get_messages(_int_if_numeric(i), d)
                if result and not getattr(result, "empty", False):
                    _peer_cache_put(uid, (i_key,), str(i), time.time())
                    return result
            except FloodWait:
                raise
            except Exception:
                pass

            return None
        except FloodWait:
            raise
        except Exception as e:
            print(f'Private channel error: {e}')
            return None
    except FloodWait:
        raise
    except Exception as e:
        print(f'Error fetching message: {e}')
        return None


_UB_UC_LOCKS = {}

def _client_lock(uid):
    # Per-user lock serializing user_bots/user_clients creation: concurrent updates must not
    # start two clients sharing one session file.
    lock = _UB_UC_LOCKS.get(uid)
    if lock is None:
        lock = _UB_UC_LOCKS[uid] = asyncio.Lock()
    return lock


async def get_ubot(uid, prefetched=None, prefetched_epoch=None):
    from plugins import tasks as tasks_module
    tasks_module._ensure_sweeper()

    def finish(value, touch=True):
        if touch and value is not None:
            _CLIENT_LAST_USED[uid] = time.time()
        return value

    async with _client_lock(uid):
        now_epoch = cred_epoch(uid)
        if uid in user_bots:
            if _UB_EPOCH.get(uid) == now_epoch:
                return finish(user_bots.get(uid))
            # Credentials rotated after this client was built (epoch bumped):
            # evict under the same lock, then rebuild from fresh data below.
            stale = user_bots.pop(uid, None)
            _UB_EPOCH.pop(uid, None)
            if stale is not None:
                try:
                    await asyncio.wait_for(stale.stop(), timeout=10)
                except Exception:
                    pass

        # Dispatch-time document reuse keeps the whole task at one find_one —
        # but only while the credential epoch still matches inside this lock.
        # A concurrent /setbot, /rembot, login or logout bumps the epoch, and
        # the stale prefetch is then discarded for a fresh locked read.
        if prefetched is not None and prefetched_epoch == now_epoch:
            stored_bt = prefetched.get("bot_token")
            used_epoch = prefetched_epoch
        else:
            # Same capture-before-read rule as dispatch (conservative pairing).
            used_epoch = now_epoch
            stored_bt = await get_user_data_key(uid, "bot_token", None)
        try:
            bt = dcs(stored_bt)
        except Exception as e:
            candidate = stored_bt if isinstance(stored_bt, str) else None
            if candidate and _PLAINTEXT_BOT_TOKEN_PATTERN.fullmatch(candidate):
                bt = candidate
                if migrate_user_bot_token is not None:
                    try:
                        migrated = await migrate_user_bot_token(uid, candidate)
                    except Exception as migration_error:
                        logger.warning(
                            "Error migrating plaintext bot token for user %s; "
                            "using current plaintext token: %s",
                            uid,
                            migration_error,
                        )
                    else:
                        if migrated is not False:
                            # Our own migration bumps the epoch — the data is
                            # re-synchronized, not externally rotated.
                            used_epoch = cred_epoch(uid)
                        if migrated is False:
                            try:
                                # Capture-before-read: pairs with whatever the
                                # re-read returns, conservatively.
                                cas_epoch = cred_epoch(uid)
                                current_bt = await get_user_data_key(
                                    uid, "bot_token", None
                                )
                                used_epoch = cas_epoch
                            except Exception as current_error:
                                logger.warning(
                                    "Error re-reading bot token for user %s; "
                                    "using current plaintext token: %s",
                                    uid,
                                    current_error,
                                )
                            else:
                                if current_bt == candidate:
                                    logger.warning(
                                        "Bot token migration race for user %s; "
                                        "using current plaintext token",
                                        uid,
                                    )
                                else:
                                    try:
                                        bt = dcs(current_bt)
                                    except Exception as current_error:
                                        logger.error(
                                            "Invalid current bot token for user %s: %s",
                                            uid,
                                            current_error,
                                        )
                                        return finish(None)
            else:
                if stored_bt:
                    logger.error("Invalid stored bot token for user %s: %s", uid, e)
                bt = None
        if isinstance(bt, str):
            bt = bt.strip()
        if not bt:
            return finish(None)

        bot = None
        try:
            bot = Client(
                f"user_{uid}",
                bot_token=bt,
                api_id=API_ID,
                api_hash=API_HASH,
                workdir=_WORKDIR,
            )
            await bot.start()
            if used_epoch != cred_epoch(uid):
                # Rotation landed during the (slow) start — never cache stale.
                await bot.stop()
                return finish(None)
            user_bots[uid] = bot
            _UB_EPOCH[uid] = used_epoch
            return finish(bot)
        except Exception as e:
            if bot is not None:
                try:
                    await bot.stop()
                except Exception:
                    pass
            print(f"Error starting bot for user {uid}: {e}")
            return finish(None)

async def get_uclient(uid, prefetched=None, prefetched_epoch=None):
    from plugins import tasks as tasks_module
    tasks_module._ensure_sweeper()

    def finish(value, touch=True):
        if touch and value is not None:
            _CLIENT_LAST_USED[uid] = time.time()
        return value

    # Cache hit: the build-epoch tag proves the cached client predates no
    # credential rotation (e.g. /settings addsession bumps the epoch without
    # stopping user_clients — a stale tag forces eviction and rebuild below). This is
    # equivalent to locking mutations: post-commit lookups never serve
    # pre-commit clients.
    now_epoch = cred_epoch(uid)
    if uid in user_clients and _UC_EPOCH.get(uid) == now_epoch:
        return finish(user_clients.get(uid))
    if prefetched is not None and prefetched_epoch == now_epoch:
        ud = prefetched or None
        used_epoch = prefetched_epoch
    elif prefetched is not None:
        # Rotation raced the dispatch read: discard the stale prefetch.
        # Epoch captured before the read (conservative pairing, see dispatch).
        used_epoch = cred_epoch(uid)
        ud = await get_user_data(uid)
    else:
        used_epoch = cred_epoch(uid)
        ud = await get_user_data(uid)
    ubot = await get_ubot(uid, prefetched=prefetched, prefetched_epoch=prefetched_epoch)
    if uid in user_clients and _UC_EPOCH.get(uid) == cred_epoch(uid):
        return finish(user_clients.get(uid))
    if not ud:
        return finish(ubot if ubot else None)
    xxx = ud.get('session_string')
    if xxx:
        async with _client_lock(uid):
            if uid in user_clients:
                if _UC_EPOCH.get(uid) == cred_epoch(uid):
                    return finish(user_clients.get(uid))
                # Possibly built from pre-rotation credentials: evict, rebuild.
                stale = user_clients.pop(uid, None)
                _UC_EPOCH.pop(uid, None)
                if stale is not None and stale is not premium_userbot:
                    try:
                        await asyncio.wait_for(stale.stop(), timeout=10)
                    except Exception:
                        pass
            # Same epoch guard as get_ubot: a login/logout racing the task
            # invalidates the session inside this lock; the fresh read happens
            # only on actual rotation (rare), never steady-state.
            if used_epoch != cred_epoch(uid):
                used_epoch = cred_epoch(uid)
                ud = await get_user_data(uid)
                xxx = (ud or {}).get('session_string')
                if not xxx:
                    return finish(ubot if ubot else None)
            try:
                ss = dcs(xxx)
                gg = Client(f'{uid}_client', api_id=API_ID, api_hash=API_HASH, device_model="v3saver", session_string=ss, workdir=_WORKDIR)
                await gg.start()
                await upd_dlg(gg)
                if used_epoch != cred_epoch(uid):
                    # Rotation landed during start/upd_dlg — never cache stale.
                    await gg.stop()
                    return finish(ubot if ubot else None)
                user_clients[uid] = gg
                _UC_EPOCH[uid] = used_epoch
                return finish(gg)
            except Exception as e:
                print(f'User client error: {e}')
                return finish(None)
    return finish(premium_userbot, touch=False)
