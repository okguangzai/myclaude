"""Cancellation-safe per-process model/media admission control."""

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlsplit


def allow_cloud(cfg, url):
    if not cfg.privacy_allow_cloud and urlsplit(url).hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("隐私设置已禁止云模型和云语音请求；本机服务仍可使用")


class Limiter:
    def __init__(self, window=60):
        self.window, self.starts, self.active = window, deque(), 0
        self.changed = asyncio.Condition()

    @asynccontextmanager
    async def slot(self, cfg):
        async with self.changed:
            while True:
                now = time.monotonic()
                while self.starts and self.starts[0] <= now - self.window:
                    self.starts.popleft()
                if (
                    self.active < cfg.rate_limit_concurrency
                    and len(self.starts) < cfg.rate_limit_rpm
                ):
                    self.active += 1
                    self.starts.append(now)
                    break
                delay = (
                    max(0.001, self.window - (now - self.starts[0]))
                    if len(self.starts) >= cfg.rate_limit_rpm
                    else 1
                )
                try:
                    await asyncio.wait_for(self.changed.wait(), delay)
                except TimeoutError:
                    pass
        try:
            yield
        finally:
            async with self.changed:
                self.active -= 1
                self.changed.notify_all()
