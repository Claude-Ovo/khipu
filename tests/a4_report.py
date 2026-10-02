"""把 answer_compare.py 的结果（results.jsonl，每个 (qid, arm) 取最后一条）和两臂的上下文文件汇总成 A4 报告的数字：
按题型的正确数、配对变化（只旧对 / 只新对）、检索侧标签、答案侧标签、两臂返回内容的差异（返回集合、黄金轮命中、日期头）。不调接口。
用法：python tests/a4_report.py --answers /srv/aml/data/a4/answers --old contexts-old.jsonl --new contexts-new.jsonl [--out report.json]"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from math import comb
from pathlib import Path


def mcnemar_p(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def load_results(path: Path) -> dict[tuple[str, str], dict]:
    out = {}
    for l in open(path, encoding="utf-8"):
        o = json.loads(l); out[(o["qid"], o["arm"])] = o
    return out


def load_ctx(path: str) -> dict[str, dict]:
    return {json.loads(l)["qid"]: json.loads(l) for l in open(path, encoding="utf-8") if "\"returned\"" in l}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers", required=True)
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    R = load_results(Path(args.answers) / "results.jsonl")
    old_ctx, new_ctx = load_ctx(args.old), load_ctx(args.new)
    qids = [q for q in old_ctx if q in new_ctx and (q, "old") in R and (q, "new") in R]
    arms = sorted({a for _, a in R})
    rep: dict = {"questions": len(qids), "arms": arms}

    def correct(r: dict) -> int | None:
        return None if not r.get("complete") else int(r["majority"] == "CORRECT")

    # 1) 正确数，按题型
    by_type: dict[str, dict] = defaultdict(lambda: {"n": 0, **{a: 0 for a in arms}, "only_old": 0, "only_new": 0, "incomplete": 0})
    tot = {a: 0 for a in arms}; only_old = only_new = 0
    for q in qids:
        t = old_ctx[q].get("type"); row = by_type[t]; row["n"] += 1
        co, cn = correct(R[(q, "old")]), correct(R[(q, "new")])
        if co is None or cn is None:
            row["incomplete"] += 1; continue
        for a in arms:
            c = correct(R[(q, a)]) if (q, a) in R else None
            if c: row[a] += 1; tot[a] += 1
        if co and not cn: row["only_old"] += 1; only_old += 1
        if cn and not co: row["only_new"] += 1; only_new += 1
    rep["correct_total"] = tot; rep["only_old"] = only_old; rep["only_new"] = only_new; rep["mcnemar_p"] = round(mcnemar_p(only_old, only_new), 4)
    rep["by_type"] = dict(by_type)

    # 2) 标签分布
    rep["retrieval_label"] = {a: dict(Counter(R[(q, a)]["retrieval_label"] for q in qids if (q, a) in R)) for a in arms}
    rep["answer_label"] = {a: dict(Counter(R[(q, a)]["answer_label"] for q in qids if (q, a) in R)) for a in arms}

    # 3) 两臂返回的差异（不看答案）
    diff_rows = []
    same_set = same_order = 0
    for q in qids:
        o, n = old_ctx[q], new_ctx[q]
        so, sn = [r["id"] for r in o["returned"]], [r["id"] for r in n["returned"]]
        same_set += set(so) == set(sn); same_order += so == sn
        if (o["turn@100"], o["allturn@100"], o["allsess@100"]) != (n["turn@100"], n["allturn@100"], n["allsess@100"]):
            diff_rows.append({"qid": q, "type": o.get("type"), "old": [o["turn@100"], o["allturn@100"], o["allsess@100"]], "new": [n["turn@100"], n["allturn@100"], n["allsess@100"]]})
    rep["returned_same_set"] = same_set; rep["returned_same_order"] = same_order; rep["gold_hit_changed"] = diff_rows
    # 4) 日期头：同一段在两臂里的头
    ex = next((q for q in qids if old_ctx[q]["returned"] and new_ctx[q]["returned"]), None)
    if ex:
        rep["header_example"] = {"old": old_ctx[ex]["returned"][0]["content"][:40], "new": new_ctx[ex]["returned"][0]["content"][:40]}

    # 5) 逐题变化清单（答案层）
    changed = []
    for q in qids:
        ro, rn = R[(q, "old")], R[(q, "new")]
        if ro.get("majority") != rn.get("majority") or ro.get("answer_label") != rn.get("answer_label"):
            changed.append({"qid": q, "type": old_ctx[q].get("type"), "question": old_ctx[q]["question"][:90], "gold": old_ctx[q]["answer"][:80],
                            "old": {"answer": (ro.get("answer") or "")[:120], "votes": f"{ro.get('correct_votes')}/{ro.get('valid_votes')}", "retrieval": ro.get("retrieval_label"), "label": ro.get("answer_label")},
                            "new": {"answer": (rn.get("answer") or "")[:120], "votes": f"{rn.get('correct_votes')}/{rn.get('valid_votes')}", "retrieval": rn.get("retrieval_label"), "label": rn.get("answer_label")}})
    rep["changed"] = changed
    if "old_repeat" in arms:
        rep["repeat"] = {"majority_same": sum(R[(q, "old")].get("majority") == R[(q, "old_repeat")].get("majority") for q in qids if (q, "old_repeat") in R),
                         "text_same": sum(R[(q, "old")].get("answer") == R[(q, "old_repeat")].get("answer") for q in qids if (q, "old_repeat") in R)}
    txt = json.dumps(rep, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(txt, encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("questions", "correct_total", "only_old", "only_new", "mcnemar_p", "returned_same_set", "returned_same_order", "retrieval_label", "answer_label")}, ensure_ascii=False, indent=1))
    print("by type:")
    for t, row in rep["by_type"].items():
        print(f"  {t:26s} n={row['n']:3d} " + " ".join(f"{a}={row[a]}" for a in arms) + f"  only_old={row['only_old']} only_new={row['only_new']} incomplete={row['incomplete']}")
    print(f"changed questions: {len(changed)}; gold-hit changed: {len(diff_rows)}")


if __name__ == "__main__":
    main()
