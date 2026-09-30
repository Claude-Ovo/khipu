"""重排：阿里百炼 gte-rerank-v2（比赛规则 Q05：reranker 不限）。
只对融合后的前 RERANK_TOPN 条打分，失败就返回 None，调用方按 RRF 顺序继续。
单次尝试的读超时是 RERANK_ATTEMPT_TIMEOUT_S，整步的上限 RERANK_TIMEOUT_S 由 search.py 掐；
第一次 Full 时两者都是 45 秒，一次慢请求就把整步吃光，没有第二次机会。"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import config
from .httpclient import client, rerank_sem, retryable_status, usage, usage_tokens

log = logging.getLogger("aml.rerank")


class _Retry(Exception):
    """429 / 5xx：换个时间再试。"""


_ATTEMPT_TIMEOUT = httpx.Timeout(connect=config.EMBED_CONNECT_TIMEOUT_S, read=config.RERANK_ATTEMPT_TIMEOUT_S,
                                 write=10.0, pool=config.EMBED_CONNECT_TIMEOUT_S)


async def rerank(query: str, docs: list[str]) -> list[float] | None:
    """返回与 docs 等长的相关性分数（0~1）；不可用时返回 None。"""
    if not config.RERANK_ENABLED or not config.EMBED_API_KEY or not docs:
        return None
    body = {"model": config.RERANK_MODEL,
            "input": {"query": query[: config.EMBED_MAX_CHARS], "documents": [d[: (config.RERANK_DOC_CHARS or config.EMBED_MAX_CHARS)] for d in docs]},
            "parameters": {"return_documents": False, "top_n": len(docs)}}
    delay = 1.0
    for attempt in range(3):
        t0 = time.monotonic()
        try:
            async with rerank_sem:
                r = await client().post(config.RERANK_URL, json=body, timeout=_ATTEMPT_TIMEOUT)
            ms = int((time.monotonic() - t0) * 1000)
            if retryable_status(r.status_code):
                raise _Retry(f"status {r.status_code}")
            if r.status_code >= 400:  # 400 欠费 / 413 超过 30k token 上限：重试不会变，记下原话就放弃
                log.warning("rerank %s (not retried): %s", r.status_code, r.text[:300])
                usage.rerank(False, None, ms)
                return None
            payload = r.json()
            # 审查 #4：响应必须每条候选恰好一个合法有限分数，缺项/越界/重复/NaN 一律整次降级，不能把没评的当 0 分
            results = payload["output"]["results"]
            scores: list[float | None] = [None] * len(docs)
            for item in results:
                i = int(item["index"])
                s = float(item["relevance_score"])
                if not (0 <= i < len(docs)) or scores[i] is not None or s != s or s in (float("inf"), float("-inf")):
                    raise ValueError(f"bad rerank response item index={i} score={s}")
                scores[i] = s
            if any(s is None for s in scores):
                raise ValueError(f"rerank response incomplete: {sum(s is None for s in scores)} of {len(docs)} unscored")
            tokens = usage_tokens(payload)
            usage.rerank(True, tokens, ms)
            log.info("rerank ok docs=%d tokens=%s %dms attempt=%d", len(docs), tokens, ms, attempt + 1)
            return [float(s) for s in scores]  # type: ignore[arg-type]
        except (httpx.HTTPError, _Retry, KeyError, ValueError, TypeError) as e:  # noqa: PERF203
            ms = int((time.monotonic() - t0) * 1000)
            log.warning("rerank attempt %d failed after %dms: %s %s", attempt + 1, ms, type(e).__name__, e)
            if attempt == 2:
                usage.rerank(False, None, ms)
                return None
            await asyncio.sleep(delay)
            delay *= 2
    return None
