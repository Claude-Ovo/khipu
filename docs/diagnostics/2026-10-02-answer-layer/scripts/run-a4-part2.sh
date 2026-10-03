#!/bin/bash
set -a; . /srv/aml/.env; . /srv/aml/.env2; set +a
P=/srv/aml/.venv/bin/python
D=/srv/aml/data/a4
echo "== NEW arm, remaining 6 questions $(date)"
cd /srv/aml/app2 && $P tests/lme_context_dump.py --out $D/contexts-new.jsonl --limit 200 --offset 194 --max-questions 6 --max-cost 1 || exit 1
echo "== answers $(date)"
cd /srv/aml/app2 && $P tests/answer_compare.py --dataset lme --arm old=$D/contexts-old.jsonl --arm new=$D/contexts-new.jsonl --out-dir $D/answers --max-cost 6 || exit 1
echo "== report $(date)"
$P tests/a4_report.py --answers $D/answers --old $D/contexts-old.jsonl --new $D/contexts-new.jsonl --out $D/report.json
echo ALL-DONE $(date)
