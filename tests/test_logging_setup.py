"""Observability tests: logging setup honors env knobs and tees stdout."""

import importlib.util
import logging
import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]


@pytest.fixture()
def logging_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "data" / "tmp"))
    (tmp_path / "data" / "tmp").mkdir(parents=True)
    # restore real stdout/stderr and root handlers after each test
    real_out, real_err = sys.stdout, sys.stderr
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    sys.stdout, sys.stderr = real_out, real_err
    root.handlers = handlers
    root.setLevel(level)


def _load():
    spec = importlib.util.spec_from_file_location(
        "logging_setup_mod", SRC / "utils" / "logging_setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_log_dir_lands_next_to_workdir_root(logging_setup, tmp_path):
    mod = _load()
    assert mod._log_dir() == str(tmp_path / "data" / "logs")


def test_file_created_and_level_applied(logging_setup, tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    mod = _load()
    path = mod.setup_logging()
    assert path == str(tmp_path / "data" / "logs" / "bot.log")
    assert os.path.isfile(path)
    assert logging.getLogger().level == logging.DEBUG
    logging.getLogger("t").debug("marker-debug-123")
    assert "marker-debug-123" in open(path, encoding="utf-8").read()


def test_pyrogram_capped_by_default(logging_setup, monkeypatch):
    monkeypatch.delenv("PYROGRAM_LOG_LEVEL", raising=False)
    mod = _load()
    mod.setup_logging()
    assert logging.getLogger("pyrogram").level == logging.WARNING
    monkeypatch.setenv("PYROGRAM_LOG_LEVEL", "INFO")
    mod.setup_logging()
    assert logging.getLogger("pyrogram").level == logging.INFO


def test_stdout_tee_captures_print(logging_setup, tmp_path, monkeypatch):
    mod = _load()
    path = mod.setup_logging()
    print("tee-marker-abc")
    sys.stdout.flush()
    assert "tee-marker-abc" in open(path, encoding="utf-8").read()


def test_tee_can_be_disabled(logging_setup, tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_TEE_STDOUT", "0")
    mod = _load()
    mod.setup_logging()
    assert not isinstance(sys.stdout, mod._Tee)
