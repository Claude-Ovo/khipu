"""A4 回归对照的第一步：对 LongMemEval-S 前 N 题，进程内跑一遍 Search（重排开，和 Full 同配置），把平台会拿到的返回原样落盘。
同一个脚本在两份代码下各跑一次（/srv/aml/app-old = 第一次 Full 的 1be823e；/srv/aml/app2 = second-shot），库都用 aml2（LME 段 9-27 写入、10-01 拷入，逐行相同）。
不依赖 trace 参数（老代码没有），只用 Search 的返回；黄金证据按 tests/replay_longmemeval.py 同一种摊平（空消息跳过，seq 从 1 起）。

用法（服务器，在对应代码目录下，先 set -a; . /srv/aml/.env; . /srv/aml/.env2; set +a）：
  cd /srv/aml/app-old && python tests/lme_context_dump.py --out /srv/aml/data/a4/contexts-old.jsonl --limit 200
  cd /srv/aml/app2    && python tests/lme_context_dump.py --out /srv/aml/data/a4/contexts-new.jsonl --limit 200
每题一行：qid、type、question、question_date、answer、gold（has_answer 的 seq）、gold_sessions、returned（rank/id/seq/content/tokens）、命中指标、累计花费。"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, search as S  # noqa: E402
from app.db import pool  # noqa: E402
try:   # 第一次 Full 的代码（1be823e）还没有 httpclient / usage 计数，老臂的花费只能用新臂的 token 数估
    from app.httpclient import aclose, usage  # noqa: E402
except ImportError:  # pragma: no cover
    aclose = None
    usage = None
from app.textutil import count_tokens  # noqa: E402

RERANK_YUAN_PER_TOKEN = 0.8 / 1_000_000
EMBED_YUAN_PER_TOKEN = 0.5 / 1_000_000
LME_TAG = "lme-s-vec-v031"
KS = (10, 20, 50, 100)


def seq_of(item_id: str) -> int | None:
    m = re.search(r"#(\d+)\.\d+$", item_id)
    return int(m.group(1)) if m else None


def flatten(q: dict) -> list[tuple[str, bool]]:
    meta = []
    for sid, turns in zip(q["haystack_session_ids"], q["haystack_sessions"]):
        for t in turns:
            if not str(t.get("content", "")).strip():
                continue
            meta.append((sid, bool(t.get("has_answer", False))))
    return meta


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/srv/aml/data/longmemeval_s.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--max-cost", type=float, default=3.0, help="本次运行重排 + 查询向量累计花费上限（元）")
    args = ap.parse_args()

    data = json.load(open(args.data, encoding="utf-8"))[: args.limit]
    todo = data[args.offset:]
    commit = (Path(__file__).resolve().parents[1] / "COMMIT")
    commit = commit.read_text().strip() if commit.exists() else "unknown"
    print(f"code {commit[:7]} rerank={config.RERANK_ENABLED} fusion={getattr(config, 'FUSION_RULE', 'n/a')} "
          f"RERANK_TOPN={config.RERANK_TOPN} top_k={args.top_k}; {len(todo)} questions from offset {args.offset}", flush=True)
    pool.open()
    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    stopped = None
    with open(out, "a" if args.offset else "w", encoding="utf-8") as f:
        for n, q in enumerate(todo, start=args.offset + 1):
            meta = flatten(q)
            user_id = f"replay:{LME_TAG}:lme:{q['question_id']}"
            t0 = time.monotonic()
            # 预检：花费已到上限就不再发起新的调用
            u = usage.snapshot() if usage else {"rerank": {"tokens": 0, "failed": 0}, "embed": {"tokens": 0}}
            cost = u["rerank"]["tokens"] * RERANK_YUAN_PER_TOKEN + u["embed"]["tokens"] * EMBED_YUAN_PER_TOKEN
            if usage and cost >= args.max_cost:
                stopped = f"cost cap ¥{args.max_cost} reached before question {n}"
                print("STOP:", stopped, flush=True)
                break
            try:
                items = await S.search(user_id, q["question"], None, args.top_k)
            except Exception as e:  # noqa: BLE001
                f.write(json.dumps({"n": n, "qid": q["question_id"], "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n")
                f.flush()
                print(f"[{n}] {q['question_id']} ERROR {type(e).__name__}: {e}", flush=True)
                continue
            dt = time.monotonic() - t0
            returned = []
            for k, it in enumerate(items, 1):
                s = seq_of(it["id"])
                sid, has = meta[s - 1] if s and 1 <= s <= len(meta) else ("", False)
                returned.append({"rank": k, "id": it["id"], "seq": s, "session": sid, "has_answer": has,
                                 "score": it.get("score"), "tokens": count_tokens(it["content"]), "content": it["content"]})
            gold_idx = [i + 1 for i, (_, h) in enumerate(meta) if h]
            ans_sessions = sorted(set(q.get("answer_session_ids") or []))
            rec = {"n": n, "qid": q["question_id"], "type": q.get("question_type"), "question": q["question"],
                   "question_date": q.get("question_date"), "answer": str(q.get("answer", "")),
                   "gold": gold_idx, "gold_sessions": ans_sessions, "n_msgs": len(meta), "latency_s": round(dt, 3),
                   "returned": returned, "total_tokens": sum(r["tokens"] for r in returned)}
            got_seq = [r["seq"] for r in returned]
            got_sess = [r["session"] for r in returned]
            for k in KS:
                rec[f"turn@{k}"] = int(any(r["has_answer"] for r in returned[:k]))
                rec[f"sess@{k}"] = int(any(s in ans_sessions for s in got_sess[:k])) if ans_sessions else 0
                rec[f"allturn@{k}"] = int(set(gold_idx) <= set(got_seq[:k])) if gold_idx else 0
                rec[f"allsess@{k}"] = int(set(ans_sessions) <= set(got_sess[:k])) if ans_sessions else 0
            u = usage.snapshot() if usage else {"rerank": {"tokens": 0, "failed": 0}, "embed": {"tokens": 0}}
            cost = u["rerank"]["tokens"] * RERANK_YUAN_PER_TOKEN + u["embed"]["tokens"] * EMBED_YUAN_PER_TOKEN
            rec["cum_cost_yuan"] = round(cost, 4) if usage else None
            rec["rerank_calls_failed"] = u["rerank"].get("failed", 0)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if n % 20 == 0 or n == args.offset + 1:
                print(f"[{n}] {q.get('question_type')} turn@10={rec['turn@10']} allturn@100={rec['allturn@100']} "
                      f"tokens={rec['total_tokens']} rerank_tokens={u['rerank']['tokens']} cost=¥{cost:.3f}", flush=True)
    u = usage.snapshot() if usage else {"rerank": {"tokens": 0}, "embed": {"tokens": 0}, "note": "no usage counters in this code version"}
    summary = {"code": commit, "questions": len(todo), "offset": args.offset, "stopped": stopped, "usage": u,
               "cost_yuan": round(u["rerank"]["tokens"] * RERANK_YUAN_PER_TOKEN + u["embed"]["tokens"] * EMBED_YUAN_PER_TOKEN, 4),
               "config": {"RERANK_ENABLED": config.RERANK_ENABLED, "RERANK_MODEL": config.RERANK_MODEL, "RERANK_TOPN": config.RERANK_TOPN,
                          "RERANK_MIX": config.RERANK_MIX, "BUDGET_TOKENS": config.BUDGET_TOKENS, "top_k": args.top_k,
                          "FUSION_RULE": getattr(config, "FUSION_RULE", "n/a")},
               "finished": time.strftime("%Y-%m-%d %H:%M:%S %z")}
    Path(str(out) + f".summary-{args.offset}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("code", "questions", "stopped", "cost_yuan")}, ensure_ascii=False))
    if aclose:
        await aclose()
    pool.close()


if __name__ == "__main__":
    asyncio.run(main())
