"""本地答题闭环：拿 replay 脚本 --dump 出来的 JSONL，跑官方 agent-memory-leaderboard 的 answer + evaluate，算正确率。
只用官方代码答题判分，我们不写 prompt。模型和地址走环境变量（和官方 api_config.py 一样）：
  ANSWER_API_BASE / ANSWER_API_KEY / ANSWER_MODEL / JUDGE_API_BASE / JUDGE_API_KEY / JUDGE_MODEL
用法：python tests/run_official_eval.py --input dump.jsonl --pipeline /srv/aml/agent-memory-leaderboard/data/longmemeval-s/pipeline.py --tag x"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def rows(p: Path) -> list[dict]:
    # 只按换行符切，不用 splitlines：正文里可能带 U+2028 一类的 Unicode 行分隔符，splitlines 会把一行切断
    return [json.loads(l) for l in p.read_text(encoding="utf-8").split(chr(10)) if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--pipeline", required=True)
    ap.add_argument("--tag", default="eval")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    for k in ("ANSWER_API_BASE", "ANSWER_API_KEY", "ANSWER_MODEL", "JUDGE_API_BASE", "JUDGE_API_KEY", "JUDGE_MODEL"):
        if not os.environ.get(k):
            sys.exit(f"missing env {k}")

    src = Path(args.input)
    items = rows(src)
    if args.limit:
        items = items[: args.limit]
    work = Path(f"eval-{args.tag}")
    work.mkdir(exist_ok=True)
    inp = work / "input.jsonl"
    # 写给官方管线的输入用纯 ASCII 转义：它用 splitlines 读，裸的 U+2028 会把一行切断
    inp.write_text("".join(json.dumps(r) + "\n" for r in items), encoding="utf-8")
    answers = work / "answers.jsonl"
    results = work / "results.jsonl"

    py = sys.executable
    subprocess.run([py, args.pipeline, "answer", "--input", str(inp), "--output", str(answers)], check=True)
    subprocess.run([py, args.pipeline, "evaluate", "--input", str(inp), "--answers", str(answers), "--output", str(results)], check=True)

    by_id = {r["id"]: r for r in items}
    res = rows(results)
    # 审查 #4：答案和判分的 id 必须与输入一一对应，缺题、重复都不许静默出摘要
    ans_ids = [r["id"] for r in rows(answers)]
    res_ids = [r["id"] for r in res]
    for name, ids in (("answers", ans_ids), ("results", res_ids)):
        if len(ids) != len(set(ids)) or set(ids) != set(by_id):
            sys.exit(f"{name} ids mismatch: {len(ids)} rows, {len(set(ids))} unique, input {len(by_id)}; "
                     f"missing {sorted(set(by_id) - set(ids))[:5]} extra {sorted(set(ids) - set(by_id))[:5]}")
    def score_of(r: dict) -> float:
        # LongMemEval/LoCoMo/PersonaMem 给 is_correct（0/1）；BEAM 给 llm_judge_score（rubric 均分 0~1）；CL-Bench 给 rubric_clbench_score
        for k in ("is_correct", "llm_judge_score", "rubric_clbench_score", "score"):
            if k in r and r[k] is not None:
                return float(r[k])
        raise KeyError(f"no score field in result {r.get('id')}: {sorted(r)}")
    total = sum(score_of(r) for r in res) / max(1, len(res))
    cat: dict[str, list[float]] = defaultdict(list)
    for r in res:
        k = by_id.get(r["id"], {}).get("category") or by_id.get(r["id"], {}).get("question_type") or "all"
        cat[str(k)].append(score_of(r))
    summary = {"tag": args.tag, "questions": len(res), "accuracy": round(total, 4),
               "by_category": {k: {"n": len(v), "acc": round(sum(v) / len(v), 4)} for k, v in sorted(cat.items())},
               "answer_model": os.environ["ANSWER_MODEL"], "judge_model": os.environ["JUDGE_MODEL"]}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    (work / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
