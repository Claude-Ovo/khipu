#!/usr/bin/env bash
# 把 OpenRouter key 放到 Morrow 的 /srv/aml/.openrouter（600，不进仓库、不进 .env/.env2，不重启任何服务）。
# key 从本机剪贴板读（先在 OpenRouter 页面上复制），经 SSH 标准输入传过去，不进命令行参数、不打印。
# 装好后用免费的 GET /api/v1/key 验一下，只打印额度和用量。用法：bash deploy/set-openrouter-key.sh
set -euo pipefail
K="${OPENROUTER_API_KEY:-$(powershell.exe -NoProfile -Command Get-Clipboard 2>/dev/null | tr -d '\r\n')}"
case "$K" in
  sk-or-*) ;;
  *) echo "clipboard does not hold an OpenRouter key (expected sk-or-...); copy it first"; exit 1 ;;
esac
printf '%s' "$K" | ssh morrow 'IFS= read -r K; umask 077; printf "OPENROUTER_API_KEY=%s\n" "$K" > /srv/aml/.openrouter; chmod 600 /srv/aml/.openrouter
curl -s -m 15 https://openrouter.ai/api/v1/key -H "Authorization: Bearer $K" | python3 -c "
import json, sys
d = json.load(sys.stdin).get(\"data\", {})
print(\"label:\", d.get(\"label\"), \"| limit:\", d.get(\"limit\"), \"| usage:\", d.get(\"usage\"), \"| free tier:\", d.get(\"is_free_tier\"))
"'
unset K
echo "key stored on Morrow at /srv/aml/.openrouter"
