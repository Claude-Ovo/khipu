# Answer-layer diagnostics: A4 regression, evidence-only ceilings, B1 (dates), B2 (knowledge update)

Status: **closed 2026-10-04 04:xx**. Paid calls in this round: ¥19.9 (A4, approved at ¥14, overran) + ¥0.95 (everything after A4). No default changed, nothing deployed. All proxies: `qwen-plus-2025-12-01` answers and judges (3 seeds, majority), official LongMemEval templates, LME-s questions. The platform's model, prompt and judge differ; numbers here are directions, not score predictions.

Scripts in `scripts/`, raw per-question records in `results/`. Server-side artefacts under `/srv/aml/data/{a4,b1,b2}` on Morrow.

## 1. A4: second-shot base vs first-Full code, LME dev 200, answer level

Arms: `old` = `1be823e` (first Full, `/srv/aml/app-old`, +08 headers, no usage counters), `new` = `fc9a2b5` (second-shot, `FUSION_RULE=legacy`, UTC headers). Same DB `aml2`, rerank on, window 200, top_k 100.

| | old | new |
|---|---|---|
| correct / 200 | 144 | 150 |
| single-session-user (70) | 67 | 68 |
| multi-session (100) | 61 | 63 |
| single-session-preference (30) | 16 | 19 |
| retrieval label handed / not_found | 189 / 11 | 189 / 11 |

only_old 6, only_new 12, McNemar p = 0.24. Same-input reruns flip ~13/200 on this proxy, so this run shows **no large regression and no demonstrated improvement**; a regression smaller than the rerun noise (roughly ±10 questions) cannot be excluded by this check. gold-hit changed: 0 questions. Context size identical (13.4k tokens mean).

Cost: retrieval new arm ¥7.0 (stopped at question 194 by the cap, 6 questions added later), old arm ≈ ¥7.2 (estimated, no counters in that code), answers + 3 judges ¥5.43. Lesson recorded: estimate by the most expensive question type and add 30 %; multi-session questions send ~48k rerank tokens each, not the 34k of a single-session question.

## 2. Where the 50 remaining errors are (new arm), and the evidence-only ceiling

Free classification of the 50 wrong answers:

| kind | n | all gold rows inside top-100 |
|---|---|---|
| multi-session counting / sums | 33 | 31 |
| single-session-preference | 11 | 11 |
| other | 6 | 6 |

Retrieval is not the bottleneck on LME dev 200: 46/50 errors had every gold row handed. Deepest gold rank among correct answers: median 2, p90 11; among wrong counting answers it is often 27–82 (undercounts) or the context carries look-alike items (overcounts, ≈17 vs 13).

**Evidence-only rerun** (`a4-oracle`): the 46 errors whose gold rows were all present, answered again with *only* the gold rows (chronological), ¥0.11.

| kind | n | correct with gold only |
|---|---|---|
| counting / sums | 29 | 14 |
| preference | 11 | 4 |
| other | 6 | 1 |
| total | 46 | 19 |

So the ceiling for any context-cleaning change on this set is ≈ +19/200 (150 → ~169), and it is a ceiling: real extraction will not hand evidence this cleanly. The other 27 are answer-model failures with perfect evidence (miscounts such as 17 fish → 16, averages, and preference questions answered "no hotel suggestions in memory"). Not reachable from Add/Search.

## 3. B1: date headers (UTC vs +08), temporal-reasoning questions

Design: the 133 temporal questions are all in LME-s 200–500 (validation half) and are not ingested; a full retrieval loop would cost ≈ ¥14, not the ¥1–2 written in the TODO. Used the first 66 temporal questions only (67 untouched), gold rows only, two header renderings: dataset time (= second-shot UTC behaviour) and +8 h (= first-Full behaviour). 34/66 questions have at least one gold row whose calendar date moves under +08. Cost ¥0.32.

| header | correct / 66 |
|---|---|
| UTC (second-shot) | 40 |
| +08 (first Full) | 38 |

Only 4 questions differ (3 favour UTC). The header fix is confirmed faithful to the data (a session the old code stamped 2024-01-11 03:48, after the question time, is 2024-01-10 19:48 in the data), but its answer-level weight is small.

Remaining 26 errors under UTC: **19 are "how many days/weeks ago" questions** (19 wrong of 30 such questions). The official answer template carries no question date, so the model guesses "0 days ago" / "1 week ago". Checked the obvious workaround — anchor "today" to the newest stored session — for free: question date is 2–30 days (max 188) after the newest haystack session, so that anchor would be wrong by the gap. Not run. Whether the platform's own answer prompt supplies the question date is unknown (organizer said Search does not carry it); worth one question in the next mail.

Conclusion: B1 is closed. Keep UTC. No further local gain available without the question date.

## 4. B2: knowledge update (new value overrides old), 39 of 78 questions

Gold rows only (old and new statement both present), three renderings, ¥0.27:

| rendering | correct / 39 |
|---|---|
| old statement first, new last (chronological) | 32 |
| new first | 32 |
| chronological + header tag `most recent statement on this topic` / `earlier statement, may be outdated` | **37** |

Order alone does nothing in aggregate but 10 questions flip with order (5 each way): the model follows position, not dates. Explicit tags fix 6 and break 1 (Converse "worn how many times", gold 6: with the earlier count tagged outdated the model stops accumulating and answers 4). Two errors are tag-independent.

**Can the system produce those tags itself?** The tags above were placed from the gold labels (open book). Tested, offline and free after one ¥0.25 embedding pass over the 9,569 user turns of these 39 haystacks (`rows.npy` on Morrow), whether similarity grouping can find "the same topic" and name its newest statement:

- Partner retrieval: for each gold row, where its gold partner ranks among all the user's other turns — lexical TF-IDF: 1st in 25/80, top-3 61/80, worst 16; text-embedding-v4 cosine: 1st in 20/80, top-3 50/80, worst 11. Same-topic chatter sits between the two statements.
- Connected-component grouping on a simulated top-100 (100 turns closest to the question, gold forced in), union edges at cosine ≥ τ:

| τ | gold pair in one group /39 | noise rows in that group mean/max | newest row of the group is the true new statement /39 |
|---|---|---|---|
| 0.55 | 35 | 14.8 / 39 | 1 |
| 0.60 | 30 | 7.8 / 16 | 5 |
| 0.65 | 22 | 4.8 / 10 | 8 |
| 0.70 | 14 | 2.9 / 9 | 9 |
| 0.80 | 4 | 1.8 / 4 | 3 |

Restricting the group to rows that carry a number / weekday word (26 numeric questions): newest-is-true-new 5–9 of 26. In LME-s the distractor sessions on the same topic carry random, often later, dates, so "newest in the group" is usually a later mention that does not state the value; the tag would mostly be applied to the wrong row and would mark the real current value as outdated.

Conclusion: the +5/39 ceiling exists but is not reachable with similarity-based grouping. Reaching it needs a model that reads the two statements (value extraction / supersede chain at Add time), i.e. it folds into B3 and the gpt-4o-mini decision. B2 closed as a heuristic item.

## 5. What this changes in the TODO

- A4: done, no regression; second-shot base is safe to submit from.
- B1: closed, keep UTC; ask the organizer whether the answer prompt includes the question date.
- B2: closed as heuristic; merged into B3 as "value extraction with supersede marking", with the measured ceiling (+5/39 on this proxy) and the measured failure of the heuristic route.
- B3 remains the only item with a measured upside (counting +≤19/200, update +≤5/39 on proxies), and it is the one that needs gpt-4o-mini.
- Preference questions (11/30 wrong, 7 unfixable with perfect evidence) and "days ago" questions (19/30 wrong) are answer-side on this proxy; nothing scheduled.

## 6. Addendum 2026-10-04: per-question error reading and the event-list experiment

Prompted by a second reading (hers, via GPT) of sections 2–4: "the 19/46 is not a ceiling", "the 27 are not proven unreachable", "state replacement vs event accumulation must be kept apart". All three accepted; section 2's wording "ceiling" is withdrawn. Reasons found while re-reading the raw records: (a) gold-only is one presentation, not the best one; (b) the dataset's `has_answer` flags are incomplete — for 85fa3a3f the $20 flea-collar row is not flagged, so the gold-only arm could not answer it; (c) single runs, and the same-input rerun below flips 3/20.

**Ten counting/sum errors with all gold rows handed, read against the raw prompts, answers and judge reasoning** (`results/a4-*`, `calls.jsonl` on Morrow): omission 3 (yoga in an aside; mattress; lemon at rank 82), arithmetic 1 (fish: list right, sum wrong), category judgement 2 (physical therapy ≠ doctor's appointment; mattress), negation missed 1 (a graduation the user *missed*), assistant suggestion taken as user fact 1 (grapefruit), distractor-session events counted 2 (another persona's benefit concert and sister's wedding — LME-s distractors are other users' chats), double counting 2 (old arm: "$2,000" said twice; cousin's = Rachel's wedding), state/accumulate confusion 1 (two playthroughs of the same game collapsed into one), refusal 1. No judge disagreement found in the ten. The platform's distractor construction is unknown, so the two distractor cases may not exist there.

**Event-list experiment** (¥0.58). 10 of these errors + 10 originally-correct counting questions. Two fresh agents with no access to gold answers or to the analysis above read question + the actual 100 returned rows and wrote one line per event with date, row citation, verbatim quote and a status tag (done / plan / did not happen / assistant suggestion / borderline). Arms: `base` = the A4 context unchanged (also a same-input rerun), `eventlist` = the list prepended and rows numbered. qwen-plus answers, 3 judges.

| group | A4 original | base rerun | event list |
|---|---|---|---|
| originally wrong (10) | 0 | 2 | 2 |
| originally right (10) | 10 | 9 | 9 |

Same-input rerun moved 3/20. The list fixed 2 (graduation: the "did not happen" tag worked; pet cost: the itemized prices summed to $50) and did not fix the other 8 even when the list stated the missing fact outright (yoga on Sundays is listed; the model still answered four; fish still summed to 21; the distractor concert still added). It broke 1 of the correct ones (projects: six listed items, model counted all of them). Lists are in `eventlists/`.

Conclusion (scope deliberately narrow): on these 20 questions, with qwen-plus-2025-12-01 at temperature 0 and this run's settings, prepending a human-grade event list did not achieve a higher correct count than re-answering the unchanged context (2 vs 2 on the originally-wrong group, 9 vs 9 on the originally-right group). This does not show that presentation changes are ineffective in general, nor that Add/Search has no room left; it shows that this particular presentation, on this proxy, at this sample size, produced no measurable gain. The same-input rerun moved 3/20, so single-run differences of this size are within run-to-run variation on this proxy; whether a future experiment needs repeats is decided per experiment, not fixed here. Decisions taken 10-04: event-list automation is not pursued; gpt-4o-mini is not adopted as answer proxy at this point (see `../../../collab` note of 10-04 for what is and is not known about the platform's answering setup).
