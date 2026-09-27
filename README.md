# Homing（洄游）

Entry for the 2nd Agent Memory Challenge (Agent Memory Leaderboard), textual track, open-source methods group. Salmon grow up at sea and swim back to the river they were born in, guided by its smell; growing and returning are one motion. Repository working name: aml-memory.

The system exposes only the two endpoints the platform calls, `POST /add` and `POST /search` (plus an unauthenticated `GET /health`), and holds one position: **memory that does not rewrite what it stores.** Every message is kept verbatim, cut into small self-describing segments that carry their own date, weekday and speaker, and returned in an order chosen by relevance alone. No LLM is used anywhere in v1.

## How it works

**Add.** The platform sends conversation chunks (≤20 messages or ≤2000 words). Each message becomes one segment; long messages are split at sentence boundaries into parts. Timestamps are taken from the message, inherited from neighbours in the same request or session when missing, and marked `unknown` otherwise; the server clock is never used. Writes are idempotent on `(user_id, request_id)`; byte-identical text is flagged as a duplicate but kept. A BM25 index, an entity table (rule-based names with alias normalisation) and a date table are built per user; embeddings (`text-embedding-v4`) are computed synchronously when possible and back-filled otherwise.

**Search.** Five channels can admit a segment: BM25, vector similarity, an entity channel (all segments about a person named in the query, newest first), a literal-match guard (names, numbers, dates that appear verbatim), and a date channel for temporal questions. Only these channels open the door; nothing about a memory's age or length can. Channels are fused with reciprocal rank fusion (k=60) with weights chosen by a rule-based intent (temporal / latest / aggregate / who / default); segments hit by both BM25 and vector are moved to the front. A small quota of "rule" segments (imperatives such as *from now on…*) is added only when the query is a task request. A few neighbouring turns of the top anchors are appended after the first twenty hits. Results are packed whole under a token budget, never truncated, and capped at `top_k`.

**Content format.** `[2023-05-08 (Mon)] Caroline: <verbatim text>`; `created_at` is date-only when the source has no time of day.

## Running

```
pip install -r requirements.txt
cp deploy/env.example .env   # DATABASE_URL, AML_API_TOKEN (set a real one), DASHSCOPE_API_KEY
uvicorn app.main:app --host 127.0.0.1 --port 8080 --env-file .env
python tests/contract_smoke.py http://127.0.0.1:8080 <token>
python tests/replay_locomo.py --base http://127.0.0.1:8080 --token <token> --data data/locomo10.json
```

Requires PostgreSQL 16 with the `vector` extension. `deploy/` holds the systemd unit, Caddy config and a deploy script used for the competition server.

## Results so far (LoCoMo replay, any-hit@k on gold evidence, 1982 questions)

| version | any@10 | any@100 |
|---|---|---|
| BM25 + entity + literal + date, neighbours 20 %, rule slot always on | 0.472 | 0.779 |
| same, neighbours and rule slot off | 0.539 | 0.809 |
| v0.2 (neighbours 5 % placed after the first 20 hits; rule slot only for task requests) | 0.539 | 0.806 |
| v0.3.1 + vector channel (text-embedding-v4) | 0.659 | 0.878 |

LongMemEval-S (500 questions, one 115k-token haystack each, v0.2, no vectors): answer session in top 10 for 90.8 % of questions and in top 100 for 99.8 %; the exact answer turn in top 10 for 55.2 % and in top 100 for 89.2 %. Weakest type is single-session-preference (answer turn in top 10: 20 %), where question and evidence share no vocabulary.

The numbers above are BM25-only. With the vector channel (v0.3.1, first 200 questions of LongMemEval-S, all 30 preference questions included): answer session in top 10 for 98 %, exact answer turn in top 10 for 79 % and in top 100 for 92 %. single-session-preference turn@10 went from 20 % to 63 %. p50 search latency 0.9 s at top_k 100.

## Attribution

This is original code, but the design borrows ideas from four open-source projects, read in source. See `ATTRIBUTION.md` for what was taken from each and what was changed. No code was copied.

- [twig-memory](https://github.com/qimingjiu/twig-memory) — the shape of a minimal AML adapter and its offline LoCoMo replay
- [MemoryConstellations](https://github.com/ClaraShafiq/MemoryConstellations) — RRF fusion, intent-weighted channels, entity channel with a virtual rank, dual-hit confidence
- [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) — verbatim storage, whole-unit packing, literal-match guard, relevance-only admission, idempotent writes
- [write-him-back](https://github.com/reneyuxi0402/write-him-back) — every returned memory must be self-sufficient for a reader with no context

## License

MIT.
