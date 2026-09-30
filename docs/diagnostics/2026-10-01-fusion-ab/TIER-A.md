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

Repeatability (old vs old_repeat, identical prompt and settings): majority identical on 15/15; the answer text differed on **7/15** ((0,7), (3,55), (3,83), (4,7), (4,18), (7,1), (8,77): wording or list order); the number of CORRECT votes differed on 3 questions ((3,83) 1 to 0, (4,14) 0 to 1, (8,77) 1 to 0); (3,75) got 2 votes in all three arms. The judge was not unanimous on **7 of the 45** answers ((3,75) x3, (3,83) old, (4,14) old_repeat, (7,1) new, (8,77) old). So the answer model is not deterministic at temperature 0 with a fixed seed, and the judge is not stable either. This is a repeatability observation from one repeat, not a noise bound. (The first version of this section said 5/15 and 6/45 and listed (3,75) among the vote changes; corrected 10-01 07:4x after review.)

### 12b. Per question: what changed and whether the added or dropped fact mattered

- **(4,21) gained; consistent with "fact entered, list completed".** Old context lacked D6:3 (John: "I was in Chicago"); old answers "New York City, Seattle" (WRONG, missing Chicago); new answer "New York City, Seattle, Chicago" (CORRECT 3-0). D6:3 is the only returned turn naming Chicago, so the explanation fits; but the two contexts differ in more than this one line (membership and order), and no single-line add/remove control was run, so the cause is not isolated.
- **(4,14) gained by a judge rule switch, not by the answer.** The turn that entered (D2:1, Tim discussing favourite books on a fantasy forum) appears in neither answer. Old: "Writes articles about fantasy novels for an online magazine." (0-3); new: "Writes articles about fantasy novels." (3-0); the identical old answer on the repeat got 1-2. The judge explanations show which rule was applied: all three old-arm judgements and two of three repeat judgements required the answer to cover **all** distinct items of the five-item gold ("fails the all distinct facts requirement"); all three new-arm judgements and one repeat judgement applied the **preference exception** ("only one required"). Same answer content, opposite rule. This is not an answer improvement, and unanimity does not protect against it: three judges can apply the wrong rule together.
- **(7,1) lost; consistent with "new context added ambiguity", cause not isolated.** The new arm's answer adds a fourth deceased person, "a friend who wrote her the quote 'Let go of what no longer serves you'". That phrase comes from D23:22 (Deborah, 2023-08-30: "written to me by a friend who, unfortunately, will never be able to support me. I miss him here"), which is in **both** arms' contexts (old #28, new #31). The line that entered with the new fusion is D6:4 (2023-02-22, "I lost a friend last week", new #8). Judges 2-1 WRONG for the extra item. Two readings fit and cannot be separated here: the model tied D6:4's friend to Karlie (D6:8, new #20) and then counted D23:22's friend as another death; or D6:4 primed it to look for a second friend. See 12f for how the context presents these lines.
- **(0,7)** no change: the old context already gave "single" (from "after that tough breakup"); the added "single parent" turn only shortened the answer.
- **(3,79)** no change: "At least three" in all arms; the count was derivable from D2:3 / D4:10 / D5:1 without the D12:13-14 exchange that entered the window.
- **(4,7)** no change: the new answer now includes the added fact (Smoky Mountains) but, like the old one, omits California and adds places not in the gold (Galway, Cliffs of Moher, a UK castle), so it stays WRONG under the list rule. The fact arrived; the answer did not become correct.
- **(3,83)** no change by majority (W 1-2 to W 0-3): the dropped turn D18:8 (teaching a dairy-free dessert) did not remove the fact, "vegan ice cream recipes" is in all three answers; the one CORRECT vote on the old answer went to the wording "gaming tips".
- **(8,77)** no change by majority (W 1-2 to W 0-3): the dropped snowshoeing turn is absent from all three answers, including the old one that had it at rank 98; the old answer's single CORRECT vote came from mentioning the family get-together, which the new and repeat answers replaced with the honeymoon.
- **(4,38)** no change: the dropped UK-conference turn did not matter, "United Kingdom" in all arms.
- **(3,55)** no change: the dropped turn was a mislabel (§9b); all arms CORRECT. Answers differ in extras; the judge treats this as a preference-type list.
- **(0,55), (3,75), (6,8), (9,60), (4,18)** no change; (3,75) judges split 2-1 on every arm (2 of 4 gold desserts covered); (9,60)'s new answer adds "a necklace with a diamond pendant", gold says "gold chain", WRONG either way (see 12c: the text supports the necklace, not a chain).

### 12c. Known annotation issues (gold kept as is; scored against the original gold in the table above)

- (3,55): evidence turn D4:6 is mislabelled (§9b); the gold *answer* is not affected. Human review: all three answers cover the supportable part.
- (3,79): gold "three" reflects the state part-way through the conversation (later sessions mention more scripts, §1). All arms answered "At least three", judged CORRECT.
- (4,18): gold includes Tolkien; no arm names him. Original text check: "Tolkien" never appears in the conversation; it is supported only through titles, "The Hobbit is great" (Tim, D20:21, in both contexts at #12) and "my favorite, Lord of the Rings!" (Tim, D26:32, in neither context). Reaching "Tolkien" needs world knowledge the answer model did not apply. Original scoring kept; human evidence judgement: the answer omits an item the context supports only indirectly.
- (9,60): gold "gold chain". Original text check: no turn in conv 9 says "chain"; the gift is described only as "this beautiful necklace with a diamond pendant" (Calvin, D4:24) and "that's a great necklace" (Dave, D4:25). D4:24 is in the new context (#41) and not in the old; D4:25 is in both (#21). So the new answer (guitar + necklace with a diamond pendant) is the one that matches the conversation text, and the gold wording "gold chain" has no textual support. Original scoring kept (WRONG in all arms); human evidence judgement: the new answer is supported, the gold's wording is not.
- (4,7): gold "California" is supported only by "Last week, I had a nice chat with a Harry Potter fan in California" (Tim, D3:2; in both contexts, #53 / #58). Talking with someone who is in California does not establish that Tim was there; the model in all arms did not count it, and on the text that is defensible. Original scoring kept (WRONG in all arms, also because of extra places); human evidence judgement: the gold's "California" is not established by the text.

### 12d. Conclusion (these 15 selected diagnostic questions only)

By majority vote, 8/15 to 9/15 with 2 gained and 1 lost. One gain ((4,14)) is a judge rule switch on near-identical answers and is not an answer improvement. The other gain ((4,21)) and the loss ((7,1)) are each consistent with a mechanism, "fact entered, list completed" and "added context, added ambiguity", but both arms differ in many lines and their order, no single-line control was run, and so neither cause is isolated. The single repeat of the old arm changed 7 answer texts and 3 vote counts but no majority. Token F1 (offline, `tier-a/f1.json`, computed by `tests/tier_a_f1.py`, our own fixed implementation, not the official metric): mean old 0.45, new 0.48, repeat 0.45; per question it moves both ways ((4,21) 0.75 to 0.89, (4,7) 0.20 to 0.43, (9,60) 0.44 to 0.26, (3,55) 0.24 to 0.13). On this evidence nothing is shown about improvement, harm or equivalence of the fusion change at the answer level; what was learned is concrete: facts did enter the context ((4,21), (4,7), (7,1), (9,60)) and the answer still failed for reasons on the answer/judge side (list rule with extras, unresolved coreference, gold wording without textual support, author names needing world knowledge). Because these 15 were selected by their retrieval outcome, nothing here estimates the effect on the full category or the leaderboard.

Per instruction: no further experiment queued; default configuration unchanged (§10).


### 12e. Independent read-only review of the runner (Codex, 2026-10-01 07:1x)

Codex reviewed `tests/tier_a_answer_compare.py` at 9dc63d1 without network or API access (log kept locally). Requirements 1, 2, 3 and 5 (answer prompt = question + memories only; judge prompt = question + original gold + generated only; `old_repeat` builds the identical prompt; gold never rewritten) PASS at the script level; the speaker split matches `replay_locomo.py`. Requirement 4 FAIL on robustness, with five findings: (a) a call that raises (timeout, non-2xx, bad JSON, empty `choices`) is not logged; (b) the cost cap is checked after each call, so one call can overshoot and missing `usage` counts as zero; (c) a question is only saved to `results.json` once all its arms finish, and only `RuntimeError` is caught; (d) a `None` context line (possible in `dump_pair_contexts.py` when a segment id is not found) would crash the speaker split; (e) an unparsable judge reply is counted as a WRONG vote.

Checked against this run's artefacts: 180 logged calls = 45 answers + 135 judge runs, every `finish_reason` is `stop`, all 135 judge labels parsed as CORRECT/WRONG, no `None` context line exists in `fusion-pair-contexts.jsonl`, and the cap was not approached (¥0.31 of ¥1). None of the five failure paths occurred, so the results above are not affected. The runner should be hardened before any larger (Tier B) use; not done now, since no further run is queued.


### 12f. (7,1): how the related evidence is presented in the context

Returned order and date headers of the lines about deaths in the new arm (old arm in brackets): D2:1 dad passed away, #1 [#1]; D2:13 mom passed away, #2 [#2]; D1:5 last photo with her, #4 [#4]; D22:23 mother's cat, #6 [#6]; **D6:4 "I lost a friend last week", #8 [not returned]**; D6:16 "Take care!", #17 [#15]; **D6:8 last photo with Karlie, #20 [#17]**; **D23:22 quote written by a friend "who will never be able to support me. I miss him", #31 [#28]**; D1:6 Jolene's mother, #38 [#36]; D6:6 "comforted by remembering our time together", #99 [#94]. All session-6 lines carry the same header `[2023-02-22 (Wed) 16:12] Deborah:`. The two turns that connect D6:4 to Karlie in the original conversation, Jolene's D6:5 ("Sorry to hear about your friend") and D6:7, are returned in neither arm; the model has to bridge D6:4 to D6:8 across 11 intervening lines by the shared date alone. D23:22 refers to a male friend six months later, whose death the text does not state. Whether a tighter presentation (session grouping, adjacent lines kept together) would change the answer is untested; noted only.

### 12g. Corrections applied after external review (10-01 07:3x)

Repeatability counts corrected (7/15 answer texts, 7/45 non-unanimous judgements, vote changes on (3,83), (4,14), (8,77)); (4,14) restated as a judge rule switch with the judges' own explanations; causal wording for (4,21) and (7,1) changed to "consistent with", cause not isolated; original-text checks added for (4,18), (9,60), (4,7) with original scoring kept and separate evidence judgements; token F1 computed offline and reported (planned in section 11, omitted from the first version of section 12). The 180-call log was read in full by the author of this section; the external reviewer could not read all of it and did not claim a full call audit.
