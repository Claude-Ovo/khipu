"""B3 试跑：挑几道 LongMemEval-S 题，按平台分块规则 Add（同一用户内顺序、不同用户并行），记每次 Add 的耗时，
再每题 Search 一次，统计返回里笔记行的数量和名次，把结果写成 JSONL 供逐题读。花多少钱看实例的 /health。
用法：python tests/b3_trial.py --base http://127.0.0.1:8096 --token T --data /srv/aml/data/longmemeval_s.json \
        --per-type knowledge-update=2,multi-session=2,temporal-reasoning=2,single-session-user=1,single-session-preference=1 \
        --pool 200 --out /srv/aml/data/b3-trial/trial.jsonl"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_longmemeval as lm  # noqa: E402


def pick(data: list[dict], per_type: dict[str, int], pool: int) -> list[tuple[int, dict]]:
    out, seen = [], {t: 0 for t in per_type}
    for qi, q in enumerate(data[:pool]):
        t = q.get("question_type")
        if t in seen and seen[t] < per_type[t]:
            seen[t] += 1
            out.append((qi, q))
    return out


def run_user(client: httpx.Client, tag: str, qi: int, q: dict, top_k: int) -> dict:
    uid, sid = f"{tag}:lme:{q['question_id']}", f"{tag}:sample:{qi}"
    msgs = []
    for sdate, turns in zip(q["haystack_dates"], q["haystack_sessions"]):
        ts = lm.parse_date(sdate)
        msgs.extend({"role": t["role"], "content": t["content"], **({"timestamp": ts} if ts else {})}
                    for t in turns if str(t.get("content", "")).strip())
    lat = []
    for k, ch in enumerate(lm.chunks(msgs)):
        body = {"request_id": f"{uid}:chunk-{k}", "user_id": uid, "session_id": sid, "messages": ch}
        for attempt in range(6):  # 503 是可重试的（和平台一样）
            t0 = time.time()
            r = client.post("/add", json=body)
            if r.status_code != 503:
                break
            time.sleep(5 * (attempt + 1))
        lat.append(time.time() - t0)
        r.raise_for_status()
    t0 = time.time()
    items = client.post("/search", json={"query": q["question"], "user_id": uid, "top_k": top_k}).json()["data"]
    s_lat = time.time() - t0
    note_ranks = [i for i, it in enumerate(items) if "(memory note)" in it["content"] or "(conversation summary)" in it["content"]]
    return {"qi": qi, "question_id": q["question_id"], "type": q["question_type"], "question": q["question"],
            "answer": q.get("answer"), "question_date": q.get("question_date"), "adds": len(lat),
            "add_latency": lat, "search_latency": s_lat, "returned": len(items), "note_ranks": note_ranks,
            "items": items}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--data", default="/srv/aml/data/longmemeval_s.json")
    ap.add_argument("--per-type", required=True)
    ap.add_argument("--pool", type=int, default=200)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--tag", default=time.strftime("b3trial-%m%d%H%M"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    per_type = {k: int(v) for k, v in (p.split("=") for p in args.per_type.split(","))}
    chosen = pick(json.load(open(args.data, encoding="utf-8")), per_type, args.pool)
    print(f"{len(chosen)} questions: {[(qi, q['question_type']) for qi, q in chosen]}", flush=True)
    client = httpx.Client(base_url=args.base, headers={"Authorization": f"Bearer {args.token}"} if args.token else {},
                          timeout=300)
    t0 = time.time()
    with ThreadPoolExecutor(args.parallel) as ex:
        rows = list(ex.map(lambda x: run_user(client, args.tag, x[0], x[1], args.top_k), chosen))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    lat = sorted(x for r in rows for x in r["add_latency"])
    print(f"\nwall {time.time() - t0:.0f}s, adds {len(lat)}, add latency p50 {statistics.median(lat):.1f}s "
          f"p95 {lat[int(len(lat) * 0.95) - 1]:.1f}s max {lat[-1]:.1f}s")
    for r in rows:
        top = r["note_ranks"][:10]
        print(f"[{r['qi']}] {r['type']}: returned {r['returned']}, notes in result {len(r['note_ranks'])}, "
              f"first note ranks {top}, search {r['search_latency']:.1f}s")
    print("health:", client.get("/health").json().get("usage"))


if __name__ == "__main__":
    main()
