"""答题对照（通用版，接 lme_context_dump.py / dump_pair_contexts.py 的上下文文件）：同一批题、若干个「臂」（每臂一份已保存的返回上下文），
用官方答题模板答题、官方判卷模板判 3 次，逐题分开标：检索侧（黄金轮没返回 / 返回了但正文为空没交给模型 / 交给了模型）和答案侧（对 / 错 / 无多数 / 多数但有异议）。
不重新检索。答题输入只有题目 + 返回正文（按说话人分两栏）；判卷只有题目 + 原始标准答案 + 生成答案。提示词里没有 gold、证据标记、审计备注、臂标签。

Codex 10-02 审查后的改法：题集固定（各臂 qid、题目、gold 必须逐字一致，缺题报错不静默取交集）；续跑按 (qid, arm, 运行签名) 认已完成，
答案已有就只补缺的判卷，签名不同拒绝混跑；费用上限从已有 calls.jsonl 累计、调用前按保守估计预检、usage 缺失按保守估计计费；
平票 / 无多数不判 WRONG；空白行跳过并记数；判卷解析失败不算票并标出。

用法（服务器 /srv/aml/app2 下，先 set -a; . /srv/aml/.env; set +a）：
  python tests/answer_compare.py --dataset lme --arm old=/srv/aml/data/a4/contexts-old.jsonl --arm new=/srv/aml/data/a4/contexts-new.jsonl \\
      --out-dir /srv/aml/data/a4/answers --max-cost 6
--repeat old 可选：旧臂再答一遍（old_repeat），只作为重复性观察。"""
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
    "locomo": {"pipeline": "/srv/aml/pipelines/locomo-refined", "speakers": None},   # LoCoMo 的说话人用 --speakers 传
}
AML_REPO_DIR = os.environ.get("AML_REPO_DIR", "/srv/aml/agent-memory-leaderboard")
BASE_URL = os.environ.get("ANSWER_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
PRICE_IN = 0.8 / 1_000_000     # 百炼 qwen-plus 标价（元/百万 token），只用来算停机线，账单以控制台为准
PRICE_OUT = 2.0 / 1_000_000
EST_CHARS_PER_TOKEN = 3.0      # 预检用的保守换算（英文正文实测约 3.5–4）
EST_OUT_TOKENS = 1024          # 预检按这个输出量算；官方模板不设 max_tokens，这里也不设


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def file_sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


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
        self.cost = 0.0
        self.calls = self.errors = self.usage_missing = 0
        if log_path.exists():   # 续跑：把之前已经花掉的钱接上
            for l in open(log_path, encoding="utf-8"):
                try:
                    self.cost = max(self.cost, float(json.loads(l).get("cum_cost_yuan") or 0.0))
                except Exception:  # noqa: BLE001
                    pass
        self.log = open(log_path, "a", encoding="utf-8")
        self.max_cost = max_cost

    def precheck(self, prompt_chars: int) -> None:
        est = self.cost + (prompt_chars / EST_CHARS_PER_TOKEN) * PRICE_IN + EST_OUT_TOKENS * PRICE_OUT
        if est > self.max_cost:
            raise RuntimeError(f"cost cap ¥{self.max_cost}: spent ¥{self.cost:.4f}, next call could reach ¥{est:.4f}")

    def chat(self, tag: dict, model: str, prompt: str, seed: int) -> dict:
        self.precheck(len(prompt))
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "seed": seed, "enable_thinking": False}
        rec = {**tag, "model": model, "request": body}
        t0 = time.monotonic()
        charged_in = charged_out = 0
        try:
            r = self.client.post(BASE_URL.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {API_KEY}"}, json=body)
            rec["http_status"] = r.status_code
            try:
                res = r.json()
            except Exception as e:  # noqa: BLE001
                res = {"raw_text": r.text[:2000], "json_error": f"{type(e).__name__}: {e}"}
            rec["response"] = res
            u = res.get("usage") or {}
            if u.get("prompt_tokens") is not None:
                charged_in, charged_out = u.get("prompt_tokens", 0) or 0, u.get("completion_tokens", 0) or 0
            else:   # 没有 usage（出错或供应商没返回）：按保守估计计费，不漏算
                charged_in, charged_out = int(len(prompt) / EST_CHARS_PER_TOKEN), EST_OUT_TOKENS
                self.usage_missing += 1
                rec["usage_estimated"] = True
            if r.status_code != 200 or not res.get("choices"):
                raise RuntimeError(f"bad response: status {r.status_code}, keys {list(res)[:5]}")
            rec["usage"] = u
            rec["finish_reason"] = res["choices"][0].get("finish_reason")
            rec["text"] = ((res["choices"][0].get("message") or {}).get("content") or "").strip()
            rec["ok"] = True
        except Exception as e:  # noqa: BLE001
            rec["ok"] = False
            rec["error"] = f"{type(e).__name__}: {e}"
            self.errors += 1
            if not charged_in:   # 连 usage 都没拿到的失败也按保守估计计费
                charged_in, charged_out = int(len(prompt) / EST_CHARS_PER_TOKEN), EST_OUT_TOKENS
        self.cost += charged_in * PRICE_IN + charged_out * PRICE_OUT
        rec["latency_s"] = round(time.monotonic() - t0, 3)
        rec["cum_cost_yuan"] = round(self.cost, 4)
        self.calls += 1
        self.log.write(json.dumps(rec, ensure_ascii=False) + "\n"); self.log.flush()
        return rec


def load_arm(path: str) -> tuple[dict[str, dict], list[dict]]:
    rows, errors = {}, []
    for l in open(path, encoding="utf-8"):
        o = json.loads(l)
        if "error" in o:
            errors.append(o); continue
        if o["qid"] in rows:
            raise SystemExit(f"duplicate qid {o['qid']} in {path}")
        rows[o["qid"]] = o
    return rows, errors


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
    ap.add_argument("--allow-missing", action="store_true", help="某臂检索出错的题跳过并记录（默认：有缺题就拒绝开跑）")
    args = ap.parse_args()
    assert API_KEY, "DASHSCOPE_API_KEY missing"

    ds = DATASETS[args.dataset]
    sys.path.insert(0, ds["pipeline"]); sys.path.insert(0, AML_REPO_DIR)
    from pipeline import ACCURACY_PROMPT, OPEN_ENDED_ANSWER_TEMPLATE, parse_judge_label, render_accuracy_prompt, render_answer_prompt  # noqa: E402

    # 题集固定：各臂 qid 顺序、题目、gold 逐字一致；检索出错的题默认拒绝开跑
    arms: dict[str, dict[str, dict]] = {}
    arm_files: dict[str, str] = {}
    missing: list[dict] = []
    for spec in args.arm:
        name, path = spec.split("=", 1)
        arms[name], errs = load_arm(path)
        arm_files[name] = path
        missing += [{"arm": name, **e} for e in errs]
    names = list(arms)
    qids = list(arms[names[0]])
    for name in names[1:]:
        if list(arms[name]) != qids:
            diff = sorted(set(qids) ^ set(arms[name]))
            if not args.allow_missing:
                raise SystemExit(f"question sets differ between {names[0]} and {name}: {diff[:10]} (use --allow-missing to drop them, they will be listed)")
            qids = [q for q in qids if q in arms[name]]
    for q in qids:
        base = arms[names[0]][q]
        for name in names[1:]:
            o = arms[name][q]
            if o["question"] != base["question"] or o["answer"] != base["answer"]:
                raise SystemExit(f"question/gold mismatch for {q} between {names[0]} and {name}")
    if missing and not args.allow_missing:
        raise SystemExit(f"{len(missing)} retrieval errors in the context files; fix or pass --allow-missing: {missing[:3]}")
    if args.only:
        want = set(args.only.split(",")); qids = [q for q in qids if q in want]
    plan = names + ([f"{args.repeat}_repeat"] if args.repeat else [])
    sa, sb = ds["speakers"] or tuple(args.speakers.split(","))

    run_sig = sha(json.dumps({"dataset": args.dataset, "answer_model": args.answer_model, "judge_model": args.judge_model, "seed": args.seed,
                              "judge_runs": args.judge_runs, "answer_t": sha(OPEN_ENDED_ANSWER_TEMPLATE), "judge_t": sha(ACCURACY_PROMPT),
                              "arms": {n: file_sha(p) for n, p in arm_files.items()}, "speakers": [sa, sb]}, sort_keys=True))
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    config = {"run_sig": run_sig, "dataset": args.dataset, "arms": arm_files, "arm_file_sha256_16": {n: file_sha(p) for n, p in arm_files.items()},
              "repeat": args.repeat, "plan": plan, "answer_model": args.answer_model, "judge_model": args.judge_model, "temperature": 0,
              "seed": args.seed, "judge_seeds": [args.seed + i for i in range(args.judge_runs)], "enable_thinking": False,
              "max_tokens": "not set (official pipeline)", "answer_template_sha256_16": sha(OPEN_ENDED_ANSWER_TEMPLATE),
              "judge_template_sha256_16": sha(ACCURACY_PROMPT), "pipeline_dir": ds["pipeline"], "speakers": [sa, sb],
              "questions": len(qids), "excluded_for_retrieval_error": missing, "price_assumption_yuan_per_M": {"in": 0.8, "out": 2.0},
              "question_date": "present in the LME data but not a field of the official answer template; not supplied",
              "started": time.strftime("%Y-%m-%d %H:%M:%S %z")}
    cfg_path = out / "config.json"
    if cfg_path.exists():
        old = json.loads(cfg_path.read_text(encoding="utf-8"))
        if old.get("run_sig") != run_sig:
            raise SystemExit(f"{cfg_path} belongs to a different run (sig {old.get('run_sig')} != {run_sig}); use another --out-dir")
    else:
        cfg_path.write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")

    # 续跑：读已有结果，只有签名相同的才算
    results_path = out / "results.jsonl"
    have: dict[tuple[str, str], dict] = {}
    if results_path.exists():
        for l in open(results_path, encoding="utf-8"):
            o = json.loads(l)
            if o.get("run_sig") == run_sig:
                have[(o["qid"], o["arm"])] = o
    caller = Caller(out / "calls.jsonl", args.max_cost)
    rf = open(results_path, "a", encoding="utf-8")

    def write(rec: dict) -> None:
        rf.write(json.dumps(rec, ensure_ascii=False) + "\n"); rf.flush()
        have[(rec["qid"], rec["arm"])] = rec

    def finalize(rec: dict, src: dict) -> None:
        votes = [j["label"] for j in rec["judges"] if j.get("label") in ("CORRECT", "WRONG")]
        c, w = votes.count("CORRECT"), votes.count("WRONG")
        need = args.judge_runs // 2 + 1
        rec["valid_votes"] = len(votes); rec["correct_votes"] = c; rec["wrong_votes"] = w
        rec["majority"] = "CORRECT" if c >= need else ("WRONG" if w >= need else None)
        rec["unanimous"] = len(votes) == args.judge_runs and (c == args.judge_runs or w == args.judge_runs)
        rec["complete"] = bool(rec["answer_ok"]) and len(rec["judges"]) == args.judge_runs
        # 检索侧：黄金轮有没有回来、回来的那几段正文是不是真交给了模型（空白的不算）
        gold_rows = [r for r in src["returned"] if r.get("has_answer_turn") or r.get("has_answer")]
        handed_rows = [r for r in gold_rows if (r.get("content") or "").strip()]
        rec["gold_turn_returned"] = bool(gold_rows)
        rec["gold_rows_returned"] = len(gold_rows); rec["gold_rows_handed"] = len(handed_rows)
        rec["gold_session_returned"] = any(r.get("session") in set(src.get("gold_sessions") or []) for r in src["returned"])
        rec["retrieval_label"] = "not_found" if not gold_rows else ("found_not_handed" if not handed_rows else "handed")
        if not rec["answer_ok"]:
            rec["answer_label"] = "no_answer"
        elif rec["majority"] is None:
            rec["answer_label"] = "disputed_no_majority"
        elif not rec["unanimous"]:
            rec["answer_label"] = f"{rec['majority'].lower()}_with_dissent"
        else:
            rec["answer_label"] = rec["majority"].lower()

    stopped = None
    try:
        for qid in qids:
            for arm in plan:
                src = arms[arm.removesuffix("_repeat")][qid]
                rec = have.get((qid, arm))
                if rec and rec.get("complete"):
                    continue
                if rec is None:
                    lines = [r["content"] for r in src["returned"] if (r.get("content") or "").strip()]
                    blank = sum(1 for r in src["returned"] if not (r.get("content") or "").strip())
                    mem_a, mem_b = split_memories(lines, sa, sb)
                    item = {"question": src["question"], "speaker_1_name": sa, "speaker_1_memories": "\n".join(mem_a),
                            "speaker_2_name": sb, "speaker_2_memories": "\n".join(mem_b)}
                    prompt = render_answer_prompt(item)
                    a = caller.chat({"kind": "answer", "qid": qid, "arm": arm}, args.answer_model, prompt, args.seed)
                    rec = {"run_sig": run_sig, "qid": qid, "arm": arm, "type": src.get("type"), "question": src["question"],
                           "gold_answer": src["answer"], "answer_prompt_sha": sha(prompt), "blank_lines_skipped": blank,
                           "answer_ok": a["ok"], "answer": a.get("text"), "answer_error": a.get("error"), "finish_reason": a.get("finish_reason"),
                           "prompt_tokens": (a.get("usage") or {}).get("prompt_tokens"), "judges": []}
                    finalize(rec, src); write(rec)   # 答完先落盘；判卷逐轮补
                if rec["answer_ok"]:
                    done_runs = {j["run"] for j in rec["judges"]}
                    for k in range(args.judge_runs):
                        if k + 1 in done_runs:
                            continue
                        jp = render_accuracy_prompt({"question": src["question"], "gold_answer": src["answer"]}, rec["answer"])
                        j = caller.chat({"kind": "judge", "qid": qid, "arm": arm, "run": k + 1}, args.judge_model, jp, args.seed + k)
                        label, err = None, j.get("error")
                        if j["ok"]:
                            try:
                                label = parse_judge_label(j["text"])
                                if label not in ("CORRECT", "WRONG"):
                                    label, err = None, f"invalid label {label!r}"
                            except Exception as e:  # noqa: BLE001
                                label, err = None, f"{type(e).__name__}: {e}"
                        rec["judges"].append({"run": k + 1, "label": label, "ok": j["ok"], "finish_reason": j.get("finish_reason"),
                                              "response": j.get("text"), "error": err})
                        finalize(rec, src); write(rec)   # 每轮判卷后都落盘，中途停了不丢
                print(f"{qid} {arm:11s} ok={rec['answer_ok']} votes={rec['correct_votes']}/{rec['valid_votes']} {rec['retrieval_label']:16s} "
                      f"{rec['answer_label']:22s} | {(rec['answer'] or rec.get('answer_error') or '')[:60]!r}  ¥{caller.cost:.3f}", flush=True)
    except RuntimeError as e:
        stopped = str(e); print("STOP:", stopped, flush=True)
    finally:
        rf.close()
    final = [r for r in have.values() if r.get("run_sig") == run_sig]
    summary = {"run_sig": run_sig, "questions": len(qids), "plan": plan, "records": len(final), "complete": sum(1 for r in final if r.get("complete")),
               "calls_this_process": caller.calls, "call_errors": caller.errors, "usage_missing": caller.usage_missing,
               "cost_yuan_cumulative": round(caller.cost, 4), "stopped": stopped, "finished": time.strftime("%Y-%m-%d %H:%M:%S %z")}
    (out / "summary.json").write_text(json.dumps({"config": config, "summary": summary}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
