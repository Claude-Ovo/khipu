"""多模态赛道（2026-10-11）：图片进来交给 gpt-4o-mini 写成描述 + 图里的文字（OCR），当文本走现有的分窗、抽取、向量、召回、重排；
Search 的题图同样先转文字；返回时把命中的原图挑前几张原样带回去当证据（接口允许 data[].content 是 ContentPart[]）。
只在 MM_ENABLED=1 的实例上参与；文本赛道的实例一个字不碰（Message 校验器那条分支不走）。

披露口径（FAQ Q23 允许 caption-only，但要如实说）：原图经 gpt-4o-mini-2024-07-18 转成描述与 OCR 文本后检索，不做图像向量；
检索命中的原图作为图片证据原样返回。描述缓存按 (提示词版本, user_id, 图片 sha256) 键，和抽取缓存同一套隔离与留档。"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .db import pool

log = logging.getLogger("khipu.mm")

CAPTION_VERSION = "c1"
CAPTION_PROMPT = """You describe one photo for a long-term memory system. Later, someone will search these descriptions with questions about the person who shared the photo, so write what a careful observer would note and nothing that is not visible.

Write 3 to 6 sentences: what the photo shows (people and what they look like and wear, animals, objects, food, vehicles, buildings, places, scenery, screenshots), the setting and time of day if visible, colors, counts, brands, and anything unusual. Do not guess names, dates or locations that are not visible.
Then one line "Text in image: ..." with every readable word, number, sign, label, price or date exactly as written (or "Text in image: none").
Plain text only."""

_IMAGE_TYPES = {"image_url", "image", "input_image"}
_DATA_URI = re.compile(r"^\s*data:(image/(?:jpeg|jpg|png|webp))\s*;\s*base64\s*,\s*(.*)$", re.S | re.I)
_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp"}
MARK = re.compile(r"\[image ([0-9a-f]{12})\]")
_inflight: dict[str, asyncio.Future] = {}


@dataclass
class ImageRef:
    sha: str          # 图片字节的 sha256（整串）
    mime: str
    data: bytes

    @property
    def tag(self) -> str:
        return f"[image {self.sha[:12]}]"


# ---------- 请求里的 ContentPart[] → 文本 + 图片 ----------

def _is_text_block(p: Any) -> bool:
    return isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)


def _is_image_block(p: Any) -> bool:
    return isinstance(p, dict) and p.get("type") in _IMAGE_TYPES


def _image_url(p: dict) -> str | None:
    u = p.get("image_url", p.get("url"))
    if isinstance(u, dict):
        u = u.get("url")
    return u if isinstance(u, str) else None


def decode_image(url: str) -> ImageRef | None:
    """只收合同规定的内联 base64 data URI（JPEG/PNG/WebP）。远程 URL、坏 base64、超大都返回 None，由调用方写成占位文字。"""
    m = _DATA_URI.match(url)
    if not m:
        return None
    mime = m.group(1).lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    b64 = re.sub(r"\s+", "", m.group(2))
    if len(b64) > config.MM_IMAGE_MAX_BYTES * 4 // 3 + 4:
        return None
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > config.MM_IMAGE_MAX_BYTES:
        return None
    return ImageRef(hashlib.sha256(data).hexdigest(), mime, data)


def split_parts(v: Any) -> tuple[str, list[ImageRef]] | None:
    """有序 ContentPart[] → (文本, 图片列表)。图片在原来的位置留一个 [image <sha12>] 占位，后面 resolve 时接上描述。
    不是我们认得的数组结构就返回 None，让 main._content_to_text 的旧逻辑接手（它把图片丢掉、其余转 JSON 文本）。"""
    if not isinstance(v, list) or not v or not all(isinstance(p, str) or _is_text_block(p) or _is_image_block(p) for p in v):
        return None
    pieces: list[str] = []
    images: list[ImageRef] = []
    for p in v:
        if isinstance(p, str):
            pieces.append(p)
        elif _is_text_block(p):
            pieces.append(p["text"])
        else:
            url = _image_url(p)
            ref = decode_image(url) if url else None
            if ref is None:
                pieces.append("[image unreadable]")
                log.warning("mm: image part not decodable (%s)", (url or "")[:40])
                continue
            images.append(ref)
            pieces.append(ref.tag)
    return "\n".join(s for s in pieces if s is not None), images


# ---------- 存盘 ----------

def image_path(sha: str, mime: str) -> Path:
    return Path(config.MM_IMAGE_DIR) / sha[:2] / f"{sha}.{_EXT.get(mime, 'bin')}"


def shrink(data: bytes, max_px: int) -> tuple[str, bytes]:
    """最长边缩到 max_px 以内、重编码成 JPEG（质量 85）。10-11 实测：Mem-Gallery 的原图直接送 gpt-4o-mini 一张两万多 token，
    缩到 1024 后一千上下，描述质量够用。缩不了（Pillow 没装、图坏了）就原样返回。"""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(data))
        im.load()
        w, h = im.size
        if max(w, h) <= max_px and im.format == "JPEG":
            return "image/jpeg", data
        if max(w, h) > max_px:
            im.thumbnail((max_px, max_px))
        if im.mode not in ("RGB", "L"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").getchannel("A"))
            im = bg
        out = BytesIO()
        im.save(out, format="JPEG", quality=85, optimize=True)
        return "image/jpeg", out.getvalue()
    except Exception as e:  # noqa: BLE001  图坏了或 Pillow 缺失：原样送，别让一张图把 Add 打成 500
        log.warning("mm: shrink failed (%s), sending original", type(e).__name__)
        return None, data


def small_path(sha: str) -> Path:
    return Path(config.MM_IMAGE_DIR) / sha[:2] / f"{sha}.s{config.MM_SEND_MAX_PX}.jpg"


def _write_atomic(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)


def store_image(ref: ImageRef) -> Path:
    """原图存盘（留档、复核），旁边放一份缩小版（送模型、当证据返回）。"""
    p = image_path(ref.sha, ref.mime)
    if not p.exists():
        _write_atomic(p, ref.data)
    sp = small_path(ref.sha)
    if not sp.exists():
        mime, data = shrink(ref.data, config.MM_SEND_MAX_PX)
        if mime == "image/jpeg":
            _write_atomic(sp, data)
    return p


def send_bytes(ref: ImageRef) -> tuple[str, bytes]:
    sp = small_path(ref.sha)
    if sp.exists():
        return "image/jpeg", sp.read_bytes()
    mime, data = shrink(ref.data, config.MM_SEND_MAX_PX)
    return (mime or ref.mime), data


def _find_image(sha12: str) -> Path | None:
    """返回证据用的那份：优先缩小版，没有就原图。"""
    d = Path(config.MM_IMAGE_DIR) / sha12[:2]
    if not d.is_dir():
        return None
    small = [p for p in d.glob(f"{sha12}*.s*.jpg") if not p.name.endswith(".tmp")]
    if small:
        return small[0]
    for p in d.glob(f"{sha12}*"):
        if p.suffix != ".tmp":
            return p
    return None


# ---------- 描述（缓存按 用户 + 图片 隔离，留回复身份） ----------

def caption_key(sha: str, scope: str) -> str:
    return hashlib.sha256((CAPTION_VERSION + "\n" + scope + "\n" + config.EXTRACT_MODEL + "\n" + sha).encode()).hexdigest()


def _cache_get(key: str) -> str | None:
    with pool.connection() as conn:
        row = conn.execute("SELECT caption FROM image_cache WHERE cache_sha = %s", (key,)).fetchone()
    return row[0] if row else None


def _cache_put(key: str, ref: ImageRef, scope: str, caption: str, status: str, pt: int | None, ct: int | None, meta: dict) -> None:
    with pool.connection() as conn:
        conn.execute("INSERT INTO image_cache (cache_sha, image_sha, user_id, mime, bytes, caption, status, model, prompt_version, "
                     "response_id, system_fingerprint, prompt_tokens, completion_tokens) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                     "ON CONFLICT (cache_sha) DO NOTHING",
                     (key, ref.sha, scope or None, ref.mime, len(ref.data), caption, status, config.EXTRACT_MODEL, CAPTION_VERSION,
                      meta.get("response_id"), meta.get("system_fingerprint"), pt, ct))
        conn.commit()


def caption_body(ref: ImageRef) -> dict:
    mime, data = send_bytes(ref)
    url = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    body = {
        "model": config.EXTRACT_MODEL,
        "messages": [{"role": "system", "content": CAPTION_PROMPT},
                     {"role": "user", "content": [{"type": "text", "text": "Describe this photo."},
                                                  {"type": "image_url", "image_url": {"url": url, "detail": config.MM_DETAIL}}]}],
        "temperature": 0,
        "seed": config.EXTRACT_SEED,
        "max_tokens": config.MM_CAPTION_MAX_TOKENS,
    }
    if config.EXTRACT_PROVIDERS:
        body["provider"] = {"only": config.EXTRACT_PROVIDERS, "allow_fallbacks": False, "require_parameters": True}
    return body


async def _caption_uncached(key: str, ref: ImageRef, scope: str) -> str:
    from .extract import _call, clean_text  # 同一条调用路径：模型名核验、重试、用量计数、ReplyMeta
    content, status, pt, ct, meta = await _call(caption_body(ref), kind="extract")
    text = " ".join(clean_text(content).split())[:2000] if isinstance(content, str) else ""
    if status != "ok" or not text:
        log.warning("mm: caption for %s kept empty (%s)", ref.sha[:12], status)
        text = ""
    await asyncio.to_thread(_cache_put, key, ref, scope, text, status if text else (status if status != "ok" else "empty:blank"),
                            pt, ct, meta)
    return text


async def caption(ref: ImageRef, scope: str) -> str:
    """一张图在一个用户范围内只调一次模型（平台重试同一个 Add 不重复花钱）；同进程内同一张图同时到达只飞一次。"""
    key = caption_key(ref.sha, scope)
    hit = await asyncio.to_thread(_cache_get, key)
    if hit is not None:
        return hit
    fut = _inflight.get(key)
    if fut is None:
        fut = asyncio.get_running_loop().create_future()
        _inflight[key] = fut
        try:
            text = await _caption_uncached(key, ref, scope)
            fut.set_result(text)
        except BaseException as e:
            fut.set_exception(e)
            raise
        finally:
            _inflight.pop(key, None)
        return text
    return await asyncio.shield(fut)


async def resolve_text(text: str, images: list[ImageRef], scope: str) -> str:
    """把占位符接上描述：`[image <sha12>] <描述> Text in image: …`。图片先存盘（返回证据时按 sha 找）。"""
    if not images:
        return text
    await asyncio.to_thread(lambda: [store_image(r) for r in images])
    caps = await asyncio.gather(*(caption(r, scope) for r in images))
    for ref, cap in zip(images, caps):
        text = text.replace(ref.tag, f"{ref.tag} {cap}" if cap else f"{ref.tag} (photo)", 1)
    return text


# ---------- 返回：命中的原图当证据 ----------

def attach_images(items: list[dict]) -> list[dict]:
    """data[].content 改成有序 ContentPart[]：先是现在的文本（日期头 + 正文，含描述），再把正文里标了 [image <sha12>] 的原图
    按出现顺序带回去，整个响应最多 MM_RETURN_IMAGES 张、MM_RETURN_BYTES 字节；一张图只带一次。text 字段仍是字符串。"""
    n = 0
    used = 0
    seen: set[str] = set()
    for it in items:
        content = it.get("content")
        if not isinstance(content, str):
            continue
        parts: list[dict] = [{"type": "text", "text": content}]
        for sha12 in MARK.findall(content):
            if sha12 in seen or n >= config.MM_RETURN_IMAGES:
                continue
            p = _find_image(sha12)
            if p is None:
                continue
            data = p.read_bytes()
            if used + len(data) > config.MM_RETURN_BYTES:
                continue
            mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(p.suffix[1:], "image/jpeg")   # 缩小版一律 .jpg
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")}})
            seen.add(sha12)
            n += 1
            used += len(data)
        it["content"] = parts
    return items


def parts_text(parts: list[dict]) -> str:
    """测试和日志用：ContentPart[] 里的文本部分。"""
    return "\n".join(p["text"] for p in parts if p.get("type") == "text")
