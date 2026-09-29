"""AML Add / Search 服务。合同：agentmemoryleaderboard.ai/api-guide。
写入路径（审查 #3 P0-01/02/03 之后）：一个事务里先抢 (user_id, request_id)，再对 (user_id, session_id) 上事务级咨询锁分配序号，
段和请求记录一起提交；向量在提交之后尽力补，失败交给后台回填。所有同步数据库操作都在线程池里跑，不堵事件循环。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from . import config
from .chunking import build_segments
from .db import init_schema, pool
from .embed import embed_texts
from .index import invalidate
from .search import search as run_search


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("aml")

MAX_TS_MS = 4_102_444_800_000  # 2100-01-01


# ---------- 模型 ----------

# 宽进原则（2026-09-29 Full 因 422 中断后改）：凡是以前会被 422 拒掉的输入，尽量接下来；以前能通过的输入，行为和
# payload_sha 完全不变。平台对 422 不会重试，一条它认为合法、我们认为不合法的数据就能让整场 Full 失败。

_ROLE_ALIASES = {"human": "user", "ai": "assistant", "bot": "assistant", "model": "assistant", "gpt": "assistant",
                 "chatbot": "assistant", "agent": "assistant"}


def _clean_text(s: str) -> str:
    """去掉 Postgres 存不了的 NUL，孤立代理项换成替代字符（否则 encode 时 500）。合法文本原样返回。"""
    if "\x00" in s:
        s = s.replace("\x00", "")
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        s = s.encode("utf-8", "replace").decode("utf-8")
    return s


def _content_to_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return _clean_text(v)
    if isinstance(v, list):
        # ContentPart[]（每项都是字符串或带 type 的 dict）：只取文字部分，按原顺序拼；图片不存
        if v and all(isinstance(p, str) or (isinstance(p, dict) and "type" in p) for p in v):
            parts = [p if isinstance(p, str) else p.get("text") for p in v]
            return _clean_text("\n".join(x for x in parts if isinstance(x, str)))
        return _clean_text(json.dumps(v, ensure_ascii=False))  # 其他结构化内容原样转成 JSON 文本，不丢
    if isinstance(v, dict):
        return _clean_text(json.dumps(v, ensure_ascii=False))
    return _clean_text(str(v))


_LME_TS = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s*\([A-Za-z]{3}\)\s*(\d{2}):(\d{2})")


def _parse_datetime_ms(s: str) -> int | None:
    t = s.strip().replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(t, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ms = int(dt.timestamp() * 1000)
        return ms if 0 <= ms <= MAX_TS_MS else None
    m = _LME_TS.match(t)  # LongMemEval 式 "2023/05/20 (Sat) 02:21"
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _clean_timestamp(v: Any) -> Any:
    """合法值原样返回（交给字段类型照旧处理）；越界、非数、布尔、解析不了的一律当成没给时间。"""
    if v is None:
        return None
    if isinstance(v, bool):  # 旧版把 True/False 当成 1/0 毫秒接受了；保持原样，别让重试的 payload_sha 变
        return v
    if isinstance(v, (int, float)):
        return v if (math.isfinite(v) and 0 <= v <= MAX_TS_MS) else None
    if isinstance(v, str):
        try:
            f = float(v)
        except ValueError:
            return _parse_datetime_ms(v)
        return v if (math.isfinite(f) and 0 <= f <= MAX_TS_MS) else None
    return None


class Message(BaseModel):
    role: str
    content: str
    timestamp: int | float | None = None

    @model_validator(mode="before")
    @classmethod
    def _lenient(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return {"role": "user", "content": _content_to_text(data)}
        d = dict(data)
        r = d.get("role")
        r = r.strip().lower() if isinstance(r, str) else ""
        if r not in ("user", "assistant"):
            r = _ROLE_ALIASES.get(r, "user")
        d["role"] = r
        d["content"] = _content_to_text(d.get("content"))
        if "timestamp" in d:
            d["timestamp"] = _clean_timestamp(d.get("timestamp"))
        return d

    @field_validator("timestamp")
    @classmethod
    def _ts(cls, v: int | float | None) -> int | float | None:
        if v is None:
            return None
        if isinstance(v, bool) or not math.isfinite(v) or v < 0 or v > MAX_TS_MS:
            return None
        return v


class AddRequest(BaseModel):
    request_id: str = Field(min_length=1)
    messages: list[Message] = Field(default_factory=list)
    user_id: str = Field(min_length=1)
    session_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def _lenient(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        if d.get("messages") is None:
            d["messages"] = []
        elif isinstance(d["messages"], dict):
            d["messages"] = [d["messages"]]
        for k in ("request_id", "user_id", "session_id"):
            if d.get(k) is not None and not isinstance(d[k], str):
                d[k] = str(d[k])
        if d.get("session_id") is None:
            d["session_id"] = ""
        return d


class SearchRequest(BaseModel):
    query: str = ""
    user_id: str = Field(min_length=1)
    top_k: int | None = None  # 合同必填；缺了或非正数按上限处理，不回 422
    options: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _lenient(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        q = d.get("query")
        d["query"] = "" if q is None else _clean_text(q if isinstance(q, str) else str(q))
        tk = d.get("top_k")
        if isinstance(tk, bool) or not isinstance(tk, (int, float, str)):
            d["top_k"] = None
        else:
            try:
                tk = int(float(tk))
            except (ValueError, OverflowError):
                tk = None
            d["top_k"] = tk if (tk is not None and tk >= 1) else None
        ops = d.get("options")
        if ops is not None:
            d["options"] = [_clean_text(o if isinstance(o, str) else json.dumps(o, ensure_ascii=False))
                            for o in (ops if isinstance(ops, list) else [ops]) if o is not None]
        if d.get("user_id") is not None and not isinstance(d["user_id"], str):
            d["user_id"] = str(d["user_id"])
        return d


# ---------- 鉴权 ----------

async def require_auth(authorization: str | None = Header(default=None), x_api_key: str | None = Header(default=None)) -> None:
    if not config.API_TOKEN:
        return  # 只允许在 AML_ALLOW_NO_AUTH=1 时走到这里（启动时校验）
    token = None
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
            token = parts[1].strip()
    token = token or x_api_key
    if token != config.API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")


# ---------- 数据库操作（同步，放线程池） ----------

def _embed_text_of(role: str, speaker: str | None, text: str) -> str:
    # 向量文本不带日期：日期依赖库里的继承状态，放进去就没法在事务前算；日期信号交给 BM25 索引
    return f"{speaker or role}: {text}"


def _add_transaction(req: AddRequest, payload_sha: str, vectors: dict[str, list[float]]) -> tuple[dict, list]:
    """返回 (响应, 新写入的段列表)。段列表为空表示这是重放。vectors 以「正文」为键，写入时一并落库，
    所以只要 key 配好，库里每一段都带向量，复现时不会因为回填时机不同而结果不同。"""
    response = {"success": True, "request_id": req.request_id, "user_id": req.user_id, "session_id": req.session_id}
    with pool.connection() as conn:
        with conn.transaction():
            # 1) 抢请求记录：唯一索引冲突时 Postgres 会等对方提交再返回，所以并发的同 request_id 只有一个能写
            got = conn.execute(
                "INSERT INTO requests (user_id, request_id, payload_sha, response) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (user_id, request_id) DO NOTHING RETURNING request_id",
                (req.user_id, req.request_id, payload_sha, json.dumps(response))).fetchone()
            if got is None:
                prev = conn.execute("SELECT payload_sha, response FROM requests WHERE user_id = %s AND request_id = %s",
                                    (req.user_id, req.request_id)).fetchone()
                if prev[0] != payload_sha:
                    raise HTTPException(status_code=409, detail="request_id reused with a different payload")
                return prev[1], []
            # 2) 同一用户同一会话的序号分配串行化（固定加锁顺序：先请求记录，后会话锁）
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{req.user_id}\n{req.session_id}",))
            last = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM segments WHERE user_id = %s AND session_id = %s",
                                (req.user_id, req.session_id)).fetchone()[0]
            last_ts = conn.execute("SELECT ts_value, ts_granularity FROM segments WHERE user_id = %s AND session_id = %s "
                                   "AND ts_value IS NOT NULL ORDER BY seq DESC, part DESC LIMIT 1",
                                   (req.user_id, req.session_id)).fetchone()
            session_last_ts = (last_ts[0], last_ts[1]) if last_ts else (None, "unknown")
            segments = build_segments(req.user_id, req.session_id, req.request_id,
                                      [m.model_dump() for m in req.messages], int(last) + 1, session_last_ts)
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO segments (user_id, id, session_id, request_id, seq, part, total, role, speaker_name, "
                    "ts_value, ts_granularity, ts_provenance, text, content_sha, is_rule, embedding) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [(s.user_id, s.id, s.session_id, s.request_id, s.seq, s.part, s.total, s.role, s.speaker_name,
                      s.ts_value, s.ts_granularity, s.ts_provenance, s.text, s.content_sha, s.is_rule,
                      (np.array(vectors[_embed_text_of(s.role, s.speaker_name, s.text)], dtype=np.float32)
                       if _embed_text_of(s.role, s.speaker_name, s.text) in vectors else None)) for s in segments])
    return response, segments


def _saved_response(user_id: str, request_id: str, payload_sha: str) -> dict | None:
    """重放快路径：同 (user_id, request_id) 已提交过就直接回存档，不再算向量。
    平台 Add 最多重试 32 次，没有这一步每次重试都要重新花一遍 embedding 的钱。事务里的抢占逻辑照旧兜并发。"""
    with pool.connection() as conn:
        prev = conn.execute("SELECT payload_sha, response FROM requests WHERE user_id = %s AND request_id = %s",
                            (user_id, request_id)).fetchone()
    if prev is None:
        return None
    if prev[0] != payload_sha:
        raise HTTPException(status_code=409, detail="request_id reused with a different payload")
    return prev[1]


def _store_vectors(user_id: str, ids: list[str], vecs: list[list[float] | None]) -> int:
    n = 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for sid, v in zip(ids, vecs):
                if v is not None:
                    cur.execute("UPDATE segments SET embedding = %s WHERE user_id = %s AND id = %s",
                                (np.array(v, dtype=np.float32), user_id, sid))
                    n += 1
        conn.commit()
    return n


def _pending_vectors(limit: int) -> list[tuple]:
    with pool.connection() as conn:
        return conn.execute("SELECT user_id, id, text, speaker_name, role, ts_value, ts_granularity FROM segments "
                            "WHERE embedding IS NULL LIMIT %s", (limit,)).fetchall()


def _health_check() -> None:
    with pool.connection() as conn:
        conn.execute("SELECT 1")


# ---------- 生命周期 ----------

async def _backfill_vectors_loop() -> None:
    """向量是可重建的派生：Add 时没做上的，后台补。有活就连着干（每批 500 条），没活歇一分钟。"""
    while True:
        try:
            if not config.EMBED_API_KEY:
                await asyncio.sleep(60)
                continue
            rows = await asyncio.to_thread(_pending_vectors, 500)
            if not rows:
                await asyncio.sleep(60)
                continue
            texts = [_embed_text_of(r[4], r[3], r[2]) for r in rows]
            vecs = await embed_texts(texts)
            by_user: dict[str, tuple[list, list]] = {}
            for r, v in zip(rows, vecs):
                by_user.setdefault(r[0], ([], []))
                by_user[r[0]][0].append(r[1])
                by_user[r[0]][1].append(v)
            done = 0
            for u, (ids, vs) in by_user.items():
                done += await asyncio.to_thread(_store_vectors, u, ids, vs)
                invalidate(u)
            log.info("backfilled vectors for %d/%d segments", done, len(rows))
            await asyncio.sleep(0.5 if done else 30)  # 整批失败多半是限流或 key 问题，别硬冲
        except Exception as e:  # noqa: BLE001
            log.warning("backfill loop error: %s", e)
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.API_TOKEN and not config.ALLOW_NO_AUTH:
        raise RuntimeError("AML_API_TOKEN is empty; set it, or AML_ALLOW_NO_AUTH=1 for a local smoke server")
    if config.API_TOKEN in config.PLACEHOLDER_TOKENS:
        raise RuntimeError("AML_API_TOKEN is still the example value; generate a real one")
    pool.open()
    await asyncio.to_thread(init_schema)
    task = asyncio.create_task(_backfill_vectors_loop())
    try:
        yield
    finally:
        task.cancel()
        pool.close()


app = FastAPI(title="khipu", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
    """剩下真拒的 422：记下是哪个字段、什么原因、哪个 request_id，不记正文，方便事后查。"""
    errs = [{"loc": [str(x) for x in e.get("loc", ())], "type": e.get("type"), "msg": e.get("msg")} for e in exc.errors()]
    rid = uid = None
    try:
        body = json.loads((await request.body()) or b"{}")
        if isinstance(body, dict):
            rid, uid = body.get("request_id"), body.get("user_id")
    except Exception:  # noqa: BLE001
        pass
    log.warning("validation 422 on %s request_id=%s user_id=%s errors=%s", request.url.path, rid, uid,
                json.dumps(errs, ensure_ascii=False)[:2000])
    return JSONResponse(status_code=422, content={"detail": errs})


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"success": False, "error": "internal error"})


# ---------- 接口 ----------

@app.get("/health")
async def health() -> dict[str, Any]:
    await asyncio.to_thread(_health_check)
    return {"ok": True, "service": "khipu"}


@app.post("/add", dependencies=[Depends(require_auth)])
async def add(req: AddRequest) -> dict[str, Any]:
    payload_sha = hashlib.sha256(json.dumps(req.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    saved = await asyncio.to_thread(_saved_response, req.user_id, req.request_id, payload_sha)
    if saved is not None:
        return saved
    kept = [m for m in req.messages if m.content.strip()]
    if len(kept) != len(req.messages):  # 空内容以前回 422；现在跳过这几条，其余照常写（合法请求不会走到这里）
        log.info("add %s: skipped %d empty message(s) of %d", req.request_id, len(req.messages) - len(kept), len(req.messages))
        req = req.model_copy(update={"messages": kept})
    # 先算向量再进事务：向量文本只含说话人和正文，不依赖库里状态，所以能在写入前算好、随段一起落库。
    # 算不出来（限流、断网）就回 503 让平台稍后重试（合同里 503 是可重试的），不留没向量的段——复现时库的状态必须一样。
    vectors: dict[str, list[float]] = {}
    if config.EMBED_API_KEY:
        from .chunking import _split_long, speaker_prefix  # 只为拿到与写入完全一致的切分
        texts: list[str] = []
        for m in req.messages:
            sp = speaker_prefix(m.content)
            texts.extend(_embed_text_of(m.role, sp, piece) for piece in _split_long(m.content, config.SEGMENT_MAX_TOKENS))
        uniq = list(dict.fromkeys(texts))
        vecs = await embed_texts(uniq)
        if any(v is None for v in vecs):
            raise HTTPException(status_code=503, detail="embedding temporarily unavailable, retry later")
        vectors = dict(zip(uniq, vecs))
    response, segments = await asyncio.to_thread(_add_transaction, req, payload_sha, vectors)
    if segments:
        invalidate(req.user_id)
    return response


@app.post("/search", dependencies=[Depends(require_auth)])
async def search(req: SearchRequest) -> dict[str, Any]:
    if not req.query.strip():
        return {"data": []}
    data = await run_search(req.user_id, req.query, req.options, req.top_k or config.HARD_TOP_K)
    return {"data": data}
