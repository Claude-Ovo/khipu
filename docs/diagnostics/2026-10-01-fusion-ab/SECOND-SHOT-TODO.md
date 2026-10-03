# Second shot: what is still open (2026-10-01 08:0x)

> **Update 2026-10-04 04:xx** — A4 done (no regression), B1 closed (keep UTC), B2 closed as a heuristic and folded into B3. Numbers and reasoning in `../2026-10-02-answer-layer/REPORT.md`. Rows below carry a `[10-04]` status note; the rest of the text is as written on 10-01.

Written at the close of the fusion diagnostic round (`诊断-事实与多跳-20260930/fusion-sim/REPORT.md`). Purpose: pick the next item on evidence and cost, not on momentum. Nothing below is started; no paid call is queued.

Fixed reference points: first Full 48.01 (09-30, v0.3.1 at 1be823e, instance `:8080` untouched, under organizer review); second Full can start 10-29 11:58 at the earliest, evaluation closes 11-04; Morrow expires 10-26. Bailian balance was ¥702.49 on 09-30 09:1x; spent since then: ¥15 (09-30 diagnostics) + ¥6.32 (10-01), so about ¥680, to be confirmed on the console. A Full costs about ¥300–400 (first Full: ¥400.5, 92% of it reranking).

## A. Required before the second Full (process; do regardless of any quality work)

| # | item | evidence / state | minimal cost | affects submission |
|---|---|---|---|---|
| A1 | Organizer's reply to the 09-30 21:34 mail (Caddy change and test row during review) | waiting; log #0-d | 0 | yes: decides whether a re-verification or rollback of `:8080` is requested |
| A2 | Register the new version on the site, bind it to `https://43.128.132.126/v2/…`, run Smoke on the **final** commit (log #0-c: Smoke and Full must share one commit) | not done; academic-board Key login is on her side only | Smoke ≈ ¥50–100 Bailian | yes |
| A3 | Pre-Full checklist: search the mailbox for organizer mail, verify the confirm dialog (system / version / mode / concurrency), Morrow renewed, balance ≥ Full estimate + 30% | log #0-a/#0-b; balance rule in `第二枪-预算-20260930.md` | ¥60 renewal | yes |
| A4 | Regression guard for the fixes already on `second-shot` (#1 timeouts, #3 UTC, #4 rerank timeouts, #5 entity matcher, load-test fixes A/B): the earlier claim "retrieval unchanged" was withdrawn on 09-30 21:5x (193/200 returned lists differ, every context's date header changed). No answer-level check of the second-shot base against the first-Full code exists. | log "第二枪修复进度", correction note | LME dev 200, closed loop with qwen-plus on both codes ≈ ¥2–3; retrieval-only paired replay ≈ ¥0.01 | yes: this is the code that will be submitted, whether or not anything else changes. **[10-04] done: old 144 / new 150 of 200, p=0.24, within rerun noise → no regression; actual cost ¥19.9 (rerank was missing from the estimate).** |

## B. Score items from the first Full (weak abilities), with what a cheap local check would look like

| # | ability (first Full) | what is known | candidate change | minimal verification | note |
|---|---|---|---|---|---|
| B1 | C1 dates / intervals 41.60 | log #3: +08 header put evening sessions on the wrong day; fixed to UTC on `second-shot` (not yet verified at answer level) | already fixed; then Hindsight §6 items 5–6 (resolve relative dates at Add; anchor Search dates to the store, not the wall clock) | LME temporal-reasoning subset (133 q) closed loop before/after ≈ ¥1–2 | highest confidence that a fix exists and can be measured locally. **[10-04] closed: gold-only on 66 q, UTC 40 vs +08 38, 4 q differ; 19 of the remaining 26 errors are "days ago" questions that need the question date, which the answer template does not carry; store-anchored "today" is off by 2–30 days. Keep UTC; ask organizer about the question date.** |
| B2 | D1 new value overrides 27.50, D3 deletion / suppression 20.00 | old and new memories are returned as equals; nothing marks the current value | Hindsight §6 item 10: within near-duplicate groups order by time and mark the newest; "supersede" chain idea from the run log | LME knowledge-update subset (78 q) closed loop ≈ ¥1 | proxy fits D1 well; D3 (forget) has no local proxy identified. **[10-04] closed as heuristic: gold-only 39 q, order 32/32, open-book newest/outdated tags 37 (+6/−1); but similarity grouping names the true newest statement in ≤9/39 (same-topic distractors carry later dates). Reaching the +5 needs value extraction at Add → folded into B3.** |
| B3 | G4 pattern discovery 16.41, F1 long-history synthesis 18.51 | top-k per question cannot cover "the whole history" | session- and user-level summary / statistics blocks written at Add time, ranked with the rest; this is the item that needs gpt-4o-mini (budget plan 1) | no local proxy identified yet; would need a small hand-made set first | blocked on the gpt-4o-mini decision (her call; `第二枪-预算` plan 0 vs 1, plus OpenRouter account) |
| B4 | G3 procedures 25.00, B2 causal chains 30.84 | neighbor expansion only adds a few adjacent lines | wider same-session expansion for procedure-type questions | no clean local proxy; LoCoMo multi-hop is a different shape | low confidence; park |
| B5 | E3 care / background 33.54, H2 minimal disclosure 40.68 | no diagnosis yet | none | none yet | park until B1–B2 done |
| B6 | Multi-hop evidence coverage (this round) | candidate fusion 47e590a lifts all-gold-in-window 202→218 and all@100 190→197 on 280 LoCoMo multi-hop (p=0.12); Tier A on the 15 changed questions: 8/15→9/15, effects not isolated | candidate kept on `second-shot`/`:8082`, **not adopted** | only if reopened: Tier B category-level answer test ≈ ¥4.6 after hardening the runner (12e) | closed for now; reopen only if a submission-level decision needs it |

## C. Cost and reproducibility

| # | item | evidence | minimal cost | affects submission |
|---|---|---|---|---|
| C1 | Reranker spend: 92% of the Full bill. Options: `RERANK_DOC_CHARS` (already a switch), qwen3-rerank (¥0.5/M vs ¥0.8/M), per-intent window | 09-27: global window 80/120 loses coverage; 09-30/10-01 runs show the window matters for multi-hop | paired retrieval on LME 200 + LoCoMo multi-hop for any change ≈ ¥3–6 | budget only, unless it moves coverage |
| C2 | Usage accounting exists since the #1 fix (`/health` counters); LLM output caching by input hash and in-flight request_id dedup (budget doc items 1 and 4) | not needed unless an LLM is added at Add | code only | needed if B3 goes ahead: organizer reproduces Add/Search and rejects large divergence |

## D. Findings from this round that are not ours to fix but shape expectations

- The answer side fails for reasons unrelated to retrieval: list questions with one extra item are scored WRONG; the judge switched rules on identical answers; a gold answer's wording ("gold chain") is absent from the text while the model's answer matches the text; author names need world knowledge. On the platform, answering and judging are theirs; the only lever on our side is presentation (what is returned, in what order and grouping). Any presentation study must read the actual request text (12f clarification), and its first test costs ≈ ¥0.3–1 on the saved contexts.
- Evidence that entered the context did not reliably become a correct answer ((4,7), (7,1), (9,60)). Coverage metrics alone will over-credit retrieval changes.

## E. Suggested order (proposal, not started)

1. A1–A3 are calendar-driven; A2 needs her login and can wait for the final commit.
2. A4 first among paid checks: it guards the code that will be submitted no matter what, and costs ≈ ¥2–3.
3. Then B1 (C1 dates): fix exists, proxy exists, ≈ ¥1–2 to measure.
4. Then B2 (D1): cheap proxy, clear mechanism.
5. B3 only after the gpt-4o-mini decision; it is the only item that needs new money and new accounts.
6. B6 stays closed unless the submission decision needs it.

Everything above is measured on local proxies; the platform's ability mix, question pool and judge are not reproducible here, so local gains are directions, not score predictions.
