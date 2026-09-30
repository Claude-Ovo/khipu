"""把配对实验（diag_locomo_rerank_pair.py 的输出）里两臂最终返回的 100 条，按线上同一个 _render 渲染成答题模型会看到的正文。
只读库、不调任何接口。给审阅打包和之后的答题对照用（collab/诊断-事实与多跳-20260930/fusion-sim/REPORT.md §11）。

用法（服务器 /srv/aml/app2 下，先 set -a; . /srv/aml/.env; . /srv/aml/.env2; set +a）：
  python tests/dump_pair_contexts.py /srv/aml/data/diag-locomo/fusion-pair.jsonl --out /srv/aml/data/diag-locomo/fusion-pair-contexts.jsonl
输出每题一行：题目、标准答案、黄金证据原文，以及每个臂的 100 条（名次、id、dia、渲染后的正文）。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import search as S  # noqa: E402
from app.db import pool  # noqa: E402
from tests.diag_locomo_rerank_pair import conversation_dia_ids, conversation_texts, seq_of  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pair")
    ap.add_argument("--data", default="/srv/aml/data/locomo10.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="", help='只导这些题："conv,qi;conv,qi"')
    args = ap.parse_args()

    data = json.load(open(args.data, encoding="utf-8"))
    convs = []
    for conv in data:
        dias = conversation_dia_ids(conv)
        texts = conversation_texts(conv)
        c = conv["conversation"]
        speakers, dates = [], []
        for n in sorted(int(k.split("_")[1]) for k in c if re.fullmatch(r"session_\d+", k)):
            for t in c[f"session_{n}"]:
                speakers.append(t["speaker"])
                dates.append(c.get(f"session_{n}_date_time"))
        convs.append({"seq_to_dia": {i + 1: d for i, d in enumerate(dias)}, "text": dict(zip(dias, texts)),
                      "speaker": dict(zip(dias, speakers)), "date": dict(zip(dias, dates))})
    only = {tuple(int(x) for x in f.split(",")) for f in args.only.split(";") if f.strip()}

    pool.open()
    arms = None
    n_out = 0
    with open(args.out, "w", encoding="utf-8") as out:
        for line in open(args.pair, encoding="utf-8"):
            r = json.loads(line)
            if only and (r["conv"], r["qi"]) not in only:
                continue
            if arms is None:
                arms = [a for a in ("off", "on", "old", "new") if a in r]
            C = convs[r["conv"]]
            user_id = f"replay:bm25-v03:locomo:conv-{r['conv']}"
            idx = S.get_index(user_id)
            by_id = {row.id: row for row in idx.rows}
            rec = {"conv": r["conv"], "qi": r["qi"], "question": r["question"], "answer": r["answer"],
                   "gold": [{"dia": g, "speaker": C["speaker"].get(g), "date": C["date"].get(g), "text": C["text"].get(g)} for g in r["gold"]],
                   "unmapped_gold": r["unmapped_gold"]}
            for a in arms:
                ctx = []
                for k, it in enumerate(r[a]["final"], 1):
                    row = by_id.get(it["id"])
                    dia = C["seq_to_dia"].get(seq_of(it["id"]) or -1)
                    ctx.append({"rank": k, "id": it["id"], "dia": dia, "score": it.get("score"),
                                "content": S._render(idx, row) if row else None})
                rec[f"{a}_context"] = ctx
                rec[f"{a}_all@100"] = r[a]["all@100"]
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_out += 1
    pool.close()
    print(f"wrote {n_out} questions, arms {arms}, to {args.out}")


if __name__ == "__main__":
    main()
