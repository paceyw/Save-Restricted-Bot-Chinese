# Copyright (c) 2025 devgagan : https://github.com/devgaganin.  
# Licensed under the GNU General Public License v3.0.  
# See LICENSE file in the repository root for full license text.

import time
import os
import re
import json
import logging
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGO_DB as MONGO_URI, DB_NAME
from utils.encrypt import ecs

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

PUBLIC_LINK_PATTERN = re.compile(r'^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([^/]+)/(?:\d+/)?(\d+)')
PRIVATE_LINK_PATTERN = re.compile(r'^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/c/(\d+)/(?:\d+/)?(\d+)')
# Canonical extension sets (single source for deliver.process_msg). Membership
# matches the pre-split process_msg local lists exactly: mpeg/mpg are NOT
# treated as video (they fell through to the plain-document path).
VIDEO_EXTENSIONS = {
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "3gp", "m4v", "ogv",
}
AUDIO_EXTENSIONS = {
    "mp3", "wav", "flac", "aac", "ogg", "wma", "m4a", "opus", "aiff", "ac3",
}

mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]
users_collection = db["users"]
premium_users_collection = db["premium_users"]
statistics_collection = db["statistics"]
codedb = db["redeem_code"]
SETTINGS_DEFAULTS = {
    "caption": "",
    "chat_id": None,
    "replacement_words": {},
    "delete_words": [],
    "rename_tag": "",
    "bot_token": None,
}



def is_private_link(link):
    return bool(PRIVATE_LINK_PATTERN.match(link))


def thumbnail(sender):
    # plugins.settings.handle_setthumb stores the file under _WORKDIR (the
    # writable volume); resolve the same absolute path here.
    from shared_client import _WORKDIR
    path = os.path.join(_WORKDIR, f'{sender}.jpg')
    return path if os.path.exists(path) else None


def hhmmss(seconds):
    return time.strftime('%H:%M:%S', time.gmtime(seconds))


def parse_link(L):
    """Parse a t.me link into (chat_id, msg_id, link_type, comment_id).

    comment_id is non-None when the URL contains ?comment=N — that points to a
    reply in the channel's discussion group, NOT the channel post itself.
    Callers resolve it via the linked discussion chat at fetch time.
    """
    if not isinstance(L, str):
        return None, None, None, None
    private_match = re.match(r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/c/(\d+)/(?:\d+/)?(\d+)', L)
    public_match = re.match(r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([^/]+)/(?:\d+/)?(\d+)', L)

    comment_match = re.search(r'[?&]comment=(\d+)', L)
    comment_id = int(comment_match.group(1)) if comment_match else None

    if private_match:
        return f'-100{private_match.group(1)}', int(private_match.group(2)), 'private', comment_id
    elif public_match:
        return public_match.group(1), int(public_match.group(2)), 'public', comment_id

    return None, None, None, None


def get_display_name(user):
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    elif user.first_name:
        return user.first_name
    elif user.last_name:
        return user.last_name
    elif user.username:
        return user.username
    else:
        return "Unknown User"


def sanitize_filename(filename):
    return re.sub(r'[<>:"/\\|?*]', '_', filename)


def get_dummy_filename(info):
    file_type = info.get("type", "file")
    extension = {
        "video": "mp4",
        "photo": "jpg",
        "document": "pdf",
        "audio": "mp3"
    }.get(file_type, "bin")
    
    return f"downloaded_file_{int(time.time())}.{extension}"


async def is_private_chat(event):
    return event.is_private


async def save_user_data(user_id, key, value):
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {key: value}},
        upsert=True
    )
    # Generic setter must not bypass the credential epoch: callers such as
    # plugins.settings.handle_addsession write session_string through here.
    if key in ("session_string", "bot_token"):
        bump_cred_epoch(user_id)
   # print(users_collection)


async def get_user_data_key(user_id, key, default=None):
    user_data = await users_collection.find_one({"user_id": int(user_id)})
  #  print(f"Fetching key '{key}' for user {user_id}: {user_data}")
    return user_data.get(key, default) if user_data else default
def filter_settings(user_data) -> dict:
    """Build a settings snapshot from a users document (or None).

    Copies only declared settings keys: the raw document also carries
    session_string (encrypted), _id and bookkeeping fields that must not be
    retained in the per-task snapshot (TASKS history)."""
    settings = deepcopy(SETTINGS_DEFAULTS)
    if user_data:
        for key in SETTINGS_DEFAULTS:
            if key in user_data:
                settings[key] = user_data[key]
    return settings


async def get_user_settings(uid) -> dict:
    """Load one user's settings document and merge it with safe defaults."""
    user_data = await users_collection.find_one({"user_id": int(uid)})
    return filter_settings(user_data)



# Per-user credential generation: bumped by every credential mutation below.
# Dispatch-time prefetched documents carry the epoch at read time; client
# establishment re-reads under _client_lock when the epoch moved, so a
# concurrent /setbot, /rembot, login or logout can never revive stale
# credentials while steady-state tasks still perform exactly one find_one.
_CRED_EPOCH = {}


def cred_epoch(user_id) -> int:
    return _CRED_EPOCH.get(int(user_id), 0)


def bump_cred_epoch(user_id):
    _CRED_EPOCH[int(user_id)] = _CRED_EPOCH.get(int(user_id), 0) + 1


def prune_cred_epochs(active_uids):
    """Drop epoch entries for users with no live clients/queues/tasks.

    Invoked by the batch sweeper; keeps _CRED_EPOCH bounded by the number of
    users with in-flight state rather than by lifetime credential mutations."""
    for uid in list(_CRED_EPOCH):
        if uid not in active_uids:
            _CRED_EPOCH.pop(uid, None)


async def get_user_data(user_id):
    try:
        user_data = await users_collection.find_one({"user_id": user_id})
        return user_data
    except Exception as e:
   #     logger.error(f"Error retrieving user data for {user_id}: {e}")
        return None


async def save_user_session(user_id, session_string):
    try:
        await users_collection.update_one(
            {"user_id": user_id},
            {"$set": {
                "session_string": session_string,
                "updated_at": datetime.now()
            }},
            upsert=True
        )
        logger.info(f"Saved session for user {user_id}")
        bump_cred_epoch(user_id)
        return True
    except Exception as e:
        logger.error(f"Error saving session for user {user_id}: {e}")
        return False


async def remove_user_session(user_id):
    try:
        await users_collection.update_one(
            {"user_id": user_id},
            {"$unset": {"session_string": ""}}
        )
        logger.info(f"Removed session for user {user_id}")
        bump_cred_epoch(user_id)
        return True
    except Exception as e:
        logger.error(f"Error removing session for user {user_id}: {e}")
        return False


async def save_user_bot(user_id, bot_token):
    try:
        encrypted_bot_token = ecs(bot_token)
        await users_collection.update_one(
            {"user_id": user_id},
            {"$set": {
                "bot_token": encrypted_bot_token,
                "updated_at": datetime.now()
            }},
            upsert=True
        )
        logger.info(f"Saved bot token for user {user_id}")
        bump_cred_epoch(user_id)
        return True
    except Exception as e:
        logger.error(f"Error saving bot token for user {user_id}: {e}")
        return False


async def migrate_user_bot_token(user_id, expected_plaintext):
    encrypted_bot_token = ecs(expected_plaintext)
    result = await users_collection.update_one(
        {"user_id": user_id, "bot_token": expected_plaintext},
        {"$set": {
            "bot_token": encrypted_bot_token,
            "updated_at": datetime.now()
        }}
    )
    if result.matched_count > 0:
        bump_cred_epoch(user_id)
        return True
    return False


async def remove_user_bot(user_id):
    try:
        await users_collection.update_one(
            {"user_id": user_id},
            {"$unset": {"bot_token": ""}}
        )
        logger.info(f"Removed bot token for user {user_id}")
        bump_cred_epoch(user_id)
        return True
    except Exception as e:
        logger.error(f"Error removing bot token for user {user_id}: {e}")
        return False


def apply_text_rules(text, replacements, delete_words) -> str:
    """Apply replacement and deletion rules without accessing the database."""
    if not text:
        return ""

    try:
        processed_text = text
        for word, replacement in replacements.items():
            processed_text = processed_text.replace(word, replacement)

        if delete_words:
            words = processed_text.split()
            filtered_words = [word for word in words if word not in delete_words]
            processed_text = " ".join(filtered_words)

        return processed_text
    except Exception as e:
        logger.error(f"Error processing text with rules: {e}")
        return text


async def process_text_with_rules(user_id, text):
    if not text:
        return ""

    try:
        settings = await get_user_settings(user_id)
        return apply_text_rules(
            text,
            settings.get("replacement_words", {}),
            settings.get("delete_words", []),
        )
    except Exception as e:
        logger.error(f"Error processing text with rules: {e}")
        return text


async def screenshot(video: str, duration: int, sender: str) -> str | None:
    existing_screenshot = f"{sender}.jpg"
    if os.path.exists(existing_screenshot):
        return existing_screenshot

    from shared_client import _WORKDIR
    downloads = os.path.join(_WORKDIR, 'downloads')
    os.makedirs(downloads, exist_ok=True)

    time_stamp = hhmmss(duration // 2)
    output_file = os.path.abspath(
        os.path.join(
            downloads,
            datetime.now().isoformat("_", "seconds")
            + f"_{sender}_{time.time_ns()}.jpg",
        )
    )

    cmd = [
        "ffmpeg",
        "-ss", time_stamp,
        "-i", video,
        "-frames:v", "1",
        output_file,
        "-y"
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    stdout, stderr = await process.communicate()

    if os.path.isfile(output_file):
        return output_file
    else:
        print(f"FFmpeg Error: {stderr.decode().strip()}")
        return None

async def cleanup_stale_downloads(max_age_min=60):
    from shared_client import _WORKDIR

    downloads = os.path.join(_WORKDIR, 'downloads')
    if os.path.islink(downloads):
        logger.warning("Skipping stale download cleanup for symlinked directory: %s", downloads)
        return
    if not os.path.isdir(downloads):
        return

    cutoff = time.time() - (max_age_min * 60)
    removed = 0
    for root, _, filenames in os.walk(downloads, followlinks=False):
        for filename in filenames:
            path = os.path.join(root, filename)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except Exception:
                pass
    logger.info("Removed %d stale downloads", removed)


_touch_last = {}

def touch_file(path, min_interval=5):
    """Refresh mtime of an in-flight file (throttled per path). The stale-file
    sweepers delete by mtime (>1h at startup, >4h hourly); a multi-hour upload
    only reads its source, so without this heartbeat an active upload looks
    like a corpse and can be deleted mid-send."""
    if not path:
        return
    now = time.time()
    if now - _touch_last.get(path, 0) < min_interval:
        return
    try:
        os.utime(path, None)
        _touch_last[path] = now
    except OSError:
        pass
    if len(_touch_last) > 10000:
        _touch_last.clear()


async def ensure_audio_track(file_path):
    """Telegram treats a video without an audio track as an animation; mixed
    into SendMultiMedia that makes the whole album fail with MEDIA_EMPTY.
    Remux with a silent AAC track (stream copy, no re-encode). Returns the
    path to use (original on probe/remux failure)."""
    try:
        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0", file_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await probe.communicate()
        if out.strip():
            return file_path
        stem, _ = os.path.splitext(file_path)
        fixed = f"{stem}_mux.mp4"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", file_path,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-c:v", "copy", "-c:a", "aac", "-shortest", fixed,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode == 0 and os.path.isfile(fixed) and os.path.getsize(fixed) > 0:
            os.remove(file_path)
            return fixed
        logger.error(f"ffmpeg remux failed for {file_path}: {err.decode()[-200:]}")
        if os.path.isfile(fixed):
            os.remove(fixed)
        return file_path
    except Exception as e:
        logger.error(f"ensure_audio_track failed for {file_path}: {e}")
        return file_path


async def get_video_metadata(file_path):
    default_values = {'width': 1, 'height': 1, 'duration': 1}
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "json", file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"ffprobe exited with {process.returncode}: {detail}")

        output = stdout.decode() if isinstance(stdout, bytes) else stdout
        metadata = json.loads(output)
        stream = metadata["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
        duration = int(round(float(metadata["format"]["duration"])))
        if duration <= 0:
            raise ValueError("video duration must be positive")
        return {'width': width, 'height': height, 'duration': duration}
    except Exception as e:
        logger.error(f"Error in video_metadata for {file_path}: {e}")
        return default_values


async def init_db_indexes():
    """Create the indexes required by the users and premium-users collections.

    Index creation is intentionally best effort: deployments may already
    contain duplicate user records, which makes a unique index fail.  Each
    index is attempted independently so one failure cannot prevent the
    remaining indexes from being created or crash application startup.
    """
    try:
        await users_collection.create_index("user_id", unique=True)
    except Exception as e:
        logger.warning("Failed to create users user_id index: %s", e)

    try:
        await premium_users_collection.create_index("user_id", unique=True)
    except Exception as e:
        logger.warning("Failed to create premium_users user_id index: %s", e)

    try:
        await premium_users_collection.create_index("expireAt", expireAfterSeconds=0)
    except Exception as e:
        logger.warning("Failed to create premium_users expireAt TTL index: %s", e)



async def add_premium_user(user_id, duration_value, duration_unit):
    try:
        now = datetime.now()
        expiry_date = None
        
        if duration_unit == "min":
            expiry_date = now + timedelta(minutes=duration_value)
        elif duration_unit == "hours":
            expiry_date = now + timedelta(hours=duration_value)
        elif duration_unit == "days":
            expiry_date = now + timedelta(days=duration_value)
        elif duration_unit == "weeks":
            expiry_date = now + timedelta(weeks=duration_value)
        elif duration_unit == "month":
            expiry_date = now + timedelta(days=30 * duration_value)
        elif duration_unit == "year":
            expiry_date = now + timedelta(days=365 * duration_value)
        elif duration_unit == "decades":
            expiry_date = now + timedelta(days=3650 * duration_value)
        else:
            return False, "Invalid duration unit"
            
        await premium_users_collection.update_one(
            {"user_id": user_id},
            {"$set": {
                "user_id": user_id,
                "subscription_start": now,
                "subscription_end": expiry_date,
                "expireAt": expiry_date
            }},
            upsert=True
        )
        
        
        return True, expiry_date
    except Exception as e:
        logger.error(f"Error adding premium user {user_id}: {e}")
        return False, str(e)


async def is_premium_user(user_id):
    try:
        user = await premium_users_collection.find_one({"user_id": user_id})
        if user and "subscription_end" in user:
            now = datetime.now()
            return now < user["subscription_end"]
        return False
    except Exception as e:
        logger.error(f"Error checking premium status for {user_id}: {e}")
        return False


async def get_premium_details(user_id):
    try:
        user = await premium_users_collection.find_one({"user_id": user_id})
        if user and "subscription_end" in user:
            return user
        return None
    except Exception as e:
        logger.error(f"Error getting premium details for {user_id}: {e}")
        return None
