"""每用户一份内存索引：BM25、实体表、日期表、规矩表、会话邻居。Add 后失效，Search 时按需重建。"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime

from rank_bm25 import BM25Okapi

from . import config
from .db import pool
from .textutil import date_strings, same_person, tokenize


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
    user_id: str
    rows: list[Row]
    bm25: BM25Okapi | None
    entity_groups: dict[str, set[int]]     # 规范名 -> 行号集合
    alias_to_group: dict[str, str]         # 任意写法(小写) -> 规范名
    by_date: dict[str, set[int]]           # 'YYYY-MM-DD' -> 行号
    by_month: dict[str, set[int]]          # 'YYYY-MM' -> 行号
    rule_rows: list[int]
    session_label: dict[str, str]          # session_id -> 'session 3'
    id_to_pos: dict[str, int]
    version: int


_cache: OrderedDict[str, UserIndex] = OrderedDict()
_lock = threading.Lock()
_versions: dict[str, int] = {}


def invalidate(user_id: str) -> None:
    with _lock:
        _versions[user_id] = _versions.get(user_id, 0) + 1
        _cache.pop(user_id, None)


def _load_rows(user_id: str) -> list[Row]:
    sql = ("SELECT id, session_id, seq, part, total, role, speaker_name, ts_value, ts_granularity, text, is_rule, "
           "embedding IS NOT NULL FROM segments WHERE user_id = %s AND exact_dup_of IS NULL "
           "ORDER BY session_id, seq, part")
    rows: list[Row] = []
    with pool.connection() as conn:
        for pos, r in enumerate(conn.execute(sql, (user_id,))):
            rows.append(Row(pos, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11]))
    return rows


def _index_text(row: Row) -> str:
    who = row.speaker_name or row.role
    return " ".join([who, *date_strings(row.ts_value), row.text])


def _build(user_id: str) -> UserIndex:
    from .textutil import extract_names  # 局部导入避免循环

    rows = _load_rows(user_id)
    version = _versions.get(user_id, 0)
    corpus = [tokenize(_index_text(r)) for r in rows]
    bm25 = BM25Okapi(corpus, k1=1.5, b=0.75) if rows else None

    # 实体：抽名、归一
    groups: dict[str, set[int]] = {}
    alias: dict[str, str] = {}
    for r in rows:
        r.names = extract_names(r.text)
        if r.speaker_name:
            r.names.append(r.speaker_name)
        for n in r.names:
            key = n.lower()
            canon = alias.get(key)
            if canon is None:
                for existing in list(groups):
                    if same_person(existing, n):
                        canon = existing
                        break
                canon = canon or n
                alias[key] = canon
                groups.setdefault(canon, set())
            groups[canon].add(r.pos)
    # 泛指词保护：一个短名被两个以上不同的名包含，它不该并进任何一组
    for key, canon in list(alias.items()):
        containers = {c for c in groups if key != c.lower() and key in c.lower()}
        if len(containers) >= 2 and canon.lower() != key:
            alias[key] = key.capitalize()
            groups.setdefault(key.capitalize(), set())

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
                     {r.id: r.pos for r in rows}, version)


def get_index(user_id: str) -> UserIndex:
    with _lock:
        idx = _cache.get(user_id)
        if idx is not None and idx.version == _versions.get(user_id, 0):
            _cache.move_to_end(user_id)
            return idx
    idx = _build(user_id)
    with _lock:
        _cache[user_id] = idx
        _cache.move_to_end(user_id)
        while len(_cache) > config.INDEX_CACHE_USERS:
            _cache.popitem(last=False)
    return idx
