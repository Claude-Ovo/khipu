"""Postgres 连接池与表结构。原文逐字落库，向量是可重建的派生列。
段的主键是 (user_id, id)：user_id 是唯一隔离范围，session_id 不保证全局唯一（审查 #3 P0-03）。"""
from __future__ import annotations

from psycopg_pool import ConnectionPool
from pgvector.psycopg import register_vector

from . import config

# 会话时区固定 UTC（桌面日志 #3）：平台把数据集里的钟面时间当 UTC 存成毫秒；服务器本地是 +08，以前 ts_value 读回来带 +08，
# 内容头、按日期索引、BM25 里的日期串都比 created_at 快 8 小时，UTC 16:00 以后的会话在正文里显示成第二天。
pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=8, open=False, configure=register_vector,
                      kwargs={"options": "-c TimeZone=UTC"})

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS requests (
    user_id     TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    payload_sha TEXT NOT NULL,
    response    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, request_id)
);

CREATE TABLE IF NOT EXISTS segments (
    user_id       TEXT NOT NULL,
    id            TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    part          INTEGER NOT NULL DEFAULT 1,
    total         INTEGER NOT NULL DEFAULT 1,
    role          TEXT NOT NULL,
    speaker_name  TEXT,
    ts_value      TIMESTAMPTZ,
    ts_granularity TEXT NOT NULL DEFAULT 'unknown',
    ts_provenance TEXT NOT NULL DEFAULT 'unknown',
    text          TEXT NOT NULL,
    content_sha   TEXT NOT NULL,
    is_rule       BOOLEAN NOT NULL DEFAULT false,
    embedding     vector({config.EMBED_DIM}),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, id)
);
CREATE INDEX IF NOT EXISTS segments_user_seq ON segments (user_id, session_id, seq, part);
CREATE INDEX IF NOT EXISTS segments_user_novec ON segments (user_id) WHERE embedding IS NULL;

-- B3 抽取（2026-10-06）：kind = msg（原文）/ note（抽出的事实行）/ summary（会话摘要）；note_key 是事实的话题键（user.job 之类）。
-- 旧库原地加列，已有的行全是 msg。
ALTER TABLE segments ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'msg';
ALTER TABLE segments ADD COLUMN IF NOT EXISTS note_key TEXT;

-- 抽取结果按输入哈希缓存：同一段原文（同模型、同提示词版本）只调一次模型。也是交给主办方核对的抽取存档。
CREATE TABLE IF NOT EXISTS extract_cache (
    input_sha      TEXT PRIMARY KEY,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    output         JSONB NOT NULL,
    status         TEXT NOT NULL,
    prompt_tokens  INTEGER,
    completion_tokens INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def init_schema() -> None:
    with pool.connection() as conn:
        # 旧版 segments 的主键只有 id（v0.2）；结构不同就整表重建，库里只有回放数据
        row = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_name='segments' AND column_name='exact_dup_of'").fetchone()
        if row:
            conn.execute("DROP TABLE segments")
        conn.execute(SCHEMA)
        conn.commit()
