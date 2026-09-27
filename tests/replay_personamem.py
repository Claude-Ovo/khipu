"""PersonaMem-v1（32k 版）回放：多选题，options 随查询一起进 Search。
数据：questions_32k.csv + shared_contexts_32k.jsonl（HF bowen-upenn/PersonaMem-v1）。
用户粒度 = (shared_context_id, end_index)：同一段共享上下文按题目的截止位置各建一个用户，避免把「之后」的偏好变化泄露给「之前」的题。
--dump 写成官方 personamem/pipeline_v1.py 要的字段：context_messages（检索结果按说话人还原成 chat messages）+ question + all_options + correct_answer。
没有 gold 证据位置，所以这里只量条数/延迟，正确率交给官方管线的答题闭环。"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import statistics
import time

import httpx


def words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def chunks(msgs: list[dict]) -> list[list[dict]]:
    out: list[list[dict]] = []
    cur: list[dict] = []
    w = 0
    for m in msgs:
        mw = words(m["content"])
        if cur and (len(cur) >= 20 or w + mw > 2000):
            out.append(cur)
            cur, w = [], 0
        cur.append(m)
        w += mw
    if cur:
        out.append(cur)
    return out


def to_add_messages(ctx: list[dict], end: int) -> list[dict]:
    """system 角色（人设卡）合同不收，按官方管线的做法转成 user 并加 [System]: 前缀。"""
    out: list[dict] = []
    for m in ctx[: end + 1]:
        role, content = m["role"], str(m.get("content", ""))
        if not content.strip():
            continue
        if role == "system":
            out.append({"role": "user", "content": "[System]: " + content})
        elif role in ("user", "assistant"):
            out.append({"role": role, "content": content})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--token", default="")
    ap.add_argument("--questions", default="/srv/aml/data/pm/questions_32k.csv")
    ap.add_argument("--contexts", default="/srv/aml/data/pm/shared_contexts_32k.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--tag", default=time.strftime("%m%d-%H%M"))
    ap.add_argument("--skip-add", action="store_true")
    ap.add_argument("--dump", default="")
    args = ap.parse_args()

    ctx: dict[str, list[dict]] = {}
    for line in open(args.contexts, encoding="utf-8"):
        if line.strip():
            ctx.update(json.loads(line))
    qs = list(csv.DictReader(open(args.questions, encoding="utf-8")))
    if args.limit:
        qs = qs[: args.limit]

    client = httpx.Client(base_url=args.base, headers={"Authorization": f"Bearer {args.token}"} if args.token else {}, timeout=300)
    dump = open(args.dump, "w", encoding="utf-8") if args.dump else None
    added: set[str] = set()
    rows: list[dict] = []
    t_start = time.time()
    for qi, q in enumerate(qs):
        cid, end = q["shared_context_id"], int(q["end_index_in_shared_context"])
        user_id = f"replay:{args.tag}:pm:{cid[:12]}:{end}"
        if not args.skip_add and user_id not in added:
            msgs = to_add_messages(ctx[cid], end)
            for k, ch in enumerate(chunks(msgs)):
                body = {"request_id": f"{user_id}:chunk-{k}", "user_id": user_id, "session_id": f"{user_id}:s0", "messages": ch}
                r = client.post("/add", json=body)
                r.raise_for_status()
            added.add(user_id)
        try:
            options = json.loads(q["all_options"])
        except json.JSONDecodeError:  # CSV 里是 Python 字面量（单引号）
            options = ast.literal_eval(q["all_options"])
        t0 = time.time()
        r = client.post("/search", json={"query": q["user_question_or_message"], "user_id": user_id, "top_k": args.top_k, "options": options})
        dt = time.time() - t0
        r.raise_for_status()
        items = r.json()["data"]
        if dump is not None:
            context_messages = []
            for it in items:
                role = "assistant" if re.match(r"^\[[^\]]*\]\s+assistant:", it["content"]) else "user"
                context_messages.append({"role": role, "content": it["content"]})
            dump.write(json.dumps({"id": q["question_id"], "question_type": q["question_type"], "question": q["user_question_or_message"],
                                   "all_options": options, "correct_answer": q["correct_answer"],
                                   "context_messages": context_messages}, ensure_ascii=False) + "\n")
        rows.append({"qid": q["question_id"], "type": q["question_type"], "n": len(items), "latency": dt,
                     "chars": sum(len(it["content"]) for it in items)})
        if (qi + 1) % 50 == 0:
            print(f"{qi + 1}/{len(qs)} done, {time.time() - t_start:.0f}s, users {len(added)}, last search {dt:.3f}s", flush=True)

    def summarize(rs: list[dict]) -> dict:
        return {"questions": len(rs), "avg_items": round(statistics.mean(r["n"] for r in rs), 1),
                "avg_chars": int(statistics.mean(r["chars"] for r in rs)),
                "p50_latency": round(statistics.median(r["latency"] for r in rs), 3),
                "max_latency": round(max(r["latency"] for r in rs), 3)}

    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    summary = {"tag": args.tag, "overall": summarize(rows), "by_type": {t: summarize(r) for t, r in sorted(by_type.items())}}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    with open(f"replay-{args.tag}.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
