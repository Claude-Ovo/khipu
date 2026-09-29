"""宽进校验的回归测试（不需要数据库和网络）。
1) 以前合法的请求：新旧模型解析结果、payload_sha 完全一致；
2) 以前会 422 的请求：现在能解析，且结果合理。
用法：python tests/test_lenient_models.py [旧版 commit，默认 33f03a3]"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

OLD = sys.argv[1] if len(sys.argv) > 1 else "33f03a3"


def _models_from(src: str) -> dict:
    start = src.index("MAX_TS_MS =")
    end = src.index("# ---------- 鉴权 ----------")
    ns: dict[str, Any] = {"BaseModel": BaseModel, "Field": Field, "field_validator": field_validator,
                          "model_validator": model_validator, "math": math, "json": json, "re": re,
                          "datetime": datetime, "timezone": timezone, "Any": Any, "__name__": "m"}
    exec("from __future__ import annotations\n" + src[start:end], ns)
    for name in ("Message", "AddRequest", "SearchRequest"):
        ns[name].model_rebuild(_types_namespace=ns)
    return ns


old = _models_from(subprocess.check_output(["git", "show", f"{OLD}:app/main.py"]).decode("utf-8"))
new = _models_from(open("app/main.py", encoding="utf-8").read())


def sha(m) -> str:
    return hashlib.sha256(json.dumps(m.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


fails = 0


def check(cond: bool, msg: str) -> None:
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += 0 if cond else 1


VALID_ADD = [
    {"request_id": "r1", "user_id": "u", "session_id": "s",
     "messages": [{"role": "user", "content": "hello", "timestamp": 1704067200000}]},
    {"request_id": "r2", "user_id": "u", "session_id": "s",
     "messages": [{"role": "assistant", "content": "  hi there  ", "timestamp": 1704067200000.5},
                  {"role": "user", "content": "中文内容 🙂", "timestamp": "1704067200000"},
                  {"role": "user", "content": "no ts"},
                  {"role": "user", "content": "null ts", "timestamp": None},
                  {"role": "user", "content": "zero ts", "timestamp": 0},
                  {"role": "user", "content": "extra field", "name": "Caroline", "id": 7}]},
    {"request_id": "r3", "user_id": "u", "session_id": "s", "extra": 1,
     "messages": [{"role": "user", "content": "Caroline: I went to the LGBTQ support group\nyesterday.", "timestamp": 4102444800000}]},
]
for i, p in enumerate(VALID_ADD):
    a, b = old["AddRequest"].model_validate(p), new["AddRequest"].model_validate(p)
    check(a.model_dump() == b.model_dump() and sha(a) == sha(b), f"valid add #{i}: identical dump and payload_sha")

VALID_SEARCH = [
    {"query": "what did Caroline do", "user_id": "u", "top_k": 100},
    {"query": "q", "user_id": "u", "top_k": 5, "options": ["A. x", "B. y"]},
    {"query": "q", "user_id": "u", "top_k": 5.0},
    {"query": "q", "user_id": "u", "top_k": "10", "options": None},
    {"query": "q", "user_id": "u", "top_k": 250},
]
for i, p in enumerate(VALID_SEARCH):
    a, b = old["SearchRequest"].model_validate(p), new["SearchRequest"].model_validate(p)
    check(a.model_dump() == b.model_dump(), f"valid search #{i}: identical parse {b.model_dump()}")

# 以前会 422 的输入
def old_rejects(model: str, p: dict) -> bool:
    try:
        old[model].model_validate(p)
        return False
    except Exception:  # noqa: BLE001
        return True


BAD_ADD = {
    "empty content": {"role": "user", "content": ""},
    "whitespace content": {"role": "user", "content": "   \n "},
    "null content": {"role": "user", "content": None},
    "list content": {"role": "user", "content": [{"type": "text", "text": "part one"}, {"type": "image_url", "image_url": {"url": "data:"}}]},
    "number content": {"role": "user", "content": 42},
    "dict content": {"role": "user", "content": {"id": "cell_2_9_0", "value": "28"}},
    "list of records": {"role": "user", "content": [{"id": "c1", "value": "a"}, {"id": "c2", "value": "b"}]},
    "system role": {"role": "system", "content": "sys"},
    "missing role": {"content": "no role"},
    "role Human": {"role": "Human", "content": "hi"},
    "ts 1e30": {"role": "user", "content": "x", "timestamp": 1e30},
    "ts negative": {"role": "user", "content": "x", "timestamp": -5},
    "ts nan string": {"role": "user", "content": "x", "timestamp": "nan"},
    "ts ISO": {"role": "user", "content": "x", "timestamp": "2023-05-08T13:56:00Z"},
    "ts LME": {"role": "user", "content": "x", "timestamp": "2023/05/20 (Sat) 02:21"},
    "ts garbage": {"role": "user", "content": "x", "timestamp": "yesterday"},
    "ts bool": {"role": "user", "content": "x", "timestamp": True},
}
for name, msg in BAD_ADD.items():
    p = {"request_id": "r", "user_id": "u", "session_id": "s", "messages": [msg]}
    was = old_rejects("AddRequest", p)
    try:
        m = new["AddRequest"].model_validate(p).messages[0]
        check(True, f"bad add '{name}' (old 422={was}) -> role={m.role} ts={m.timestamp} content={m.content[:20]!r}")
    except Exception as e:  # noqa: BLE001
        check(False, f"bad add '{name}' still rejected: {e}")

for name, p in {"no messages key": {"request_id": "r", "user_id": "u", "session_id": "s"},
                "empty messages": {"request_id": "r", "user_id": "u", "session_id": "s", "messages": []},
                "missing session": {"request_id": "r", "user_id": "u", "messages": [{"role": "user", "content": "x"}]},
                "numeric ids": {"request_id": 5, "user_id": 6, "session_id": 7, "messages": [{"role": "user", "content": "x"}]}}.items():
    try:
        m = new["AddRequest"].model_validate(p)
        check(True, f"bad add '{name}' (old 422={old_rejects('AddRequest', p)}) -> {len(m.messages)} msgs session={m.session_id!r}")
    except Exception as e:  # noqa: BLE001
        check(False, f"bad add '{name}' still rejected: {e}")

m = new["Message"].model_validate({"role": "user", "content": "a\x00b\ud83d"})
check("\x00" not in m.content and m.content.encode("utf-8"), f"NUL and lone surrogate cleaned -> {m.content!r}")
m = new["Message"].model_validate({"role": "user", "content": "x", "timestamp": "2023/05/20 (Sat) 02:21"})
check(m.timestamp == int(datetime(2023, 5, 20, 2, 21, tzinfo=timezone.utc).timestamp() * 1000), "LME timestamp parsed to ms")

for name, p in {"empty query": {"query": "", "user_id": "u", "top_k": 5},
                "missing top_k": {"query": "q", "user_id": "u"},
                "top_k 0": {"query": "q", "user_id": "u", "top_k": 0},
                "top_k garbage": {"query": "q", "user_id": "u", "top_k": "lots"},
                "options with dict": {"query": "q", "user_id": "u", "top_k": 5, "options": ["A", {"B": 1}, None]},
                "null query": {"query": None, "user_id": "u", "top_k": 5}}.items():
    try:
        m = new["SearchRequest"].model_validate(p)
        check(True, f"bad search '{name}' (old 422={old_rejects('SearchRequest', p)}) -> {m.model_dump()}")
    except Exception as e:  # noqa: BLE001
        check(False, f"bad search '{name}' still rejected: {e}")

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
