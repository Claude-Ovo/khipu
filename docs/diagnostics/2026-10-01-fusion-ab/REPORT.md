# LoCoMo multi-hop: full relaxed audit + offline fusion simulation (2026-10-01 00:2x)

Cost: ¥0. No API calls, no server access. Inputs are the local copies in `../data/` (md5-checked against the server on 09-30).

## 1. Full relaxed audit of all 137 missing gold items (90 questions)

Six read-only Claude agents, 15 questions each, same rubric as the Codex audit (valid / wrong / uncertain), blind to Codex's verdicts. Each also judged whether the fact the item should contribute is already stated in some *other* returned top-100 turn, and whether the question's gold answer is derivable from the returned top-100 as a whole. Packets the agents saw: `packets/shard*.txt`. Output: `../relaxed-audit-claude.jsonl` (137 item lines + 90 question lines).

| | count |
|---|---|
| items: valid / wrong / uncertain | 109 / 21 / 7 |
| items: fact already in returned top-100 (yes / partial / no) | 73 / 36 / 28 |
| questions answerable from returned top-100 (yes / partial / no) | 54 / 33 / 3 |

- Label agreement with Codex on its 30 overlapping items: **26/30**. The 4 disagreements are borderline (a sympathetic echo turn, a photo-only turn, image-metadata-only facts).
- Coverage, 280 questions: exact gold-ID all@100 = 190 (0.679). Counting a question as covered when its answer is fully derivable from what was returned: **244 (0.871)**. 33 more are partial; 3 get nothing: (3,34), (3,42) Little Women / LOTR, and (4,11) surfing.
- Several gold answers carry components that exist only in the hidden image `query` metadata or nowhere in the conversation (e.g. "A Court of Thorns and Roses", "car museum", "transgender symbol", "yellow" guitar). Those were judged against the supportable part.
- Caveat: these are LLM judgments, one pass, no second annotator except the 30-item overlap with Codex.

**Consequence for the 200→300 window experiment.** The 32 questions whose missing gold all sit at fused ranks 201–300 (the ceiling of what a 300 window can fix) break down as 18 already answerable and 14 partial. At the observed rerank conversion (below), the realistic gain is a handful of partial questions, while every Full Search pays +50% rerank tokens. **Recommendation: do not run it now.**

## 2. Offline fusion simulation

Script: `sim_fusion.py`. It rebuilds `app/search.py` `_rrf` + `_both_first` from the per-channel hit lists saved in the rerank-on trace. **V0 reproduces the trace's fused order exactly on 280/280 questions.** Everything below changes only the pre-rerank order; rerank scores are not simulated.

Rerank conversion observed in the same trace (share of gold at a fused-rank band that ends in the final top-100): 1–50 0.99, 51–100 0.89, 101–150 0.89, 151–175 0.91, 176–200 0.75. So "all gold inside the 200 window" is a reasonable proxy for "all gold returned".

873 gold items over 280 questions; 9 are in no channel.

| variant | gold@100 | gold@200 | all-gold-in-window@200 (Q) | vs V0 (lost / gained) |
|---|---|---|---|---|
| V0 current | 584 | 758 | 202 | — |
| V1 no both-first | 613 | 758 | 202 | 0 / 0 |
| V3 equal weights, no both-first | 609 | 761 | 204 | 1 / 3 |
| V4 rank-space weights (Hindsight §6 item 7) | 608 | 761 | 204 | 0 / 2 |
| V6 bm25 + vector only | 677 | 786 | 218 | 1 / 17 |
| V7 drop entity channel | 596 | 778 | 214 | 2 / 14 |
| V8 drop literal channel | 595 | 761 | 205 | 2 / 5 |
| V11 entity + literal weight ×0.5 | 597 | 775 | 213 | 1 / 12 |
| **V14 entity channel ordered by relevance** | 604 | **790** | **221** | **1 / 20** |

**Root cause.** `_entity_channel` returns *every* segment that mentions a matched person (median 406 rows per question in LoCoMo, where every question names a speaker) sorted **newest first**, and it enters RRF at virtual rank 4. The most recent ~50 segments mentioning "Caroline" therefore get a bonus comparable to a top bm25/vector hit, whether or not they are relevant, and older true evidence is pushed past 200. Evidence for the mechanism:
- Weight changes barely move the window (V3/V4 ≈ V0). The problem is not weight scale.
- Capping the entity list at 100 makes it *worse* (V10: 190), capping at 50 better (V9: 211). This non-monotone behaviour fits an ordering that is unrelated to relevance.
- The entity channel contains 848/873 gold but holds only 3 gold that no other channel has. As a door it adds almost nothing; as a recency-ordered bonus it does harm.

V14 keeps the channel and its membership and changes only its order: rows that bm25/vector also retrieved are sorted by their best rank there (the literal channel already sorts by bm25 score), and the rest keep recency order after them. For intent `latest`, recency order is kept.

**Against the relaxed audit.** Look at the questions that still have a valid, uncovered missing fact (42 questions). In V0 the real-gap items all fall inside the 200 window for 9 of them; in V14 that rises to 19.

`both-first` (hit by both bm25 and vector → placed before everything else) does not change window membership here. It only reshuffles inside the top 200 (gold@100 584 → 613 without it). It matters only if the rerank window is cut below the both-set size.

## 3. What is not known

- **Only LoCoMo category 1 was tested.** The entity channel was tuned for `who` / `aggregate` / `latest` questions. V14 must be checked on the other LoCoMo categories and on LME before it ships, and so must any other variant.
- The rerank scores of segments newly pulled into the window are not simulated. The conversion rates above come from segments that fusion already liked.
- The metric is retrieval only. Answer-level impact needs a closed loop.

## 4. Proposed next step (near-free, needs the server)

Run a retrieval-only replay with rerank **off**, trace on, over all LoCoMo categories (1,986 questions) and the LME dev 200. Save the per-channel lists and simulate V0 against V14 offline, the same way as here. The only paid calls are query embeddings: about 2.2k queries × ~30 tokens ≈ 70k tokens, well under ¥0.1. The replay runs against `aml2` / `:8082` code in-process and never touches `:8080`. If V14 holds up (no category loses coverage), implement it (a few lines in `_entity_channel`). After that, a single paired rerank run on the 280 multi-hop questions (≈ ¥3) will confirm the window gain end to end.


---

## 5. Full-category replay and the chosen rule (2026-10-01 05:3x–06:1x, ¥0.015)

Ran the retrieval-only replay proposed in §4 (`tests/diag_channels_replay.py`, rerank off, trace on, in-process on the `aml2` instance; `:8080` untouched): all 1,982 evidenced LoCoMo questions (categories 1–5) plus the first 200 LongMemEval-S questions (LME users copied from `aml` into `aml2` first). Cost: 26k + 3k query-embedding tokens, ¥0.0147. Files: `data/channels-locomo.jsonl`, `data/channels-lme.jsonl`. The offline analyzer (`tests/analyze_channels.py`) reproduces the traced fused order on all **2182/2182** replayed questions before any variant is applied. Coverage below is over the **2171** questions that have at least one gold turn: the 11 LME abstention questions (`*_abs`) have none and are excluded from every count.

Metric: all gold turns inside the rerank window (fused rank ≤ 200), paired per question against V0 (lost / gained). LoCoMo categories: 1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop, 5 adversarial. LME: turn-level; session-level is 189/189 for every variant so it is not shown.

| variant | LoCoMo 1 (282) | 2 (321) | 3 (92) | 4 (841) | 5 (446) | LME (189) | total | lost / gained |
|---|---|---|---|---|---|---|---|---|
| V0 current | 203 | 308 | 67 | 816 | 397 | 186 | 1977 | — |
| V11 entity+literal weight ×0.5 | 214 | 309 | 69 | 817 | 406 | 186 | 2001 | 1 / 25 |
| V14 entity ordered by relevance | 222 | 306 | 70 | 816 | 402 | 186 | 2002 | 7 / 32 |
| V17 V14 + weights ×0.5 | 220 | 308 | 69 | 819 | 407 | 186 | 2009 | 4 / 36 |
| **V19 V17, but temporal/latest keep recency order** | **219** | **310** | **69** | **819** | **407** | **186** | **2010** | **1 / 34** |
| V22 blended entity order min(rel, 5·recency) + weights ×0.5 | 220 | 309 | 69 | 818 | 407 | 186 | 2009 | 2 / 34 |

Split check (LoCoMo conv 0–4 vs conv 5–9, `--split`): V19 gains on both halves in every category where it moves at all (1a +12/−0, 1b +5/−1, 2a +1, 2b +1, 4a +1, 4b +2, 5a +8, 5b +2). The single loss is one multi-hop question in conv 5–9.

Without rerank, gold in the final top-100 (turn_all@100) moves 1691 → 1744 under V19 (1 lost / 53 gained); LME multi-session 74 → 79.

**What the V14 losses were.** All seven are questions whose gold turn sat at fused rank 143–188 under V0 only because the entity channel's recency order happened to put it at entity rank 1–22 (vector rank 96–148, no bm25 hit); relevance ordering drops them to 205–284. Five of the seven are temporal-intent questions. Keeping recency order for `temporal` and `latest` intents (V19) removes those; this gate was chosen after seeing the losses, so it is a fitted choice, checked only by the conv-half split above.

**Shipped as commit (second-shot):** `_WEIGHTS` entity and literal halved for every intent; `_entity_by_relevance` reorders the entity list by best bm25/vector rank (unranked rows keep recency order after them) for all intents except `temporal` and `latest`. Checked offline that the new code's fusion equals analyzer V19 on 2182/2182 questions. Not yet verified with the reranker on.

## 6. Remaining verification (paid)

A paired run on the 280 multi-hop questions with rerank **on**, same harness as 09-30 (`tests/diag_locomo_rerank_pair.py`, ≈ ¥3), against the 09-30 file: this is the only way to see whether the 16 newly-windowed questions survive the reranker, and whether anything inside the window got worse. Answer-level effect still needs a closed loop after that.


**Count reconciliation (asked 10-01 06:1x).** Baseline is **1977**, not 1978: the first analyzer pass collapsed LME segment parts to their `seq` and double-counted split messages; the rerun stores `seq.part` keys, and with it V0 on LME multi-session is 92/95, total 1977. 1977 − 1 + 34 = 2010. The multi-hop delta is +16 net = 17 gained − 1 lost.

## 7. The 17 gained and 1 lost multi-hop questions, against the relaxed audit (no new run)

For each question whose window status changes under the new fusion: the gold turn(s) that were outside the window on 09-30, their new fused rank, and what the 137-item audit (§1) said about the fact they carry. "Fact already returned" = the audit found the same fact stated in another turn of the 09-30 top-100.

| group | questions | detail |
|---|---|---|
| Gained, but 09-30 already had every gold turn in the top-100 (all@100 = 1) | 2 | (4,36) D12:20 271→143; (7,6) D1:8 205→183. Window change is nominal. |
| Gained, missing turn valid, fact already returned (audit = yes) | 8 | (0,4) D1:5 [audit **uncertain**: caption-only]; (0,15) D1:12; (0,55) D14:5; (1,18) D6:8; (3,75) D3:4; (4,18) D4:7; (6,8) D1:12; (9,60) D4:24. Precise-ID gain only; the answer was already derivable. |
| Gained, missing turn valid, fact only partly returned (audit = partial) | 5 | (0,7) D2:14 208→152; (3,79) D12:13/14 236/260→177/195; (4,14) D2:1 210→166; (7,1) D6:4 247→164 (friend passed away); (7,82) D2:13 208→141 (yoga at mother's old home). |
| Gained, missing turn valid, fact absent from the 09-30 top-100 (audit = no) | 2 | (4,7) "Which geographical locations has Tim been to?" D14:16 206→169 (Tim: trip to the **Smoky Mountains**); (4,21) "Which US cities does John mention visiting to Tim?" D6:3 233→173 (John: **Chicago**). These are the two where a real answer fact can only come from the new window. (A chat message on 10-01 06:2x ran the two together as "(4,7) 和 (4,21) 的 Chicago"; that was sloppy wording, not a different finding: (4,7) has always been Smoky Mountains, see §1 shard-0 notes and the audit item at source line 27 of the relaxed audit.) |
| Mislabelled (audit = wrong) among the gained | 0 | — |
| **Lost** | 1 | (8,77) "How does Evan spend his time with his bride after the wedding?" (intent temporal). D24:9 fused 188→216; on 09-30 it was returned at rank 98. D24:9 is where Evan first mentions snowshoeing, one of the answer's components; Sam's echo D24:10 also names it. Real potential loss of one fact component, pending the rerank run. |

So of 17 gained, at most 7 (5 partial + 2 absent) can add an answer fact; 10 are precise-ID only. Whether the 7 actually reach the returned top-100 is what the rerank run decides.

## 8. How the fusion A/B with the reranker on would run, and what it costs

`tests/diag_locomo_rerank_pair.py --pair fusion`: same harness as 09-30, same 282 multi-hop questions, same server-side library, one query vector per question shared by both arms; arm `old` = weights before 47e590a and recency-ordered entity list (checked offline to reproduce the 09-30 traced order on 1982/1982), arm `new` = current code. Both arms rerank with gte-rerank-v2, window 200, top_k 100. Per question the file keeps both arms' channel lists, fused order, rerank window/scores and final list, so the exact-turn metrics and the audit mapping in §7 can both be produced from it.

Cost: the 09-30 run reranked 3.75M tokens for one arm (¥3.00). Two arms ≈ 7.5M tokens ≈ **¥6.0** plus a few fen of embeddings; `--max-cost 7`. A cheaper variant (new arm only, ¥3, compared against the 09-30 `on` arm) is not strictly paired: the query vector would be recomputed and the reranker has shown run-to-run drift (13/200 on LME), so a 1–2 question difference could not be attributed.

Reporting plan for that run: exact all-hop recall@10/@100 for both arms with paired counts; for every question that changes, the §7 mapping (fact already returned / partly / absent / mislabelled), read from the returned text, not from IDs. Scope: these 280 diagnostic questions only; not a generalisation test, and no claim about answer accuracy.


---

## 9. Fusion A/B with the reranker on (2026-10-01 06:25–06:28, ¥5.99)

Run exactly as §8: `diag_locomo_rerank_pair.py --pair fusion --max-cost 7`, code d324cc3 deployed to `app2`/`:8082` (`:8080` and Caddy untouched), 282 multi-hop questions, one query vector per question shared by both arms, both arms reranked by gte-rerank-v2 with window 200, top_k 100. 564 rerank calls, all ok, 0 timeouts, 7.49M rerank tokens. Files: `data/fusion-pair.jsonl` (63 MB, both arms' full traces), `data/fusion-pair.jsonl.summary-0.json`, `data/fusion-pair.log`, `fusion-pair-analysis.txt` (output of `tests/analyze_fusion_pair.py`). 280 questions scored after excluding the 2 with non-existent gold, as on 09-30.

### 9a. Exact-turn metrics (all gold turns of the question inside the returned list)

| metric | old fusion | new fusion | only old | only new | exact McNemar p |
|---|---|---|---|---|---|
| all-hop recall@10 | 87 | 88 | 1 | 2 | 1.0 |
| all-hop recall@100 | 190 | 197 | 4 | 11 | 0.12 |
| gold turns in top-100 | 737/874 | 751/874 | | | |
| all gold inside window (200) | 202 | 218 | | | |

The old arm reproduces the 09-30 rerank-on result (190/280). The +7 net at @100 is not significant at the usual threshold (exact p = 0.12). **No repeated run was made, so the reranker's run-to-run variation on this set has not been measured here**; the 13/200 figure from LME (09-27) is a different dataset and configuration and is not used as a noise estimate for this run.

### 9b. What the changed questions actually gain or lose (read from the returned text, not IDs)

**Title-list difference asked for:** the "16" in §5 was the net change in multi-hop window membership (+17 − 1); the 17 gained and 1 lost were listed in §7. In the reranked run 11 of the 17 window-gains became all@100 gains; the other 6 did not: (4,36) and (7,6) were already complete in both arms, (0,4), (0,15), (1,18) had their newly-windowed turn pushed back out by the reranker, and (7,82) D2:13 (yoga at her mother's old home) reached fused rank 141 but the reranker left it outside the top-100 in both arms.

Gained, real answer fact added (6). Speaker of the added turn matches the question's subject in every case:

| q | turn now returned (new rank) | fact | equivalent already in the old top-100? |
|---|---|---|---|
| (0,7) Caroline's relationship status | D2:14 Caroline, #94 | "as a single parent" | no; old had only D3:13 "after that tough breakup" (weak) |
| (3,79) how many screenplays | D12:13 Nate "Is that your third one?" #36 + D12:14 Joanna "Yep!" #19 | the count "three" | no; old had D12:2 "just finished something" only |
| (4,7) places Tim has been | D14:16 Tim, #13 | Smoky Mountains | no |
| (4,14) Tim's writing | D2:1 Tim, #42 | forum talk about favourite books | partial (D4:3 mentions the forum only) |
| (4,21) US cities John visited | D6:3 John, #13 | Chicago | no |
| (7,1) who passed away | D6:4 Deborah, #8 | "I lost a friend last week" | partial; D6:8 (Karlie photo, #17/#20) implied it |

Gained, precise-ID only (5): (0,55), (3,75), (4,18), (6,8), (9,60). The audit had already found the same fact in another returned turn; nothing new for the answer.

Lost (4):

| q | turn dropped (old rank → new) | cause | fact effect |
|---|---|---|---|
| (8,77) Evan after the wedding | D24:9 Evan, #98 → out of window (fused 188 → 216) | **the fusion change** | real: "tried snowshoeing this weekend", one of five answer components. Sam's D24:10 also says snowshoeing but is not Evan's statement and is not returned in either arm, so it is not counted as a substitute. |
| (3,83) skills Nate taught | D18:8 Nate, #100 → #101+ | identical reranker score (0.1978) in both arms; new window brought two competitors above it | possible loss of the "teaching dairy-free dessert" component; D26:12 and D14:16 (gaming) stay at #3 and #2 |
| (3,55) what inspires Joanna | D4:6, #96 → out | identical score (0.1935) displaced at the cutoff | none: D4:6 is "always up for something sweet", a mislabelled evidence turn; the other five gold turns are returned in both arms |
| (4,38) country Tim visits most | D13:1 Tim (UK conference), #34 → #104 | the reranker returned different scores for the same query and the same text in the two arms (0.1880 vs 0.1752). **Cause not determined.** The two calls differ in their candidate sets, and in this run the candidate set is itself a product of the fusion change, so this is not attributed to the reranker alone and is not excluded as an effect of the change. | partial: D1:18 (London) and D18:1 stay at #5 and #9 |

So, by facts rather than IDs: 6 questions gain an answer fact; 1 loses one component because its turn left the window; 1 loses a component at the rank-100 boundary with an identical score; 1 loses a fact because the reranker scored the same text differently, cause undetermined; 1 is a mislabel.

### 9c. Conclusion (limited to these 280 diagnostic questions; corrected 10-01 06:4x after review)

Observed: the new fusion puts more gold turns inside the window (202 → 218) and, after reranking, changes the outcome of 15 questions: 11 gained and 4 lost by the exact-turn measure; by facts read from the text, 6 questions gain an answer fact and 3 lose one (one because the turn left the window, one at the rank-100 cutoff, one with a changed reranker score of undetermined cause), plus 5 nominal gains and 1 mislabel.

Not established: (a) a stable improvement, since the paired difference is not significant (p = 0.12) and rests on a single run per arm; (b) that the change is harmless, since three real losses were observed; (c) the size of the reranker's own run-to-run variation on this set, since no arm was repeated, so it is **not** claimed that the gains lie "within noise", nor that they lie outside it; (d) the cause of the (4,38) score difference. No answer-level claim. No generalisation beyond LoCoMo multi-hop.

Per instruction: results saved; no further experiment queued. Status and rollback of the candidate fusion are in §10.


## 10. Status of the candidate fusion and how to roll it back

- Branch `second-shot`, commit **47e590a** (`app/search.py`: `_WEIGHTS` entity/literal halved, `_entity_by_relevance`, `_ENTITY_KEEPS_RECENCY`), followed by diagnostics-only commits 0c3b857, d324cc3, a5fa22b. The experimental instance `:8082` (`/srv/aml/app2`, database `aml2`, Caddy path `/v2/*`) runs d324cc3 = this fusion. Nothing has been promoted: the evaluated instance `:8080` (`/srv/aml/app`, database `aml`) still runs 1be823e and was not touched by any step in this report.
- The previous rule is kept verbatim in `tests/diag_locomo_rerank_pair.py` as `FUSION_OLD` (weights doubled back, recency order for every intent) and was checked to reproduce the traced pre-change order on 1982/1982 questions.
- Rollback: `git revert 47e590a` (touches only `app/search.py`; the later diagnostics commits do not depend on it), then `bash deploy/deploy-v2.sh` to put `:8082` back. There is no runtime switch; adding one was not done because the instruction was to leave defaults alone.
- Whether this fusion goes into the second Full is an open decision; it is not the default of anything that the platform evaluates.

## 11. Proposed minimal answer-level comparison (plan only; no paid call made)

**Contexts.** Reuse what this run saved: for every question, both arms' final lists (`fusion-pair.jsonl` → `old.final` / `new.final`, 100 segment ids each, in the order they were returned). The served text of a segment is re-rendered from the `aml2` database with the same `_render` the API uses (header `[YYYY-MM-DD (Weekday) HH:MM] speaker:` + text), a read-only DB operation, so the answer model sees exactly what the platform would have received from Search. No new Search, no embedding, no rerank.

**Answer model, prompt, judge — fixed.**
- Answer model: `qwen-plus` on the same Bailian account, `temperature 0`, `seed 20261001`, `max_tokens 256`. (The platform's own model is not disclosed for the academic board beyond gpt-4o-mini being the only LLM allowed on the participant side; we cannot reproduce its prompt, so this is a diagnostic answerer, not a score estimate.)
- Prompt: the LoCoMo answer template already used in the 09-27 closed loop (`/srv/aml/pipelines/`, patched copy of the official pipeline): question + question date + the 100 context lines, "answer briefly using only the context". Identical for both arms.
- Judge: (1) the official LoCoMo LLM-judge prompt with `qwen-plus`, `temperature 0`, run **3 times** per answer, majority vote; (2) token-level F1 against the gold answer as a second, model-free signal. Both reported; disagreements listed.

**Question selection — two tiers.**
- Tier A (diagnostic): the 15 questions whose exact-turn outcome changed (11 gained, 4 lost). Answers both arms. Tells whether the added or dropped turn changed the answer. **Cannot estimate overall gain**, because the selection is conditioned on the retrieval outcome.
- Tier B (category estimate): all 280 multi-hop questions, both arms. Gives a paired answer-accuracy difference for this category only; still not an estimate for LoCoMo as a whole, the platform's judge, or the leaderboard.

**Noise handling.**
- Answer-model noise floor: re-run the answerer on the *old* arm's contexts a second time with the same settings; the fraction of questions whose judged result flips between the two identical-context runs is the floor against which the old-vs-new difference is read. If the old-vs-new flip count is not clearly above that floor, the result is reported as inconclusive.
- Judge noise: 3× majority as above; questions where the 3 votes are not unanimous are flagged.
- Paired per-question reporting throughout (only-old / only-new), never aggregate deltas alone.

**Cost (qwen-plus list price: ¥0.8 / M input, ¥2 / M output).**
- Context ≈ 5k tokens per question per arm (measured: `final_tokens_cl100k` averages ~4.9k).
- Tier A: 15 q × 2 arms × ~5.2k in ≈ 0.16M in → ~¥0.13; judges 15×2×3 short calls → ~¥0.05; noise re-run ~¥0.07. **≈ ¥0.3.**
- Tier B: 280 × 2 × 5.2k ≈ 2.9M in → ~¥2.3; output ~¥0.1; judges 280×2×3 ≈ 1.7k calls × ~0.5k tokens ≈ 0.85M → ~¥0.7; noise re-run (old arm again + its judges) ~¥1.5. **≈ ¥4.6, cap ¥6.**
- Zero Bailian rerank/embedding spend either way.

**Deliverable.** One table per tier: per question, old/new judged result (3-vote), F1, flip direction; the noise-floor run; the 15 changed questions cross-referenced to §9b so each answer change is tied to the specific turn that entered or left the context. Conclusion scope: Tier A diagnostic only; Tier B multi-hop category only.
