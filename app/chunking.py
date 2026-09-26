"""Add 侧：一条消息一段，长消息在句边界切子段，时间戳按「自带 → 同包裹继承 → 同会话继承 → 不详」兜底。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config
from .textutil import count_tokens, extract_names, looks_like_rule, split_sentences

_NAME_PREFIX = re.compile(r"^([A-Z][a-zA-Z]{1,20}):\s")


@dataclass
class Segment:
    id: str
    user_id: str
    session_id: str
    request_id: str
    seq: int
    part: int
    total: int
    role: str
    speaker_name: str | None
    ts_value: datetime | None
    ts_granularity: str  # datetime | date | unknown
    ts_provenance: str   # given | inherited | unknown
    text: str
    content_sha: str
    is_rule: bool
    names: list[str] = field(default_factory=list)


def _ts_from_ms(ms: int | float | None) -> tuple[datetime | None, str]:
    if ms is None:
        return None, "unknown"
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    gran = "date" if (dt.hour == 0 and dt.minute == 0 and dt.second == 0) else "datetime"
    return dt, gran


def _split_long(text: str, max_tokens: int) -> list[str]:
    if count_tokens(text) <= max_tokens:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for sent in split_sentences(text):
        t = count_tokens(sent)
        if buf and buf_tokens + t > max_tokens:
            parts.append(" ".join(buf))
            buf, buf_tokens = [], 0
        buf.append(sent)
        buf_tokens += t
    if buf:
        parts.append(" ".join(buf))
    return parts


def build_segments(user_id: str, session_id: str, request_id: str, messages: list[dict],
                   start_seq: int, session_last_ts: tuple[datetime | None, str]) -> list[Segment]:
    # 第一遍：解析自带时间
    raw: list[tuple[datetime | None, str]] = [_ts_from_ms(m.get("timestamp")) for m in messages]
    # 第二遍：同包裹内前向、后向继承；再不行继承同会话上一块的末尾时间；再不行 unknown
    resolved: list[tuple[datetime | None, str, str]] = []
    last: tuple[datetime | None, str] = session_last_ts
    for ts, gran in raw:
        if ts is not None:
            last = (ts, gran)
            resolved.append((ts, gran, "given"))
        else:
            resolved.append((None, "unknown", "unknown"))
    # 后向：第一条没时间但后面有，向后借
    nxt: tuple[datetime | None, str] | None = None
    for i in range(len(resolved) - 1, -1, -1):
        ts, gran, prov = resolved[i]
        if ts is not None:
            nxt = (ts, gran)
        elif nxt is not None:
            resolved[i] = (nxt[0], nxt[1], "inherited")
    # 前向：前面有时间的往后传；整包都没有就用会话末尾时间
    prev: tuple[datetime | None, str] = session_last_ts
    for i, (ts, gran, prov) in enumerate(resolved):
        if ts is not None:
            prev = (ts, gran)
        elif prev[0] is not None:
            resolved[i] = (prev[0], prev[1], "inherited")

    segments: list[Segment] = []
    seq = start_seq
    for m, (ts, gran, prov) in zip(messages, resolved):
        role = m["role"]
        content = m["content"]
        speaker = None
        pm = _NAME_PREFIX.match(content)
        if pm:
            speaker = pm.group(1)
        pieces = _split_long(content, config.SEGMENT_MAX_TOKENS)
        total = len(pieces)
        for part, piece in enumerate(pieces, start=1):
            sha = hashlib.sha256(f"{role}\n{piece}".encode("utf-8")).hexdigest()[:24]
            segments.append(Segment(
                id=f"{session_id}#{seq}.{part}",
                user_id=user_id, session_id=session_id, request_id=request_id,
                seq=seq, part=part, total=total, role=role, speaker_name=speaker,
                ts_value=ts, ts_granularity=gran, ts_provenance=prov,
                text=piece, content_sha=sha,
                is_rule=looks_like_rule(piece, role),
                names=extract_names(piece),
            ))
        seq += 1
    return segments
