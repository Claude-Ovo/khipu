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


def _find_image(sha: str, mime: str | None) -> Path | None:
    """返回证据用的那份：优先缩小版，没有就原图。按完整 sha 定位（Codex #12-5：短前缀查目录可能取到同前缀的另一张）。"""
    sp = small_path(sha)
    if sp.exists():
        return sp
    p = image_path(sha, mime or "image/jpeg")
    return p if p.exists() else None


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


# ---------- EXIF：拍摄时间与 GPS（ATM-Bench 的照片全带；平台发的是原始字节，EXIF 就在里面） ----------

def exif_info(data: bytes) -> tuple[str | None, float | None, float | None]:
    """返回 (拍摄时间 'YYYY-MM-DD HH:MM', 纬度, 经度)。没有或读不出就是 None。"""
    try:
        from io import BytesIO
        from PIL import Image
        e = Image.open(BytesIO(data)).getexif()
        d = e.get_ifd(0x8769).get(36867) or e.get(306)
        when = None
        if isinstance(d, str) and len(d) >= 16:
            y, mo, dd = d[0:4], d[5:7], d[8:10]
            if y.isdigit() and mo.isdigit() and dd.isdigit():
                when = f"{y}-{mo}-{dd} {d[11:16]}"
        g = e.get_ifd(0x8825)
        lat = lon = None
        if g and 2 in g and 4 in g:
            def deg(v):
                return float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            lat = deg(g[2]) * (-1 if str(g.get(1, "N")).upper() == "S" else 1)
            lon = deg(g[4]) * (-1 if str(g.get(3, "E")).upper() == "W" else 1)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                lat = lon = None
        return when, lat, lon
    except Exception:  # noqa: BLE001
        return None, None, None


def exif_ts_ms(data: bytes) -> int | None:
    """消息没带时间戳时用拍摄时间（当 UTC，和平台把钟面时间当 UTC 的做法一致）。"""
    when, _, _ = exif_info(data)
    if not when:
        return None
    try:
        from datetime import datetime, timezone
        return int(datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


_geo_lock = asyncio.Lock()
_geo_last = 0.0


def _geo_key(lat: float, lon: float) -> str:
    return f"{lat:.4f},{lon:.4f}"   # 约 10 m 一格；同一地点连拍共用一次查询（审查 #12-4：100 m 格子会让隔壁两家店共用一个门牌）


def _geo_get(key: str) -> str | None:
    with pool.connection() as conn:
        row = conn.execute("SELECT place FROM geo_cache WHERE k = %s", (key,)).fetchone()
    return row[0] if row else None


def _geo_put(key: str, lat: float, lon: float, place: str, raw: str | None) -> None:
    with pool.connection() as conn:
        conn.execute("INSERT INTO geo_cache (k, lat, lon, place, raw) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (k) DO NOTHING",
                     (key, lat, lon, place, raw))
        conn.commit()


async def reverse_geocode(lat: float, lon: float) -> str:
    """OpenStreetMap Nominatim 反查地址（不是模型，是查表；对外声明为外部依赖）。全进程串行、每秒至多 1 次（Nominatim 的使用政策），
    结果按 100 m 网格缓存进库。查不到或超时返回空串，正文里只留经纬度。"""
    global _geo_last
    if not config.MM_GEOCODE:
        return ""
    key = _geo_key(lat, lon)
    hit = await asyncio.to_thread(_geo_get, key)
    if hit is not None:
        return hit
    import time
    import httpx
    async with _geo_lock:
        hit = await asyncio.to_thread(_geo_get, key)
        if hit is not None:
            return hit
        wait = _geo_last + 1.05 - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        place, raw = "", None
        try:
            async with httpx.AsyncClient(timeout=config.MM_GEOCODE_TIMEOUT_S, headers={"User-Agent": config.MM_GEOCODE_UA}) as c:
                r = await c.get(config.MM_GEOCODE_URL, params={"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "format": "jsonv2", "zoom": 18,
                                                              "addressdetails": 1, "accept-language": "en"})
            _geo_last = time.monotonic()
            if r.status_code == 200:
                j = r.json()
                place = " ".join(str(j.get("display_name", "")).split())[:300]
                raw = json.dumps(j, ensure_ascii=False)[:4000]
            else:
                log.warning("mm: geocode %s -> %s", key, r.status_code)
        except Exception as e:  # noqa: BLE001
            _geo_last = time.monotonic()
            log.warning("mm: geocode %s failed: %s", key, type(e).__name__)
        if place or raw is not None:
            await asyncio.to_thread(_geo_put, key, lat, lon, place, raw)
        return place


async def exif_line(ref: ImageRef) -> str:
    """`Photo taken 2024-05-27 17:55 at <地址> (52.204, 0.127).` 没有 EXIF 就是空串。"""
    when, lat, lon = await asyncio.to_thread(exif_info, ref.data)
    if not when and lat is None:
        return ""
    parts = ["Photo taken"]
    if when:
        parts.append(when)
    if lat is not None and lon is not None:
        place = await reverse_geocode(lat, lon)
        parts.append(f"at {place} ({lat:.4f}, {lon:.4f})" if place else f"at ({lat:.4f}, {lon:.4f})")
    return " ".join(parts) + "."


async def resolve_text(text: str, images: list[ImageRef], scope: str) -> str:
    """把占位符接上描述：`[image <sha12>] Photo taken … at …. <描述> Text in image: …`。图片先存盘（返回证据时按 sha 找）。"""
    if not images:
        return text
    await asyncio.to_thread(lambda: [store_image(r) for r in images])
    caps, exifs = await asyncio.gather(asyncio.gather(*(caption(r, scope) for r in images)),
                                       asyncio.gather(*(exif_line(r) for r in images)))
    for ref, cap, ex in zip(images, caps, exifs):
        body = " ".join(s for s in (ex, cap or "(photo)") if s)
        text = text.replace(ref.tag, f"{ref.tag} {body}", 1)
    return text


# ---------- 返回：命中的原图当证据 ----------

def wanted_tags(items: list[dict]) -> list[str]:
    """返回正文里出现的全部图片短标签（去重、排序）。"""
    return sorted({sha12 for it in items if isinstance(it.get("content"), str) for sha12 in MARK.findall(it["content"])})


def owned_images(user_id: str, sha12s: list[str]) -> dict[str, tuple[str, str | None]]:
    """短标签 → (完整 sha, mime)，只认这个 user_id 名下进过描述缓存的图。正文里的标签不当凭证（审查 #12-3：谁的正文里写了
    一串别人的哈希，就能把别人的图带出来）；图片目录是全局按 sha 存的，归属只认 image_cache 的 user_id。同步查库，调用方放线程池。"""
    if not sha12s:
        return {}
    with pool.connection() as conn:
        rows = conn.execute("SELECT DISTINCT image_sha, mime FROM image_cache WHERE user_id = %s AND left(image_sha, 12) = ANY(%s)",
                            (user_id, sha12s)).fetchall()
    out: dict[str, tuple[str, str | None]] = {}
    for sha, mime in rows:
        out.setdefault(sha[:12], (sha, mime))
    return out


def attach_images(items: list[dict], owned: dict[str, tuple[str, str | None]]) -> list[dict]:
    """data[].content 改成有序 ContentPart[]：先是现在的文本（日期头 + 正文，含描述），再把正文里标了 [image <sha12>] 的原图
    按出现顺序带回去，整个响应最多 MM_RETURN_IMAGES 张、MM_RETURN_BYTES 字节；一张图只带一次。text 字段仍是字符串。
    只带 owned（该用户名下的 短标签→完整 sha）里有的图；对不上归属的标签原样留在正文里、不附图。读文件是同步的，调用方放线程池。"""
    n = 0
    used = 0
    seen: set[str] = set()
    for it in items:
        content = it.get("content")
        if not isinstance(content, str):
            continue
        parts: list[dict] = [{"type": "text", "text": content}]
        for sha12 in MARK.findall(content):
            if sha12 in seen or n >= config.MM_RETURN_IMAGES or sha12 not in owned:
                continue
            p = _find_image(*owned[sha12])
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
