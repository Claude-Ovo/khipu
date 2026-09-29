"""续跑前的线上探针：根因（2100 年以后的时间戳）修好了没有，幂等和 409 还对不对。
用法：python tests/probe_2100.py https://43.128.132.126 <token>"""
import sys
import time

import httpx

base, token = sys.argv[1], sys.argv[2]
c = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=60, verify=True, trust_env=False)
U = f"probe_2100:{int(time.time())}"
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += 0 if cond else 1


body = {"request_id": f"{U}:r1", "user_id": U, "session_id": f"{U}:s",
        "messages": [{"role": "user", "content": "We agreed to meet at the harbour lighthouse at noon.", "timestamp": 4104153600000},
                     {"role": "assistant", "content": "Noted: harbour lighthouse, noon.", "timestamp": 4104153660000}]}
r1 = c.post("/add", json=body)
check(r1.status_code == 200, f"Add with 2100-01-20 timestamps -> 200 (got {r1.status_code} {r1.text[:200]})")
r2 = c.post("/add", json=body)
check(r2.status_code == 200 and r2.json() == r1.json(), "identical re-POST -> same response (idempotent)")
changed = dict(body, messages=[{"role": "user", "content": "different", "timestamp": 4104153600000}])
r3 = c.post("/add", json=changed)
check(r3.status_code == 409, f"same request_id, changed payload -> 409 (got {r3.status_code})")
r = c.post("/search", json={"query": "where do we meet", "user_id": U, "top_k": 100})
items = r.json().get("data", []) if r.status_code == 200 else []
check(r.status_code == 200 and items, f"search finds it (got {r.status_code}, {len(items)} items)")
check(any(str(i.get("created_at", "")).startswith("2100-01-20") for i in items), f"created_at keeps the 2100 date ({[i.get('created_at') for i in items]})")
check(all("date unknown" not in i.get("content", "") for i in items), f"content header has the date ({items[0]['content'][:60] if items else ''!r})")
r = c.post("/search", json={"query": "what happened on 9999-12-31?", "user_id": U, "top_k": 10})
check(r.status_code == 200, f"query with 9999-12-31 -> 200 (got {r.status_code})")
print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
