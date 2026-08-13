import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1]


class _FakeMongo:
    def __init__(self, *args, **kwargs):
        pass

    def __getitem__(self, key):
        return self


class _FakeProcess:
    def __init__(self, stdout=b"", returncode=0, stderr=b""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr

    async def communicate(self):
        return self.stdout, self.stderr


@pytest.fixture
def func_module(monkeypatch):
    motor = types.ModuleType("motor")
    motor_asyncio = types.ModuleType("motor.motor_asyncio")
    motor_asyncio.AsyncIOMotorClient = _FakeMongo
    motor.motor_asyncio = motor_asyncio
    monkeypatch.setitem(sys.modules, "motor", motor)
    monkeypatch.setitem(sys.modules, "motor.motor_asyncio", motor_asyncio)

    config = types.ModuleType("config")
    config.MONGO_DB = "mongodb://unused"
    config.DB_NAME = "test"
    monkeypatch.setitem(sys.modules, "config", config)

    module_name = "test_video_metadata_func"
    spec = importlib.util.spec_from_file_location(module_name, SRC / "utils" / "func.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _stub_ffprobe(monkeypatch, module, process):
    async def fake_create_subprocess_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


def test_get_video_metadata_parses_ffprobe_json(monkeypatch, func_module):
    process = _FakeProcess(
        json.dumps(
            {
                "streams": [{"width": 1920, "height": 1080}],
                "format": {"duration": "12.6"},
            }
        ).encode()
    )
    _stub_ffprobe(monkeypatch, func_module, process)

    result = asyncio.run(func_module.get_video_metadata("/tmp/video.mp4"))

    assert result == {"width": 1920, "height": 1080, "duration": 13}


@pytest.mark.parametrize(
    "stdout, returncode",
    [(b"not json", 0), (b"", 1)],
)
def test_get_video_metadata_failure_returns_defaults(
    monkeypatch, func_module, stdout, returncode
):
    _stub_ffprobe(monkeypatch, func_module, _FakeProcess(stdout, returncode))

    result = asyncio.run(func_module.get_video_metadata("/tmp/video.mp4"))

    assert result == {"width": 1, "height": 1, "duration": 1}


@pytest.mark.parametrize(
    "payload",
    [
        {"streams": [{"width": 1920, "height": 1080}], "format": {"duration": "0.0"}},
        {"streams": [], "format": {"duration": "12.0"}},
    ],
)
def test_get_video_metadata_invalid_duration_or_stream_returns_defaults(
    monkeypatch, func_module, payload
):
    _stub_ffprobe(monkeypatch, func_module, _FakeProcess(json.dumps(payload).encode()))

    result = asyncio.run(func_module.get_video_metadata("/tmp/video.mp4"))

    assert result == {"width": 1, "height": 1, "duration": 1}
