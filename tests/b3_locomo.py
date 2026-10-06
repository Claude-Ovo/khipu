"""B3 的 LoCoMo 检查（10-06）：LoCoMo 是两个有名字的人对话、多跳题多，笔记挤掉原文的代价在这里可能比 LongMemEval 大。
  ingest：十段对话按平台分块规则经 HTTP Add 进开着抽取的实例（各段并行、段内顺序）。
  dump：  进程内 Search（和 lme_context_dump.py 同一种做法），按类别分层抽题，写成 answer_compare.py 认的上下文格式，
          另附每段对话的两个说话人（answer_compare 的 locomo 模式要 --speakers）和金标证据覆盖。
用法（服务器 /srv/aml/b3 下，先 set -a; . /srv/aml/.env; . /srv/aml/.env2; set +a，DATABASE_URL 指向实验库）：
  python tests/b3_locomo.py ingest --base http://127.0.0.1:8096 --token T
  NOTES_IN_SEARCH=0 python tests/b3_locomo.py dump --out /srv/aml/data/b3-loc/contexts-raw.jsonl"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay_locomo as lc  # noqa: E402

TAG = "b3loc"
PER_CAT = {1: 60, 2: 30, 4: 30}   # multi-hop / temporal / single-hop；open-domain 和 adversarial 不抽
_ID = re.compile(r"#(\d+)\.(\d+)$")


def users(data: list[dict]):
    for ci, conv in enumerate(data):
        yield ci, f"{TAG}:locomo:conv-{ci}", f"{TAG}:sample:{ci}", conv


def ingest(args) -> None:
    import httpx
    data = json.load(open(args.data, encoding="utf-8"))
    client = httpx.Client(base_url=args.base, headers={"Authorization": f"Bearer {args.token}"} if args.token else {}, timeout=300)

    def one(u):
        ci, uid, sid, conv = u
        msgs = [{k: m[k] for k in ("role", "content", "timestamp")} for m in lc.conversation_messages(conv)]
        lat = []
        for k, ch in enumerate(lc.chunks(msgs)):
            body = {"request_id": f"{uid}:chunk-{k}", "user_id": uid, "session_id": sid, "messages": ch}
            for attempt in range(6):
                t0 = time.time()
                r = client.post("/add", json=body)
                if r.status_code != 503:
                    break
                time.sleep(5 * (attempt + 1))
            lat.append(time.time() - t0)
            r.raise_for_status()
        print(f"conv-{ci}: {len(msgs)} msgs, {len(lat)} adds, add p50 {sorted(lat)[len(lat) // 2]:.1f}s", flush=True)

    with ThreadPoolExecutor(10) as ex:
        list(ex.map(one, list(users(data))))
    print("health:", client.get("/health").json().get("usage"), flush=True)


def pick(data: list[dict]) -> list[tuple[int, int, dict]]:
    by_cat: dict[int, list] = {c: [] for c in PER_CAT}
    for ci, conv in enumerate(data):
        for qi, qa in enumerate(conv["qa"]):
            if qa.get("category") in by_cat and any(isinstance(e, str) for e in qa.get("evidence") or []):
                by_cat[qa["category"]].append((ci, qi, qa))
    out = []
    for c, n in PER_CAT.items():
        cands = by_cat[c]
        out.extend(cands[round(i * len(cands) / n)] for i in range(min(n, len(cands))))   # 均匀间隔取样，跨十段对话
    return out


async def dump(args) -> None:
    from app import config, search as S
    from app.db import pool
    from app.httpclient import usage
    db_name = config.DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if db_name == "aml" or not config.RERANK_ENABLED:
        raise SystemExit(f"refusing: db={db_name} rerank={config.RERANK_ENABLED}")
    data = json.load(open(args.data, encoding="utf-8"))
    chosen = pick(data)
    if args.routed_only:   # 只导会触发时间线清单的题（和不开清单的那一臂逐题对照）
        chosen = [c for c in chosen if S.is_ledger_query(c[2]["question"])]
    pool.open()
    meta = {}
    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    print(f"db={db_name} NOTES_IN_SEARCH={config.NOTES_IN_SEARCH} NOTES_MAX_RETURNED={config.NOTES_MAX_RETURNED} "
          f"{len(chosen)} questions", flush=True)
    any10 = all100 = 0
    with open(out, "w", encoding="utf-8") as f:
        for ci, qi, qa in chosen:
            conv = data[ci]
            msgs = lc.conversation_messages(conv)
            dia_to_seq = {m["dia_id"]: i + 1 for i, m in enumerate(msgs)}
            gold = []
            for e in qa.get("evidence") or []:
                if isinstance(e, str):
                    gold += [dia_to_seq[d] for d in (f"D{int(a)}:{int(b)}" for a, b in re.findall(r"D:?(\d+):(\d+)", e)) if d in dia_to_seq]
            gold = sorted(set(gold))
            spk = []
            for m in msgs:
                s = m["content"].split(":", 1)[0]
                if s not in spk:
                    spk.append(s)
            meta[ci] = spk[:2]
            cost = usage.snapshot()["rerank"]["tokens"] * 0.8e-6 + usage.snapshot()["embed"]["tokens"] * 0.5e-6
            if cost >= args.max_cost:
                print(f"STOP: cost cap ¥{args.max_cost}", flush=True)
                break
            items = await S.search(f"{TAG}:locomo:conv-{ci}", qa["question"], None, 100)
            returned = []
            for k, it in enumerate(items, 1):
                m = _ID.search(it["id"])
                seq = int(m.group(1)) if m else None
                returned.append({"rank": k, "id": it["id"], "seq": seq, "has_answer_turn": bool(seq in gold),
                                 "session": "", "content": it.get("content") or ""})
            got = [r["seq"] for r in returned]
            a10 = int(any(s in gold for s in got[:10]))
            a100 = int(set(gold) <= set(got))
            any10 += a10
            all100 += a100
            rec = {"qid": f"c{ci}q{qi}", "conv": ci, "type": lc.CATEGORY.get(qa["category"]), "question": qa["question"],
                   "answer": str(qa.get("answer", "")), "gold": gold, "gold_sessions": [], "returned": returned,
                   "any@10": a10, "all@100": a100,
                   "notes_returned": sum(1 for r in returned if "(memory note)" in r["content"] or "(conversation summary)" in r["content"])}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    Path(str(out) + ".speakers.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    u = usage.snapshot()
    print(f"any@10 {any10}  all@100 {all100}  cost ¥{u['rerank']['tokens'] * 0.8e-6 + u['embed']['tokens'] * 0.5e-6:.2f}", flush=True)
    pool.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("ingest", "dump"))
    ap.add_argument("--data", default="/srv/aml/data/locomo10.json")
    ap.add_argument("--base", default="http://127.0.0.1:8096")
    ap.add_argument("--token", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--max-cost", type=float, default=6.0)
    ap.add_argument("--routed-only", action="store_true")
    args = ap.parse_args()
    if args.cmd == "ingest":
        ingest(args)
    else:
        asyncio.run(dump(args))


if __name__ == "__main__":
    main()
