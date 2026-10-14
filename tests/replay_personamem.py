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
    ap.add_argument("--only-users", default="", help="文件，每行一个 user_id；只跑这些用户上的题（用来复用已入库的数据）")
    ap.add_argument("--parallel", type=int, default=1, help="10-15：并行入库的用户数（抽取开着时一次 Add 8–20 s）")
    ap.add_argument("--no-search", action="store_true", help="10-15：只入库")
    args = ap.parse_args()

    ctx: dict[str, list[dict]] = {}
    for line in open(args.contexts, encoding="utf-8"):
        if line.strip():
            ctx.update(json.loads(line))
    qs = list(csv.DictReader(open(args.questions, encoding="utf-8")))
    if args.limit:
        qs = qs[: args.limit]
    if args.only_users:
        keep = {l.strip() for l in open(args.only_users, encoding="utf-8") if l.strip()}
        qs = [q for q in qs if f"replay:{args.tag}:pm:{q['shared_context_id'][:12]}:{q['end_index_in_shared_context']}" in keep]
        print(f"only-users: {len(keep)} users, {len(qs)} questions", flush=True)

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    client = httpx.Client(base_url=args.base, headers=headers, timeout=300)
    t_start = time.time()
    users: dict[str, tuple[str, int]] = {}
    for q in qs:
        cid, end = q["shared_context_id"], int(q["end_index_in_shared_context"])
        users.setdefault(f"replay:{args.tag}:pm:{cid[:12]}:{end}", (cid, end))

    def post_retry(c: httpx.Client, path: str, body: dict) -> httpx.Response:
        delay = 2.0
        for i in range(8):
            try:
                r = c.post(path, json=body)
            except httpx.TransportError as e:   # Codex #13 R4：超时/断连也重试，同 request_id 重放对服务端幂等
                if i == 7:
                    raise
                print(f"  retry {path} after {type(e).__name__}", flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if r.status_code == 409 and path == "/add":   # 同 ID 不同载荷，重试不会变
                r.raise_for_status()
            if r.status_code in (408, 425, 429, 500, 502, 503, 504, 524) and i < 7:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError("unreachable")

    def ingest(item: tuple[str, tuple[str, int]]) -> None:
        user_id, (cid, end) = item
        c = httpx.Client(base_url=args.base, headers=headers, timeout=300)
        for k, ch in enumerate(chunks(to_add_messages(ctx[cid], end))):
            post_retry(c, "/add", {"request_id": f"{user_id}:chunk-{k}", "user_id": user_id, "session_id": f"{user_id}:s0", "messages": ch})

    if not args.skip_add:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
            for i, _ in enumerate(ex.map(ingest, list(users.items())), 1):
                if i % 10 == 0 or i == len(users):
                    print(f"ingested {i}/{len(users)} users, {time.time() - t_start:.0f}s", flush=True)
    if args.no_search:
        return
    dump = open(args.dump, "w", encoding="utf-8") if args.dump else None
    added: set[str] = set(users)
    rows: list[dict] = []
    for qi, q in enumerate(qs):
        cid, end = q["shared_context_id"], int(q["end_index_in_shared_context"])
        user_id = f"replay:{args.tag}:pm:{cid[:12]}:{end}"
        try:
            options = json.loads(q["all_options"])
        except json.JSONDecodeError:  # CSV 里是 Python 字面量（单引号）
            options = ast.literal_eval(q["all_options"])
        t0 = time.time()
        r = post_retry(client, "/search", {"query": q["user_question_or_message"], "user_id": user_id, "top_k": args.top_k, "options": options})
        dt = time.time() - t0
        items = r.json()["data"]
        if dump is not None:
            context_messages = []
            for it in items:
                role = "assistant" if re.match(r"^\[[^\]]*\]\s+assistant:", it["content"]) else "user"
                context_messages.append({"role": role, "content": it["content"]})
            dump.write(json.dumps({"id": q["question_id"], "question_type": q["question_type"], "question": q["user_question_or_message"],
                                   "all_options": q["all_options"], "correct_answer": q["correct_answer"],
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
