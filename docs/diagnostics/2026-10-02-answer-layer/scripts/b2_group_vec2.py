import re, json
import numpy as np
exec(open("b2_group_vec.py").read().split("partner_rank = []")[0])
NUM = re.compile(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|forty|fifty|hundred|once|twice|daily|weekly|monthly|every|monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekday|weekend)\b", re.I)
print("tau | numeric questions | pair linked | newest numeric row in group = true new | newest numeric among group & no-noise-newer")
for tau in (0.55,0.60,0.65):
    nq=0; linked=0; ok=0
    for qi, rows in byq.items():
        qrow=[i for i in rows if meta[i].get("is_q")][0]
        if not re.match(r"(how many|how long|how often|how much|what day|how far)", meta[qrow]["text"].lower()): continue
        nq+=1
        cand=[i for i in rows if not meta[i].get("is_q")]; gold=[i for i in cand if meta[i]["gold"]]
        sims=arr[cand]@arr[qrow]; top=[cand[j] for j in np.argsort(-sims)[:100]]
        ctx=list(dict.fromkeys(top+gold)); comps=components(ctx,tau)
        c=[c for c in comps if all(g in c for g in gold)]
        if not c: continue
        linked+=1; c=c[0]
        numeric=[i for i in c if NUM.search(meta[i]["text"])]
        newest=max(numeric,key=lambda i:(pdate(meta[i]["date"]),meta[i]["seq"]))
        true_new=max(gold,key=lambda i:(pdate(meta[i]["date"]),meta[i]["seq"]))
        ok+= newest==true_new
    print(f"{tau:.2f} | {nq} | {linked} | {ok}")
