"""Add 时抽取（B3，2026-10-06）：一个 Add 请求的消息 → 按 token 切成几窗 → gpt-4o-mini（经 OpenRouter）→ 事实行 + 会话摘要。

为什么：第一枪分项最低的是发现规律、长历史综合、新值覆盖、删除——逐字存原文再捞 100 条片段，答不了「把这段历史捋一遍」
的题；只喂标准证据重答也错一半，错在算术、类别、相对日期、助手建议当事实（docs/diagnostics/2026-10-02-answer-layer）。
抽出来的事实行把相对日期换成绝对日期、把主语写明、把否定和变更写成一句话，和原文一起进召回、重排、装箱。

复现（主办方会用自己的 key 重放 Add/Search，差异过大判无效）：
- 锁快照 gpt-4o-mini-2024-07-18、temperature 0、固定 seed、只走 OpenAI/Azure、require_parameters；
- 整个请求体的哈希当缓存键，结果存 extract_cache：同一段原文只调一次模型，平台重试、并发重复都不再花钱；
- 确定性的坏结果（400、内容审核、解析不了）按空结果收下并缓存——重试只会得到同样的结果，回 503 会让整场 Full 卡死；
- 运维类失败（没 key、401/402/404、重试用尽、超出本进程预算）抛 ExtractUnavailable，Add 回 503 让平台稍后重试，
  不写半截数据：库里要么有这一包的原文和笔记，要么都没有。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass

import httpx

from . import config
from .chunking import speaker_prefix, ts_from_ms
from .db import pool
from .httpclient import extract_sem, llm_client, retryable_status, usage
from .textutil import WEEKDAYS, count_tokens

log = logging.getLogger("aml.extract")

PROMPT_VERSION = "x1"

SYSTEM_PROMPT = """You turn one chunk of a conversation into memory notes for a long-term memory system. Later, someone will ask questions about the people in the conversation; your notes will be searched together with the original messages.

Input: numbered messages, each with its date (when known) and its speaker.

Return a JSON object: {"facts": [...], "summary": "..."}

facts: at most 30 items, each {"text": "...", "subject": "...", "key": "..." or null}.
- text: one self-contained sentence. Name the subject explicitly (no "he", "she", "I", "they"); write "The user" for the user when no name is given. Keep concrete details exactly as said: names, numbers, amounts, prices, places, titles, brands, durations, frequencies.
- Dates: when the message has a date and the event's time is given or implied ("yesterday", "last weekend", "two weeks ago", "next Friday"), write the absolute date or period, e.g. "on 19 May 2023" or "in the week before 20 May 2023". If it cannot be resolved, keep the original words and add "(said on 20 May 2023)".
- Record what people did, have, own, bought, visited, finished, plan, decided, like and dislike, and their relationships, jobs, places, health, routines, preferences and goals.
- Write negations and changes explicitly: "The user no longer owns a car; they sold it in April 2023." "The user moved from Boston to Denver in March 2023."
- Write counts and amounts as stated in each message; do not add up across messages.
- The assistant's suggestions are not facts about the user. Record them only when the user accepts or acts on one: "The user chose X, which the assistant had suggested." Do not record general knowledge the assistant explains.
- One fact per item; do not merge unrelated facts. Skip greetings and small talk.
- subject: the person the fact is about: "user", or the person's name.
- key: for facts that can change over time, a short lowercase topic key "<subject>.<attribute>", e.g. "user.job", "user.city", "caroline.relationship_status", "melanie.pets", "user.car". Reuse the same key for the same attribute. null for one-off events.

summary: one to three sentences on what this chunk covers: who talked about what, and the date.

Write in the language of the conversation. Output only the JSON object."""

NOTE_ROLES = ("note", "summary")
_GENERIC_SUBJECTS = {"", "user", "the user", "assistant", "the assistant", "i", "me", "you", "speaker", "unknown",
                     "none", "null", "n/a"}
_KEY_BAD = re.compile(r"[^a-z0-9_.]+")
_FLAT_OBJ = re.compile(r"\{[^{}]*\}")


@dataclass(frozen=True)
class Note:
    kind: str            # note | summary
    text: str
    subject: str | None  # 具体人名；泛指的 user/assistant 记 None
    key: str | None      # 会变的事实的话题键，如 user.job


class ExtractUnavailable(Exception):
    """调不通、没 key、超出预算：Add 回 503，平台稍后重试。"""


class _Retry(Exception):
    """408 / 429 / 5xx / 200 但没有 choices：换个时间再试。"""


# ---------- 输入：消息 → 窗 ----------

def _clip(text: str, max_tokens: int) -> str:
    if count_tokens(text) <= max_tokens:
        return text
    return text[: max_tokens * 4].rstrip() + " …[truncated]"


def _when(ms: int | float | None) -> str:
    ts, gran = ts_from_ms(ms)
    if ts is None:
        return ""
    s = f"{ts.strftime('%Y-%m-%d')} ({WEEKDAYS[ts.weekday()]})"
    return s + ts.strftime(" %H:%M") if gran == "datetime" else s


def message_line(i: int, m: dict) -> str:
    content = m["content"]
    who = speaker_prefix(content)
    if who:
        content = content[len(who) + 1:].lstrip()
    else:
        who = m["role"]
    when = _when(m.get("timestamp"))
    return f"[{i}] {when + ' ' if when else ''}{who}: {_clip(content, config.EXTRACT_MSG_MAX_TOKENS)}"


def build_windows(messages: list[dict]) -> list[str]:
    """按消息边界切窗，每窗原文不超过 EXTRACT_WINDOW_TOKENS（单条超长的消息自成一窗）。只看请求本身，不看库，
    所以同一个请求永远切出同样的窗、同样的缓存键。"""
    windows: list[str] = []
    cur: list[str] = []
    used = 0
    for i, m in enumerate(messages, start=1):
        if not m["content"].strip():
            continue
        line = message_line(i, m)
        t = count_tokens(line)
        if cur and used + t > config.EXTRACT_WINDOW_TOKENS:
            windows.append("\n".join(cur))
            cur, used = [], 0
        cur.append(line)
        used += t
    if cur:
        windows.append("\n".join(cur))
    return windows


def request_body(window: str) -> dict:
    return {
        "model": config.EXTRACT_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": window}],
        "temperature": 0,
        "seed": config.EXTRACT_SEED,
        "max_tokens": config.EXTRACT_MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_object"},
        "provider": {"only": config.EXTRACT_PROVIDERS, "allow_fallbacks": False, "require_parameters": True},
    }


def body_sha(body: dict) -> str:
    """缓存键：整个请求体（模型、提示词、seed、供应商、原文）。改了提示词不用手动换版本号，键自己会变。"""
    return hashlib.sha256((PROMPT_VERSION + "\n" + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()


# ---------- 输出：模型回复 → 笔记 ----------

def _subject(v: object) -> str | None:
    if not isinstance(v, str):
        return None
    s = " ".join(v.split())
    return None if s.lower() in _GENERIC_SUBJECTS or len(s) > 40 else s


def _key(v: object) -> str | None:
    if not isinstance(v, str):
        return None
    k = _KEY_BAD.sub("", v.strip().lower().replace(" ", "_")).strip("._")
    return k[:60] or None


def _load_json(content: str) -> dict | None:
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    # 模型偶尔包一层 ```json；被 max_tokens 截断时整段解析不了，但前面完整的事实对象还在（事实对象是扁平的，没有嵌套花括号）
    body = re.sub(r"^\s*```(?:json)?|```\s*$", "", content.strip())
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    facts = []
    for m in _FLAT_OBJ.finditer(body):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(obj, dict) and "text" in obj:
            facts.append(obj)
    return {"facts": facts} if facts else None


def parse_output(content: str) -> tuple[list[Note], bool]:
    """返回 (笔记, 是否认得这个回复)。宽进：字符串事实、缺字段、超长、重复都收拾掉，不因一条坏事实丢整窗。"""
    data = _load_json(content or "")
    if data is None:
        return [], False
    out: list[Note] = []
    seen: set[str] = set()
    facts = data.get("facts")
    for f in facts if isinstance(facts, list) else []:
        if isinstance(f, str):
            text, subj, key = f, None, None
        elif isinstance(f, dict):
            text, subj, key = f.get("text"), f.get("subject"), f.get("key")
        else:
            continue
        if not isinstance(text, str):
            continue
        text = " ".join(text.split())[:400]
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(Note("note", text, _subject(subj), _key(key)))
        if len(out) >= config.EXTRACT_MAX_FACTS:
            break
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        out.append(Note("summary", " ".join(summary.split())[:1000], None, None))
    return out, True


# ---------- 缓存（库） ----------

def _cache_get(sha: str) -> dict | None:
    with pool.connection() as conn:
        row = conn.execute("SELECT output FROM extract_cache WHERE input_sha = %s", (sha,)).fetchone()
    return row[0] if row else None


def _cache_put(sha: str, output: dict, status: str, prompt_tokens: int | None, completion_tokens: int | None) -> None:
    with pool.connection() as conn:
        conn.execute("INSERT INTO extract_cache (input_sha, model, prompt_version, output, status, prompt_tokens, "
                     "completion_tokens) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (input_sha) DO NOTHING",
                     (sha, config.EXTRACT_MODEL, PROMPT_VERSION, json.dumps(output, ensure_ascii=False), status,
                      prompt_tokens, completion_tokens))
        conn.commit()


def _notes_from(output: dict) -> list[Note]:
    return [Note(**it) for it in output.get("items", [])]


# ---------- 调用 ----------

async def _call(body: dict) -> tuple[str | None, str, int | None, int | None]:
    """返回 (回复正文, 状态, prompt_tokens, completion_tokens)。状态 ok，或 empty:<原因>（确定性的坏结果，正文为 None）。"""
    delay = 1.0
    for attempt in range(config.EXTRACT_ATTEMPTS):
        if config.EXTRACT_TOKEN_CAP and usage.extract_tokens() >= config.EXTRACT_TOKEN_CAP:
            raise ExtractUnavailable(f"token cap {config.EXTRACT_TOKEN_CAP} reached")
        t0 = time.monotonic()
        ms = 0
        try:
            async with extract_sem:
                r = await llm_client().post(f"{config.EXTRACT_BASE_URL}/chat/completions", json=body)
            ms = int((time.monotonic() - t0) * 1000)
            if r.status_code == 408 or retryable_status(r.status_code):
                raise _Retry(f"status {r.status_code}")
            if r.status_code in (401, 402, 404):  # key 错、没钱、没有满足供应商限制的端点：重试不会好，人来处理
                usage.extract(False, None, None, ms)
                log.error("extract %s: %s", r.status_code, r.text[:300])
                raise ExtractUnavailable(f"status {r.status_code}")
            if r.status_code >= 400:  # 400 / 403 内容审核 / 413 太长：输入本身的问题，按空结果收下
                usage.extract(False, None, None, ms)
                log.warning("extract %s, keeping empty: %s", r.status_code, r.text[:300])
                return None, f"empty:http{r.status_code}", None, None
            payload = r.json()
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not choices:  # OpenRouter 偶尔 200 里装着 error
                raise _Retry(f"no choices: {str(payload)[:200]}")
            content = choices[0]["message"].get("content") or ""
            u = payload.get("usage") or {}
            pt, ct = u.get("prompt_tokens"), u.get("completion_tokens")
            usage.extract(True, pt, ct, ms)
            log.info("extract ok in=%s out=%s %dms attempt=%d finish=%s", pt, ct, ms, attempt + 1,
                     choices[0].get("finish_reason"))
            return content, "ok", pt, ct
        except (httpx.HTTPError, _Retry, ValueError, KeyError, TypeError, IndexError, AttributeError) as e:
            ms = ms or int((time.monotonic() - t0) * 1000)
            usage.extract(False, None, None, ms)
            log.warning("extract attempt %d failed after %dms: %s %s", attempt + 1, ms, type(e).__name__, e)
            if attempt == config.EXTRACT_ATTEMPTS - 1:
                raise ExtractUnavailable(f"{type(e).__name__}: {e}") from e
            await asyncio.sleep(delay)
            delay *= 2
    raise ExtractUnavailable("no attempts configured")


async def _extract_uncached(sha: str, body: dict) -> list[Note]:
    content, status, pt, ct = await _call(body)
    notes: list[Note] = []
    if content is not None:
        notes, ok = parse_output(content)
        if not ok:
            status = "empty:parse"
    if status != "ok":
        usage.extract_gave_empty()
        log.warning("extract window %s kept empty (%s)", sha[:12], status)
    await asyncio.to_thread(_cache_put, sha, {"items": [asdict(n) for n in notes], "raw": content}, status, pt, ct)
    return notes


_inflight: dict[str, asyncio.Future] = {}


def _mark_retrieved(f: asyncio.Future) -> None:
    if not f.cancelled():
        f.exception()  # 没人等的时候别报「exception was never retrieved」


async def extract_window(window: str) -> list[Note]:
    body = request_body(window)
    sha = body_sha(body)
    fut = _inflight.get(sha)
    if fut is not None:  # 同一窗已经有人在办（平台并发重试同一个请求、或两个请求带着相同的原文）：等它的结果
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            if fut.cancelled():  # 领头的请求被取消了，自己来
                return await extract_window(window)
            raise
    # 先登记再查缓存：登记和上面的检查之间没有 await，进程内不会有两个人同时去调模型；
    # 领头的写完缓存才注销，注销之后来的人一定能在缓存里查到
    fut = asyncio.get_running_loop().create_future()
    fut.add_done_callback(_mark_retrieved)
    _inflight[sha] = fut
    try:
        cached = await asyncio.to_thread(_cache_get, sha)
        if cached is not None:
            usage.extract_cached()
            notes = _notes_from(cached)
        else:
            notes = await _extract_uncached(sha, body)
        fut.set_result(notes)
        return notes
    except asyncio.CancelledError:
        fut.cancel()
        raise
    except Exception as e:
        fut.set_exception(e)
        raise
    finally:
        _inflight.pop(sha, None)


async def extract_request(messages: list[dict]) -> list[Note]:
    """一个 Add 请求的全部笔记，按窗的顺序；跨窗重复的事实只留第一次。"""
    if not config.EXTRACT_API_KEY:
        raise ExtractUnavailable("OPENROUTER_API_KEY is empty")
    windows = build_windows(messages)
    if not windows:
        return []
    results = await asyncio.gather(*(extract_window(w) for w in windows))
    out: list[Note] = []
    seen: set[tuple[str, str]] = set()
    for notes in results:
        for n in notes:
            k = (n.kind, n.text.lower())
            if k not in seen:
                seen.add(k)
                out.append(n)
    return out
