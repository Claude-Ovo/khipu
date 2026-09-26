"""Postgres 连接池与表结构。原文逐字落库，向量是可重建的派生列。"""
from __future__ import annotations

from psycopg_pool import ConnectionPool
from pgvector.psycopg import register_vector

from . import config

pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=8, open=False, configure=register_vector)

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
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
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
    exact_dup_of  TEXT,
    embedding     vector({config.EMBED_DIM}),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS segments_user_seq ON segments (user_id, session_id, seq, part);
CREATE INDEX IF NOT EXISTS segments_user_sha ON segments (user_id, content_sha);
CREATE INDEX IF NOT EXISTS segments_user_novec ON segments (user_id) WHERE embedding IS NULL;
"""


def init_schema() -> None:
    with pool.connection() as conn:
        conn.execute(SCHEMA)
        conn.commit()
