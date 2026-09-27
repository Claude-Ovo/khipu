"""逐题成对比较两次闭环（run_official_eval 的 eval-<tag>/results.jsonl）：谁赢谁输、按题型分层、翻转题列表。
总分差 7 题以内不算区别（9-27 实测同输入复跑逐题翻 13/200），看净赢输和翻转集合才有意义。
用法：python tests/compare_results.py eval-A/results.jsonl eval-B/results.jsonl [--input eval-A/input.jsonl] [--show 10]"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def rows(p: str) -> list[dict]:
    return [json.loads(l) for l in Path(p).read_text(encoding="utf-8").split(chr(10)) if l.strip()]


def score(r: dict) -> float:
    for k in ("is_correct", "llm_judge_score", "rubric_clbench_score", "score"):
        if k in r and r[k] is not None:
            return float(r[k])
    raise KeyError(f"no score in {r.get('id')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--input", default="", help="input.jsonl，用来拿题型和题干")
    ap.add_argument("--show", type=int, default=10)
    args = ap.parse_args()
    a = {r["id"]: score(r) for r in rows(args.a)}
    b = {r["id"]: score(r) for r in rows(args.b)}
    ids = sorted(set(a) & set(b))
    if set(a) != set(b):
        print(f"warning: id sets differ (A {len(a)}, B {len(b)}, common {len(ids)})")
    meta: dict[str, dict] = {}
    if args.input:
        meta = {r["id"]: r for r in rows(args.input)}

    def qtype(i: str) -> str:
        m = meta.get(i, {})
        return str(m.get("question_type") or m.get("category") or "all")

    wins = [i for i in ids if b[i] > a[i]]
    losses = [i for i in ids if b[i] < a[i]]
    print(f"n={len(ids)}  A={sum(a[i] for i in ids) / len(ids):.4f}  B={sum(b[i] for i in ids) / len(ids):.4f}  "
          f"B wins {len(wins)}  B loses {len(losses)}  net {len(wins) - len(losses):+d}")
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for i in ids:
        t = qtype(i)
        by[t][2] += 1
        if b[i] > a[i]:
            by[t][0] += 1
        elif b[i] < a[i]:
            by[t][1] += 1
    for t, (w, l, n) in sorted(by.items()):
        print(f"  {t:32s} n={n:4d}  wins {w:3d}  losses {l:3d}  net {w - l:+d}")
    if args.show:
        for label, lst in (("B wins", wins), ("B loses", losses)):
            print(f"\n{label} (first {args.show}):")
            for i in lst[: args.show]:
                q = str(meta.get(i, {}).get("question", ""))[:100]
                print(f"  {i}  [{qtype(i)}]  A={a[i]:.2f} B={b[i]:.2f}  {q}")


if __name__ == "__main__":
    main()
