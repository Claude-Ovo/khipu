"""Add 侧：一条消息一段，长消息在句边界切子段（子段是原文的精确切片，不重拼），
时间戳按「自带 → 同包裹继承 → 同会话继承 → 不详」兜底，绝不用服务器时间。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config
from .textutil import count_tokens, extract_names, looks_like_rule, sentence_spans

_NAME_PREFIX = re.compile(r"^([A-Z][a-zA-Z]{1,20}):\s")
# 这些是正文里常见的标签，不是说话人（审查 #3 P1-12）
_NOT_A_SPEAKER = {"Note", "Notes", "Tip", "Tips", "Warning", "Update", "Summary", "Answer", "Question", "Step",
                  "Example", "Task", "Context", "Reminder", "Hint", "Hints", "Ps", "Edit", "Result", "Output",
                  "Input", "Error", "Info", "Important", "Remember", "Topic", "Subject", "Re", "Fyi", "Idea",
                  "Plan", "Goal", "Status", "Title", "Description", "Prompt", "Response", "System", "User", "Assistant"}


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
    kind: str = "msg"            # msg 原文 / note 抽出的事实行 / summary 会话摘要（B3）
    note_key: str | None = None  # 事实的话题键，只有 note 有


def ts_from_ms(ms: int | float | None) -> tuple[datetime | None, str]:
    if ms is None:
        return None, "unknown"
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    midnight = dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0
    return dt, ("date" if midnight else "datetime")


def _split_long(text: str, max_tokens: int) -> list[str]:
    """按句边界切，每段是原文的精确子串（保留切点处的换行和空白）。"""
    if count_tokens(text) <= max_tokens:
        return [text]
    parts: list[str] = []
    start = 0
    acc_tokens = 0
    last_cut = 0
    for a, b in sentence_spans(text):
        t = count_tokens(text[a:b])
        if last_cut > start and acc_tokens + t > max_tokens:
            parts.append(text[start:last_cut])
            start = last_cut
            acc_tokens = 0
        acc_tokens += t
        last_cut = b
    if start < len(text):
        parts.append(text[start:])
    return [p for p in parts if p.strip()] or [text]


def speaker_prefix(content: str) -> str | None:
    m = _NAME_PREFIX.match(content)
    if not m or m.group(1) in _NOT_A_SPEAKER:
        return None
    return m.group(1)


def resolve_timestamps(messages: list[dict], session_last_ts: tuple[datetime | None, str]) -> list[tuple[datetime | None, str, str]]:
    raw = [ts_from_ms(m.get("timestamp")) for m in messages]
    resolved: list[tuple[datetime | None, str, str]] = [
        (ts, gran, "given") if ts is not None else (None, "unknown", "unknown") for ts, gran in raw]
    # 后向：前面没时间、后面有，向后借
    nxt: tuple[datetime | None, str] | None = None
    for i in range(len(resolved) - 1, -1, -1):
        ts, gran, _ = resolved[i]
        if ts is not None:
            nxt = (ts, gran)
        elif nxt is not None:
            resolved[i] = (nxt[0], nxt[1], "inherited")
    # 前向：前面有时间的往后传；整包都没有就用同会话上一块的末尾时间
    prev: tuple[datetime | None, str] = session_last_ts
    for i, (ts, gran, _) in enumerate(resolved):
        if ts is not None:
            prev = (ts, gran)
        elif prev[0] is not None:
            resolved[i] = (prev[0], prev[1], "inherited")
    return resolved


def build_segments(user_id: str, session_id: str, request_id: str, messages: list[dict],
                   start_seq: int, session_last_ts: tuple[datetime | None, str]) -> list[Segment]:
    resolved = resolve_timestamps(messages, session_last_ts)
    segments: list[Segment] = []
    seq = start_seq
    for m, (ts, gran, prov) in zip(messages, resolved):
        role = m["role"]
        content = m["content"]
        speaker = speaker_prefix(content)
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


NOTE_PART_BASE = 1000  # 笔记的 part 从 1001 起：同 seq 排在这一包最后一条消息的所有子段之后、下一包之前


def build_note_segments(msg_segments: list[Segment], notes: list) -> list[Segment]:
    """B3：一个 Add 请求抽出的笔记（extract.Note）挂在这一包最后一条消息后面，时间用那条消息的时间（即「哪天说的」；
    事件本身的日期模型已经写进正文）。id 是 session#seq.nN，和原文的 session#seq.part 不会撞，回放脚本认金标证据的正则也碰不到它。"""
    if not msg_segments or not notes:
        return []
    last = msg_segments[-1]
    out: list[Segment] = []
    for n, note in enumerate(notes, start=1):
        sha = hashlib.sha256(f"{note.kind}\n{note.text}".encode("utf-8")).hexdigest()[:24]
        out.append(Segment(
            id=f"{last.session_id}#{last.seq}.n{n}",
            user_id=last.user_id, session_id=last.session_id, request_id=last.request_id,
            seq=last.seq, part=NOTE_PART_BASE + n, total=1, role=note.kind, speaker_name=note.subject,
            ts_value=last.ts_value, ts_granularity=last.ts_granularity, ts_provenance="derived",
            text=note.text, content_sha=sha, is_rule=False, names=extract_names(note.text),
            kind=note.kind, note_key=note.key,
        ))
    return out
