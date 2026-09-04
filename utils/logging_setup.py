# Copyright (c) 2025 devgagan : https://github.com/devgajanin.
# Licensed under the GNU General Public License v3.0
# See LICENSE file in the repository root for full license text.

"""Process-wide logging setup for manual-testing observability.

Design (boring on purpose):
  - Root logger level from ``LOG_LEVEL`` (default INFO; set DEBUG while
    hand-testing the bot).
  - RotatingFileHandler on ``$TMPDIR/../logs/bot.log`` — i.e. /data/logs/
    bot.log in the container, on the persistent volume, so logs survive
    restarts for post-mortem reading. 20 MB x 10 files bounds disk use.
  - stdout keeps a plain StreamHandler: docker logs stays as-is.
  - ``LOG_TEE_STDOUT=1`` (default on) tees bare ``print()`` output — the
    delivery layer still uses print — into the same file, so ONE file
    holds the full story during a test session.
  - pyrogram's own logger is capped at ``PYROGRAM_LOG_LEVEL`` (default
    WARNING): at DEBUG it emits per-packet noise that buries real signal.
  - aiohttp access logging stays off (HealthServer passes access_log=None),
    so healthchecks do not pollute the file.
"""

import io
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class _Tee(io.TextIOBase):
    """Write-through splitter: original stream + the bot log file."""

    def __init__(self, original, file_stream):
        self._original = original
        self._file = file_stream

    def write(self, s):
        try:
            self._original.write(s)
        except Exception:
            pass
        try:
            self._file.write(s)
            self._file.flush()
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        try:
            self._file.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._original.isatty()
        except Exception:
            return False


def _log_dir():
    # shared_client resolves workdir from TMPDIR (/data/tmp -> /data); the
    # log dir is the volume root's logs/ so it survives container restarts.
    base = os.environ.get("TMPDIR", os.getcwd())
    if base.endswith("/tmp"):
        base = base[:-4]
    return os.path.join(base, "logs")


def setup_logging():
    """Idempotent; call once at process start (main.py). Returns log path."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT)

    stream = logging.StreamHandler(sys.__stdout__ or sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    log_path = None
    try:
        log_dir = _log_dir()
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "bot.log")
        rotating = RotatingFileHandler(
            log_path, maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8")
        rotating.setFormatter(formatter)
        root.addHandler(rotating)

        if os.environ.get("LOG_TEE_STDOUT", "1") not in ("0", "false", "no"):
            log_file = open(log_path, "a", encoding="utf-8", buffering=1)
            sys.stdout = _Tee(sys.__stdout__ or sys.stdout, log_file)
            sys.stderr = _Tee(sys.__stderr__ or sys.stderr, log_file)
    except OSError as exc:
        # read-only FS or missing perms: stdout logging still works
        root.warning("file logging disabled (%s); using stdout only", exc)

    pyrogram_level = getattr(
        logging, os.environ.get("PYROGRAM_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
    logging.getLogger("pyrogram").setLevel(pyrogram_level)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    root.info("logging ready: level=%s file=%s", level_name, log_path or "<stdout-only>")
    return log_path
