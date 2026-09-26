"""AML Add / Search 服务。合同：agentmemoryleaderboard.ai/api-guide。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import config
from .chunking import build_segments
from .db import init_schema, pool
from .embed import embed_texts
from .index import invalidate
from .search import search as run_search
from .textutil import date_header

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("aml")


# ---------- 模型 ----------

class Message(BaseModel):
    role: str
    content: str
    timestamp: int | float | None = None

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in ("user", "assistant"):
            raise ValueError("role must be user or assistant")
        return v

    @field_validator("content")
    @classmethod
    def _content(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("content must be a non-empty string")
        return v


class AddRequest(BaseModel):
    request_id: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    top_k: int = Field(default=100, ge=1)
    options: list[str] | None = None


# ---------- 鉴权 ----------

async def require_auth(authorization: str | None = Header(default=None), x_api_key: str | None = Header(default=None)) -> None:
    if not config.API_TOKEN:
        return
    token = None
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
            token = parts[1].strip()
    token = token or x_api_key
    if token != config.API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")


# ---------- 生命周期 ----------

async def _backfill_vectors_loop() -> None:
    """向量是可重建的派生：Add 时没做上的，后台补。有活就连着干（每批 500 条），没活歇一分钟。"""
    while True:
        try:
            if not config.EMBED_API_KEY:
                await asyncio.sleep(60)
                continue
            with pool.connection() as conn:
                rows = conn.execute("SELECT id, user_id, text, speaker_name, role, ts_value, ts_granularity FROM segments "
                                    "WHERE embedding IS NULL AND exact_dup_of IS NULL LIMIT 500").fetchall()
            if not rows:
                await asyncio.sleep(60)
                continue
            texts = [f"{date_header(r[5], r[6], '')} {r[3] or r[4]}: {r[2]}" for r in rows]
            vecs = await embed_texts(texts)
            with pool.connection() as conn:
                touched = set()
                for r, v in zip(rows, vecs):
                    if v is not None:
                        conn.execute("UPDATE segments SET embedding = %s WHERE id = %s", (np.array(v, dtype=np.float32), r[0]))
                        touched.add(r[1])
                conn.commit()
            for u in touched:
                invalidate(u)
            done = sum(v is not None for v in vecs)
            log.info("backfilled vectors for %d/%d segments", done, len(rows))
            await asyncio.sleep(0.5 if done else 30)  # 整批失败多半是限流或 key 问题，别硬冲
        except Exception as e:  # noqa: BLE001
            log.warning("backfill loop error: %s", e)
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()
    init_schema()
    task = asyncio.create_task(_backfill_vectors_loop())
    try:
        yield
    finally:
        task.cancel()
        pool.close()


app = FastAPI(title="aml-memory", lifespan=lifespan)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"success": False, "error": "internal error"})


# ---------- 接口 ----------

@app.get("/health")
async def health() -> dict[str, Any]:
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True, "service": "aml-memory"}


@app.post("/add", dependencies=[Depends(require_auth)])
async def add(req: AddRequest) -> dict[str, Any]:
    payload_sha = hashlib.sha256(json.dumps(req.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    response = {"success": True, "request_id": req.request_id, "user_id": req.user_id, "session_id": req.session_id}

    with pool.connection() as conn:
        prev = conn.execute("SELECT payload_sha, response FROM requests WHERE user_id = %s AND request_id = %s",
                            (req.user_id, req.request_id)).fetchone()
        if prev:
            if prev[0] != payload_sha:
                raise HTTPException(status_code=409, detail="request_id reused with a different payload")
            return prev[1]  # 幂等：重放返回同一个结果
        last = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM segments WHERE user_id = %s AND session_id = %s",
                            (req.user_id, req.session_id)).fetchone()[0]
        last_ts = conn.execute("SELECT ts_value, ts_granularity FROM segments WHERE user_id = %s AND session_id = %s "
                               "AND ts_value IS NOT NULL ORDER BY seq DESC, part DESC LIMIT 1",
                               (req.user_id, req.session_id)).fetchone()
    session_last_ts = (last_ts[0], last_ts[1]) if last_ts else (None, "unknown")
    segments = build_segments(req.user_id, req.session_id, req.request_id,
                              [m.model_dump() for m in req.messages], int(last) + 1, session_last_ts)

    # 向量：同步尽力做；失败不阻塞返回，后台补
    texts = [f"{date_header(s.ts_value, s.ts_granularity, '')} {s.speaker_name or s.role}: {s.text}" for s in segments]
    vecs = await embed_texts(texts)

    with pool.connection() as conn:
        existing = {r[0] for r in conn.execute("SELECT content_sha FROM segments WHERE user_id = %s", (req.user_id,))}
        for s, v in zip(segments, vecs):
            dup_of = None
            if s.content_sha in existing:
                # 逐字相同：标记，不删（跨块的重复是独立证据，只是不重复返回）
                row = conn.execute("SELECT id FROM segments WHERE user_id = %s AND content_sha = %s LIMIT 1",
                                   (req.user_id, s.content_sha)).fetchone()
                dup_of = row[0] if row else None
            conn.execute(
                "INSERT INTO segments (id, user_id, session_id, request_id, seq, part, total, role, speaker_name, "
                "ts_value, ts_granularity, ts_provenance, text, content_sha, is_rule, exact_dup_of, embedding) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                (s.id, s.user_id, s.session_id, s.request_id, s.seq, s.part, s.total, s.role, s.speaker_name,
                 s.ts_value, s.ts_granularity, s.ts_provenance, s.text, s.content_sha, s.is_rule, dup_of,
                 np.array(v, dtype=np.float32) if v is not None else None))
            existing.add(s.content_sha)
        conn.execute("INSERT INTO requests (user_id, request_id, payload_sha, response) VALUES (%s,%s,%s,%s) "
                     "ON CONFLICT DO NOTHING", (req.user_id, req.request_id, payload_sha, json.dumps(response)))
        conn.commit()
    invalidate(req.user_id)
    return response


@app.post("/search", dependencies=[Depends(require_auth)])
async def search(req: SearchRequest) -> dict[str, Any]:
    data = await run_search(req.user_id, req.query, req.options, req.top_k)
    return {"data": data}
