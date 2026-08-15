# ---------------------------------------------------
# File Name: ytdl.py (pure code)
# Description: A Pyrogram bot for downloading yt and other sites videos from Telegram channels or groups 
#              and uploading them back to Telegram.
# Author: Gagan
# GitHub: https://github.com/devgaganin/
# Telegram: https://t.me/team_spy_pro
# YouTube: https://youtube.com/@dev_gagan
# Created: 2025-01-11
# Last Modified: 2025-01-11
# Version: 2.0.5
# License: MIT License
# ---------------------------------------------------

import yt_dlp
import os
import tempfile
import time
import asyncio
import random
import string
import requests
import logging
import math
from shared_client import app, _WORKDIR
from pyrogram import filters
from utils.func import get_video_metadata, screenshot, touch_file
from utils.missav import (
    DEFAULT_MIRRORS as _MISSAV_DEFAULT_MIRRORS,
    MissAVError,
    build_caption,
    download_missav,
    is_missav_url,
)
from concurrent.futures import ThreadPoolExecutor
import aiohttp 
import aiofiles
from config import (
    INSTA_COOKIES,
    MISSAV_MAX_JOBS,
    MISSAV_MIRRORS,
    MISSAV_SEGMENT_CONCURRENCY,
    PROGRESS_MIN_INTERVAL,
    YT_COOKIES,
)
from mutagen.id3 import ID3, TIT2, TPE1, COMM, APIC
from mutagen.mp3 import MP3
 
logger = logging.getLogger(__name__)
 
 
thread_pool = ThreadPoolExecutor()
ongoing_downloads = {}
screenshot_lock = asyncio.Lock()
# cross-user cap on simultaneous missav pipelines (disk/bandwidth guard)
_missav_jobs_sem = None


def _get_missav_jobs_sem():
    global _missav_jobs_sem
    if _missav_jobs_sem is None:
        _missav_jobs_sem = asyncio.Semaphore(max(1, MISSAV_MAX_JOBS))
    return _missav_jobs_sem


UPLOAD_HEADER = "╭─────────────────────╮\n│      **__上传中__**\n├─────────────────────"
 
def d_thumbnail(thumbnail_url, save_path, timeout=(5, 20), max_bytes=10 * 1024 * 1024):
    try:
        response = requests.get(thumbnail_url, stream=True, timeout=timeout)
        response.raise_for_status()
        received = 0
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                received += len(chunk)
                if received > max_bytes:
                    raise requests.exceptions.RequestException(
                        f"thumbnail exceeds {max_bytes} bytes")
                f.write(chunk)
        return save_path
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download thumbnail: {e}")
        _remove_quiet(save_path)
        return None
    except OSError as e:
        logger.error(f"Failed to save thumbnail: {e}")
        _remove_quiet(save_path)
        return None


def _remove_quiet(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
 
 
async def download_thumbnail_async(url, path):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                with open(path, 'wb') as f:
                    f.write(await response.read())
 
 
async def extract_audio_async(ydl_opts, url):
    def sync_extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)
    return await asyncio.get_event_loop().run_in_executor(thread_pool, sync_extract)
 
 
def get_random_string(length=7):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length)) 
 
 
async def process_audio(message, url, cookies_env_var=None):
    cookies = cookies_env_var if cookies_env_var else None

    temp_cookie_path = None
    if cookies:
        with tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.txt') as temp_cookie_file:
            temp_cookie_file.write(cookies)
            temp_cookie_path = temp_cookie_file.name

    download_dir = os.path.join(_WORKDIR, 'downloads')
    os.makedirs(download_dir, exist_ok=True)
    random_filename = os.path.join(download_dir, f"@team_spy_pro_{message.from_user.id}")
    download_path = f"{random_filename}.mp3"
    thumbnail_path = None

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f"{random_filename}.%(ext)s",
        'cookiefile': temp_cookie_path,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        'quiet': False,
        'noplaylist': True,
    }

    chat_id = message.chat.id
    progress_message = await message.reply_text("**__开始提取音频...__**")

    try:
        info_dict = await extract_audio_async(ydl_opts, url)
        title = info_dict.get('title', '提取的音频')

        await progress_message.edit_text("**__正在编辑元数据...__**")

        if os.path.exists(download_path):
            def edit_metadata():
                nonlocal thumbnail_path
                audio_file = MP3(download_path, ID3=ID3)
                try:
                    audio_file.add_tags()
                except Exception:
                    pass
                audio_file.tags["TIT2"] = TIT2(encoding=3, text=title)
                audio_file.tags["TPE1"] = TPE1(encoding=3, text="Team SPY")
                audio_file.tags["COMM"] = COMM(encoding=3, lang="eng", desc="Comment", text="Processed by Team SPY")
                thumbnail_url = info_dict.get('thumbnail')
                if thumbnail_url:
                    thumbnail_path = os.path.join(
                        download_dir, f"thumb_{get_random_string()}.jpg"
                    )
                    asyncio.run(download_thumbnail_async(thumbnail_url, thumbnail_path))
                    with open(thumbnail_path, 'rb') as img:
                        audio_file.tags["APIC"] = APIC(
                            encoding=3, mime='image/jpeg', type=3, desc='Cover', data=img.read()
                        )
                audio_file.save()

            await asyncio.to_thread(edit_metadata)

        if os.path.exists(download_path):
            await progress_message.delete()
            prog = await app.send_message(chat_id, "**__开始上传...__**")
            await app.send_audio(
                chat_id,
                audio=download_path,
                caption=f"**{title}**\n\n**__由 Team SPY 提供支持__**",
                title=title,
                performer="Team SPY",
                progress=progress_bar,
                progress_args=(UPLOAD_HEADER, prog, time.time(), download_path)
            )
            if prog:
                await prog.delete()
        else:
            await message.reply_text("**__提取后未找到音频文件！__**")

    except Exception as e:
        logger.exception("Error during audio extraction or upload")
        await message.reply_text(f"**__发生错误：{e}__**")
    finally:
        if os.path.exists(download_path):
            os.remove(download_path)
        if thumbnail_path and os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)
        if temp_cookie_path and os.path.exists(temp_cookie_path):
            os.remove(temp_cookie_path)


@app.on_message(filters.command("adl"))
async def adl_handler(client, message):
    user_id = message.from_user.id
    if user_id in ongoing_downloads:
        await message.reply_text("**您已有正在进行的下载，请等待完成！**")
        return

    if len(message.text.split()) < 2:
        await message.reply_text("**用法：** `/adl <video-link>`\n\n请提供有效的视频链接！")
        return    

    url = message.text.split()[1]
    ongoing_downloads[user_id] = True

    try:
        if "instagram.com" in url:
            await process_audio(message, url, cookies_env_var=INSTA_COOKIES)
        elif "youtube.com" in url or "youtu.be" in url:
            await process_audio(message, url, cookies_env_var=YT_COOKIES)
        else:
            await process_audio(message, url)
    except Exception as e:
        await message.reply_text(f"**发生错误：** `{e}`")
    finally:
        ongoing_downloads.pop(user_id, None)


async def fetch_video_info(url, ydl_opts, progress_message, check_duration_and_size):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=False)

        if check_duration_and_size:
            duration = info_dict.get('duration', 0)
            if duration and duration > 3 * 3600:   
                await progress_message.edit_text("**❌ __视频时长超过 3 小时，已中止下载...__**")
                return None

            estimated_size = info_dict.get('filesize_approx', 0)
            if estimated_size and estimated_size > 2 * 1024 * 1024 * 1024:   
                await progress_message.edit_text("**🤞 __视频大小超过 2GB，已中止下载。__**")
                return None

        return info_dict

def download_video(url, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


@app.on_message(filters.command("dl"))
async def dl_handler(client, message):
    user_id = message.from_user.id

    if user_id in ongoing_downloads:
        await message.reply_text("**您已有正在进行的 ytdlp 下载，请等待完成！**")
        return

    if len(message.text.split()) < 2:
        await message.reply_text("**用法：** `/dl <video-link>`\n\n请提供有效的视频链接！")
        return

    url = message.text.split()[1]
    ongoing_downloads[user_id] = True
    missav_hosts = MISSAV_MIRRORS or list(_MISSAV_DEFAULT_MIRRORS)

    try:
        if is_missav_url(url, missav_hosts):
            await process_missav(message, url, missav_hosts)
        elif "instagram.com" in url:
            await process_video(message, url, INSTA_COOKIES, check_duration_and_size=False)
        elif "youtube.com" in url or "youtu.be" in url:
            await process_video(message, url, YT_COOKIES, check_duration_and_size=True)
        else:
            await process_video(message, url, None, check_duration_and_size=False)

    except Exception as e:
        await message.reply_text(f"**发生错误：** `{e}`")
    finally:
        ongoing_downloads.pop(user_id, None)


async def _finalize_and_upload(message, download_path, title, thumbnail_url,
                               progress_message, extra_meta=None,
                               target_chat=None, sender=None,
                               caption_override=None):
    """Probe metadata, resolve a thumbnail (download → screenshot fallback),
    then upload (splitting first when >2 GB).

    Progress/notice messages always go to the requesting chat via the main
    bot; the media itself goes to ``target_chat`` (default: requesting
    chat) via ``sender`` (default: main bot). ``caption_override``
    replaces the default bold-title caption entirely.

    Owns and cleans its thumbnail/screenshot temp files; the caller owns
    ``download_path``.
    """
    chat_id = message.chat.id
    dest_chat = target_chat if target_chat is not None else chat_id
    uploader = sender or app
    download_dir = os.path.dirname(download_path)
    extra = extra_meta or {}
    thumbnail_file = None
    thumbnail_path = None
    screenshot_file = None
    try:
        k = await get_video_metadata(download_path)
        duration = int(extra.get('duration') or 0) or k['duration']
        width = extra.get('width') or k['width']
        height = extra.get('height') or k['height']

        THUMB = None
        if thumbnail_url:
            thumbnail_path = os.path.join(download_dir, get_random_string() + ".jpg")
            thumbnail_file = await asyncio.to_thread(d_thumbnail, thumbnail_url, thumbnail_path)
            if thumbnail_file:
                logger.info(f"Thumbnail saved at: {thumbnail_file}")
            else:
                thumbnail_file = None

        if thumbnail_file:
            THUMB = thumbnail_file
        else:
            thumbnail_path = None
            async with screenshot_lock:
                previous_cwd = os.getcwd()
                try:
                    os.chdir(download_dir)
                    THUMB = await screenshot(download_path, duration, message.from_user.id)
                    if THUMB and not os.path.isabs(THUMB):
                        THUMB = os.path.join(download_dir, THUMB)
                finally:
                    os.chdir(previous_cwd)
            screenshot_file = THUMB

        # clamp remote titles: Telegram captions cap at 1024 chars and a
        # hostile og:title must not fail the upload after the full download
        caption = caption_override if caption_override is not None else f"**{title[:500]}**"
        # Telegram bot API single-file limit is 2 GB; larger files are split.
        SIZE = 2 * 1024 * 1024 * 1024
        if os.path.exists(download_path) and os.path.getsize(download_path) > SIZE:
            prog = await app.send_message(chat_id, "**__开始上传...__**")
            await split_and_upload_file(uploader, dest_chat, download_path, caption)
            await prog.delete()
            await _safe_delete(progress_message)
            return

        if os.path.exists(download_path):
            prog = await app.send_message(chat_id, "**__开始上传...__**")
            await uploader.send_video(
                dest_chat,
                video=download_path,
                caption=caption,
                duration=duration,
                width=width,
                height=height,
                supports_streaming=True,
                thumb=THUMB if THUMB else None,
                progress=progress_bar,
                progress_args=(UPLOAD_HEADER, prog, time.time(), download_path)
            )
            if prog:
                await prog.delete()
        else:
            await message.reply_text("**__下载后未找到文件。出现了问题！__**")
    finally:
        for temp_path in (thumbnail_file, screenshot_file):
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)


async def process_missav(message, url, hosts):
    """Download a missav.ai video page via the dedicated HLS pipeline and
    upload it back through the shared finalize/upload tail."""
    logger.info(f"Received missav link: {url}")
    sem = _get_missav_jobs_sem()
    if sem.locked():
        await message.reply_text(
            f"**__当前 missav 下载任务已满（最多 {MISSAV_MAX_JOBS} 个），请稍后再试__**")
        return
    progress_message = await message.reply_text("**__开始下载 missav 视频...__**")

    async with sem:
        await _run_missav_download(message, url, hosts, progress_message)


async def _run_missav_download(message, url, hosts, progress_message):
    download_dir = os.path.join(_WORKDIR, 'downloads')
    os.makedirs(download_dir, exist_ok=True)
    download_path = os.path.join(download_dir, f"{get_random_string()}.mp4")

    async def progress(done, total, stage):
        if stage != "segments" or total <= 0:
            return
        final = done >= total
        if not final and (time.time() - progress._last_edit) < PROGRESS_MIN_INTERVAL:
            return
        progress._last_edit = time.time()
        pct = int(done * 100 / total)
        try:
            await progress_message.edit_text(
                f"**__missav 下载中 {pct}%（{done}/{total} 段）...__**"
            )
        except Exception:
            pass  # message deleted / flood-limited: progress display is best-effort

    progress._last_edit = 0.0

    try:
        info = await download_missav(
            url,
            download_path,
            hosts=hosts,
            concurrency=MISSAV_SEGMENT_CONCURRENCY,
            progress=progress,
        )
        details = info.get('details') or {}
        caption = build_caption(details) or f"**{info.get('title') or 'missav 视频'}**"

        target_chat, sender = await _resolve_missav_delivery(message)
        try:
            await _finalize_and_upload(
                message,
                download_path,
                info.get('title') or 'missav 视频',
                info.get('thumbnail'),
                progress_message,
                target_chat=target_chat,
                sender=sender,
                caption_override=caption,
            )
        except Exception as e:
            if sender is app or target_chat == message.chat.id:
                raise  # no alternate route to try
            # channel send failed (custom bot not a member / no rights):
            # fall back to the main bot, then to the requesting chat
            logger.warning("missav channel delivery failed (%s); retrying via main bot", e)
            try:
                await _finalize_and_upload(
                    message, download_path, info.get('title') or 'missav 视频',
                    info.get('thumbnail'), progress_message,
                    target_chat=target_chat, sender=app, caption_override=caption,
                )
            except Exception as e2:
                logger.warning("missav main-bot delivery failed too (%s); sending to user chat", e2)
                await _finalize_and_upload(
                    message, download_path, info.get('title') or 'missav 视频',
                    info.get('thumbnail'), progress_message,
                    caption_override=caption,
                )
    except MissAVError as e:
        logger.warning("missav download failed: %s", e)
        await _safe_delete(progress_message)
        await message.reply_text(f"**__missav 下载失败：{e}__**")
    except Exception as e:
        logger.exception("missav download/upload failed.")
        await _safe_delete(progress_message)
        await message.reply_text(f"**__发生错误：{e}__**")
    finally:
        if os.path.exists(download_path):
            os.remove(download_path)


async def _resolve_missav_delivery(message):
    """Pick the missav delivery target/sender, mirroring resolve_delivery:
    per-user settings chat -> LOG_GROUP -> the requesting chat. Channel
    targets are served by the user's /setbot bot when available (same
    membership rules as the fetch flow), else the main bot."""
    try:
        from plugins.deliver import resolve_delivery
        from plugins.fetch import get_ubot
        from utils.func import get_user_settings

        settings = await get_user_settings(message.from_user.id) or {}
        tcid, rtmid, deliver_via_bot = await resolve_delivery(message.chat.id, settings)
        if deliver_via_bot and tcid != message.chat.id:
            ubot = None
            try:
                ubot = await get_ubot(message.from_user.id)
            except Exception as e:
                logger.info("get_ubot unavailable for missav delivery: %s", e)
            return tcid, (ubot or app)
        return tcid, app
    except Exception as e:
        logger.warning("missav delivery resolution failed, defaulting to chat: %s", e)
        return message.chat.id, app


async def _safe_delete(message):
    try:
        await message.delete()
    except Exception:
        pass


async def process_video(message, url, cookies, check_duration_and_size=False):
    logger.info(f"Received link: {url}")

    download_dir = os.path.join(_WORKDIR, 'downloads')
    os.makedirs(download_dir, exist_ok=True)
    download_path = os.path.join(download_dir, f"{get_random_string()}.mp4")
    logger.info(f"Generated random download path: {download_path}")

    temp_cookie_path = None
    if cookies:
        with tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.txt') as temp_cookie_file:
            temp_cookie_file.write(cookies)
            temp_cookie_path = temp_cookie_file.name
        logger.info(f"Created temporary cookie file at: {temp_cookie_path}")

    ydl_opts = {
        'outtmpl': download_path,
        'format': 'best',
        'cookiefile': temp_cookie_path if temp_cookie_path else None,
        'writethumbnail': True,
        'verbose': True,
    }

    progress_message = await message.reply_text("**__开始下载...__**")
    logger.info("Starting the download process...")
    try:
        info_dict = await fetch_video_info(url, ydl_opts, progress_message, check_duration_and_size)
        if not info_dict:
            return

        await asyncio.to_thread(download_video, url, ydl_opts)
        await _finalize_and_upload(
            message,
            download_path,
            info_dict.get('title', '由 Team SPY 提供支持'),
            info_dict.get('thumbnail', None),
            progress_message,
            extra_meta=info_dict,
        )
    except Exception as e:
        logger.exception("An error occurred during download or upload.")
        await message.reply_text(f"**__发生错误：{e}__**")
    finally:
        cleanup_paths = {
            download_path,
            os.path.splitext(download_path)[0] + ".jpg",
            os.path.splitext(download_path)[0] + ".webp",
        }
        for output_path in cleanup_paths:
            if output_path and os.path.exists(output_path):
                os.remove(output_path)
        if temp_cookie_path and os.path.exists(temp_cookie_path):
            os.remove(temp_cookie_path)


async def split_and_upload_file(app, sender, file_path, caption):
    if not os.path.exists(file_path):
        await app.send_message(sender, "❌ 未找到文件！")
        return

    file_size = os.path.getsize(file_path)
    start = await app.send_message(sender, f"ℹ️ 文件大小：{file_size / (1024 * 1024):.2f} MB")
    PART_SIZE = int(1.9 * 1024 * 1024 * 1024)
    CHUNK_SIZE = 8 * 1024 * 1024

    part_number = 0
    base_name, file_ext = os.path.splitext(file_path)
    async with aiofiles.open(file_path, mode="rb") as f:
        while True:
            part_file = f"{base_name}.part{str(part_number).zfill(3)}{file_ext}"
            bytes_written = 0
            try:
                async with aiofiles.open(part_file, mode="wb") as part_f:
                    while bytes_written < PART_SIZE:
                        chunk = await f.read(min(CHUNK_SIZE, PART_SIZE - bytes_written))
                        if not chunk:
                            break
                        await part_f.write(chunk)
                        bytes_written += len(chunk)

                if bytes_written == 0:
                    break

                edit = None
                try:
                    edit = await app.send_message(sender, f"⬆️ 正在上传第 {part_number + 1} 部分...")
                    part_caption = f"{caption} \n\n**第 {part_number + 1} 部分：**"
                    await app.send_document(sender, document=part_file, caption=part_caption,
                        progress=progress_bar,
                        progress_args=(UPLOAD_HEADER, edit, time.time(), part_file)
                    )
                    # The source file outlives every part upload; keep its mtime
                    # fresh so the sweeper cannot delete it mid-split.
                    touch_file(file_path)
                finally:
                    if edit:
                        try:
                            await edit.delete()
                        except Exception:
                            logger.warning("Failed to delete part upload progress message", exc_info=True)
            finally:
                if os.path.exists(part_file):
                    os.remove(part_file)

            part_number += 1

    await start.delete()
    os.remove(file_path)


PROGRESS_BAR = """
│ **__已完成：__** {1}/{2}
│ **__字节：__** {0}%
│ **__速度：__** {3}/秒
│ **__预计剩余时间：__** {4}
╰─────────────────────╯
"""

async def get_seconds(time_string: str) -> int:
    """
    Converts a time string (e.g., '5min', '2hour') into seconds.
    """
    def extract_value_and_unit(ts: str):
        value = ''.join(filter(str.isdigit, ts))
        unit = ts[len(value):].strip()
        return int(value) if value else 0, unit
    
    value, unit = extract_value_and_unit(time_string)
    time_units = {
        's': 1,
        'min': 60,
        'hour': 3600,
        'day': 86400,
        'month': 86400 * 30,
        'year': 86400 * 365
    }
    
    return value * time_units.get(unit, 0)

async def progress_bar(current: int, total: int, ud_type: str, message, start: float, fp=None):
    """
    Updates the progress bar for an ongoing process.
    """
    if fp:
        # Upload heartbeat: keep source mtime fresh against the stale-file sweeper.
        touch_file(fp)
    now = time.time()
    diff = now - start
    
    if round(diff % 10) == 0 or current == total:
        percentage = (current * 100) / total
        speed = current / diff if diff else 0
        elapsed_time = round(diff * 1000)
        time_to_completion = round((total - current) / speed) * 1000 if speed else 0
        estimated_total_time = elapsed_time + time_to_completion

        elapsed_time_str = TimeFormatter(elapsed_time)
        estimated_total_time_str = TimeFormatter(estimated_total_time)

        progress = "".join(["♦" for _ in range(math.floor(percentage / 10))]) + \
                   "".join(["◇" for _ in range(10 - math.floor(percentage / 10))])
        
        progress_text = progress + PROGRESS_BAR.format(
            round(percentage, 2),
            humanbytes(current),
            humanbytes(total),
            humanbytes(speed),
            estimated_total_time_str if estimated_total_time_str else "0 s"
        )
        try:
            await message.edit(text=f"{ud_type}\n│ {progress_text}")
        except:
            pass

def humanbytes(size: int) -> str:
    """
    Converts bytes into a human-readable format.
    """
    if not size:
        return ""
    
    power = 2**10
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    n = 0
    while size > power and n < len(units) - 1:
        size /= power
        n += 1
    
    return f"{round(size, 2)} {units[n]}"

def TimeFormatter(milliseconds: int) -> str:
    """
    Formats milliseconds into a human-readable duration.
    """
    seconds, milliseconds = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if seconds: parts.append(f"{seconds}s")
    if milliseconds: parts.append(f"{milliseconds}ms")
    
    return ', '.join(parts)

def convert(seconds: int) -> str:
    """
    Converts seconds into HH:MM:SS format.
    """
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"
