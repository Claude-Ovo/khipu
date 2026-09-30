"""Offline regressions for prebuilt entity/literal matching and recall scheduling.

Run with the project Python and ``pytest -p no:cacheprovider``. No database,
embedding service, or tokenizer download is needed.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path
import random
import re
import socket
import string
import sys
import threading
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# textutil has an intentional fallback when tiktoken is absent. Use that here:
# get_encoding() can otherwise download its data on a machine with no cache.
with patch.dict(sys.modules, {"tiktoken": None}):
    from app import index as index_module
    from app.index import Row, UserIndex, build_alias_matcher
    from app.textutil import literal_terms
    search_module = importlib.import_module("app.search")


@pytest.fixture(autouse=True)
def prohibit_network_and_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("This test must remain offline")

    # Windows implements the event loop's private wake-up socketpair using a
    # loopback connect. Permit only that stdlib operation, never service sockets.
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair
    internal_pipe = threading.local()

    def guarded_connect(sock, address):
        if getattr(internal_pipe, "creating", False):
            return original_connect(sock, address)
        return forbidden()

    def socketpair(*args, **kwargs):
        internal_pipe.creating = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            internal_pipe.creating = False

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(index_module.pool, "connection", forbidden)


def _row(pos: int, text: str, timestamp: datetime | None = None) -> Row:
    return Row(
        pos=pos, id=f"row-{pos}", session_id="offline-session", seq=pos,
        part=1, total=1, role="user", speaker_name=None,
        ts_value=timestamp, ts_granularity="datetime" if timestamp else "unknown",
        text=text, is_rule=False, has_vector=False,
    )


def _index(rows: list[Row], aliases: dict[str, str], groups: dict[str, set[int]]) -> UserIndex:
    words, patterns = build_alias_matcher(aliases)
    return UserIndex(
        user_id="offline-user", rows=rows, bm25=None, entity_groups=groups,
        alias_to_group=aliases, by_date={}, by_month={}, rule_rows=[],
        session_label={"offline-session": "session 1"},
        id_to_pos={r.id: r.pos for r in rows}, version=0,
        token_sets=[set() for _ in rows], alias_words=words,
        alias_patterns=patterns, lower_texts=[r.text.lower() for r in rows],
    )


def _old_hit_groups(aliases: dict[str, str], q: str) -> set[str]:
    # Copy the old predicate literally; no old production code is imported.
    ql = q.lower()
    hit_groups: set[str] = set()
    for alias, canon in aliases.items():
        if len(alias) > 2 and re.search(rf"\b{re.escape(alias)}\b", ql):
            hit_groups.add(canon)
    return hit_groups


def _compiled_hit_groups(idx: UserIndex, q: str) -> set[str]:
    ql = q.lower()
    groups = {idx.alias_words[word] for word in re.findall(r"\w+", ql)
              if word in idx.alias_words}
    groups.update(canon for rx, canon in idx.alias_patterns if rx.search(ql))
    return groups


def _old_entity_with_pos_tie(idx: UserIndex, q: str) -> list[int]:
    rows: set[int] = set()
    for group in _old_hit_groups(idx.alias_to_group, q):
        rows |= idx.entity_groups.get(group, set())
    return sorted(rows, key=lambda p: (
        idx.rows[p].ts_value is None,
        -(idx.rows[p].ts_value.timestamp() if idx.rows[p].ts_value else 0),
        p,
    ))


def _old_literal(idx: UserIndex, q: str, bm25_scores: dict[int, float]) -> list[int]:
    terms = [t.lower() for t in literal_terms(q)]
    if not terms:
        return []
    hits = [r.pos for r in idx.rows if any(t in r.text.lower() for t in terms)]
    return sorted(hits, key=lambda p: -bm25_scores.get(p, 0.0))


@pytest.mark.parametrize("seed", [713, 7019, 20260930])
def test_three_thousand_seeded_match_and_output_cases(seed):
    rng = random.Random(seed)
    bank = [
        "smith", "jones", "alice", "bob", "r2d2", "unit42", "a_1", "___",
        "o'neil", "mary-jane", "dr. smith", "ann marie", "ann-marie",
        "ann-marie-smith", "marie-smith", "李小龙", "张三丰", "東京123",
        "éva", "ångström", "straße", "e\u0301va", "i\u0307pek", "σίσυφος",
        ".ann", "ann.", "-ann-", "[ann]", "a+b", "a(b)", "ab", "x", "李明", "",
    ]
    stamps = [None, datetime(2024, 1, 1, tzinfo=timezone.utc),
              datetime(2025, 5, 3, tzinfo=timezone.utc)]
    boundaries = [("", ""), ("(", ")"), ("", "'s"), ("", "-jones"),
                  ("x", "son"), ("_", "9"), ("李", "龙"), ("!", "?"),
                  ("\n", "\n"), ("x", "y")]
    for case in range(1000):
        raw_aliases = rng.sample(bank, rng.randint(8, 20))
        stem = "".join(rng.choices(string.ascii_letters, k=rng.randint(3, 9)))
        raw_aliases.extend([stem, stem + "7", stem + "_tag", stem + "-lee"])
        # Real indexes store lowercased aliases even when the name was mixed case.
        aliases = {a.lower(): f"group-{rng.randrange(7)}" for a in raw_aliases}
        groups = {f"group-{g}": {p for p in range(9) if rng.random() < 0.35}
                  for g in range(7)}
        fragments = []
        for alias in rng.sample(raw_aliases, rng.randint(1, min(6, len(raw_aliases)))):
            left, right = rng.choice(boundaries)
            spelling = rng.choice([alias, alias.upper(), alias.title(), alias.swapcase()])
            fragments.append(left + spelling + right)
        q = rng.choice(["", "Ask ", "Who knows "]) + " ".join(fragments)
        if case % 3 == 0:
            q += " Alice 2024 05/03"
        rows = [_row(p, " ".join(rng.choices(raw_aliases, k=5)) +
                     rng.choice([" ALICE 2024", " 05/03", " noise"]), rng.choice(stamps))
                for p in range(9)]
        idx = _index(rows, aliases, groups)
        scores = {p: rng.choice([-1.0, 0.0, 0.5, 2.0])
                  for p in range(9) if rng.random() < 0.75}
        context = f"seed={seed}, case={case}, query={q!r}, aliases={aliases!r}"
        assert _compiled_hit_groups(idx, q) == _old_hit_groups(aliases, q), context
        assert search_module._entity_channel(idx, q) == _old_entity_with_pos_tie(idx, q), context
        assert search_module._literal_channel(idx, q, scores) == _old_literal(idx, q, scores), context


@pytest.mark.parametrize(("aliases", "query", "expected"), [
    ({"ann-marie": "short", "ann-marie-smith": "long", "marie-smith": "overlap"},
     "ANN-MARIE-SMITH", {"short", "long", "overlap"}),
    ({"smith": "smith"}, "smithson xsmith _smith smith9 李smith", set()),
    ({"smith": "smith"}, "smith's smith-jones (smith)", {"smith"}),
    ({".ann": "leading", "ann.": "trailing", "-ann-": "both"},
     "x.ann ann.x y-ann-z", {"leading", "trailing", "both"}),
    ({".ann": "leading", "ann.": "trailing", "-ann-": "both"},
     ".ann ann. -ann-", set()),
    ({"éva": "accent", "a_1": "underscore", "123": "number", "李小龙": "cjk",
      "e\u0301va": "combining", "i\u0307pek": "expanding-lower", "___": "underscores",
      "ab": "too-short", "李明": "too-short", "_x": "too-short"},
     "ÉVA (A_1) 123 李小龙 E\u0301VA İpek ___ ab 李明 _x",
     {"accent", "underscore", "number", "cjk", "combining", "expanding-lower", "underscores"}),
    ({"李小龙": "cjk", "123": "number", "a_1": "underscore"},
     "王李小龙王 x123y xa_1z", set()),
])
def test_boundaries_overlaps_unicode_and_short_aliases(aliases, query, expected):
    canons = sorted(set(aliases.values()))
    rows = [_row(i, canon) for i, canon in enumerate(canons)]
    groups = {canon: {i} for i, canon in enumerate(canons)}
    idx = _index(rows, aliases, groups)
    assert _old_hit_groups(aliases, query) == expected
    assert _compiled_hit_groups(idx, query) == expected
    assert search_module._entity_channel(idx, query) == [
        i for i, canon in enumerate(canons) if canon in expected]


def test_manual_eight_rows_timestamp_pos_and_literal_score_order():
    old = datetime(2024, 1, 1, tzinfo=timezone.utc)
    new = datetime(2025, 1, 1, tzinfo=timezone.utc)
    texts = ["Alice oldest", "Alice recent", "ticket 2024", "alice undated",
             "unrelated", "ALICE old", "ticket 2024 undated", "unrelated again"]
    dates = [old, new, new, None, new, old, None, new]
    rows = [_row(i, text, dates[i]) for i, text in enumerate(texts)]
    idx = _index(rows, {"alice": "Alice"}, {"Alice": {0, 1, 2, 3, 5, 6}})
    assert search_module._entity_channel(idx, "Alice") == [1, 2, 0, 5, 3, 6]
    scores = {0: -1.0, 1: 3.0, 2: 3.0, 3: 0.0, 5: 2.0}
    assert search_module._literal_channel(idx, "Find Alice in 2024", scores) == [1, 2, 5, 3, 6, 0]
    assert search_module._literal_channel(idx, "nothing special", scores) == []
    assert search_module._entity_channel(idx, "nobody") == []


def test_empty_index_and_missing_entity_group():
    idx = _index([], {"alice": "missing"}, {})
    assert search_module._entity_channel(idx, "Alice") == []
    assert search_module._literal_channel(idx, "Alice 2024", {}) == []
    assert build_alias_matcher({}) == ({}, ())


def test_entity_retrieval_does_not_compile_patterns(monkeypatch):
    idx = _index([_row(0, "Alice"), _row(1, "Ann-Marie")],
                 {"alice": "a", "ann-marie": "b"}, {"a": {0}, "b": {1}})

    def no_compile(*args, **kwargs):
        raise AssertionError("Regex compilation during entity retrieval")

    # _compile also catches re.search/findall going through the module cache.
    with monkeypatch.context() as scoped:
        scoped.setattr(re, "compile", no_compile)
        scoped.setattr(re, "_compile", no_compile)
        result = search_module._entity_channel(idx, "ALICE Ann-Marie")
    assert result == [0, 1]


def test_literal_retrieval_uses_cached_lower_texts():
    class NoLower(str):
        def lower(self):
            raise AssertionError("Row text was lowercased during literal retrieval")

    idx = _index([_row(0, "Alice 2024"), _row(1, "Bob 2025")], {}, {})
    for row in idx.rows:
        row.text = NoLower(row.text)
    assert search_module._literal_channel(idx, "Find Alice in 2024", {}) == [0]


class _ReachedRRF(Exception):
    pass


@pytest.mark.parametrize("vector_mode", ["success", "exception", "timeout", "cancelled"])
def test_search_threads_parallel_recall_and_preserves_rrf_input(monkeypatch, caplog, vector_mode):
    idx = _index([_row(p, f"row {p}") for p in range(8)], {}, {})
    main_thread = threading.get_ident()
    worker_threads = []
    calls = []
    vector_started = threading.Event()
    cpu_finished = threading.Event()
    vector_finished = threading.Event()
    scores = {5: 2.0, 1: -0.5}
    expected_q = "Who has Alice? Alice Jones 2024"
    expected_n = 7 * search_module.config.CHANNEL_TOPN_MULT

    def check_cpu(name, received_idx, q):
        assert received_idx is idx and q == expected_q
        worker_threads.append(threading.get_ident())
        calls.append(name)

    def bm25(received_idx, q, n):
        check_cpu("bm25", received_idx, q)
        assert n == expected_n
        # This rendezvous fails if CPU recall blocks the event-loop thread or
        # if vector recall starts only after the complete CPU batch finishes.
        assert vector_started.wait(timeout=2.0), "Vector and CPU work did not overlap"
        return [5, 1], scores

    def entity(received_idx, q):
        check_cpu("entity", received_idx, q)
        return [3, 2]

    def literal(received_idx, q, received_scores):
        check_cpu("literal", received_idx, q)
        assert received_scores is scores
        return [1, 4]

    def date(received_idx, q, intent):
        check_cpu("date", received_idx, q)
        assert intent == "who"
        cpu_finished.set()
        return [6, 0]

    async def vector(received_idx, q, n):
        assert threading.get_ident() == main_thread
        assert received_idx is idx and q == expected_q and n == expected_n
        vector_started.set()
        try:
            if vector_mode == "exception":
                raise ValueError("offline vector failure")
            if vector_mode == "cancelled":
                assert await asyncio.to_thread(cpu_finished.wait, 2.0)
                raise asyncio.CancelledError()
            if vector_mode == "timeout":
                await asyncio.Event().wait()
            assert await asyncio.to_thread(cpu_finished.wait, 2.0)
            return [4, 5], [0.1, 0.2]
        finally:
            vector_finished.set()

    received = {}

    def rrf(channels, intent):
        received["channels"] = channels
        received["intent"] = intent
        raise _ReachedRRF()

    monkeypatch.setattr(search_module, "get_index", lambda user_id: idx)
    monkeypatch.setattr(search_module, "_bm25_channel", bm25)
    monkeypatch.setattr(search_module, "_entity_channel", entity)
    monkeypatch.setattr(search_module, "_literal_channel", literal)
    monkeypatch.setattr(search_module, "_date_channel", date)
    monkeypatch.setattr(search_module, "_vector_channel", vector)
    monkeypatch.setattr(search_module, "_rrf", rrf)
    monkeypatch.setattr(search_module.config, "SEARCH_TIMEOUT_S", 0.02 if vector_mode == "timeout" else 5.0)

    expected_exception = asyncio.CancelledError if vector_mode == "cancelled" else _ReachedRRF
    with caplog.at_level(logging.WARNING, logger="aml.search"):
        with pytest.raises(expected_exception):
            asyncio.run(search_module.search("offline-user", "Who has Alice?", ["Alice Jones", "2024"], 7))
    assert vector_finished.is_set()
    assert calls == ["bm25", "entity", "literal", "date"]
    assert len(set(worker_threads)) == 1
    assert worker_threads[0] != main_thread
    if vector_mode == "cancelled":
        assert received == {}
        assert "vector channel skipped" not in caplog.text
    else:
        expected_channels = {
            "bm25": [5, 1], "vector": [4, 5] if vector_mode == "success" else [],
            "entity": [3, 2], "literal": [1, 4], "date": [6, 0],
        }
        assert list(received["channels"]) == list(expected_channels)
        assert received == {"channels": expected_channels, "intent": "who"}
        assert ("vector channel skipped" in caplog.text) == (vector_mode != "success")


def test_search_external_cancellation_reaches_vector(monkeypatch):
    idx = _index([_row(0, "Alice")], {}, {})
    vector_cancelled = threading.Event()
    reached_rrf = []
    monkeypatch.setattr(search_module, "get_index", lambda user_id: idx)
    monkeypatch.setattr(search_module, "_bm25_channel", lambda *args: ([], {}))
    monkeypatch.setattr(search_module, "_entity_channel", lambda *args: [])
    monkeypatch.setattr(search_module, "_literal_channel", lambda *args: [])
    monkeypatch.setattr(search_module, "_date_channel", lambda *args: [])
    monkeypatch.setattr(search_module, "_rrf", lambda *args: reached_rrf.append(args))
    monkeypatch.setattr(search_module.config, "SEARCH_TIMEOUT_S", 5.0)

    async def scenario():
        vector_started = asyncio.Event()

        async def vector(*args):
            vector_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                vector_cancelled.set()

        monkeypatch.setattr(search_module, "_vector_channel", vector)
        task = asyncio.create_task(search_module.search("offline-user", "Alice", None, 1))
        await asyncio.wait_for(vector_started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert vector_cancelled.is_set()
    assert reached_rrf == []


def test_watchdog_mock_clock_threshold_maximum_and_cancellation(monkeypatch, caplog):
    from app import watchdog

    monkeypatch.setattr(watchdog, "_max_lag_s", 0.0)
    monkeypatch.setattr(watchdog, "_lag_events", 0)
    monkeypatch.setattr(watchdog.config, "LOOP_LAG_WARN_S", 0.5)
    now = [100.0]
    durations = iter([1.0, 1.25, 1.5, 1.75, 0.9])
    snapshots = []

    async def fake_sleep(delay):
        assert delay == 1.0
        snapshots.append(watchdog.snapshot())
        try:
            now[0] += next(durations)
        except StopIteration:
            raise asyncio.CancelledError() from None

    monkeypatch.setattr(watchdog.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(watchdog.asyncio, "sleep", fake_sleep)
    assert watchdog.snapshot() == {"max_lag_s": 0.0, "lag_events": 0}
    with caplog.at_level(logging.WARNING, logger="aml.watchdog"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(watchdog.run())
    assert snapshots[2] == {"max_lag_s": 0.25, "lag_events": 0}
    assert snapshots[3] == {"max_lag_s": 0.5, "lag_events": 0}
    assert watchdog.snapshot() == {"max_lag_s": 0.75, "lag_events": 1}
    warnings = [record for record in caplog.records if record.name == "aml.watchdog"]
    assert [record.getMessage() for record in warnings] == ["event loop lagged 0.75s"]
    copy = watchdog.snapshot()
    copy["lag_events"] = 999
    assert watchdog.snapshot()["lag_events"] == 1
