"""离线补算 Tier A 的 token F1（REPORT §11 里说过要报、§12 第一版漏了）。固定实现，不调接口：
SQuAD 式归一化（小写、去标点、去冠词 a/an/the、合并空白）后按 token 多重集算 F1。这不是官方判分，只是第二个不靠模型的信号。
用法：python tests/tier_a_f1.py <tier-a/results.json> --out <tier-a/f1.json>"""
from __future__ import annotations

import argparse
import json
import re
import string
from collections import Counter


def normalize(s: str) -> list[str]:
    s = s.lower()
    s = "".join(ch for ch in s if ch not in set(string.punctuation))
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    return s.split()


def f1(pred: str, gold: str) -> float:
    p, g = Counter(normalize(pred)), Counter(normalize(gold))
    common = sum((p & g).values())
    if common == 0:
        return 0.0
    precision, recall = common / sum(p.values()), common / sum(g.values())
    return 2 * precision * recall / (precision + recall)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    R = json.load(open(args.results, encoding="utf-8"))
    out = {"implementation": "token F1 after SQuAD-style normalization (lowercase, strip punctuation, drop a/an/the); "
                             "multiset overlap; not the official metric", "questions": []}
    sums = {}
    for q in R["results"]:
        row = {"conv": q["conv"], "qi": q["qi"], "gold_answer": q["gold_answer"]}
        for arm, a in q["arms"].items():
            row[arm] = round(f1(a["answer"], q["gold_answer"]), 4)
            sums[arm] = sums.get(arm, 0.0) + row[arm]
        out["questions"].append(row)
    n = len(R["results"])
    out["mean"] = {arm: round(v / n, 4) for arm, v in sums.items()}
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out["mean"]))
    for row in out["questions"]:
        print(f"({row['conv']},{row['qi']}) old {row['old']:.2f} new {row['new']:.2f} repeat {row['old_repeat']:.2f}")


if __name__ == "__main__":
    main()
