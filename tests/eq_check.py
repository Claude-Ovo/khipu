"""两个实例逐字节对拍：同样的 Add，逐题比 Search 的完整返回（id、正文、分数、created_at、顺序）。
用途：证明 B3 代码在 EXTRACT_ENABLED 关着时与第二枪候选 full-2-candidate 行为一致。两边都不带百炼 key、不开重排，零花费。
用法：python tests/eq_check.py --a http://127.0.0.1:8093 --b http://127.0.0.1:8094 --token T \
        --locomo /srv/aml/data/locomo10.json --lme /srv/aml/data/longmemeval_s.json --lme-limit 20"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_locomo as lc  # noqa: E402
import replay_longmemeval as lm  # noqa: E402


def users(args) -> list[tuple[str, str, list[list[dict]], list[str]]]:
    """(user_id, session_id, 分块后的消息, 问题) 列表。"""
    out = []
    tag = f"eq-{time.strftime('%m%d%H%M%S')}"
    for ci, conv in enumerate(json.load(open(args.locomo, encoding="utf-8"))):
        msgs = [{k: m[k] for k in ("role", "content", "timestamp")} for m in lc.conversation_messages(conv)]
        out.append((f"{tag}:locomo:{ci}", f"{tag}:locomo-s:{ci}", lc.chunks(msgs), [qa["question"] for qa in conv["qa"]]))
    if args.lme:
        for qi, q in enumerate(json.load(open(args.lme, encoding="utf-8"))[: args.lme_limit]):
            msgs = []
            for sdate, turns in zip(q["haystack_dates"], q["haystack_sessions"]):
                ts = lm.parse_date(sdate)
                msgs.extend({"role": t["role"], "content": t["content"], **({"timestamp": ts} if ts else {})}
                            for t in turns if str(t.get("content", "")).strip())
            out.append((f"{tag}:lme:{q['question_id']}", f"{tag}:lme-s:{qi}", lm.chunks(msgs), [q["question"]]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--locomo", default="/srv/aml/data/locomo10.json")
    ap.add_argument("--lme", default="")
    ap.add_argument("--lme-limit", type=int, default=20)
    args = ap.parse_args()
    h = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    ca = httpx.Client(base_url=args.a, headers=h, timeout=300)
    cb = httpx.Client(base_url=args.b, headers=h, timeout=300)

    n_q = n_diff = n_add = 0
    first_diff = None
    for uid, sid, chs, questions in users(args):
        for k, ch in enumerate(chs):
            body = {"request_id": f"{uid}:chunk-{k}", "user_id": uid, "session_id": sid, "messages": ch}
            ra, rb = ca.post("/add", json=body), cb.post("/add", json=body)
            if ra.status_code != rb.status_code or ra.json() != rb.json():
                print(f"ADD DIFF {uid} chunk {k}: {ra.status_code} {ra.text[:200]} | {rb.status_code} {rb.text[:200]}")
                n_diff += 1
            n_add += 1
        for q in questions:
            for top_k in (100, 10):
                req = {"query": q, "user_id": uid, "top_k": top_k}
                da, db = ca.post("/search", json=req).json(), cb.post("/search", json=req).json()
                n_q += 1
                if da != db:
                    n_diff += 1
                    if first_diff is None:
                        first_diff = (uid, q, top_k, da, db)
        print(f"{uid}: {len(chs)} adds, {len(questions)} questions, diffs so far {n_diff}", flush=True)
    print(f"\nadds {n_add}, searches {n_q}, diffs {n_diff}")
    if first_diff:
        uid, q, top_k, da, db = first_diff
        print(f"first diff: {uid} top_k={top_k} q={q!r}")
        for i, (x, y) in enumerate(zip(da["data"], db["data"])):
            if x != y:
                print(f"  rank {i}:\n    a={json.dumps(x, ensure_ascii=False)[:300]}\n    b={json.dumps(y, ensure_ascii=False)[:300]}")
                break
        else:
            print(f"  lengths {len(da['data'])} vs {len(db['data'])}")
    sys.exit(1 if n_diff else 0)


if __name__ == "__main__":
    main()
