#!/usr/bin/env bash
# Full 前的一次性准备（2026-09-29 体检结论 M-2..M-5）。在本机仓库根目录跑：bash deploy/preflight-full.sh
# 只改服务器配置，不改代码；跑完 /health 正常、6 个环境变量对上、tiktoken 离线能加载才算过。
set -euo pipefail
cd "$(dirname "$0")/.."

# 新 Caddyfile 先传到服务器临时位置，校验通过再替换
scp -q deploy/Caddyfile morrow:/tmp/Caddyfile.new

ssh morrow bash -s <<'EOF'
set -euo pipefail

echo "== M-2 停掉 :8081 对照实例"
pid=$(ss -ltnp | sed -n 's/.*127.0.0.1:8081 .*pid=\([0-9]*\).*/\1/p' | head -1)
if [ -n "$pid" ]; then kill "$pid"; sleep 3; fi
echo "8081 listeners: $(ss -ltnp | grep -c '127.0.0.1:8081 ' || true)"

echo "== M-4 加 4G 交换区"
if ! swapon --show | grep -q swap2.img; then
  sudo fallocate -l 4G /swap2.img
  sudo chmod 600 /swap2.img
  sudo mkswap /swap2.img >/dev/null
  sudo swapon /swap2.img
fi
grep -q swap2.img /etc/fstab || echo "/swap2.img none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
free -m | tail -1

echo "== M-3 环境变量 + tiktoken 本地缓存"
mkdir -p /srv/aml/tiktoken-cache
cp -pn /tmp/data-gym-cache/* /srv/aml/tiktoken-cache/ 2>/dev/null || true
cp -p /srv/aml/.env /srv/aml/.env.bak-prefull
setenv() { if grep -q "^$1=" /srv/aml/.env; then sed -i "s|^$1=.*|$1=$2|" /srv/aml/.env; else echo "$1=$2" >> /srv/aml/.env; fi; }
setenv SEARCH_TIMEOUT_S 30
setenv RERANK_TIMEOUT_S 45
setenv EMBED_TIMEOUT_S 10
setenv INDEX_CACHE_USERS 16
setenv TIKTOKEN_CACHE_DIR /srv/aml/tiktoken-cache

echo "== M-5 Caddy：校验通过才替换并平滑重载"
sudo caddy validate --config /tmp/Caddyfile.new --adapter caddyfile >/dev/null 2>&1
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-prefull
sudo cp /tmp/Caddyfile.new /etc/caddy/Caddyfile
sudo cp /tmp/Caddyfile.new /srv/aml/app/deploy/Caddyfile
sudo systemctl reload caddy

echo "== 重启 aml 让新环境变量生效"
sudo systemctl restart aml
for i in $(seq 1 20); do curl -sf -m 5 --noproxy '*' http://127.0.0.1:8080/health && break || sleep 1; done; echo
tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value aml)/environ \
  | grep -E '^(SEARCH_TIMEOUT_S|RERANK_TIMEOUT_S|EMBED_TIMEOUT_S|INDEX_CACHE_USERS|TIKTOKEN_CACHE_DIR|RERANK_ENABLED)='
echo -n "tiktoken offline: "
TIKTOKEN_CACHE_DIR=/srv/aml/tiktoken-cache HTTPS_PROXY=http://127.0.0.1:9 /srv/aml/.venv/bin/python -c \
  "import tiktoken;print(len(tiktoken.get_encoding('cl100k_base').encode('hello world')))"
echo "== 完成"
EOF
