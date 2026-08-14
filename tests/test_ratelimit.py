import asyncio
import importlib
import os


os.environ.setdefault('MASTER_KEY', 'phase7-master-key')
os.environ.setdefault('IV_KEY', 'phase7-iv-key')

from utils.ratelimit import RateLimiter


def test_default_construction_starts_at_base():
    limiter = RateLimiter(2, 10)

    assert limiter.current == 2.0


def test_wait_sleeps_for_current_interval(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, 'sleep', fake_sleep)
    limiter = RateLimiter(2, 10)
    limiter.report_flood()

    asyncio.run(limiter.wait())

    assert slept == [6.0]


def test_report_flood_multiplies_and_caps_at_ceiling():
    limiter = RateLimiter(2, 10)

    limiter.report_flood()
    assert limiter.current == 6.0

    limiter.report_flood()
    assert limiter.current == 10.0

    limiter.report_flood()
    assert limiter.current == 10.0


def test_report_flood_secs_can_raise_interval_beyond_factor_step():
    limiter = RateLimiter(2, 10)

    limiter.report_flood(secs=9)

    assert limiter.current == 9.0

    limiter.report_flood(secs=20)

    assert limiter.current == 10.0


def test_report_success_halves_toward_base_without_crossing_it():
    limiter = RateLimiter(2, 20)
    limiter.report_flood()
    limiter.report_flood()

    assert limiter.current == 18.0
    # The first success after a flood is consumed by the flood flag: a flood
    # observed by a concurrent prefetch must survive the predecessor's
    # success in the same overlap window.
    limiter.report_success()
    assert limiter.current == 18.0
    limiter.report_success()
    assert limiter.current == 9.0
    limiter.report_success()
    assert limiter.current == 4.5
    limiter.report_success()
    assert limiter.current == 2.25
    limiter.report_success()
    assert limiter.current == 2.0
    limiter.report_success()
    assert limiter.current == 2.0


def test_flood_flag_suppresses_exactly_one_success():
    limiter = RateLimiter(2, 10)

    limiter.report_flood()
    assert limiter.current == 6.0
    limiter.report_success()  # consumed by the flood flag
    assert limiter.current == 6.0
    limiter.report_success()
    assert limiter.current == 3.0
    limiter.report_success()
    assert limiter.current == 2.0


def test_full_flood_and_success_cycle_recovers_to_base():
    limiter = RateLimiter(2, 10)

    limiter.report_flood()
    limiter.report_flood()
    assert limiter.current == 10.0

    # First success is consumed by the flood flag; three more halve
    # 10 -> 5 -> 2.5 -> 2 (floored at base).
    for _ in range(4):
        limiter.report_success()

    assert limiter.current == 2.0


def test_config_rate_control_defaults(monkeypatch):
    monkeypatch.delenv('BATCH_MIN_INTERVAL', raising=False)
    monkeypatch.delenv('PROGRESS_MIN_INTERVAL', raising=False)
    monkeypatch.setenv('MASTER_KEY', 'phase7-master-key')
    monkeypatch.setenv('IV_KEY', 'phase7-iv-key')

    import config

    config = importlib.reload(config)

    assert config.BATCH_MIN_INTERVAL == 2.0
    assert config.PROGRESS_MIN_INTERVAL == 3.0
