import json
from datetime import datetime

WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse(date):
    day, rest = date.split(" (")
    return datetime.strptime(day + " " + rest.split(") ")[1], "%Y/%m/%d %H:%M")


d = {q["question_id"]: q for q in json.load(open("/srv/aml/data/longmemeval_s.json"))}
gaps = []
out = open("ctx-anchor.jsonl", "w")
for l in open("ctx-utc.jsonl"):
    c = json.loads(l)
    q = d[c["qid"]]
    last = max(parse(x) for x in q["haystack_dates"])
    gaps.append((parse(q["question_date"]).date() - last.date()).days)
    note = {"rank": 0, "id": "anchor", "seq": -1, "part": 1, "session": "", "has_answer_turn": False, "score": 1.0, "tokens": 0,
            "content": "[%s (%s)] system note: this is the date of the most recent conversation stored in memory; "
                       "when a question asks how long ago something happened, count from this date."
                       % (last.strftime("%Y-%m-%d"), WD[last.weekday()])}
    c["returned"] = [note] + c["returned"]
    for i, r in enumerate(c["returned"]):
        r["rank"] = i + 1
    out.write(json.dumps(c, ensure_ascii=False) + "\n")
out.close()
gaps.sort()
from collections import Counter
print("gap days between question date and latest stored session:", Counter(gaps).most_common(8), "max", gaps[-1])
