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

# 切分
SEGMENT_MAX_TOKENS = _int("SEGMENT_MAX_TOKENS", 350)

# 检索
RRF_K = _int("RRF_K", 60)
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
