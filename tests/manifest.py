"""复现清单（审查 #4 第 5 条）：把一轮评测依赖的外部状态写成一份无密钥 manifest。
主办方复现时如果结果对不上，先比这份：代码提交、生效配置、模型名、库里段数与向量覆盖率、重排开关。
用法（在 Morrow 上）：/srv/aml/.venv/bin/python tests/manifest.py [--user-prefix replay:lme-s-vec-v031] > manifest.json"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.db import pool  # noqa: E402

SECRET_MARKERS = ("KEY", "TOKEN", "PASSWORD", "SECRET", "DATABASE_URL")


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def effective_config() -> dict:
    out = {}
    for name in dir(config):
        if name.isupper() and not any(m in name for m in SECRET_MARKERS):
            v = getattr(config, name)
            if isinstance(v, (set, frozenset)):
                v = sorted(v)
            out[name] = v
    out["EMBED_API_KEY_set"] = bool(config.EMBED_API_KEY)
    out["API_TOKEN_set"] = bool(config.API_TOKEN)
    return out


def db_stats(prefix: str | None) -> dict:
    pool.open()
    try:
        with pool.connection() as conn:
            where = "WHERE user_id LIKE %s" if prefix else ""
            params = (prefix + "%",) if prefix else ()
            n, nvec, nusers, nsess = conn.execute(
                f"SELECT count(*), count(embedding), count(DISTINCT user_id), count(DISTINCT session_id) FROM segments {where}", params).fetchone()
            nreq = conn.execute(f"SELECT count(*) FROM requests {where}", params).fetchone()[0]
            gran = dict(conn.execute(f"SELECT ts_granularity, count(*) FROM segments {where} GROUP BY 1", params).fetchall())
            prov = dict(conn.execute(f"SELECT ts_provenance, count(*) FROM segments {where} GROUP BY 1", params).fetchall())
            ext = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
            pg = conn.execute("SHOW server_version").fetchone()[0]
    finally:
        pool.close()
    return {"segments": n, "with_vector": nvec, "vector_coverage": round(nvec / n, 4) if n else None,
            "users": nusers, "sessions": nsess, "requests": nreq,
            "ts_granularity": gran, "ts_provenance": prov, "postgres": pg, "pgvector": ext[0] if ext else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-prefix", default="", help="只统计这个前缀的用户（如 replay:lme-s-vec-v031）")
    args = ap.parse_args()
    py = sys.version.split()[0]
    pkgs = {}
    try:
        from importlib.metadata import version
        for name in ("fastapi", "uvicorn", "psycopg", "pgvector", "rank_bm25", "httpx", "numpy", "tiktoken"):
            try:
                pkgs[name] = version(name)
            except Exception:  # noqa: BLE001
                pkgs[name] = None
    except Exception:  # noqa: BLE001
        pass
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "git": {"commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                "dirty": bool(git("status", "--porcelain"))},
        "python": py, "packages": pkgs,
        "models": {"embedding": config.EMBED_MODEL, "embedding_dim": config.EMBED_DIM, "embed_base_url": config.EMBED_BASE_URL,
                   "rerank": config.RERANK_MODEL if config.RERANK_ENABLED else None, "rerank_url": config.RERANK_URL if config.RERANK_ENABLED else None},
        "config": effective_config(),
        "db": db_stats(args.user_prefix or None),
    }
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
