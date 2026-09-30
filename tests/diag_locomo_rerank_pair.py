"""LoCoMo 多跳：同一 commit、同一配置、同一批题，重排关 / 开各跑一次，逐题配对，逐条黄金证据记名次。
2026-09-30 诊断实验（collab/诊断-事实与多跳-20260930/）。在服务器上进程内跑，不走 HTTP，这样能拿到中间过程：
各路命中、融合后（重排前）的完整顺序、重排窗口与每条分数、重排后顺序、邻居/规矩插入后的顺序、最终装箱的条目与 token 数。

两个臂共用同一个查询向量（每题只算一次，缓存），所以差别只来自重排这一步。库里的段是 9-27 回放写进去的
（tag bm25-v03，一条消息一段，seq 从 1 起按对话顺序，seq -> dia_id 一一对应），先拷进 aml2 再跑，不碰评测用的库。

用法（服务器上，/srv/aml/app2 下）：
  DATABASE_URL=postgresql://aml:aml-local-only@127.0.0.1:5432/aml2 \\
  python tests/diag_locomo_rerank_pair.py --out /srv/aml/data/diag-locomo/pair.jsonl --limit 20 --max-cost 5
  （--offset N 从第 N+1 题接着跑；--max-cost 是重排累计花费上限，元，按 ¥0.8/百万 token 算，超了立刻停）
输出：每题一行 JSON；另写 <out>.summary.json。"""
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
from app.embed import embed_query as _real_embed_query  # noqa: E402
from app.httpclient import aclose, usage  # noqa: E402
from app.textutil import count_tokens  # noqa: E402

TAG = "bm25-v03"
RERANK_YUAN_PER_TOKEN = 0.8 / 1_000_000
EMBED_YUAN_PER_TOKEN = 0.5 / 1_000_000
_DIA = re.compile(r"^D:?(\d+):(\d+)$")   # 标注里有 "D:11:26" 这种写法


def norm_dia(s: str) -> str | None:
    m = _DIA.match(s.strip())
    return f"D{int(m.group(1))}:{int(m.group(2))}" if m else None


def conversation_dia_ids(conv: dict) -> list[str]:
    """与 tests/replay_locomo.py 的 conversation_messages 同一顺序：按会话号、会话内按轮次。"""
    c = conv["conversation"]
    sessions = sorted(int(k.split("_")[1]) for k in c if re.fullmatch(r"session_\d+", k))
    return [norm_dia(t["dia_id"]) or t["dia_id"] for n in sessions for t in c[f"session_{n}"]]


def conversation_texts(conv: dict) -> list[str]:
    c = conv["conversation"]
    sessions = sorted(int(k.split("_")[1]) for k in c if re.fullmatch(r"session_\d+", k))
    return [t["text"] for n in sessions for t in c[f"session_{n}"]]


def seq_of(item_id: str) -> int | None:
    m = re.search(r"#(\d+)\.\d+$", item_id)
    return int(m.group(1)) if m else None


def ranks(ids: list[str], seq_to_dia: dict[int, str], gold: list[str]) -> dict[str, int | None]:
    """每条黄金证据在列表里第一次出现的名次（1 起）；不在就是 None。"""
    first: dict[str, int] = {}
    for i, it in enumerate(ids):
        d = seq_to_dia.get(seq_of(it) or -1)
        if d is not None and d not in first:
            first[d] = i + 1
    return {g: first.get(g) for g in gold}


def arm_record(trace: dict, items: list[dict], seq_to_dia: dict[int, str], gold: list[str], latency: float) -> dict:
    final_ids = [it["id"] for it in items]
    rr = trace.get("rerank") or {}
    rec = {
        "latency_s": round(latency, 3),
        "intent": trace.get("intent"),
        "rerank_status": rr.get("status"),
        "fused_len": len(trace.get("fused", [])),
        "rank_fused": ranks(trace.get("fused", []), seq_to_dia, gold),            # 重排前（融合 + 双命中前置）
        # 重排后的完整顺序 = 窗口内按分数排 + 窗口外原样接上；另记每条证据是否在窗口内
        "rank_after_rerank": ranks(rr["after"] + trace.get("pre_rerank", trace.get("fused", []))[len(rr["head"]):], seq_to_dia, gold) if rr.get("after") else None,
        "in_rerank_window": {g: (r is not None and r <= len(rr.get("head", []))) for g, r in ranks(trace.get("pre_rerank", trace.get("fused", [])), seq_to_dia, gold).items()},
        "rank_final": ranks(final_ids, seq_to_dia, gold),                          # 最终返回、送进答题上下文的顺序
        "final_n": len(items),
        "final_tokens_cl100k": trace.get("boxed_tokens"),
        "budget_skipped": trace.get("budget_skipped"),
        "trace": trace,                                                            # 原样保存：各路命中、fused、重排 head/scores/after、pre_box、boxed
        "final": [{"id": it["id"], "score": it.get("score"), "tokens": count_tokens(it["content"])} for it in items],
    }
    for k in (10, 100):
        hit = [g for g, r in rec["rank_final"].items() if r is not None and r <= k]
        rec[f"all@{k}"] = int(bool(gold) and len(hit) == len(gold))
        rec[f"hops@{k}"] = len(hit)
    return rec


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/srv/aml/data/locomo10.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--category", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--max-cost", type=float, default=5.0, help="本次运行重排 + 查询向量的累计花费上限（元）")
    args = ap.parse_args()

    data = json.load(open(args.data, encoding="utf-8"))
    questions = []
    for ci, conv in enumerate(data):
        dias = conversation_dia_ids(conv)
        seq_to_dia = {i + 1: d for i, d in enumerate(dias)}
        for qi, qa in enumerate(conv["qa"]):
            if qa.get("category") != args.category:
                continue
            raw = [e for e in (qa.get("evidence") or []) if isinstance(e, str)]
            # 个别标注把几条写在一个字符串里（"D8:6; D9:17"），逐个抽出来；抽不出的记为坏标注
            gold: list[str] = []
            bad: list[str] = []
            for e in raw:
                found = [f"D{int(a)}:{int(b)}" for a, b in re.findall(r"D:?(\d+):(\d+)", e)]
                if not found:
                    bad.append(e)
                gold.extend(g for g in found if g not in gold)
            questions.append({"conv": ci, "qi": qi, "question": qa["question"], "answer": qa.get("answer"),
                              "raw_evidence": raw, "gold": gold, "bad_evidence": bad,
                              "unmapped_gold": [g for g in gold if g not in set(dias)],   # 对话里没有这一轮：这题的 all@k 不可算，汇总时排除
                              "seq_to_dia": seq_to_dia, "texts": conversation_texts(conv)})
    total = len(questions)
    todo = questions[args.offset:]
    if args.limit:
        todo = todo[: args.limit]
    print(f"category {args.category}: {total} questions; running {len(todo)} from offset {args.offset}", flush=True)

    # 每题的查询向量只算一次，两个臂共用
    cache: dict[str, list[float] | None] = {}

    async def cached_embed(text: str):
        if text not in cache:
            cache[text] = await _real_embed_query(text)
        return cache[text]
    S.embed_query = cached_embed

    pool.open()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.offset else "w"
    stopped = None
    checked_text = False
    with open(out_path, mode, encoding="utf-8") as f:
        for n, q in enumerate(todo, start=args.offset + 1):
            user_id = f"replay:{TAG}:locomo:conv-{q['conv']}"
            if not checked_text:
                # 核对 seq -> dia_id 映射：库里第 1 段的正文应当包含对话第 1 条的原文
                idx = S.get_index(user_id)
                r0 = next(r for r in idx.rows if r.seq == 1)
                assert q["texts"][0][:40] in r0.text, (r0.text[:80], q["texts"][0][:80])
                checked_text = True
            rec = {k: q[k] for k in ("conv", "qi", "question", "answer", "raw_evidence", "gold", "bad_evidence", "unmapped_gold")}
            rec["n"] = n
            for arm, enabled in (("off", False), ("on", True)):
                config.RERANK_ENABLED = enabled
                trace: dict = {}
                t0 = time.monotonic()
                items = await S.search(user_id, q["question"], None, args.top_k, trace=trace)
                rec[arm] = arm_record(trace, items, q["seq_to_dia"], q["gold"], time.monotonic() - t0)
            u = usage.snapshot()
            cost = u["rerank"]["tokens"] * RERANK_YUAN_PER_TOKEN + u["embed"]["tokens"] * EMBED_YUAN_PER_TOKEN
            rec["cum_rerank_tokens"] = u["rerank"]["tokens"]
            rec["cum_cost_yuan"] = round(cost, 4)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{n}/{total}] conv{q['conv']} q{q['qi']} gold={len(q['gold'])} "
                  f"off all@10/100={rec['off']['all@10']}/{rec['off']['all@100']} "
                  f"on={rec['on']['all@10']}/{rec['on']['all@100']} rerank={rec['on']['rerank_status']} "
                  f"cum_tokens={u['rerank']['tokens']} cost=¥{cost:.3f}", flush=True)
            if cost > args.max_cost:
                stopped = f"cost cap ¥{args.max_cost} reached after question {n}"
                print("STOP:", stopped, flush=True)
                break
    u = usage.snapshot()
    summary = {"questions_in_category": total, "offset": args.offset, "ran": len(todo), "stopped": stopped,
               "usage": u, "cost_yuan": round(u["rerank"]["tokens"] * RERANK_YUAN_PER_TOKEN + u["embed"]["tokens"] * EMBED_YUAN_PER_TOKEN, 4),
               "config": {"RERANK_MODEL": config.RERANK_MODEL, "RERANK_TOPN": config.RERANK_TOPN, "RERANK_MIX": config.RERANK_MIX,
                          "RERANK_DOC_CHARS": config.RERANK_DOC_CHARS, "BUDGET_TOKENS": config.BUDGET_TOKENS,
                          "CHANNEL_TOPN_MULT": config.CHANNEL_TOPN_MULT, "NEIGHBOR_CAP_RATIO": config.NEIGHBOR_CAP_RATIO,
                          "HOP_ENABLED": config.HOP_ENABLED, "top_k": args.top_k},
               "commit": (Path(__file__).resolve().parents[1] / "COMMIT").read_text().strip() if (Path(__file__).resolve().parents[1] / "COMMIT").exists() else "unknown"}
    Path(str(out_path) + f".summary-{args.offset}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("ran", "stopped", "cost_yuan")}, ensure_ascii=False))
    await aclose()
    pool.close()


if __name__ == "__main__":
    asyncio.run(main())
