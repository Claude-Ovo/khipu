"""Search 侧：意图路由 → 五路召回 → RRF → 后处理（双命中前置、规矩口袋、邻居扩展）→ 装箱。
门只看相关性；顺序即产品；整条不截断；不造假记忆。"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timedelta

import numpy as np

from . import config
from .db import pool
from .embed import embed_query
from .rerank import rerank
from .index import Row, UserIndex, get_index
from .textutil import count_tokens, created_at_value, date_header, extract_dates, literal_terms, tokenize

log = logging.getLogger("aml.search")

_INTENT = [
    ("latest", re.compile(r"\b(currently|now|nowadays|these days|at the moment|still|latest|current|present)\b|现在|目前|最近|如今", re.I)),
    ("temporal", re.compile(r"\b(when|how long|how many (days|weeks|months|years)|before|after|first|last|since|until|ago|date|\d{4})\b|什么时候|多久|之前|之后|哪天|哪年", re.I)),
    ("aggregate", _AGG_RX := re.compile(r"\b(how many|how much|all|list|which|every|each|what are|name the|total)\b|有哪些|多少|所有|列出", re.I)),
    ("who", re.compile(r"\b(who|whose|whom)\b|谁", re.I)),
]

# 各路在 RRF 里的分量；只有这五路能开门
_WEIGHTS = {
    "default":   {"bm25": 1.0, "vector": 1.0, "entity": 0.8, "literal": 1.0, "date": 0.5},
    "temporal":  {"bm25": 1.2, "vector": 0.9, "entity": 0.7, "literal": 1.0, "date": 1.5},
    "latest":    {"bm25": 1.0, "vector": 1.0, "entity": 1.0, "literal": 0.8, "date": 0.3},
    "aggregate": {"bm25": 1.0, "vector": 1.0, "entity": 1.4, "literal": 1.0, "date": 0.5},
    "who":       {"bm25": 1.0, "vector": 1.0, "entity": 1.4, "literal": 1.2, "date": 0.3},
}
_VIRTUAL_RANK = 4  # 实体/字面/日期通道的起始虚拟排名（MemoryConstellations 的做法）
for _w in _WEIGHTS.values():   # 第二跳两路的分量，只有 HOP_ENABLED 时才会出现在通道表里
    _w.setdefault("bm25_hop", config.HOP_W)
    _w.setdefault("vector_hop", config.HOP_W)

# 第二跳挑扩展词时要跳过的词：tokenize 不做停用词，这里只拦最常见的功能词和对话套话
_HOP_STOP = set("""the and for that this with have has had was were are you your yours our ours they them their there here
what when where which who whom how why not but can could would should will just also very really about from into than then
them some any all more most much many few lot lots one two three like get got going went make made take took come came
think know want need let see look feel felt say said tell told ask asked yes yeah okay sure thanks thank please sorry
been being does did doing done its it's i'm i've i'd i'll you're we're they're don't didn't doesn't can't won't isn't
because while though although since after before again still even ever never always often sometimes today yesterday
tomorrow week weeks month months year years day days time times thing things something anything nothing everything
user assistant""".split())


_TASK = re.compile(r"^\s*(please\s+)?(write|draft|compose|help me|make|create|plan|recommend|suggest|give me|tell me how|"
                   r"can you|could you|would you|i need you to|i want you to|let's|any (?:good |other )?(?:suggestions|recommendations|ideas))\b"
                   r"|帮我|请你|给我写|替我|帮忙|推荐", re.I)
_FACTUAL = re.compile(r"^\s*(what|when|who|whom|whose|where|which|how (?:many|much|long|old|often)|did|do|does|is|was|were|are|has|have|had)\b", re.I)


def detect_intent(query: str) -> str:
    for name, rx in _INTENT:
        if rx.search(query):
            return name
    return "default"


def is_task_request(query: str) -> bool:
    """「要你做事」的题才打开规矩口袋（write / can you recommend / 帮我…）。
    问事实的题（what/when/who/did… 开头）不开：LoCoMo 消融里常开会挤掉真命中，-3 个点。"""
    return bool(_TASK.search(query)) and not _FACTUAL.match(query)


# ---------- 五路 ----------

def _bm25_channel(idx: UserIndex, q: str, n: int) -> tuple[list[int], dict[int, float]]:
    """候选 = 至少含一个查询词的段（rank_bm25 在一两条记忆的用户上 idf 为负，不能拿 score>0 当命中），再按分排序。"""
    if idx.bm25 is None:
        return [], {}
    q_tokens = set(tokenize(q))
    if not q_tokens:
        return [], {}
    scores = idx.bm25.get_scores(list(q_tokens))
    cand = [i for i, ts in enumerate(idx.token_sets) if ts & q_tokens]
    cand.sort(key=lambda i: -scores[i])
    hits = cand[:n]
    return hits, {i: float(scores[i]) for i in hits}


def _vector_sql(user_id: str, vec: list[float], n: int) -> list[str]:
    sql = ("SELECT id FROM segments WHERE user_id = %s AND embedding IS NOT NULL "
           "ORDER BY embedding <=> %s::vector LIMIT %s")
    with pool.connection() as conn:
        return [r[0] for r in conn.execute(sql, (user_id, np.array(vec, dtype=np.float32), n))]


async def _vector_channel(idx: UserIndex, q: str, n: int) -> tuple[list[int], list[float] | None]:
    """返回 (命中, 查询向量)；查询向量留给第二跳做质心用。"""
    if not any(r.has_vector for r in idx.rows):
        return [], None
    vec = await embed_query(q)
    if vec is None:
        return [], None
    ids = await asyncio.to_thread(_vector_sql, idx.user_id, vec, n)
    return [idx.id_to_pos[i] for i in ids if i in idx.id_to_pos], vec


def _entity_channel(idx: UserIndex, q: str) -> list[int]:
    ql = q.lower()
    hit_groups: set[str] = set()
    for alias, canon in idx.alias_to_group.items():
        if len(alias) > 2 and re.search(rf"\b{re.escape(alias)}\b", ql):
            hit_groups.add(canon)
    rows: set[int] = set()
    for g in hit_groups:
        rows |= idx.entity_groups.get(g, set())
    # 同一个人的段按时间倒序，没时间的排最后
    return sorted(rows, key=lambda p: (idx.rows[p].ts_value is None,
                                       -(idx.rows[p].ts_value.timestamp() if idx.rows[p].ts_value else 0)))


def _literal_channel(idx: UserIndex, q: str, bm25_scores: dict[int, float]) -> list[int]:
    terms = [t.lower() for t in literal_terms(q)]
    if not terms:
        return []
    hits = [r.pos for r in idx.rows if any(t in r.text.lower() for t in terms)]
    return sorted(hits, key=lambda p: -bm25_scores.get(p, 0.0))


def _date_channel(idx: UserIndex, q: str, intent: str) -> list[int]:
    if intent != "temporal":
        return []
    rows: set[int] = set()
    for gran, d in extract_dates(q):  # extract_dates 已过滤非法日期
        if gran == "month":
            rows |= idx.by_month.get(d, set())
        else:
            base = datetime.strptime(d, "%Y-%m-%d")
            for delta in range(-3, 4):
                try:  # 0001-01-01 / 9999-12-31 附近加减会越界，跳过那几天而不是 500
                    d2 = base + timedelta(days=delta)
                except OverflowError:
                    continue
                rows |= idx.by_date.get(d2.strftime("%Y-%m-%d"), set())
    return sorted(rows, key=lambda p: idx.rows[p].seq)


# ---------- 第二跳（伪相关反馈） ----------

def _hop_terms(idx: UserIndex, anchors: list[int], q_tokens: set[str]) -> list[str]:
    """从锚段里挑扩展词：不在查询里、不是功能词，按「几个锚都提到 × 语料里稀有」排。"""
    df: Counter[str] = Counter()
    for p in anchors:
        for t in idx.token_sets[p]:
            if t not in q_tokens and t not in _HOP_STOP and len(t) > 2 and not t.isdigit():
                df[t] += 1
    idf = idx.bm25.idf if idx.bm25 is not None else {}
    ranked = sorted(df, key=lambda t: -(df[t] * max(float(idf.get(t, 0.0)), 0.0)))
    return ranked[: config.HOP_TERMS]


def _bm25_hop(idx: UserIndex, terms: list[str], n: int) -> list[int]:
    if idx.bm25 is None or not terms:
        return []
    tset = set(terms)
    scores = idx.bm25.get_scores(terms)
    cand = [i for i, ts in enumerate(idx.token_sets) if ts & tset]
    cand.sort(key=lambda i: -scores[i])
    return cand[:n]


def _anchor_vectors_sql(user_id: str, ids: list[str]) -> list[np.ndarray]:
    with pool.connection() as conn:
        rows = conn.execute("SELECT embedding FROM segments WHERE user_id = %s AND id = ANY(%s) AND embedding IS NOT NULL",
                            (user_id, ids)).fetchall()
    # pgvector 的 psycopg 适配器返回的是 Vector 对象，不是 ndarray
    return [np.asarray(r[0].to_numpy() if hasattr(r[0], "to_numpy") else r[0], dtype=np.float32) for r in rows]


async def _vector_hop(idx: UserIndex, qvec: list[float], anchors: list[int], n: int) -> list[int]:
    """Rocchio：查询向量与锚段向量的质心加权后再搜一轮。"""
    ids = [idx.rows[p].id for p in anchors if idx.rows[p].has_vector]
    if not ids:
        return []
    vecs = await asyncio.to_thread(_anchor_vectors_sql, idx.user_id, ids)
    if not vecs:
        return []
    q = np.asarray(qvec, dtype=np.float32)
    q = q / (np.linalg.norm(q) or 1.0)
    c = np.mean([v / (np.linalg.norm(v) or 1.0) for v in vecs], axis=0)
    mixed = config.HOP_QUERY_W * q + (1 - config.HOP_QUERY_W) * c
    mixed = mixed / (np.linalg.norm(mixed) or 1.0)
    hits = await asyncio.to_thread(_vector_sql, idx.user_id, mixed.tolist(), n)
    return [idx.id_to_pos[i] for i in hits if i in idx.id_to_pos]


async def _second_hop(idx: UserIndex, q: str, order: list[int], qvec: list[float] | None, n: int) -> dict[str, list[int]]:
    """拿第一轮的前几条当锚，再检一轮。只开门不排序：新进来的段和别人一起过 RRF 和重排。"""
    anchors = order[: config.HOP_ANCHORS]
    if not anchors:
        return {}
    out: dict[str, list[int]] = {}
    terms = _hop_terms(idx, anchors, set(tokenize(q)))
    if terms:
        out["bm25_hop"] = _bm25_hop(idx, terms, n)
    if qvec is not None:
        try:
            out["vector_hop"] = await asyncio.wait_for(_vector_hop(idx, qvec, anchors, n), timeout=config.SEARCH_TIMEOUT_S)
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            log.warning("vector hop skipped: %s", e)
    return out


# ---------- 融合与后处理 ----------

def _rrf(channels: dict[str, list[int]], intent: str) -> tuple[list[int], dict[int, float], dict[int, set[str]]]:
    w = _WEIGHTS[intent]
    score: dict[int, float] = {}
    hit_by: dict[int, set[str]] = {}
    for name, hits in channels.items():
        start = 0 if name in ("bm25", "vector") else _VIRTUAL_RANK
        for i, pos in enumerate(hits):
            score[pos] = score.get(pos, 0.0) + w[name] / (config.RRF_K + start + i + 1)
            hit_by.setdefault(pos, set()).add(name)
    order = sorted(score, key=lambda p: -score[p])
    return order, score, hit_by


def _both_first(order: list[int], hit_by: dict[int, set[str]]) -> list[int]:
    both = [p for p in order if {"bm25", "vector"} <= hit_by.get(p, set())]
    rest = [p for p in order if p not in set(both)]
    return both + rest


def _with_neighbors(idx: UserIndex, order: list[int], k: int) -> list[int]:
    """前 N 个锚点的同会话前后各一段，放在前 20 条命中之后，总数封顶 k 的一小部分。
    紧贴锚点插入会把真命中挤出前 10（9-26 消融 any@10 0.54 → 0.47），每条自带日期和说话人，读者不靠相邻也能对上。"""
    cap = int(k * config.NEIGHBOR_CAP_RATIO)
    if cap <= 0:
        return order
    cut = min(20, len(order))
    # 审查 #4：以前用 seen = set(order) 判重，邻居只要在候选里（哪怕排在第 201 位、根本返回不了）就不补。
    # 现在只把「大致会被返回的前 k 条」当作已在场，其余邻居搬进槽位并删掉旧位置，不重复。
    inside = set(order[:k])
    neighbors: list[int] = []
    for p in order[: config.NEIGHBOR_ANCHORS]:
        cands = [p + s * d for d in range(1, config.NEIGHBOR_RADIUS + 1) for s in (-1, 1)]  # 先近后远
        for q in cands:
            ok = 0 <= q < len(idx.rows) and q not in inside and q not in neighbors
            if ok and idx.rows[q].session_id == idx.rows[p].session_id:
                neighbors.append(q)
                if len(neighbors) >= cap:
                    break
        if len(neighbors) >= cap:
            break
    if not neighbors:
        return order
    nset = set(neighbors)
    return order[:cut] + neighbors + [p for p in order[cut:] if p not in nset]


def _insert_rules(idx: UserIndex, order: list[int]) -> list[int]:
    """规矩口袋：固定名额，不看相似度，放在前 10 条命中之后。"""
    present = set(order)
    rules = [p for p in idx.rule_rows if p not in present][: config.RULE_SLOT]
    if not rules:
        return order
    cut = min(10, len(order))
    return order[:cut] + rules + order[cut:]


# ---------- 装箱 ----------

async def _reranked(idx: UserIndex, q: str, order: list[int], scores: dict[int, float],
                    must: list[int] | None = None) -> list[int]:
    """对融合后的前 RERANK_TOPN 条过一遍交叉编码器，按重排分（可与 RRF 名次混合）重排；后面的原样接上。
    重排不可用时原样返回——它只改顺序，不改准入，谁能进门仍由五路通道决定。
    must：必须进重排窗口的候选（审查 #4：第二跳捞到的新证据会被双命中前置挤到窗口外，永远没机会被重排）。"""
    head = order[: config.RERANK_TOPN]
    if must:
        hs = set(head)
        extra = [p for p in must if p not in hs]
        if extra:
            head = head[: max(2, config.RERANK_TOPN - len(extra))] + extra
            hs = set(head)
            order = head + [p for p in order if p not in hs]
    if len(head) < 2:
        return order
    try:
        rs = await asyncio.wait_for(rerank(q, [_render(idx, idx.rows[p]) for p in head]), timeout=config.RERANK_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
        log.warning("rerank skipped: %s", e)
        rs = None
    if rs is None:
        return order
    n = len(head)
    mixed = {p: config.RERANK_MIX * rs[i] + (1 - config.RERANK_MIX) * (1 - i / n) for i, p in enumerate(head)}
    head = sorted(head, key=lambda p: -mixed[p])
    for p in head:
        scores[p] = round(mixed[p], 6)
    return head + order[len(head):]


def _render(idx: UserIndex, r: Row) -> str:
    who = r.speaker_name or r.role
    text = r.text
    if r.speaker_name and text.startswith(f"{r.speaker_name}:"):
        text = text[len(r.speaker_name) + 1:].lstrip()
    head = date_header(r.ts_value, r.ts_granularity, idx.session_label.get(r.session_id, "session ?"))
    part = f" (part {r.part}/{r.total})" if r.total > 1 else ""
    return f"{head} {who}:{part} {text}"


def _box(idx: UserIndex, order: list[int], scores: dict[int, float], top_k: int) -> list[dict]:
    out: list[dict] = []
    used = 0
    for p in order:
        if len(out) >= top_k:
            break
        r = idx.rows[p]
        content = _render(idx, r)
        t = count_tokens(content)
        if used + t > config.BUDGET_TOKENS:
            continue  # 整条跳过，绝不截断
        item = {"id": r.id, "content": content, "text": content, "score": round(scores.get(p, 0.0), 6)}  # text 与 content 同值：CL-Bench 管线读的是 text，空 text 静默跳过
        ca = created_at_value(r.ts_value, r.ts_granularity)
        if ca:
            item["created_at"] = ca
        out.append(item)
        used += t
    return out


# ---------- 入口 ----------

async def search(user_id: str, query: str, options: list[str] | None, top_k: int) -> list[dict]:
    # 建索引是 CPU 活（BM25 + 抽名），放线程池，别堵住事件循环里别的请求
    idx = await asyncio.to_thread(get_index, user_id)
    if not idx.rows:
        return []
    k = min(top_k, config.HARD_TOP_K)
    q = query if not options else query + " " + " ".join(options)
    intent = detect_intent(query)
    n = k * config.CHANNEL_TOPN_MULT

    bm25_hits, bm25_scores = _bm25_channel(idx, q, n)
    try:
        vec_hits, qvec = await asyncio.wait_for(_vector_channel(idx, q, n), timeout=config.SEARCH_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
        log.warning("vector channel skipped: %s", e)
        vec_hits, qvec = [], None

    channels = {
        "bm25": bm25_hits,
        "vector": vec_hits,
        "entity": _entity_channel(idx, q),
        "literal": _literal_channel(idx, q, bm25_scores),
        "date": _date_channel(idx, q, intent),
    }
    order, scores, hit_by = _rrf(channels, intent)
    order = _both_first(order, hit_by)
    # 「how many … last month」会被意图路由判成 temporal，但它仍是计数题，第二跳看题型不看路由结果
    must: list[int] = []
    if config.HOP_ENABLED and (intent in config.HOP_INTENTS or _AGG_RX.search(query)):
        hop = await _second_hop(idx, q, order, qvec, n)
        if hop:
            first_round = set(order)
            channels.update(hop)
            order, scores, hit_by = _rrf(channels, intent)
            order = _both_first(order, hit_by)
            # 第二跳独有的新候选，各路前 HOP_RESERVE 条保证进重排窗口
            for hits in hop.values():
                must.extend(p for p in hits[: config.HOP_RESERVE] if p not in first_round)
    order = await _reranked(idx, q, order, scores, must)
    if is_task_request(query):
        order = _insert_rules(idx, order)
    order = _with_neighbors(idx, order, k)
    for p in order:
        scores.setdefault(p, 0.0)
    return _box(idx, order, scores, k)
