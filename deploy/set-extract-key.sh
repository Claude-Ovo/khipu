#!/usr/bin/env bash
# 把 B3 抽取用的中转 key 放到 Morrow 的 /srv/aml/.extract（600，不进仓库、不进 .env/.env2，不重启任何服务），
# 然后在 Morrow 上跑 tests/probe_relay.py 体检这个中转（花费不到一分钱）。
# key 从本机剪贴板读（先在中转站页面上复制），经 SSH 标准输入传过去，不进命令行参数、不打印。
# 用法：bash deploy/set-extract-key.sh [base_url，默认 https://aihubmix.com/v1]
set -euo pipefail
BASE="${1:-https://aihubmix.com/v1}"
case "$BASE" in https://*) ;; *) echo "base url must start with https://"; exit 1 ;; esac
HERE="$(cd "$(dirname "$0")/.." && pwd)"
K="${EXTRACT_API_KEY:-$(powershell.exe -NoProfile -Command Get-Clipboard 2>/dev/null | tr -d '\r\n')}"
case "$K" in
  sk-*) ;;
  *) echo "clipboard does not hold an API key (expected sk-...); copy it first"; exit 1 ;;
esac
ssh morrow 'mkdir -p /srv/aml/tools' && scp -q "$HERE/tests/probe_relay.py" morrow:/srv/aml/tools/probe_relay.py
printf '%s' "$K" | ssh morrow "IFS= read -r K; umask 077; printf 'EXTRACT_API_KEY=%s\nEXTRACT_BASE_URL=%s\n' \"\$K\" '$BASE' > /srv/aml/.extract; chmod 600 /srv/aml/.extract; set -a; . /srv/aml/.extract; set +a; /srv/aml/.venv/bin/python /srv/aml/tools/probe_relay.py"
unset K
echo "key stored on Morrow at /srv/aml/.extract"
