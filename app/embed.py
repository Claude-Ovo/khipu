"""text-embedding-v4（阿里百炼 OpenAI 兼容口）。没 key 就返回 None，调用方按 BM25-only 继续。
客户端、超时、并发上限、用量计数都在 httpclient.py；这里只管一批文本怎么变成向量。"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import config
from .httpclient import client, embed_sem, retryable_status, usage, usage_tokens

log = logging.getLogger("aml.embed")


class _Retry(Exception):
    """429 / 5xx：换个时间再试。"""


def _clip(text: str) -> str:
    return text if len(text) <= config.EMBED_MAX_CHARS else text[: config.EMBED_MAX_CHARS]


async def _post(inputs: list[str]) -> list[list[float]] | None:
    body = {"model": config.EMBED_MODEL, "input": [_clip(t) for t in inputs], "dimensions": config.EMBED_DIM,
            "encoding_format": "float"}
    delay = 1.0
    for attempt in range(4):
        t0 = time.monotonic()
        try:
            async with embed_sem:
                r = await client().post(f"{config.EMBED_BASE_URL}/embeddings", json=body)
            ms = int((time.monotonic() - t0) * 1000)
            if retryable_status(r.status_code):
                raise _Retry(f"status {r.status_code}")
            if r.status_code >= 400:  # 400 欠费 / 401 key 错 / 413 太长：重试不会变，记下百炼的原话就放弃
                log.warning("embed %s (not retried): %s", r.status_code, r.text[:300])
                usage.embed(False, None, ms)
                return None
            payload = r.json()
            data = sorted(payload["data"], key=lambda d: d["index"])
            vecs = [d["embedding"] for d in data]
            if len(vecs) != len(inputs):
                raise ValueError(f"embedding response has {len(vecs)} vectors for {len(inputs)} inputs")
            tokens = usage_tokens(payload)
            usage.embed(True, tokens, ms)
            log.info("embed ok n=%d tokens=%s %dms attempt=%d", len(inputs), tokens, ms, attempt + 1)
            return vecs
        except (httpx.HTTPError, _Retry, KeyError, ValueError, TypeError) as e:  # noqa: PERF203
            ms = int((time.monotonic() - t0) * 1000)
            log.warning("embed attempt %d failed after %dms: %s %s", attempt + 1, ms, type(e).__name__, e)
            if attempt == 3:
                usage.embed(False, None, ms)
                return None
            await asyncio.sleep(delay)
            delay *= 2
    return None


async def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """按批调用；某一批失败就该批全 None，别的批不受影响。一次调用最多 4 批同时在飞，全进程再受 EMBED_CONCURRENCY 限制。"""
    if not config.EMBED_API_KEY or not texts:
        return [None] * len(texts)
    out: list[list[float] | None] = [None] * len(texts)
    batches = [(i, texts[i:i + config.EMBED_BATCH]) for i in range(0, len(texts), config.EMBED_BATCH)]
    sem = asyncio.Semaphore(4)

    async def run(start: int, chunk: list[str]) -> None:
        async with sem:
            vecs = await _post(chunk)
        if vecs:
            for j, v in enumerate(vecs):
                out[start + j] = v

    await asyncio.gather(*(run(s, c) for s, c in batches))
    return out


async def embed_query(text: str) -> list[float] | None:
    res = await embed_texts([text])
    return res[0] if res else None
