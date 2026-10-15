"""总结/历程路由（10-15）：问法识别 + 日期轮选 + 时间排序；开关关着时 search() 不走这段。"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, search  # noqa: E402


def test_summary_query_routing():
    yes = ["Summarize what I've told you about my fitness journey.",
           "Give a summarization of my career.",
           "Can you give me a comprehensive summary of how my budget tracker project has progressed?",
           "How has my relationship with my sister evolved over the past year?",
           "Give me an overview of my job search so far",
           "What patterns do you see in my sleep?",
           "Is there a trend in my spending?",
           "Walk me through the history of my car troubles",
           "总结一下我这几个月的健身情况",
           "总结我这几年改变了多少"]
    no = ["How many weddings have I attended this year?",
          "How much have I spent on bike repairs in total over the past year?",
          "Could you tell me how many jobs I held over the past year?",
          "When did my fitness journey begin?",
          "What is my current sleep pattern?",
          "How many days ago did I start my journey?",
          "When did I adopt Max?",
          "What did I buy for my sister's birthday?",
          "What activities does Melanie partake in?",
          "What are my hobbies?",
          "我一共去了几次医院"]
    assert all(search.is_summary_query(q) for q in yes), [q for q in yes if not search.is_summary_query(q)]
    assert not any(search.is_summary_query(q) for q in no), [q for q in no if search.is_summary_query(q)]


def _row(i: int, day: int | None, kind: str = "msg"):
    ts = datetime(2024, 3, day, 12, 0, tzinfo=timezone.utc) if day else None
    return SimpleNamespace(pos=i, kind=kind, ts_value=ts, seq=i, part=1)


def test_same_timestamp_keeps_storage_order(monkeypatch):
    """Codex #13 R3：BEAM 的时间锚只到天，同一天两段会话按存储位置排，不按会话内 seq 交错。"""
    monkeypatch.setattr(config, "SUMMARY_POOL", 200)
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 0)
    ts = datetime(2024, 3, 1, tzinfo=timezone.utc)
    rows = [SimpleNamespace(pos=0, kind="msg", ts_value=ts, seq=1, part=1),   # s0#1
            SimpleNamespace(pos=1, kind="msg", ts_value=ts, seq=2, part=1),   # s0#2
            SimpleNamespace(pos=2, kind="msg", ts_value=ts, seq=1, part=1),   # s1#1
            SimpleNamespace(pos=3, kind="msg", ts_value=ts, seq=2, part=1)]   # s1#2
    out = search._summary_select(SimpleNamespace(rows=rows), [3, 1, 2, 0], k=4)
    assert out == [0, 1, 2, 3]


def test_summary_select_round_robin_then_chronological(monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_POOL", 200)
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 20)
    # 相关度顺序：0(3日) 1(3日) 2(1日) 3(3日) 4(2日) 5(1日) 6(无日期)
    rows = [_row(0, 3), _row(1, 3), _row(2, 1), _row(3, 3), _row(4, 2), _row(5, 1), _row(6, None)]
    idx = SimpleNamespace(rows=rows)
    out = search._summary_select(idx, list(range(7)), k=5)
    # 第一轮各组取最相关：0(3日) 2(1日) 4(2日) 6(无日期)；第二轮从第一组开始：1(3日) → 够 5 条
    # 选出的按时间排：1日 2 → 2日 4 → 3日 0,1 → 无日期 6；其余 3,5 按原顺序接在后面
    assert out == [2, 4, 0, 1, 6, 3, 5]


def test_summary_select_respects_pool_and_note_cap(monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_POOL", 4)
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 1)
    rows = [_row(0, 1, "note"), _row(1, 2, "note"), _row(2, 3), _row(3, 4), _row(4, 5)]
    idx = SimpleNamespace(rows=rows)
    out = search._summary_select(idx, [0, 1, 2, 3, 4], k=10)
    # 池只有前 4 条（4 不参加轮选，只能排在后面）；笔记只准进 1 条：0 进、1 被跳过；没选上的按原顺序接在后面
    assert out == [0, 2, 3, 1, 4]


def test_all_summaries_take_seats_first(monkeypatch):
    """SUMMARY_ROUTE_ALL_SUMMARIES：索引里全部 kind=summary 的行先占名额（最多 k 的一半），没被召回的也进；轮选只填剩下的。"""
    monkeypatch.setattr(config, "SUMMARY_POOL", 200)
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 10)
    monkeypatch.setattr(config, "SUMMARY_ROUTE_ALL_SUMMARIES", True)
    rows = [_row(0, 5), _row(1, 6), _row(2, 7), _row(3, 8),            # 原文，相关度顺序 0 1 2 3
            _row(4, 1, "summary"), _row(5, 9, "summary")]              # 两段摘要，5 没有被召回
    out = search._summary_select(SimpleNamespace(rows=rows), [0, 1, 2, 3, 4], k=4)
    # 摘要 4、5 占 2 席，轮选填 2 席：0(5日) 1(6日)；按时间：4(1日) 0 1 5(9日)；其余 2 3 接后面
    assert out == [4, 0, 1, 5, 2, 3]
    monkeypatch.setattr(config, "SUMMARY_ROUTE_ALL_SUMMARIES", False)
    out = search._summary_select(SimpleNamespace(rows=rows), [0, 1, 2, 3, 4], k=4)
    assert out == [0, 1, 2, 3, 4]   # 关着时摘要 4 只是普通候选：前四个日期组各取一条就满了，4 排在后面；5 没被召回根本不在顺序里


def test_route_scoped_note_cap(monkeypatch):
    """SUMMARY_NOTES_MAX_RETURNED 只管走路由的那次：_summary_select 和 _box 都按传入的 notes_cap 数笔记，不传就用 NOTES_MAX_RETURNED。"""
    monkeypatch.setattr(config, "SUMMARY_POOL", 200)
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 1)
    monkeypatch.setattr(config, "SUMMARY_ROUTE_ALL_SUMMARIES", False)
    rows = [_row(0, 1, "note"), _row(1, 2, "note"), _row(2, 3, "note"), _row(3, 4)]
    idx = SimpleNamespace(rows=rows, id_to_pos={})
    assert search._summary_select(idx, [0, 1, 2, 3], k=10) == [0, 3, 1, 2]              # 默认上限 1：只进一条笔记
    assert search._summary_select(idx, [0, 1, 2, 3], k=10, notes_cap=3) == [0, 1, 2, 3]  # 路由自己的上限 3
    monkeypatch.setattr(search, "_render", lambda idx, r: "x")
    monkeypatch.setattr(search, "_rendered_tokens", lambda idx, p: 1)
    monkeypatch.setattr(config, "BUDGET_TOKENS", 1000)
    monkeypatch.setattr(config, "NOTE_SOURCE_PACK", False)
    for r in rows:
        r.id = f"r{r.pos}"
        r.ts_value = None
        r.ts_granularity = "day"
    assert [it["id"] for it in search._box(idx, [0, 1, 2, 3], {}, 10)] == ["r0", "r3"]
    assert [it["id"] for it in search._box(idx, [0, 1, 2, 3], {}, 10, notes_cap=3)] == ["r0", "r1", "r2", "r3"]


def test_all_summaries_preselection_respects_note_cap(monkeypatch):
    """Codex #14 补充：摘要先占名额也不能超过这次的笔记上限。"""
    monkeypatch.setattr(config, "SUMMARY_POOL", 200)
    monkeypatch.setattr(config, "SUMMARY_ROUTE_ALL_SUMMARIES", True)
    rows = [_row(0, 9), _row(1, 1, "summary"), _row(2, 2, "summary"), _row(3, 3, "summary")]
    out = search._summary_select(SimpleNamespace(rows=rows), [0, 1, 2, 3], k=6, notes_cap=1)
    assert out[:2] == [1, 0]   # 只预占 1 条摘要（最早那条），再轮选原文 0；其余摘要按原顺序接后面
    monkeypatch.setattr(config, "SUMMARY_ROUTE_ALL_SUMMARIES", False)


def test_summary_select_short_pool_keeps_everything(monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_POOL", 200)
    monkeypatch.setattr(config, "NOTES_MAX_RETURNED", 0)
    rows = [_row(0, 2), _row(1, 1)]
    out = search._summary_select(SimpleNamespace(rows=rows), [0, 1], k=100)
    assert out == [1, 0]
