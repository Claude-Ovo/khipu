"""Search 时的时间链（10-09）：聚合、计数、时序类的题，把重排后的前 CHAIN_TOPN 条候选交给 gpt-4o-mini，
按时间排成一条事实链（同一件事的几种说法并成一行，带绝对日期，写明状态），作为第一条返回项，原文照常跟在后面。

为什么：第一枪分项最低的是发现规律、长历史综合；10-08 的对照说明证据齐全时答题模型照样数错（四个电影节数成三个）——
四次提及散在四个会话、措辞各异，读到第三个就以为数完了。规则版的时间线（LEDGER）没用：合并不了换说法的重复，
分不清「在卖的鼓」和「考虑买的尤克里里」。这一步让模型做的正是规则做不到的那两件事。

边界（官方答疑 Q18）：可以合并整理后返回，不许加外部信息，不许拿到问题直接生成答案伪装成记忆。所以提示词禁止计数、
合计、比较、下结论，链上只有「几月几日，发生了什么」；来源编号对不上的行丢掉（模型编的不要）；用了生成模型要在
参赛备注里申报版本和提示词。

复现与失败：请求体哈希当缓存键（和抽取共用 extract_cache 表，前缀不同）；调不到模型就不给这一条，Search 照常返回
原文（不回 503：Search 失败平台不一定重试，少一条链比少一次检索好），/health 的 chain 块能看到跳过了多少次。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass

from . import config
from .extract import ExtractUnavailable, _cache_get, _cache_put, _call
from .httpclient import usage
from .textutil import count_tokens

log = logging.getLogger("aml.chain")

PROMPT_VERSION = "c2"   # c1 (10-09 first run): no scan step; on LME 77 the model dropped a festival mention sitting at #43 of 50

SYSTEM_PROMPT = """You organize retrieved memory excerpts for a question-answering system. You are given a question and numbered excerpts from one user's past conversations, each with the date it was said and the speaker. Someone else will answer the question later, seeing both your output and the original excerpts.

Return a JSON object: {"relevant": [...], "events": [...]}

relevant: first go through ALL the excerpts in order, including the last ones, and list the number of every excerpt that bears on the question. An excerpt bears on the question when it mentions a thing of the kind the question asks about, even in passing or as part of another topic. When in doubt, include it.

events: the relevant excerpts arranged as dated entries, oldest first, at most 40. Each entry: {"date": "...", "when": "event" or "said", "text": "...", "sources": [n, ...]}.
- Every number in "relevant" must appear in the sources of some entry. Include every distinct event, purchase, trip, activity, plan, change, decision or statement that bears on the question; leave out excerpts that do not bear on it.
- When several excerpts refer to the same event or fact (a repeated or reworded mention, a plan and its later completion, an update to the same thing), merge them into ONE entry and list all their source numbers. Keep different events of the same kind as separate entries.
- date: when the event happened, as "YYYY-MM-DD" (or "YYYY-MM" / "YYYY" when only that much is known), resolved from the excerpt's date and words like "yesterday", "last weekend", "two weeks ago", "next Friday"; then "when" is "event". If the excerpt does not say when the event happened, use the excerpt's own date and set "when" to "said". Use "unknown" only when there is no date at all.
- text: one sentence stating what happened or what was said, keeping names, numbers, amounts, prices, places and titles exactly as in the excerpts. State the status as said: planned, done, cancelled, sold, returned, considering, no longer true. Name the subject (no "he", "she", "I"); write "The user" for the user.
- The assistant's suggestions and recommendations are not events; include them only when the user said they did or chose them.
- Do not count, total, compare, conclude, or answer the question. Do not add anything that is not in the excerpts. Do not guess.

Write in the language of the excerpts. Output only the JSON object."""

_DATE_OK = re.compile(r"^(\d{4}(-\d{2}){0,2}|unknown)$")


@dataclass
class Event:
    date: str
    when: str
    text: str
    sources: list[int]


def _clip(text: str, max_tokens: int) -> str:
    if count_tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + " …"


def build_input(query: str, lines: list[str]) -> tuple[str, int]:
    """问题 + 编号候选；每条截到 CHAIN_LINE_TOKENS，总量到 CHAIN_INPUT_TOKENS 为止。返回 (正文, 实际放进去的条数)。"""
    head = f"Question: {' '.join(query.split())}\n\nExcerpts:\n"
    used = count_tokens(head)
    out = []
    for i, ln in enumerate(lines, 1):
        s = f"#{i} {_clip(' '.join(ln.split()), config.CHAIN_LINE_TOKENS)}\n"
        t = count_tokens(s)
        if used + t > config.CHAIN_INPUT_TOKENS:
            break
        out.append(s)
        used += t
    return head + "".join(out), len(out)


def request_body(text: str) -> dict:
    body = {
        "model": config.EXTRACT_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}],
        "temperature": 0,
        "seed": config.EXTRACT_SEED,
        "max_tokens": config.CHAIN_MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_object"},
    }
    if config.EXTRACT_PROVIDERS:
        body["provider"] = {"only": config.EXTRACT_PROVIDERS, "allow_fallbacks": False, "require_parameters": True}
    return body


def body_sha(body: dict) -> str:
    return hashlib.sha256((PROMPT_VERSION + "\n" + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()


def _load(content: str) -> dict | None:
    for s in (content, re.sub(r"^\s*```(?:json)?|```\s*$", "", (content or "").strip())):
        try:
            data = json.loads(s)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def parse_output(content: str, n_sources: int) -> tuple[list[Event], bool]:
    """返回 (事件, 是否认得这个回复)。来源编号一个都对不上的行丢掉：那是模型编的，不是记忆里的。
    按日期从早到晚稳定排序（模型给的顺序只做并列时的次序），unknown 排最后。"""
    data = _load(content or "")
    if data is None:
        return [], False
    raw = data.get("events")
    out: list[Event] = []
    seen: set[str] = set()
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        text = e.get("text")
        if not isinstance(text, str):
            continue
        text = " ".join(text.split())[:400]
        if not text or text.lower() in seen:
            continue
        src = e.get("sources")
        srcs = sorted({int(s) for s in (src if isinstance(src, list) else []) if isinstance(s, (int, float)) and not isinstance(s, bool)
                       and 1 <= int(s) <= n_sources})
        if not srcs:
            continue
        date = e.get("date")
        date = " ".join(date.split()) if isinstance(date, str) else "unknown"
        if not _DATE_OK.match(date):
            date = "unknown"
        when = "said" if e.get("when") == "said" else "event"
        seen.add(text.lower())
        out.append(Event(date, when, text, srcs))
    out.sort(key=lambda ev: (ev.date == "unknown", ev.date))   # 稳定排序：同日保持模型给的顺序
    return out[: config.CHAIN_MAX_EVENTS], True


def render(events: list[Event]) -> str:
    head = (f"[timeline · {len(events)} dated entries found in the retrieved memories, oldest first; repeated mentions of the "
            f"same event are merged into one line; it lists what the memories say and does not answer the question; "
            f"the memories below are the source and may contain more]")
    lines = []
    for ev in events:
        d = f"said on {ev.date}" if ev.when == "said" and ev.date != "unknown" else ev.date
        lines.append(f"- {d}: {ev.text}")
    return head + "\n" + "\n".join(lines)


async def build_chain(query: str, lines: list[str]) -> str | None:
    """成功给渲染好的时间链；模型调不到、超时、解析不了、一条都没有 → None（调用方照常返回原文）。"""
    if not config.EXTRACT_API_KEY or not lines:
        usage.chain_skipped()
        return None
    text, n = build_input(query, lines)
    if n == 0:
        usage.chain_skipped()
        return None
    body = request_body(text)
    sha = body_sha(body)
    try:
        cached = await asyncio.to_thread(_cache_get, sha)
        if cached is not None:
            usage.chain_cached()
            events = [Event(**it) for it in cached.get("items", [])]
        else:
            content, status, pt, ct, model = await asyncio.wait_for(_call(body, kind="chain", attempts=config.CHAIN_ATTEMPTS),
                                                                   timeout=config.CHAIN_TIMEOUT_S)
            events: list[Event] = []
            if content is not None:
                events, ok = parse_output(content, n)
                if not ok:
                    status = "empty:parse"
            if status != "ok":
                log.warning("chain %s kept empty (%s)", sha[:12], status)
            await asyncio.to_thread(_cache_put, sha, {"items": [ev.__dict__ for ev in events], "raw": content, "model": model},
                                    status, pt, ct)
    except (ExtractUnavailable, asyncio.TimeoutError) as e:
        usage.chain_skipped()
        log.warning("chain skipped: %s %s", type(e).__name__, e)
        return None
    except Exception as e:  # noqa: BLE001  链是锦上添花，任何意外都不许拖垮检索
        usage.chain_skipped()
        log.exception("chain failed: %s %s", type(e).__name__, e)
        return None
    if not events:
        return None
    return render(events)
