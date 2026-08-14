# Copyright (c) 2025 devgagan : https://github.com/devgaganin.
# Licensed under the GNU General Public License v3.0.
# See LICENSE file in the repository root for full license text.

import os
from dotenv import load_dotenv
load_dotenv()

# ════════════════════════════════════════════════════════════════════════════════
# ░ CONFIGURATION SETTINGS
# ════════════════════════════════════════════════════════════════════════════════

# VPS --- FILL COOKIES 🍪 in """ ... """ 
INST_COOKIES = """
# write up here insta cookies
"""

YTUB_COOKIES = """
# write here yt cookies
"""

# ─── BOT / DATABASE CONFIG ──────────────────────────────────────────────────────
API_ID       = os.getenv("API_ID", "")
API_HASH     = os.getenv("API_HASH", "")
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
MONGO_DB     = os.getenv("MONGO_DB", "")
DB_NAME      = os.getenv("DB_NAME", "telegram_downloader")

# ─── OWNER / CONTROL SETTINGS ───────────────────────────────────────────────────
OWNER_ID     = list(map(int, os.getenv("OWNER_ID", "").split()))  # space-separated list
STRING       = os.getenv("STRING", None)  # optional session string
LOG_GROUP    = int(os.getenv("LOG_GROUP", "0"))
FORCE_SUB    = int(os.getenv("FORCE_SUB", "0"))

# ─── SECURITY KEYS ──────────────────────────────────────────────────────────────
def _required_key(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is required. Generate it with 'openssl rand -hex 32' "
            f"and set it in the environment before starting the bot."
        )
    return value


MASTER_KEY   = _required_key("MASTER_KEY")  # session encryption
IV_KEY       = _required_key("IV_KEY")  # decryption key

# ─── COOKIES HANDLING ───────────────────────────────────────────────────────────
YT_COOKIES   = os.getenv("YT_COOKIES", YTUB_COOKIES)
INSTA_COOKIES = os.getenv("INSTA_COOKIES", INST_COOKIES)

# ─── USAGE LIMITS ───────────────────────────────────────────────────────────────
FREEMIUM_LIMIT = int(os.getenv("FREEMIUM_LIMIT", "0"))
PREMIUM_LIMIT  = int(os.getenv("PREMIUM_LIMIT", "500"))

# ─── RATE CONTROL (anti-flood) ──────────────────────────────────────────────────
# Adaptive batch/count interval (seconds): starts at the floor and backs off to the ceiling.
BATCH_INTERVAL   = float(os.getenv("BATCH_INTERVAL", "10"))   # adaptive ceiling for batch/count loops
BATCH_MIN_INTERVAL = float(os.getenv("BATCH_MIN_INTERVAL", "2"))  # adaptive floor (was fixed 10s)
PROGRESS_MIN_INTERVAL = float(os.getenv("PROGRESS_MIN_INTERVAL", "3"))  # progress edit throttle
MERGE_INTERVAL   = float(os.getenv("MERGE_INTERVAL", "5"))    # merge: between links
CHANNEL_INTERVAL = float(os.getenv("CHANNEL_INTERVAL", "5"))  # merge: between channels
UPLOAD_INTERVAL  = float(os.getenv("UPLOAD_INTERVAL", "2"))   # after each media upload
MAX_FLOOD_RETRIES = int(os.getenv("MAX_FLOOD_RETRIES", "3"))  # FloodWait retry attempts

# ─── UI / LINKS ─────────────────────────────────────────────────────────────────
JOIN_LINK     = os.getenv("JOIN_LINK", "https://t.me/team_spy_pro")
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "https://t.me/username_of_admin")

# ─── PAY NOTICE (统一付费提示文案) ─────────────────────────────────────────────
# 所有支付/会员入口均展示此文案，引导用户联系人工开通。
# 通过环境变量 PAY_NOTICE 配置；默认值不含真实联系方式，部署时在 .env 中填写。
PAY_NOTICE = os.getenv(
    "PAY_NOTICE",
    "私密消息转发BOT（限私域使用），如需使用请联系管理员付费。"
)

# ════════════════════════════════════════════════════════════════════════════════
# ░ PREMIUM PLANS CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════════

P0 = {
    "d": {
        "s": int(os.getenv("PLAN_D_S", 1)),
        "du": int(os.getenv("PLAN_D_DU", 1)),
        "u": os.getenv("PLAN_D_U", "days"),
        "l": os.getenv("PLAN_D_L", "Daily"),
    },
    "w": {
        "s": int(os.getenv("PLAN_W_S", 3)),
        "du": int(os.getenv("PLAN_W_DU", 1)),
        "u": os.getenv("PLAN_W_U", "weeks"),
        "l": os.getenv("PLAN_W_L", "Weekly"),
    },
    "m": {
        "s": int(os.getenv("PLAN_M_S", 5)),
        "du": int(os.getenv("PLAN_M_DU", 1)),
        "u": os.getenv("PLAN_M_U", "month"),
        "l": os.getenv("PLAN_M_L", "Monthly"),
    },
}

# ════════════════════════════════════════════════════════════════════════════════
# ░ DEVGAGAN
# ════════════════════════════════════════════════════════════════════════════════
