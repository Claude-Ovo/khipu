"""多模态层的离线测试（10-11）：ContentPart[] 拆分、占位符、描述缓存、返回时带回原图。不碰网络和库。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, mm  # noqa: E402

# 1x1 PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
JPG_URI = "data:image/jpeg;base64," + base64.b64encode(JPG).decode()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def imgdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MM_IMAGE_DIR", str(tmp_path / "img"))
    return tmp_path / "img"


@pytest.fixture
def offline_cache(monkeypatch):
    store: dict[str, tuple] = {}

    def get(key):
        return store[key][0] if key in store else None

    def put(key, ref, scope, caption, status, pt, ct, meta):
        store.setdefault(key, (caption, status, scope, ref.sha))

    monkeypatch.setattr(mm, "_cache_get", get)
    monkeypatch.setattr(mm, "_cache_put", put)
    return store


def test_split_parts_keeps_order_and_placeholders():
    sha12 = hashlib.sha256(PNG).hexdigest()[:12]
    out = mm.split_parts([{"type": "text", "text": "look at my dog"}, {"type": "image_url", "image_url": {"url": PNG_URI}},
                          {"type": "text", "text": "isn't he cute"}])
    assert out is not None
    text, images = out
    assert text == f"look at my dog\n[image {sha12}]\nisn't he cute"
    assert [i.mime for i in images] == ["image/png"] and images[0].data == PNG


def test_split_parts_rejects_remote_and_bad_base64():
    text, images = mm.split_parts([{"type": "image_url", "image_url": {"url": "https://example.com/a.jpg"}},
                                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,@@@"}}, "tail"])
    assert images == [] and text == "[image unreadable]\n[image unreadable]\ntail"
    assert mm.split_parts("plain string") is None
    assert mm.split_parts([{"type": "table", "rows": []}]) is None   # 认不得的结构交给旧逻辑


def test_too_large_image_is_unreadable(monkeypatch):
    monkeypatch.setattr(config, "MM_IMAGE_MAX_BYTES", 10)
    text, images = mm.split_parts([{"type": "image_url", "image_url": {"url": JPG_URI}}])
    assert images == [] and text == "[image unreadable]"


def test_caption_cached_per_user_and_resolved_into_text(monkeypatch, imgdir, offline_cache):
    calls = []

    async def fake_call(body, kind="extract", attempts=None):
        calls.append(body)
        url = body["messages"][1]["content"][1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,") and body["model"] == config.EXTRACT_MODEL   # 送模型的是缩小版 JPEG
        return "A small white dog on a sofa. Text in image: none", "ok", 500, 30, {"model": config.EXTRACT_MODEL, "response_id": "r1"}

    import app.extract as ex
    monkeypatch.setattr(ex, "_call", fake_call)
    text, images = mm.split_parts([{"type": "text", "text": "my dog"}, {"type": "image_url", "image_url": {"url": PNG_URI}}])
    out = _run(mm.resolve_text(text, images, scope="user-a"))
    assert out == f"my dog\n{images[0].tag} A small white dog on a sofa. Text in image: none"
    assert mm.image_path(images[0].sha, "image/png").read_bytes() == PNG   # 原图存盘了
    # 同一用户再来：命中缓存，不再调模型；另一个用户：再调一次（主办方 10-10：缓存按用户隔离）
    _run(mm.resolve_text(text, images, scope="user-a"))
    assert len(calls) == 1
    _run(mm.resolve_text(text, images, scope="user-b"))
    assert len(calls) == 2
    assert sorted(v[2] for v in offline_cache.values()) == ["user-a", "user-b"]


def test_empty_caption_keeps_placeholder(monkeypatch, imgdir, offline_cache):
    async def fake_call(body, kind="extract", attempts=None):
        return None, "empty:http400", None, None, {}

    import app.extract as ex
    monkeypatch.setattr(ex, "_call", fake_call)
    text, images = mm.split_parts([{"type": "image_url", "image_url": {"url": PNG_URI}}])
    assert _run(mm.resolve_text(text, images, scope="u")) == f"{images[0].tag} (photo)"


def test_attach_images_returns_original_images_in_order_with_caps(monkeypatch, imgdir):
    a = mm.ImageRef(hashlib.sha256(PNG).hexdigest(), "image/png", PNG)
    b = mm.ImageRef(hashlib.sha256(JPG).hexdigest(), "image/jpeg", JPG)
    mm.store_image(a); mm.store_image(b)
    items = [{"id": "1", "content": f"[2023-05-20 (Sat)] user: {a.tag} a dog {b.tag} a cat", "text": "x", "score": 0.9},
             {"id": "2", "content": f"[2023-05-21 (Sun)] user: {a.tag} again", "text": "y", "score": 0.8},
             {"id": "3", "content": "[2023-05-22 (Mon)] user: no image here", "text": "z", "score": 0.7}]
    monkeypatch.setattr(config, "MM_RETURN_IMAGES", 5)
    out = mm.attach_images(json.loads(json.dumps(items)))
    assert [p["type"] for p in out[0]["content"]] == ["text", "image_url", "image_url"]
    assert out[0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")   # 缩小版一律 JPEG
    assert out[0]["content"][2]["image_url"]["url"] == JPG_URI                                # 假 JPEG 缩不了：原样
    assert [p["type"] for p in out[1]["content"]] == ["text"]          # 同一张图只带一次
    assert out[2]["content"] == [{"type": "text", "text": items[2]["content"]}]
    assert out[0]["text"] == "x"                                        # text 仍是字符串
    monkeypatch.setattr(config, "MM_RETURN_IMAGES", 1)
    out = mm.attach_images(json.loads(json.dumps(items)))
    assert [p["type"] for p in out[0]["content"]] == ["text", "image_url"]
    monkeypatch.setattr(config, "MM_RETURN_IMAGES", 5)
    monkeypatch.setattr(config, "MM_RETURN_BYTES", len(mm.small_path(a.sha).read_bytes()))
    out = mm.attach_images(json.loads(json.dumps(items)))
    assert [p["type"] for p in out[0]["content"]] == ["text", "image_url"]   # 字节上限


def test_shrink_caps_long_side_and_keeps_original(imgdir, monkeypatch):
    from io import BytesIO
    from PIL import Image
    buf = BytesIO(); Image.new("RGB", (3000, 2000), (200, 30, 30)).save(buf, format="PNG"); big = buf.getvalue()
    monkeypatch.setattr(config, "MM_SEND_MAX_PX", 1024)
    mime, data = mm.shrink(big, 1024)
    assert mime == "image/jpeg" and Image.open(BytesIO(data)).size == (1024, 683) and len(data) < len(big)
    ref = mm.ImageRef(hashlib.sha256(big).hexdigest(), "image/png", big)
    mm.store_image(ref)
    assert mm.image_path(ref.sha, "image/png").read_bytes() == big          # 原图原样留档
    m2, d2 = mm.send_bytes(ref)
    assert m2 == "image/jpeg" and d2 == mm.small_path(ref.sha).read_bytes()  # 送模型的是缩小版
    assert mm.shrink(b"not an image", 1024) == (None, b"not an image")


def test_message_model_text_track_unchanged(monkeypatch):
    # 文本实例（MM_ENABLED=0）：带图片的数组照旧只取文字、图片丢掉，和第二枪的行为一个字不差
    monkeypatch.setattr(config, "MM_ENABLED", False)
    from app.main import Message, SearchRequest
    m = Message(role="user", content=[{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": PNG_URI}}])
    assert m.content == "hi" and m.images == []
    assert "images" not in m.model_dump()
    s = SearchRequest(query=[{"type": "text", "text": "q"}], user_id="u", top_k=5)
    assert s.query.startswith("[") and s.query_images == []


def test_message_model_mm_track(monkeypatch):
    monkeypatch.setattr(config, "MM_ENABLED", True)
    from app.main import AddRequest, Message, SearchRequest
    m = Message(role="user", content=[{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": PNG_URI}}])
    assert m.content == "hi\n[image " + hashlib.sha256(PNG).hexdigest()[:12] + "]" and len(m.images) == 1
    assert "images" not in m.model_dump()     # 图片字节不进 payload_sha 的输入、不进 requests 表；占位符里的 sha 已经代表了图
    req = AddRequest(request_id="r", user_id="u", session_id="s", messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": PNG_URI}}]}])
    assert req.messages[0].content.startswith("[image ") and "images" not in json.dumps(req.model_dump())
    s = SearchRequest(query=[{"type": "text", "text": "which dog"}, {"type": "image_url", "image_url": {"url": PNG_URI}}], user_id="u", top_k=5)
    assert s.query.startswith("which dog\n[image ") and len(s.query_images) == 1
    assert "query_images" not in s.model_dump()


def test_extract_prompt_gets_image_bullet_only_when_mm(monkeypatch):
    import importlib
    import app.extract as ex
    assert "[image <id>]" not in ex.SYSTEM_PROMPT and "+mm" not in ex.PROMPT_VERSION   # 测试进程里 MM 关着：文本提示词原样
    monkeypatch.setattr(config, "MM_ENABLED", True)
    ex2 = importlib.reload(ex)
    try:
        assert "[image <id>]" in ex2.SYSTEM_PROMPT and ex2.PROMPT_VERSION.endswith("+mm1")
    finally:
        monkeypatch.setattr(config, "MM_ENABLED", False)
        importlib.reload(ex)
