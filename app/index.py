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
from .textutil import date_strings, extract_names, looks_like_forget, same_person, tokenize


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
    kind: str = "msg"            # msg 原文 / note 抽出的事实行 / summary 会话摘要（B3）
    note_key: str | None = None
    note_tag: str = ""           # EXTRACT_MARK_LATEST 时同话题键的新旧标记，渲染时接在正文后


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
    token_cache: dict[int, int] = field(default_factory=dict)   # 行号 -> 渲染后的 token 数（search._rendered_tokens 按需填）
    speakers: frozenset[str] = frozenset()   # 原文里出现过的说话人名（LoCoMo 这类有名字的对话）；笔记的主语是其中之一时挂到他名下渲染
    forget_rows: list[int] = field(default_factory=list)   # 遗忘/撤回指令的原文段（建索引时按正则认，不进库）


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
           "embedding IS NOT NULL, kind, note_key FROM segments WHERE user_id = %s"
           + ("" if config.NOTES_IN_SEARCH else " AND kind = 'msg'") + " ORDER BY session_id, seq, part")
    rows: list[Row] = []
    with pool.connection() as conn:
        for pos, r in enumerate(conn.execute(sql, (user_id,))):
            rows.append(Row(pos, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11],
                            kind=r[12], note_key=r[13]))
    return rows


def _index_text(row: Row) -> str:
    # 笔记的 role 是 note/summary，不是说话人，别让这两个词进 BM25
    who = row.speaker_name or (row.role if row.kind == "msg" else "")
    return " ".join([who, *date_strings(row.ts_value), row.text])


def mark_latest(rows: list[Row]) -> None:
    """同一话题键（user.job 之类）有几条笔记时，按「哪天说的」标出新旧，只陈述日期不下结论：
    键是模型逐窗起的，多值的话题（宠物、爱好）也会共用一个键，旧的不一定作废，判断留给答题的人。
    同一天的几条都算最新。"""
    groups: dict[str, list[Row]] = {}
    for r in rows:
        if r.kind == "note" and r.note_key:
            groups.setdefault(r.note_key, []).append(r)
    for key, rs in groups.items():
        days = sorted({r.ts_value.date() for r in rs if r.ts_value is not None})
        if len(days) < 2:
            continue
        for r in rs:
            if r.ts_value is None:
                continue
            d = r.ts_value.date()
            if d == days[-1]:
                r.note_tag = f"[newest note on {key}]"
            else:
                later = days[days.index(d) + 1]
                r.note_tag = f"[older note on {key}; a newer one is dated {later.isoformat()}]"


def _containers_index(lowers: list[str]) -> dict[str, list[str]]:
    """三字子串 -> 含它的名字（按 lowers 顺序）。「s 是 l 的子串」必要条件是 s 的任一三字子串也在 l 里，
    所以查 s 最稀有的那个三字子串的倒排就够了，再逐个核对 s in l。顺序保持 lowers 顺序，后面数 distinct 时要用。"""
    tri: dict[str, list[str]] = {}
    for l in lowers:
        for g in {l[i:i + 3] for i in range(len(l) - 2)}:
            tri.setdefault(g, []).append(l)
    return tri


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def group_names(names_per_row: list[list[str]]) -> tuple[dict[str, set[int]], dict[str, str]]:
    """两遍归一：先汇总候选名，找出「被两个以上互不相同的长名包含」的歧义短名，独立保留、不做合并依据；
    再对其余名字按互相包含 / bigram ≥ 0.5 分组（审查 #3 P1-07）。

    09-30：两步原来都是 O(N²)（维基类用户 16,278 个名字要 16.7 秒，每次 Add 后重建索引都要再来一遍）。
    现在第一步用三字子串倒排找包含关系，第二步用 numpy 一次算出某个名字与所有已有规范名的 bigram 重叠数；
    判定条件和扫描顺序（取第一个匹配的规范名）与原实现完全一致，输出逐字相同（tests/test_group_names.py 对拍）。
    只有长度不到 2 的名字（没有 bigram，same_person 走子串路径）退回逐个比较。"""
    import numpy as np

    all_names: dict[str, str] = {}  # 小写 -> 首次出现的写法
    for names in names_per_row:
        for n in names:
            all_names.setdefault(n.lower(), n)
    lowers = list(all_names)

    # ---- 第一步：歧义短名 ----
    tri = _containers_index(lowers)
    ambiguous: set[str] = set()
    for s in lowers:
        if len(s) >= 3:
            rarest = min((s[i:i + 3] for i in range(len(s) - 2)), key=lambda g: len(tri.get(g, ())))
            containers = [l for l in tri.get(rarest, ()) if l != s and s in l]
        else:
            containers = [l for l in lowers if l != s and s in l]
        distinct = [c for i, c in enumerate(containers) if not any(same_person(c, o) for o in containers[:i])]
        if len(distinct) >= 2:
            ambiguous.add(s)

    # ---- 第二步：归到第一个「同一个人」的规范名 ----
    col: dict[str, int] = {}
    for l in lowers:
        for b in _bigrams(l):
            col.setdefault(b, len(col))
    cap = 256
    mat = np.zeros((cap, max(len(col), 1)), dtype=np.uint8)   # 规范名 × bigram
    size = np.zeros(cap, dtype=np.int32)                      # 每个规范名的 bigram 数
    canon_of: dict[str, str] = {}
    canons: list[str] = []
    short_canons: list[int] = []                              # 长度 < 2 的规范名，没有 bigram，只能逐个比
    for l in lowers:
        if l in ambiguous:
            canon_of[l] = all_names[l]
            continue
        name = all_names[l]
        bg = _bigrams(l)
        cols = [col[b] for b in bg]
        found = None
        n = len(canons)
        if n:
            hit = np.zeros(n, dtype=bool)
            if cols:
                shared = mat[:n, cols].sum(axis=1, dtype=np.int32)
                sz = size[:n]
                # same_person：互相包含，或 |交| / min(|a|,|b|) ≥ 0.5。两边都有 bigram 时，包含关系蕴含后者
                hit = (sz > 0) & (shared * 2 >= np.minimum(sz, len(bg)))
            for i in short_canons:
                hit[i] = same_person(canons[i], name)
            if not cols:  # 自己没有 bigram：只可能走子串路径，逐个比
                for i in range(n):
                    hit[i] = same_person(canons[i], name)
            idx = int(np.argmax(hit)) if hit.any() else -1
            if idx >= 0:
                found = canons[idx]
        if found is None:
            found = name
            if n >= cap:
                cap *= 2
                mat = np.concatenate([mat, np.zeros((cap - n, mat.shape[1]), dtype=np.uint8)])
                size = np.concatenate([size, np.zeros(cap - n, dtype=np.int32)])
            mat[n, cols] = 1
            size[n] = len(bg)
            if not cols:
                short_canons.append(n)
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
    if config.EXTRACT_MARK_LATEST:
        mark_latest(rows)
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
                     alias_words=alias_words, alias_patterns=alias_patterns, lower_texts=lower_texts,
                     speakers=frozenset(r.speaker_name for r in rows if r.kind == "msg" and r.speaker_name),
                     forget_rows=[r.pos for r in rows if r.kind == "msg" and looks_like_forget(r.text, r.role)])


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
