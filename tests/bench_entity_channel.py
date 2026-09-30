"""Offline, deterministic entity/literal microbenchmark; run this file directly.

Builds no database-backed index. The 20 legacy samples span the full 200-query
set and contain ten alias hits and ten misses. p95 uses the nearest-rank rule.
"""
from __future__ import annotations

import math
import random
import re
import socket
import statistics
import string
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Callable
from unittest.mock import patch

if TYPE_CHECKING:
    from app.index import UserIndex


SEED = 20260930
ROW_COUNT = 10_000
ALIAS_COUNT = 20_000
QUERY_COUNT = 200
NONWORD_COUNT = 200
SHORT_COUNT = 200
WORD_RX = re.compile(r"\w+\Z")


def _deny_network(*args: object, **kwargs: object) -> None:
    raise AssertionError("This benchmark must not use the network or database")


def _offline_guard() -> ExitStack:
    """Protect imports as well as execution, including libpq's C connections."""
    stack = ExitStack()
    # textutil's existing fallback avoids tiktoken's possible first-run download.
    stack.enter_context(patch.dict(sys.modules, {"tiktoken": None}))
    stack.enter_context(patch.object(socket.socket, "connect", _deny_network))
    stack.enter_context(patch.object(socket.socket, "connect_ex", _deny_network))
    stack.enter_context(patch.object(socket, "create_connection", _deny_network))
    stack.enter_context(patch.object(socket, "getaddrinfo", _deny_network))
    import psycopg

    stack.enter_context(patch.object(psycopg.Connection, "connect", _deny_network))
    stack.enter_context(patch.object(psycopg.AsyncConnection, "connect", _deny_network))
    return stack


def _encoded(number: int, alphabet: str, width: int = 4) -> str:
    chars = []
    for _ in range(width):
        number, remainder = divmod(number, len(alphabet))
        chars.append(alphabet[remainder])
    assert number == 0
    return "".join(reversed(chars))


def _make_fixture() -> tuple[UserIndex, list[str], dict[int, float], list[bool]]:
    from app.index import Row, UserIndex, build_alias_matcher

    rng = random.Random(SEED)
    # Raw spellings deliberately mix case; real alias_to_group keys are lowercased.
    sources: list[str] = []
    categories: list[list[str]] = []
    cjk = "张王李赵陈刘杨黄周吴林何高郑文明"
    per_category = (ALIAS_COUNT - NONWORD_COUNT - SHORT_COUNT) // 4
    for category in range(4):
        entries = []
        for i in range(per_category):
            letters = _encoded(i, string.ascii_lowercase)
            alias = (
                "PeRsOn" + letters,
                f"PeRsOn{i:05d}",
                "PeRsOn_" + letters,
                "人" + _encoded(i, cjk),
            )[category]
            entries.append(alias)
        categories.append(entries)
        sources.extend(entries)
    punctuation = [
        "PeRsOn" + "'-. "[i % 4] + _encoded(i, string.ascii_lowercase)
        for i in range(NONWORD_COUNT)
    ]
    categories.append(punctuation)
    sources.extend(punctuation)
    short_alphabet = string.ascii_lowercase + string.digits + "_"
    sources.extend(_encoded(i, short_alphabet, width=2) for i in range(SHORT_COUNT))
    aliases = {alias.lower(): f"entity-{i // 2}" for i, alias in enumerate(sources)}
    assert len(aliases) == ALIAS_COUNT
    assert sum(not WORD_RX.fullmatch(a) for a in aliases) == NONWORD_COUNT
    assert sum(len(a) <= 2 for a in aliases) == SHORT_COUNT
    assert all(a == a.lower() for a in aliases)
    assert any(a != a.lower() for a in sources)

    rows = []
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Include Unicode and uppercase in the random corpus to exercise .lower().
    alphabet = string.ascii_letters + string.digits + "         .,!?_-'" + cjk
    for pos in range(ROW_COUNT):
        length = rng.randint(300, 2_000)
        raw_name = sources[pos * 2]
        prefix = f"Note {raw_name} record {9000000000 + pos}. "
        text = prefix + "".join(rng.choices(alphabet, k=length - len(prefix)))
        rows.append(Row(
            pos=pos, id=f"row-{pos}", session_id=f"session-{pos // 100}",
            seq=pos % 100, part=0, total=1, role="user", speaker_name=raw_name,
            ts_value=None if pos % 7 == 0 else base_time + timedelta(days=pos % 365),
            ts_granularity="unknown" if pos % 7 == 0 else "day",
            text=text, is_rule=False, has_vector=False, names=[raw_name],
        ))
    groups = {
        f"entity-{pos}": {pos, (pos + 137) % ROW_COUNT}
        for pos in range(ROW_COUNT)
    }
    alias_words, alias_patterns = build_alias_matcher(aliases)
    idx = UserIndex(
        user_id="offline-benchmark", rows=rows, bm25=None,
        entity_groups=groups, alias_to_group=aliases,
        alias_words=alias_words, alias_patterns=alias_patterns,
        lower_texts=[row.text.lower() for row in rows],
        by_date={}, by_month={}, rule_rows=[], session_label={},
        id_to_pos={row.id: row.pos for row in rows}, version=0,
        token_sets=[set() for _ in rows],
    )
    queries = []
    expected_hits = []
    for i in range(QUERY_COUNT // 2):
        raw_alias = rng.choice(categories[i % len(categories)])
        # Numeric literals make both hit and miss queries scan the full corpus.
        record = 9000000000 + rng.randrange(ROW_COUNT)
        queries.extend([
            f"look up ({raw_alias}) and record {record}",
            f"look up an absent subject and record {record}",
        ])
        expected_hits.extend([True, False])
    assert len(rows) == ROW_COUNT and all(300 <= len(r.text) <= 2_000 for r in rows)
    assert len(queries) == QUERY_COUNT and sum(expected_hits) == QUERY_COUNT // 2
    scores = {pos: float((pos * 17) % 23) for pos in range(ROW_COUNT) if pos % 3}
    return idx, queries, scores, expected_hits


def _old_entity_channel(idx: UserIndex, q: str) -> list[int]:
    """Original implementation, with only the permitted position tie-breaker."""
    ql = q.lower()
    hit_groups: set[str] = set()
    for alias, canon in idx.alias_to_group.items():
        if len(alias) > 2 and re.search(rf"\b{re.escape(alias)}\b", ql):
            hit_groups.add(canon)
    rows: set[int] = set()
    for group in hit_groups:
        rows |= idx.entity_groups.get(group, set())
    return sorted(rows, key=lambda p: (
        idx.rows[p].ts_value is None,
        -(idx.rows[p].ts_value.timestamp() if idx.rows[p].ts_value else 0),
        p,
    ))


def _time_call(function: Callable[[], list[int]]) -> tuple[float, list[int]]:
    started = perf_counter()
    result = function()
    return (perf_counter() - started) * 1000, result


def _report(label: str, samples: list[float]) -> None:
    p95 = sorted(samples)[math.ceil(0.95 * len(samples)) - 1]
    print(f"{label:26s} n={len(samples):3d} "
          f"mean_ms={statistics.fmean(samples):9.3f} p95_ms={p95:9.3f} "
          f"max_ms={max(samples):9.3f}", flush=True)


def _run() -> None:
    from app.search import _entity_channel, _literal_channel
    from app.textutil import literal_terms

    def old_literal_channel(idx: UserIndex, q: str, bm25_scores: dict[int, float]) -> list[int]:
        # Original implementation, kept inline rather than importing old code.
        terms = [t.lower() for t in literal_terms(q)]
        if not terms:
            return []
        hits = [r.pos for r in idx.rows if any(t in r.text.lower() for t in terms)]
        return sorted(hits, key=lambda p: -bm25_scores.get(p, 0.0))

    idx, queries, scores, expected_hits = _make_fixture()
    print(f"seed={SEED} rows={len(idx.rows)} aliases={len(idx.alias_to_group)} "
          f"nonword_aliases={NONWORD_COUNT} short_aliases={SHORT_COUNT} "
          f"text_chars={sum(len(r.text) for r in idx.rows)} "
          f"query_alias_hits={sum(expected_hits)} query_alias_misses={expected_hits.count(False)}",
          flush=True)
    print("p95=nearest-rank (sorted samples[ceil(0.95*n)-1]); fixture/imports excluded", flush=True)

    # Exercise initial interpreter/cache setup outside timed samples.
    _entity_channel(idx, queries[0])
    _literal_channel(idx, queries[0], scores)
    entity_times, literal_times = [], []
    all_results = []
    for query, expected in zip(queries, expected_hits):
        elapsed, entity_result = _time_call(lambda: _entity_channel(idx, query))
        entity_times.append(elapsed)
        elapsed, literal_result = _time_call(lambda: _literal_channel(idx, query, scores))
        literal_times.append(elapsed)
        assert bool(entity_result) == expected, "Query hit/miss composition differs from fixture"
        all_results.append((entity_result, literal_result))
    _report("new entity (all queries)", entity_times)
    _report("new literal (all queries)", literal_times)

    # Ten evenly spread pairs include every alias category twice and ten misses.
    sample_ids = [q for pair in range(10) for q in (2 * pair * 11, 2 * pair * 11 + 1)]
    assert len(sample_ids) == 20 and sum(expected_hits[q] for q in sample_ids) == 10
    old_entity_times, old_literal_times = [], []
    paired_entity_times, paired_literal_times = [], []
    for sample_number, query_id in enumerate(sample_ids):
        query = queries[query_id]
        # Alternate order within pairs to reduce the effect of execution order.
        functions = [
            ("old_entity", lambda: _old_entity_channel(idx, query)),
            ("new_entity", lambda: _entity_channel(idx, query)),
            ("old_literal", lambda: old_literal_channel(idx, query, scores)),
            ("new_literal", lambda: _literal_channel(idx, query, scores)),
        ]
        if sample_number % 2:
            functions.reverse()
        timings, results = {}, {}
        for name, function in functions:
            timings[name], results[name] = _time_call(function)
        assert results["old_entity"] == results["new_entity"] == all_results[query_id][0]
        assert results["old_literal"] == results["new_literal"] == all_results[query_id][1]
        old_entity_times.append(timings["old_entity"])
        paired_entity_times.append(timings["new_entity"])
        old_literal_times.append(timings["old_literal"])
        paired_literal_times.append(timings["new_literal"])
    print("paired sample: 20 queries, 10 alias hits + 10 misses; output parity PASS", flush=True)
    _report("old entity (paired)", old_entity_times)
    _report("new entity (paired)", paired_entity_times)
    _report("old literal (paired)", old_literal_times)
    _report("new literal (paired)", paired_literal_times)
    print("paired mean speedup: "
          f"entity={statistics.fmean(old_entity_times) / statistics.fmean(paired_entity_times):.2f}x "
          f"literal={statistics.fmean(old_literal_times) / statistics.fmean(paired_literal_times):.2f}x",
          flush=True)


def main() -> None:
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with _offline_guard():
        _run()


if __name__ == "__main__":
    main()
