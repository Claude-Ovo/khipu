"""B3 抽取（Add 时 gpt-4o-mini）的离线测试：不连库、不联网、不花钱。
切窗、请求体、解析、缓存与并发去重、失败分类、笔记入段、邻居跳过笔记、笔记渲染、同话题新旧标记。
用法：.venv/Scripts/python.exe -m pytest tests/test_extract.py -q"""
from __future__ import annotations

import asyncio
import json
import random
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import chunking, config, extract, httpclient, search  # noqa: E402
from app.index import Row, _index_text, mark_latest  # noqa: E402
from app.main import _embed_text_of  # noqa: E402

MAY20 = int(datetime(2023, 5, 20, 14, 2, tzinfo=timezone.utc).timestamp() * 1000)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _resp(status: int, payload: dict | None = None, text: str = "") -> httpx.Response:
    req = httpx.Request("POST", "https://example.invalid/chat/completions")
    if payload is not None:
        return httpx.Response(status, json=payload, request=req)
    return httpx.Response(status, text=text, request=req)


def _completion(content: str, pt: int = 100, ct: int = 20, model: str = "gpt-4o-mini-2024-07-18") -> dict:
    return {"id": "chatcmpl-test", "system_fingerprint": "fp_test", "created": 1700000000, "model": model,
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct}}


GOOD = ('{"facts": [{"text": "Caroline adopted a dog named Max on 19 May 2023.", "subject": "Caroline", '
        '"key": "caroline.pets"}], "summary": "Caroline told Melanie about her new dog on 20 May 2023."}')


class _Script:
    def __init__(self, items, gate: asyncio.Event | None = None):
        self.items = list(items)
        self.calls = 0
        self.bodies: list[dict] = []
        self.gate = gate

    async def post(self, url, json=None, **kw):
        self.calls += 1
        self.bodies.append(json)
        if self.gate is not None:
            await self.gate.wait()
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    store: dict[str, tuple] = {}
    monkeypatch.setattr(config, "EXTRACT_API_KEY", "test-key")
    monkeypatch.setattr(config, "EXTRACT_TOKEN_CAP", 0)
    def put(sha, out, status, pt, ct, version=None, scope="", input_text=None):
        json.dumps(out, ensure_ascii=False).encode("utf-8")   # 库里是 jsonb + UTF-8：编不过去就是真实路径会 500（复审 #9-E）
        if input_text is not None:
            input_text.encode("utf-8")
        store.setdefault(sha, (out, status, scope, input_text))
    monkeypatch.setattr(extract, "_cache_get", lambda sha: store[sha][0] if sha in store else None)
    monkeypatch.setattr(extract, "_cache_put", put)
    monkeypatch.setattr(extract, "_cache_delete", lambda sha: store.pop(sha, None))
    monkeypatch.setattr(extract, "extract_sem", asyncio.Semaphore(4))
    monkeypatch.setattr(extract, "_inflight", {})
    monkeypatch.setattr(httpclient, "usage", httpclient._Usage())
    monkeypatch.setattr(extract, "usage", httpclient.usage)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(extract.asyncio, "sleep", no_sleep)
    return store


def _use(monkeypatch, script):
    monkeypatch.setattr(extract, "llm_client", lambda: script)


# ---------- 切窗与请求体 ----------

def test_windows_render_dates_speakers_and_keep_numbering():
    msgs = [{"role": "user", "content": "Caroline: I adopted a dog yesterday", "timestamp": MAY20},
            {"role": "assistant", "content": "   ", "timestamp": None},
            {"role": "assistant", "content": "Congrats!", "timestamp": None}]
    w = extract.build_windows(msgs)
    assert w == ["[1] 2023-05-20 (Sat) 14:02 Caroline: I adopted a dog yesterday\n[3] assistant: Congrats!"]
    assert extract.build_windows(msgs) == w  # 只看请求本身，永远切出同样的窗


def test_windows_split_on_message_boundaries(monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_WINDOW_TOKENS", 30)
    msgs = [{"role": "user", "content": f"message number {i} " + "word " * 8, "timestamp": MAY20} for i in range(6)]
    w = extract.build_windows(msgs)
    assert len(w) > 1
    lines = [ln for win in w for ln in win.split("\n")]
    assert [ln.split("]")[0] for ln in lines] == [f"[{i}" for i in range(1, 7)]  # 每条消息完整、按序、只出现一次


def test_long_message_is_clipped_for_extraction_only(monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_MSG_MAX_TOKENS", 10)
    line = extract.message_line(1, {"role": "assistant", "content": "lorem ipsum " * 200, "timestamp": None})
    assert line.endswith("…[truncated]") and len(line) < 120


def test_request_body_locks_snapshot(monkeypatch):
    b = extract.request_body("[1] user: hi")
    assert b["model"].endswith("gpt-4o-mini-2024-07-18")
    assert b["temperature"] == 0 and b["seed"] == config.EXTRACT_SEED
    assert b["response_format"] == {"type": "json_object"}
    monkeypatch.setattr(config, "EXTRACT_PROVIDERS", [])
    assert "provider" not in extract.request_body("[1] user: hi")   # 中转不认这个字段，不发
    monkeypatch.setattr(config, "EXTRACT_PROVIDERS", ["openai", "azure"])
    b = extract.request_body("[1] user: hi")
    assert b["provider"] == {"only": ["openai", "azure"], "allow_fallbacks": False, "require_parameters": True}
    sha = extract.body_sha(b)
    assert sha == extract.body_sha(extract.request_body("[1] user: hi"))
    assert sha != extract.body_sha(extract.request_body("[1] user: hello"))
    monkeypatch.setattr(extract, "SYSTEM_PROMPT", extract.SYSTEM_PROMPT + " ")
    assert sha != extract.body_sha(extract.request_body("[1] user: hi"))  # 改提示词，缓存键自己变


# ---------- 解析 ----------

def test_parse_normal_output():
    notes, ok = extract.parse_output(GOOD)
    assert ok
    assert notes == [extract.Note("note", "Caroline adopted a dog named Max on 19 May 2023.", "Caroline", "caroline.pets"),
                     extract.Note("summary", "Caroline told Melanie about her new dog on 20 May 2023.", None, None)]


def test_parse_cleans_subjects_keys_duplicates_and_caps(monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_MAX_FACTS", 3)
    out = ('{"facts": [{"text": "The user  works at\\n Acme.", "subject": "user", "key": " User.Job "},'
           ' "The user has two cats.", {"text": "the user works at acme."}, {"text": 42}, 7,'
           ' {"text": "The user lives in Denver.", "subject": "the user", "key": "user/city!"},'
           ' {"text": "The user runs daily."}], "summary": ""}')
    notes, ok = extract.parse_output(out)
    assert ok
    assert notes == [extract.Note("note", "The user works at Acme.", None, "user.job"),
                     extract.Note("note", "The user has two cats.", None, None),
                     extract.Note("note", "The user lives in Denver.", None, "usercity")]


def test_parse_fenced_and_truncated_output():
    notes, ok = extract.parse_output("```json\n" + GOOD + "\n```")
    assert ok and len(notes) == 2
    cut = '{"facts": [{"text": "A did x.", "subject": "Anna", "key": null}, {"text": "B did'
    notes, ok = extract.parse_output(cut)
    assert ok and notes == [extract.Note("note", "A did x.", "Anna", None)]


def test_parse_garbage():
    assert extract.parse_output("Sorry, I can't help with that.") == ([], False)
    assert extract.parse_output("") == ([], False)
    assert extract.parse_output("[1, 2]") == ([], False)


# ---------- 调用、缓存、失败分类 ----------

def test_cache_miss_then_hit(monkeypatch, _offline):
    s = _Script([_resp(200, _completion(GOOD, 120, 30))])
    _use(monkeypatch, s)
    first = _run(extract.extract_window("[1] Caroline: I adopted a dog"))
    again = _run(extract.extract_window("[1] Caroline: I adopted a dog"))
    assert first == again and len(first) == 2
    assert s.calls == 1
    (out, status, scope, _), = _offline.values()
    assert status == "ok" and out["raw"] == GOOD and out["model"] == "gpt-4o-mini-2024-07-18"
    # 主办方 10-10：仅凭模型名不足以核验身份，回复的 id / system_fingerprint / created 一起留档
    assert (out["response_id"], out["system_fingerprint"], out["created"]) == ("chatcmpl-test", "fp_test", 1700000000)
    assert scope == ""
    u = httpclient.usage.snapshot()["extract"]
    assert (u["calls"], u["ok"], u["prompt_tokens"], u["completion_tokens"], u["cache_hits"]) == (1, 1, 120, 30, 1)


def test_concurrent_identical_windows_call_once(monkeypatch):
    async def go():
        gate = asyncio.Event()
        s = _Script([_resp(200, _completion(GOOD))], gate=gate)
        _use(monkeypatch, s)
        tasks = [asyncio.ensure_future(extract.extract_window("[1] same")) for _ in range(3)]
        gate.set()
        res = await asyncio.gather(*tasks)
        late = await extract.extract_window("[1] same")   # 领头的已经注销：走缓存
        return s.calls, res + [late]
    calls, res = _run(go())
    assert calls == 1
    assert res[0] == res[1] == res[2] and len(res[0]) == 2


def test_400_and_moderation_kept_empty_and_cached(monkeypatch, _offline):
    s = _Script([_resp(400, text="bad"), _resp(403, text="flagged")])
    _use(monkeypatch, s)
    assert _run(extract.extract_window("[1] a")) == []
    assert _run(extract.extract_window("[1] a")) == []   # 第二次走缓存，不再花钱
    assert _run(extract.extract_window("[1] b")) == []
    assert s.calls == 2
    assert sorted(v[1] for v in _offline.values()) == ["empty:http400", "empty:http403"]
    assert httpclient.usage.snapshot()["extract"]["empty"] == 2


def test_parse_failure_kept_empty_and_cached(monkeypatch, _offline):
    s = _Script([_resp(200, _completion("not json at all"))])
    _use(monkeypatch, s)
    assert _run(extract.extract_window("[1] a")) == []
    assert [v[1] for v in _offline.values()] == ["empty:parse"]


@pytest.mark.parametrize("status", [401, 402, 404])
def test_operational_errors_raise_and_do_not_cache(monkeypatch, _offline, status):
    s = _Script([_resp(status, text="nope")])
    _use(monkeypatch, s)
    with pytest.raises(extract.ExtractUnavailable):
        _run(extract.extract_window("[1] a"))
    assert s.calls == 1 and not _offline


def test_retries_then_succeeds(monkeypatch):
    s = _Script([_resp(503, text="busy"), httpx.ReadTimeout("slow"), _resp(200, _completion(GOOD))])
    _use(monkeypatch, s)
    assert len(_run(extract.extract_window("[1] a"))) == 2
    assert s.calls == 3
    u = httpclient.usage.snapshot()["extract"]
    assert (u["calls"], u["ok"], u["failed"]) == (3, 1, 2)


def test_200_without_choices_is_retried(monkeypatch):
    s = _Script([_resp(200, {"error": {"message": "upstream"}}), _resp(200, _completion(GOOD))])
    _use(monkeypatch, s)
    assert len(_run(extract.extract_window("[1] a"))) == 2


def test_retries_exhausted_raise_and_do_not_cache(monkeypatch, _offline):
    s = _Script([_resp(500, text="x")] * config.EXTRACT_ATTEMPTS)
    _use(monkeypatch, s)
    with pytest.raises(extract.ExtractUnavailable):
        _run(extract.extract_window("[1] a"))
    assert not _offline


def test_token_cap_stops_before_calling(monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_TOKEN_CAP", 100)
    httpclient.usage.extract(True, 90, 20, 5)
    s = _Script([_resp(200, _completion(GOOD))])
    _use(monkeypatch, s)
    with pytest.raises(extract.ExtractUnavailable):
        _run(extract.extract_window("[1] a"))
    assert s.calls == 0


def test_extract_request_needs_key_and_dedups_across_windows(monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_API_KEY", "")
    with pytest.raises(extract.ExtractUnavailable):
        _run(extract.extract_request([{"role": "user", "content": "hi", "timestamp": None}]))
    monkeypatch.setattr(config, "EXTRACT_API_KEY", "k")
    monkeypatch.setattr(config, "EXTRACT_WINDOW_TOKENS", 5)
    s = _Script([_resp(200, _completion(GOOD)), _resp(200, _completion(GOOD))])
    _use(monkeypatch, s)
    msgs = [{"role": "user", "content": "first message here", "timestamp": MAY20},
            {"role": "user", "content": "second message here", "timestamp": MAY20}]
    notes = _run(extract.extract_request(msgs))
    assert s.calls == 2 and len(notes) == 2   # 两窗各回同样的两条，只留一份
    assert _run(extract.extract_request([{"role": "user", "content": " ", "timestamp": None}])) == []


# ---------- 入段 ----------

def test_note_segments_follow_last_message():
    segs = chunking.build_segments("u", "s1", "r1", [
        {"role": "user", "content": "Caroline: I adopted a dog yesterday", "timestamp": MAY20},
        {"role": "assistant", "content": "Congrats!", "timestamp": None}], 5, (None, "unknown"))
    notes, _ = extract.parse_output(GOOD)
    ns = chunking.build_note_segments(segs, notes)
    assert [n.id for n in ns] == ["s1#6.n1", "s1#6.n2"]
    assert {n.id for n in ns}.isdisjoint({s.id for s in segs})
    assert all(n.seq == 6 and n.part > chunking.NOTE_PART_BASE and n.total == 1 for n in ns)
    assert all(n.ts_value == segs[-1].ts_value and n.ts_provenance == "derived" for n in ns)
    assert [(n.kind, n.role, n.speaker_name, n.note_key) for n in ns] == [
        ("note", "note", "Caroline", "caroline.pets"), ("summary", "summary", None, None)]
    assert "Max" in ns[0].names and not ns[0].is_rule
    assert chunking.build_note_segments(segs, []) == [] and chunking.build_note_segments([], notes) == []


def test_embed_text_of_notes_is_plain_text():
    assert _embed_text_of("note", "Caroline", "Caroline adopted a dog.") == "Caroline adopted a dog."
    assert _embed_text_of("summary", None, "They talked.") == "They talked."
    assert _embed_text_of("user", "Caroline", "hi") == "Caroline: hi"
    assert _embed_text_of("assistant", None, "hi") == "assistant: hi"


# ---------- 索引与检索 ----------

def _row(pos, sid, kind="msg", day=20, text="t", key=None, role=None, speaker=None):
    ts = datetime(2023, 5, day, 14, 2, tzinfo=timezone.utc)
    return Row(pos, f"{sid}#{pos}", sid, pos, 1, 1, role or ("user" if kind == "msg" else kind), speaker, ts,
               "datetime", text, False, True, kind=kind, note_key=key)


def _old_with_neighbors(idx, order, k):
    """第二枪候选 ab8ffb7 的原实现，做对拍。"""
    cap = int(k * config.NEIGHBOR_CAP_RATIO)
    if cap <= 0:
        return order
    cut = min(20, len(order))
    inside = set(order[:k])
    neighbors = []
    for p in order[: config.NEIGHBOR_ANCHORS]:
        cands = [p + s * d for d in range(1, config.NEIGHBOR_RADIUS + 1) for s in (-1, 1)]
        for q in cands:
            ok = 0 <= q < len(idx.rows) and q not in inside and q not in neighbors
            if ok and idx.rows[q].session_id == idx.rows[p].session_id:
                neighbors.append(q)
                if len(neighbors) >= cap:
                    break
        if len(neighbors) >= cap:
            break
    if not neighbors:
        return order
    nset = set(neighbors)
    return order[:cut] + neighbors + [p for p in order[cut:] if p not in nset]


@pytest.mark.parametrize("radius", [1, 2])
def test_neighbors_unchanged_without_notes(monkeypatch, radius):
    monkeypatch.setattr(config, "NEIGHBOR_RADIUS", radius)
    rng = random.Random(7)
    for trial in range(200):
        sizes = [rng.randint(1, 6) for _ in range(rng.randint(1, 8))]
        rows = [_row(i, f"s{si}") for si, n in enumerate(sizes) for i in range(n)]
        for i, r in enumerate(rows):
            r.pos = i
        idx = SimpleNamespace(rows=rows)
        order = rng.sample(range(len(rows)), rng.randint(1, len(rows)))
        k = rng.choice([20, 40, 100])
        assert search._with_neighbors(idx, order, k) == _old_with_neighbors(idx, order, k), trial


def test_neighbors_skip_notes():
    rows = [_row(0, "a"), _row(1, "a"), _row(2, "a", kind="note"), _row(3, "a", kind="summary"), _row(4, "a"),
            _row(5, "b")]
    idx = SimpleNamespace(rows=rows)
    assert search._msg_step(idx, 1, 1, 1) == 4       # 跳过两条笔记
    assert search._msg_step(idx, 4, -1, 1) == 1
    assert search._msg_step(idx, 4, 1, 1) is None    # 会话边界
    assert search._msg_step(idx, 0, -1, 1) is None
    assert search._with_neighbors(idx, [1], 100) == [1, 0, 4]
    assert search._with_neighbors(idx, [2], 100) == [2]  # 笔记不当锚


def test_render_notes():
    idx = SimpleNamespace(session_label={"a": "session 1"})
    r = _row(0, "a", kind="note", text="Caroline adopted a dog named Max on 19 May 2023.", key="caroline.pets")
    assert search._render(idx, r) == "[2023-05-20 (Sat) 14:02] (memory note) Caroline adopted a dog named Max on 19 May 2023."
    r.note_tag = "[newest note on caroline.pets]"
    assert search._render(idx, r).endswith("19 May 2023. [newest note on caroline.pets]")
    s = _row(1, "a", kind="summary", text="They talked about dogs.")
    assert search._render(idx, s) == "[2023-05-20 (Sat) 14:02] (conversation summary) They talked about dogs."
    m = _row(2, "a", text="hello", speaker="Caroline")
    assert search._render(idx, m) == "[2023-05-20 (Sat) 14:02] Caroline: hello"


def test_note_rendered_under_speaker_only_when_subject_is_a_speaker():
    idx = SimpleNamespace(session_label={}, speakers=frozenset({"Caroline", "Melanie"}))
    r = _row(0, "a", kind="note", text="Caroline adopted a dog.", speaker="Caroline")
    assert search._render(idx, r) == "[2023-05-20 (Sat) 14:02] Caroline: (memory note) Caroline adopted a dog."
    r = _row(0, "a", kind="note", text="Dr. Smith sees the user weekly.", speaker="Dr. Smith")
    assert search._render(idx, r) == "[2023-05-20 (Sat) 14:02] (memory note) Dr. Smith sees the user weekly."


def test_notes_cap_in_box(monkeypatch):
    rows = [_row(i, "a", kind=("note" if i % 2 else "msg"), text=f"t{i}") for i in range(10)]
    idx = SimpleNamespace(rows=rows, session_label={}, speakers=frozenset(), token_cache={}, id_to_pos={r.id: r.pos for r in rows})
    order = list(range(10))
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 0)
    assert [it["id"] for it in search._box(idx, order, {}, 6)] == [rows[i].id for i in range(6)]
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 1)
    assert [it["id"] for it in search._box(idx, order, {}, 6)] == [rows[i].id for i in (0, 1, 2, 4, 6, 8)]


def test_index_text_of_notes_has_no_role_word():
    assert not _index_text(_row(0, "a", kind="note", text="The user works at Acme.")).split()[0] == "note"
    assert _index_text(_row(0, "a", kind="summary", text="x")).split()[0] == "2023-05-20"
    assert _index_text(_row(0, "a", kind="note", text="x", speaker="Caroline")).startswith("Caroline ")
    assert _index_text(_row(0, "a", text="x")).startswith("user ")


def test_mark_latest():
    rows = [_row(0, "a", kind="note", day=1, key="user.job"), _row(1, "a"), _row(2, "b", kind="note", day=9, key="user.job"),
            _row(3, "b", kind="note", day=9, key="user.job"), _row(4, "b", kind="note", day=9, key="user.city"),
            _row(5, "b", kind="note", day=3), _row(6, "c", kind="note", day=5, key="user.job")]
    mark_latest(rows)
    assert rows[0].note_tag == "[older note on user.job; a newer one is dated 2023-05-05]"
    assert rows[6].note_tag == "[older note on user.job; a newer one is dated 2023-05-09]"
    assert rows[2].note_tag == rows[3].note_tag == "[newest note on user.job]"
    assert rows[4].note_tag == "" and rows[5].note_tag == "" and rows[1].note_tag == ""


def test_relay_model_mismatch_is_rejected_and_retried(monkeypatch, caplog, _offline):
    # 审查 #8-1：中转换了模型的回复不收、不缓存；下一次尝试给对的就用对的
    s = _Script([_resp(200, _completion(GOOD, model="qwen-turbo")), _resp(200, _completion(GOOD))])
    _use(monkeypatch, s)
    assert len(_run(extract.extract_window("[1] a"))) == 2
    assert "relay answered with model 'qwen-turbo'" in caplog.text
    assert httpclient.usage.extract_failed == 1 and httpclient.usage.extract_ok == 1
    assert httpclient.usage.extract_tokens() == 2 * (100 + 20)   # 错模型那次的 token 也花了，要记（且只记一次）
    assert all(v[0]["model"] == "gpt-4o-mini-2024-07-18" for v in _offline.values())


def test_model_allowed_is_an_allowlist_not_a_substring():
    ok = ["gpt-4o-mini-2024-07-18", "gpt-4o-mini", "openai/gpt-4o-mini-2024-07-18", "openai/gpt-4o-mini", " GPT-4o-mini "]
    bad = ["gpt-4o-mini-fake", "gpt-4o-mini-2024-07-18-distill", "gpt-4o", "qwen-turbo", None, 7, ""]
    assert all(extract.model_allowed(m) for m in ok)
    assert not any(extract.model_allowed(m) for m in bad)


def test_cached_output_from_wrong_model_is_discarded_and_redone(monkeypatch, _offline, caplog):
    sha = extract.body_sha(extract.request_body("[1] stale"))
    _offline[sha] = ({"items": [{"kind": "note", "text": "stale", "subject": None, "key": None}], "raw": "x", "model": "deepseek-chat"}, "ok")
    s = _Script([_resp(200, _completion(GOOD))])
    _use(monkeypatch, s)
    notes = _run(extract.extract_window("[1] stale"))
    assert [n.text for n in notes][0].startswith("Caroline") and s.calls == 1
    assert _offline[sha][0]["model"] == "gpt-4o-mini-2024-07-18" and "discarding" in caplog.text
    assert httpclient.usage.extract_cache_hits == 0


def test_raw_with_lone_surrogate_survives_cache_serialization(monkeypatch, _offline):
    # 中转站返回的 JSON 文本里带 \ud800 转义：外层 r.json() 解出来的 content 里是转义序列，parse_output 再解一层才变成孤立代理项
    bad_raw = '{"facts": [{"text": "ok"}], "summary": "s\\ud800"}'
    body = json.dumps(_completion(bad_raw), ensure_ascii=True)
    _use(monkeypatch, _Script([_resp(200, text=body)]))
    assert len(_run(extract.extract_window("[1] sur"))) == 2   # put 桩会对整个 payload 做 UTF-8 编码，编不过就抛
    (out, status, _, _), = _offline.values()
    assert status == "ok" and "\ud800" not in out["raw"]


def test_relay_model_mismatch_every_time_is_unavailable(monkeypatch, _offline):
    monkeypatch.setattr(config, "EXTRACT_ATTEMPTS", 2)
    s = _Script([_resp(200, _completion(GOOD, model="qwen-turbo")), _resp(200, _completion(GOOD, model="deepseek-chat"))])
    _use(monkeypatch, s)
    with pytest.raises(extract.ExtractUnavailable):
        _run(extract.extract_window("[1] b"))
    assert not _offline  # 什么都没进缓存


def test_relay_reply_without_model_field_is_kept(monkeypatch):
    payload = _completion(GOOD)
    del payload["model"]
    _use(monkeypatch, _Script([_resp(200, payload)]))
    assert len(_run(extract.extract_window("[1] c"))) == 2


def test_parse_output_cleans_lone_surrogates_and_nul():
    # 审查 #8-2：模型吐出孤立代理项，以前缓存序列化时 UTF-8 编码失败 → Add 500
    raw = '{"facts": [{"text": "name\\ud800here\\u0000!", "subject": "Ca\\udfffrol", "key": "k\\u0000"}], "summary": "s\\ud800"}'
    notes, ok = extract.parse_output(raw)
    assert ok and len(notes) == 2
    blob = json.dumps([asdict(n) for n in notes], ensure_ascii=False)
    blob.encode("utf-8")  # 不再抛
    assert "\x00" not in blob and notes[0].text.startswith("name") and notes[0].text.endswith("here!")
    assert extract.clean_text("plain ascii 中文") == "plain ascii 中文"


def test_request_deadline_turns_into_unavailable(monkeypatch):
    monkeypatch.setattr(config, "EXTRACT_REQUEST_TIMEOUT_S", 0.05)
    gate = asyncio.Event()  # 永远不开：模拟中转站挂着不回
    s = _Script([_resp(200, _completion(GOOD))], gate=gate)
    _use(monkeypatch, s)
    with pytest.raises(extract.ExtractUnavailable, match="deadline"):
        _run(extract.extract_request([{"role": "user", "content": "I adopted a dog named Max."}]))
    assert not extract._inflight  # 被取消的领头窗注销了


def test_waiter_cancelled_together_with_leader_does_not_restart(monkeypatch):
    # 复审 #9-A：两个同窗请求同时到期，等待者看到领头被取消就「自己来」，第二次抽取让 deadline 失效
    monkeypatch.setattr(config, "EXTRACT_REQUEST_TIMEOUT_S", 0.05)
    gate = asyncio.Event()
    s = _Script([_resp(200, _completion(GOOD)), _resp(200, _completion(GOOD))], gate=gate)
    _use(monkeypatch, s)
    msgs = [{"role": "user", "content": "I adopted a dog named Max."}]

    async def both():
        r = await asyncio.gather(extract.extract_request(msgs), extract.extract_request(msgs), return_exceptions=True)
        await asyncio.sleep(0.15)   # 给「自己来」的那一路机会去发第二次请求（修好后它不该发）
        return r
    t0 = asyncio.new_event_loop().time()
    res = _run(both())
    assert all(isinstance(x, extract.ExtractUnavailable) for x in res), res
    assert s.calls == 1, f"second extraction was started by a cancelled waiter ({s.calls} calls)"
    assert not extract._inflight


def test_notes_cap_counts_only_returned_notes(monkeypatch):
    # 审查 #8-4：预算放不下而跳过的笔记以前也占配额
    long = "word " * 1500
    rows = [_row(0, "a", text="t0"), _row(1, "a", kind="note", text=long), _row(2, "a", kind="note", text=long),
            _row(3, "a", kind="note", text="short note"), _row(4, "a", text="t4")]
    idx = SimpleNamespace(rows=rows, session_label={}, speakers=frozenset(), token_cache={}, id_to_pos={r.id: r.pos for r in rows})
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 1)
    monkeypatch.setattr(config, "BUDGET_TOKENS", 300)
    assert [it["id"] for it in search._box(idx, [0, 1, 2, 3, 4], {}, 3)] == [rows[0].id, rows[3].id, rows[4].id]
    # 带 lead（时间线清单当第一条）：lead 占 top_k 和 token，不占笔记名额；短笔记仍然进得来
    lead = [{"id": "timeline:x", "content": "[timeline]\n- 2023-05-01: a", "text": "[timeline]\n- 2023-05-01: a", "score": 1.0}]
    out = search._box(idx, [0, 1, 2, 3, 4], {}, 4, None, lead)
    assert [it["id"] for it in out] == ["timeline:x", rows[0].id, rows[3].id, rows[4].id]


# ---------- 时间线清单 ----------

def test_ledger_router():
    yes = ["How many musical instruments do I currently own?", "What activities does Melanie partake in?",
           "How many days passed between my visit to MoMA and the exhibit?", "Can you summarize my progress over the past months?",
           "How often do I go running?", "我一共去了几次健身房", "What kinds of books does Joanna like?"]
    no = ["When did Caroline have a picnic?", "What was my personal best time?", "What is Evan's favorite food?",
          "Is somebody manyfold?", "Which bus did I take?"]
    assert all(search.is_ledger_query(q) for q in yes)
    assert not any(search.is_ledger_query(q) for q in no)


def _unit(*xs):
    import numpy as np
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_build_ledger_merges_repeats_and_sorts_by_date():
    rows = [_row(0, "a", kind="note", day=27, text="The user owns a Korg B1 digital piano."),
            _row(1, "a", kind="note", day=5, text="The user has a Korg B1 piano."),
            _row(2, "a", kind="note", day=12, text="The user has a Pearl Export drum set."),
            _row(3, "a", text="raw message"),
            _row(4, "a", kind="summary", day=1, text="They talked."),
            _row(5, "a", kind="note", day=20, text="The user is considering selling the drum set.")]
    idx = SimpleNamespace(rows=rows)
    vec = {rows[0].id: _unit(1, 0, 0), rows[1].id: _unit(0.99, 0.1, 0), rows[2].id: _unit(0, 1, 0), rows[5].id: _unit(0, 0.6, 0.8)}
    text = search.build_ledger(idx, [0, 3, 2, 5, 1, 4], vec)
    lines = text.split("\n")
    assert lines[0].startswith("[timeline · 3 memory notes")
    assert lines[1:] == ["- 2023-05-05 [also mentioned 2023-05-27]: The user owns a Korg B1 digital piano.",
                         "- 2023-05-12: The user has a Pearl Export drum set.",
                         "- 2023-05-20: The user is considering selling the drum set."]
    assert search.build_ledger(idx, [3, 4, 0], vec) is None   # 不到两条笔记不出清单


def test_build_span_uses_raw_messages_only():
    rows = [_row(0, "a", day=3), _row(1, "a", day=9), _row(2, "b", day=5), _row(3, "b", kind="note", day=30)]
    rows[0].ts_value = None
    text = search.build_span(SimpleNamespace(rows=rows))
    assert text == ("[memory span] The conversations on record run from 2023-05-05 (Fri) to 2023-05-09 (Tue); "
                    "the most recent conversation is on 2023-05-09 (Tue). (2 conversation threads, 2 dated messages)")
    assert search.build_span(SimpleNamespace(rows=[_row(0, "a", kind="note")])) is None


def _hist_idx(rows):
    from app.textutil import tokenize
    return SimpleNamespace(rows=rows, session_label={"a": "session 1", "b": "session 2"}, speakers=frozenset(), token_cache={},
                           id_to_pos={r.id: r.pos for r in rows}, token_sets=[set(tokenize(r.text)) for r in rows])


def test_history_blocks_chronological_and_capped():
    rows = [_row(0, "b", day=9, text="later session line one"), _row(1, "b", day=9, text="later session line two"),
            _row(2, "a", day=3, text="early session about cats"), _row(3, "a", day=3, text="early session about dogs"),
            _row(4, "a", kind="note", day=3, text="a note that must not appear")]
    idx = _hist_idx(rows)
    out = search.build_history_blocks(idx, slots=10, budget=10000, q_tokens={"cats"}, block_tokens=1000)
    assert [it["id"] for it in out] == ["a#history1", "b#history2"]          # 按会话时间排，不按存储顺序
    assert out[0]["content"].startswith("[history · session 1 · 2023-05-03]\n")
    assert "about cats" in out[0]["content"] and "about dogs" in out[0]["content"]
    assert "note that must not" not in out[0]["content"] + out[1]["content"]
    assert out[0]["created_at"] == "2023-05-03T14:02:00Z"
    # 只剩一个名额：留与问题词重叠多的那块
    out = search.build_history_blocks(idx, slots=1, budget=10000, q_tokens={"cats"}, block_tokens=1)
    assert [it["id"] for it in out] == ["a#history1"]
    # 预算不够：同样丢不相关的
    out = search.build_history_blocks(idx, slots=10, budget=40, q_tokens={"later"}, block_tokens=1)
    assert [it["id"] for it in out] == ["b#history1"]
    assert search.build_history_blocks(idx, 0, 10000, set(), 1000) == []


def test_history_blocks_split_long_sessions_and_scale_block_size():
    rows = [_row(i, "a", day=1, text=f"line {i} " + "word " * 40) for i in range(12)]
    idx = _hist_idx(rows)
    small = search.build_history_blocks(idx, slots=100, budget=100000, q_tokens=set(), block_tokens=60)
    assert len(small) > 1 and all(it["id"].startswith("a#history") for it in small)
    assert "".join(it["content"] for it in small).count("line ") == 12
    few = search.build_history_blocks(idx, slots=2, budget=100000, q_tokens=set(), block_tokens=60)
    assert len(few) <= 2 and "".join(it["content"] for it in few).count("line ") == 12   # 名额少就放大块，不丢内容


def test_looks_like_forget():
    from app.textutil import looks_like_forget as f
    yes = ["Please forget what I told you about my salary.", "Don't mention my ex again.", "Actually, scratch that, I never went.",
           "Delete the note about my address.", "That's no longer true, I moved.", "把我说的工资那件事忘了吧", "以后别再提我前男友"]
    no = ["I forgot my umbrella at the office today.", "Can you remind me what I said about the trip?", "I never liked spinach.",
          "Delete is a key on the keyboard.", "I'm worried that I might forget some of my tasks.", "Sometimes we forget that conflict helps.",
          "I think I'll make it from scratch.", "I'll remove the apps from my phone.", "Please don't use title case."]
    assert all(f(t, "user") for t in yes)
    assert not any(f(t, "user") for t in no)
    assert not f(yes[0], "assistant") and not f(yes[0] * 20, "user")


def test_insert_forgets_promotes_matching_directive_only():
    rows = [_row(0, "a", day=1, text="My salary is 90k."), _row(1, "a", day=2, text="Forget what I told you about my salary."),
            _row(2, "a", day=3, text="Please don't mention my cousin Tom."), _row(3, "a", day=4, text="unrelated")]
    idx = _hist_idx(rows)
    idx.forget_rows = [1, 2]
    from app.textutil import tokenize
    assert search._insert_forgets(idx, [0, 3], set(tokenize("What is my salary?"))) == [1, 0, 3]
    assert search._insert_forgets(idx, [0, 3], set(tokenize("Where do I live?"))) == [0, 3]
    assert search._insert_forgets(idx, [2, 0], set(tokenize("Tell me about Tom and my salary"))) == [2, 1, 0]   # 新的在前，不重复


def test_box_puts_lead_first_and_counts_it(monkeypatch):
    rows = [_row(i, "a", text=f"t{i}") for i in range(5)]
    idx = SimpleNamespace(rows=rows, session_label={}, speakers=frozenset(), token_cache={}, id_to_pos={r.id: r.pos for r in rows})
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 0)
    lead = [{"id": "timeline:x", "content": "[timeline]\n- 2023-05-01: a", "text": "[timeline]\n- 2023-05-01: a", "score": 1.0}]
    out = search._box(idx, [0, 1, 2, 3, 4], {}, 3, None, lead)
    assert [it["id"] for it in out] == ["timeline:x", rows[0].id, rows[1].id]


# ---------- 主办方 10-10 回信：缓存按用户隔离 ----------

def test_cache_is_scoped_by_user(monkeypatch, _offline):
    # 两个用户发来一字不差的原文：各抽各的（两次调用、两条缓存、各记各的 user_id），同一用户重复才命中
    s = _Script([_resp(200, _completion(GOOD)), _resp(200, _completion(GOOD))])
    _use(monkeypatch, s)
    assert len(_run(extract.extract_window("[1] Caroline: I adopted a dog", scope="user-a"))) == 2
    assert len(_run(extract.extract_window("[1] Caroline: I adopted a dog", scope="user-b"))) == 2
    assert len(_run(extract.extract_window("[1] Caroline: I adopted a dog", scope="user-a"))) == 2
    assert s.calls == 2
    assert sorted(v[2] for v in _offline.values()) == ["user-a", "user-b"]
    assert httpclient.usage.snapshot()["extract"]["cache_hits"] == 1
    b = extract.request_body("[1] x")
    assert extract.body_sha(b, "user-a") != extract.body_sha(b, "user-b") != extract.body_sha(b)


def test_extract_request_passes_user_scope(monkeypatch, _offline):
    s = _Script([_resp(200, _completion(GOOD))])
    _use(monkeypatch, s)
    msgs = [{"role": "user", "content": "I adopted a dog named Max.", "timestamp": None}]
    assert len(_run(extract.extract_request(msgs, scope="user-z"))) == 2
    (out, status, scope, _), = _offline.values()
    assert scope == "user-z" and status == "ok"


def test_cache_row_keeps_input_window_and_cleans_metadata(monkeypatch, _offline):
    # 复审 #10-E1：缓存行要带送给模型的原文窗，Add 后面失败时也能对回来源；#10-C：元数据里的坏字符不能把 Add 打成 500
    payload = _completion(GOOD)
    payload["id"] = "id-" + chr(0xD800) + "x" + chr(0) + "y"   # 孤立代理项 + NUL
    payload["system_fingerprint"] = 12345          # 非字符串：丢掉
    payload["created"] = True                       # bool 不算 int
    body = json.dumps(payload, ensure_ascii=True)   # 中转的 JSON 文本里是转义序列，r.json() 解出来才是孤立代理项
    _use(monkeypatch, _Script([_resp(200, text=body)]))
    assert len(_run(extract.extract_window("[1] 2023-05-20 user: I adopted a dog", scope="u1"))) == 2
    (out, status, scope, window), = _offline.values()
    assert window == "[1] 2023-05-20 user: I adopted a dog" and scope == "u1"
    assert out["response_id"] == "id-?xy"   # clean_text 把孤立代理项换成 ?、去掉 NUL
    assert out["system_fingerprint"] is None and out["created"] is None


def test_prompt_keeps_uncertainty_and_no_invented_dates():
    # 主办方 10-10：定不下来的日期和事件状态要保留不确定性。这里只能核提示词写了这条要求，模型照不照做要看线上样本
    p = extract.SYSTEM_PROMPT
    assert "do not invent one" in p and "never write a planned or possible event as done" in p
    assert extract.PROMPT_VERSION == "x2"
