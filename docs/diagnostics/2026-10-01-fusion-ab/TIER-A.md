# Tier A answer-level comparison (also §12 of REPORT.md)

## 12. Tier A answer-level comparison on the 15 changed questions (2026-10-01 07:1x, ¥0.31)

Run as specified in §11 with the constraints given at 07:0x: `tests/tier_a_answer_compare.py` (commit 9dc63d1) on the server, reading the saved contexts of both arms (`fusion-pair-contexts.jsonl`, production `_render`, returned order), no new retrieval or reranking. Outputs in `tier-a/`: `config.json`, `calls.jsonl` (all 180 calls: request body without auth header, raw response, usage, finish_reason, latency), `results.json`.

**Fixed settings.** Answer and judge model `qwen-plus-2025-12-01` (dated snapshot), temperature 0, seed 20261001 (judge runs use seeds 20261001/2/3), `enable_thinking` false, no `max_tokens` (same as the official pipeline). Prompts are the official LoCoMo-refined templates imported from the pipeline module (template hashes in `config.json`; the server copy's prompt text was diffed against the public copy: identical). The answer prompt contains the question and the returned memories split by speaker exactly as `replay_locomo.py --dump` does; no gold answer, no evidence marks, no audit notes, no arm label. The judge prompt contains question, original gold answer and generated answer only. LoCoMo10 has no per-question date and the official template has no such field, so none was supplied. All 45 answers finished with `finish_reason = stop` (none truncated). Prompt sizes 4.9k to 6.6k tokens. Cost ¥0.31 (347k in / 14k out at list price), cap ¥1 not reached.

**Arms.** `old` = old-fusion context, `new` = new-fusion context, `old_repeat` = the old context answered again with identical prompt and settings (repeatability check only).

### 12a. Results by 3-vote majority

| q | question | old | new | old_repeat | change |
|---|---|---|---|---|---|
| (0,7) | Caroline's relationship status | C 3-0 | C 3-0 | C 3-0 | none |
| (0,55) | subject both painted | C 3-0 | C 3-0 | C 3-0 | none |
| (3,55) | what inspires Joanna | C 3-0 | C 3-0 | C 3-0 | none |
| (3,75) | Nate's favourite desserts | C 2-1 | C 2-1 | C 2-1 | none (judges split each time) |
| (3,79) | how many screenplays | C 3-0 | C 3-0 | C 3-0 | none |
| (3,83) | skills Nate taught | W 1-2 | W 0-3 | W 0-3 | none |
| (4,7) | places Tim has been | W 0-3 | W 0-3 | W 0-3 | none |
| (4,14) | Tim's writing | W 0-3 | **C 3-0** | W 1-2 | gained |
| (4,18) | authors Tim read | W 0-3 | W 0-3 | W 0-3 | none |
| (4,21) | US cities John visited | W 0-3 | **C 3-0** | W 0-3 | gained |
| (4,38) | country Tim visits most | C 3-0 | C 3-0 | C 3-0 | none |
| (6,8) | how many pets | C 3-0 | C 3-0 | C 3-0 | none |
| (7,1) | who passed away | C 3-0 | **W 1-2** | C 3-0 | lost |
| (8,77) | Evan after the wedding | W 1-2 | W 0-3 | W 0-3 | none |
| (9,60) | gifts from artist friends | W 0-3 | W 0-3 | W 0-3 | none |
| **total correct** | | **8/15** | **9/15** | **8/15** | +2 / -1 |

Repeatability (old vs old_repeat, identical prompt and settings): majority identical on 15/15; the answer text differed on 5/15 (wording or list order) and the vote split differed on 3 answers ((3,75), (3,83), (4,14)). So the answer model is not deterministic at temperature 0 with a fixed seed, and the judge is not unanimous on 6 of the 45 answers. This is a repeatability observation from one repeat, not a noise bound.

### 12b. Per question: what changed and whether the added or dropped fact mattered

- **(4,21) gained, attributable to the new fact.** Old context lacked D6:3 (John: "I was in Chicago"); old answers "New York City, Seattle" (WRONG, missing Chicago); new answer "New York City, Seattle, Chicago" (CORRECT 3-0). The turn that entered the window is the one that completed the list.
- **(4,14) gained, not attributable to the new fact.** The turn that entered (D2:1, Tim discussing favourite books on a fantasy forum) appears in neither answer. Old: "Writes articles about fantasy novels for an online magazine." (0-3); new: "Writes articles about fantasy novels." (3-0); the identical old answer on the repeat got 1-2. The difference is the judge's reaction to the extra phrase "for an online magazine" against a gold list of five items, plus judge variance. Counted as a gain by the metric, not as evidence for the retrieval change.
- **(7,1) lost, attributable to the new fact (negatively).** The turn that entered (D6:4, Deborah: "I lost a friend last week") made the model list a fourth deceased person ("a friend who wrote her the quote ...") in addition to Karlie; D6:8 shows that friend *is* Karlie. Judges 2-1 WRONG for an extra list item. The added fact was correct but the answer model did not resolve it to the same person, and the list rule penalised the extra item.
- **(0,7)** no change: the old context already gave "single" (from "after that tough breakup"); the added "single parent" turn only shortened the answer.
- **(3,79)** no change: "At least three" in all arms; the count was derivable from D2:3 / D4:10 / D5:1 without the D12:13-14 exchange that entered the window.
- **(4,7)** no change: the new answer now includes the added fact (Smoky Mountains) but, like the old one, omits California and adds places not in the gold (Galway, Cliffs of Moher, a UK castle), so it stays WRONG under the list rule. The fact arrived; the answer did not become correct.
- **(3,83)** no change by majority (W 1-2 to W 0-3): the dropped turn D18:8 (teaching a dairy-free dessert) did not remove the fact, "vegan ice cream recipes" is in all three answers; the one CORRECT vote on the old answer went to the wording "gaming tips".
- **(8,77)** no change by majority (W 1-2 to W 0-3): the dropped snowshoeing turn is absent from all three answers, including the old one that had it at rank 98; the old answer's single CORRECT vote came from mentioning the family get-together, which the new and repeat answers replaced with the honeymoon.
- **(4,38)** no change: the dropped UK-conference turn did not matter, "United Kingdom" in all arms.
- **(3,55)** no change: the dropped turn was a mislabel (§9b); all arms CORRECT. Answers differ in extras; the judge treats this as a preference-type list.
- **(0,55), (3,75), (6,8), (9,60), (4,18)** no change; (3,75) judges split 2-1 on every arm (2 of 4 gold desserts covered); (9,60)'s new answer adds "a necklace with a diamond pendant", gold says "gold chain", WRONG either way.

### 12c. Known annotation issues (gold kept as is; scored against the original gold in the table above)

- (3,55): evidence turn D4:6 is mislabelled (§9b); the gold *answer* is not affected. Human review: all three answers cover the supportable part.
- (3,79): gold "three" reflects the state part-way through the conversation (later sessions mention more scripts, §1). All arms answered "At least three", judged CORRECT.
- (4,18): gold includes Tolkien; no arm names him. Human review: the model's four authors are supported by the context; Tolkien's support was not checked here.
- (9,60): gold "gold chain"; the new context led the model to "a necklace with a diamond pendant". Human review not done; original scoring kept.
- (4,7): gold "California" is supported only by "a nice chat with a Harry Potter fan in California" (D3:2); the model in all arms did not treat that as a visit.

### 12d. Conclusion (these 15 selected diagnostic questions only)

By majority vote, 8/15 to 9/15 with 2 gained and 1 lost. Of the two gains, one is caused by the retrieval change ((4,21), Chicago) and one is not ((4,14), judge sensitivity); the one loss is caused by the retrieval change ((7,1), an added correct fact double-counted by the answer model). Net attributable effect: +1 / -1. The single repeat of the old arm changed 5 answer texts and 3 vote splits but no majority. On this evidence the fusion change neither improves nor degrades answer accuracy in a way that can be told apart from answer-model and judge variation; it does deliver facts into the context that were absent before ((4,21), (4,7), (7,1)), and what the answer model does with them cuts both ways. Because these 15 were selected by their retrieval outcome, nothing here estimates the effect on the full category or the leaderboard.

Per instruction: no further experiment queued; default configuration unchanged (§10).
