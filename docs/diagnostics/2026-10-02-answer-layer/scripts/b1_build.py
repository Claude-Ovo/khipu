import json
from datetime import datetime, timedelta

WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
d = json.load(open("/srv/aml/data/longmemeval_s.json"))
tq = [q for q in d if q["question_type"] == "temporal-reasoning"][:66]


def hdr(dt):
    return "[%s (%s) %s]" % (dt.strftime("%Y-%m-%d"), WD[dt.weekday()], dt.strftime("%H:%M"))


out = {"utc": open("ctx-utc.jsonl", "w"), "plus8": open("ctx-plus8.jsonl", "w")}
shift_rows = 0
shift_q = 0
nrows = 0
for n, q in enumerate(tq, 1):
    gold = set(q["answer_session_ids"])
    rows = {"utc": [], "plus8": []}
    seq = 0
    moved = False
    for sid, date, turns in zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]):
        day, rest = date.split(" (")
        dt = datetime.strptime(day + " " + rest.split(") ")[1], "%Y/%m/%d %H:%M")
        for t in turns:
            seq += 1
            if sid in gold and t.get("has_answer"):
                for arm, x in (("utc", dt), ("plus8", dt + timedelta(hours=8))):
                    rows[arm].append({"rank": 0, "id": "oracle#%d" % seq, "seq": seq, "part": 1, "session": sid,
                                      "has_answer_turn": True, "score": 1.0, "tokens": 0,
                                      "content": "%s %s: %s" % (hdr(x), t["role"], t["content"])})
                nrows += 1
                if (dt + timedelta(hours=8)).date() != dt.date():
                    shift_rows += 1
                    moved = True
    shift_q += moved
    for arm in out:
        rs = sorted(rows[arm], key=lambda r: r["seq"])
        for i, r in enumerate(rs):
            r["rank"] = i + 1
        out[arm].write(json.dumps({"n": n, "qid": q["question_id"], "type": q["question_type"], "question": q["question"],
                                   "question_date": q["question_date"], "answer": q["answer"],
                                   "gold": [r["seq"] for r in rs], "gold_sessions": sorted(gold), "returned": rs},
                                  ensure_ascii=False) + "\n")
for f in out.values():
    f.close()
print("questions", len(tq), "gold rows", nrows, "rows whose date moves under +08:", shift_rows, "questions affected:", shift_q)
print("empty-gold questions:", sum(1 for l in open("ctx-utc.jsonl") if not json.loads(l)["returned"]))
