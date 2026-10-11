# Attribution

The competition rules for the open-source group ask entrants to state what prior work was reused and what was changed. This entry contains no copied code. Most of the retrieval design is our own earlier work (first section); the remaining sections list the outside projects whose source we read and what we took from each. Listed per project is only what we did not already have.

## Our own prior work (2026-07 to 2026-09, before the competition)

- **Tidemark** (Claude-Ovo/tidemark, hackathon entry, 2026-07/08): idempotent writes keyed by content hash, verbatim storage with the original text returned unchanged, embeddings treated as a rebuildable derivative that never blocks persistence, receipts that tie every stored unit to its source.
- **ob-recall** (private retrieval sidecar for our personal memory store, 2026-08/09): the multi-channel retrieval core reused here almost as-is: lexical (BM25), vector, literal-match and topic channels fused by weighted reciprocal rank fusion; admission decided only by relevance signals (priors may reorder, never admit); a cross-encoder reranker with a cutoff and circuit breaker; near-duplicate folding; packing whole units under a token budget without truncation; abstaining when nothing clears the bar.

What was new for this entry: the Add/Search contract, the `[date] speaker:` rendering, time-stamp inheritance, sentence-boundary splitting of long messages, date and entity channels, the offline replays, the Add-time extraction (B3) and the multimodal layer.

## twig-memory (qimingjiu/twig-memory, TypeScript)

Adopted: the shape of a minimal AML adapter (`server/aml.ts`): one message per memory unit, a `[date] speaker: text` content prefix, multiple-choice options appended to the query, a per-stage timeout with BM25 as the fallback so a search never returns empty; its BM25 parameters (k1=1.5, b=0.75) and RRF constant (k=60, top 2·k from each arm) were kept for comparability. The offline LoCoMo replay (`aml-replay.ts`, platform chunking of 20 messages / 2000 words, hit@k on gold evidence) was reimplemented in Python as `tests/replay_locomo.py`.

Changed: speaker names are kept (twig's AML path reduced speakers to user/assistant); missing timestamps are inherited from the same request or session or marked unknown instead of defaulting to the server date; long messages are split at sentence boundaries; indexes are built at write time rather than lazily. The narrative engine (threads, claims, decay) was not used.

## MemoryConstellations (ClaraShafiq/MemoryConstellations, Node.js)

Adopted: rule-based query intent routing with per-intent channel weights; an entity channel whose results enter fusion at a fixed virtual rank; deterministic person-name normalisation (mutual containment or bigram overlap ≥ 0.5, with protection for short generic names).

Changed: the LLM extraction, consolidation and lifecycle stages were not used (they rewrite text and delete information); confidence tiers by age and the read-count novelty penalty were dropped, since the evaluation is offline and old facts must still be answered.

## Ombre-Brain (P0luz/Ombre-Brain, Python)

We use Ombre-Brain daily as a memory store; ob-recall above was written as a sidecar for it. Taken from it as discipline rather than engine: no merging or rewriting of stored text without a judge, and its own code comments on a relevance-only admission gate, which is the shape ob-recall and then this entry implemented.

Changed: decay, surfacing quotas, feel/plan/letter types and the seven-dimension composite score were not used; the segment here carries an explicit event timestamp field, which Ombre-Brain does not have.

## write-him-back (reneyuxi0402/write-him-back, documentation only)

Adopted as a design constraint: every returned memory must be readable on its own by a reader with no other context, so each segment carries speaker, date and the original wording ("tags do not travel; scenes do").

## Not used

No benchmark data, prompts, judge code or answers from the competition organisers are included in this repository. The platform's public evaluation pipelines were read to understand the scoring contract and are referenced, not vendored.
