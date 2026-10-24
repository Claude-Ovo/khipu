#!/usr/bin/env bash
# 多模态赛道实例（2026-10-11，分支 mm）：本机 -> Morrow /srv/aml/app3，端口 8084，Caddy 路径 /mm/*。
# 2026-10-24 第二版 mm-2：库换成空的 aml_mm2、图片目录 /srv/aml/mm2/images（主办方 10-17：旧任务记忆不得复用于新评测，评测数据空间要隔离），
# 图片描述 detail 改 low（10-24 两臂对照：同代码、各自新库、Mem-Gallery 208 题，auto 0.654 / low 0.613；low 每张固定约 3,000 token，auto 均 1.4–1.7 万）。
# 应用代码与 3fbe817 相同，只改这份部署脚本。旧库 aml_mm 和 /srv/aml/mm/images 不碰，按清理期限另行处理。
# 不碰 :8080（第一次 Full）和 :8082（文本第二枪）。在本机仓库根目录、分支 mm 上跑：
#   bash deploy/deploy-mm.sh            彩排：抽取+描述累计 300 万 token 就停（Add 回 503）
#   bash deploy/deploy-mm.sh full       Full：不限额
set -euo pipefail
MODE="${1:-rehearsal}"
case "$MODE" in
  rehearsal) CAP=3000000 ;;
  full) CAP=0 ;;
  *) echo "usage: $0 [rehearsal|full]"; exit 2 ;;
esac
echo "== mode: $MODE (EXTRACT_TOKEN_CAP=$CAP)"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
[ "$(git branch --show-current)" = "mm" ] || { echo "!! deploy-mm.sh runs from branch mm (now: $(git branch --show-current))"; exit 2; }
git rev-parse HEAD > COMMIT 2>/dev/null || echo unknown > COMMIT
tar czf - --exclude='__pycache__' app tests deploy requirements.txt COMMIT | ssh morrow 'mkdir -p /srv/aml/app3 && tar xzf - -C /srv/aml/app3'
ssh morrow CAP="$CAP" MODE="$MODE" bash -s <<'EOF'
set -euo pipefail
cd /srv/aml/app3
/srv/aml/.venv/bin/pip install -q -r requirements.txt

echo "== 库 aml_mm2（没有就建，有就不动；mm-1 的 aml_mm 不碰）"
if ! sudo -u postgres psql -Atc "SELECT 1 FROM pg_database WHERE datname='aml_mm2'" | grep -q 1; then
  sudo -u postgres createdb -O aml aml_mm2
  sudo -u postgres psql -d aml_mm2 -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null
  echo "created aml_mm2"
fi
mkdir -p /srv/aml/mm2/images

echo "== /srv/aml/.env3（只放覆盖项，没有密钥）"
umask 077
cat > /srv/aml/.env3 <<ENV
DATABASE_URL=postgresql://aml:aml-local-only@127.0.0.1:5432/aml_mm2
RERANK_ENABLED=1
RERANK_TOPN=100
FUSION_RULE=legacy
EMBED_TIMEOUT_S=20
EMBED_CONNECT_TIMEOUT_S=5
RERANK_TIMEOUT_S=45
RERANK_ATTEMPT_TIMEOUT_S=20
SEARCH_TIMEOUT_S=30
INDEX_CACHE_USERS=16
EXTRACT_ENABLED=1
NOTES_IN_SEARCH=1
NOTES_MAX_RETURNED=20
NOTES_MAX_CANDIDATES=40
EXTRACT_REQUEST_TIMEOUT_S=600
MM_ENABLED=1
MM_IMAGE_DIR=/srv/aml/mm2/images
MM_DETAIL=low
MM_RETURN_IMAGES=5
MM_RETURN_BYTES=6291456
MM_GEOCODE=0
FORGET_ENABLED=0
CHAIN_ENABLED=0
HISTORY_ENABLED=0
LEDGER_ENABLED=0
SPAN_ENABLED=0
HOP_ENABLED=0
EXTRACT_MARK_LATEST=0
EXTRACT_TOKEN_CAP=$CAP
ENV
[ -s /srv/aml/.extract ] || { echo "!! /srv/aml/.extract missing"; exit 1; }
BAD_LINES="$(tr -d '\r' < /srv/aml/.extract | grep -vE '^\s*([#;]|$|EXTRACT_API_KEY=|OPENROUTER_API_KEY=|EXTRACT_BASE_URL=|EXTRACT_MODEL=|EXTRACT_PROVIDERS=)' | sed 's/=.*/=.../' || true)"
if [ -n "$BAD_LINES" ]; then echo "!! /srv/aml/.extract contains lines other than key/endpoint:"; echo "$BAD_LINES"; exit 1; fi

echo "== systemd aml3"
sudo cp deploy/aml3.service /etc/systemd/system/aml3.service
sudo systemctl daemon-reload
sudo systemctl enable aml3 >/dev/null 2>&1 || true
sudo systemctl restart aml3

echo "== Caddy：内容没变就不碰；变了才校验、替换、重载（/mm/* 路由已在分支 b3-extract 的 Caddyfile 里，文本部署不会把它抹掉）"
if cmp -s deploy/Caddyfile /etc/caddy/Caddyfile; then
  echo "Caddyfile unchanged, not touched"
else
  sudo caddy validate --config deploy/Caddyfile --adapter caddyfile >/dev/null
  sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M)
  sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
  sudo systemctl reload caddy
fi

for i in $(seq 1 20); do curl -sf -m 5 http://127.0.0.1:8084/health >/dev/null && break || sleep 1; done
H="$(curl -s -m 5 http://127.0.0.1:8084/health)"
echo "8084: $(echo "$H" | cut -c1-80)"
echo "$H" | /srv/aml/.venv/bin/python -c 'import sys,json; c=json.load(sys.stdin)["config"]; print(json.dumps(c, ensure_ascii=False))'
EXPECT_COMMIT="$(cat COMMIT)" EXPECT_CAP="$CAP" /srv/aml/.venv/bin/python - "$H" <<'PY'
import json, os, sys
c = json.loads(sys.argv[1])["config"]
bad = []
if c.get("commit") != os.environ["EXPECT_COMMIT"]: bad.append(f"commit={c.get('commit')}")
if str(c.get("extract_token_cap")) != os.environ["EXPECT_CAP"]: bad.append(f"extract_token_cap={c.get('extract_token_cap')}")
bad += [f"{k} should be on" for k in ("mm", "extract", "extract_key_set", "notes_in_search", "rerank") if not c.get(k)]
bad += [f"{k} should be off" for k in ("forget", "chain", "history", "ledger", "span", "hop", "mark_latest") if c.get(k)]
if c.get("extract_model") != "gpt-4o-mini-2024-07-18": bad.append(f"extract_model={c.get('extract_model')!r}")
if c.get("extract_host") != "aihubmix.com": bad.append(f"extract_host={c.get('extract_host')!r}")
if c.get("mm_detail") != "low": bad.append(f"mm_detail={c.get('mm_detail')!r} (mm-2 must run low)")
if c.get("mm_geocode"): bad.append("mm_geocode should be off")
if bad:
    print("!! config check failed:"); [print("   " + b) for b in bad]; sys.exit(1)
print("config check ok (commit, cap, mm, model, host, flags)")
PY
echo "8080: $(curl -s -m 5 http://127.0.0.1:8080/health | cut -c1-60)"
echo "8082: $(curl -s -m 5 http://127.0.0.1:8082/health | cut -c1-60)"
echo "public /mm: $(curl -s -m 10 https://43.128.132.126/mm/health | cut -c1-80)"
sudo systemctl --no-pager --lines=3 status aml3 | tail -3
EOF
