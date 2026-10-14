"""笔记旁边贴原话（NOTE_SOURCE_PACK）与笔记事件日期（NOTE_EVENT_DATES），10-15 抄作业 P1-4 / P1-1。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, index, search  # noqa: E402


def _row(pos, kind, req, text="", rid=None):
    return SimpleNamespace(pos=pos, kind=kind, request_id=req, text=text, id=rid or f"r{pos}", ts_value=None,
                           ts_granularity="day", seq=pos, part=1)


def test_link_note_sources_same_request_best_overlap():
    rows = [_row(0, "msg", "A"), _row(1, "msg", "A"), _row(2, "msg", "B"),
            _row(3, "note", "A"), _row(4, "note", "B"), _row(5, "note", "A"), _row(6, "summary", "A")]
    tokens = [{"caroline", "adopted", "dog", "max", "may"}, {"melanie", "hiking", "weekend"}, {"user", "bought", "car", "april"},
              {"caroline", "dog", "max", "adopted"},            # → 0（重叠 4）
              {"user", "car", "april"},                          # → 2（只在 B 里找）
              {"caroline", "zzz"},                               # 与 0 重叠 1 < 2，不连
              {"caroline", "dog", "max"}]                        # 摘要不连
    out = index.link_note_sources(rows, tokens, min_overlap=2)
    assert out == {3: 0, 4: 2}


def test_link_note_sources_tie_takes_earlier_row():
    rows = [_row(0, "msg", "A"), _row(1, "msg", "A"), _row(2, "note", "A")]
    tokens = [{"a", "b", "c"}, {"a", "b", "d"}, {"a", "b"}]
    assert index.link_note_sources(rows, tokens, min_overlap=2) == {2: 0}


def test_note_event_dates_reads_absolute_dates_from_note_text():
    rows = [_row(0, "msg", "A", "I moved on 19 May 2023"), _row(1, "note", "A", "The user moved from Boston to Denver on 19 May 2023."),
            _row(2, "note", "A", "The user plans a trip in June 2024."), _row(3, "summary", "A", "On 2023-05-20 they talked.")]
    got = index.note_event_dates(rows)
    assert (1, "day", "2023-05-19") in got
    assert not any(pos == 0 or pos == 3 for pos, _, _ in got)   # 原文和摘要不算
    days = [(p, g, d) for p, g, d in got if p == 2]
    assert days and all(g in ("day", "month") for _, g, _ in days)


def test_box_packs_source_after_note_without_duplicates(monkeypatch):
    monkeypatch.setattr(config, "NOTE_SOURCE_PACK", True)
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 20)
    monkeypatch.setattr(config, "BUDGET_TOKENS", 1000)
    monkeypatch.setattr(search, "_render", lambda idx, r: "x")
    monkeypatch.setattr(search, "_rendered_tokens", lambda idx, p: 1)
    rows = [_row(0, "msg", "A"), _row(1, "note", "A"), _row(2, "msg", "A"), _row(3, "note", "A")]
    idx = SimpleNamespace(rows=rows, note_source={1: 0, 3: 0}, id_to_pos={r.id: r.pos for r in rows})
    out = search._box(idx, [1, 2, 0, 3], {}, top_k=10)
    assert [it["id"] for it in out] == ["r1", "r0", "r2", "r3"]   # 来源 r0 紧跟笔记 r1；后面名次轮到 r0 时不再重复；r3 的来源已在场
    monkeypatch.setattr(config, "NOTE_SOURCE_PACK", False)
    out = search._box(idx, [1, 2, 0, 3], {}, top_k=10)
    assert [it["id"] for it in out] == ["r1", "r2", "r0", "r3"]


def test_box_packed_source_counts_against_top_k_not_note_cap(monkeypatch):
    monkeypatch.setattr(config, "NOTE_SOURCE_PACK", True)
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 1)
    monkeypatch.setattr(config, "BUDGET_TOKENS", 1000)
    monkeypatch.setattr(search, "_render", lambda idx, r: "x")
    monkeypatch.setattr(search, "_rendered_tokens", lambda idx, p: 1)
    rows = [_row(0, "msg", "A"), _row(1, "note", "A"), _row(2, "note", "A"), _row(3, "msg", "A")]
    idx = SimpleNamespace(rows=rows, note_source={1: 0, 2: 3}, id_to_pos={r.id: r.pos for r in rows})
    out = search._box(idx, [1, 2, 3], {}, top_k=2)
    assert [it["id"] for it in out] == ["r1", "r0"]   # top_k=2：笔记 + 它的来源就满了
    out = search._box(idx, [1, 2, 3], {}, top_k=10)
    assert [it["id"] for it in out] == ["r1", "r0", "r3"]   # 笔记上限 1：第二条笔记 r2 不进，它的来源也不跟着进；r3 按名次进
