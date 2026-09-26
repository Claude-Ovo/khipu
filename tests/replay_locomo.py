"""LoCoMo 回放：按平台分块规则（20 条消息或 2000 词）把对话喂进 Add，再逐题 Search，算 hit@k。
这是靶场的第一把尺子。用法：
  python tests/replay_locomo.py --base http://127.0.0.1:8080 --token XXX --data /srv/aml/data/locomo10.json --convos 10 --tag bm25
输出：每类题的 any-hit@{10,20,50,100}、全覆盖@100、平均返回 token、平均延迟；写到 replay-<tag>.json。"""
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
CATEGORY = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


def parse_session_time(s: str) -> int:
    # "1:56 pm on 8 May, 2023"
    dt = datetime.strptime(s.strip(), "%I:%M %p on %d %B, %Y").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def conversation_messages(conv: dict) -> list[dict]:
    """把 LoCoMo 的多段会话摊平成一条消息流，每条带自己会话的时间戳和 dia_id。"""
    c = conv["conversation"]
    sessions = sorted((int(k.split("_")[1]) for k in c if re.fullmatch(r"session_\d+", k)))
    speakers: list[str] = []
    out: list[dict] = []
    for n in sessions:
        ts = parse_session_time(c[f"session_{n}_date_time"])
        for turn in c[f"session_{n}"]:
            if turn["speaker"] not in speakers:
                speakers.append(turn["speaker"])
            role = "user" if speakers.index(turn["speaker"]) == 0 else "assistant"
            text = turn["text"]
            if turn.get("blip_caption"):
                text = f"{text} [shared image: {turn['blip_caption']}]"
            out.append({"role": role, "content": f"{turn['speaker']}: {text}", "timestamp": ts, "dia_id": turn["dia_id"]})
    return out


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
    ap.add_argument("--data", default="/srv/aml/data/locomo10.json")
    ap.add_argument("--convos", type=int, default=10)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--tag", default=time.strftime("%m%d-%H%M"))
    ap.add_argument("--skip-add", action="store_true", help="数据已经在库里，只跑 Search")
    ap.add_argument("--dump", default="", help="把每题的检索结果按官方答题模板的字段写成 JSONL，喂给 agent-memory-leaderboard 的 pipeline.py answer")
    args = ap.parse_args()
    dump = open(args.dump, "w", encoding="utf-8") if args.dump else None

    client = httpx.Client(base_url=args.base, headers={"Authorization": f"Bearer {args.token}"} if args.token else {}, timeout=120)
    data = json.load(open(args.data, encoding="utf-8"))[: args.convos]

    per_cat: dict[str, list[dict]] = {}
    all_rows: list[dict] = []
    for ci, conv in enumerate(data):
        user_id = f"replay:{args.tag}:locomo:conv-{ci}"
        session_id = f"replay:{args.tag}:sample:{ci}"
        msgs = conversation_messages(conv)
        # 我们的段 id 是 session#seq.part，seq 从 1 起按消息顺序递增；这里建 seq -> dia_id 的映射
        seq_to_dia = {i + 1: m["dia_id"] for i, m in enumerate(msgs)}
        if not args.skip_add:
            t0 = time.time()
            for k, ch in enumerate(chunks(msgs)):
                body = {"request_id": f"{user_id}:chunk-{k}", "user_id": user_id, "session_id": session_id,
                        "messages": [{"role": m["role"], "content": m["content"], "timestamp": m["timestamp"]} for m in ch]}
                r = client.post("/add", json=body)
                r.raise_for_status()
                assert r.json().get("success") is True
            print(f"conv-{ci}: {len(msgs)} msgs, {len(chunks(msgs))} chunks, add took {time.time() - t0:.1f}s", flush=True)

        for qa in conv["qa"]:
            evidence = [e for e in (qa.get("evidence") or []) if isinstance(e, str)]
            cat = CATEGORY.get(qa.get("category"), str(qa.get("category")))
            if not evidence:
                continue  # adversarial 之类没有金标证据的题不算命中率
            t0 = time.time()
            r = client.post("/search", json={"query": qa["question"], "user_id": user_id, "top_k": args.top_k})
            dt = time.time() - t0
            r.raise_for_status()
            items = r.json()["data"]
            got = []
            for it in items:
                m = re.search(r"#(\d+)\.\d+$", it["id"])
                if m:
                    got.append(seq_to_dia.get(int(m.group(1))))
            ev = set(evidence)
            if dump is not None:
                sa, sb = conv["conversation"].get("speaker_a", "speaker 1"), conv["conversation"].get("speaker_b", "speaker 2")
                mem_a, mem_b = [], []
                for it in items:
                    m = re.match(r"^\[[^\]]*\]\s+([^:]+):", it["content"])
                    (mem_b if m and m.group(1).strip() == sb else mem_a).append(it["content"])
                gold = qa.get("answer") if qa.get("answer") not in (None, "") else qa.get("adversarial_answer", "")
                dump.write(json.dumps({"id": f"conv-{ci}:{qa.get('question', '')[:40]}:{len(all_rows)}", "category": cat,
                                       "question": qa["question"], "gold_answer": str(gold),
                                       "speaker_1_name": sa, "speaker_1_memories": "\n".join(mem_a),
                                       "speaker_2_name": sb, "speaker_2_memories": "\n".join(mem_b)}, ensure_ascii=False) + "\n")
            row = {"conv": ci, "cat": cat, "n": len(items), "latency": dt,
                   "chars": sum(len(it["content"]) for it in items)}
            for k in KS:
                topk = set(got[:k])
                row[f"any@{k}"] = int(bool(ev & topk))
                row[f"all@{k}"] = int(ev <= topk)
            per_cat.setdefault(cat, []).append(row)
            all_rows.append(row)

    def summarize(rows: list[dict]) -> dict:
        s: dict = {"questions": len(rows)}
        for k in KS:
            s[f"any@{k}"] = round(statistics.mean(r[f"any@{k}"] for r in rows), 4)
        s["all@100"] = round(statistics.mean(r["all@100"] for r in rows), 4)
        s["avg_items"] = round(statistics.mean(r["n"] for r in rows), 1)
        s["avg_chars"] = int(statistics.mean(r["chars"] for r in rows))
        s["p50_latency"] = round(statistics.median(r["latency"] for r in rows), 3)
        s["max_latency"] = round(max(r["latency"] for r in rows), 3)
        return s

    summary = {"tag": args.tag, "convos": len(data), "overall": summarize(all_rows),
               "by_category": {c: summarize(r) for c, r in sorted(per_cat.items())}}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    with open(f"replay-{args.tag}.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": all_rows}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
