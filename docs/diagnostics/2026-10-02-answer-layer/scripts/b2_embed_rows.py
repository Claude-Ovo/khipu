"""B2 可行性·付费部分（估 ¥0.6 封顶）：把 39 道知识更新题 haystack 里所有 user 发言向量化，存成 npy，后面的分组实验离线用。
只向量化一次，不碰线上库。"""
import asyncio, json, os, sys, time
import numpy as np

sys.path.insert(0, "/srv/aml/app2")
from app import config  # noqa: E402
from app.embed import embed_texts  # noqa: E402
from app.httpclient import usage  # noqa: E402

d = json.load(open("/srv/aml/data/longmemeval_s.json"))
kq = [q for q in d if q["question_type"] == "knowledge-update"][:39]
meta, texts = [], []
for qi, q in enumerate(kq):
    gold = set(q["answer_session_ids"])
    seq = 0
    for sid, date, turns in zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]):
        for t in turns:
            seq += 1
            if t["role"] != "user":
                continue
            meta.append({"qi": qi, "qid": q["question_id"], "seq": seq, "sid": sid, "date": date,
                         "gold": bool(sid in gold and t.get("has_answer")), "text": t["content"]})
            texts.append(t["content"])
    meta.append({"qi": qi, "qid": q["question_id"], "seq": -1, "sid": "", "date": "", "gold": False, "text": q["question"], "is_q": True})
    texts.append(q["question"])
est_tokens = sum(len(t) // 4 for t in texts)
print("rows", len(texts), "est tokens", est_tokens, "est yuan", round(est_tokens / 1e6 * 0.5, 3), flush=True)
if est_tokens / 1e6 * 0.5 > 0.8:
    raise SystemExit("estimate above cap, not running")
t0 = time.time()
vecs = asyncio.run(embed_texts(texts))
missing = sum(v is None for v in vecs)
print("done in", round(time.time() - t0, 1), "s; missing", missing, "; usage:", usage.snapshot() if hasattr(usage, "snapshot") else "n/a", flush=True)
arr = np.array([v if v is not None else [0.0] * config.EMBED_DIM for v in vecs], dtype=np.float32)
np.save("/srv/aml/data/b2/rows.npy", arr)
with open("/srv/aml/data/b2/rows.meta.jsonl", "w") as f:
    for m in meta:
        f.write(json.dumps(m, ensure_ascii=False) + "\n")
