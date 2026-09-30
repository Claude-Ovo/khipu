"""汇总 diag_locomo_rerank_pair.py 的输出：逐题配对、逐条黄金证据的名次、证据在哪一步丢的。
用法：python tests/analyze_locomo_pair.py /srv/aml/data/diag-locomo/pair.jsonl > report.md"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from math import comb


def mcnemar_exact(b: int, c: int) -> float:
    """b = 只有 off 对，c = 只有 on 对；双侧精确符号检验。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(comb(n, i) for i in range(k + 1)) / 2 ** n * 2
    return min(1.0, p)


def pct(x: float) -> str:
    return f"{x:.3f}"


def main() -> None:
    rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
    seen = set()
    uniq = []
    for r in rows:  # 同一题重复跑过的只留最后一条
        key = (r["conv"], r["qi"])
        if key in seen:
            uniq = [u for u in uniq if (u["conv"], u["qi"]) != key]
        seen.add(key)
        uniq.append(r)
    rows = [r for r in uniq if r["gold"]]
    n = len(rows)
    print(f"# LoCoMo 多跳（category 1）重排关 / 开 配对结果\n")
    print(f"- 题数：{n}（有黄金证据的）；黄金证据条数分布：{dict(sorted(Counter(len(r['gold']) for r in rows).items()))}")
    print(f"- 坏标注：{sum(bool(r['bad_evidence']) for r in rows)} 题；映射不到对话的证据：{sum(bool(r['unmapped_gold']) for r in rows)} 题")
    fused_same = sum(r["off"]["trace"]["fused"] == r["on"]["trace"]["fused"] for r in rows)
    print(f"- 两臂融合顺序（重排前）完全一致：{fused_same}/{n}")
    st = Counter(r["on"]["rerank_status"] for r in rows)
    print(f"- 开重排那一臂的重排状态：{dict(st)}")
    print(f"- 累计重排 token / 花费：见 summary 文件\n")

    print("## 1. 全部证据都进前 k 条的比例（all-hop recall，按每条黄金证据原句算）\n")
    print("| 指标 | 重排关 | 重排开 | 只有关对 | 只有开对 | 精确检验 p |")
    print("|---|---|---|---|---|---|")
    for k in (10, 100):
        off = [r["off"][f"all@{k}"] for r in rows]
        on = [r["on"][f"all@{k}"] for r in rows]
        b = sum(1 for x, y in zip(off, on) if x and not y)
        c = sum(1 for x, y in zip(off, on) if y and not x)
        print(f"| all-hop recall@{k} | {pct(statistics.mean(off))} | {pct(statistics.mean(on))} | {b} | {c} | {mcnemar_exact(b, c):.2g} |")
    for k in (10, 100):
        tot = sum(len(r["gold"]) for r in rows)
        off = sum(r["off"][f"hops@{k}"] for r in rows) / tot
        on = sum(r["on"][f"hops@{k}"] for r in rows) / tot
        print(f"| 单条证据 recall@{k}（按证据条数） | {pct(off)} | {pct(on)} | | | |")

    print("\n## 2. 按证据条数分组（重排开）\n")
    print("| 证据条数 | 题数 | all@10 关→开 | all@100 关→开 |")
    print("|---|---|---|---|")
    groups: dict[str, list] = {}
    for r in rows:
        g = str(len(r["gold"])) if len(r["gold"]) < 4 else "4+"
        groups.setdefault(g, []).append(r)
    for g in sorted(groups):
        rs = groups[g]
        f = lambda arm, k: pct(statistics.mean(x[arm][f"all@{k}"] for x in rs))
        print(f"| {g} | {len(rs)} | {f('off',10)}→{f('on',10)} | {f('off',100)}→{f('on',100)} |")

    print("\n## 3. 开重排时，没进前 100 的那些证据丢在哪一步\n")
    print("每条缺失的黄金证据只归一类，按管线顺序判：")
    print("- A 五路召回都没捞到（不在融合列表里）\n- B 捞到了，但融合后排在 200 名以外，没进重排窗口\n"
          "- C 进了重排窗口，重排后排到 100 名以外\n- D 重排后在前 100，但最终装箱时没进（邻居/规矩插入挤掉，或预算跳过）\n")
    cls = Counter()
    per_q_cls = Counter()
    examples: dict[str, list] = {}
    window = None
    for r in rows:
        on = r["on"]
        head = on["trace"]["rerank"].get("head") or []
        window = window or len(head)
        after = on["rank_after_rerank"] or {}
        missing = [g for g, rk in on["rank_final"].items() if rk is None or rk > 100]
        qcls = set()
        for g in missing:
            fr = on["rank_fused"].get(g)
            ar = after.get(g)
            if fr is None:
                c = "A"
            elif fr > len(head):
                c = "B"
            elif ar is None or ar > 100:
                c = "C"
            else:
                c = "D"
            cls[c] += 1
            qcls.add(c)
            examples.setdefault(c, []).append((r["conv"], r["qi"], g, fr, ar, on["rank_final"].get(g)))
        for c in qcls:
            per_q_cls[c] += 1
    print("| 类别 | 缺失证据条数 | 涉及题数 |")
    print("|---|---|---|")
    for c in "ABCD":
        print(f"| {c} | {cls[c]} | {per_q_cls[c]} |")
    print(f"\n（重排窗口 = {window} 条）")
    for c in "ABCD":
        if examples.get(c):
            print(f"\n{c} 类前 5 个例子（conv, qi, 证据, 融合名次, 重排后名次, 最终名次）：")
            for e in examples[c][:5]:
                print(f"- {e}")

    print("\n## 4. 每条黄金证据的名次分布（重排开）\n")
    fr_all, ar_all, fin_all, off_fin = [], [], [], []
    for r in rows:
        for g in r["gold"]:
            fr_all.append(r["on"]["rank_fused"].get(g))
            ar_all.append((r["on"]["rank_after_rerank"] or {}).get(g))
            fin_all.append(r["on"]["rank_final"].get(g))
            off_fin.append(r["off"]["rank_final"].get(g))

    def dist(xs: list) -> str:
        buckets = Counter()
        for x in xs:
            if x is None:
                buckets["不在"] += 1
            elif x <= 10:
                buckets["1-10"] += 1
            elif x <= 50:
                buckets["11-50"] += 1
            elif x <= 100:
                buckets["51-100"] += 1
            elif x <= 200:
                buckets["101-200"] += 1
            else:
                buckets[">200"] += 1
        order = ["1-10", "11-50", "51-100", "101-200", ">200", "不在"]
        return " / ".join(f"{b}: {buckets[b]}" for b in order)
    print(f"- 融合后（重排前）：{dist(fr_all)}")
    print(f"- 重排后（只含窗口内）：{dist(ar_all)}")
    print(f"- 最终返回（重排开）：{dist(fin_all)}")
    print(f"- 最终返回（重排关）：{dist(off_fin)}")

    print("\n## 5. 最终上下文的大小\n")
    for arm in ("off", "on"):
        toks = [r[arm]["final_tokens_cl100k"] for r in rows]
        ns = [r[arm]["final_n"] for r in rows]
        print(f"- {arm}：条数中位 {statistics.median(ns)}，token（cl100k）中位 {statistics.median(toks)}、最大 {max(toks)}；"
              f"因预算跳过的条目共 {sum(r[arm]['budget_skipped'] or 0 for r in rows)}")
    lat = [r["on"]["latency_s"] for r in rows]
    print(f"- 开重排单题延迟中位 {statistics.median(lat):.2f}s，最大 {max(lat):.2f}s")


if __name__ == "__main__":
    main()
