"""中转站体检（花费不到一分钱）：在把 B3 抽取接到一个 OpenAI 兼容中转之前，先看它背后是不是真的 gpt-4o-mini。
1) 回复里报的模型名（自己报的，只能当线索）；
2) prompt_tokens 和本地 o200k_base（gpt-4o 系分词器）按 chat 格式算出来的数对不对得上——背后换成 Qwen / DeepSeek / Claude
   之类别家模型时分词器不同，这个数会明显对不上；cl100k 的数一并打印，换成老一代 OpenAI 模型也看得出来；
3) 同一个请求（temperature 0、固定 seed）发两次，回复是否一致、system_fingerprint 是否一致；
4) json_object 输出能不能解析。
它挡不住「同家族换个更小的模型」（gpt-4.1-nano 之类分词器一样），那要靠价格和用量账单对照。
key 从环境变量读，不打印。用法：EXTRACT_API_KEY=… EXTRACT_BASE_URL=https://aihubmix.com/v1 python tests/probe_relay.py"""
from __future__ import annotations

import json
import os
import sys

import httpx
import tiktoken

KEY = os.environ.get("EXTRACT_API_KEY", "")
BASE = os.environ.get("EXTRACT_BASE_URL", "https://aihubmix.com/v1").rstrip("/")
MODEL = os.environ.get("EXTRACT_MODEL", "gpt-4o-mini-2024-07-18")

MESSAGES = [
    {"role": "system", "content": "You extract facts. Reply with a JSON object only."},
    {"role": "user", "content": (
        "[1] 2023-05-20 (Sat) 14:02 Caroline: I adopted a puppy named Max yesterday! He cost $350 and he's a corgi mix.\n"
        "[2] 2023-05-20 (Sat) 14:03 Melanie: 太好了！我上个月在福州也领养了一只猫，叫 Luna，三岁。\n"
        "[3] 2023-05-20 (Sat) 14:05 Caroline: We should do a pet playdate on June 3rd at Riverside Park.\n"
        'Return {"facts": ["..."], "count": <number of pets mentioned>}.')},
]


def chat_tokens(enc) -> int:
    """OpenAI cookbook 的 gpt-4o 系计数：每条消息 3 + role + content，回复引导再 + 3。"""
    return sum(3 + len(enc.encode(m["role"])) + len(enc.encode(m["content"])) for m in MESSAGES) + 3


def main() -> int:
    if not KEY:
        print("EXTRACT_API_KEY is empty")
        return 2
    body = {"model": MODEL, "messages": MESSAGES, "temperature": 0, "seed": 7, "max_tokens": 200,
            "response_format": {"type": "json_object"}}
    c = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {KEY}"})
    replies = []
    # 第 0 次不带 json 模式、只要 1 个输出 token，专门对分词器（json 模式会不会多算提示词没有官方说法，别让它干扰这一项）
    plain = {k: v for k, v in body.items() if k != "response_format"} | {"max_tokens": 1}
    for i, b_ in enumerate((plain, body, body)):
        r = c.post(f"{BASE}/chat/completions", json=b_)
        if r.status_code != 200:
            print(f"call {i}: HTTP {r.status_code} {r.text[:300]}")
            return 1
        replies.append(r.json())
    p0, *replies = replies
    want_o200k = chat_tokens(tiktoken.get_encoding("o200k_base"))
    want_cl100k = chat_tokens(tiktoken.get_encoding("cl100k_base"))
    fails = 0

    def check(ok: bool, msg: str) -> None:
        nonlocal fails
        print(("ok   " if ok else "FAIL ") + msg)
        fails += 0 if ok else 1

    a, b = replies
    model = a.get("model")
    check(isinstance(model, str) and "gpt-4o-mini" in model, f"reported model: {model!r}")
    pt = (p0.get("usage") or {}).get("prompt_tokens")
    check(isinstance(pt, int) and abs(pt - want_o200k) <= 1,
          f"prompt_tokens {pt} vs o200k (gpt-4o family) {want_o200k}; cl100k would be {want_cl100k}"
          + ("" if not isinstance(pt, int) or abs(pt - want_o200k) <= 1 else
             "  <- a different tokenizer, or the relay adds its own hidden prompt"))
    ca, cb = (x["choices"][0]["message"].get("content") or "" for x in replies)
    check(ca == cb, "same request twice -> same reply" + ("" if ca == cb else f"\n     1: {ca[:200]}\n     2: {cb[:200]}"))
    fa, fb = a.get("system_fingerprint"), b.get("system_fingerprint")
    print(f"info system_fingerprint: {fa!r} / {fb!r}")
    try:
        parsed = json.loads(ca)
        check(isinstance(parsed, dict) and "facts" in parsed, f"json_object parsed: {json.dumps(parsed, ensure_ascii=False)[:300]}")
    except ValueError:
        check(False, f"json_object did not parse: {ca[:200]}")
    u = a.get("usage") or {}
    cost = (u.get("prompt_tokens") or 0) * 0.15e-6 + (u.get("completion_tokens") or 0) * 0.60e-6
    print(f"info usage per call: {u}; at official price ≈ ${cost:.6f}")
    print(f"\nFAILS: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
