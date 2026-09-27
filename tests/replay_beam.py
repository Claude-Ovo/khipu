"""BEAM（HF Mohammadta/BEAM，100K 分片）回放：20 段长对话、每段 10 类探针题（abstention / contradiction_resolution /
event_ordering / information_extraction / instruction_following / knowledge_update / ...），rubric 判分。
用户粒度 = 一段对话；对话里的每个 session 一个 session_id；time_anchor（如 March-15-2024）转成时间戳。
--dump 写成官方 beam/pipeline.py 要的字段：id / question / question_type / rubric_nuggets / speaker_1(2)_memories。
没有 gold 证据位置，只量条数/延迟，分数交给官方闭环。"""
from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
import time
from datetime import datetime, timezone

import httpx
import pandas as pd


def parse_pq(s: str):
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return ast.literal_eval(s)


def anchor_ms(s: str | None) -> int | None:
    if not s:
        return None
    for fmt in ("%B-%d-%Y", "%B %d, %Y", "%Y-%m-%d", "%b-%d-%Y"):
        try:
            return int(datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--token", default="")
    ap.add_argument("--data", default="/srv/aml/data/beam/beam-100K.parquet")
    ap.add_argument("--convos", type=int, default=0)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--tag", default=time.strftime("%m%d-%H%M"))
    ap.add_argument("--skip-add", action="store_true")
    ap.add_argument("--dump", default="")
    args = ap.parse_args()

    df = pd.read_parquet(args.data)
    if args.convos:
        df = df.iloc[: args.convos]
    client = httpx.Client(base_url=args.base, headers={"Authorization": f"Bearer {args.token}"} if args.token else {}, timeout=300)
    dump = open(args.dump, "w", encoding="utf-8") if args.dump else None
    rows: list[dict] = []
    t_start = time.time()
    for ci, (_, conv) in enumerate(df.iterrows()):
        user_id = f"replay:{args.tag}:beam:{conv['conversation_id']}"
        if not args.skip_add:
            for si, sess in enumerate(conv["chat"]):
                msgs = []
                for m in sess:
                    role = m["role"]
                    if role not in ("user", "assistant") or not str(m["content"]).strip():
                        continue
                    ts = anchor_ms(m.get("time_anchor"))
                    msgs.append({"role": role, "content": str(m["content"]), **({"timestamp": ts} if ts else {})})
                for k, ch in enumerate(chunks(msgs)):
                    body = {"request_id": f"{user_id}:s{si}:chunk-{k}", "user_id": user_id, "session_id": f"{user_id}:s{si}", "messages": ch}
                    r = client.post("/add", json=body)
                    r.raise_for_status()
        pq = parse_pq(conv["probing_questions"])
        groups = pq.items() if isinstance(pq, dict) else [("?", pq)]
        for qtype, qlist in groups:
            for qi, q in enumerate(qlist if isinstance(qlist, list) else [qlist]):
                question = str(q.get("question", ""))
                rubric = q.get("rubric") or q.get("rubrics") or ([q["ideal_response"]] if q.get("ideal_response") else [])
                if not question or not rubric:
                    continue
                t0 = time.time()
                r = client.post("/search", json={"query": question, "user_id": user_id, "top_k": args.top_k})
                dt = time.time() - t0
                r.raise_for_status()
                items = r.json()["data"]
                qid = f"{conv['conversation_id']}:{qtype}:{qi}"
                if dump is not None:
                    mem_a = [it["content"] for it in items if re.match(r"^\[[^\]]*\]\s+assistant:", it["content"])]
                    mem_u = [it["content"] for it in items if it["content"] not in mem_a]
                    dump.write(json.dumps({"id": qid, "question_type": qtype, "question": question,
                                           "rubric_nuggets": [str(x) for x in rubric], "ideal_response": q.get("ideal_response", ""),
                                           "speaker_1_name": "user", "speaker_1_memories": "\n".join(mem_u),
                                           "speaker_2_name": "assistant", "speaker_2_memories": "\n".join(mem_a)}, ensure_ascii=False) + "\n")
                rows.append({"qid": qid, "type": qtype, "n": len(items), "latency": dt, "chars": sum(len(it["content"]) for it in items)})
        print(f"conv {ci + 1}/{len(df)} done, {time.time() - t_start:.0f}s, questions so far {len(rows)}", flush=True)

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
