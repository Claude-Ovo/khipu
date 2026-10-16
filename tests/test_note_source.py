"""笔记旁边贴原话（NOTE_SOURCE_PACK）与笔记事件日期（NOTE_EVENT_DATES），10-15 抄作业 P1-4 / P1-1；Codex #14 之后改成正文实词 + 覆盖比例。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, index, search  # noqa: E402
from app.textutil import content_tokens  # noqa: E402


def _row(pos, kind, req, text="", rid=None):
    return SimpleNamespace(pos=pos, kind=kind, request_id=req, text=text, id=rid or f"r{pos}", ts_value=None,
                           ts_granularity="day", seq=pos, part=1)


def _link(rows, min_overlap=2, min_coverage=0.5):
    return index.link_note_sources(rows, [content_tokens(r.text) for r in rows], min_overlap, min_coverage)


def test_link_uses_content_words_not_headers():
    """Codex #14 A1 的反例：同一天、同 request，日期头和 the/is 不能把无关原文连上。"""
    rows = [_row(0, "msg", "A", "Bananas ripen quickly."), _row(1, "msg", "A", "Volcanoes erupt violently."),
            _row(2, "note", "A", "The user adopted a golden retriever named Max."),
            _row(3, "msg", "A", "The kettle is on the stove."), _row(4, "note", "A", "The user is allergic to peanuts.")]
    assert _link(rows) == {}


def test_link_best_coverage_same_request_only():
    rows = [_row(0, "msg", "A", "I adopted a golden retriever, Max, last weekend."), _row(1, "msg", "A", "Melanie went hiking."),
            _row(2, "msg", "B", "I adopted a golden retriever named Max."),
            _row(3, "note", "A", "The user adopted a golden retriever named Max."),
            _row(4, "note", "B", "The user adopted a golden retriever named Max."),
            _row(5, "summary", "A", "The user adopted a golden retriever named Max.")]
    out = _link(rows)
    assert out == {3: 0, 4: 2}   # 各自只在自己的 request 里找；摘要不连


def test_link_requires_overlap_and_coverage():
    rows = [_row(0, "msg", "A", "Denver is expensive but the mountains are close."),
            _row(1, "note", "A", "The user moved from Boston to Denver in March 2023."),     # 实词 moved boston denver march 2023 → 与 0 只共 denver
            _row(2, "msg", "A", "I moved to Denver in March 2023, leaving Boston behind."),
            _row(3, "note", "A", "The user likes Denver.")]                                   # 实词 likes denver：与 0 共 1 个，不够
    out = _link(rows)
    assert out == {1: 2}
    # 门槛放松才连：3 与 0、2 都只共 denver 一个词（覆盖 0.5 并列），取位置靠前的 0
    assert _link(rows, min_overlap=1, min_coverage=0.2) == {1: 2, 3: 0}


def test_link_tie_prefers_higher_coverage_then_earlier_row():
    rows = [_row(0, "msg", "A", "bought a bike helmet"), _row(1, "msg", "A", "bought a bike helmet and lights"),
            _row(2, "note", "A", "The user bought a bike helmet.")]
    # 2 的实词 bought bike helmet；0 覆盖 3/3，1 也覆盖 3/3 且共有词数相同 → 取位置靠前的 0
    assert _link(rows) == {2: 0}


def test_split_parts_pick_the_part_with_more_shared_content():
    """Codex #14 A2：分片各算一行，只连一片，这是已知限制；这里只锁住行为（覆盖高的那片）。"""
    rows = [_row(0, "msg", "A", "My job search involved interviews and applications before an offer."),
            _row(1, "msg", "A", "Accepted a position at Acme."),
            _row(2, "note", "A", "The user accepted a job offer from Acme after job interviews and applications.")]
    assert _link(rows) == {2: 0}


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
    trace = {}
    out = search._box(idx, [1, 2, 0, 3], {}, top_k=10, trace=trace)
    assert [it["id"] for it in out] == ["r1", "r0", "r2", "r3"]   # 来源 r0 紧跟笔记 r1；后面名次轮到 r0 时不再重复；r3 的来源已在场
    assert trace["packed_sources"] == 1 and trace["source_budget_skipped"] == 0
    assert [(e["note"], e["status"]) for e in trace["source_pack"]] == [("r1", "packed"), ("r3", "already_present")]
    monkeypatch.setattr(config, "NOTE_SOURCE_PACK", False)
    out = search._box(idx, [1, 2, 0, 3], {}, top_k=10)
    assert [it["id"] for it in out] == ["r1", "r2", "r0", "r3"]


def test_box_source_budget_failure_is_traced_not_counted_as_skip(monkeypatch):
    """Codex #14 B2：来源放不下时笔记照留，单独计数，不混进 budget_skipped。"""
    monkeypatch.setattr(config, "NOTE_SOURCE_PACK", True)
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 20)
    monkeypatch.setattr(config, "BUDGET_TOKENS", 2)
    monkeypatch.setattr(search, "_render", lambda idx, r: "x")
    monkeypatch.setattr(search, "_rendered_tokens", lambda idx, p: {0: 3}.get(p, 1))
    rows = [_row(0, "msg", "A"), _row(1, "note", "A"), _row(2, "msg", "A")]
    idx = SimpleNamespace(rows=rows, note_source={1: 0}, id_to_pos={r.id: r.pos for r in rows})
    trace = {}
    out = search._box(idx, [1, 2], {}, top_k=10, trace=trace)
    assert [it["id"] for it in out] == ["r1", "r2"]
    assert trace["packed_sources"] == 0 and trace["source_budget_skipped"] == 1 and trace["budget_skipped"] == 0


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


def test_date_channel_day_beats_month_submatch():
    """Codex #14 E1：「20 May 2023」会同时解析出日和月；整月那条是子匹配，不扩召也不把日距离归零。"""
    rows = [SimpleNamespace(seq=0), SimpleNamespace(seq=1), SimpleNamespace(seq=2)]
    idx = SimpleNamespace(rows=rows,
                          by_date={"2023-05-17": {0}, "2023-05-20": {1}, "2023-05-01": {2}},
                          by_month={"2023-05": {0, 1, 2}})
    for q in ("What happened on 2023-05-20?", "What happened on May 20, 2023?", "What happened on 20 May 2023?"):
        assert search._date_channel(idx, q, "temporal") == [1, 0], q   # 问到的那天先，3 天内的次之，月初的不进
    assert search._date_channel(idx, "What did I do in May 2023?", "temporal") == [0, 1, 2]   # 真正问整月时照旧
    # Codex #15 B：完整日期之外单独写出的同一个月，整月条件保留（旧通道也是 [0, 1, 2]）；异月独立写出的不影响
    assert search._date_channel(idx, "What happened on 2023-05-20 and throughout May 2023?", "temporal") == [0, 1, 2]
    assert search._date_channel(idx, "On May 20, 2023, and then in June 2023?", "temporal") == [1, 0]


def test_link_skips_blank_request_id_and_empty_content():
    """Codex #15 C/E：request_id 空串或 None 不连、不抛；min_overlap=0 时空实词集合（「the user」）不除零。"""
    rows = [_row(0, "msg", "", "Bananas ripen quickly."), _row(1, "note", "", "The user adopted a golden retriever named Max."),
            _row(2, "msg", None, "Volcanoes erupt violently."), _row(3, "note", None, "The user is allergic to peanuts."),
            _row(4, "msg", "A", "the user"), _row(5, "note", "A", "the user")]
    assert _link(rows) == {}
    assert _link(rows, min_overlap=0, min_coverage=0.0) == {}


def test_box_source_blocked_by_top_k_is_traced(monkeypatch):
    """Codex #15 B：top_k 满了来源追不上，trace 记 top_k，不越界，笔记照留。"""
    monkeypatch.setattr(config, "NOTE_SOURCE_PACK", True)
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 20)
    monkeypatch.setattr(config, "BUDGET_TOKENS", 1000)
    monkeypatch.setattr(search, "_render", lambda idx, r: "x")
    monkeypatch.setattr(search, "_rendered_tokens", lambda idx, p: 1)
    rows = [_row(0, "msg", "A"), _row(1, "note", "A")]
    idx = SimpleNamespace(rows=rows, note_source={1: 0}, id_to_pos={r.id: r.pos for r in rows})
    trace = {}
    out = search._box(idx, [1, 0], {}, top_k=1, trace=trace)
    assert [it["id"] for it in out] == ["r1"]
    assert trace["packed_sources"] == 0 and trace["source_budget_skipped"] == 0
    assert [(e["note"], e["status"]) for e in trace["source_pack"]] == [("r1", "top_k")]
