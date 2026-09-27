"""重排：阿里百炼 gte-rerank-v2（比赛规则 Q05：reranker 不限）。
只对融合后的前 RERANK_TOPN 条打分，失败就原样返回，调用方按 RRF 顺序继续。"""
from __future__ import annotations

import asyncio
import logging

import httpx

from . import config

log = logging.getLogger("aml.rerank")


async def rerank(query: str, docs: list[str]) -> list[float] | None:
    """返回与 docs 等长的相关性分数（0~1）；不可用时返回 None。"""
    if not config.RERANK_ENABLED or not config.EMBED_API_KEY or not docs:
        return None
    body = {"model": config.RERANK_MODEL,
            "input": {"query": query[: config.EMBED_MAX_CHARS], "documents": [d[: config.EMBED_MAX_CHARS] for d in docs]},
            "parameters": {"return_documents": False, "top_n": len(docs)}}
    delay = 1.0
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {config.EMBED_API_KEY}"}) as client:
        for attempt in range(3):
            try:
                r = await client.post(config.RERANK_URL, json=body, timeout=config.RERANK_TIMEOUT_S)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"status {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                scores = [0.0] * len(docs)
                for item in r.json()["output"]["results"]:
                    scores[int(item["index"])] = float(item["relevance_score"])
                return scores
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:  # noqa: PERF203
                log.warning("rerank attempt %d failed: %s", attempt + 1, e)
                if attempt == 2:
                    return None
                await asyncio.sleep(delay)
                delay *= 2
    return None
