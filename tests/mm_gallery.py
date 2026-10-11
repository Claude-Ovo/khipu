"""Mem-Gallery 靶场（多模态赛道，10-11）：把对话按会话经 HTTP Add 进开着 MM 的实例，再把人工标注的题逐题 Search，
算线索轮次的召回（clue 轮的原文段有没有进前 k）和「答案是某张图」的题里那张图有没有被当证据带回。
只做「有没有大错」的冒烟，不调参。

数据：HF Ethan-Bei/Mem-Gallery → data/dialog/*.json（每个文件一个场景 = 一个用户）、data/image/<场景>/<id>.jpg。
用法（Morrow）：
  /srv/aml/.venv/bin/python tests/mm_gallery.py ingest --base http://127.0.0.1:8084 --token T --data /srv/aml/data/memgallery/data --convs 3
  /srv/aml/.venv/bin/python tests/mm_gallery.py search --base ... --token T --data ... --convs 3 --out /srv/aml/data/mmg/search.jsonl"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

TAG = "mmg"
_IMG_ID = re.compile(r"^D\d+:IMG_\d+$")


def convs(data: Path, n: int) -> list[tuple[str, Path, dict]]:
    files = sorted((data / "dialog").glob("*.json"))[:n]
    return [(f.stem, f, json.load(open(f, encoding="utf-8"))) for f in files]


def session_ts(date: str) -> int | None:
    try:
        return int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


def image_uri(data: Path, scene: str, rel: str) -> str | None:
    p = data / "image" / scene / Path(rel).name
    if not p.exists():
        return None
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(p.suffix[1:].lower(), "image/jpeg")
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def image_sha12(data: Path, scene: str, rel: str) -> str | None:
    p = data / "image" / scene / Path(rel).name
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.exists() else None


def session_messages(data: Path, scene: str, sess: dict) -> list[dict]:
    """一轮 = user 一条（带图就是 ContentPart[]）+ assistant 一条；时间戳用会话日期。"""
    ts = session_ts(sess.get("date", ""))
    out = []
    for d in sess["dialogues"]:
        imgs = d.get("input_image") or []
        if imgs:
            parts: list[dict] = [{"type": "text", "text": d["user"]}]
            for rel in imgs:
                u = image_uri(data, scene, rel)
                if u:
                    parts.append({"type": "image_url", "image_url": {"url": u}})
            content: object = parts
        else:
            content = d["user"]
        out.append({"role": "user", "content": content, **({"timestamp": ts} if ts else {})})
        out.append({"role": "assistant", "content": d["assistant"], **({"timestamp": ts} if ts else {})})
    return out


def ingest(a) -> None:
    data = Path(a.data)
    client = httpx.Client(base_url=a.base, headers={"Authorization": f"Bearer {a.token}"} if a.token else {}, timeout=1800)
    for scene, _, conv in convs(data, a.convs):
        uid = f"{TAG}:{scene}"
        t0 = time.time()
        n_img = 0
        for sess in conv["multi_session_dialogues"]:
            sid = f"{TAG}:{scene}:{sess['session_id']}"
            msgs = session_messages(data, scene, sess)
            n_img += sum(1 for m in msgs if isinstance(m["content"], list))
            body = {"request_id": f"{sid}:chunk-0", "user_id": uid, "session_id": sid, "messages": msgs}
            for attempt in range(6):
                r = client.post("/add", json=body)
                if r.status_code != 503:
                    break
                time.sleep(5 * (attempt + 1))
            r.raise_for_status()
            print(f"  {sid} {len(msgs)} msgs {r.status_code} {time.time() - t0:.0f}s", flush=True)
        print(f"{scene}: {len(conv['multi_session_dialogues'])} sessions, {n_img} image rounds, {time.time() - t0:.0f}s", flush=True)


def search(a) -> None:
    data = Path(a.data)
    client = httpx.Client(base_url=a.base, headers={"Authorization": f"Bearer {a.token}"} if a.token else {}, timeout=300)
    tot = hit10 = hit100 = 0
    img_q = img_hit = 0
    by_point: dict[str, list[int]] = {}
    with open(a.out, "w", encoding="utf-8") as f:
        for scene, _, conv in convs(data, a.convs):
            uid = f"{TAG}:{scene}"
            # 轮次 → 该轮两条消息的段 id 前缀（一轮两条：user 是 2k-1，assistant 是 2k）
            round_pos: dict[str, tuple[str, int]] = {}
            img_of_round: dict[str, str] = {}
            for sess in conv["multi_session_dialogues"]:
                sid = f"{TAG}:{scene}:{sess['session_id']}"
                for k, d in enumerate(sess["dialogues"], start=1):
                    round_pos[d["round"]] = (sid, k)
                    for iid, rel in zip(d.get("image_id") or [], d.get("input_image") or []):
                        s = image_sha12(data, scene, rel)
                        if s:
                            img_of_round[iid] = s
            for q in conv["human-annotated QAs"]:
                query: object = q["question"]
                if q.get("question_image"):   # 题目带图（VS/VR 类）：按合同用 ContentPart[] 发
                    u = image_uri(data, scene, q["question_image"])
                    if u:
                        query = [{"type": "text", "text": q["question"]}, {"type": "image_url", "image_url": {"url": u}}]
                r = client.post("/search", json={"query": query, "user_id": uid, "top_k": 100})
                r.raise_for_status()
                items = r.json()["data"]
                ids = [it["id"] for it in items]
                want = set()
                for c in q.get("clue") or []:
                    if c in round_pos:
                        sid, k = round_pos[c]
                        want.add(f"{sid}#{2 * k - 1}.")
                        want.add(f"{sid}#{2 * k}.")
                def rank() -> int | None:
                    for i, iid in enumerate(ids):
                        if any(iid.startswith(w) for w in want):
                            return i
                    return None
                rk = rank()
                tot += 1
                hit10 += rk is not None and rk < 10
                hit100 += rk is not None
                by_point.setdefault(q["point"], []).append(1 if rk is not None and rk < 10 else 0)
                row = {"scene": scene, "point": q["point"], "question": q["question"], "answer": q["answer"], "clue": q.get("clue"),
                       "rank": rk, "returned": len(ids), "question_image": bool(q.get("question_image"))}
                ans = q["answer"]
                if isinstance(ans, str) and _IMG_ID.match(ans.strip()):
                    img_q += 1
                    sha = img_of_round.get(ans.strip())
                    got = [p for it in items for p in (it["content"] if isinstance(it["content"], list) else []) if p.get("type") == "image_url"]
                    tags = [m for it in items for m in re.findall(r"\[image ([0-9a-f]{12})\]", json.dumps(it["content"]))]
                    row["answer_image_sha"] = sha
                    row["answer_image_returned"] = bool(sha and sha in tags[:a.img_top])
                    row["images_returned"] = len(got)
                    img_hit += row["answer_image_returned"]
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"{scene}: done {tot} so far", flush=True)
    print(f"questions {tot}: clue in top10 {hit10} ({100 * hit10 / max(tot, 1):.0f}%), in top100 {hit100} ({100 * hit100 / max(tot, 1):.0f}%)")
    print("by point (top10 rate):", {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_point.items())})
    print(f"image-answer questions {img_q}: answer image tagged within first {a.img_top} tags {img_hit}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["ingest", "search"])
    ap.add_argument("--base", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--data", required=True)
    ap.add_argument("--convs", type=int, default=3)
    ap.add_argument("--out", default="mmg-search.jsonl")
    ap.add_argument("--img-top", type=int, default=10)
    a = ap.parse_args()
    ingest(a) if a.cmd == "ingest" else search(a)


if __name__ == "__main__":
    main()
