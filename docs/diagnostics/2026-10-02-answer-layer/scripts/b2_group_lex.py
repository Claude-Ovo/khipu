"""B2 可行性·免费部分：知识更新题里「旧说法」和「新说法」两条，用词面相似度能不能从同一用户的其它发言里认出来是同一件事。
对每道题：黄金行两两之间的相似度 vs 每条黄金行与所有噪声行（同一 haystack 里其它 user 发言）的相似度。
输出：黄金对的相似度名次（在该黄金行的所有候选里排第几）。名次 1 = 最像它的就是另一条黄金行。"""
import json, math, re
from collections import Counter

STOP = set("""a an the and or but if of to in on at for with by from as is are was were be been being i me my we our you your
he she it its they them their this that these those have has had do does did not no so than then there here what which who
when where how about into over after before up down out just also very can could would should will may might really like
i'm i've i'd it's that's don't didn't can't let's some any much many more most other such only own same too s t""".split())


def toks(s):
    s = s.split("]", 1)[1] if s.startswith("[") else s
    s = re.sub(r"^\s*(user|assistant):", "", s)
    return [w for w in re.findall(r"[a-z0-9']+", s.lower()) if w not in STOP and len(w) > 1]


d = json.load(open("/srv/aml/data/longmemeval_s.json"))
kq = [q for q in d if q["question_type"] == "knowledge-update"][:39]
ranks = []
detail = []
for q in kq:
    gold = set(q["answer_session_ids"])
    rows = []  # (is_gold, text)
    for sid, turns in zip(q["haystack_session_ids"], q["haystack_sessions"]):
        for t in turns:
            if t["role"] != "user":
                continue
            rows.append((sid in gold and bool(t.get("has_answer")), t["content"]))
    docs = [toks(t) for _, t in rows]
    df = Counter()
    for dd in docs:
        df.update(set(dd))
    N = len(docs)
    vecs = []
    for dd in docs:
        tf = Counter(dd)
        v = {w: (1 + math.log(c)) * math.log((N + 1) / (df[w] + 0.5)) for w, c in tf.items()}
        n = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({w: x / n for w, x in v.items()})

    def cos(a, b):
        return sum(x * b.get(w, 0.0) for w, x in a.items())

    gidx = [i for i, (g, _) in enumerate(rows) if g]
    if len(gidx) < 2:
        continue
    qr = []
    for i in gidx:
        sims = sorted(((cos(vecs[i], vecs[j]), j) for j in range(N) if j != i), reverse=True)
        for k, (s, j) in enumerate(sims, 1):
            if j in gidx:
                qr.append(k)
                break
        # 最像的噪声行的相似度 vs 黄金伙伴的相似度
    ranks.extend(qr)
    detail.append((q["question"][:50], len(gidx), N, qr))
ranks.sort()
print("gold rows:", len(ranks), "| partner ranked 1st:", sum(r == 1 for r in ranks), "| within top3:", sum(r <= 3 for r in ranks),
      "| within top10:", sum(r <= 10 for r in ranks), "| median rank:", ranks[len(ranks) // 2], "| worst:", ranks[-1])
for x in detail:
    print(x)
