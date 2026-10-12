"""ATM-Bench 冒烟（多模态赛道，10-11）：一个人四年的相册（3,759 张 jpg，文件名就是拍摄时间）+ 邮件（6,742 封，有时间戳和正文）。
平台怎么分块、图片配不配文字都不知道，这里按最保守的猜：每张图一条 user 消息，只有图片部分；每封邮件一条 user 消息，正文是 detail；
按时间排序，20 条一个 Add。只取一部分做冒烟：N 道题的全部证据 + 随机的干扰图和干扰邮件，看证据能不能被召回、图片证据能不能带回。
用法（Morrow）：
  python tests/mm_atm.py ingest --base http://127.0.0.1:8084 --token T --data /srv/aml/data/atm/data --questions 100 --extra-images 400 --extra-emails 1500
  python tests/mm_atm.py search --base ... --token T --data ... --questions 100 --out /srv/aml/data/atm/search.jsonl"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

TAG = "atm"
UID = f"{TAG}:user"
SID = f"{TAG}:life"


def set_tag(tag: str) -> None:
    global TAG, UID, SID
    TAG, UID, SID = tag, f"{tag}:user", f"{tag}:life"


def load(data: Path):
    qs = json.load(open(data / "atm-bench" / "atm-bench.json", encoding="utf-8"))
    emails = {e["id"]: e for e in json.load(open(data / "raw_memory" / "email" / "emails.json", encoding="utf-8"))}
    images = {p.stem: p for p in (data / "raw_memory" / "image").glob("*.jpg")}
    return qs, emails, images


def pick(qs: list[dict], n: int, seed: int) -> list[dict]:
    img = [q for q in qs if any(not e.startswith("email") for e in q["evidence_ids"])]
    em = [q for q in qs if all(e.startswith("email") for e in q["evidence_ids"])]
    r = random.Random(seed)
    r.shuffle(img); r.shuffle(em)
    return img[: n // 2] + em[: n - n // 2]


def ts_email(s: str) -> int | None:
    try:
        return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


def ts_image(stem: str) -> int | None:
    try:
        return int(datetime.strptime(stem[:15], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


def thumb(data: bytes) -> list[int]:
    """16x16 灰度缩略图：返回的是 1024 px 的 JPEG 副本，字节哈希对不上原图，只能按像素认（审查 #12-5）。"""
    import io
    from PIL import Image
    return list(Image.open(io.BytesIO(data)).convert("L").resize((16, 16)).getdata())


def same(a: list[int], b: list[int]) -> bool:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) < 12


def attached_images(items: list[dict]) -> list[bytes]:
    """响应里真正附上的图片（按出现顺序），不是正文里的标签。"""
    out = []
    for it in items:
        c = it["content"]
        if isinstance(c, list):
            for part in c:
                if part.get("type") == "image_url":
                    out.append(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]))
    return out


def corpus(a) -> tuple[list[dict], list[dict]]:
    """返回 (题, 时间序消息)。"""
    data = Path(a.data)
    qs, emails, images = load(data)
    chosen = pick(qs, a.questions, a.seed)
    need = {e for q in chosen for e in q["evidence_ids"]}
    r = random.Random(a.seed + 1)
    extra_img = r.sample([k for k in images if k not in need], min(a.extra_images, len(images)))
    extra_em = r.sample([k for k in emails if k not in need], min(a.extra_emails, len(emails)))
    msgs = []
    for k in list(need) + extra_img + extra_em:
        if k.startswith("email"):
            e = emails.get(k)
            if not e:
                continue
            msgs.append({"ts": ts_email(e["timestamp"]) or 0, "kind": "email", "key": k, "text": e["detail"]})
        else:
            p = images.get(k)
            if not p:
                continue
            msgs.append({"ts": ts_image(k) or 0, "kind": "image", "key": k, "path": p})
    msgs.sort(key=lambda m: m["ts"])
    return chosen, msgs


def to_message(m: dict) -> dict:
    if m["kind"] == "email":
        return {"role": "user", "content": m["text"], "timestamp": m["ts"]}
    data = m["path"].read_bytes()
    return {"role": "user", "timestamp": m["ts"],
            "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}}]}


def ingest(a) -> None:
    chosen, msgs = corpus(a)
    print(f"questions {len(chosen)}, messages {len(msgs)} (images {sum(m['kind'] == 'image' for m in msgs)}, emails {sum(m['kind'] == 'email' for m in msgs)})", flush=True)
    client = httpx.Client(base_url=a.base, headers={"Authorization": f"Bearer {a.token}"} if a.token else {}, timeout=1800)
    t0 = time.time()
    for k in range(0, len(msgs), a.chunk):
        body = {"request_id": f"{SID}:chunk-{k // a.chunk}", "user_id": UID, "session_id": SID, "messages": [to_message(m) for m in msgs[k:k + a.chunk]]}
        for attempt in range(6):
            r = client.post("/add", json=body)
            if r.status_code != 503:
                break
            time.sleep(5 * (attempt + 1))
        r.raise_for_status()
        if (k // a.chunk) % 10 == 0:
            print(f"  chunk {k // a.chunk} ok {time.time() - t0:.0f}s", flush=True)
    print(f"ingest done {time.time() - t0:.0f}s", flush=True)


def search(a) -> None:
    chosen, msgs = corpus(a)
    data = Path(a.data)
    _, emails, images = load(data)
    sha12 = {k: hashlib.sha256(p.read_bytes()).hexdigest()[:12] for k, p in images.items()}
    subj = {}
    for k, e in emails.items():
        m = re.search(r"^Subject: (.+)$", e["detail"], re.M)
        subj[k] = (m.group(1).strip() if m else e["detail"][:80]).lower()
    client = httpx.Client(base_url=a.base, headers={"Authorization": f"Bearer {a.token}"} if a.token else {}, timeout=300)
    tot = hit10 = hit100 = allhit10 = 0
    by = {"image": [0, 0], "email": [0, 0]}
    img_evidence = img_returned = img_attached = 0
    n_attached: list[int] = []
    with open(a.out, "w", encoding="utf-8") as f:
        for q in chosen:
            r = client.post("/search", json={"query": q["question"], "user_id": UID, "top_k": 100})
            r.raise_for_status()
            items = r.json()["data"]
            texts = []
            for it in items:
                c = it["content"]
                texts.append(" ".join(p["text"] for p in c if p.get("type") == "text").lower() if isinstance(c, list) else str(c).lower())
            ranks = []
            for e in q["evidence_ids"]:
                key = sha12.get(e, "\u0000") if not e.startswith("email") else subj.get(e, "\u0000")
                rk = next((i for i, t in enumerate(texts) if key in t), None)
                ranks.append(rk)
            kind = "email" if all(e.startswith("email") for e in q["evidence_ids"]) else "image"
            first = min((x for x in ranks if x is not None), default=None)
            tot += 1
            hit10 += first is not None and first < 10
            hit100 += first is not None
            allhit10 += all(x is not None and x < 10 for x in ranks)
            by[kind][0] += first is not None and first < 10
            by[kind][1] += 1
            attached_rank = None
            if kind == "image":
                img_evidence += 1
                tags = [m for it in items for m in re.findall(r"\[image ([0-9a-f]{12})\]", json.dumps(it["content"]))]
                img_returned += any(sha12.get(e) in tags[:a.img_top] for e in q["evidence_ids"] if not e.startswith("email"))
                # 真附上的图：逐张解码，和证据原图的缩略图比像素
                ev = [thumb(images[e].read_bytes()) for e in q["evidence_ids"] if e in images]
                got = attached_images(items)
                n_attached.append(len(got))
                for i, data in enumerate(got):
                    t = thumb(data)
                    if any(same(t, x) for x in ev):
                        attached_rank = i
                        break
                img_attached += attached_rank is not None
            f.write(json.dumps({"id": q["id"], "qtype": q["qtype"], "kind": kind, "question": q["question"], "answer": q["answer"],
                                "evidence": q["evidence_ids"], "ranks": ranks, "returned": len(items),
                                "attached_rank": attached_rank}, ensure_ascii=False) + "\n")
    print(f"questions {tot}: first evidence in top10 {hit10} ({100 * hit10 / tot:.0f}%), in top100 {hit100} ({100 * hit100 / tot:.0f}%), all evidence in top10 {allhit10}")
    print("by kind (top10):", {k: f"{v[0]}/{v[1]}" for k, v in by.items()})
    print(f"image-evidence questions {img_evidence}: an evidence image among first {a.img_top} returned image tags {img_returned} "
          f"(tag count, old metric); an evidence image actually attached in the response {img_attached} "
          f"(images attached per response: avg {sum(n_attached) / max(1, len(n_attached)):.1f}, max {max(n_attached, default=0)})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ingest", "search"])
    ap.add_argument("--base", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--data", required=True)
    ap.add_argument("--questions", type=int, default=100)
    ap.add_argument("--extra-images", type=int, default=400)
    ap.add_argument("--extra-emails", type=int, default=1500)
    ap.add_argument("--chunk", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--img-top", type=int, default=10)
    ap.add_argument("--out", default="atm-search.jsonl")
    ap.add_argument("--tag", default="atm")
    a = ap.parse_args()
    set_tag(a.tag)
    ingest(a) if a.cmd == "ingest" else search(a)


if __name__ == "__main__":
    main()
