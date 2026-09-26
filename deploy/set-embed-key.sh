#!/usr/bin/env bash
# 把百炼 key 写进 Morrow 的 /srv/aml/.env 并重启服务；key 从本机环境变量 DASHSCOPE_API_KEY 或第一个参数来，不进仓库不进日志。
set -euo pipefail
KEY="${1:-${DASHSCOPE_API_KEY:-}}"
[ -n "$KEY" ] || { echo "usage: DASHSCOPE_API_KEY=sk-... bash deploy/set-embed-key.sh   (or pass the key as \$1)"; exit 1; }
ssh morrow "sudo sed -i '/^DASHSCOPE_API_KEY=/d' /srv/aml/.env && echo 'DASHSCOPE_API_KEY=$KEY' | sudo tee -a /srv/aml/.env >/dev/null && sudo systemctl restart aml && sleep 3 && curl -sf http://127.0.0.1:8080/health && echo && sudo journalctl -u aml -n 3 --no-pager | tail -3"
echo "key installed; backfill loop will start embedding existing segments"
