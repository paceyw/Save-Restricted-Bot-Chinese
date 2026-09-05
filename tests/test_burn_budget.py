"""Transcode budget tests (plan Phase 1 §4.2).

Covers the four budget guarantees for the libx264 subtitle burn:
  1. BURN_CONCURRENCY serializes concurrent re-encodes.
  2. FFMPEG_BURN_THREADS overrides the CPU heuristic when set.
  3. _run_ffmpeg timeout kills and reaps the child, raising MissAVError
     (so callers fall back to the plain remux path).
  4. Task cancellation kills and reaps the child too.
  5. burn.start/burn.done/burn.fail logs carry task id + sizes + duration.

All offline: ffmpeg process spawning is faked; semaphore behaviour is real.
"""

import asyncio
import os
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]

os.environ.setdefault("MASTER_KEY", "burn-test-master")
os.environ.setdefault("IV_KEY", "burn-test-iv")

import importlib.util

spec = importlib.util.spec_from_file_location("missav_mod", SRC / "utils" / "missav.py")
missav = importlib.util.module_from_spec(spec)
import sys
sys.modules["missav_mod"] = missav
spec.loader.exec_module(missav)


@pytest.fixture(autouse=True)
def ffmpeg_present(monkeypatch):
    monkeypatch.setattr(missav.shutil, "which", lambda name: "/usr/bin/ffmpeg")


def test_burn_semaphore_serializes_concurrent_encodes(monkeypatch, tmp_path):
    events = []

    async def fake_run(args, timeout_s=None, env=None):
        events.append("start")
        await asyncio.sleep(0.05)
        events.append("end")

    monkeypatch.setattr(missav, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(missav.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(missav.os.path, "getsize", lambda p: 10)
    # fresh 1-slot semaphore for isolation
    monkeypatch.setattr(missav, "_burn_slots", asyncio.Semaphore(1))

    async def scenario():
        await asyncio.gather(*[
            missav.burn_subtitles_to_mp4("a.ts", f"out{i}.mp4", "s.vtt", task_id=f"t{i}")
            for i in range(3)
        ])

    asyncio.run(scenario())
    # With one slot, encodes cannot interleave: start/end strictly alternate.
    assert events == ["start", "end"] * 3


def test_burn_threads_config_override(monkeypatch, tmp_path):
    calls = []

    async def fake_run(args, timeout_s=None, env=None):
        calls.append(args)

    monkeypatch.setattr(missav, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(missav.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(missav.os.path, "getsize", lambda p: 10)
    monkeypatch.setattr(missav, "FFMPEG_BURN_THREADS", 3)

    asyncio.run(missav.burn_subtitles_to_mp4("a.ts", "a.mp4", "s.vtt"))
    args = calls[0]
    assert args[args.index("-threads") + 1] == "3"


def test_run_ffmpeg_timeout_kills_child(monkeypatch):
    class FakeProc:
        def __init__(self):
            self.killed = False
            self.reaped = False
            self.returncode = None

        def kill(self):
            self.killed = True

        async def wait(self):
            self.reaped = True
            self.returncode = -9
            return self.returncode

        async def communicate(self):
            await asyncio.sleep(10)  # longer than the timeout
            return b"", b""

    proc = FakeProc()

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(missav.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(missav.MissAVError, match="超时"):
        asyncio.run(missav._run_ffmpeg(["ffmpeg", "-y"], timeout_s=0.05))
    assert proc.killed and proc.reaped


def test_run_ffmpeg_cancellation_kills_child(monkeypatch):
    class FakeProc:
        def __init__(self):
            self.killed = False
            self.reaped = False

        def kill(self):
            self.killed = True

        async def wait(self):
            self.reaped = True

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

    proc = FakeProc()

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(missav.asyncio, "create_subprocess_exec", fake_exec)

    async def scenario():
        task = asyncio.create_task(missav._run_ffmpeg(["ffmpeg", "-y"]))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert proc.killed and proc.reaped


def test_burn_failure_logs_exit_code_and_raises(monkeypatch, caplog):
    async def failing_run(args, timeout_s=None, env=None):
        exc = missav.MissAVError("ffmpeg remux 失败: boom")
        exc.exit_code = 3
        raise exc

    monkeypatch.setattr(missav, "_run_ffmpeg", failing_run)
    monkeypatch.setattr(missav.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(missav.os.path, "getsize", lambda p: 7)

    import logging
    with caplog.at_level(logging.INFO, logger="missav_mod"):
        with pytest.raises(missav.MissAVError):
            asyncio.run(missav.burn_subtitles_to_mp4("a.ts", "a.mp4", "s.vtt", task_id="job-9"))

    starts = [r for r in caplog.records if r.getMessage().startswith("burn.start")]
    fails = [r for r in caplog.records if r.getMessage().startswith("burn.fail")]
    assert len(starts) == 1 and "task=job-9" in starts[0].getMessage()
    assert "threads=" in starts[0].getMessage()
    assert len(fails) == 1
    msg = fails[0].getMessage()
    assert "task=job-9" in msg and "exit=3" in msg and "input_bytes=7" in msg


def test_burn_success_logs_sizes_and_duration(monkeypatch, caplog):
    sizes = {"a.ts": 100, "a.mp4": 80}

    async def fake_run(args, timeout_s=None, env=None):
        pass

    monkeypatch.setattr(missav, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(missav.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(missav.os.path, "getsize", lambda p: sizes[p])

    import logging
    with caplog.at_level(logging.INFO, logger="missav_mod"):
        asyncio.run(missav.burn_subtitles_to_mp4("a.ts", "a.mp4", "s.vtt", task_id="job-1"))

    done = [r for r in caplog.records if r.getMessage().startswith("burn.done")]
    assert len(done) == 1
    msg = done[0].getMessage()
    assert "task=job-1" in msg and "input_bytes=100" in msg and "output_bytes=80" in msg
    assert "duration_s=" in msg


def test_burn_passes_timeout_to_ffmpeg_runner(monkeypatch):
    timeouts = []

    async def fake_run(args, timeout_s=None, env=None):
        timeouts.append(timeout_s)

    monkeypatch.setattr(missav, "_run_ffmpeg", fake_run)
    monkeypatch.setattr(missav.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(missav.os.path, "getsize", lambda p: 1)
    monkeypatch.setattr(missav, "BURN_TIMEOUT_S", 1234)

    asyncio.run(missav.burn_subtitles_to_mp4("a.ts", "a.mp4", "s.vtt"))
    assert timeouts == [1234]
