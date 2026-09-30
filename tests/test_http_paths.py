"""百炼调用路径的重试、放弃、超时和用量计数（不连库、不联网）。
第二枪前改的（桌面日志 #1 / #4）：共用客户端、429/5xx 才重试、别的 4xx 立刻放弃、超时原因写进日志、token 记进计数器。
用法：.venv/Scripts/python.exe -m pytest tests/test_http_paths.py -q"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, embed, httpclient, rerank, search  # noqa: E402


class _Script:
    """按顺序回放响应或异常，记下每次调用。"""

    def __init__(self, items):
        self.items = list(items)
        self.calls = 0

    async def post(self, url, **kw):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _resp(status: int, payload: dict | None = None, text: str = "") -> httpx.Response:
    req = httpx.Request("POST", "https://example.invalid/x")
    if payload is not None:
        return httpx.Response(status, json=payload, request=req)
    return httpx.Response(status, text=text, request=req)


def _embed_payload(n: int, tokens: int = 7) -> dict:
    return {"data": [{"index": i, "embedding": [float(i)] * 4} for i in range(n)], "usage": {"total_tokens": tokens}}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(config, "EMBED_API_KEY", "test-key")
    monkeypatch.setattr(config, "RERANK_ENABLED", True)

    async def no_sleep(_):
        return None
    # 只把 embed / rerank 模块里的退避睡眠换掉；asyncio.sleep 本身别动，超时用例要靠它
    monkeypatch.setattr(embed, "asyncio", types.SimpleNamespace(sleep=no_sleep, Semaphore=asyncio.Semaphore, gather=asyncio.gather))
    monkeypatch.setattr(rerank, "asyncio", types.SimpleNamespace(sleep=no_sleep))
    # 每个用例从零计数
    monkeypatch.setattr(httpclient, "usage", httpclient._Usage())
    monkeypatch.setattr(embed, "usage", httpclient.usage)
    monkeypatch.setattr(rerank, "usage", httpclient.usage)
    monkeypatch.setattr(search, "usage", httpclient.usage)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_embed_retries_5xx_then_succeeds(monkeypatch):
    s = _Script([_resp(503, text="busy"), _resp(200, _embed_payload(2, tokens=11))])
    monkeypatch.setattr(embed, "client", lambda: s)
    out = _run(embed.embed_texts(["a", "b"]))
    assert out == [[0.0] * 4, [1.0] * 4]
    assert s.calls == 2
    u = httpclient.usage.snapshot()["embed"]
    assert u == {"calls": 1, "ok": 1, "failed": 0, "tokens": 11, "avg_ms": u["avg_ms"]}


def test_embed_retries_timeouts_and_tracks_type(monkeypatch, caplog):
    s = _Script([httpx.ConnectTimeout("connect"), httpx.ReadTimeout("read"), _resp(200, _embed_payload(1))])
    monkeypatch.setattr(embed, "client", lambda: s)
    with caplog.at_level("WARNING", logger="aml.embed"):
        out = _run(embed.embed_texts(["a"]))
    assert out[0] == [0.0] * 4 and s.calls == 3
    msgs = [r.getMessage() for r in caplog.records]
    assert any("ConnectTimeout" in m for m in msgs) and any("ReadTimeout" in m for m in msgs)


def test_embed_gives_up_after_four_attempts(monkeypatch):
    s = _Script([httpx.ReadTimeout("read")] * 4)
    monkeypatch.setattr(embed, "client", lambda: s)
    out = _run(embed.embed_texts(["a"]))
    assert out == [None] and s.calls == 4
    assert httpclient.usage.snapshot()["embed"]["failed"] == 1


def test_embed_does_not_retry_400(monkeypatch, caplog):
    """欠费（400 Arrearage）、key 错、太长：重试四次只会多等 7 秒，还可能多计费。"""
    s = _Script([_resp(400, text='{"error":{"code":"Arrearage"}}'), _resp(200, _embed_payload(1))])
    monkeypatch.setattr(embed, "client", lambda: s)
    with caplog.at_level("WARNING", logger="aml.embed"):
        out = _run(embed.embed_texts(["a"]))
    assert out == [None] and s.calls == 1
    assert any("Arrearage" in r.getMessage() and "not retried" in r.getMessage() for r in caplog.records)


def test_embed_batches_fail_independently(monkeypatch):
    monkeypatch.setattr(config, "EMBED_BATCH", 2)
    s = _Script([_resp(200, _embed_payload(2)), _resp(400, text="bad")])
    monkeypatch.setattr(embed, "client", lambda: s)
    out = _run(embed.embed_texts(["a", "b", "c"]))
    assert out[0] is not None and out[1] is not None and out[2] is None


def test_embed_rejects_short_response(monkeypatch):
    """返回的向量条数和输入对不上：不能静默把别的段的向量错位存进去。"""
    s = _Script([_resp(200, _embed_payload(1))] * 4)
    monkeypatch.setattr(embed, "client", lambda: s)
    out = _run(embed.embed_texts(["a", "b"]))
    assert out == [None, None]


def _rerank_payload(n: int, tokens: int = 123) -> dict:
    return {"output": {"results": [{"index": i, "relevance_score": 1.0 - i / 10} for i in range(n)]},
            "usage": {"total_tokens": tokens}}


def test_rerank_success_counts_tokens(monkeypatch):
    s = _Script([_resp(200, _rerank_payload(3, tokens=456))])
    monkeypatch.setattr(rerank, "client", lambda: s)
    out = _run(rerank.rerank("q", ["a", "b", "c"]))
    assert out == [1.0, 0.9, 0.8]
    assert httpclient.usage.snapshot()["rerank"]["tokens"] == 456


def test_rerank_400_not_retried(monkeypatch):
    s = _Script([_resp(400, text="input too long"), _resp(200, _rerank_payload(2))])
    monkeypatch.setattr(rerank, "client", lambda: s)
    assert _run(rerank.rerank("q", ["a", "b"])) is None and s.calls == 1


def test_rerank_incomplete_response_degrades(monkeypatch):
    bad = {"output": {"results": [{"index": 0, "relevance_score": 0.5}]}}
    s = _Script([_resp(200, bad)] * 3)
    monkeypatch.setattr(rerank, "client", lambda: s)
    assert _run(rerank.rerank("q", ["a", "b"])) is None and s.calls == 3
    assert httpclient.usage.snapshot()["rerank"]["failed"] == 1


def test_reranked_step_timeout_keeps_order_and_counts(monkeypatch, caplog):
    """外层整步超时：顺序原样返回，日志写明是超时、窗口多大，计数器记一次 timeout。"""
    from app.index import Row, UserIndex

    rows = [Row(i, f"id{i}", "s1", i, 1, 1, "user", None, None, "unknown", f"text {i}", False, False) for i in range(3)]
    idx = UserIndex("u", rows, None, {}, {}, {}, {}, [], {"s1": "session 1"}, {r.id: r.pos for r in rows}, 0, [set() for _ in rows])

    async def slow(query, docs):
        await asyncio.sleep(5)
        return [1.0] * len(docs)
    monkeypatch.setattr(search, "rerank", slow)
    monkeypatch.setattr(config, "RERANK_TIMEOUT_S", 0.05)
    scores = {0: 0.3, 1: 0.2, 2: 0.1}
    with caplog.at_level("WARNING", logger="aml.search"):
        out = _run(search._reranked(idx, "q", [0, 1, 2], scores))
    assert out == [0, 1, 2]
    assert any("step timeout" in r.getMessage() and "window=3" in r.getMessage() for r in caplog.records)
    u = httpclient.usage.snapshot()["rerank"]
    assert u["skipped"] == 1 and u["timeouts"] == 1


def test_reranked_none_counts_skip_without_timeout(monkeypatch):
    from app.index import Row, UserIndex

    rows = [Row(i, f"id{i}", "s1", i, 1, 1, "user", None, None, "unknown", f"text {i}", False, False) for i in range(2)]
    idx = UserIndex("u", rows, None, {}, {}, {}, {}, [], {"s1": "session 1"}, {r.id: r.pos for r in rows}, 0, [set() for _ in rows])

    async def gave_up(query, docs):
        return None
    monkeypatch.setattr(search, "rerank", gave_up)
    out = _run(search._reranked(idx, "q", [1, 0], {1: 0.2, 0: 0.1}))
    assert out == [1, 0]
    u = httpclient.usage.snapshot()["rerank"]
    assert u["skipped"] == 1 and u["timeouts"] == 0


def test_usage_tokens_parsing():
    assert httpclient.usage_tokens({"usage": {"total_tokens": 5}}) == 5
    assert httpclient.usage_tokens({"usage": {"total_tokens": 5.0}}) == 5
    assert httpclient.usage_tokens({"usage": {}}) is None
    assert httpclient.usage_tokens({"data": []}) is None
    assert httpclient.usage_tokens("nope") is None


def test_retryable_status():
    assert httpclient.retryable_status(429) and httpclient.retryable_status(500) and httpclient.retryable_status(503)
    assert not httpclient.retryable_status(400) and not httpclient.retryable_status(401) and not httpclient.retryable_status(413)
