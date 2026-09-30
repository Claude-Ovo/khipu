# Review pack: fusion change A/B on LoCoMo multi-hop (2026-10-01)

Everything needed to audit the claims in REPORT.md. No keys, tokens or connection strings are included.

| file | what |
|---|---|
| REPORT.md | the full diagnostic report (§1 relaxed audit → §9 A/B results → §10 status/rollback → §11 answer-level plan) |
| CHANGED-QUESTIONS.md / changed-questions-contexts.jsonl | the 15 questions whose exact-turn outcome changed: question, gold answer, evidence text, per-turn ranks in both arms, audit verdicts, both arms' full returned contexts |
| fusion-pair-analysis.txt | machine output of tests/analyze_fusion_pair.py on the A/B file (metrics, changed questions, focus dump) |
| label-audit-codex.md / .jsonl | Codex's 30-item annotation audit (09-30) |
| relaxed-audit-claude.jsonl | the 137-item / 90-question relaxed audit (10-01, six agents) |
| LoCoMo-rerank-pair.md | the 09-30 rerank off/on experiment this builds on |
| CODE-AND-CONFIG.md | commits, instance, effective configuration, full diff of the fusion change, how the old rule was re-created |
| *.summary-0.json | usage/cost/config records of the three runs |

## Large files kept outside the pack (local machine, not on GitHub)

Base directory: the local `collab/诊断-事实与多跳-20260930/` folder on the development machine (not in this repository)

- `fusion-sim/data/fusion-pair.jsonl` — both arms' full traces (channels, fused order, rerank window+scores, final), 282 lines; 63.5 MB; md5 072c8fa9e59e080f703d08d695d30b09
- `fusion-sim/data/fusion-pair-contexts.jsonl` — both arms' rendered contexts for all 282 questions; 16.1 MB; md5 913fb5a6fda1f50323d63f0c54a9c682
- `fusion-sim/data/channels-locomo.jsonl` — rerank-off channel replay, 1982 LoCoMo questions; 17.4 MB; md5 10836e87b4dd9a959594a4ee4da6b0f2
- `fusion-sim/data/channels-lme.jsonl` — rerank-off channel replay, 200 LME questions; 1.7 MB; md5 16126a8931ed8434b59be21628605086
- `data/pair.jsonl` — 09-30 rerank off/on paired traces; 51.1 MB; md5 603c7d9a3031e24f9fbe122a039e00bf
- `data/locomo10.json` — dataset; 2.8 MB; md5 03e2a21d45726cd5ad29602c4f9998e0
- `data/missing_evidence.jsonl` — 137 missing gold items (09-30); 0.6 MB; md5 d9725afa29d15a43bf87f8bb7f287285

Server copies: `/srv/aml/data/diag-locomo/` on Morrow (same file names).

## Status (2026-10-01 08:0x)

Round closed. Conclusion: the candidate fusion (47e590a) is not yet shown to be worth adopting; it stays on `second-shot` / `:8082`, is not selected for the second Full, and nothing further is queued on it. `TIER-A.md` holds the answer-level comparison with post-review corrections (12a–12g); `SECOND-SHOT-TODO.md` lists what is still open for the second shot as a whole, with evidence and minimal verification cost per item.
