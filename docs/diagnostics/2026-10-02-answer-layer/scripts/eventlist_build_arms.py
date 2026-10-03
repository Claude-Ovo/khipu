"""事件清单实验：从 A4 新臂上下文里取 20 题，造两组：base = 原样 100 条；eventlist = 清单放第 1 条，后面原样 100 条。"""
import json, os, sys
D = "/srv/aml/data/a4"
sel = json.load(open(f"{D}/eventlist/selection.json"))
qids = sel["wrong"] + sel["right"]
ctx = {}
for l in open(f"{D}/contexts-new.jsonl"):
    o = json.loads(l)
    if o["qid"] in qids:
        ctx[o["qid"]] = o
base = open(f"{D}/eventlist/ctx-base.jsonl", "w")
ev = open(f"{D}/eventlist/ctx-eventlist.jsonl", "w")
n_lines = {}
for q in qids:
    c = ctx[q]
    base.write(json.dumps(c, ensure_ascii=False) + "\n")
    md = open(f"{D}/eventlist/out/{q}.md", encoding="utf-8").read().strip()
    lines = [x for x in md.splitlines() if x.startswith("- [")]
    n_lines[q] = len(lines)
    note = {"rank": 0, "id": "eventlist", "seq": -1, "part": 1, "session": "", "has_answer_turn": False, "score": 1.0, "tokens": 0,
            "content": "[event list] Index of events found in the memories below, with the row each one comes from (rows are numbered in order):\n" + "\n".join(lines)}
    d = dict(c)
    d["returned"] = [note] + [dict(r, rank=r["rank"] + 1, content=f"(row {r['rank']}) " + r["content"]) for r in c["returned"]]
    ev.write(json.dumps(d, ensure_ascii=False) + "\n")
base.close(); ev.close()
print("questions", len(qids), "event lines per q:", n_lines)
