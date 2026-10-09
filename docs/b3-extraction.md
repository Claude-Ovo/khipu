# B3 — Add-time extraction with gpt-4o-mini

Branch `b3-extract`, written 2026-10-06. Off by default (`EXTRACT_ENABLED` unset). Not validated on answers yet; nothing here has been run against a paid model.

## Why

First Full (48.01): the lowest abilities were pattern discovery 16, long-history synthesis 18, deletion 20, new value overrides 27. Storing raw text verbatim and returning 100 fragments cannot answer "walk me through the history" questions. The 10-02 diagnostics also showed that with only gold evidence in context the stand-in answer model still fails on arithmetic, categories, relative dates ("three days ago") and assistant suggestions read as user facts (`docs/diagnostics/2026-10-02-answer-layer/REPORT.md`). B2 (newest/older marking) was closed because similarity grouping could not find the true newest statement; it needs value extraction at Add time, so it was folded in here.

## What it does

For each Add request, after the empty-message filter and before embedding:

1. The messages are rendered as `[n] 2023-05-20 (Sat) 14:02 Caroline: …` lines (speaker prefix stripped from the content, role used when there is none; each message clipped to `EXTRACT_MSG_MAX_TOKENS` for extraction only) and cut at message boundaries into windows of at most `EXTRACT_WINDOW_TOKENS`. Only the request is read, never the database, so a request always produces the same windows.
2. Each window is one chat completion to an OpenAI-compatible endpoint (see **Relay** below): `gpt-4o-mini-2024-07-18`, `temperature 0`, `seed 7`, `response_format json_object`. On OpenRouter the body also carries `provider: {only: [openai, azure], allow_fallbacks: false, require_parameters: true}`; other endpoints do not get that field.
3. The model returns up to 30 facts `{text, subject, key}` and one `summary`. The prompt asks for explicit subjects, exact values, absolute dates for relative expressions, explicit negations and changes, no summing across messages, and assistant suggestions recorded only when the user acts on them.
4. Facts and the summary become extra rows in `segments`, written in the same transaction as the raw messages:

| field | value |
|---|---|
| `kind` | `note` / `summary` (raw messages are `msg`) |
| `id` | `<session>#<seq>.n<k>` — never collides with `<session>#<seq>.<part>`; the replay scripts' gold-evidence regex does not match it, so coverage metrics keep measuring raw evidence only |
| `seq`, `part` | seq of the request's last message, part `1000 + k`: sorted after that message, before the next request |
| `ts_value` | the last message's time (when it was said); the event date is in the text |
| `speaker_name` | the subject when it is a name (generic `user`/`assistant` → null), so the entity channel and BM25 see it |
| `note_key` | topic key such as `user.job`, notes only |

They are embedded as plain text, enter the five channels, RRF, the rerank window and boxing like any other row, and render as

```
[2023-05-20 (Sat) 14:02] (memory note) Caroline adopted a dog named Max on 19 May 2023.
[2023-05-20 (Sat) 14:02] (conversation summary) Caroline told Melanie about her new dog.
```

Neighbor expansion skips notes (they are not anchors and are stepped over when looking for the previous/next message).

`EXTRACT_MARK_LATEST=1` adds, at index build time, a tag to notes that share a `note_key` across different days: `[newest note on user.job]` or `[older note on user.job; a newer one is dated 2023-06-01]`. It states dates, not conclusions — keys are chosen per window by the model and multi-valued topics (pets, hobbies) share keys. It is Search-side only: the same stored data can be compared with and without it, no re-extraction.

## Relay

OpenRouter was the first plan. On 10-06 its credits page showed: "Your billing address is in a region that does not have access to models from OpenAI, Anthropic, and Google." OpenAI's own API does not serve mainland China either. The organizer's public Q&A (Xiaohongshu, 09-22) says the open-source method board must use gpt-4o-mini at Add time and that **relay channels are allowed if declared in the submission notes**. So the default endpoint is now AiHubMix, which lists the dated snapshot `gpt-4o-mini-2024-07-18` at the official price ($0.15 / $0.60 per M).

A relay can substitute a cheaper model. Before any paid run, `deploy/set-extract-key.sh` stores the key and runs `tests/probe_relay.py` on Morrow (cost under one cent):

- the model name the reply reports (a hint only);
- `prompt_tokens` against the gpt-4o-family tokenizer (o200k_base) using the chat-format count; a non-OpenAI model behind the relay, or a hidden injected prompt, makes this disagree (for the probe text: o200k 153, cl100k 165);
- the same request twice at temperature 0 / fixed seed, and `system_fingerprint`;
- whether `json_object` output parses.

It cannot catch a same-family substitution (e.g. a smaller model with the same tokenizer); the per-call token usage against the relay's bill is the remaining check. During runs, every reply's reported model is stored in `extract_cache.output.model`.

Submission note (draft): "Add stage uses gpt-4o-mini (gpt-4o-mini-2024-07-18) via the AiHubMix relay (OpenAI-compatible API); embedding text-embedding-v4 (Alibaba Cloud Bailian); reranker gte-rerank-v2."

## Reproducibility and failure handling

The organizer replays Add/Search with their own keys and rejects large divergence.

- **Cache.** `extract_cache` is keyed by the SHA-256 of the full request body (model, prompt, seed, provider constraints, window text). One paid call per distinct window; platform retries and same-content requests reuse it. Changing the prompt changes the key without a manual version bump. The table doubles as an archive of every extraction output (`raw` model text, parsed items, status, token counts) that can be handed to the organizer.
- **In-flight dedup.** Concurrent requests for the same window wait on the first one. Registration happens before the cache read with no `await` in between, and the leader writes the cache before unregistering, so a process never pays twice for one window.
- **Deterministic bad results** — HTTP 400, 403 (moderation), 413, or a reply that cannot be parsed — are kept as empty and cached. At temperature 0 a retry returns the same thing, and a 503 would make the platform retry 32 times and then interrupt the Full. Truncated JSON is salvaged: complete fact objects before the cut are kept.
- **Operational failures** — no key, 401/402/404, 408/429/5xx or timeouts after `EXTRACT_ATTEMPTS`, a 200 without `choices`, or `EXTRACT_TOKEN_CAP` reached — raise, and Add returns 503 (retryable per the contract). Nothing is written: a request is stored with its notes or not at all, same rule as embeddings.
- **Counters.** `/health` → `usage.extract`: calls, ok, failed, prompt/completion tokens, avg latency, cache hits, empty windows. A non-zero `empty` early in a run means a configuration problem (e.g. a 400 from a parameter a provider rejects) and should stop the run.

Remaining non-determinism: OpenAI does not guarantee identical output for identical requests even with a seed. A reproduction will word some facts differently. Raw messages are stored and returned unchanged, but notes share the BM25, vector and entity indexes with them, so the recall and ranking of raw messages can shift when the notes differ; `NOTES_MAX_RETURNED` caps how many notes are returned, not how much the ordering may move. A reply that names a model other than gpt-4o-mini is rejected and retried (then 503), never cached.

## Config

| variable | default | |
|---|---|---|
| `EXTRACT_ENABLED` | off | `1` to extract |
| `EXTRACT_API_KEY` | — | `OPENROUTER_API_KEY` also accepted; stored on Morrow in `/srv/aml/.extract` by `deploy/set-extract-key.sh`, never in the repo |
| `EXTRACT_BASE_URL` | `https://aihubmix.com/v1` | any OpenAI-compatible endpoint |
| `EXTRACT_MODEL` | `gpt-4o-mini-2024-07-18` (`openai/…` on OpenRouter) | the model name each reply reports is stored with the cached result and logged as an error if it is not a gpt-4o-mini |
| `EXTRACT_PROVIDERS` | `openai,azure` on OpenRouter, empty elsewhere | |
| `EXTRACT_SEED` | 7 | |
| `EXTRACT_WINDOW_TOKENS` | 6000 | per call; a platform chunk (20 messages / 2,000 words) is one window |
| `EXTRACT_MSG_MAX_TOKENS` | 3000 | per message, extraction input only |
| `EXTRACT_MAX_FACTS` | 40 | parser cap; the prompt asks for at most 30 |
| `EXTRACT_MAX_OUTPUT_TOKENS` | 3000 | |
| `EXTRACT_TIMEOUT_S` / `EXTRACT_ATTEMPTS` | 60 / 3 | |
| `EXTRACT_CONCURRENCY` | 16 | own HTTP client and pool, separate from Bailian |
| `EXTRACT_TOKEN_CAP` | 0 (off) | per-process spend guard for replays |
| `EXTRACT_MARK_LATEST` | off | Search-side newest/older tags. **Do not enable** — trial 10-06: keys differ across windows for the same attribute (`user.korean_restaurants` vs `user.korean_restaurants_experience`), so it would tag a stale value as newest |
| `NOTES_IN_SEARCH` | on | `0` hides notes from indexing and vector search: two instances on one database give a paired with/without comparison |

## Flag off = second-shot candidate

This section is about commit `f591e31` (the extraction code alone). The next commit, `7bd8c74`, deliberately changes tie order in BM25 (see the following section), so from there on the branch matches the candidate only up to that fix.

With `EXTRACT_ENABLED` unset the differences from `full-2-candidate` are: two added columns (`kind` default `msg`, `note_key`), the `extract_cache` table, `AND kind = 'msg'` in the session-last-timestamp query, and an `extract` block in `/health`. Checks:

- offline: `tests/test_extract.py` (28 cases) compares the new neighbor selection with the old implementation on 400 random layouts; the existing suites pass unchanged (65 total);
- `tests/contract_smoke.py` against a keyless instance of this branch: 0 failures;
- `tests/eq_check.py`: the candidate and this branch side by side on Morrow (keyless, no rerank, separate databases), same Adds, every Search response compared byte for byte — LoCoMo all 10 conversations and the first 20 LongMemEval-S questions, at top_k 100 and 10 (1,160 Adds, 4,012 Searches). With both processes at `PYTHONHASHSEED=0`: **0 differences**.

## Found on the way: Search depended on the process hash seed

The first `eq_check` run, without a fixed seed, showed 71 of 4,012 Searches differing, all at tail ranks (first one at rank 86 of 100), none in the LongMemEval part. Cause: `_bm25_channel` passed `list(set(tokens))` to `rank_bm25.get_scores`, which accumulates float scores token by token. Set order follows the per-process string hash seed, so the last bits of the scores, and the order among near-equal candidates, changed with every restart. This is in `full-2-candidate` too: two restarts of the same build return slightly different tails for about 1.8 % of queries, and so would the organizer's reproduction.

Fix (separate commit, so it can be cherry-picked onto `second-shot` on its own): score `sorted(tokens)`. Check: two instances of the fixed build at `PYTHONHASHSEED=11` and `22`, same `eq_check`: **0 differences**. Vector search is an exact scan (`ORDER BY <=>`, no ANN index), so it does not add run-to-run variation; the reranker is an external call and is not covered by this check.

## Cost and time (estimate, not measured)

Basis: the first Full sent about 55 M tokens of message text through Add. With platform chunks of about 2,700 tokens that is about 20,000 windows.

- input: 55 M + 20,000 × ~600 (system prompt) ≈ 67 M × $0.15/M ≈ $10
- output: 20,000 × ~1,000–1,200 ≈ 20–24 M × $0.60/M ≈ $12–14.5
- with a relay at official price (no 5.5 % OpenRouter fee): total ≈ $22–24.5 ≈ ¥170–190; with the 30 % margin rule, **budget ¥250 for a Full**. The archive's ¥170 assumed fewer output tokens.
- Bailian side: notes add roughly 15 M embedding tokens (≈ ¥8); rerank windows stay at 200 candidates.
- Local validation, LongMemEval dev 200 (about 4,000 windows): ≈ $5 at the relay + Bailian for the replay; measure the actual per-window tokens on the first 20 questions with `EXTRACT_TOKEN_CAP` set, then re-estimate before going further.

Latency: an Add now waits for one completion (≈ 1,000 output tokens, 10–20 s). At 16 concurrent Adds that is roughly 1 Add/s, so the Add phase of a Full may take several hours longer than the first one (20 h 51 m in total). Unknown: whether the platform has an Add timeout below that. Measure p50/p95 Add latency in local validation.

## Not done

- No paid call has been made; prompt quality is untested. First paid step: 20 LME questions with `EXTRACT_TOKEN_CAP`, read the notes by eye, then the 200-question answer comparison (extraction vs current, paired per question).
- User-level summaries across sessions are not built: they would depend on Add order, which the platform controls.

## Trial 1 (10-06, 8 LongMemEval-S questions, AiHubMix)

Probe: reported model `gpt-4o-mini-2024-07-18`, prompt_tokens 153 = o200k count, identical replies to identical requests, fingerprint `fp_99f88af1c5`.

346 windows, 0 failures, 0 empty. Per window: 3,094 input / 577 output tokens, 7.1 s average (Add p50 8.2 s, p95 16–18 s, max 37 s). Cost: extraction $0.28, Bailian about ¥0.7. Notes ≈ 0.8 per stored message; about 40–50 of the 100 returned items are notes on temporal and knowledge-update questions.

Read by eye:
- single-session fact and preference questions: the answering note is at rank 0–1;
- temporal reasoning: notes carry absolute dates for each event (helpful for ordering and day counts);
- knowledge update: Korean restaurants — the newest value ("four") at rank 1; 5K personal best — the **stale** value (27:12) at rank 0, the current one (25:50) at rank 9;
- the model still records general knowledge and assistant recommendations despite the prompt.

Full projection: about 22,000 windows → extraction ≈ $18 (≈ ¥130; budget ¥200), Add phase +2–3 h at 16 concurrent Adds. Whether notes help overall needs the paired answer comparison.

## Comparison 1 (10-06, 100 LongMemEval-S questions, paired)

Set: the first 40 multi-session questions of the dev 200, plus the first 30 knowledge-update and 30 temporal-reasoning questions (these two types do not occur in the dev 200). One database (`aml_b3`), ingested once with extraction on; two Search arms over the same rows: `notes` (default) and `raw` (`NOTES_IN_SEARCH=0`). Rerank on, top_k 100, `FUSION_RULE=legacy`. Answers and judging: official LongMemEval templates, qwen-plus-2025-12-01, temperature 0, 3 judge runs (`tests/answer_compare.py`).

| type | n | raw | notes | notes-only right | raw-only right |
|---|---|---|---|---|---|
| knowledge-update | 30 | 27 | 29 | 2 | 0 |
| multi-session | 40 | 20 | 20 | 6 | 6 |
| temporal-reasoning | 30 | 8 | 12 | 6 | 2 |
| all | 100 | 55 | 61 | 14 | 8 |

Sign test on the discordant pairs: p = 0.29. The earlier same-input rerun noise was 13 flips in 200, so +6 in 100 is not established by this run alone; the per-type pattern matches the mechanism (absolute dates for durations, current values for updates).

Retrieval: raw-evidence coverage drops when notes take slots — answer turn in top 10: 96 → 91; all gold turns in top 100: 98 → 93; about 40 of the 100 returned items are notes; context 14.3k → 10.4k tokens.

Losses read by eye: multi-session counts where a note adds an item that is not one (a third "project", yoga hours) or double-counts across sessions; two event-order questions.

Cost of this round (trial + comparison): AiHubMix $3.67; Bailian ≈ ¥16 (embedding ¥6.4, rerank ¥7.3, answering/judging ¥2.55). Ingest: 4,332 Adds in 55 min at 12 parallel users, Add p50 7.6 s, p95 19 s, max 52 s; 4,073 extraction calls, 0 failed, 0 empty.

Open: (a) cap or demote notes in the returned list to win back raw coverage without losing the temporal/update gains — Search-side only, re-dump the same data; (b) LoCoMo (two named speakers, multi-hop), where coverage loss could cost more than on LME.

## Comparison 2 (10-06 night): notes caps, and LoCoMo

Code `7c16619`: `NOTES_MAX_RETURNED` (cap on notes among the returned items) and notes whose subject is one of the conversation's speakers render under that speaker (`[date] Caroline: (memory note) …`), so the answer templates that split memories by speaker put them in the right column. Judging counts majority-correct-with-dissent as correct (the comparison-1 table counted only unanimous-or-plain correct; under this rule `notes` there is 62).

LongMemEval-S, same 100 questions and data:

| arm | total | KU (30) | MS (40) | TR (30) | vs raw | turn@10 | allturn@100 |
|---|---|---|---|---|---|---|---|
| raw | 55 | 27 | 20 | 8 | | 96 | 98 |
| notes | 62 | 29 | 20 | 13 | +15 / −8 | 91 | 93 |
| cap 20 | 61 | 28 | 22 | 11 | +13 / −7 | 91 | 94 |
| cap 10 | 60 | 29 | 20 | 11 | +13 / −8 | 91 | 94 |

LoCoMo, 120 questions sampled evenly across the 10 conversations (multi-hop 60, temporal 30, single-hop 30), all 10 conversations ingested with extraction (299 windows, 11.3 s each, ingest 67 min at 10 parallel conversations):

| arm | total | multi-hop | single-hop | temporal | vs raw (p) | gold in top 10 | all gold in top 100 |
|---|---|---|---|---|---|---|---|
| raw | 66 | 30 | 24 | 12 | | 109 | 105 |
| notes | 64 | 28 | 22 | 14 | +17 / −19 (0.87) | 96 | 93 |
| cap 20 | 70 | 31 | 23 | 16 | +20 / −16 (0.62) | 96 | 97 |
| cap 10 | 69 | 30 | 24 | 15 | +20 / −17 (0.74) | 96 | 97 |

Reading: on both sets the temporal questions move the most (LME 8 → 11–13, LoCoMo 12 → 14–16). Uncapped notes lose on LoCoMo; capped arms do not. Every difference is inside the noise of a single run (about 30 % of LoCoMo questions flip between any two arms). Several LoCoMo "losses" are judge artefacts against relative gold answers: "20 May 2023" for "the weekend before May 24, 2023" and "17 June 2022" for "the Friday before 24 June 2022" are both the right days and were marked wrong. Real extraction errors seen: a picnic dated from the wrong session, an extra item from a note in list questions.

Combined, cap 20 vs raw over 220 questions: +33 / −23 discordant (sign test p ≈ 0.23).

Cost of this round: AiHubMix $0.18; Bailian ≈ ¥17 (LME dumps ¥6.2, LME answers ¥2.9, LoCoMo dumps ¥4.8, LoCoMo answers ¥3.2).

## Two Search-side ideas that did not help (10-07)

Both on the same stored data, compared per question with the `cap 20` arm; both stay off (`LEDGER_ENABLED`, `SPAN_ENABLED`).

- **Timeline ledger** (`d9ffe8d`): routed aggregation/synthesis questions get one composite item first — notes from the reranked order, near-duplicates merged (cosine ≥ 0.93), one dated line per fact, oldest first. LME 77 routed questions: 47 vs 48 (+4/−5); LoCoMo 23 routed: 11 vs 12. Reading the ledgers: 57 lines for "how many instruments do I own", mixing assistant suggestions and unrelated people; paraphrased repeats survive the 0.93 merge; the errors are judgement calls (a drum set the user is selling, a ukulele they are considering) that ordering does not fix. Cost ¥3.8.
- **Memory-span anchor** (`23461fa`): one item stating the earliest and latest conversation dates, because the answer templates carry no question date (on LME the question is asked on the last session's day for 97 % of non-temporal questions; temporal-reasoning median gap 4 days). LME 100: 60 vs 61 (+5/−6); the flips are spread over questions with no relative-time component, i.e. noise. Cost ¥4.5.

Noise note: changing one line of context flips about 10 % of answers at temperature 0 with this proxy answer model, so effects under about 5 points per 100 questions are not measurable here.

## Whole-history fill (10-08): no gain with this answer proxy

`HISTORY_ENABLED`: routed aggregation/synthesis questions keep 30 precise hits, then the user's raw messages in chronological session blocks up to 100k tokens (the platform's answer input cap is 117,760; LME-S haystacks are ~102k, LoCoMo conversations ~18k). The first context had 92 items / 98k tokens with all three gold turns inside.

LME 77 routed: 47 vs cap 20 48 (+5/−6). LoCoMo 23 routed: 10 vs 12 (+1/−3). The answer model counted differently with the whole history in front of it, not better: "three" where cap-20 had listed the four events by name. With qwen-plus as the stand-in reader, more context did not turn into more correct counts. Cost ¥12. Stays off.

`FORGET_ENABLED` (forget/retract directives promoted to rank 1 on overlapping questions) cannot be measured locally: LongMemEval has no such directives (63 of 122k user turns match the detector, nearly all incidental uses of "forget"). It is harmless on these sets and is a candidate for the second shot on that basis only.

## Timeline chain at Search time (10-09): the model does what the ledger could not

`CHAIN_ENABLED` (`app/chain.py`, commit `f9d200a`): the same routed questions (how many / how often / what kinds of / over time / summarize…) send the top `CHAIN_TOPN`=50 reranked candidates, rendered exactly as they would be returned, plus the question to gpt-4o-mini-2024-07-18 (temperature 0, seed 7, json_object). The model returns dated entries with source numbers; entries whose sources do not exist are dropped, the rest are sorted by date and returned as the first item (`chain:<sha>`), the originals follow unchanged. The prompt forbids counting, totals, comparison, conclusions and anything not in the excerpts (organizer Q18: organized memories are allowed, answers disguised as memories are not; a confirmation request went to the organizer on 10-09). No model, timeout or unparsable output → no chain, Search unchanged; `/health` has a `chain` block. Cache shares `extract_cache` (prompt prefix `c1`); `EXTRACT_TOKEN_CAP` covers extract + chain.

Why it might work where the ledger did not: the ledger merged by vector cosine and listed every note, so paraphrased repeats survived and assistant suggestions and other people's events came along; the misses on counting questions were judgement calls (a drum set being sold, a ukulele under consideration). The chain asks the model to make exactly those calls, and only those.

Routing rate: LongMemEval-S 194/500 (multi-session 76, temporal 55, knowledge-update 42), LoCoMo 273/1986. Smoke: one LME question, 4.2 s, ¥0.04 of Bailian plus the model call. Full-scale estimate at 30 % routed: ~2,900 calls × (8k in + 0.8k out) ≈ $5.

Result (10-09, same 77 + 23 routed questions, paired with `cap 20`):

| arm | LME 77 | vs cap 20 | KU 18 | MS 36 | TR 23 | LoCoMo 23 | vs cap 20 |
|---|---|---|---|---|---|---|---|
| cap 20 | 48 | | 18 | 20 | 10 | 12 | |
| chain c1 | 47 | +6 / −7 | 17 | 18 | 12 | 11 | +0 / −1 |
| chain c2 (scan step, softer header) | 47 | +6 / −7 | 17 | 19 | 11 | – | |

Every LME question got a chain (77/77), 4.3 s mean latency, 3 entries median. Reading the flips: the chain is usually right (bakes 4/4, instruments 4 owned + drum set for sale + ukulele considered, bereavement both mentions) and the proxy answer model still miscounts with it in front; where the chain is wrong it is wrong by omission, and a confident three-line list at rank 1 then outweighs the originals ("How many movie festivals": the Seattle mention sat at candidate #42 of 50 and gpt-4o-mini left it out in both prompt versions, c1 folding it into the Portland entry; the answer went from "four" to "three"). Chain vs raw is +12/−5, but cap 20 already captures that. Cost of both rounds ≈ ¥14 Bailian + ≈ $0.5 relay. Stays off; the code remains behind `CHAIN_ENABLED` with the prompt and parameters declared, in case the organizer's answer to the 10-09 compliance question and a stronger reader change the picture.
