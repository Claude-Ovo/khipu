"""本地合同冒烟：不打官方额度，先把形状、幂等、隔离、上限、空结果过一遍。
用法：python tests/contract_smoke.py http://127.0.0.1:8080 [token]"""
from __future__ import annotations

import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
H = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
run = uuid.uuid4().hex[:8]
U = f"smoke:{run}:conv-0"
S = f"smoke:{run}:sample:0"
MAY8 = 1683504000000  # 2023-05-08 00:00 UTC

fails = 0


def check(cond: bool, msg: str) -> None:
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


c = httpx.Client(base_url=BASE, headers=H, timeout=60)

r = c.get("/health")
check(r.status_code // 100 == 2, f"health {r.status_code}")

add = {"request_id": f"{U}:chunk-0", "user_id": U, "session_id": S, "messages": [
    {"role": "user", "timestamp": MAY8, "content": "Caroline: I took my cat Milo to the vet yesterday. Dr. Reyes said he needs another shot next month."},
    {"role": "assistant", "content": "That's good to hear. How is Milo feeling now?"},
    {"role": "user", "content": "He's fine. Also, from now on please don't use exclamation marks when you write emails for me."},
]}
r1 = c.post("/add", json=add)
check(r1.status_code == 200, f"add 200 (got {r1.status_code})")
b = r1.json()
check(b.get("success") is True and b.get("request_id") == add["request_id"] and b.get("user_id") == U and b.get("session_id") == S,
      "add echoes success=true + three ids")

r2 = c.post("/add", json=add)
check(r2.status_code == 200 and r2.json() == b, "add is idempotent on replay")

bad = dict(add, messages=add["messages"][:1])
r3 = c.post("/add", json=bad)
check(r3.status_code == 409, f"same request_id with different payload -> 409 (got {r3.status_code})")

r = c.post("/search", json={"query": "When did Caroline take Milo to the vet?", "user_id": U, "top_k": 100})
check(r.status_code == 200, f"search 200 (got {r.status_code})")
d = r.json()
check(isinstance(d, dict) and isinstance(d.get("data"), list), "search returns {data: [...]}")
items = d.get("data", [])
check(0 < len(items) <= 100, f"search returns 1..100 items ({len(items)})")
check(all(isinstance(i.get("id"), str) and i["id"] and isinstance(i.get("content"), str) and i["content"] for i in items),
      "every item has non-empty id and content")
check(any("2023-05-08" in i["content"] for i in items), "content carries the date header")
check(any(i.get("created_at") == "2023-05-08" for i in items), "created_at is date-only when source has no time")
check(any("Caroline:" in i["content"] for i in items), "speaker name in content")
check(any("exclamation" in i["content"] for i in items), "rule slot brings the rule along")
print("   first item:", items[0]["content"][:120] if items else "-")

r = c.post("/search", json={"query": "Which pet does she have?", "user_id": U, "top_k": 2, "options": ["A. a dog", "B. a cat", "C. a parrot"]})
check(r.status_code == 200 and len(r.json()["data"]) <= 2, "top_k is respected as an upper bound")

r = c.post("/search", json={"query": "anything", "user_id": f"{U}:other", "top_k": 100})
check(r.status_code == 200 and r.json() == {"data": []}, "unknown user -> {data: []} (isolation)")

r = c.post("/search", json={"query": "", "user_id": U, "top_k": 100})
check(r.status_code == 422, f"empty query -> 4xx (got {r.status_code})")

t = time.time()
r = c.post("/search", json={"query": "cat vet shot", "user_id": U, "top_k": 100})
print(f"   search latency {time.time() - t:.3f}s")

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
