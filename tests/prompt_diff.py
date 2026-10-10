"""提示词 x1 → x2 的逐窗对照（10-10，主办方回信后加了两句约束）。

从一个已经用 x1 抽过的库（aml_b3）里按 request 重建原文窗，用 x1 的键去缓存里取旧输出，再用当前提示词（x2）
重抽同一窗，逐窗 diff 事实行。只取缓存命中的窗（命中就证明窗重建得一字不差）。新抽的结果以 scope=promptdiff:<user>
写进同一张 extract_cache，不影响线上键。

用法（Morrow，/srv/aml/app2 的代码，库 aml_b3）：
  set -a; . /srv/aml/.env; . /srv/aml/.extract; set +a
  DATABASE_URL=postgresql://aml:aml-local-only@127.0.0.1:5432/aml_b3 \
  /srv/aml/.venv/bin/python tests/prompt_diff.py --old-prompt prompt_x1.txt --old-version x1 --requests 60 --out prompt_diff.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, extract  # noqa: E402


def old_sha(window: str, old_prompt: str, old_version: str) -> str:
    body = extract.request_body(window)
    body["messages"][0]["content"] = old_prompt
    # 旧键（r2 及之前）：版本 + 换行 + 请求体，没有 scope
    return hashlib.sha256((old_version + "\n" + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()


def load_requests(n: int, seed: int) -> list[tuple[str, str, list[dict]]]:
    with db.pool.connection() as conn:
        pairs = conn.execute("SELECT user_id, request_id FROM segments WHERE kind = 'msg' GROUP BY 1, 2").fetchall()
    random.Random(seed).shuffle(pairs)
    out = []
    with db.pool.connection() as conn:
        for user_id, request_id in pairs:
            rows = conn.execute(
                "SELECT role, text, ts_value, part, total FROM segments WHERE user_id = %s AND request_id = %s AND kind = 'msg' "
                "ORDER BY session_id, seq, part", (user_id, request_id)).fetchall()
            if any(r[4] > 1 for r in rows):
                continue  # 长消息被切成几段，原文重建不保证逐字，跳过
            msgs = [{"role": r[0], "content": r[1], "timestamp": int(r[2].timestamp() * 1000) if r[2] else None} for r in rows]
            out.append((user_id, request_id, msgs))
            if len(out) >= n:
                break
    return out


def facts(output: dict) -> list[str]:
    return sorted(it["text"] for it in output.get("items", []) if it.get("kind") == "note")


def summary(output: dict) -> str:
    return " ".join(it["text"] for it in output.get("items", []) if it.get("kind") == "summary")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-prompt", required=True)
    ap.add_argument("--old-version", default="x1")
    ap.add_argument("--new-prompt", default=None, help="用这个提示词重抽（默认当前 SYSTEM_PROMPT）；传 x1 自己就能量同提示词的复跑噪声")
    ap.add_argument("--new-version", default=None)
    ap.add_argument("--scope-prefix", default="promptdiff")
    ap.add_argument("--requests", type=int, default=60)
    ap.add_argument("--max-windows", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    old_prompt = Path(a.old_prompt).read_text(encoding="utf-8")
    if a.new_prompt:
        extract.SYSTEM_PROMPT = Path(a.new_prompt).read_text(encoding="utf-8")
    if a.new_version:
        extract.PROMPT_VERSION = a.new_version
    db.pool.open()
    db.init_schema()   # aml_b3 还没有 r3/r4 加的列
    reqs = load_requests(a.requests, a.seed)
    print(f"requests sampled: {len(reqs)}")
    done = miss = 0
    same = changed = 0
    added_total = removed_total = 0
    with open(a.out, "w", encoding="utf-8") as f:
        for user_id, request_id, msgs in reqs:
            for w in extract.build_windows(msgs):
                if done >= a.max_windows:
                    break
                cached = extract._cache_get(old_sha(w, old_prompt, a.old_version))
                if cached is None:
                    miss += 1
                    continue
                notes = await extract.extract_window(w, scope=f"{a.scope_prefix}:{user_id}")
                new = {"items": [extract.asdict(n) for n in notes]}
                of, nf = facts(cached), facts(new)
                added = sorted(set(nf) - set(of)); removed = sorted(set(of) - set(nf))
                done += 1
                if added or removed:
                    changed += 1
                else:
                    same += 1
                added_total += len(added); removed_total += len(removed)
                f.write(json.dumps({"user_id": user_id, "request_id": request_id, "n_old": len(of), "n_new": len(nf),
                                    "added": added, "removed": removed, "old_summary": summary(cached), "new_summary": summary(new)},
                                   ensure_ascii=False) + "\n")
    u = extract.usage.snapshot()["extract"]
    print(f"windows compared: {done} (old-cache misses skipped: {miss}); identical fact sets: {same}; changed: {changed}; "
          f"facts added: {added_total}, removed: {removed_total}")
    print(f"model calls: {u['calls']} ok={u['ok']} prompt_tokens={u['prompt_tokens']} completion_tokens={u['completion_tokens']}")


if __name__ == "__main__":
    asyncio.run(main())
