"""运行配置，全部来自环境变量，没有第二个来源。"""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aml:aml-local-only@127.0.0.1:5432/aml")
API_TOKEN = os.environ.get("AML_API_TOKEN", "")  # 空 = 不鉴权，只允许本地/冒烟

# 向量：text-embedding-v4（比赛规定），走阿里百炼 OpenAI 兼容口
EMBED_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-v4")
EMBED_DIM = _int("EMBED_DIM", 1024)
EMBED_BATCH = _int("EMBED_BATCH", 10)          # 百炼同步接口单次上限
EMBED_TIMEOUT_S = _float("EMBED_TIMEOUT_S", 20)
EMBED_MAX_CHARS = _int("EMBED_MAX_CHARS", 6000)

# 切分
SEGMENT_MAX_TOKENS = _int("SEGMENT_MAX_TOKENS", 350)

# 检索
RRF_K = _int("RRF_K", 60)
CHANNEL_TOPN_MULT = _int("CHANNEL_TOPN_MULT", 2)   # 每路取 top_k * 2
# 9-26 LoCoMo 消融：邻居 20% + 规矩口袋常开，any@100 0.779 → 关掉 0.809。邻居改小，规矩只在「要你做事」的题上开
NEIGHBOR_CAP_RATIO = _float("NEIGHBOR_CAP_RATIO", 0.05)
NEIGHBOR_ANCHORS = _int("NEIGHBOR_ANCHORS", 10)
RULE_SLOT = _int("RULE_SLOT", 8)
SEARCH_TIMEOUT_S = _float("SEARCH_TIMEOUT_S", 10)
BUDGET_TOKENS = _int("SEARCH_BUDGET_TOKENS", 60000)
HARD_TOP_K = 100

# 缓存
INDEX_CACHE_USERS = _int("INDEX_CACHE_USERS", 64)
