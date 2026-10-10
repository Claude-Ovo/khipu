"""Search 时时间链（app/chain.py）的离线测试：不连库、不联网、不花钱。
输入编号与截断、请求体与缓存键、解析（来源校验、排序、上限）、渲染、调用与缓存、失败时静默不给、检索装配。
用法：.venv/Scripts/python.exe -m pytest tests/test_chain.py -q"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import chain, config, extract, httpclient, search  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _resp(status: int, payload: dict | None = None, text: str = "") -> httpx.Response:
    req = httpx.Request("POST", "https://example.invalid/chat/completions")
    return httpx.Response(status, json=payload, request=req) if payload is not None else httpx.Response(status, text=text, request=req)


def _completion(content: str, pt: int = 100, ct: int = 20) -> dict:
    return {"model": "gpt-4o-mini-2024-07-18", "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct}}


class _Script:
    def __init__(self, items):
        self.items = list(items)
        self.calls = 0
        self.bodies: list[dict] = []

    async def post(self, url, json=None, **kw):
        self.calls += 1
        self.bodies.append(json)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    store: dict[str, tuple] = {}
    monkeypatch.setattr(config, "EXTRACT_API_KEY", "test-key")
    monkeypatch.setattr(config, "EXTRACT_TOKEN_CAP", 0)
    monkeypatch.setattr(config, "CHAIN_TIMEOUT_S", 5)
    monkeypatch.setattr(chain, "_cache_get", lambda sha: store[sha][0] if sha in store else None)
    monkeypatch.setattr(chain, "_cache_put", lambda sha, out, status, pt, ct, version=None: store.setdefault(sha, (out, status, version)))
    monkeypatch.setattr(extract, "extract_sem", asyncio.Semaphore(4))
    monkeypatch.setattr(httpclient, "usage", httpclient._Usage())
    monkeypatch.setattr(extract, "usage", httpclient.usage)
    monkeypatch.setattr(chain, "usage", httpclient.usage)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(extract.asyncio, "sleep", no_sleep)
    return store


def _use(monkeypatch, script):
    monkeypatch.setattr(extract, "llm_client", lambda: script)


LINES = ["[2023-05-20 (Sat)] user: I went to the Tribeca film festival last weekend, it was great",
         "[2023-06-02 (Fri)] user: just got back from Sundance!  Loved the documentaries",
         "[2023-06-10 (Sat)] (memory note) The user attended the Tribeca film festival in the weekend before 20 May 2023.",
         "[2023-06-15 (Thu)] assistant: You could also try the Toronto festival in September."]

GOOD = json.dumps({"events": [
    {"date": "2023-06", "when": "event", "text": "The user went to Sundance.", "sources": [2]},
    {"date": "2023-05-14", "when": "event", "text": "The user attended the Tribeca film festival.", "sources": [1, 3]},
    {"date": "2023-06-15", "when": "said", "text": "The assistant suggested the Toronto festival.", "sources": [4]},
    {"date": "2023-07-01", "when": "event", "text": "The user went to Cannes.", "sources": [9]},        # 来源不存在：模型编的
    {"date": "bad date", "when": "event", "text": "The user went to Venice.", "sources": [2]},
    {"date": "2023-05-14", "when": "event", "text": "the user attended the tribeca film festival.", "sources": [1]},  # 重复
]})


# ---------- 输入与请求体 ----------

def test_build_input_numbers_lines_and_respects_budget(monkeypatch):
    text, n = chain.build_input("How many film festivals did I attend?", LINES)
    assert n == 4
    assert text.startswith("Question: How many film festivals did I attend?\n\nExcerpts:\n#1 [2023-05-20 (Sat)] user:")
    assert "\n#4 [2023-06-15 (Thu)] assistant:" in text
    monkeypatch.setattr(config, "CHAIN_INPUT_TOKENS", 60)
    text, n = chain.build_input("How many film festivals did I attend?", LINES)
    assert 0 < n < 4 and f"#{n + 1} " not in text
    monkeypatch.setattr(config, "CHAIN_LINE_TOKENS", 8)
    monkeypatch.setattr(config, "CHAIN_INPUT_TOKENS", 14000)
    text, n = chain.build_input("q", LINES)
    assert n == 4 and text.count(" …") == 4


def test_request_body_and_sha(monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_PROVIDERS", [])
    body = chain.request_body("Question: q\n\nExcerpts:\n#1 a\n")
    assert body["temperature"] == 0 and body["seed"] == config.EXTRACT_SEED and body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == config.CHAIN_MAX_OUTPUT_TOKENS and "provider" not in body
    sha = chain.body_sha(body)
    assert sha == chain.body_sha(chain.request_body("Question: q\n\nExcerpts:\n#1 a\n"))
    assert sha != chain.body_sha(chain.request_body("Question: q\n\nExcerpts:\n#1 b\n"))
    assert sha != extract.body_sha(body)   # 和抽取共用缓存表，前缀不同，键不会撞


# ---------- 解析与渲染 ----------

def test_parse_validates_sources_dates_duplicates_and_sorts():
    events, ok = chain.parse_output(GOOD, n_sources=4)
    assert ok
    assert [(e.date, e.when, e.sources) for e in events] == [
        ("2023-05-14", "event", [1, 3]), ("2023-06", "event", [2]), ("2023-06-15", "said", [4]), ("unknown", "event", [2])]
    assert events[0].text == "The user attended the Tribeca film festival."


def test_parse_handles_fenced_truncated_and_garbage(monkeypatch):
    fenced = "```json\n" + GOOD + "\n```"
    assert len(chain.parse_output(fenced, 4)[0]) == 4
    assert chain.parse_output("not json at all", 4) == ([], False)
    assert chain.parse_output('{"events": "nope"}', 4) == ([], True)
    monkeypatch.setattr(config, "CHAIN_MAX_EVENTS", 2)
    assert len(chain.parse_output(GOOD, 4)[0]) == 2


def test_render_marks_said_dates_and_header():
    events, _ = chain.parse_output(GOOD, 4)
    text = chain.render(events)
    lines = text.split("\n")
    assert lines[0].startswith("[timeline · 4 dated entries found in the retrieved memories, oldest first;")
    assert "does not answer the question; the memories below are the source and may contain more]" in lines[0]
    assert lines[1] == "- 2023-05-14: The user attended the Tribeca film festival."
    assert lines[3] == "- said on 2023-06-15: The assistant suggested the Toronto festival."
    assert lines[4] == "- unknown: The user went to Venice."


# ---------- 调用、缓存、失败 ----------

def test_build_chain_calls_once_then_cache(monkeypatch, _offline):
    s = _Script([_resp(200, _completion(GOOD, pt=900, ct=120))])
    _use(monkeypatch, s)
    t1 = _run(chain.build_chain("How many film festivals did I attend?", LINES))
    t2 = _run(chain.build_chain("How many film festivals did I attend?", LINES))
    assert t1 == t2 and t1.startswith("[timeline · 4 dated entries")
    assert s.calls == 1 and s.bodies[0]["messages"][0]["content"] == chain.SYSTEM_PROMPT
    u = httpclient.usage.snapshot()["chain"]
    assert (u["calls"], u["ok"], u["prompt_tokens"], u["completion_tokens"], u["cache_hits"], u["skipped"]) == (1, 1, 900, 120, 1, 0)
    assert httpclient.usage.snapshot()["extract"]["calls"] == 0   # 不混进抽取的计数


def test_build_chain_returns_none_on_failures(monkeypatch, _offline):
    # 没 key
    monkeypatch.setattr(config, "EXTRACT_API_KEY", "")
    assert _run(chain.build_chain("how many", LINES)) is None
    monkeypatch.setattr(config, "EXTRACT_API_KEY", "k")
    # 重试用尽
    monkeypatch.setattr(config, "CHAIN_ATTEMPTS", 2)
    s = _Script([_resp(500), _resp(503)])
    _use(monkeypatch, s)
    assert _run(chain.build_chain("how many", LINES)) is None and s.calls == 2
    # 401：人来处理，这次不给
    _use(monkeypatch, _Script([_resp(401, text="bad key")]))
    assert _run(chain.build_chain("how many", LINES)) is None
    # 解析不了：按空结果缓存，下次不再调
    s = _Script([_resp(200, _completion("garbage"))])
    _use(monkeypatch, s)
    assert _run(chain.build_chain("how many x", LINES)) is None
    assert _run(chain.build_chain("how many x", LINES)) is None and s.calls == 1
    # 一行都没有（来源全错）
    _use(monkeypatch, _Script([_resp(200, _completion('{"events": [{"date": "2023", "text": "x", "sources": [7]}]}'))]))
    assert _run(chain.build_chain("how many y", LINES)) is None
    assert httpclient.usage.snapshot()["chain"]["skipped"] == 3


def test_build_chain_timeout_is_silent(monkeypatch, _offline):
    monkeypatch.setattr(config, "CHAIN_TIMEOUT_S", 0.01)

    async def slow(body, kind="chain", attempts=None):
        await asyncio.Event().wait()   # 永远等不到（fixture 把 asyncio.sleep 换成了立刻返回，不能用它装慢）
        return GOOD, "ok", 1, 1, extract.ReplyMeta(model="gpt-4o-mini-2024-07-18")
    monkeypatch.setattr(chain, "_call", slow)
    assert _run(chain.build_chain("how many", LINES)) is None
    assert httpclient.usage.snapshot()["chain"]["skipped"] == 1


def test_token_cap_counts_chain_too(monkeypatch, _offline):
    monkeypatch.setattr(config, "EXTRACT_TOKEN_CAP", 500)
    s = _Script([_resp(200, _completion(GOOD, pt=450, ct=100))])
    _use(monkeypatch, s)
    assert _run(chain.build_chain("how many a", LINES)) is not None
    assert _run(chain.build_chain("how many b", LINES)) is None   # 超了预算：不再调
    assert s.calls == 1


# ---------- 检索装配 ----------

def test_search_puts_chain_first_only_on_routed_questions(monkeypatch):
    from types import SimpleNamespace
    calls = []

    async def fake_chain(query, lines):
        calls.append((query, list(lines)))
        return "[timeline · 1 dated entries]\n- 2023-05-14: The user attended Tribeca."
    monkeypatch.setattr(chain, "build_chain", fake_chain)
    monkeypatch.setattr(config, "CHAIN_ENABLED", True)
    monkeypatch.setattr(config, "CHAIN_TOPN", 2)

    from tests.test_extract import _row
    rows = [_row(0, "a", day=20, text="I went to Tribeca last weekend"), _row(1, "a", day=21, text="Sundance next month"),
            _row(2, "a", day=22, text="unrelated")]
    idx = SimpleNamespace(user_id="u", rows=rows, session_label={"a": "session 1"}, speakers=frozenset(), token_cache={},
                          id_to_pos={r.id: r.pos for r in rows}, token_sets=[set() for _ in rows], rule_rows=[], forget_rows=[],
                          bm25=None, entity_groups={}, alias_to_group={}, by_date={}, by_month={}, lower_texts=[r.text.lower() for r in rows],
                          alias_words={}, alias_patterns=(), version=1)
    monkeypatch.setattr(search, "get_index", lambda user_id: idx)
    monkeypatch.setattr(search, "_bm25_channel", lambda idx, q, n: ([0, 1, 2], {0: 3.0, 1: 2.0, 2: 1.0}))
    monkeypatch.setattr(search, "_entity_channel", lambda idx, q: [])
    monkeypatch.setattr(search, "_literal_channel", lambda idx, q, s: [])
    monkeypatch.setattr(search, "_date_channel", lambda idx, q, intent: [])

    async def no_vec(idx, q, n):
        return [], None
    monkeypatch.setattr(search, "_vector_channel", no_vec)

    async def no_rerank(idx, q, order, scores, must, trace):
        return order
    monkeypatch.setattr(search, "_reranked", no_rerank)
    monkeypatch.setattr(config, "HOP_ENABLED", False)

    out = _run(search.search("u", "How many film festivals did I attend?", None, 10))
    assert out[0]["id"].startswith("chain:") and out[0]["content"].startswith("[timeline")
    assert out[0]["text"] == out[0]["content"] and "created_at" not in out[0]
    assert [it["id"] for it in out[1:]] == [r.id for r in rows]
    assert len(calls) == 1 and calls[0][0] == "How many film festivals did I attend?"
    assert len(calls[0][1]) == 2 and calls[0][1][0].endswith("user: I went to Tribeca last weekend")

    out = _run(search.search("u", "When did I go to Tribeca?", None, 10))
    assert not out[0]["id"].startswith("chain:") and len(calls) == 1
