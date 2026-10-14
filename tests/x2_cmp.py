"""第二枪实验（10-15）的成对覆盖比较：两份检索转储（lme_context_dump.py 或 b3_locomo.py dump 的 contexts-*.jsonl），
逐题比 gold 覆盖（turn@10 / allturn@100 / 首条 gold 名次）和返回里笔记条数，报 W/L。只读文件，不花钱。
用法：python tests/x2_cmp.py A.jsonl B.jsonl [--kind lme|locomo] [--show 8]"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

_ID = re.compile(r"#(\d+)\.(\d+)$")


def load(p: str) -> dict[str, dict]:
    out = {}
    for line in Path(p).read_text(encoding="utf-8").split("\n"):
        if line.strip():
            r = json.loads(line)
            out[str(r["qid"])] = r
    return out


def item_id(it) -> str:
    if isinstance(it, dict):
        return str(it.get("id") or it.get("memory_id") or "")
    return str(it)


def item_text(it) -> str:
    if isinstance(it, dict):
        return str(it.get("content") or it.get("text") or "")
    return ""


def first_gold_rank(r: dict) -> int | None:
    gold = set(int(g) for g in r.get("gold", []))
    for i, it in enumerate(r.get("returned", [])):
        m = _ID.search(item_id(it))
        if m and int(m.group(1)) in gold:
            return i + 1
    return None


def notes_in(r: dict) -> int:
    if "notes_returned" in r:
        return int(r["notes_returned"])
    n = 0
    for it in r.get("returned", []):
        t = item_text(it)
        if "(memory note)" in t or "(conversation summary)" in t:
            n += 1
        elif ".n" in item_id(it) and _ID.search(item_id(it)) is None:
            n += 1
    return n


def flag(r: dict, k: str) -> int | None:
    v = r.get(k)
    return None if v is None else int(v)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--kind", choices=("lme", "locomo"), default="lme")
    ap.add_argument("--show", type=int, default=8)
    args = ap.parse_args()
    A, B = load(args.a), load(args.b)
    qids = [q for q in A if q in B]
    missing = (set(A) ^ set(B))
    print(f"paired {len(qids)} questions" + (f" (unpaired {len(missing)})" if missing else ""))
    keys = ("turn@10", "allturn@100", "turn@20", "allturn@50") if args.kind == "lme" else ("any@10", "all@100")
    for k in keys:
        a = [flag(A[q], k) for q in qids]
        b = [flag(B[q], k) for q in qids]
        if any(x is None for x in a + b):
            continue
        w = sum(1 for x, y in zip(a, b) if y > x)
        l = sum(1 for x, y in zip(a, b) if y < x)
        print(f"{k:12s} A={sum(a):3d}  B={sum(b):3d}  B wins {w} / loses {l}")
    ra = [first_gold_rank(A[q]) for q in qids]
    rb = [first_gold_rank(B[q]) for q in qids]
    both = [(x, y) for x, y in zip(ra, rb) if x and y]
    if both:
        xa, xb = [x for x, _ in both], [y for _, y in both]
        print(f"first-gold rank (both hit, n={len(both)}): A median {statistics.median(xa):.0f} p90 {sorted(xa)[int(len(xa)*0.9)]}"
              f" | B median {statistics.median(xb):.0f} p90 {sorted(xb)[int(len(xb)*0.9)]}"
              f" | B better {sum(1 for x, y in both if y < x)} worse {sum(1 for x, y in both if y > x)}")
    print(f"gold found in top-100: A {sum(1 for x in ra if x)} B {sum(1 for x in rb if x)}")
    na = [notes_in(A[q]) for q in qids]
    nb = [notes_in(B[q]) for q in qids]
    print(f"notes returned avg: A {statistics.mean(na):.1f}  B {statistics.mean(nb):.1f}")
    ta = [A[q].get("total_tokens") for q in qids]
    tb = [B[q].get("total_tokens") for q in qids]
    if all(ta) and all(tb):
        print(f"returned tokens avg: A {statistics.mean(ta):.0f}  B {statistics.mean(tb):.0f}")
    by_type: dict[str, list[str]] = {}
    for q in qids:
        by_type.setdefault(str(A[q].get("type")), []).append(q)
    k0 = keys[0]
    k1 = keys[1]
    for t, qs in sorted(by_type.items()):
        a0 = sum(int(A[q].get(k0) or 0) for q in qs)
        b0 = sum(int(B[q].get(k0) or 0) for q in qs)
        a1 = sum(int(A[q].get(k1) or 0) for q in qs)
        b1 = sum(int(B[q].get(k1) or 0) for q in qs)
        print(f"  {t:24s} n={len(qs):3d}  {k0}: {a0}->{b0}  {k1}: {a1}->{b1}")
    shown = 0
    for q in qids:
        if shown >= args.show:
            break
        diff = [k for k in keys if flag(A[q], k) is not None and flag(A[q], k) != flag(B[q], k)]
        if diff:
            print(f"  flip {q} {A[q].get('type')}: " + ", ".join(f"{k} {flag(A[q], k)}->{flag(B[q], k)}" for k in diff)
                  + f"  firstgold {first_gold_rank(A[q])}->{first_gold_rank(B[q])}  | {str(A[q].get('question'))[:70]}")
            shown += 1


if __name__ == "__main__":
    main()
