"""BEAM（HF Mohammadta/BEAM，100K 分片）回放：20 段长对话、每段 10 类探针题（abstention / contradiction_resolution /
event_ordering / information_extraction / instruction_following / knowledge_update / ...），rubric 判分。
用户粒度 = 一段对话；对话里的每个 session 一个 session_id；time_anchor（如 March-15-2024）转成时间戳。
--dump 写成官方 beam/pipeline.py 要的字段：id / question / question_type / rubric_nuggets / speaker_1(2)_memories。
没有 gold 证据位置，只量条数/延迟，分数交给官方闭环。

10-15 加的（第二枪 BEAM 对照）：
  --types     只对这几类题 Search（逗号分隔），入库不受影响
  --no-search 只入库（对照时两臂各自用 --skip-add 导出）
  --parallel  入库时并行的对话数（段内 Add 仍按顺序；抽取开着时一次 Add 8–20 s，串行 20 段要两个多小时）
  Add 的 408/429/5xx/503 按平台规则重试（抽取实例忙时回 503，合同允许重试）
  每阶段前后读 /health 的 usage，把 token 增量写进 replay-<tag>.json 和 dump 的 .summary.json（算钱用）"""
from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pandas as pd

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 524}


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


def post_retry(client: httpx.Client, path: str, body: dict, attempts: int = 8) -> httpx.Response:
    """平台规则的那几个状态码重试；网络层异常（超时、连接断）也重试——同一个 request_id 重放对服务端是幂等的（Codex #13 R4）。
    同 ID 不同载荷的 409 重试不会变，直接报错。"""
    delay = 2.0
    for i in range(attempts):
        try:
            r = client.post(path, json=body)
        except httpx.TransportError as e:
            if i == attempts - 1:
                raise
            print(f"  retry {path} after {type(e).__name__}", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if r.status_code == 409 and path == "/add":
            r.raise_for_status()
        if r.status_code in RETRY_STATUS and i < attempts - 1:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError("unreachable")


def health_usage(client: httpx.Client) -> dict | None:
    try:
        return client.get("/health", timeout=10).json().get("usage")
    except Exception:  # noqa: BLE001
        return None


def usage_delta(a: dict | None, b: dict | None) -> dict | None:
    if not a or not b:
        return None
    out = {}
    for k in ("embed", "rerank", "extract"):
        if k in a and k in b:
            out[k] = {f: b[k][f] - a[k][f] for f in b[k] if isinstance(b[k][f], (int, float)) and f != "avg_ms"}
    return out


def conv_questions(conv, types: set[str] | None) -> list[tuple[str, int, dict]]:
    pq = parse_pq(conv["probing_questions"])
    groups = pq.items() if isinstance(pq, dict) else [("?", pq)]
    out = []
    for qtype, qlist in groups:
        if types and qtype not in types:
            continue
        for qi, q in enumerate(qlist if isinstance(qlist, list) else [qlist]):
            out.append((qtype, qi, q))
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
    ap.add_argument("--no-search", action="store_true", help="只入库")
    ap.add_argument("--types", default="", help="只对这几类题 Search，逗号分隔；空=全部")
    ap.add_argument("--parallel", type=int, default=1, help="并行入库的对话数")
    ap.add_argument("--dump", default="")
    args = ap.parse_args()
    types = {t.strip() for t in args.types.split(",") if t.strip()} or None

    df = pd.read_parquet(args.data)
    if args.convos:
        df = df.iloc[: args.convos]
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    client = httpx.Client(base_url=args.base, headers=headers, timeout=300)
    convs = [conv for _, conv in df.iterrows()]
    t_start = time.time()
    usage0 = health_usage(client)
    add_stats = {"adds": 0, "latencies": []}
    lock = threading.Lock()

    def ingest(conv) -> None:
        c = httpx.Client(base_url=args.base, headers=headers, timeout=300)
        user_id = f"replay:{args.tag}:beam:{conv['conversation_id']}"
        n, lat = 0, []
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
                t0 = time.time()
                post_retry(c, "/add", body)
                lat.append(time.time() - t0)
                n += 1
        with lock:
            add_stats["adds"] += n
            add_stats["latencies"].extend(lat)
        print(f"ingested {conv['conversation_id']}: {n} adds, p50 {statistics.median(lat):.1f}s max {max(lat):.1f}s, {time.time() - t_start:.0f}s", flush=True)

    add_report = None
    if not args.skip_add:
        with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
            list(ex.map(ingest, convs))
        usage1 = health_usage(client)
        lat = add_stats["latencies"]
        add_report = {"phase": "add", "adds": add_stats["adds"], "wall_s": round(time.time() - t_start),
                      "p50": round(statistics.median(lat), 2) if lat else None, "p95": round(sorted(lat)[int(len(lat) * 0.95)], 2) if lat else None,
                      "max": round(max(lat), 2) if lat else None, "usage_before": usage0, "usage_after": usage1,
                      "usage_delta": usage_delta(usage0, usage1)}
        print(json.dumps(add_report), flush=True)
        with open(f"replay-{args.tag}.add.json", "w", encoding="utf-8") as f:   # Codex #13 R5：入库用量落盘，不只打印
            json.dump(add_report, f, ensure_ascii=False)
        usage0 = usage1
    if args.no_search:
        return

    dump = open(args.dump, "w", encoding="utf-8") if args.dump else None
    rows: list[dict] = []
    for ci, conv in enumerate(convs):
        user_id = f"replay:{args.tag}:beam:{conv['conversation_id']}"
        for qtype, qi, q in conv_questions(conv, types):
            question = str(q.get("question", ""))
            rubric = q.get("rubric") or q.get("rubrics") or ([q["ideal_response"]] if q.get("ideal_response") else [])
            if not question or not rubric:
                continue
            t0 = time.time()
            r = post_retry(client, "/search", {"query": question, "user_id": user_id, "top_k": args.top_k})
            dt = time.time() - t0
            items = r.json()["data"]
            qid = f"{conv['conversation_id']}:{qtype}:{qi}"
            notes = sum(1 for it in items if "(memory note)" in it["content"] or "(conversation summary)" in it["content"])
            if dump is not None:
                mem_a = [it["content"] for it in items if re.match(r"^\[[^\]]*\]\s+assistant:", it["content"])]
                mem_u = [it["content"] for it in items if it["content"] not in mem_a]
                # context = 返回顺序原样拼接（官方管线优先读这个字段；分栏字段保留给旧用法）。Codex #13 R1：
                # 分栏会把 user 栏整体放在 assistant 栏前面，Search 排好的全局顺序就没了
                dump.write(json.dumps({"id": qid, "question_type": qtype, "question": question,
                                       "rubric_nuggets": [str(x) for x in rubric], "ideal_response": q.get("ideal_response", ""),
                                       "context": "\n".join(it["content"] for it in items),
                                       "speaker_1_name": "user", "speaker_1_memories": "\n".join(mem_u),
                                       "speaker_2_name": "assistant", "speaker_2_memories": "\n".join(mem_a)}, ensure_ascii=False) + "\n")
            rows.append({"qid": qid, "type": qtype, "n": len(items), "notes": notes, "latency": dt,
                         "chars": sum(len(it["content"]) for it in items)})
        print(f"conv {ci + 1}/{len(convs)} searched, {time.time() - t_start:.0f}s, questions so far {len(rows)}", flush=True)
    if dump is not None:
        dump.close()
    usage1 = health_usage(client)

    def summarize(rs: list[dict]) -> dict:
        return {"questions": len(rs), "avg_items": round(statistics.mean(r["n"] for r in rs), 1),
                "avg_notes": round(statistics.mean(r["notes"] for r in rs), 1),
                "avg_chars": int(statistics.mean(r["chars"] for r in rs)),
                "p50_latency": round(statistics.median(r["latency"] for r in rs), 3),
                "max_latency": round(max(r["latency"] for r in rs), 3)}

    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    summary = {"tag": args.tag, "types": sorted(types) if types else "all", "overall": summarize(rows) if rows else None,
               "by_type": {t: summarize(r) for t, r in sorted(by_type.items())}, "usage_delta": usage_delta(usage0, usage1)}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    out = (args.dump + ".summary.json") if args.dump else f"replay-{args.tag}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
