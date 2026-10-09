"""逐题比较两份检索转储（lme_context_dump.py / b3_locomo.py dump 的 contexts-*.jsonl）：返回的 id 序列和正文是否逐字一致。
用来在只改了正确性、预期结果不变的提交后确认「没改坏」，并把有变化的题挑出来只对它们重新答题（省钱）。
用法：python tests/context_diff.py OLD.jsonl NEW.jsonl [--changed-out changed.qids] [--show 5]
退出码 0 = 全部一致；1 = 有差异（差异本身不是错误，只是提醒要去看）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(p: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in Path(p).read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[str(r["qid"])] = r
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--changed-out", default="", help="把有差异的 qid 一行一个写到这个文件（给 answer_compare --only 用）")
    ap.add_argument("--show", type=int, default=5, help="每类差异最多展示几题")
    a = ap.parse_args()
    old, new = load(a.old), load(a.new)
    common = [q for q in old if q in new]
    only_old = [q for q in old if q not in new]
    only_new = [q for q in new if q not in old]
    same = order_changed = content_changed = 0
    changed: list[str] = []
    examples: dict[str, list[str]] = {"order": [], "content": []}
    for q in common:
        ro, rn = old[q].get("returned", []), new[q].get("returned", [])
        ids_o, ids_n = [x["id"] for x in ro], [x["id"] for x in rn]
        if ids_o == ids_n and [x.get("content") for x in ro] == [x.get("content") for x in rn]:
            same += 1
            continue
        changed.append(q)
        if ids_o != ids_n:
            order_changed += 1
            so, sn = set(ids_o), set(ids_n)
            first = next((i for i, (x, y) in enumerate(zip(ids_o, ids_n)) if x != y), min(len(ids_o), len(ids_n)))
            examples["order"].append(f"{q}: {len(ids_o)}→{len(ids_n)} items, first diff at rank {first + 1}, "
                                     f"dropped {len(so - sn)}, added {len(sn - so)}")
        else:
            content_changed += 1
            k = sum(1 for x, y in zip(ro, rn) if x.get("content") != y.get("content"))
            examples["content"].append(f"{q}: same ids, {k} item(s) with different content")
    print(f"questions: old {len(old)}, new {len(new)}, common {len(common)}, only-old {len(only_old)}, only-new {len(only_new)}")
    print(f"identical: {same}/{len(common)}; order/set changed: {order_changed}; content-only changed: {content_changed}")
    for kind in ("order", "content"):
        for line in examples[kind][: a.show]:
            print("  " + line)
    if a.changed_out:
        Path(a.changed_out).write_text("\n".join(changed) + ("\n" if changed else ""), encoding="utf-8")
        print(f"changed qids -> {a.changed_out} ({len(changed)})")
    sys.exit(1 if changed or only_old or only_new else 0)


if __name__ == "__main__":
    main()
