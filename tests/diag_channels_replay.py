"""只检索回放（重排关、trace 开）：把每题五路召回的命中列表和融合顺序落盘，供离线重算融合规则用。
2026-10-01 诊断（collab/诊断-事实与多跳-20260930/fusion-sim/）：多跳配对实验里发现实体路按时间倒序灌进 RRF 把老证据挤出重排窗口，
改法只在 LoCoMo 多跳上验过；这里把 LoCoMo 全部有证据的类别和 LME 前 200 题都跑一遍，改法好不好在离线数据上比，不再花重排的钱。

只花查询向量的钱（每题一次，约 30 token）。进程内跑，不走 HTTP，不碰 8080；库用 aml2（LoCoMo 段已在，LME 段先用 copy_replay_users.py 拷）。

用法（服务器上，/srv/aml/app2 下，先 set -a; . /srv/aml/.env; . /srv/aml/.env2; set +a）：
  python tests/diag_channels_replay.py --out /srv/aml/data/diag-locomo/channels-locomo.jsonl --dataset locomo
  python tests/diag_channels_replay.py --out /srv/aml/data/diag-locomo/channels-lme.jsonl --dataset lme --limit 200
输出每题一行：各路命中（按 seq）、融合后顺序、最终返回顺序、黄金证据的 seq。"""
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
from tests.diag_locomo_rerank_pair import conversation_dia_ids, conversation_texts, seq_of  # noqa: E402

EMBED_YUAN_PER_TOKEN = 0.5 / 1_000_000
LOCOMO_TAG = "bm25-v03"
LME_TAG = "lme-s-vec-v031"


_KEY = re.compile(r"#(\d+)\.(\d+)$")


def seqs(ids: list[str]) -> list[str]:
    """段 id 末尾的 "seq.part"。LME 的长消息会被切成几个 part，只留 seq 会让两个 part 撞成一条、离线重算时对不上；
    判黄金证据时取小数点前面的 seq 就行。"""
    return [(m.group(1) + "." + m.group(2)) if (m := _KEY.search(i)) else "-1.0" for i in ids]


def locomo_questions(path: str) -> list[dict]:
    data = json.load(open(path, encoding="utf-8"))
    out = []
    for ci, conv in enumerate(data):
        dias = conversation_dia_ids(conv)
        dia_to_seq = {d: i + 1 for i, d in enumerate(dias)}
        for qi, qa in enumerate(conv["qa"]):
            raw = [e for e in (qa.get("evidence") or []) if isinstance(e, str)]
            gold = []
            for e in raw:
                gold.extend(g for g in (f"D{int(a)}:{int(b)}" for a, b in re.findall(r"D:?(\d+):(\d+)", e)) if g not in gold)
            if not gold:            # 第 5 类（对抗）没有证据，跳过
                continue
            out.append({"dataset": "locomo", "category": qa.get("category"), "conv": ci, "qi": qi,
                        "question": qa["question"], "answer": qa.get("answer"),
                        "user_id": f"replay:{LOCOMO_TAG}:locomo:conv-{ci}",
                        "gold_dia": gold, "gold": [dia_to_seq[g] for g in gold if g in dia_to_seq],
                        "unmapped_gold": [g for g in gold if g not in dia_to_seq],
                        "first_text": conversation_texts(conv)[0]})
    return out


def lme_questions(path: str, limit: int) -> list[dict]:
    data = json.load(open(path, encoding="utf-8"))
    if limit:
        data = data[:limit]
    out = []
    for q in data:
        # 与 tests/replay_longmemeval.py 同一种摊平：空消息跳过，seq 从 1 起
        meta: list[tuple[str, bool]] = []
        first_text = None
        for sid, turns in zip(q["haystack_session_ids"], q["haystack_sessions"]):
            for t in turns:
                if not str(t.get("content", "")).strip():
                    continue
                if first_text is None:
                    first_text = t["content"]
                meta.append((sid, bool(t.get("has_answer", False))))
        ans = set(q.get("answer_session_ids") or [])
        out.append({"dataset": "lme", "category": q.get("question_type"), "qid": q["question_id"],
                    "question": q["question"], "answer": str(q.get("answer", "")),
                    "user_id": f"replay:{LME_TAG}:lme:{q['question_id']}",
                    "gold": [i + 1 for i, (_, h) in enumerate(meta) if h],
                    "gold_sessions": {sid: [i + 1 for i, (s, _) in enumerate(meta) if s == sid] for sid in sorted(ans)},
                    "first_text": first_text or ""})
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=("locomo", "lme"), required=True)
    ap.add_argument("--data", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--max-cost", type=float, default=0.5, help="查询向量累计花费上限（元）")
    args = ap.parse_args()

    config.RERANK_ENABLED = False        # 只看融合之前的东西；重排的效果用配对实验的数据另算
    if args.dataset == "locomo":
        qs = locomo_questions(args.data or "/srv/aml/data/locomo10.json")
    else:
        qs = lme_questions(args.data or "/srv/aml/data/longmemeval_s.json", args.limit or 200)
    todo = qs[args.offset:]
    if args.dataset == "locomo" and args.limit:
        todo = todo[: args.limit]
    print(f"{args.dataset}: {len(qs)} questions; running {len(todo)} from offset {args.offset}; rerank={config.RERANK_ENABLED} hop={config.HOP_ENABLED}", flush=True)

    cache: dict[str, list[float] | None] = {}

    async def cached_embed(text: str):
        if text not in cache:
            cache[text] = await _real_embed_query(text)
        return cache[text]
    S.embed_query = cached_embed

    pool.open()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    checked: set[str] = set()
    stopped = None
    with open(out_path, "a" if args.offset else "w", encoding="utf-8") as f:
        for n, q in enumerate(todo, start=args.offset + 1):
            if q["user_id"] not in checked:
                # 核对 seq 映射：库里第 1 段应包含这份数据第 1 条消息的原文
                idx = S.get_index(q["user_id"])
                assert idx.rows, f"no segments for {q['user_id']}"
                r0 = next(r for r in idx.rows if r.seq == 1)
                assert q["first_text"][:40] in r0.text, (q["user_id"], r0.text[:80], q["first_text"][:80])
                checked.add(q["user_id"])
            trace: dict = {}
            t0 = time.monotonic()
            items = await S.search(q["user_id"], q["question"], None, args.top_k, trace=trace)
            if not trace["channels"].get("vector"):
                stopped = f"vector channel empty on question {n} ({q['user_id']}): embedding failed? stopping so the file stays clean"
                print("STOP:", stopped, flush=True)
                break
            rec = {k: v for k, v in q.items() if k not in ("first_text",)}
            rec.update({"n": n, "latency_s": round(time.monotonic() - t0, 3), "intent": trace["intent"],
                        "channels": {name: seqs(ids) for name, ids in trace["channels"].items()},
                        "fused": seqs(trace["fused"]), "final": seqs([it["id"] for it in items])})
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            u = usage.snapshot()
            cost = u["embed"]["tokens"] * EMBED_YUAN_PER_TOKEN
            pos = {}
            for i, key in enumerate(rec["fused"]):
                pos.setdefault(int(key.split(".")[0]), i + 1)
            g = [pos.get(s) for s in q["gold"]]
            if n % 25 == 0 or n == args.offset + 1:
                print(f"[{n}] {q.get('category')} gold_fused_ranks={g} sizes={ {k: len(v) for k, v in rec['channels'].items()} } "
                      f"embed_tokens={u['embed']['tokens']} cost=¥{cost:.4f}", flush=True)
            if cost > args.max_cost:
                stopped = f"cost cap ¥{args.max_cost} reached after question {n}"
                print("STOP:", stopped, flush=True)
                break
    u = usage.snapshot()
    summary = {"dataset": args.dataset, "questions": len(qs), "offset": args.offset, "ran": len(todo), "stopped": stopped,
               "usage": u, "cost_yuan": round(u["embed"]["tokens"] * EMBED_YUAN_PER_TOKEN, 4),
               "config": {"RERANK_ENABLED": config.RERANK_ENABLED, "HOP_ENABLED": config.HOP_ENABLED,
                          "CHANNEL_TOPN_MULT": config.CHANNEL_TOPN_MULT, "RRF_K": config.RRF_K, "top_k": args.top_k},
               "commit": (Path(__file__).resolve().parents[1] / "COMMIT").read_text().strip() if (Path(__file__).resolve().parents[1] / "COMMIT").exists() else "unknown"}
    Path(str(out_path) + f".summary-{args.offset}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("ran", "stopped", "cost_yuan")}, ensure_ascii=False))
    await aclose()
    pool.close()


if __name__ == "__main__":
    asyncio.run(main())
