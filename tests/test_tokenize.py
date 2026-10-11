"""BM25 分词：拉丁文本不变，中日韩按双字（10-11，第一枪库里 12% 的消息含中文）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.textutil import tokenize  # noqa: E402


def test_latin_unchanged():
    assert tokenize("I bought a car in Beijing on 2023-05-20, it's red.") == ["bought", "car", "in", "beijing", "on", "2023", "05", "20", "it's", "red"]
    assert tokenize("a b c 7") == []


def test_cjk_bigrams_and_single_chars():
    assert tokenize("我昨天在北京买了车") == ["我昨", "昨天", "天在", "在北", "北京", "京买", "买了", "了车"]
    assert tokenize("在北京。买车") == ["在北", "北京", "买车"]
    assert tokenize("只有一个字：猫") == ["只有", "有一", "一个", "个字", "猫"]


def test_mixed_text_keeps_both():
    assert tokenize("用 Python 写了 BM25，在东京") == ["python", "bm25", "用", "写了", "在东", "东京"]


def test_japanese_and_korean_runs():
    assert "東京" in tokenize("東京に行った") and "서울" in tokenize("서울에 갔다")
