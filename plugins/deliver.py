# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.
import asyncio
import os
import time
from pyrogram.errors import FloodWait
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from config import LOG_GROUP, MAX_FLOOD_RETRIES, UPLOAD_INTERVAL
try:
    from config import PROGRESS_MIN_INTERVAL
except ImportError:
    # Keep lightweight legacy config shims importable until they expose the
    # progress-throttle setting.
    PROGRESS_MIN_INTERVAL = 3.0
from shared_client import app as main_bot, _WORKDIR
from utils.func import (
    apply_text_rules, screenshot, thumbnail, get_video_metadata,
    ensure_audio_track, touch_file, VIDEO_EXTENSIONS, AUDIO_EXTENSIONS,
)
from plugins.fetch import (
    fetch_origin, get_msg, resolve_linked_chat, upd_dlg, premium_userbot,
)
from plugins.tasks import sanitize

progress_state = {}
_PROGRESS_TTL = 3600
async def prog(c, t, C, h, m, st, fp=None):
    global progress_state
    if fp:
        # Upload-only heartbeat: keep the source file's mtime fresh so the
        # hourly stale-downloads sweep never deletes it mid-upload.
        touch_file(fp)
    p = c / t * 100
    interval = 10 if t >= 100 * 1024 * 1024 else 20 if t >= 50 * 1024 * 1024 else 30 if t >= 10 * 1024 * 1024 else 50
    step = int(p // interval) * interval
    now = time.time()
    previous = progress_state.get(m)
    previous_ts = previous[1] if isinstance(previous, tuple) else None
    if (
        m not in progress_state
        or previous_ts is None
        or now - previous_ts >= PROGRESS_MIN_INTERVAL
        or p >= 100
    ):
        progress_state[m] = (step, now)
        c_mb = c / (1024 * 1024)
        t_mb = t / (1024 * 1024)
        bar = '🟢' * int(p / 10) + '🔴' * (10 - int(p / 10))
        speed = c / (time.time() - st) / (1024 * 1024) if time.time() > st else 0
        eta = time.strftime('%M:%S', time.gmtime((t - c) / (speed * 1024 * 1024))) if speed > 0 else '00:00'
        await C.edit_message_text(h, m, f"__**Pyro 处理器...**__\n\n{bar}\n\n⚡**__已完成__**：{c_mb:.2f} MB / {t_mb:.2f} MB\n📊 **__完成度__**：{p:.2f}%\n🚀 **__速度__**：{speed:.2f} MB/s\n⏳ **__预计剩余时间__**：{eta}\n\n**__由 Team SPY 提供支持__**")
        if p >= 100: progress_state.pop(m, None)


async def send_direct(c, m, tcid, ft=None, rtmid=None):
    try:
        if m.video:
            await c.send_video(
                tcid,
                m.video.file_id,
                caption=ft,
                duration=m.video.duration,
                width=m.video.width,
                height=m.video.height,
                reply_to_message_id=rtmid,
            )
        elif m.video_note:
            await c.send_video_note(
                tcid,
                m.video_note.file_id,
                reply_to_message_id=rtmid,
            )
        elif m.voice:
            await c.send_voice(
                tcid,
                m.voice.file_id,
                reply_to_message_id=rtmid,
            )
        elif m.sticker:
            await c.send_sticker(
                tcid,
                m.sticker.file_id,
                reply_to_message_id=rtmid,
            )
        elif m.audio:
            await c.send_audio(
                tcid,
                m.audio.file_id,
                caption=ft,
                duration=m.audio.duration,
                performer=m.audio.performer,
                title=m.audio.title,
                reply_to_message_id=rtmid,
            )
        elif m.photo:
            photo_id = (
                m.photo.file_id
                if hasattr(m.photo, 'file_id')
                else m.photo[-1].file_id
            )
            await c.send_photo(
                tcid,
                photo_id,
                caption=ft,
                reply_to_message_id=rtmid,
            )
        elif m.document:
            await c.send_document(
                tcid,
                m.document.file_id,
                caption=ft,
                file_name=m.document.file_name,
                reply_to_message_id=rtmid,
            )
        else:
            return False, '消息没有可直接发送的媒体'
        return True, None
    except Exception as e:
        error = str(e)
        print(f'Direct send error: {error}')
        return False, error

async def resolve_delivery(d, settings):
    """Resolve the delivery target for user chat ``d``.

    Priority:
    1. /settings chat_id (per-user, custom bot must be admin there)
    2. LOG_GROUP from .env (deployment-level channel, custom bot must be a member)
    3. the user's own chat (fallback, delivered via the user client)

    Returns (tcid, rtmid, deliver_via_bot).
    """
    cfg_chat = settings.get('chat_id')
    if cfg_chat is not None:
        cfg_chat = str(cfg_chat).strip()
    tcid = d
    rtmid = None
    deliver_via_bot = False
    if cfg_chat:
        if '/' in cfg_chat:
            parts = cfg_chat.split('/', 1)
            tcid = int(parts[0])
            rtmid = int(parts[1]) if len(parts) > 1 else None
        else:
            tcid = int(cfg_chat)
        deliver_via_bot = True
    elif LOG_GROUP:
        tcid = LOG_GROUP
        deliver_via_bot = True
    elif isinstance(tcid, str):
        try:
            tcid = int(tcid)
        except ValueError:
            pass
    return tcid, rtmid, deliver_via_bot


async def _send_album_item(sender, tcid, im, rtmid):
    """Send one InputMedia item individually (fallback when SendMultiMedia rejects the group)."""
    touch_file(getattr(im, 'media', None))
    cap = getattr(im, 'caption', None)
    if isinstance(im, InputMediaPhoto):
        return await sender.send_photo(tcid, im.media, caption=cap, reply_to_message_id=rtmid)
    if isinstance(im, InputMediaVideo):
        return await sender.send_video(
            tcid, im.media, caption=cap, duration=im.duration,
            width=im.width, height=im.height, thumb=im.thumb,
            reply_to_message_id=rtmid,
        )
    if isinstance(im, InputMediaAudio):
        return await sender.send_audio(tcid, im.media, caption=cap, duration=im.duration,
                                       reply_to_message_id=rtmid)
    return await sender.send_document(tcid, im.media, caption=cap, reply_to_message_id=rtmid)

async def _send_album_items(sender, tcid, media, rtmid):
    sent = 0
    for im in media:
        flood_seen = False

        async def send_item():
            nonlocal flood_seen
            try:
                return await _send_album_item(sender, tcid, im, rtmid)
            except FloodWait:
                flood_seen = True
                raise

        try:
            await with_flood_retry(
                send_item,
                context='per-item album send',
                max_retries=2,
            )
            sent += 1
        except Exception as e2:
            if flood_seen:
                print(f'Per-item send failed after flood wait: {e2}')
            else:
                print(f'Per-item send failed: {e2}')
        await asyncio.sleep(UPLOAD_INTERVAL)
    return sent


def _flood_secs(e):
    return getattr(e, 'value', getattr(e, 'x', 10))


async def with_flood_retry(coro_fn, context='', max_retries=None, on_flood=None):
    """Call ``coro_fn()`` (a zero-arg async factory), retrying on FloodWait.

    Waits the server-requested seconds, then retries. After MAX_FLOOD_RETRIES
    attempts the FloodWait re-raises so the caller can handle or surface it.
    """
    retries = max_retries if max_retries is not None else MAX_FLOOD_RETRIES
    last_exc = None
    for attempt in range(retries):
        try:
            return await coro_fn()
        except FloodWait as e:
            last_exc = e
            secs = _flood_secs(e)
            if attempt < retries - 1:
                print(f'FloodWait {secs}s on {context} (attempt {attempt + 1}/{retries}), waiting')
                if on_flood is not None:
                    try:
                        on_flood(secs)
                    except Exception as hook_error:
                        print(f'FloodWait hook failed on {context}: {hook_error}')
                await asyncio.sleep(secs)
            else:
                print(f'FloodWait {secs}s on {context}: retries exhausted')
    raise last_exc

async def _safe_cleanup(coro):
    """Post-delivery cleanup must never propagate (esp. FloodWait): a raised
    error here would make the caller retry an already-delivered send and
    duplicate the content."""
    try:
        await coro
    except Exception as e:
        print(f'cleanup failed (delivery already done): {e}')


async def _download_media_item(u, one, uid, idx, tag, main_bot, did, p_id, st):
    """Download one message's media and wrap it as an InputMedia for grouping.

    Returns (input_media, [local_files_to_cleanup]); (None, []) when the
    message has no usable media or its download fails. ``tag`` ('album'/
    'merge') namespaces temp filenames so concurrent flows never collide.
    Shared by process_album and process_merged.
    """
    if not (one.photo or one.video or one.document or one.audio):
        return None, []
    # SendMultiMedia validates uploads by file extension (PHOTO_EXT_INVALID
    # otherwise), so the temp name must carry one.
    if one.photo:
        ext = '.jpg'
    elif one.video:
        ext = os.path.splitext(one.video.file_name or '')[1] or '.mp4'
    elif one.audio:
        ext = os.path.splitext(one.audio.file_name or '')[1] or '.mp3'
    else:
        ext = os.path.splitext(one.document.file_name or '')[1]
    f = await u.download_media(
        one,
        file_name=os.path.join(_WORKDIR, 'downloads', f'{tag}_{uid}_{int(time.time())}_{idx}{ext}'),
        progress=prog, progress_args=(main_bot, did, p_id, st),
    )
    if not f:
        print(f'{tag} item {idx + 1} download failed, skipping')
        return None, []
    files = [f]
    if one.video:
        # Videos without an audio track are treated as animations by Telegram;
        # mixed into SendMultiMedia they fail the whole group (MEDIA_EMPTY).
        f = await ensure_audio_track(f)
        files = [f]
        # Keep the source channel's thumbnail; without one Telegram shows the
        # first frame, which is often black.
        thumb_path = None
        if one.video.thumbs:
            try:
                thumb_path = await u.download_media(
                    one.video.thumbs[-1].file_id,
                    file_name=os.path.join(
                        _WORKDIR, 'downloads',
                        f'{tag}_thumb_{uid}_{int(time.time())}_{idx}.jpg',
                    ),
                )
            except Exception as e:
                print(f'Thumb download failed for {tag} item {idx + 1}: {e}')
        if thumb_path:
            files.append(thumb_path)
        return InputMediaVideo(
            f, duration=one.video.duration,
            width=one.video.width, height=one.video.height,
            thumb=thumb_path,
        ), files
    if one.photo:
        return InputMediaPhoto(f), files
    if one.audio:
        return InputMediaAudio(f, duration=one.audio.duration), files
    return InputMediaDocument(f), files

async def process_album(c, u, msgs, d, lt, uid, i, oc=None, *, settings):
    """Forward an album 1:1 — grouping, order, caption and tags preserved.

    Fast path: server-side copy_media_group (works for unrestricted chats).
    Fallback: download every item with the user client and re-upload as ONE
    media group (works for restricted content). Progress reports go to the
    user's chat with the main bot, never to the target channel.
    """
    tcid, rtmid, deliver_via_bot = await resolve_delivery(d, settings)
    sender = c if deliver_via_bot else (u or c)
    did = int(d)
    p = await main_bot.send_message(did, f'正在处理相册（{len(msgs)} 项）...')

    # ``oc`` (override caption) replaces the original text entirely; the
    # /settings default caption (user_cap) is still appended below.
    if oc is not None:
        proc_text = oc
    else:
        orig_caption = next((one.caption.markdown for one in msgs if one.caption), '')
        proc_text = apply_text_rules(
            orig_caption,
            settings.get('replacement_words', {}),
            settings.get('delete_words', []),
        )
    user_cap = settings.get('caption', '')
    ft = f'{proc_text}\n\n{user_cap}' if proc_text and user_cap else user_cap if user_cap else proc_text

    # Fast server-side copy preserves the ORIGINAL caption — skip it whenever
    # text rules or a user caption apply, so both paths produce the same text.
    if deliver_via_bot and not ft:
        try:
            await sender.copy_media_group(tcid, msgs[0].chat.id, msgs[0].id)
            await _safe_cleanup(main_bot.delete_messages(did, p.id))
            return f'✅ 相册已一比一转发（{len(msgs)} 项）'
        except Exception as e:
            print(f'copy_media_group failed, falling back to re-upload: {e}')

    st = time.time()
    media = []
    files = []
    try:
        for idx, one in enumerate(msgs):
            await main_bot.edit_message_text(did, p.id, f'正在下载 {idx + 1}/{len(msgs)}...')
            im, ifiles = await _download_media_item(u, one, uid, idx, 'album', main_bot, did, p.id, st)
            if im is None:
                continue
            media.append(im)
            files.extend(ifiles)
    except Exception:
        # A failed download/progress step must not leak already-downloaded
        # files. FloodWait propagates too — the retry re-downloads cleanly.
        for ff in files:
            if os.path.exists(ff):
                os.remove(ff)
        raise

    if not media:
        await main_bot.edit_message_text(did, p.id, '相册下载失败')
        return '❌ 相册下载失败'

    if ft:
        media[0].caption = ft

    await main_bot.edit_message_text(did, p.id, f'正在上传相册（{len(media)} 项）...')
    # send_media_group has no progress hook: refresh mtimes once at upload start
    # so a long group upload is not mistaken for stale corpses by the sweeper.
    for ff in files:
        touch_file(ff)
    upload_error = None
    try:
        await sender.send_media_group(tcid, media, reply_to_message_id=rtmid)
    except TypeError as e:
        if 'keyword-only argument' in str(e):
            # pyrofork 2.3.69 breaks parsing the SendMultiMedia response AFTER
            # the RPC already succeeded — the album is already delivered.
            # Treat the parse bug as success.
            print(f'send_media_group response parse bug (treating as success): {e}')
        else:
            upload_error = str(e)
    except Exception as e:
        upload_error = str(e)

    if upload_error:
        err = upload_error
        # Telegram rejects some groups (e.g. MEDIA_EMPTY when a no-audio-track
        # video is treated as an animation and mixed into an album). Sending the
        # items individually still delivers the good ones — partial success
        # beats total failure.
        print(f'send_media_group failed ({err}), falling back to per-item sends')
        sent = await _send_album_items(sender, tcid, media, rtmid)
        for f in files:
            if os.path.exists(f):
                os.remove(f)
        if sent:
            await _safe_cleanup(main_bot.delete_messages(did, p.id))
            return f'⚠️ 整组发送被拒，已逐条发送 {sent}/{len(media)} 项（{err[:40]}）'
        if 'PEER_ID_INVALID' in err or 'CHAT_WRITE_FORBIDDEN' in err or 'ADMIN' in err.upper():
            hint = '请将 /setbot 的机器人加入目标频道并授予发帖权限。'
        else:
            hint = ''
        await main_bot.edit_message_text(did, p.id, f'相册上传失败：{err[:60]} {hint}')
        return f'❌ 相册上传失败：{err[:60]}'

    for f in files:
        if os.path.exists(f):
            os.remove(f)
    await _safe_cleanup(main_bot.delete_messages(did, p.id))
    return f'✅ 相册已发送（{len(media)} 项）'

async def process_merged(c, u, msgs, d, uid, oc=None, *, settings):
    """Merge multiple fetched messages into ONE delivery.

    All media (photo/video/audio/document) across every message is re-uploaded
    as a single album — chunked into groups of <= 10 (Telegram's media-group
    limit). All text (standalone text messages + media captions) is combined
    into one block: used as the album caption when it fits (<= 1024 chars),
    otherwise sent as a standalone message after the album.  When ``oc`` is
    provided it replaces the combined original text entirely.
    """
    tcid, rtmid, deliver_via_bot = await resolve_delivery(d, settings)
    sender = c if deliver_via_bot else (u or c)
    did = int(d)
    p = await main_bot.send_message(did, f'正在合并 {len(msgs)} 条消息...')

    # Partition into media items and text pieces; media captions count as text.
    media_msgs = []
    text_pieces = []
    for one in msgs:
        if one.media and (one.photo or one.video or one.document or one.audio):
            media_msgs.append(one)
            if one.caption:
                text_pieces.append(one.caption.markdown)
        elif one.text:
            text_pieces.append(one.text.markdown)

    if oc is not None:
        proc_text = oc
    else:
        combined = '\n\n'.join(tp for tp in text_pieces if tp)
        proc_text = apply_text_rules(
            combined,
            settings.get('replacement_words', {}),
            settings.get('delete_words', []),
        )
    user_cap = settings.get('caption', '')
    ft = f'{proc_text}\n\n{user_cap}' if proc_text and user_cap else user_cap if user_cap else proc_text

    # No media: send the combined text as one (or chunked) message(s).
    if not media_msgs:
        if not ft:
            await main_bot.edit_message_text(did, p.id, '没有可合并的内容。')
            return '❌ 没有可合并的内容'
        for i in range(0, len(ft), 4096):
            await sender.send_message(tcid, text=ft[i:i + 4096], reply_to_message_id=rtmid)
        await _safe_cleanup(main_bot.delete_messages(did, p.id))
        return '✅ 文字已合并发送'

    # Download every media item.
    st = time.time()
    media = []
    files = []
    try:
        for idx, one in enumerate(media_msgs):
            await main_bot.edit_message_text(did, p.id, f'正在下载 {idx + 1}/{len(media_msgs)}...')
            flood_seen = False

            async def download_item():
                nonlocal flood_seen
                try:
                    return await _download_media_item(
                        u, one, uid, idx, 'merge', main_bot, did, p.id, st
                    )
                except FloodWait as e:
                    flood_seen = True
                    secs = _flood_secs(e)
                    print(f'FloodWait {secs}s downloading merge item {idx + 1}, waiting')
                    await main_bot.edit_message_text(
                        did, p.id, f'Telegram 限流，等待 {secs}s 后重试...'
                    )
                    raise

            try:
                im, ifiles = await with_flood_retry(
                    download_item,
                    context=f'downloading merge item {idx + 1}',
                    max_retries=2,
                )
            except Exception as e2:
                if not flood_seen:
                    raise
                print(f'Retry download failed for merge item {idx + 1}: {e2}')
                im = None
                ifiles = []
            if im is None:
                continue
            media.append(im)
            files.extend(ifiles)
    except Exception:
        for ff in files:
            if os.path.exists(ff):
                os.remove(ff)
        raise
    if not media:
        await main_bot.edit_message_text(did, p.id, '媒体下载全部失败')
        return '❌ 媒体下载全部失败'

    # Caption distribution: when oc is set and media needs >1 chunk, each
    # chunk's album carries the SAME text plus a (n/N) progress marker so the
    # recipient knows which part is which. Without oc, the original behavior
    # is preserved (caption only on the first item, or standalone if >1024).
    num_chunks = (len(media) + 9) // 10
    standalone_text = None
    if ft:
        if oc is not None and num_chunks > 1:
            marker_tmpl = '\n\n({}/{})'
            for ci in range(num_chunks):
                chunk_start = ci * 10
                chunk_media = media[chunk_start:chunk_start + 10]
                marker = marker_tmpl.format(ci + 1, num_chunks)
                # Leave room for the marker within Telegram's 1024-char cap.
                max_cap = 1024 - len(marker)
                chunk_media[0].caption = ft[:max_cap] + marker
        elif len(ft) <= 1024:
            media[0].caption = ft
        else:
            standalone_text = ft

    await main_bot.edit_message_text(did, p.id, f'正在上传（{len(media)} 项）...')
    sent_items = 0
    for start in range(0, len(media), 10):
        chunk = media[start:start + 10]
        # No progress hook on send_media_group — refresh mtimes per chunk so
        # slow chunked uploads stay above the stale-sweep watermark.
        for im in chunk:
            touch_file(getattr(im, 'media', None))
        flood_seen = False

        async def send_group():
            nonlocal flood_seen
            try:
                return await sender.send_media_group(
                    tcid, chunk, reply_to_message_id=rtmid
                )
            except FloodWait:
                flood_seen = True
                raise

        try:
            await with_flood_retry(
                send_group,
                context=f'send_media_group chunk {start}',
                max_retries=2,
            )
            sent_items += len(chunk)
            continue
        except TypeError as e:
            if flood_seen:
                print(f'Retry send_media_group failed on chunk {start}: {e}')
            elif 'keyword-only argument' in str(e):
                # pyrofork parse bug: the RPC already succeeded — the album is
                # already delivered, only response parsing failed.
                sent_items += len(chunk)
                continue
            else:
                print(f'send_media_group failed on chunk {start}: {e}')
        except Exception as e:
            if flood_seen:
                print(f'Retry send_media_group failed on chunk {start}: {e}')
            else:
                print(f'send_media_group failed on chunk {start} ({e}), falling back to per-item')
        # Per-item fallback for whatever the group attempt above could not send.
        sent_items += await _send_album_items(sender, tcid, chunk, rtmid)

    for ff in files:
        if os.path.exists(ff):
            os.remove(ff)

    if standalone_text:
        try:
            await sender.send_message(tcid, text=standalone_text, reply_to_message_id=rtmid)
        except Exception as e:
            print(f'Standalone text send failed: {e}')

    if sent_items:
        await _safe_cleanup(main_bot.delete_messages(did, p.id))
        return f'✅ 已合并发送（{sent_items} 项媒体）'
    await main_bot.edit_message_text(did, p.id, '合并上传失败')
    return '❌ 合并上传失败'


def _cleanup_downloaded_thumbnail(th, downloads_dir):
    if not th:
        return
    try:
        if os.path.dirname(os.path.abspath(th)) == downloads_dir and os.path.exists(th):
            os.remove(th)
    except Exception:
        pass

class _PreparedMsg:
    """Message prepared for a later finish phase.

    ``kind`` is one of ``'text'``, ``'direct'`` or ``'downloaded'``.
    Every instance carries the delivery context needed by
    :func:`finish_prepared_msg`; downloaded instances additionally have
    ``f`` and ``p`` for the temporary file and progress message.
    """

    def __init__(self, kind, **fields):
        self.kind = kind
        for name, value in fields.items():
            setattr(self, name, value)


class _PreparedLink:
    """A prefetched link.

    ``kind == 'album'`` stores ``msgs``, ``src_lt`` and ``src_chat`` plus
    ``ubot``, ``uc``, ``d``, ``uid``, ``oc`` and ``settings`` for
    :func:`process_album`.  ``kind == 'single'`` stores the
    :class:`_PreparedMsg` in ``prepared``.
    """

    def __init__(self, kind, **fields):
        self.kind = kind
        for name, value in fields.items():
            setattr(self, name, value)


def _cleanup_prepared(prep):
    if prep is None:
        return
    paths = []
    for candidate in (
        getattr(prep, 'f', None),
        getattr(prep, 'download_path', None),
    ):
        if candidate and isinstance(candidate, str) and candidate not in paths:
            paths.append(candidate)
    for path in paths:
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    _cleanup_downloaded_thumbnail(
        getattr(prep, 'th', None),
        getattr(prep, 'downloads_dir', ''),
    )


async def _cancel_cleanup_prepared(prep):
    """Best-effort cleanup for cancellation while preparation is awaiting."""
    _cleanup_prepared(prep)
    if prep is None or getattr(prep, '_cancel_cleanup_done', False):
        return
    prep._cancel_cleanup_done = True
    try:
        if getattr(prep, 'p', None) is not None:
            await main_bot.delete_messages(prep.did, prep.p.id)
    except BaseException:
        pass


async def _download_prepared_msg(prep):
    """Allocate the progress message and download a media message.

    The helper is shared by the normal re-upload preparation path and by the
    direct-send fallback in ``finish_prepared_msg``.  It deliberately performs
    no post-download work or delivery send.
    """
    try:
        prep.st = time.time()
        prep.p = await main_bot.send_message(prep.did, '正在下载...')

        # A preparation can overlap the preceding finish, so nanoseconds are
        # required here.  In particular, document names used to have only
        # second-resolution uniqueness and could overwrite the previous item.
        c_name = f'{prep.uid}_{time.time_ns()}'
        m = prep.m
        if m.video:
            file_name = m.video.file_name
            if not file_name:
                file_name = f'{time.time()}.mp4'
                c_name = sanitize(f'{prep.uid}_{time.time_ns()}.mp4')
        elif m.audio:
            file_name = m.audio.file_name
            if not file_name:
                file_name = f'{time.time()}.mp3'
                c_name = sanitize(f'{prep.uid}_{time.time_ns()}.mp3')
        elif m.document:
            file_name = m.document.file_name
            if not file_name:
                file_name = f'{time.time()}'
            else:
                c_name = sanitize(f'{prep.uid}_{time.time_ns()}_{file_name}')
        elif m.photo:
            file_name = f'{time.time()}.jpg'
            c_name = sanitize(f'{prep.uid}_{time.time_ns()}.jpg')

        # pyrofork download_media resolves relative names against PARENT_DIR
        # (Path(sys.argv[0]).parent = /app, read-only image layer), ignoring the
        # client workdir. Pass an absolute path under the writable volume.
        download_path = os.path.join(_WORKDIR, 'downloads', c_name)
        prep.download_path = download_path
        # Download with the client that fetched the message: bot_fetched is the
        # prepare-time snapshot of (public link + fetch_origin False), i.e. the
        # bot holds access; otherwise the user client holds access. The live
        # fetch_origin entry is deliberately NOT re-read here: a concurrent
        # prefetch may have overwritten it for this chat.
        dl_client = (prep.c or prep.u) if getattr(prep, 'bot_fetched', False) else (prep.u or prep.c)
        prep.f = await dl_client.download_media(
            m,
            file_name=download_path,
            progress=prog,
            progress_args=(main_bot, prep.did, prep.p.id, prep.st),
        )

        if not prep.f:
            await main_bot.edit_message_text(prep.did, prep.p.id, '失败。')
            return 'Failed.'
        prep.kind = 'downloaded'
        return None
    except asyncio.CancelledError:
        await _cancel_cleanup_prepared(prep)
        raise


async def prepare_msg(c, u, m, d, lt, uid, i, oc=None, *, settings):
    """Prepare one message without sending delivered content.

    The returned pair is ``(result, prepared)``.  Terminal failures return a
    status and ``None``; text, direct-send candidates and downloaded media
    return ``None`` plus a :class:`_PreparedMsg` of the corresponding kind.
    Only the progress message is sent here; delivered-content sends belong to
    :func:`finish_prepared_msg`.
    """
    prep = None
    downloads_dir = os.path.abspath(os.path.join(_WORKDIR, 'downloads'))
    try:
        tcid, rtmid, deliver_via_bot = await resolve_delivery(d, settings)
        did = int(d)

        if m.media:
            if oc is not None:
                proc_text = oc
            else:
                orig_text = m.caption.markdown if m.caption else ''
                proc_text = apply_text_rules(
                    orig_text,
                    settings.get('replacement_words', {}),
                    settings.get('delete_words', []),
                )
            user_cap = settings.get('caption', '')
            ft = f'{proc_text}\n\n{user_cap}' if proc_text and user_cap else user_cap if user_cap else proc_text
            sender = c if deliver_via_bot else (u or c)
            direct = lt == 'public' and not fetch_origin.get((uid, i), False)
            prep = _PreparedMsg(
                'direct' if direct else 'downloaded',
                c=c,
                u=u,
                m=m,
                d=d,
                lt=lt,
                uid=uid,
                i=i,
                oc=oc,
                settings=settings,
                tcid=tcid,
                rtmid=rtmid,
                ft=ft,
                sender=sender,
                did=did,
                # Snapshot of the fetch-origin marker at prepare time. A
                # deferred (kind='direct') download runs during finish, where
                # a concurrent prefetch's get_msg for the same chat may have
                # overwritten fetch_origin — the client that actually fetched
                # THIS message must be pinned here.
                bot_fetched=direct,
                f=None,
                p=None,
                st=None,
                th=None,
                downloads_dir=downloads_dir,
            )
            if direct:
                return None, prep
            result = await _download_prepared_msg(prep)
            if result is not None:
                _cleanup_prepared(prep)
                return result, None
            return None, prep

        if m.text:
            sender = c if deliver_via_bot else (u or c)
            prep = _PreparedMsg(
                'text',
                c=c,
                u=u,
                m=m,
                d=d,
                lt=lt,
                uid=uid,
                i=i,
                oc=oc,
                settings=settings,
                tcid=tcid,
                rtmid=rtmid,
                did=did,
                ft=None,
                sender=sender,
                text=oc if oc is not None else m.text.markdown,
                f=None,
                p=None,
                th=None,
                downloads_dir=downloads_dir,
            )
            return None, prep

        # Preserve the historical implicit-None result for an unsupported
        # message shape.  Such a message has no phase-2 work to carry.
        return None, None
    except asyncio.CancelledError:
        await _cancel_cleanup_prepared(prep)
        raise
    except Exception as e:
        _cleanup_prepared(prep)
        return f'Error: {str(e)[:50]}', None


async def _finish_downloaded_msg(prep):
    """Finish a downloaded message, preserving process_msg's old body."""
    f = prep.f
    th = prep.th
    try:
        await main_bot.edit_message_text(prep.did, prep.p.id, '正在重命名...')
        if (
            (prep.m.video and prep.m.video.file_name)
            or (prep.m.audio and prep.m.audio.file_name)
            or (prep.m.document and prep.m.document.file_name)
        ):
            f = await rename_file(f, prep.d, prep.p, prep.settings)
            prep.f = f

        fsize = os.path.getsize(f) / (1024 * 1024 * 1024)
        th = thumbnail(prep.d)
        prep.th = th

        if fsize > 2 and premium_userbot:
            st = time.time()
            await main_bot.edit_message_text(
                prep.did, prep.p.id, '文件大于 2GB，正在使用备用方法...'
            )
            await upd_dlg(premium_userbot)
            mtd = await get_video_metadata(f)
            dur, w, h = mtd['duration'], mtd['width'], mtd['height']
            th = await screenshot(f, dur, prep.d)
            prep.th = th

            send_funcs = {
                'video': premium_userbot.send_video,
                'video_note': premium_userbot.send_video_note,
                'voice': premium_userbot.send_voice,
                'audio': premium_userbot.send_audio,
                'photo': premium_userbot.send_photo,
                'document': premium_userbot.send_document,
            }

            for mtype, func in send_funcs.items():
                if f.endswith('.mp4'):
                    mtype = 'video'
                if getattr(prep.m, mtype, None):
                    sent = await func(
                        LOG_GROUP,
                        f,
                        thumb=th if mtype == 'video' else None,
                        duration=dur if mtype == 'video' else None,
                        height=h if mtype == 'video' else None,
                        width=w if mtype == 'video' else None,
                        caption=(
                            prep.ft
                            if prep.m.caption and mtype not in ['video_note', 'voice']
                            else None
                        ),
                        reply_to_message_id=prep.rtmid,
                        progress=prog,
                        progress_args=(main_bot, prep.did, prep.p.id, st, f),
                    )
                    break
            else:
                sent = await premium_userbot.send_document(
                    LOG_GROUP,
                    f,
                    thumb=th,
                    caption=prep.ft if prep.m.caption else None,
                    reply_to_message_id=prep.rtmid,
                    progress=prog,
                    progress_args=(main_bot, prep.did, prep.p.id, st, f),
                )

            await prep.sender.copy_message(prep.tcid, LOG_GROUP, sent.id)
            os.remove(f)
            await _safe_cleanup(main_bot.delete_messages(prep.did, prep.p.id))
            return 'Done (Large file).'

        await main_bot.edit_message_text(prep.did, prep.p.id, '正在上传...')
        st = time.time()

        try:
            file_ext = os.path.splitext(f)[1].lower().lstrip('.')
            if prep.m.video or (prep.m.document and file_ext in VIDEO_EXTENSIONS):
                mtd = await get_video_metadata(f)
                dur, w, h = mtd['duration'], mtd['width'], mtd['height']
                th = await screenshot(f, dur, prep.d)
                prep.th = th
                await prep.sender.send_video(
                    prep.tcid,
                    video=f,
                    caption=prep.ft if prep.m.caption else None,
                    thumb=th,
                    width=w,
                    height=h,
                    duration=dur,
                    progress=prog,
                    progress_args=(main_bot, prep.did, prep.p.id, st, f),
                    reply_to_message_id=prep.rtmid,
                )
            elif prep.m.video_note:
                await prep.sender.send_video_note(
                    prep.tcid,
                    video_note=f,
                    progress=prog,
                    progress_args=(main_bot, prep.did, prep.p.id, st, f),
                    reply_to_message_id=prep.rtmid,
                )
            elif prep.m.voice:
                await prep.sender.send_voice(
                    prep.tcid,
                    f,
                    progress=prog,
                    progress_args=(main_bot, prep.did, prep.p.id, st, f),
                    reply_to_message_id=prep.rtmid,
                )
            elif prep.m.sticker:
                await prep.sender.send_sticker(
                    prep.tcid, f, reply_to_message_id=prep.rtmid
                )
            elif prep.m.audio or (prep.m.document and file_ext in AUDIO_EXTENSIONS):
                await prep.sender.send_audio(
                    prep.tcid,
                    audio=f,
                    caption=prep.ft if prep.m.caption else None,
                    thumb=th,
                    progress=prog,
                    progress_args=(main_bot, prep.did, prep.p.id, st, f),
                    reply_to_message_id=prep.rtmid,
                )
            elif prep.m.photo:
                await prep.sender.send_photo(
                    prep.tcid,
                    photo=f,
                    caption=prep.ft if prep.m.caption else None,
                    progress=prog,
                    progress_args=(main_bot, prep.did, prep.p.id, st, f),
                    reply_to_message_id=prep.rtmid,
                )
            elif prep.m.document:
                await prep.sender.send_document(
                    prep.tcid,
                    document=f,
                    caption=prep.ft if prep.m.caption else None,
                    progress=prog,
                    progress_args=(main_bot, prep.did, prep.p.id, st, f),
                    reply_to_message_id=prep.rtmid,
                )
            else:
                await prep.sender.send_document(
                    prep.tcid,
                    document=f,
                    caption=prep.ft if prep.m.caption else None,
                    progress=prog,
                    progress_args=(main_bot, prep.did, prep.p.id, st, f),
                    reply_to_message_id=prep.rtmid,
                )
        except Exception as e:
            err = str(e)
            if 'PEER_ID_INVALID' in err or 'CHAT_WRITE_FORBIDDEN' in err or 'ADMIN' in err.upper():
                hint = '请将 /setbot 的机器人加入目标频道并授予发帖权限。'
            else:
                hint = ''
            try:
                await main_bot.edit_message_text(
                    prep.did, prep.p.id, f'上传失败：{err[:60]} {hint}'
                )
            except Exception:
                pass
            if os.path.exists(f):
                os.remove(f)
            return f'上传失败：{err[:60]} {hint}'.strip()

        os.remove(f)
        await _safe_cleanup(main_bot.delete_messages(prep.did, prep.p.id))
        return 'Done.'
    except Exception as e:
        return f'Error: {str(e)[:50]}'
    finally:
        prep.f = f
        prep.th = th
        _cleanup_prepared(prep)


async def finish_prepared_msg(prep):
    """Send the content represented by a :class:`_PreparedMsg`."""
    if prep.kind == 'text':
        try:
            await prep.sender.send_message(
                prep.tcid,
                text=prep.text,
                reply_to_message_id=prep.rtmid,
            )
            return 'Sent.'
        except Exception as e:
            return f'Error: {str(e)[:50]}'

    if prep.kind == 'direct':
        try:
            sent, error = await send_direct(
                prep.c, prep.m, prep.tcid, prep.ft, prep.rtmid
            )
            if sent:
                return 'Sent directly.'
            if error and 'PEER_ID_INVALID' in error:
                return (
                    '发送失败：目标聊天不可用。请在 /settings 设置正确的 '
                    '-100... 聊天 ID，并将 /setbot 机器人加入该频道且设为管理员。'
                )
            print(f'Direct send failed ({error}), falling back to re-upload')
            result = await _download_prepared_msg(prep)
            if result is not None:
                return result
            return await _finish_downloaded_msg(prep)
        except Exception as e:
            _cleanup_prepared(prep)
            return f'Error: {str(e)[:50]}'

    if prep.kind == 'downloaded':
        return await _finish_downloaded_msg(prep)
    return None


async def abort_prepared_msg(prep):
    """Cancel a prepared downloaded message without propagating cleanup errors."""
    if prep is None or getattr(prep, 'kind', None) != 'downloaded':
        return
    try:
        _cleanup_prepared(prep)
    except Exception:
        pass
    try:
        if getattr(prep, 'p', None) is not None:
            await main_bot.delete_messages(prep.did, prep.p.id)
    except Exception:
        pass


async def process_msg(c, u, m, d, lt, uid, i, oc=None, *, settings):
    result, prep = await prepare_msg(
        c, u, m, d, lt, uid, i, oc, settings=settings
    )
    if prep is None:
        return result
    return await finish_prepared_msg(prep)


def _ok(res):
    # Success strings are either process_msg's English markers or the
    # emoji-prefixed album results (✅ full, ⚠️ partial per-item fallback).
    return (res.startswith(('✅', '⚠️'))
            or 'Done' in res or 'Copied' in res or 'Sent' in res)


async def process_one_link(
    ubot, uc, i, s, lt, d, uid, oc=None, comment_id=None, *, settings
):
    """Fetch and deliver one t.me link (expanding albums), with one FloodWait retry."""
    return await with_flood_retry(
        lambda: _process_one_link(
            ubot, uc, i, s, lt, d, uid, oc, comment_id, settings=settings
        ),
        context=f'{i}/{s}',
        max_retries=2,
    )


async def prepare_one_link(
    ubot, uc, i, s, lt, d, uid, oc=None, comment_id=None, *, settings
):
    """Prepare one link into a :class:`_PreparedLink` without delivery sends.

    The return pair is ``(result, prepared_link)``.  Album links carry the
    original media messages and source coordinates; single links carry the
    :class:`_PreparedMsg` returned by :func:`prepare_msg`.
    """
    if not uc and lt != 'public':
        return '用户会话无效或未登录，请先使用 /login。', None
    msg = await get_msg(ubot, uc, i, s, lt, uid, comment_id)
    if not msg:
        return '未找到消息', None
    msgs = [msg]
    # Comment links resolve to a discussion-group message whose chat differs
    # from the URL's channel. Use the message's own chat for media-group
    # expansion so we fetch the right group.
    src_chat = msg.chat.id if getattr(msg, 'chat', None) else i
    src_lt = 'private' if comment_id else lt
    if getattr(msg, 'media_group_id', None):
        fetch_client = (
            uc
            if (uc and (src_lt == 'private' or fetch_origin.get((uid, src_chat), False)))
            else ubot
        )
        try:
            group = await fetch_client.get_media_group(src_chat, msg.id)
            if group:
                msgs = group
        except FloodWait:
            raise
        except Exception as e:
            print(f'Media group fetch failed, falling back to single: {e}')
    if len(msgs) > 1:
        return None, _PreparedLink(
            'album',
            msgs=msgs,
            src_lt=src_lt,
            src_chat=src_chat,
            ubot=ubot,
            uc=uc,
            d=d,
            uid=uid,
            oc=oc,
            settings=settings,
        )
    result, prepared = await prepare_msg(
        ubot,
        uc,
        msgs[0],
        d,
        src_lt,
        uid,
        src_chat,
        oc,
        settings=settings,
    )
    if prepared is None:
        return result, None
    return None, _PreparedLink('single', prepared=prepared)


async def finish_one_link(prepared_link):
    """Finish a :class:`_PreparedLink` after its predecessor has uploaded."""
    if prepared_link.kind == 'album':
        return await process_album(
            prepared_link.ubot,
            prepared_link.uc,
            prepared_link.msgs,
            prepared_link.d,
            prepared_link.src_lt,
            prepared_link.uid,
            prepared_link.src_chat,
            prepared_link.oc,
            settings=prepared_link.settings,
        )
    if prepared_link.kind == 'single':
        return await finish_prepared_msg(prepared_link.prepared)
    return None


async def _process_one_link(
    ubot, uc, i, s, lt, d, uid, oc=None, comment_id=None, *, settings
):
    """Compose :func:`prepare_one_link` and :func:`finish_one_link`."""
    result, prepared_link = await prepare_one_link(
        ubot,
        uc,
        i,
        s,
        lt,
        d,
        uid,
        oc,
        comment_id,
        settings=settings,
    )
    if prepared_link is None:
        return result
    return await finish_one_link(prepared_link)

async def _sweep_progress_state(now=None):
    if now is None:
        now = time.time()
    for message_id, state in list(progress_state.items()):
        try:
            timestamp = state[1]
        except (IndexError, TypeError):
            continue
        if now - timestamp > _PROGRESS_TTL:
            progress_state.pop(message_id, None)

from plugins import tasks as tasks_module
tasks_module.register_sweep_hook(_sweep_progress_state)
