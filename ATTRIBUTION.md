# Attribution

The competition rules for the open-source group ask entrants to state what prior work was reused and what was changed. This entry contains no copied code. The following design ideas were taken from reading the source of four projects (shallow clones dated 2026-09-26); each is listed with what we adopted and how it differs here.

## twig-memory (qimingjiu/twig-memory, TypeScript)

Adopted: the shape of a minimal AML adapter (`server/aml.ts`): one message per memory unit, a `[date] speaker: text` content prefix, BM25 with k1=1.5 / b=0.75 over text that includes the date string, BM25 and vector fused by reciprocal rank fusion with k=60 taking the top 2·k from each arm, multiple-choice options appended to the query, a per-stage timeout with BM25 as the fallback so a search never returns empty. The offline LoCoMo replay (`aml-replay.ts`, platform chunking of 20 messages / 2000 words, hit@k on gold evidence) was reimplemented in Python as `tests/replay_locomo.py`.

Changed: speaker names are kept (twig's AML path reduced speakers to user/assistant); missing timestamps are inherited from the same request or session or marked unknown instead of defaulting to the server date; long messages are split at sentence boundaries; entity, literal and date channels were added; indexes are built at write time rather than lazily. The narrative engine (threads, claims, decay) was not used.

## MemoryConstellations (ClaraShafiq/MemoryConstellations, Node.js)

Adopted: rule-based query intent routing with per-intent channel weights; an entity channel whose results enter fusion at a fixed virtual rank; treating a hit on both lexical and vector channels as higher confidence and ordering those first; deterministic name normalisation (mutual containment or bigram overlap ≥ 0.5, with protection for short generic names); keeping the provenance chain from a memory back to its source messages; content-hash de-duplication that treats repeats on different days as independent evidence.

Changed: the LLM extraction, consolidation and lifecycle stages were not used (they rewrite text and delete information); confidence tiers by age and the read-count novelty penalty were dropped, since the evaluation is offline and old facts must still be answered.

## Ombre-Brain (P0luz/Ombre-Brain, Python)

Adopted as discipline rather than engine: store text verbatim and return it verbatim; pack results as whole units under a token budget and never truncate; a literal-match guard that admits segments containing the query's names, numbers or dates; admission decided only by relevance signals (the project's own code comments describe its composite gate as debt and sketch the relevance-only shape we implemented); idempotent writes with content hashing; no merging without a judge; embeddings treated as a rebuildable derivative that never blocks persistence.

Changed: decay, surfacing quotas, feel/plan/letter types and the seven-dimension composite score were not used; the segment carries an explicit event timestamp field, which Ombre-Brain does not have.

## write-him-back (reneyuxi0402/write-him-back, documentation only)

Adopted as a design constraint: every returned memory must be readable on its own by a reader with no other context, so each segment carries speaker, date and the original wording; memories are returned as original text, not summaries ("tags do not travel; scenes do").

## Not used

No benchmark data, prompts, judge code or answers from the competition organisers are included in this repository. The platform's public evaluation pipelines were read to understand the scoring contract and are referenced, not vendored.
