"""deploy-v2.sh 写出的 .env2 按 systemd EnvironmentFile 的语义读一遍（Codex #15 A：行尾 # 不是注释，会连注释一起成为值）。
用法：.venv/Scripts/python.exe -m pytest tests/test_deploy_env.py -q"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env2_lines(script: str) -> list[str]:
    body = re.search(r"cat > /srv/aml/\.env2 <<ENV\n(.*?)\nENV\n", script, re.S)
    assert body, "heredoc for .env2 not found"
    return body.group(1).splitlines()


def _systemd_env(lines: list[str]) -> dict[str, str]:
    """systemd.exec EnvironmentFile：整行以 # 或 ; 开头才是注释；赋值行只去首尾空白，值里的 # 原样保留。"""
    env: dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        k, _, v = s.partition("=")
        env[k.strip()] = v.strip()
    return env


def test_env2_assignments_have_no_trailing_comments_and_flags_are_exact():
    lines = _env2_lines((ROOT / "deploy" / "deploy-v2.sh").read_text(encoding="utf-8"))
    bad = [line for line in lines if line.strip() and line.strip()[0] not in "#;" and re.search(r"\s#", line)]
    assert bad == [], bad
    env = _systemd_env(lines)
    assert all(re.fullmatch(r"[A-Z0-9_]+", k) for k in env), list(env)
    assert env["NOTE_SOURCE_PACK"] == "1"
    assert env["EXTRACT_ENABLED"] == "1" and env["NOTES_IN_SEARCH"] == "1" and env["RERANK_ENABLED"] == "1"
    assert env["NOTES_MAX_RETURNED"] == "20" and env["NOTES_MAX_CANDIDATES"] == "40"
    for k in ("SUMMARY_ROUTE_ENABLED", "SUMMARY_ROUTE_ALL_SUMMARIES", "SUMMARY_NOTES_MAX_RETURNED", "NOTE_EVENT_DATES",
              "FORGET_ENABLED", "CHAIN_ENABLED", "HISTORY_ENABLED", "LEDGER_ENABLED", "SPAN_ENABLED", "HOP_ENABLED",
              "EXTRACT_MARK_LATEST"):
        assert env[k] == "0", k
    assert env["EXTRACT_TOKEN_CAP"] == "$CAP"   # 由部署模式填：rehearsal=3000000 / full=0
