"""百炼两条线（向量、重排）共用的 HTTP 客户端和用量计数。
第一次 Full 的教训（桌面日志 #1）：每次调用都新建 AsyncClient，首尔到北京每次重新握手，10 秒读超时下
首次尝试失败 1,686 次、建连超时占四成。现在全进程一个客户端、长连接复用、连接和读超时分开、
每路一个全局并发上限；每次响应的 usage.total_tokens 记进计数器，/health 能看到这一场花了多少 token。"""
from __future__ import annotations

import asyncio
import logging
import threading

import httpx

from . import config

log = logging.getLogger("aml.http")

_client: httpx.AsyncClient | None = None

embed_sem = asyncio.Semaphore(config.EMBED_CONCURRENCY)
rerank_sem = asyncio.Semaphore(config.RERANK_CONCURRENCY)


def client() -> httpx.AsyncClient:
    """懒建；uvicorn 单 worker 单事件循环，进程里只会有这一个。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {config.EMBED_API_KEY}"},
            limits=httpx.Limits(max_connections=config.HTTP_MAX_CONNECTIONS,
                                max_keepalive_connections=config.HTTP_MAX_CONNECTIONS,
                                keepalive_expiry=config.HTTP_KEEPALIVE_S),
            timeout=httpx.Timeout(connect=config.EMBED_CONNECT_TIMEOUT_S, read=config.EMBED_TIMEOUT_S,
                                  write=10.0, pool=config.EMBED_CONNECT_TIMEOUT_S),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def retryable_status(status: int) -> bool:
    """429 和 5xx 重试；别的 4xx（400 欠费、401 key 错、413 太长）重试也不会变，立刻放弃。"""
    return status == 429 or status >= 500


def usage_tokens(payload: object) -> int | None:
    """OpenAI 兼容口和百炼原生口都把用量放在 usage.total_tokens。"""
    if isinstance(payload, dict):
        u = payload.get("usage")
        if isinstance(u, dict):
            t = u.get("total_tokens")
            if isinstance(t, (int, float)):
                return int(t)
    return None


class _Usage:
    """进程内累计，重启清零。只做计数，不做判断。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.embed_calls = self.embed_ok = self.embed_failed = self.embed_tokens = 0
        self.embed_ms = 0
        self.rerank_calls = self.rerank_ok = self.rerank_failed = self.rerank_tokens = 0
        self.rerank_ms = 0
        self.rerank_timeouts = 0      # 外层 RERANK_TIMEOUT_S 掐断的次数（search.py 记）
        self.rerank_skipped = 0       # 最终没做重排、按融合顺序返回的检索次数

    def embed(self, ok: bool, tokens: int | None, ms: int) -> None:
        with self._lock:
            self.embed_calls += 1
            self.embed_ms += ms
            if ok:
                self.embed_ok += 1
                self.embed_tokens += tokens or 0
            else:
                self.embed_failed += 1

    def rerank(self, ok: bool, tokens: int | None, ms: int) -> None:
        with self._lock:
            self.rerank_calls += 1
            self.rerank_ms += ms
            if ok:
                self.rerank_ok += 1
                self.rerank_tokens += tokens or 0
            else:
                self.rerank_failed += 1

    def rerank_gave_up(self, timeout: bool) -> None:
        with self._lock:
            self.rerank_skipped += 1
            if timeout:
                self.rerank_timeouts += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "embed": {"calls": self.embed_calls, "ok": self.embed_ok, "failed": self.embed_failed,
                          "tokens": self.embed_tokens,
                          "avg_ms": round(self.embed_ms / self.embed_calls) if self.embed_calls else 0},
                "rerank": {"calls": self.rerank_calls, "ok": self.rerank_ok, "failed": self.rerank_failed,
                           "tokens": self.rerank_tokens,
                           "avg_ms": round(self.rerank_ms / self.rerank_calls) if self.rerank_calls else 0,
                           "skipped": self.rerank_skipped, "timeouts": self.rerank_timeouts},
            }


usage = _Usage()
