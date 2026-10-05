"""运行配置，全部来自环境变量，没有第二个来源。"""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aml:aml-local-only@127.0.0.1:5432/aml")
API_TOKEN = os.environ.get("AML_API_TOKEN", "")
ALLOW_NO_AUTH = os.environ.get("AML_ALLOW_NO_AUTH", "") == "1"   # 只给本地冒烟用；线上必须有 token
PLACEHOLDER_TOKENS = {"change-me", "changeme", "example", "test", "token"}

# 向量：text-embedding-v4（比赛规定），走阿里百炼 OpenAI 兼容口
EMBED_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-v4")
EMBED_DIM = _int("EMBED_DIM", 1024)
EMBED_BATCH = _int("EMBED_BATCH", 10)          # 百炼同步接口单次上限
EMBED_TIMEOUT_S = _float("EMBED_TIMEOUT_S", 20)          # 单次尝试的读超时。第一次 Full 调到 10 秒，首次失败 1,686 次，别再调低
EMBED_CONNECT_TIMEOUT_S = _float("EMBED_CONNECT_TIMEOUT_S", 5)
EMBED_CONCURRENCY = _int("EMBED_CONCURRENCY", 16)         # 全进程同时在飞的向量请求上限（16 路 Add × 每个 Add 4 批，以前能冲到 64）
EMBED_MAX_CHARS = _int("EMBED_MAX_CHARS", 6000)
HTTP_MAX_CONNECTIONS = _int("HTTP_MAX_CONNECTIONS", 32)   # 百炼共用客户端的连接池大小
HTTP_KEEPALIVE_S = _float("HTTP_KEEPALIVE_S", 30)

# 重排：gte-rerank-v2（规则允许任意 reranker）。默认关，靶场对比过再开
RERANK_ENABLED = os.environ.get("RERANK_ENABLED", "") == "1"
RERANK_MODEL = os.environ.get("RERANK_MODEL", "gte-rerank-v2")
RERANK_URL = os.environ.get("RERANK_URL", "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank")
RERANK_TOPN = _int("RERANK_TOPN", 200)         # 只重排融合后的前 N 条
RERANK_TIMEOUT_S = _float("RERANK_TIMEOUT_S", 45)          # 整个重排步骤的上限（含重试），超了按融合顺序返回；线上 .env 也是 45
RERANK_ATTEMPT_TIMEOUT_S = _float("RERANK_ATTEMPT_TIMEOUT_S", 20)  # 单次尝试的读超时，比外层短才有机会重试一次
RERANK_CONCURRENCY = _int("RERANK_CONCURRENCY", 16)        # 全进程同时在飞的重排请求上限
RERANK_DOC_CHARS = _int("RERANK_DOC_CHARS", 0)  # >0 时只把每条候选的前 N 个字符送去重排（省钱），返回给平台的正文不受影响
RERANK_MIX = _float("RERANK_MIX", 1.0)         # 1.0 = 完全按重排分排；0.5 = 重排分与 RRF 名次各半

# 第二跳（伪相关反馈）：拿第一轮前几条命中的词和向量再检一轮，专治「how many / 有哪些」这类证据散在多处的题。默认关
HOP_ENABLED = os.environ.get("HOP_ENABLED", "") == "1"
HOP_INTENTS = set(filter(None, os.environ.get("HOP_INTENTS", "aggregate").split(",")))
HOP_ANCHORS = _int("HOP_ANCHORS", 5)          # 取第一轮前几条当锚
HOP_TERMS = _int("HOP_TERMS", 8)              # 从锚里挑几个扩展词
HOP_QUERY_W = _float("HOP_QUERY_W", 0.6)      # 向量第二跳里原查询向量的权重，其余给锚的质心
HOP_W = _float("HOP_W", 0.6)                  # 两路第二跳在 RRF 里的分量
HOP_RESERVE = _int("HOP_RESERVE", 20)         # 第二跳各路前几条保证进重排窗口（审查 #4：否则被双命中前置挤出窗口）

# Add 时抽取（B3，2026-10-06）：每个 Add 请求的原文交给 gpt-4o-mini，抽出带日期和主语的事实行 + 一条会话摘要，
# 作为额外的段（kind=note/summary）和原文一起进五路召回、重排、装箱。默认关；关着时写入、索引、返回与第二枪候选逐字相同。
# 复现：锁快照、temperature 0、固定 seed、只走 OpenAI/Azure 供应商、按输入哈希缓存（同一段原文只调一次，重试不重复花钱）。
EXTRACT_ENABLED = os.environ.get("EXTRACT_ENABLED", "") == "1"
# 任何 OpenAI 兼容口都行（OpenRouter、国内中转）。主办方 09-22 答疑：中转渠道允许，须在参赛备注里写明。
# OpenRouter 的账单地址在大陆时不开放 OpenAI 模型（10-06 实测页面警告），所以默认不再假定是它。
EXTRACT_API_KEY = os.environ.get("EXTRACT_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
EXTRACT_BASE_URL = os.environ.get("EXTRACT_BASE_URL", "https://aihubmix.com/v1").rstrip("/")
_ON_OPENROUTER = "openrouter.ai" in EXTRACT_BASE_URL
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", ("openai/" if _ON_OPENROUTER else "") + "gpt-4o-mini-2024-07-18")
# 只有 OpenRouter 认 provider 字段；别家收到不认识的字段可能直接 400，所以默认只在 OpenRouter 上发
EXTRACT_PROVIDERS = [p for p in os.environ.get("EXTRACT_PROVIDERS", "openai,azure" if _ON_OPENROUTER else "").split(",") if p]
EXTRACT_SEED = _int("EXTRACT_SEED", 7)
EXTRACT_MAX_OUTPUT_TOKENS = _int("EXTRACT_MAX_OUTPUT_TOKENS", 3000)   # 30 条事实 + 摘要约 1,500；留余量，截断时解析器还能捞回完整的那几条
EXTRACT_WINDOW_TOKENS = _int("EXTRACT_WINDOW_TOKENS", 6000)     # 一次调用的原文上限，超了按消息边界切成几窗
EXTRACT_MSG_MAX_TOKENS = _int("EXTRACT_MSG_MAX_TOKENS", 3000)   # 单条消息送去抽取的上限（长篇助手回答只截前面；原文照常全量入库）
EXTRACT_MAX_FACTS = _int("EXTRACT_MAX_FACTS", 40)               # 每窗最多收几条事实
EXTRACT_TIMEOUT_S = _float("EXTRACT_TIMEOUT_S", 60)             # 单次尝试的读超时
EXTRACT_ATTEMPTS = _int("EXTRACT_ATTEMPTS", 3)
EXTRACT_CONCURRENCY = _int("EXTRACT_CONCURRENCY", 16)
EXTRACT_TOKEN_CAP = _int("EXTRACT_TOKEN_CAP", 0)                # >0：本进程累计 token 超过它就不再调用、Add 回 503（回放时防烧钱）
EXTRACT_MARK_LATEST = os.environ.get("EXTRACT_MARK_LATEST", "") == "1"   # 同一话题键的多条事实，渲染时标出最新/已被更新（只影响 Search，不动库）

# 切分
SEGMENT_MAX_TOKENS = _int("SEGMENT_MAX_TOKENS", 350)

# 检索
RRF_K = _int("RRF_K", 60)
# 融合规则开关（2026-10-02）。默认 legacy = 第一次 Full 的规则（实体路按时间倒序、v0.3.1 分量）；
# candidate = 10-01 诊断里的候选（实体路除 temporal/latest 外按相关度排、实体+字面分量减半，collab/诊断-事实与多跳-20260930/fusion-sim/REPORT.md）。
# 候选只在实验里开，没有采用；这个开关让同一份代码两种规则都能跑、都能对照。
FUSION_RULE = os.environ.get("FUSION_RULE", "legacy")
assert FUSION_RULE in ("legacy", "candidate"), f"FUSION_RULE must be legacy or candidate, got {FUSION_RULE!r}"
CHANNEL_TOPN_MULT = _int("CHANNEL_TOPN_MULT", 2)   # 每路取 top_k * 2
# 9-26 LoCoMo 消融：邻居 20% + 规矩口袋常开，any@100 0.779 → 关掉 0.809。邻居改小，规矩只在「要你做事」的题上开
NEIGHBOR_CAP_RATIO = _float("NEIGHBOR_CAP_RATIO", 0.05)
NEIGHBOR_ANCHORS = _int("NEIGHBOR_ANCHORS", 10)
NEIGHBOR_RADIUS = _int("NEIGHBOR_RADIUS", 1)     # 锚点前后各补几段（同会话）
RULE_SLOT = _int("RULE_SLOT", 8)
SEARCH_TIMEOUT_S = _float("SEARCH_TIMEOUT_S", 10)
BUDGET_TOKENS = _int("SEARCH_BUDGET_TOKENS", 60000)
HARD_TOP_K = 100

# 缓存
INDEX_CACHE_USERS = _int("INDEX_CACHE_USERS", 64)
THREAD_POOL_SIZE = _int("THREAD_POOL_SIZE", 16)   # to_thread 共用的线程池；默认值 min(32, cpu+4) 在 2 核上只有 6

# 事件循环延迟看门狗
LOOP_LAG_WARN_S = _float("LOOP_LAG_WARN_S", 0.5)
