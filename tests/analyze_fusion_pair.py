"""读 diag_locomo_rerank_pair.py --pair fusion 的输出（两臂 old / new 都开重排），报：
1) 精确轮次口径：all-hop@10 / @100 两臂各多少、配对（只有 old 中 / 只有 new 中）、精确 McNemar p；
2) 每道变化题：每条黄金证据在两臂里的融合名次、重排后名次、最终名次；
3) --focus "conv,qi;conv,qi"：把这些题的黄金证据原文、说话人、两臂最终名次，以及审计里记的等价证据（--audit）在两臂的名次都打出来，供人工核对。
不调任何接口。用法：python tests/analyze_fusion_pair.py fusion-pair.jsonl --data locomo10.json [--audit relaxed-audit-claude.jsonl] [--focus "4,7;4,21;8,77"]"""
from __future__ import annotations

import argparse
import json
import re
import sys
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.diag_locomo_rerank_pair import conversation_dia_ids, conversation_texts, seq_of  # noqa: E402

ARMS = ("old", "new")


def mcnemar_p(b: int, c: int) -> float:
    """精确二项检验：b、c 是两个不一致方向的题数。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pair")
    ap.add_argument("--data", required=True)
    ap.add_argument("--audit", default="")
    ap.add_argument("--focus", default="")
    ap.add_argument("--top-k", type=int, default=100)
    args = ap.parse_args()

    data = json.load(open(args.data, encoding="utf-8"))
    convs = []
    for conv in data:
        dias = conversation_dia_ids(conv)
        texts = conversation_texts(conv)
        speakers = [t["speaker"] for n in sorted(int(k.split("_")[1]) for k in conv["conversation"] if re.fullmatch(r"session_\d+", k))
                    for t in conv["conversation"][f"session_{n}"]]
        convs.append({"seq_to_dia": {i + 1: d for i, d in enumerate(dias)}, "dia_to_seq": {d: i + 1 for i, d in enumerate(dias)},
                      "text": dict(zip(dias, texts)), "speaker": dict(zip(dias, speakers))})

    rows = [json.loads(l) for l in open(args.pair, encoding="utf-8") if l.strip()]
    rows = [r for r in rows if r["gold"] and not r["unmapped_gold"]]
    print(f"questions: {len(rows)}  (excluded: unmapped gold / no gold)")
    print("rerank status:", {a: sorted({r[a]["rerank_status"] for r in rows}) for a in ARMS})
    print("cost so far (last row): ¥", rows[-1].get("cum_cost_yuan"))

    print("\n=== exact-turn metrics (all gold turns of the question inside the returned top-k) ===")
    print(f"{'metric':10s} {'old':>6s} {'new':>6s} {'only old':>9s} {'only new':>9s} {'p':>9s}")
    for m in ("all@10", "all@100"):
        o = sum(r["old"][m] for r in rows); n = sum(r["new"][m] for r in rows)
        b = sum(1 for r in rows if r["old"][m] and not r["new"][m]); c = sum(1 for r in rows if r["new"][m] and not r["old"][m])
        print(f"{m:10s} {o:6d} {n:6d} {b:9d} {c:9d} {mcnemar_p(b, c):9.2e}")
    ho = sum(r["old"]["hops@100"] for r in rows); hn = sum(r["new"]["hops@100"] for r in rows); tot = sum(len(r["gold"]) for r in rows)
    print(f"gold turns returned in top-100: old {ho}/{tot}  new {hn}/{tot}")
    wo = sum(all(r["old"]["in_rerank_window"].values()) for r in rows); wn = sum(all(r["new"]["in_rerank_window"].values()) for r in rows)
    print(f"all gold inside rerank window (200): old {wo}  new {wn}")

    print("\n=== questions whose all@100 changes ===")
    changed = []
    for r in rows:
        if r["old"]["all@100"] != r["new"]["all@100"]:
            changed.append(r)
            tag = "GAINED" if r["new"]["all@100"] else "LOST"
            print(f"[{tag}] conv{r['conv']} q{r['qi']} | {r['question'][:80]!r}")
            for g in r["gold"]:
                cells = []
                for a in ARMS:
                    x = r[a]
                    cells.append(f"{a}: fused {x['rank_fused'].get(g)!s:>4} win={int(x['in_rerank_window'].get(g, False))} rerank {x['rank_after_rerank'].get(g)!s:>4} final {x['rank_final'].get(g)!s:>4}")
                print(f"    {g:8s} " + " | ".join(cells))
    print(f"changed: {len(changed)}")

    audit = {}
    if args.audit:
        for l in open(args.audit, encoding="utf-8"):
            o = json.loads(l)
            if o["type"] == "item":
                audit[(o["conv"], o["qi"], o["missing_gold"])] = o

    if args.focus:
        want = {tuple(int(x) for x in f.split(",")) for f in args.focus.split(";") if f.strip()}
        print("\n=== focus questions: gold turns, speakers, both arms' final ranks; audit equivalents ===")
        for r in rows:
            if (r["conv"], r["qi"]) not in want:
                continue
            C = convs[r["conv"]]
            print(f"\n## conv{r['conv']} q{r['qi']} | {r['question']} | gold answer: {r['answer']}")
            print(f"   all@100 old={r['old']['all@100']} new={r['new']['all@100']}   hops@100 old={r['old']['hops@100']} new={r['new']['hops@100']}")
            for g in r["gold"]:
                print(f"   GOLD {g} ({C['speaker'][g]}) final old={r['old']['rank_final'].get(g)} new={r['new']['rank_final'].get(g)} | {C['text'][g][:220]!r}")
                it = audit.get((r["conv"], r["qi"], g))
                if it:
                    print(f"        audit 09-30: {it['label']}, fact_in_returned={it['fact_in_returned']} :: {it['reason'][:200]}")
                    for e in it.get("returned_equivalents", []):
                        d = e["dia"]
                        fo = fn = None
                        for a in ARMS:
                            ids = [x["id"] for x in r[a]["final"]]
                            for i, it_id in enumerate(ids):
                                if C["seq_to_dia"].get(seq_of(it_id) or -1) == d:
                                    if a == "old":
                                        fo = i + 1
                                    else:
                                        fn = i + 1
                                    break
                        print(f"        equiv {d} ({C['speaker'].get(d)}) final old={fo} new={fn} | {C['text'].get(d, '')[:160]!r}")


if __name__ == "__main__":
    main()
