"""LongMemEval 回放（S 或 oracle 版）：每道题一个 user，整个 haystack 按平台分块规则 Add，再 Search 一次。
命中口径两种：session 级（返回里有没有 answer_session 的段）、turn 级（有没有 has_answer=True 的那一条）。
用法：python tests/replay_longmemeval.py --data /srv/aml/data/longmemeval_s.json --limit 100 --tag lme-s-bm25"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from datetime import datetime, timezone

import httpx

CHUNK_MSGS = 20
CHUNK_WORDS = 2000
KS = (10, 20, 50, 100)


def parse_date(s: str) -> int | None:
    # "2023/05/20 (Sat) 02:21"
    for fmt in ("%Y/%m/%d (%a) %H:%M", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None


def words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def chunks(msgs: list[dict]) -> list[list[dict]]:
    out: list[list[dict]] = []
    buf: list[dict] = []
    w = 0
    for m in msgs:
        mw = words(m["content"])
        if buf and (len(buf) >= CHUNK_MSGS or w + mw > CHUNK_WORDS):
            out.append(buf)
            buf, w = [], 0
        buf.append(m)
        w += mw
    if buf:
        out.append(buf)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--token", default="")
    ap.add_argument("--data", default="/srv/aml/data/longmemeval_s.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--tag", default=time.strftime("%m%d-%H%M"))
    ap.add_argument("--skip-add", action="store_true")
    ap.add_argument("--types", default="", help="只跑这些 question_type（逗号分隔），配合 --skip-add 做小样本对照")
    ap.add_argument("--dump", default="", help="把每题的检索结果按官方答题模板字段写成 JSONL（speaker_1=user, speaker_2=assistant）")
    args = ap.parse_args()
    dump = open(args.dump, "w", encoding="utf-8") if args.dump else None

    client = httpx.Client(base_url=args.base, headers={"Authorization": f"Bearer {args.token}"} if args.token else {}, timeout=300)
    data = json.load(open(args.data, encoding="utf-8"))
    if args.limit:
        data = data[: args.limit]
    if args.types:
        want = set(args.types.split(","))
        data = [q for q in data if q.get("question_type") in want]

    rows: list[dict] = []
    t_start = time.time()
    for qi, q in enumerate(data):
        user_id = f"replay:{args.tag}:lme:{q['question_id']}"
        session_id = f"replay:{args.tag}:sample:{qi}"
        # 摊平：每条消息记住它属于哪个 session、是不是 has_answer
        msgs: list[dict] = []
        meta: list[tuple[str, bool]] = []
        for sid, sdate, turns in zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]):
            ts = parse_date(sdate)
            for t in turns:
                if not str(t.get("content", "")).strip():
                    continue
                msgs.append({"role": t["role"], "content": t["content"], "timestamp": ts})
                meta.append((sid, bool(t.get("has_answer", False))))
        if not args.skip_add:
            for k, ch in enumerate(chunks(msgs)):
                body = {"request_id": f"{user_id}:chunk-{k}", "user_id": user_id, "session_id": session_id,
                        "messages": [{"role": m["role"], "content": m["content"], **({"timestamp": m["timestamp"]} if m["timestamp"] else {})} for m in ch]}
                r = client.post("/add", json=body)
                r.raise_for_status()
        t0 = time.time()
        r = client.post("/search", json={"query": q["question"], "user_id": user_id, "top_k": args.top_k})
        dt = time.time() - t0
        r.raise_for_status()
        items = r.json()["data"]
        got_meta: list[tuple[str, bool]] = []
        got_idx: list[int] = []
        for it in items:
            m = re.search(r"#(\d+)\.\d+$", it["id"])
            if m and 1 <= int(m.group(1)) <= len(meta):
                got_meta.append(meta[int(m.group(1)) - 1])
                got_idx.append(int(m.group(1)) - 1)
            else:  # 审查 #4：映射不上的条目保留占位，名次不能被压缩
                got_meta.append(("", False))
                got_idx.append(-1)
        gold_idx = {i for i, (_, h) in enumerate(meta) if h}
        ans_sessions = set(q.get("answer_session_ids") or [])
        if dump is not None:
            mem_a = [it["content"] for it in items if re.match(r"^\[[^\]]*\]\s+assistant:", it["content"])]
            mem_u = [it["content"] for it in items if it["content"] not in mem_a]  # 没归到 assistant 的一律给 user，不丢
            dump.write(json.dumps({"id": q["question_id"], "question_type": q.get("question_type"), "question": q["question"],
                                   "question_date": q.get("question_date"), "gold_answer": str(q.get("answer", "")),
                                   "speaker_1_name": "user", "speaker_1_memories": "\n".join(mem_u),
                                   "speaker_2_name": "assistant", "speaker_2_memories": "\n".join(mem_a)}, ensure_ascii=False) + "\n")
        row = {"qid": q["question_id"], "type": q.get("question_type"), "n_msgs": len(msgs), "n": len(items),
               "latency": dt, "chars": sum(len(it["content"]) for it in items)}
        for k in KS:
            top = got_meta[:k]
            row[f"sess@{k}"] = int(any(s in ans_sessions for s, _ in top)) if ans_sessions else 0
            row[f"turn@{k}"] = int(any(h for _, h in top))
            # all-hit：多会话/计数题要的是证据齐全，不是碰到一条
            seen_idx = {i for i in got_idx[:k] if i >= 0}
            seen_sess = {meta[i][0] for i in seen_idx}
            row[f"allsess@{k}"] = int(ans_sessions <= seen_sess) if ans_sessions else 0
            row[f"allturn@{k}"] = int(gold_idx <= seen_idx) if gold_idx else 0
        rows.append(row)
        if (qi + 1) % 20 == 0:
            print(f"{qi + 1}/{len(data)} done, {time.time() - t_start:.0f}s, last search {dt:.3f}s, msgs {len(msgs)}", flush=True)

    def summarize(rs: list[dict]) -> dict:
        s: dict = {"questions": len(rs), "avg_msgs": int(statistics.mean(r["n_msgs"] for r in rs))}
        for k in KS:
            s[f"sess@{k}"] = round(statistics.mean(r[f"sess@{k}"] for r in rs), 4)
            s[f"turn@{k}"] = round(statistics.mean(r[f"turn@{k}"] for r in rs), 4)
            s[f"allsess@{k}"] = round(statistics.mean(r.get(f"allsess@{k}", 0) for r in rs), 4)
            s[f"allturn@{k}"] = round(statistics.mean(r.get(f"allturn@{k}", 0) for r in rs), 4)
        s["avg_items"] = round(statistics.mean(r["n"] for r in rs), 1)
        s["avg_chars"] = int(statistics.mean(r["chars"] for r in rs))
        s["p50_latency"] = round(statistics.median(r["latency"] for r in rs), 3)
        s["max_latency"] = round(max(r["latency"] for r in rs), 3)
        return s

    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(str(r["type"]), []).append(r)
    summary = {"tag": args.tag, "overall": summarize(rows), "by_type": {t: summarize(r) for t, r in sorted(by_type.items())}}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    with open(f"replay-{args.tag}.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
