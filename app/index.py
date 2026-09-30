"""每用户一份内存索引：BM25、实体表、日期表、规矩表、会话邻居。Add 后失效，Search 时按需重建。
版本在建索引前锁内捕获、建完再核对，中途失效就丢弃重建（审查 #3 P1-05）；每用户一把构建锁，并发 miss 只建一份。
部署只跑一个 worker，进程内失效才可靠（P1-04）。"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from rank_bm25 import BM25Okapi

from . import config
from .db import pool
from .textutil import date_strings, extract_names, same_person, tokenize


_ALIAS_WORD_RX = re.compile(r"\w+")


def build_alias_matcher(alias_to_group: dict[str, str]) -> tuple[
    dict[str, str], tuple[tuple[re.Pattern[str], str], ...]
]:
    """输入沿用 alias_to_group 的小写键；保留 str 正则的 Unicode 边界语义。

    纯词别名直接查查询的极大词段。少量含非词字符的别名分别预编译，
    避免单个交替正则漏掉同起点或相交的别名（它们可能属于不同规范名）。
    """
    words: dict[str, str] = {}
    patterns: list[tuple[re.Pattern[str], str]] = []
    for alias, canon in alias_to_group.items():
        if len(alias) <= 2:
            continue
        if _ALIAS_WORD_RX.fullmatch(alias):
            words[alias] = canon
        else:
            patterns.append((re.compile(rf"\b{re.escape(alias)}\b"), canon))
    return words, tuple(patterns)


@dataclass
class Row:
    pos: int
    id: str
    session_id: str
    seq: int
    part: int
    total: int
    role: str
    speaker_name: str | None
    ts_value: datetime | None
    ts_granularity: str
    text: str
    is_rule: bool
    has_vector: bool
    names: list[str] = field(default_factory=list)


@dataclass
class UserIndex:
    alias_word_rx: ClassVar[re.Pattern[str]] = _ALIAS_WORD_RX

    user_id: str
    rows: list[Row]
    bm25: BM25Okapi | None
    entity_groups: dict[str, set[int]]     # 规范名 -> 行号集合
    alias_to_group: dict[str, str]         # 任意写法(小写) -> 规范名
    by_date: dict[str, set[int]]
    by_month: dict[str, set[int]]
    rule_rows: list[int]
    session_label: dict[str, str]
    id_to_pos: dict[str, int]
    version: int
    token_sets: list[set[str]] = field(default_factory=list)  # 每行的词集合：小语料里 BM25 会打负分，命中与否按词集合判
    alias_words: dict[str, str] = field(default_factory=dict)
    alias_patterns: tuple[tuple[re.Pattern[str], str], ...] = ()
    lower_texts: list[str] = field(default_factory=list)


_cache: OrderedDict[str, UserIndex] = OrderedDict()
_lock = threading.Lock()
_versions: dict[str, int] = {}
_build_locks: dict[str, threading.Lock] = {}


def invalidate(user_id: str) -> None:
    with _lock:
        _versions[user_id] = _versions.get(user_id, 0) + 1
        _cache.pop(user_id, None)


def _load_rows(user_id: str) -> list[Row]:
    sql = ("SELECT id, session_id, seq, part, total, role, speaker_name, ts_value, ts_granularity, text, is_rule, "
           "embedding IS NOT NULL FROM segments WHERE user_id = %s ORDER BY session_id, seq, part")
    rows: list[Row] = []
    with pool.connection() as conn:
        for pos, r in enumerate(conn.execute(sql, (user_id,))):
            rows.append(Row(pos, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11]))
    return rows


def _index_text(row: Row) -> str:
    who = row.speaker_name or row.role
    return " ".join([who, *date_strings(row.ts_value), row.text])


def group_names(names_per_row: list[list[str]]) -> tuple[dict[str, set[int]], dict[str, str]]:
    """两遍归一：先汇总候选名，找出「被两个以上互不相同的长名包含」的歧义短名，独立保留、不做合并依据；
    再对其余名字按互相包含 / bigram ≥ 0.5 分组（审查 #3 P1-07）。"""
    all_names: dict[str, str] = {}  # 小写 -> 首次出现的写法
    for names in names_per_row:
        for n in names:
            all_names.setdefault(n.lower(), n)
    lowers = list(all_names)
    ambiguous: set[str] = set()
    for s in lowers:
        containers = [l for l in lowers if l != s and s in l]
        distinct = [c for i, c in enumerate(containers) if not any(same_person(c, o) for o in containers[:i])]
        if len(distinct) >= 2:
            ambiguous.add(s)
    canon_of: dict[str, str] = {}
    canons: list[str] = []
    for l in lowers:
        if l in ambiguous:
            canon_of[l] = all_names[l]
            continue
        found = None
        for c in canons:
            if c.lower() not in ambiguous and same_person(c, all_names[l]):
                found = c
                break
        if found is None:
            found = all_names[l]
            canons.append(found)
        canon_of[l] = found
    groups: dict[str, set[int]] = {}
    for pos, names in enumerate(names_per_row):
        for n in names:
            groups.setdefault(canon_of[n.lower()], set()).add(pos)
    return groups, canon_of


def _build(user_id: str, version: int) -> UserIndex:
    rows = _load_rows(user_id)
    corpus = [tokenize(_index_text(r)) for r in rows]
    bm25 = BM25Okapi(corpus, k1=1.5, b=0.75) if rows else None
    token_sets = [set(t) for t in corpus]
    for r in rows:
        r.names = extract_names(r.text)
        if r.speaker_name:
            r.names.append(r.speaker_name)
    groups, alias = group_names([r.names for r in rows])
    alias_words, alias_patterns = build_alias_matcher(alias)
    lower_texts = [r.text.lower() for r in rows]
    by_date: dict[str, set[int]] = {}
    by_month: dict[str, set[int]] = {}
    rule_rows: list[int] = []
    session_label: dict[str, str] = {}
    for r in rows:
        if r.ts_value is not None:
            by_date.setdefault(r.ts_value.strftime("%Y-%m-%d"), set()).add(r.pos)
            by_month.setdefault(r.ts_value.strftime("%Y-%m"), set()).add(r.pos)
        if r.is_rule:
            rule_rows.append(r.pos)
        if r.session_id not in session_label:
            session_label[r.session_id] = f"session {len(session_label) + 1}"
    return UserIndex(user_id, rows, bm25, groups, alias, by_date, by_month, rule_rows, session_label,
                     {r.id: r.pos for r in rows}, version, token_sets,
                     alias_words=alias_words, alias_patterns=alias_patterns, lower_texts=lower_texts)


def get_index(user_id: str) -> UserIndex:
    with _lock:
        idx = _cache.get(user_id)
        if idx is not None and idx.version == _versions.get(user_id, 0):
            _cache.move_to_end(user_id)
            return idx
        build_lock = _build_locks.setdefault(user_id, threading.Lock())
    with build_lock:
        for _ in range(3):
            with _lock:
                idx = _cache.get(user_id)
                version = _versions.get(user_id, 0)
                if idx is not None and idx.version == version:
                    return idx
            built = _build(user_id, version)
            with _lock:
                if _versions.get(user_id, 0) == version:  # 建的过程中没有新写入，才发布
                    _cache[user_id] = built
                    _cache.move_to_end(user_id)
                    while len(_cache) > config.INDEX_CACHE_USERS:
                        _cache.popitem(last=False)
                    return built
        return built  # 连续三次被写入打断：直接用最后一次（已包含到那一刻的数据），不缓存
