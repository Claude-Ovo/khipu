"""index.group_names 的索引化实现必须与原来的 O(N²) 实现逐字同结果（不连库、不联网）。
参照实现原样抄自 0e8f965 的 app/index.py；随机名字里故意混进长度 1~2 的、带撇号/连字符/空格的、CJK 的、
互相包含的、bigram 重叠正好在 0.5 边上的。
用法：.venv/Scripts/python.exe -m pytest tests/test_group_names.py -q
也可以对拍真实数据：.venv/Scripts/python.exe tests/test_group_names.py names.json（每行一个 list[str]）"""
from __future__ import annotations

import json
import random
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.index import group_names  # noqa: E402
from app.textutil import same_person  # noqa: E402


def group_names_reference(names_per_row: list[list[str]]) -> tuple[dict[str, set[int]], dict[str, str]]:
    all_names: dict[str, str] = {}
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


_ROOTS = ["ann", "anna", "hannah", "joanne", "annie", "smith", "smithson", "john", "johnny", "jon", "mary", "maria",
          "marianne", "li", "lee", "leon", "al", "alan", "allan", "bo", "bob", "bobby", "rob", "robert", "roberta",
          "陈晨", "晨", "小陈", "o'brien", "brien", "mary-jane", "jane", "de la cruz", "cruz", "x", "yy", "zed"]


def _random_name(rng: random.Random) -> str:
    kind = rng.random()
    if kind < 0.45:
        base = rng.choice(_ROOTS)
    elif kind < 0.8:
        base = "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(3, 9)))
    elif kind < 0.9:
        base = rng.choice(_ROOTS) + rng.choice(["", "s", "son", "ie", "y"])
    else:
        base = rng.choice(["a", "b", "ab", "李", "王小", "n'", "-"])
    if rng.random() < 0.3:
        base = base.capitalize()
    if rng.random() < 0.1:
        base = base.upper()
    return base


def _random_rows(rng: random.Random, n_rows: int, per_row: int) -> list[list[str]]:
    return [[_random_name(rng) for _ in range(rng.randint(0, per_row))] for _ in range(n_rows)]


def _check(names_per_row: list[list[str]]) -> None:
    g1, c1 = group_names_reference(names_per_row)
    g2, c2 = group_names(names_per_row)
    assert c1 == c2, {k: (c1[k], c2.get(k)) for k in c1 if c1[k] != c2.get(k)}
    assert g1 == g2


def test_hand_cases():
    _check([["Ann", "Anna"], ["Hannah", "ann"], ["Joanne"], ["Smith", "Smithson"], ["A"], ["Anna", "a"]])
    _check([["x"], ["xx"], ["xxx"], ["y"]])
    _check([["O'Brien", "Brien"], ["Mary-Jane", "Jane", "Mary"], ["De La Cruz", "Cruz"]])
    _check([["陈晨", "晨"], ["小陈"], ["晨"]])
    _check([[]])
    _check([])


def test_random_small_many_seeds():
    for seed in range(300):
        rng = random.Random(seed)
        _check(_random_rows(rng, rng.randint(1, 40), 4))


def test_random_larger():
    for seed in range(5):
        rng = random.Random(1000 + seed)
        _check(_random_rows(rng, 600, 6))


def test_ambiguous_short_name_stays_separate():
    # ann 被 brianna 和 joanne 包含，这两个互不相同（bigram 交 2 / min 5 < 0.5）→ ann 歧义，独立成组，不拿来合并
    rows = [["Ann"], ["Brianna"], ["Joanne"], ["ann"]]
    g, c = group_names(rows)
    assert c["ann"] == "Ann" and c["brianna"] == "Brianna" and c["joanne"] == "Joanne"
    assert g["Ann"] == {0, 3}
    _check(rows)


if __name__ == "__main__":
    rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
    import time
    t = time.time(); g1, c1 = group_names_reference(rows); t1 = time.time() - t
    t = time.time(); g2, c2 = group_names(rows); t2 = time.time() - t
    print(f"rows={len(rows)} names={len(c1)} reference {t1:.2f}s new {t2:.2f}s identical={c1 == c2 and g1 == g2}")
