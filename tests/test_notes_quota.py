"""审查 #12-2：重排窗口里笔记的席位上限（NOTES_MAX_CANDIDATES）。关着时窗口 = 前 RERANK_TOPN 条，逐字不变。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, search  # noqa: E402


def _idx(kinds: str):
    return SimpleNamespace(rows=[SimpleNamespace(kind="msg" if c == "m" else "note") for c in kinds])


def test_quota_off_keeps_window_verbatim(monkeypatch):
    monkeypatch.setattr(config, "RERANK_TOPN", 3)
    monkeypatch.setattr(config, "NOTES_MAX_CANDIDATES", 0)
    head, order = search._rerank_head(_idx("nnmnmm"), [0, 1, 2, 3, 4, 5])
    assert head == [0, 1, 2] and order == [0, 1, 2, 3, 4, 5]


def test_quota_gives_note_seats_to_raw(monkeypatch):
    monkeypatch.setattr(config, "RERANK_TOPN", 4)
    monkeypatch.setattr(config, "NOTES_MAX_CANDIDATES", 1)
    head, order = search._rerank_head(_idx("nnmnmmm"), [0, 1, 2, 3, 4, 5, 6])
    assert head == [0, 2, 4, 5]                  # 第一条笔记占掉唯一席位，后面的笔记让给原文
    assert order == [0, 2, 4, 5, 1, 3, 6]        # 让出去的笔记排在窗口后，相对顺序不变；窗口外原文接着排


def test_quota_larger_than_notes_changes_nothing(monkeypatch):
    monkeypatch.setattr(config, "RERANK_TOPN", 4)
    monkeypatch.setattr(config, "NOTES_MAX_CANDIDATES", 10)
    head, order = search._rerank_head(_idx("nmnm"), [0, 1, 2, 3])
    assert head == [0, 1, 2, 3] and order == [0, 1, 2, 3]


def test_must_survives_window_trim_with_quota(monkeypatch):
    """Codex #12-1：窗口 4、配额 1，候选 0/1 笔记、2–5 原文，must=[4,5]：4 已在窗口里，不能被腾位置时裁掉。"""
    import asyncio
    monkeypatch.setattr(config, "RERANK_TOPN", 4)
    monkeypatch.setattr(config, "NOTES_MAX_CANDIDATES", 1)
    monkeypatch.setattr(config, "RERANK_ENABLED", False)
    idx = _idx("nnmmmm")
    for r in idx.rows:
        r.id = str(id(r))
    seen = {}
    async def fake_rerank(q, docs):
        return None
    monkeypatch.setattr(search, "rerank", fake_rerank)
    monkeypatch.setattr(search, "_render", lambda idx, r: "x")
    trace = {}
    out = asyncio.run(search._reranked(idx, "q", [0, 1, 2, 3, 4, 5], {}, must=[4, 5], trace=trace))
    head = trace["rerank"]["head"]
    assert idx.rows[4].id in head and idx.rows[5].id in head
    assert len(head) == 4 and sorted(out) == [0, 1, 2, 3, 4, 5]
