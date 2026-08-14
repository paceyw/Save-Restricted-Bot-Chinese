"""AIMD-style adaptive intervals tighten toward the base when no FloodWait occurs and back off toward the ceiling after FloodWait; BATCH_INTERVAL remains the ceiling, so the old fixed-10-second behavior is restorable by setting BATCH_MIN_INTERVAL=10 through the environment."""

import asyncio
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, base, ceiling, factor=3.0):
        self._base = float(base)
        self._ceiling = float(ceiling)
        self._factor = float(factor)
        self._current = self._base
        # Set by report_flood, consumed by the next report_success: a flood
        # observed by a concurrent prefetch must not be halved away by the
        # predecessor link's success in the same overlap window.
        self._flooded = False

    @property
    def current(self):
        return self._current

    async def wait(self):
        await asyncio.sleep(self.current)

    def report_success(self):
        if self._flooded:
            self._flooded = False
            return
        previous = self._current
        self._current = max(self._base, self._current / 2.0)
        if previous != self._base and self._current == self._base:
            # Transition-level log (not per success): marks full recovery.
            logger.info('rate interval recovered to base %.1fs', self._base)

    def report_flood(self, secs=None):
        self._flooded = True
        previous = self._current
        backed_off = self._current * self._factor
        if secs is not None:
            backed_off = max(backed_off, float(secs))
        self._current = min(self._ceiling, backed_off)
        # Production-analysis signal: every backoff transition with cause.
        logger.info(
            'rate interval backoff %.1fs -> %.1fs (flood_wait=%ss)',
            previous, self._current, secs,
        )


__all__ = ['RateLimiter']
