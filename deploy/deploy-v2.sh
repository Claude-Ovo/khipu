#!/usr/bin/env bash
# 第二枪实例：本机 -> Morrow /srv/aml/app2，独立库 aml2，端口 8082，Caddy 路径 /v2/*。
# 不碰 :8080 和它的库（第一次 Full 的状态要留给主办方复现）。在本机仓库根目录跑：
#   bash deploy/deploy-v2.sh            彩排模式：抽取累计 300 万 token 就停（Add 回 503），防回放烧钱
#   bash deploy/deploy-v2.sh full       Full 模式：不限额，额度交给中转站的 key 配额管
# 审查 #8-5：以前每次部署都把 .env2 里的 EXTRACT_TOKEN_CAP 写回 300 万，「先改 0 再部署」会被覆盖掉；现在由模式决定
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
git rev-parse HEAD > COMMIT 2>/dev/null || echo unknown > COMMIT
tar czf - --exclude='__pycache__' app tests deploy requirements.txt COMMIT | ssh morrow 'mkdir -p /srv/aml/app2 && tar xzf - -C /srv/aml/app2'
ssh morrow CAP="$CAP" MODE="$MODE" bash -s <<'EOF'
set -euo pipefail
cd /srv/aml/app2
/srv/aml/.venv/bin/pip install -q -r requirements.txt

echo "== 库 aml2（没有就建，有就不动）"
if ! sudo -u postgres psql -Atc "SELECT 1 FROM pg_database WHERE datname='aml2'" | grep -q 1; then
  sudo -u postgres createdb -O aml aml2
  sudo -u postgres psql -d aml2 -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null
  echo "created aml2"
fi

echo "== /srv/aml/.env2（只放覆盖项，没有密钥）"
umask 077
cat > /srv/aml/.env2 <<ENV
DATABASE_URL=postgresql://aml:aml-local-only@127.0.0.1:5432/aml2
RERANK_ENABLED=1
FUSION_RULE=legacy
EMBED_TIMEOUT_S=20
EMBED_CONNECT_TIMEOUT_S=5
RERANK_TIMEOUT_S=45
RERANK_ATTEMPT_TIMEOUT_S=20
SEARCH_TIMEOUT_S=30
INDEX_CACHE_USERS=16
# B3（10-09 定稿）：Add 时 gpt-4o-mini 抽取，返回里笔记最多 20 条。中转 key 在 /srv/aml/.extract（unit 里单独加载）
EXTRACT_ENABLED=1
NOTES_IN_SEARCH=1
NOTES_MAX_RETURNED=20
EXTRACT_REQUEST_TIMEOUT_S=600
# 实验开关全部显式关掉（审查 #8-5：别靠代码默认值）。FORGET 10-09 审查后关：单个实词重叠就把无关遗忘指令顶到第一，本地测不了
FORGET_ENABLED=0
CHAIN_ENABLED=0
HISTORY_ENABLED=0
LEDGER_ENABLED=0
SPAN_ENABLED=0
HOP_ENABLED=0
EXTRACT_MARK_LATEST=0
# 由部署模式写入：rehearsal=3000000（累计超了 Add 回 503），full=0（不限）
EXTRACT_TOKEN_CAP=$CAP
ENV
[ -s /srv/aml/.extract ] || { echo "!! /srv/aml/.extract missing: run deploy/set-extract-key.sh first"; exit 1; }
# .extract 是最后一层覆盖，只许放 key 和端点，不许夹开关
if grep -vE '^\s*(#|$|EXTRACT_API_KEY=|OPENROUTER_API_KEY=|EXTRACT_BASE_URL=|EXTRACT_MODEL=|EXTRACT_PROVIDERS=)' /srv/aml/.extract | grep -q .; then
  echo "!! /srv/aml/.extract contains lines other than key/endpoint:"; grep -vE '^\s*(#|$|EXTRACT_API_KEY=|OPENROUTER_API_KEY=|EXTRACT_BASE_URL=|EXTRACT_MODEL=|EXTRACT_PROVIDERS=)' /srv/aml/.extract | sed 's/=.*/=.../'; exit 1
fi

echo "== systemd aml2"
sudo cp deploy/aml2.service /etc/systemd/system/aml2.service
sudo systemctl daemon-reload
sudo systemctl enable aml2 >/dev/null 2>&1 || true
sudo systemctl restart aml2

echo "== Caddy：内容没变就不碰（第一次 Full 在审核期，前面这层代理也别动）；变了才校验、替换、重载"
if cmp -s deploy/Caddyfile /etc/caddy/Caddyfile; then
  echo "Caddyfile unchanged, not touched"
else
  sudo caddy validate --config deploy/Caddyfile --adapter caddyfile >/dev/null
  sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M)
  sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
  sudo systemctl reload caddy
fi

for i in $(seq 1 20); do curl -sf -m 5 http://127.0.0.1:8082/health >/dev/null && break || sleep 1; done
echo "8082: $(curl -s -m 5 http://127.0.0.1:8082/health | cut -c1-80)"
echo "== 进程实际读到的配置（/health.config）"
H="$(curl -s -m 5 http://127.0.0.1:8082/health)"
echo "$H" | /srv/aml/.venv/bin/python -c 'import sys,json; c=json.load(sys.stdin)["config"]; print(json.dumps(c, ensure_ascii=False))'
GOT_CAP="$(echo "$H" | /srv/aml/.venv/bin/python -c 'import sys,json; print(json.load(sys.stdin)["config"]["extract_token_cap"])')"
[ "$GOT_CAP" = "$CAP" ] || { echo "!! effective EXTRACT_TOKEN_CAP=$GOT_CAP, wanted $CAP"; exit 1; }
echo "$H" | /srv/aml/.venv/bin/python -c 'import sys,json; c=json.load(sys.stdin)["config"]; bad=[k for k in ("forget","chain","history","ledger","span","hop","mark_latest") if c[k]]; assert c["extract"] and c["extract_key_set"] and c["notes_max_returned"]==20 and not bad, (bad, c); print("config check ok")'
echo "8080: $(curl -s -m 5 http://127.0.0.1:8080/health | cut -c1-80)"
echo "public /v2: $(curl -s -m 10 https://43.128.132.126/v2/health | cut -c1-80)"
echo "public /:   $(curl -s -m 10 https://43.128.132.126/health | cut -c1-80)"
sudo systemctl --no-pager --lines=3 status aml2 | tail -3
EOF
