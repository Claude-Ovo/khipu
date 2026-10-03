"""B2 可行性·离线分组：用向量余弦在「模拟 top-100」里连通分组，看黄金新旧对能不能进同一组、组里混进多少噪声。
模拟 top-100 = 与问题余弦最高的 100 条 user 发言；黄金行若不在其中强行放入（对应检索已递到的情形）。"""
import json, sys
import numpy as np
from datetime import datetime

arr = np.load("/srv/aml/data/b2/rows.npy")
meta = [json.loads(l) for l in open("/srv/aml/data/b2/rows.meta.jsonl")]
n = np.linalg.norm(arr, axis=1, keepdims=True); n[n == 0] = 1
arr = arr / n
byq = {}
for i, m in enumerate(meta):
    byq.setdefault(m["qi"], []).append(i)


def pdate(date):
    day, rest = date.split(" (")
    return datetime.strptime(day + " " + rest.split(") ")[1], "%Y/%m/%d %H:%M")


def components(idx, tau):
    k = len(idx)
    S = arr[idx] @ arr[idx].T
    parent = list(range(k))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    for a in range(k):
        for b in range(a + 1, k):
            if S[a, b] >= tau:
                parent[find(a)] = find(b)
    comp = {}
    for a in range(k):
        comp.setdefault(find(a), []).append(idx[a])
    return list(comp.values())


partner_rank = []
for qi, rows in byq.items():
    qrow = [i for i in rows if meta[i].get("is_q")][0]
    cand = [i for i in rows if not meta[i].get("is_q")]
    gold = [i for i in cand if meta[i]["gold"]]
    for g in gold:
        sims = sorted(((float(arr[g] @ arr[j]), j) for j in cand if j != g), reverse=True)
        for r, (s, j) in enumerate(sims, 1):
            if j in gold:
                partner_rank.append(r); break
partner_rank.sort()
print("vector: partner ranked 1st", sum(r == 1 for r in partner_rank), "/", len(partner_rank), "| top3", sum(r <= 3 for r in partner_rank),
      "| top10", sum(r <= 10 for r in partner_rank), "| median", partner_rank[len(partner_rank) // 2], "| worst", partner_rank[-1])

print("\ntau | gold pair linked (of 39) | noise rows in gold's group: mean/max | newest-in-group is the true new statement | rows tagged per context (mean)")
for tau in (0.55, 0.60, 0.65, 0.70, 0.75, 0.80):
    linked = 0; noise = []; newest_ok = 0; tagged = []
    for qi, rows in byq.items():
        qrow = [i for i in rows if meta[i].get("is_q")][0]
        cand = [i for i in rows if not meta[i].get("is_q")]
        gold = [i for i in cand if meta[i]["gold"]]
        sims = arr[cand] @ arr[qrow]
        top = [cand[j] for j in np.argsort(-sims)[:100]]
        ctx = list(dict.fromkeys(top + gold))
        comps = components(ctx, tau)
        tagged.append(sum(len(c) for c in comps if len(c) > 1))
        gc = [c for c in comps if any(i in gold for i in c)]
        allin = any(all(g in c for g in gold) for c in comps)
        linked += allin
        if allin:
            c = [c for c in comps if all(g in c for g in gold)][0]
            noise.append(len(c) - len(gold))
            newest = max(c, key=lambda i: (pdate(meta[i]["date"]), meta[i]["seq"]))
            true_new = max(gold, key=lambda i: (pdate(meta[i]["date"]), meta[i]["seq"]))
            newest_ok += newest == true_new
    print(f"{tau:.2f} | {linked} | {np.mean(noise) if noise else 0:.1f}/{max(noise) if noise else 0} | {newest_ok} | {np.mean(tagged):.0f}")
