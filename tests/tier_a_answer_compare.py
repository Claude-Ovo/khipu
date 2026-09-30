"""Tier A 答题对照（collab/诊断-事实与多跳-20260930/fusion-sim/REPORT.md §11）：
拿配对实验里保存的两臂最终上下文（dump_pair_contexts.py 的输出，线上 _render 渲染、原返回顺序），用官方 LoCoMo 答题模板答题、官方判卷模板判卷，
不重新检索、不重排。答题输入只有：题目 + 返回正文（按说话人分两栏，和 tests/replay_locomo.py --dump 一样）。官方模板没有提问日期这一项，LoCoMo10 也没有逐题日期，所以不放、不编。
提示词里没有标准答案、没有黄金证据标记、没有审计备注、没有新旧臂标签；判卷提示词只有题目、原始标准答案、生成答案。
每个答案判 3 次，逐次记录。旧臂再答一遍（old_repeat）只作为重复性检查。

每次调用都原样保存：请求体（去掉鉴权头）、响应 JSON、用量、finish_reason、耗时。累计花费超过 --max-cost 立刻停。
用法（服务器 /srv/aml/app2 下，先 set -a; . /srv/aml/.env; set +a）：
  python tests/tier_a_answer_compare.py --contexts /srv/aml/data/diag-locomo/fusion-pair-contexts.jsonl \\
      --questions "0,7;0,55;3,55;3,75;3,79;3,83;4,7;4,14;4,18;4,21;4,38;6,8;7,1;8,77;9,60" \\
      --out-dir /srv/aml/data/diag-locomo/tier-a --max-cost 1.0"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

PIPELINE_DIR = os.environ.get("LOCOMO_PIPELINE_DIR", "/srv/aml/pipelines/locomo-refined")
sys.path.insert(0, PIPELINE_DIR)
sys.path.insert(0, os.environ.get("AML_REPO_DIR", "/srv/aml/agent-memory-leaderboard"))   # pipeline.py 要 import 仓库根下的 api_config（无密钥也能 import）
from pipeline import ACCURACY_PROMPT, OPEN_ENDED_ANSWER_TEMPLATE, parse_judge_label, render_accuracy_prompt, render_answer_prompt  # noqa: E402

BASE_URL = os.environ.get("ANSWER_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
# 百炼 qwen-plus 标价（元 / 百万 token），只用来算停机线，不是账单
PRICE_IN = 0.8 / 1_000_000
PRICE_OUT = 2.0 / 1_000_000


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def split_memories(context: list[dict], sa: str, sb: str) -> tuple[list[str], list[str]]:
    """和 tests/replay_locomo.py --dump 同一规则：按渲染头里的说话人分两栏，认不出的归第一栏。"""
    mem_a, mem_b = [], []
    for it in context:
        c = it["content"]
        m = re.match(r"^\[[^\]]*\]\s+([^:]+):", c)
        (mem_b if m and m.group(1).strip() == sb else mem_a).append(c)
    return mem_a, mem_b


class Caller:
    def __init__(self, log_path: Path, max_cost: float):
        self.client = httpx.Client(timeout=120)
        self.log = open(log_path, "a", encoding="utf-8")
        self.max_cost = max_cost
        self.cost = 0.0
        self.tokens_in = self.tokens_out = 0
        self.calls = 0

    def chat(self, tag: dict, model: str, prompt: str, seed: int) -> dict:
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "seed": seed,
                "enable_thinking": False}
        t0 = time.monotonic()
        r = self.client.post(BASE_URL.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {API_KEY}"}, json=body)
        dt = time.monotonic() - t0
        r.raise_for_status()
        res = r.json()
        u = res.get("usage", {})
        self.tokens_in += u.get("prompt_tokens", 0); self.tokens_out += u.get("completion_tokens", 0)
        self.cost = self.tokens_in * PRICE_IN + self.tokens_out * PRICE_OUT
        self.calls += 1
        rec = {**tag, "model": model, "request": body, "response": res, "usage": u, "finish_reason": res["choices"][0].get("finish_reason"),
               "latency_s": round(dt, 3), "cum_cost_yuan": round(self.cost, 4)}
        self.log.write(json.dumps(rec, ensure_ascii=False) + "\n"); self.log.flush()
        if self.cost > self.max_cost:
            raise RuntimeError(f"cost cap ¥{self.max_cost} exceeded after {self.calls} calls (¥{self.cost:.4f})")
        return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contexts", required=True)
    ap.add_argument("--data", default="/srv/aml/data/locomo10.json")
    ap.add_argument("--questions", required=True, help='"conv,qi;conv,qi;..."')
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--answer-model", default="qwen-plus-2025-12-01")
    ap.add_argument("--judge-model", default="qwen-plus-2025-12-01")
    ap.add_argument("--judge-runs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20261001)
    ap.add_argument("--arms", default="old,new,old_repeat")
    ap.add_argument("--max-cost", type=float, default=1.0)
    args = ap.parse_args()
    assert API_KEY, "DASHSCOPE_API_KEY missing"

    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    want = [tuple(int(x) for x in f.split(",")) for f in args.questions.split(";") if f.strip()]
    data = json.load(open(args.data, encoding="utf-8"))
    ctx = {}
    for l in open(args.contexts, encoding="utf-8"):
        o = json.loads(l); ctx[(o["conv"], o["qi"])] = o

    config = {"answer_model": args.answer_model, "judge_model": args.judge_model, "temperature": 0, "seed": args.seed,
              "enable_thinking": False, "max_tokens": "not set (same as the official pipeline)", "judge_runs": args.judge_runs,
              "judge_seeds": [args.seed + i for i in range(args.judge_runs)], "arms": args.arms.split(","),
              "answer_template_sha256_16": sha(OPEN_ENDED_ANSWER_TEMPLATE), "judge_template_sha256_16": sha(ACCURACY_PROMPT),
              "pipeline_dir": PIPELINE_DIR, "base_url": BASE_URL, "price_assumption_yuan_per_M": {"in": 0.8, "out": 2.0},
              "question_date": "not available in locomo10.json and not a field of the official answer template; omitted",
              "contexts_file": args.contexts, "questions": want, "started": time.strftime("%Y-%m-%d %H:%M:%S %z")}
    (out / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
    caller = Caller(out / "calls.jsonl", args.max_cost)
    results = []
    stopped = None
    try:
        for (cv, qi) in want:
            q = ctx[(cv, qi)]
            conv = data[cv]["conversation"]
            sa, sb = conv.get("speaker_a", "speaker 1"), conv.get("speaker_b", "speaker 2")
            rec = {"conv": cv, "qi": qi, "question": q["question"], "gold_answer": q["answer"], "arms": {}}
            for arm in args.arms.split(","):
                src = "old" if arm.startswith("old") else "new"
                mem_a, mem_b = split_memories(q[f"{src}_context"], sa, sb)
                item = {"question": q["question"], "speaker_1_name": sa, "speaker_1_memories": "\n".join(mem_a),
                        "speaker_2_name": sb, "speaker_2_memories": "\n".join(mem_b)}
                prompt = render_answer_prompt(item)
                a = caller.chat({"kind": "answer", "conv": cv, "qi": qi, "arm": arm}, args.answer_model, prompt, args.seed)
                generated = a["response"]["choices"][0]["message"]["content"].strip()
                judges = []
                for k in range(args.judge_runs):
                    jp = render_accuracy_prompt({"question": q["question"], "gold_answer": q["answer"]}, generated)
                    j = caller.chat({"kind": "judge", "conv": cv, "qi": qi, "arm": arm, "run": k + 1}, args.judge_model, jp, args.seed + k)
                    text = j["response"]["choices"][0]["message"]["content"].strip()
                    try:
                        label = parse_judge_label(text)
                    except Exception as e:  # noqa: BLE001
                        label = f"UNPARSED({e})"
                    judges.append({"run": k + 1, "label": label, "finish_reason": j["finish_reason"], "response": text})
                votes = [j["label"] for j in judges]
                rec["arms"][arm] = {"answer": generated, "finish_reason": a["finish_reason"], "answer_prompt_sha": sha(prompt),
                                    "prompt_tokens": a["usage"].get("prompt_tokens"), "completion_tokens": a["usage"].get("completion_tokens"),
                                    "judges": judges, "correct_votes": votes.count("CORRECT"),
                                    "majority": "CORRECT" if votes.count("CORRECT") * 2 > len(votes) else "WRONG",
                                    "unanimous": len(set(votes)) == 1}
                print(f"conv{cv} q{qi} {arm:10s} finish={a['finish_reason']} votes={votes} | {generated[:80]!r}  cost=¥{caller.cost:.4f}", flush=True)
            results.append(rec)
    except RuntimeError as e:
        stopped = str(e); print("STOP:", stopped, flush=True)
    summary = {"questions": len(results), "calls": caller.calls, "tokens_in": caller.tokens_in, "tokens_out": caller.tokens_out,
               "cost_yuan": round(caller.cost, 4), "stopped": stopped, "finished": time.strftime("%Y-%m-%d %H:%M:%S %z")}
    (out / "results.json").write_text(json.dumps({"config": config, "summary": summary, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
