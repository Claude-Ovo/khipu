#!/usr/bin/env bash
# 本机 -> Morrow：打包 app/ tests/ deploy/ requirements.txt，装依赖，重启服务，跑健康检查。
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
tar czf - --exclude='__pycache__' app tests deploy requirements.txt | ssh morrow 'mkdir -p /srv/aml/app && tar xzf - -C /srv/aml/app'
ssh morrow bash -s <<'EOF'
set -e
cd /srv/aml/app
/srv/aml/.venv/bin/pip install -q -r requirements.txt
if [ ! -f /srv/aml/.env ]; then
  # 首次部署：不用示例文件里的公开值，生成随机 token 并只让本用户可读；token 不打印
  umask 077
  sed 's/^AML_API_TOKEN=.*/AML_API_TOKEN='"$(openssl rand -hex 24)"'/' deploy/env.example > /srv/aml/.env
  echo "generated /srv/aml/.env with a fresh AML_API_TOKEN (not shown)"
fi
sudo cp deploy/aml.service /etc/systemd/system/aml.service
sudo systemctl daemon-reload
sudo systemctl enable --now aml >/dev/null
sudo systemctl restart aml
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
for i in $(seq 1 20); do curl -sf -m 5 http://127.0.0.1:8080/health && break || sleep 1; done; echo
sudo systemctl --no-pager --lines=5 status aml | tail -5
EOF
