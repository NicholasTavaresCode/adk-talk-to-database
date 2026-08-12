import asyncio, logging, time
from google.adk.plugins import BasePlugin

_MIN_INTERVAL_SEC: float = 1.0


class RateLimiterPlugin(BasePlugin):
    """App-level plugin that throttles LLM calls across all agents."""

    def __init__(self, min_interval: float = _MIN_INTERVAL_SEC):
        super().__init__(name="rate_limiter")
        self._min_interval = min_interval
        self._last_call_time: float = 0.0

    async def before_model_callback(
        self, *, callback_context, llm_request
    ):
        now = time.monotonic()
        elapsed = now - self._last_call_time
        if elapsed < self._min_interval:
            logging.info(
                f"RateLimiterPlugin: Throttling LLM call. "
                f"Elapsed={elapsed:.2f}s, waiting for {self._min_interval - elapsed:.2f}s"
            )
            await asyncio.sleep(self._min_interval - elapsed)

        self._last_call_time = time.monotonic()
        return None