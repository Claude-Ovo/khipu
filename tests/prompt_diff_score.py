"""给 prompt_diff.py 的输出打启发式分（复审 #11-A：评分器要归档、口径要写明）。

对每个窗把 removed 行和 added 行做一对一配对（按 difflib 相似度从高到低贪心，cutoff 0.5，一行只配一次，避免一次合并同时记一丢一增），
然后数：
  dated->undated  配对里旧行有四位年份、新行没有（丢了日期）
  bare undated->dated  旧行没有年份、新行有而且不是 "(said on …)"（新行被盖上了日期）
  hedge net  新增减去移除的含不确定词的行数
这些是候选变化数，不是核实过的错误数：合法归一化（"yesterday" 换算成日期）和合并都会计进来。人工核的错误类别另列。
用法：python tests/prompt_diff_score.py a.jsonl b.jsonl …"""
from __future__ import annotations

import difflib
import json
import re
import sys

DATED = re.compile(r"\b(19|20)\d\d\b")
HEDGE = re.compile(r"\b(plans? to|planning|is considering|considering|may\b|might\b|was going to|not sure|unsure|cancell?ed|intends?|hopes? to|possibly|uncertain|unconfirmed)\b", re.I)


def pair(removed: list[str], added: list[str], cutoff: float = 0.5) -> list[tuple[str, str]]:
    cands = []
    for i, o in enumerate(removed):
        for j, n in enumerate(added):
            r = difflib.SequenceMatcher(None, o, n).ratio()
            if r >= cutoff:
                cands.append((r, i, j))
    cands.sort(reverse=True)
    used_o, used_n, out = set(), set(), []
    for r, i, j in cands:
        if i in used_o or j in used_n:
            continue
        used_o.add(i); used_n.add(j); out.append((removed[i], added[j]))
    return out


def score(path: str) -> dict:
    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    n_old = sum(r["n_old"] for r in rows); n_new = sum(r["n_new"] for r in rows)
    removed = sum(len(r["removed"]) for r in rows)
    lost = bare = soft = 0
    for r in rows:
        for o, n in pair(r["removed"], r["added"]):
            if DATED.search(o) and not DATED.search(n):
                lost += 1
            elif not DATED.search(o) and DATED.search(n):
                if "said on" in n:
                    soft += 1
                else:
                    bare += 1
    hedge = sum(1 for r in rows for a in r["added"] if HEDGE.search(a)) - sum(1 for r in rows for a in r["removed"] if HEDGE.search(a))
    return {"file": path, "windows": len(rows), "identical": sum(1 for r in rows if not r["added"] and not r["removed"]),
            "verbatim_pct": round(100 * (n_old - removed) / n_old), "lines": f"{n_old}->{n_new}",
            "dated_to_undated": lost, "bare_undated_to_dated": bare, "said_on_added": soft, "hedge_net": hedge}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for p in sys.argv[1:]:
        print(json.dumps(score(p), ensure_ascii=False))
