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
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


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
    inp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in items), encoding="utf-8")
    answers = work / "answers.jsonl"
    results = work / "results.jsonl"

    py = sys.executable
    subprocess.run([py, args.pipeline, "answer", "--input", str(inp), "--output", str(answers)], check=True)
    subprocess.run([py, args.pipeline, "evaluate", "--input", str(inp), "--answers", str(answers), "--output", str(results)], check=True)

    by_id = {r["id"]: r for r in items}
    res = rows(results)
    total = sum(r["is_correct"] for r in res) / max(1, len(res))
    cat: dict[str, list[int]] = defaultdict(list)
    for r in res:
        k = by_id.get(r["id"], {}).get("category") or by_id.get(r["id"], {}).get("question_type") or "all"
        cat[str(k)].append(int(r["is_correct"]))
    summary = {"tag": args.tag, "questions": len(res), "accuracy": round(total, 4),
               "by_category": {k: {"n": len(v), "acc": round(sum(v) / len(v), 4)} for k, v in sorted(cat.items())},
               "answer_model": os.environ["ANSWER_MODEL"], "judge_model": os.environ["JUDGE_MODEL"]}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    (work / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
