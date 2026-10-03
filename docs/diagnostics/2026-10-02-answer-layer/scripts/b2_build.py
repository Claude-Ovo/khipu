import json
from datetime import datetime

WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
d = json.load(open("/srv/aml/data/longmemeval_s.json"))
kq = [q for q in d if q["question_type"] == "knowledge-update"][:39]


def hdr(dt):
    return "[%s (%s) %s]" % (dt.strftime("%Y-%m-%d"), WD[dt.weekday()], dt.strftime("%H:%M"))


out = {"oldfirst": open("ctx-oldfirst.jsonl", "w"), "newfirst": open("ctx-newfirst.jsonl", "w")}
for n, q in enumerate(kq, 1):
    gold = set(q["answer_session_ids"])
    rows = []
    seq = 0
    for sid, date, turns in zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]):
        day, rest = date.split(" (")
        dt = datetime.strptime(day + " " + rest.split(") ")[1], "%Y/%m/%d %H:%M")
        for t in turns:
            seq += 1
            if sid in gold and t.get("has_answer"):
                rows.append((dt, seq, {"rank": 0, "id": "oracle#%d" % seq, "seq": seq, "part": 1, "session": sid,
                                       "has_answer_turn": True, "score": 1.0, "tokens": 0,
                                       "content": "%s %s: %s" % (hdr(dt), t["role"], t["content"])}))
    rows.sort(key=lambda x: (x[0], x[1]))
    for arm, rs in (("oldfirst", [dict(r[2]) for r in rows]), ("newfirst", [dict(r[2]) for r in reversed(rows)])):
        for i, r in enumerate(rs):
            r["rank"] = i + 1
        out[arm].write(json.dumps({"n": n, "qid": q["question_id"], "type": q["question_type"], "question": q["question"],
                                   "question_date": q["question_date"], "answer": q["answer"],
                                   "gold": [r["seq"] for r in rs], "gold_sessions": sorted(gold), "returned": rs},
                                  ensure_ascii=False) + "\n")
for f in out.values():
    f.close()
print("questions", len(kq), "rows per q", sorted(len(json.loads(l)["returned"]) for l in open("ctx-oldfirst.jsonl")))
