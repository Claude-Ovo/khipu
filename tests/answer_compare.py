"""答题对照（通用版，接 lme_context_dump.py / dump_pair_contexts.py 的上下文文件）：同一批题、若干个「臂」（每臂一份已保存的返回上下文），
用官方答题模板答题、官方判卷模板判 3 次，逐题分四类标：没找到 / 找到了但没交给模型 / 交给模型却答错 / 评分有争议。
不重新检索。答题输入只有题目 + 返回正文（按说话人分两栏）；判卷只有题目 + 原始标准答案 + 生成答案。提示词里没有 gold、证据标记、审计备注、臂标签。

比 tier_a_answer_compare.py 多做的（Codex 10-01 审查的五条）：出错的调用也记日志；费用在发起调用前预检；每题每臂答完立刻落盘；
正文为空的行跳过并记数；判卷解析失败不算票、单独标出。

用法（服务器 /srv/aml/app2 下，先 set -a; . /srv/aml/.env; set +a）：
  python tests/answer_compare.py --dataset lme --arm old=/srv/aml/data/a4/contexts-old.jsonl --arm new=/srv/aml/data/a4/contexts-new.jsonl \\
      --repeat old --out-dir /srv/aml/data/a4/answers --max-cost 5
--repeat old 表示旧臂再答一遍（old_repeat），只作为重复性观察。"""
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

DATASETS = {
    "lme": {"pipeline": "/srv/aml/pipelines/longmemeval-s", "speakers": ("user", "assistant")},
    "locomo": {"pipeline": "/srv/aml/pipelines/locomo-refined", "speakers": None},   # LoCoMo 的说话人从数据集里取，见 --speakers
}
AML_REPO_DIR = os.environ.get("AML_REPO_DIR", "/srv/aml/agent-memory-leaderboard")
BASE_URL = os.environ.get("ANSWER_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
PRICE_IN = 0.8 / 1_000_000     # 百炼 qwen-plus 标价（元/百万 token），只用来算停机线
PRICE_OUT = 2.0 / 1_000_000
EST_OUT_TOKENS = 300           # 预检时按这个数估下一次调用的输出


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def split_memories(lines: list[str], sa: str, sb: str) -> tuple[list[str], list[str]]:
    """和 tests/replay_locomo.py / replay_longmemeval.py --dump 同一规则：按渲染头里的说话人分两栏，认不出的归第一栏。"""
    mem_a, mem_b = [], []
    for c in lines:
        m = re.match(r"^\[[^\]]*\]\s+([^:]+):", c)
        (mem_b if m and m.group(1).strip() == sb else mem_a).append(c)
    return mem_a, mem_b


class Caller:
    def __init__(self, log_path: Path, max_cost: float):
        self.client = httpx.Client(timeout=120)
        self.log = open(log_path, "a", encoding="utf-8")
        self.max_cost = max_cost
        self.tokens_in = self.tokens_out = 0
        self.calls = self.errors = 0

    @property
    def cost(self) -> float:
        return self.tokens_in * PRICE_IN + self.tokens_out * PRICE_OUT

    def precheck(self, prompt_chars: int) -> None:
        est = self.cost + (prompt_chars / 3.5) * PRICE_IN + EST_OUT_TOKENS * PRICE_OUT
        if est > self.max_cost:
            raise RuntimeError(f"cost cap ¥{self.max_cost}: spent ¥{self.cost:.4f}, next call would reach ¥{est:.4f}")

    def chat(self, tag: dict, model: str, prompt: str, seed: int) -> dict:
        self.precheck(len(prompt))
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "seed": seed, "enable_thinking": False}
        rec = {**tag, "model": model, "request": body}
        t0 = time.monotonic()
        try:
            r = self.client.post(BASE_URL.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {API_KEY}"}, json=body)
            rec["http_status"] = r.status_code
            res = r.json()
            rec["response"] = res
            if r.status_code != 200 or not res.get("choices"):
                raise RuntimeError(f"bad response: status {r.status_code}, keys {list(res)[:5]}")
            u = res.get("usage") or {}
            rec["usage"] = u
            rec["finish_reason"] = res["choices"][0].get("finish_reason")
            rec["text"] = (res["choices"][0].get("message") or {}).get("content", "").strip()
            self.tokens_in += u.get("prompt_tokens", 0) or 0
            self.tokens_out += u.get("completion_tokens", 0) or 0
            rec["ok"] = True
        except Exception as e:  # noqa: BLE001
            rec["ok"] = False
            rec["error"] = f"{type(e).__name__}: {e}"
            self.errors += 1
        rec["latency_s"] = round(time.monotonic() - t0, 3)
        rec["cum_cost_yuan"] = round(self.cost, 4)
        self.calls += 1
        self.log.write(json.dumps(rec, ensure_ascii=False) + "\n"); self.log.flush()
        return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=tuple(DATASETS), required=True)
    ap.add_argument("--arm", action="append", required=True, help="name=path，可多次；臂名只进本地结果，不进任何提示词")
    ap.add_argument("--repeat", default="", help="把这个臂再答一遍，记作 <name>_repeat")
    ap.add_argument("--only", default="", help="只跑这些 qid（逗号分隔）")
    ap.add_argument("--speakers", default="", help="locomo 用：speaker_a,speaker_b；lme 固定 user,assistant")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--answer-model", default="qwen-plus-2025-12-01")
    ap.add_argument("--judge-model", default="qwen-plus-2025-12-01")
    ap.add_argument("--judge-runs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20261002)
    ap.add_argument("--max-cost", type=float, default=5.0)
    args = ap.parse_args()
    assert API_KEY, "DASHSCOPE_API_KEY missing"

    ds = DATASETS[args.dataset]
    sys.path.insert(0, ds["pipeline"]); sys.path.insert(0, AML_REPO_DIR)
    from pipeline import ACCURACY_PROMPT, OPEN_ENDED_ANSWER_TEMPLATE, parse_judge_label, render_accuracy_prompt, render_answer_prompt  # noqa: E402

    arms: dict[str, dict] = {}
    for spec in args.arm:
        name, path = spec.split("=", 1)
        arms[name] = {}
        for l in open(path, encoding="utf-8"):
            o = json.loads(l)
            if "error" in o:
                continue
            arms[name][o["qid"]] = o
    names = list(arms)
    qids = [q for q in arms[names[0]] if all(q in arms[a] for a in names)]
    if args.only:
        want = set(args.only.split(",")); qids = [q for q in qids if q in want]
    plan = names + ([f"{args.repeat}_repeat"] if args.repeat else [])
    sa, sb = ds["speakers"] or tuple(args.speakers.split(","))

    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    config = {"dataset": args.dataset, "arms": {n: args.arm[i].split("=", 1)[1] for i, n in enumerate(names)}, "repeat": args.repeat,
              "answer_model": args.answer_model, "judge_model": args.judge_model, "temperature": 0, "seed": args.seed,
              "judge_seeds": [args.seed + i for i in range(args.judge_runs)], "enable_thinking": False, "max_tokens": "not set (official pipeline)",
              "answer_template_sha256_16": sha(OPEN_ENDED_ANSWER_TEMPLATE), "judge_template_sha256_16": sha(ACCURACY_PROMPT),
              "pipeline_dir": ds["pipeline"], "speakers": [sa, sb], "questions": len(qids), "started": time.strftime("%Y-%m-%d %H:%M:%S %z"),
              "question_date": "present in the LME data but not a field of the official answer template; not supplied"}
    (out / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
    caller = Caller(out / "calls.jsonl", args.max_cost)
    results_path = out / "results.jsonl"
    done = set()
    if results_path.exists():
        for l in open(results_path, encoding="utf-8"):
            o = json.loads(l); done.add((o["qid"], o["arm"]))
    rf = open(results_path, "a", encoding="utf-8")
    stopped = None
    try:
        for qid in qids:
            for arm in plan:
                if (qid, arm) in done:
                    continue
                src = arms[arm.removesuffix("_repeat")][qid]
                lines = [r["content"] for r in src["returned"] if r.get("content")]
                empty = sum(1 for r in src["returned"] if not r.get("content"))
                mem_a, mem_b = split_memories(lines, sa, sb)
                item = {"question": src["question"], "speaker_1_name": sa, "speaker_1_memories": "\n".join(mem_a),
                        "speaker_2_name": sb, "speaker_2_memories": "\n".join(mem_b)}
                prompt = render_answer_prompt(item)
                a = caller.chat({"kind": "answer", "qid": qid, "arm": arm}, args.answer_model, prompt, args.seed)
                rec = {"qid": qid, "arm": arm, "type": src.get("type"), "question": src["question"], "gold_answer": src["answer"],
                       "answer_prompt_sha": sha(prompt), "empty_lines_skipped": empty, "answer_ok": a["ok"],
                       "answer": a.get("text"), "finish_reason": a.get("finish_reason"), "prompt_tokens": (a.get("usage") or {}).get("prompt_tokens"),
                       "judges": []}
                if a["ok"]:
                    for k in range(args.judge_runs):
                        jp = render_accuracy_prompt({"question": src["question"], "gold_answer": src["answer"]}, rec["answer"])
                        j = caller.chat({"kind": "judge", "qid": qid, "arm": arm, "run": k + 1}, args.judge_model, jp, args.seed + k)
                        label = None
                        if j["ok"]:
                            try:
                                label = parse_judge_label(j["text"])
                            except Exception as e:  # noqa: BLE001
                                label = None
                                j["parse_error"] = f"{type(e).__name__}: {e}"
                        rec["judges"].append({"run": k + 1, "label": label, "ok": j["ok"], "finish_reason": j.get("finish_reason"),
                                              "response": j.get("text"), "error": j.get("error") or j.get("parse_error")})
                votes = [j["label"] for j in rec["judges"] if j["label"] in ("CORRECT", "WRONG")]
                rec["valid_votes"] = len(votes)
                rec["correct_votes"] = votes.count("CORRECT")
                rec["majority"] = ("CORRECT" if votes.count("CORRECT") * 2 > len(votes) else "WRONG") if votes else None
                rec["unanimous"] = len(set(votes)) == 1 if votes else None
                # 检索侧的标签（只看这臂的返回，不看答案）：黄金轮次有没有回来、答案会话有没有回来
                rec["gold_turn_returned"] = any(r.get("has_answer") for r in src["returned"])
                rec["gold_session_returned"] = any(r.get("session") in set(src.get("gold_sessions") or []) for r in src["returned"])
                rf.write(json.dumps(rec, ensure_ascii=False) + "\n"); rf.flush()
                print(f"{qid} {arm:11s} ok={a['ok']} votes={rec['correct_votes']}/{rec['valid_votes']} turn={int(rec['gold_turn_returned'])} "
                      f"| {(rec['answer'] or a.get('error') or '')[:70]!r}  ¥{caller.cost:.3f}", flush=True)
    except RuntimeError as e:
        stopped = str(e); print("STOP:", stopped, flush=True)
    finally:
        rf.close()
    summary = {"questions": len(qids), "arms": plan, "calls": caller.calls, "call_errors": caller.errors, "tokens_in": caller.tokens_in,
               "tokens_out": caller.tokens_out, "cost_yuan": round(caller.cost, 4), "stopped": stopped, "finished": time.strftime("%Y-%m-%d %H:%M:%S %z")}
    (out / "summary.json").write_text(json.dumps({"config": config, "summary": summary}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
