# B3 — Add-time extraction with gpt-4o-mini

Branch `b3-extract`, written 2026-10-06. Off by default (`EXTRACT_ENABLED` unset). Not validated on answers yet; nothing here has been run against a paid model.

## Why

First Full (48.01): the lowest abilities were pattern discovery 16, long-history synthesis 18, deletion 20, new value overrides 27. Storing raw text verbatim and returning 100 fragments cannot answer "walk me through the history" questions. The 10-02 diagnostics also showed that with only gold evidence in context the stand-in answer model still fails on arithmetic, categories, relative dates ("three days ago") and assistant suggestions read as user facts (`docs/diagnostics/2026-10-02-answer-layer/REPORT.md`). B2 (newest/older marking) was closed because similarity grouping could not find the true newest statement; it needs value extraction at Add time, so it was folded in here.

## What it does

For each Add request, after the empty-message filter and before embedding:

1. The messages are rendered as `[n] 2023-05-20 (Sat) 14:02 Caroline: …` lines (speaker prefix stripped from the content, role used when there is none; each message clipped to `EXTRACT_MSG_MAX_TOKENS` for extraction only) and cut at message boundaries into windows of at most `EXTRACT_WINDOW_TOKENS`. Only the request is read, never the database, so a request always produces the same windows.
2. Each window is one chat completion: `openai/gpt-4o-mini-2024-07-18` on OpenRouter, `temperature 0`, `seed 7`, `response_format json_object`, `provider: {only: [openai, azure], allow_fallbacks: false, require_parameters: true}`.
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

## Reproducibility and failure handling

The organizer replays Add/Search with their own keys and rejects large divergence.

- **Cache.** `extract_cache` is keyed by the SHA-256 of the full request body (model, prompt, seed, provider constraints, window text). One paid call per distinct window; platform retries and same-content requests reuse it. Changing the prompt changes the key without a manual version bump. The table doubles as an archive of every extraction output (`raw` model text, parsed items, status, token counts) that can be handed to the organizer.
- **In-flight dedup.** Concurrent requests for the same window wait on the first one. Registration happens before the cache read with no `await` in between, and the leader writes the cache before unregistering, so a process never pays twice for one window.
- **Deterministic bad results** — HTTP 400, 403 (OpenRouter moderation), 413, or a reply that cannot be parsed — are kept as empty and cached. At temperature 0 a retry returns the same thing, and a 503 would make the platform retry 32 times and then interrupt the Full. Truncated JSON is salvaged: complete fact objects before the cut are kept.
- **Operational failures** — no key, 401/402/404, 408/429/5xx or timeouts after `EXTRACT_ATTEMPTS`, a 200 without `choices`, or `EXTRACT_TOKEN_CAP` reached — raise, and Add returns 503 (retryable per the contract). Nothing is written: a request is stored with its notes or not at all, same rule as embeddings.
- **Counters.** `/health` → `usage.extract`: calls, ok, failed, prompt/completion tokens, avg latency, cache hits, empty windows. A non-zero `empty` early in a run means a configuration problem (e.g. a 400 from a parameter a provider rejects) and should stop the run.

Remaining non-determinism: OpenAI does not guarantee identical output for identical requests even with a seed. A reproduction will word some facts differently; retrieval of raw messages is unaffected.

## Config

| variable | default | |
|---|---|---|
| `EXTRACT_ENABLED` | off | `1` to extract |
| `OPENROUTER_API_KEY` | — | goes in `/srv/aml/.env2`, never in the repo |
| `EXTRACT_MODEL` | `openai/gpt-4o-mini-2024-07-18` | |
| `EXTRACT_PROVIDERS` | `openai,azure` | |
| `EXTRACT_SEED` | 7 | |
| `EXTRACT_WINDOW_TOKENS` | 6000 | per call; a platform chunk (20 messages / 2,000 words) is one window |
| `EXTRACT_MSG_MAX_TOKENS` | 3000 | per message, extraction input only |
| `EXTRACT_MAX_FACTS` | 40 | parser cap; the prompt asks for at most 30 |
| `EXTRACT_MAX_OUTPUT_TOKENS` | 3000 | |
| `EXTRACT_TIMEOUT_S` / `EXTRACT_ATTEMPTS` | 60 / 3 | |
| `EXTRACT_CONCURRENCY` | 16 | own HTTP client and pool, separate from Bailian |
| `EXTRACT_TOKEN_CAP` | 0 (off) | per-process spend guard for replays |
| `EXTRACT_MARK_LATEST` | off | Search-side newest/older tags |

## Flag off = second-shot candidate

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
- OpenRouter fee 5.5 %: total ≈ $23.5–26 ≈ ¥170–190; with the 30 % margin rule, **budget ¥250 for a Full**. The archive's ¥170 assumed fewer output tokens.
- Bailian side: notes add roughly 15 M embedding tokens (≈ ¥8); rerank windows stay at 200 candidates.
- Local validation, LongMemEval dev 200 (about 4,000 windows): ≈ $5 OpenRouter + Bailian for the replay; measure the actual per-window tokens on the first 20 questions with `EXTRACT_TOKEN_CAP` set, then re-estimate before going further.

Latency: an Add now waits for one completion (≈ 1,000 output tokens, 10–20 s). At 16 concurrent Adds that is roughly 1 Add/s, so the Add phase of a Full may take several hours longer than the first one (20 h 51 m in total). Unknown: whether the platform has an Add timeout below that. Measure p50/p95 Add latency in local validation.

## Not done

- No paid call has been made; prompt quality is untested. First paid step: 20 LME questions with `EXTRACT_TOKEN_CAP`, read the notes by eye, then the 200-question answer comparison (extraction vs current, paired per question).
- User-level summaries across sessions are not built: they would depend on Add order, which the platform controls.
