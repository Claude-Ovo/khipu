# Code version and configuration for the fusion A/B (2026-10-01)

Repository: github.com/Claude-Ovo/khipu (local dir aml-memory), branch second-shot.

## Commits
- a5fa22b 2026-10-01 analyze_fusion_pair: exact-turn metrics, changed-question ranks and focus dump for the fusion A/B
- d324cc3 2026-10-01 diag_locomo_rerank_pair: --pair fusion (old vs new fusion rule, both arms reranked, shared query vector)
- 47e590a 2026-10-01 Fusion: entity channel ordered by relevance (temporal/latest keep recency); entity+literal RRF weights halved
- 0c3b857 2026-10-01 Diagnostics: retrieval-only channel replay + offline fusion analyzer; copy tool streams per user
- f1329e4 2026-09-30 Diagnostics: fixes from the pilot verification
- aa2ef01 2026-09-30 deploy-v2: leave Caddy alone when the Caddyfile is unchanged (first Full is under review)
- 1e6f8fb 2026-09-30 Diagnostics: optional search trace (channels, fused order, rerank head/scores/after, pre-box, boxed tokens); paired LoCoMo rerank off/on harness; replay-user copy tool

## Instance under test
- /srv/aml/app2 on Morrow, service aml2, port 8082, database aml2 (copy of the replay users from the evaluated database aml). COMMIT on the server during the A/B: d324cc3.
- The evaluated instance (/srv/aml/app, :8080, database aml) runs 1be823e and was not touched.

## Effective search configuration during the A/B (both arms)
```
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-v4")
EMBED_DIM = _int("EMBED_DIM", 1024)
RERANK_MODEL = os.environ.get("RERANK_MODEL", "gte-rerank-v2")
RERANK_TOPN = _int("RERANK_TOPN", 200)         # 只重排融合后的前 N 条
RERANK_DOC_CHARS = _int("RERANK_DOC_CHARS", 0)  # >0 时只把每条候选的前 N 个字符送去重排（省钱），返回给平台的正文不受影响
RERANK_MIX = _float("RERANK_MIX", 1.0)         # 1.0 = 完全按重排分排；0.5 = 重排分与 RRF 名次各半
HOP_ENABLED = os.environ.get("HOP_ENABLED", "") == "1"
RRF_K = _int("RRF_K", 60)
CHANNEL_TOPN_MULT = _int("CHANNEL_TOPN_MULT", 2)   # 每路取 top_k * 2
NEIGHBOR_CAP_RATIO = _float("NEIGHBOR_CAP_RATIO", 0.05)
BUDGET_TOKENS = _int("SEARCH_BUDGET_TOKENS", 60000)
HARD_TOP_K = 100
RERANK_ENABLED=1 (set per arm by the harness); top_k=100; SEARCH_BUDGET_TOKENS=60000
```
Reranker: gte-rerank-v2 via DashScope; embeddings: text-embedding-v4 (1024-d), one query vector per question shared by both arms.

## The fusion change (commit 47e590a), full diff of app/search.py
```diff
commit 47e590a4da0d57d2949c9a3471f873e9293bab85
Author: ovo-hue <273331359+ovo-hue@users.noreply.github.com>
Date:   Thu Oct 1 06:09:58 2026 +0800

    Fusion: entity channel ordered by relevance (temporal/latest keep recency); entity+literal RRF weights halved
    
    Offline replay of all 1,982 evidenced LoCoMo questions and 200 LME questions (rerank off,
    per-channel hit lists saved) showed the entity channel, which lists every segment naming a
    matched person newest-first from virtual rank 4, pushing older true evidence out of the
    200-candidate rerank window. Reordering that list by best bm25/vector rank and halving the
    entity and literal weights puts all gold turns inside the window for 2010/2171 questions
    instead of 1978 (1 lost, 34 gained), with no LoCoMo category or LME type getting worse.
    Analyzer (tests/analyze_channels.py) reproduces the traced fused order before the change
    and the new code's order after it. Reranker-on effect not yet measured.
    
    Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
    Claude-Session: https://claude.ai/code/session_01ERQKizAxKincZu8uCrC5tS

diff --git a/app/search.py b/app/search.py
index d3eeff7..fccb606 100644
--- a/app/search.py
+++ b/app/search.py
@@ -28,13 +28,17 @@ _INTENT = [
 ]
 
 # 各路在 RRF 里的分量；只有这五路能开门
+# 2026-10-01 实体、字面两路的分量减半（tests/analyze_channels.py 在 LoCoMo 1982 题 + LME 200 题的离线重算上比出来的：
+# 这两路每题几百条、和 bm25/向量大量重叠，原分量等于把同一条证据的分数记两遍，把别的证据挤出重排窗口）
 _WEIGHTS = {
-    "default":   {"bm25": 1.0, "vector": 1.0, "entity": 0.8, "literal": 1.0, "date": 0.5},
-    "temporal":  {"bm25": 1.2, "vector": 0.9, "entity": 0.7, "literal": 1.0, "date": 1.5},
-    "latest":    {"bm25": 1.0, "vector": 1.0, "entity": 1.0, "literal": 0.8, "date": 0.3},
-    "aggregate": {"bm25": 1.0, "vector": 1.0, "entity": 1.4, "literal": 1.0, "date": 0.5},
-    "who":       {"bm25": 1.0, "vector": 1.0, "entity": 1.4, "literal": 1.2, "date": 0.3},
+    "default":   {"bm25": 1.0, "vector": 1.0, "entity": 0.4, "literal": 0.5, "date": 0.5},
+    "temporal":  {"bm25": 1.2, "vector": 0.9, "entity": 0.35, "literal": 0.5, "date": 1.5},
+    "latest":    {"bm25": 1.0, "vector": 1.0, "entity": 0.5, "literal": 0.4, "date": 0.3},
+    "aggregate": {"bm25": 1.0, "vector": 1.0, "entity": 0.7, "literal": 0.5, "date": 0.5},
+    "who":       {"bm25": 1.0, "vector": 1.0, "entity": 0.7, "literal": 0.6, "date": 0.3},
 }
+# 实体路默认按时间倒序（最近提到这个人的段在前）。除了问「最近」和问时间的题，都改成按相关度排：见 _entity_by_relevance
+_ENTITY_KEEPS_RECENCY = ("latest", "temporal")
 _VIRTUAL_RANK = 4  # 实体/字面/日期通道的起始虚拟排名（MemoryConstellations 的做法）
 for _w in _WEIGHTS.values():   # 第二跳两路的分量，只有 HOP_ENABLED 时才会出现在通道表里
     _w.setdefault("bm25_hop", config.HOP_W)
@@ -119,6 +123,19 @@ def _entity_channel(idx: UserIndex, q: str) -> list[int]:
                                        -(idx.rows[p].ts_value.timestamp() if idx.rows[p].ts_value else 0), p))
 
 
+def _entity_by_relevance(channels: dict[str, list[int]]) -> list[int]:
+    """实体路按它在 bm25 / 向量里的最好名次重排；两路都没捞到的接在后面、保持原来的时间倒序。
+    2026-10-01 诊断（collab/诊断-事实与多跳-20260930/fusion-sim/）：LoCoMo 每题都点名说话人，实体路一题几百条、按时间倒序、
+    从虚拟名次 4 起进 RRF，最近几十条提到这个人的段不管相不相关都拿到接近 bm25/向量头名的分数，老的真证据被挤出重排窗口。
+    只改顺序不改成员：LoCoMo 多跳全证据进窗口 203/282 → 219，其余类别和 LME 不掉。"""
+    bm25_rank = {p: i for i, p in enumerate(channels["bm25"])}
+    vec_rank = {p: i for i, p in enumerate(channels["vector"])}
+    ent = channels["entity"]
+    ranked = sorted((p for p in ent if p in bm25_rank or p in vec_rank),
+                    key=lambda p: min(bm25_rank.get(p, 1 << 30), vec_rank.get(p, 1 << 30)))
+    return ranked + [p for p in ent if p not in bm25_rank and p not in vec_rank]
+
+
 def _literal_channel(idx: UserIndex, q: str, bm25_scores: dict[int, float]) -> list[int]:
     terms = [t.lower() for t in literal_terms(q)]
     if not terms:
@@ -398,7 +415,7 @@ async def search(user_id: str, query: str, options: list[str] | None, top_k: int
     channels = {
         "bm25": bm25_hits,
         "vector": vec_hits,
-        "entity": entity_hits,
+        "entity": entity_hits if intent in _ENTITY_KEEPS_RECENCY else _entity_by_relevance({"bm25": bm25_hits, "vector": vec_hits, "entity": entity_hits}),
         "literal": literal_hits,
         "date": date_hits,
     }
```

## The old rule as re-created for the A/B (tests/diag_locomo_rerank_pair.py)
```python
96:ARMS = {"rerank": (("off", False), ("on", True)), "fusion": (("old", True), ("new", True))}
97-# 47e590a 之前的融合规则：实体路一律按时间倒序，实体 / 字面分量是现在的两倍
98:FUSION_NEW = (S._WEIGHTS, S._ENTITY_KEEPS_RECENCY)
99:FUSION_OLD = ({k: {n: (v * 2 if n in ("entity", "literal") else v) for n, v in w.items()} for k, w in S._WEIGHTS.items()},
100-              tuple(S._WEIGHTS))
101-
102-
--
172:                    S._WEIGHTS, S._ENTITY_KEEPS_RECENCY = FUSION_OLD if arm == "old" else FUSION_NEW
173-                else:
174-                    config.RERANK_ENABLED = enabled
175-                trace: dict = {}
```
Checked offline: FUSION_OLD reproduces the fused order traced before the change on 1982/1982 LoCoMo questions; the new code equals analyzer variant V19 on 2182/2182.
