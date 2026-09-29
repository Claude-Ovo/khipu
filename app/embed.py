"""text-embedding-v4（阿里百炼 OpenAI 兼容口）。没 key 就返回 None，调用方按 BM25-only 继续。"""
from __future__ import annotations

import asyncio
import logging

import httpx

from . import config

log = logging.getLogger("aml.embed")


def _clip(text: str) -> str:
    return text if len(text) <= config.EMBED_MAX_CHARS else text[: config.EMBED_MAX_CHARS]


async def _post(client: httpx.AsyncClient, inputs: list[str]) -> list[list[float]] | None:
    body = {"model": config.EMBED_MODEL, "input": [_clip(t) for t in inputs], "dimensions": config.EMBED_DIM,
            "encoding_format": "float"}
    delay = 1.0
    for attempt in range(4):
        try:
            r = await client.post("/embeddings", json=body, timeout=config.EMBED_TIMEOUT_S)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
            if r.status_code >= 400:  # 4xx 带上百炼的原话，同一输入反复 400 时才知道为什么
                log.warning("embed %s: %s", r.status_code, r.text[:300])
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            return [d["embedding"] for d in data]
        except (httpx.HTTPError, KeyError, ValueError) as e:  # noqa: PERF203
            log.warning("embed attempt %d failed: %s %s", attempt + 1, type(e).__name__, e)
            if attempt == 3:
                return None
            await asyncio.sleep(delay)
            delay *= 2
    return None


async def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """按批调用；某一批失败就该批全 None，别的批不受影响。"""
    if not config.EMBED_API_KEY or not texts:
        return [None] * len(texts)
    out: list[list[float] | None] = [None] * len(texts)
    async with httpx.AsyncClient(base_url=config.EMBED_BASE_URL,
                                 headers={"Authorization": f"Bearer {config.EMBED_API_KEY}"}) as client:
        batches = [(i, texts[i:i + config.EMBED_BATCH]) for i in range(0, len(texts), config.EMBED_BATCH)]
        sem = asyncio.Semaphore(4)

        async def run(start: int, chunk: list[str]) -> None:
            async with sem:
                vecs = await _post(client, chunk)
            if vecs:
                for j, v in enumerate(vecs):
                    out[start + j] = v

        await asyncio.gather(*(run(s, c) for s, c in batches))
    return out


async def embed_query(text: str) -> list[float] | None:
    res = await embed_texts([text])
    return res[0] if res else None
