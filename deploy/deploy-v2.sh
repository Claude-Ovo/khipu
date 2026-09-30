#!/usr/bin/env bash
# 第二枪实例：本机 -> Morrow /srv/aml/app2，独立库 aml2，端口 8082，Caddy 路径 /v2/*。
# 不碰 :8080 和它的库（第一次 Full 的状态要留给主办方复现）。在本机仓库根目录跑：bash deploy/deploy-v2.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
git rev-parse HEAD > COMMIT 2>/dev/null || echo unknown > COMMIT
tar czf - --exclude='__pycache__' app tests deploy requirements.txt COMMIT | ssh morrow 'mkdir -p /srv/aml/app2 && tar xzf - -C /srv/aml/app2'
ssh morrow bash -s <<'EOF'
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
cat > /srv/aml/.env2 <<'ENV'
DATABASE_URL=postgresql://aml:aml-local-only@127.0.0.1:5432/aml2
RERANK_ENABLED=1
EMBED_TIMEOUT_S=20
EMBED_CONNECT_TIMEOUT_S=5
RERANK_TIMEOUT_S=45
RERANK_ATTEMPT_TIMEOUT_S=20
SEARCH_TIMEOUT_S=30
INDEX_CACHE_USERS=16
ENV

echo "== systemd aml2"
sudo cp deploy/aml2.service /etc/systemd/system/aml2.service
sudo systemctl daemon-reload
sudo systemctl enable aml2 >/dev/null 2>&1 || true
sudo systemctl restart aml2

echo "== Caddy：校验通过才替换并平滑重载"
sudo caddy validate --config deploy/Caddyfile --adapter caddyfile >/dev/null
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M)
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy

for i in $(seq 1 20); do curl -sf -m 5 http://127.0.0.1:8082/health >/dev/null && break || sleep 1; done
echo "8082: $(curl -s -m 5 http://127.0.0.1:8082/health | cut -c1-80)"
echo "8080: $(curl -s -m 5 http://127.0.0.1:8080/health | cut -c1-80)"
echo "public /v2: $(curl -s -m 10 https://43.128.132.126/v2/health | cut -c1-80)"
echo "public /:   $(curl -s -m 10 https://43.128.132.126/health | cut -c1-80)"
sudo systemctl --no-pager --lines=3 status aml2 | tail -3
EOF
