#!/usr/bin/env bash
# 把百炼 key 写进 Morrow 的 /srv/aml/.env 并重启服务。key 只从本机环境变量 DASHSCOPE_API_KEY 读，
# 通过 SSH 的标准输入传过去，不进任何一端的命令行参数（审查 #3 P1-14）。
set -euo pipefail
[ -n "${DASHSCOPE_API_KEY:-}" ] || { echo "usage: DASHSCOPE_API_KEY=sk-... bash deploy/set-embed-key.sh"; exit 1; }
printf '%s' "$DASHSCOPE_API_KEY" | ssh morrow 'IFS= read -r K; umask 077; sudo sed -i "/^DASHSCOPE_API_KEY=/d" /srv/aml/.env; printf "DASHSCOPE_API_KEY=%s\n" "$K" | sudo tee -a /srv/aml/.env >/dev/null; sudo systemctl restart aml; sleep 3; curl -sf http://127.0.0.1:8080/health; echo'
echo "key installed; the backfill loop will start embedding existing segments"
