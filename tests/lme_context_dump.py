"""A4 回归对照的第一步：对 LongMemEval-S 前 N 题，进程内跑一遍 Search（重排开，和 Full 同配置），把平台会拿到的返回原样落盘。
同一个脚本在两份代码下各跑一次（/srv/aml/app-old = 第一次 Full 的 1be823e；/srv/aml/app2 = second-shot），库都用 aml2（LME 段 9-27 写入、10-01 拷入，逐行相同）。
不依赖 trace 参数（老代码没有），只用 Search 的返回；黄金证据按 tests/replay_longmemeval.py 同一种摊平（空消息跳过，seq 从 1 起）。

Codex 10-02 审查后加的：开跑前强制核对运行条件（重排开、库名、题数、top_k）并把实际生效的配置写进 summary（含数据库会话时区、超时值）；
老代码没有用量计数，老臂的花费记 null、按新臂估，且用 --max-questions 做硬上限；新臂每题记重排/向量的调用增量，看得出哪题降级；
返回的每一行记 part/total，黄金轮被切成几段时只命中一段不等于答案文字到手，交给 answer_compare 再判。

用法（服务器，在对应代码目录下，先 set -a; . /srv/aml/.env; . /srv/aml/.env2; set +a）：
  cd /srv/aml/app-old && python tests/lme_context_dump.py --out /srv/aml/data/a4/contexts-old.jsonl --limit 200
  cd /srv/aml/app2    && python tests/lme_context_dump.py --out /srv/aml/data/a4/contexts-new.jsonl --limit 200"""
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
from app.textutil import count_tokens  # noqa: E402

try:   # 第一次 Full 的代码（1be823e）还没有 httpclient / usage 计数，老臂的花费只能用新臂的 token 数估
    from app.httpclient import aclose, usage  # noqa: E402
except ImportError:  # pragma: no cover
    aclose = None
    usage = None

RERANK_YUAN_PER_TOKEN = 0.8 / 1_000_000
EMBED_YUAN_PER_TOKEN = 0.5 / 1_000_000
LME_TAG = "lme-s-vec-v031"
KS = (10, 20, 50, 100)
_ID = re.compile(r"#(\d+)\.(\d+)$")


def seq_part(item_id: str) -> tuple[int | None, int | None]:
    m = _ID.search(item_id)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def flatten(q: dict) -> list[tuple[str, bool]]:
    """与 tests/replay_longmemeval.py 78-86 行逐字同一种摊平：三组数组一起 zip，空内容跳过。"""
    meta = []
    for sid, _date, turns in zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]):
        for t in turns:
            if not str(t.get("content", "")).strip():
                continue
            meta.append((sid, bool(t.get("has_answer", False))))
    return meta


def usage_snapshot() -> dict:
    if usage is None:
        return {"rerank": {"calls": None, "ok": None, "failed": None, "tokens": None, "skipped": None, "timeouts": None},
                "embed": {"calls": None, "ok": None, "failed": None, "tokens": None}}
    return usage.snapshot()


def delta(a: dict, b: dict) -> dict:
    out = {}
    for grp in ("rerank", "embed"):
        out[grp] = {k: (None if a[grp].get(k) is None or b[grp].get(k) is None else b[grp][k] - a[grp][k])
                    for k in ("calls", "ok", "failed", "tokens", "skipped", "timeouts") if k in b[grp]}
    return out


def db_timezone() -> str:
    try:
        with pool.connection() as conn:
            return str(conn.execute("SHOW TimeZone").fetchone()[0])
    except Exception as e:  # noqa: BLE001
        return f"unknown ({type(e).__name__})"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/srv/aml/data/longmemeval_s.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--max-cost", type=float, default=7.0, help="重排 + 查询向量累计花费上限（元）；老代码量不到花费，只靠 --max-questions")
    ap.add_argument("--max-questions", type=int, default=200, help="硬上限：最多跑这么多题，和花费无关")
    # B3 对照（10-06）加的三个：别的实验库、别的用户前缀、按 qid 挑题（不按前 N 题）
    ap.add_argument("--required-db", default="aml2", help="只在这个库上跑；评测库 aml 永远拒绝")
    ap.add_argument("--user-prefix", default=f"replay:{LME_TAG}:lme:", help="user_id = 前缀 + question_id")
    ap.add_argument("--qids", default="", help="qid 列表文件（一行一个）；给了就只跑这些题（按数据集顺序），忽略 --limit/--offset")
    args = ap.parse_args()
    if args.max_questions < 1 or args.limit < 1:
        raise SystemExit("--limit and --max-questions must be >= 1")
    REQUIRED_DB = args.required_db
    if REQUIRED_DB == "aml":   # 评测库不准碰
        raise SystemExit("refusing to run against the evaluation database 'aml'")

    # 运行条件：不满足就不跑（Codex 审查第 4 条）
    db_name = config.DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    problems = []
    if not config.RERANK_ENABLED:
        problems.append("RERANK_ENABLED is off")
    if db_name != REQUIRED_DB:
        problems.append(f"database is {db_name!r}, expected {REQUIRED_DB!r}")
    if args.top_k != 100:
        problems.append(f"top_k is {args.top_k}, expected 100")
    if problems:
        print("REFUSING TO RUN: " + "; ".join(problems), flush=True)
        sys.exit(2)

    data = json.load(open(args.data, encoding="utf-8"))
    if args.qids:
        want = {ln.strip() for ln in open(args.qids, encoding="utf-8") if ln.strip()}
        todo = [q for q in data if q["question_id"] in want][: args.max_questions]
        missing = want - {q["question_id"] for q in todo}
        if missing:
            raise SystemExit(f"qids not in dataset or over --max-questions: {sorted(missing)[:5]}")
    else:
        todo = data[: args.limit][args.offset:][: args.max_questions]
    commit_file = Path(__file__).resolve().parents[1] / "COMMIT"
    commit = commit_file.read_text().strip() if commit_file.exists() else "unknown"
    pool.open()
    effective = {name: getattr(config, name, None) for name in (
        "RERANK_ENABLED", "RERANK_MODEL", "RERANK_TOPN", "RERANK_MIX", "RERANK_DOC_CHARS", "RERANK_TIMEOUT_S", "RERANK_ATTEMPT_TIMEOUT_S",
        "EMBED_MODEL", "EMBED_DIM", "EMBED_TIMEOUT_S", "EMBED_CONNECT_TIMEOUT_S", "SEARCH_TIMEOUT_S", "BUDGET_TOKENS", "HARD_TOP_K",
        "RRF_K", "CHANNEL_TOPN_MULT", "NEIGHBOR_CAP_RATIO", "HOP_ENABLED", "INDEX_CACHE_USERS", "FUSION_RULE")}
    effective["database"] = db_name
    effective["db_session_timezone"] = db_timezone()
    effective["usage_counters"] = usage is not None
    effective["NOTES_IN_SEARCH"] = getattr(config, "NOTES_IN_SEARCH", None)
    effective["user_prefix"] = args.user_prefix
    print(f"code {commit[:7]} db={db_name} tz={effective['db_session_timezone']} rerank={config.RERANK_ENABLED} "
          f"fusion={effective['FUSION_RULE']} RERANK_TOPN={config.RERANK_TOPN} top_k={args.top_k}; "
          f"{len(todo)} questions from offset {args.offset}; usage counters: {usage is not None}", flush=True)

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    stopped = None
    t_start = time.strftime("%Y-%m-%d %H:%M:%S %z")
    with open(out, "a" if args.offset else "w", encoding="utf-8") as f:
        for n, q in enumerate(todo, start=args.offset + 1):
            meta = flatten(q)
            user_id = f"{args.user_prefix}{q['question_id']}"
            before = usage_snapshot()
            if usage is not None:
                cost = before["rerank"]["tokens"] * RERANK_YUAN_PER_TOKEN + before["embed"]["tokens"] * EMBED_YUAN_PER_TOKEN
                if cost >= args.max_cost:
                    stopped = f"cost cap ¥{args.max_cost} reached before question {n}"
                    print("STOP:", stopped, flush=True)
                    break
            t0 = time.monotonic()
            try:
                items = await S.search(user_id, q["question"], None, args.top_k)
            except Exception as e:  # noqa: BLE001
                f.write(json.dumps({"n": n, "qid": q["question_id"], "error": f"{type(e).__name__}: {e}",
                                    "usage_delta": delta(before, usage_snapshot())}, ensure_ascii=False) + "\n")
                f.flush()
                print(f"[{n}] {q['question_id']} ERROR {type(e).__name__}: {e}", flush=True)
                continue
            dt = time.monotonic() - t0
            after = usage_snapshot()
            returned = []
            for k, it in enumerate(items, 1):
                s, part = seq_part(it["id"])
                sid, has = meta[s - 1] if s and 1 <= s <= len(meta) else ("", False)
                content = it.get("content") or ""
                returned.append({"rank": k, "id": it["id"], "seq": s, "part": part, "session": sid, "has_answer_turn": has,
                                 "score": it.get("score"), "tokens": count_tokens(content) if content else 0, "content": content})
            gold_idx = [i + 1 for i, (_, h) in enumerate(meta) if h]
            ans_sessions = sorted(set(q.get("answer_session_ids") or []))
            rec = {"n": n, "qid": q["question_id"], "type": q.get("question_type"), "question": q["question"],
                   "question_date": q.get("question_date"), "answer": str(q.get("answer", "")),
                   "gold": gold_idx, "gold_sessions": ans_sessions, "n_msgs": len(meta), "latency_s": round(dt, 3),
                   "returned": returned, "total_tokens": sum(r["tokens"] for r in returned),
                   "usage_delta": delta(before, after)}
            got_seq = [r["seq"] for r in returned]
            got_sess = [r["session"] for r in returned]
            for k in KS:
                rec[f"turn@{k}"] = int(any(r["has_answer_turn"] for r in returned[:k]))
                rec[f"sess@{k}"] = int(any(s in ans_sessions for s in got_sess[:k])) if ans_sessions else 0
                rec[f"allturn@{k}"] = int(set(gold_idx) <= set(got_seq[:k])) if gold_idx else 0
                rec[f"allsess@{k}"] = int(set(ans_sessions) <= set(got_sess[:k])) if ans_sessions else 0
            if usage is not None:
                rec["cum_cost_yuan"] = round(after["rerank"]["tokens"] * RERANK_YUAN_PER_TOKEN + after["embed"]["tokens"] * EMBED_YUAN_PER_TOKEN, 4)
            else:
                rec["cum_cost_yuan"] = None
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if n % 20 == 0 or n == args.offset + 1:
                print(f"[{n}] {q.get('question_type')} turn@10={rec['turn@10']} allturn@100={rec['allturn@100']} tokens={rec['total_tokens']} "
                      f"rerank_delta={rec['usage_delta']['rerank']} cost={rec['cum_cost_yuan']}", flush=True)
    u = usage_snapshot()
    summary = {"code": commit, "questions_requested": len(todo), "offset": args.offset, "stopped": stopped, "usage": u,
               "cost_yuan": (round(u["rerank"]["tokens"] * RERANK_YUAN_PER_TOKEN + u["embed"]["tokens"] * EMBED_YUAN_PER_TOKEN, 4)
                             if usage is not None else None),
               "cost_note": None if usage is not None else "this code version has no usage counters; estimate from the other arm's per-question rerank tokens",
               "effective_config": effective, "top_k": args.top_k, "started": t_start, "finished": time.strftime("%Y-%m-%d %H:%M:%S %z")}
    Path(str(out) + f".summary-{args.offset}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("code", "questions_requested", "stopped", "cost_yuan")}, ensure_ascii=False))
    if aclose:
        await aclose()
    pool.close()


if __name__ == "__main__":
    asyncio.run(main())
