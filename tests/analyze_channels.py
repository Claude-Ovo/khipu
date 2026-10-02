"""离线重算融合规则：读 diag_channels_replay.py 落盘的各路命中，复现 app/search.py 的 RRF + 双命中前置（V0 必须逐字对上 fused），
再按候选改法重排，按数据集 / 类别比「黄金证据全部进重排窗口（前 200）」和「进前 100」。不调任何接口。

用法：python tests/analyze_channels.py channels-locomo.jsonl channels-lme.jsonl [--window 200]
LME 两种口径都算：turn 级（has_answer 的那几条）和 session 级（每个答案会话至少有一段进窗口）。"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.search import _VIRTUAL_RANK, _WEIGHTS_LEGACY as _WEIGHTS  # noqa: E402


def rrf(ch: dict[str, list[int]], w: dict[str, float], both_first: bool = True) -> list[int]:
    score: dict[int, float] = {}
    hit: dict[int, set[str]] = {}
    for name, ids in ch.items():
        start = 0 if name in ("bm25", "vector") else _VIRTUAL_RANK
        for i, x in enumerate(ids):
            score[x] = score.get(x, 0.0) + w[name] / (config.RRF_K + start + i + 1)
            hit.setdefault(x, set()).add(name)
    order = sorted(score, key=lambda p: -score[p])
    if both_first:
        both = [p for p in order if {"bm25", "vector"} <= hit[p]]
        bs = set(both)
        order = both + [p for p in order if p not in bs]
    return order


def entity_by_relevance(ch: dict[str, list[int]]) -> dict[str, list[int]]:
    """候选改法：实体路不再按时间倒序，改成按它在 bm25 / 向量里的最好名次排；两路都没捞到的接在后面、保持原顺序。"""
    br = {x: i for i, x in enumerate(ch["bm25"])}
    vr = {x: i for i, x in enumerate(ch["vector"])}
    ent = ch.get("entity", [])
    ranked = sorted([x for x in ent if x in br or x in vr], key=lambda x: min(br.get(x, 1 << 30), vr.get(x, 1 << 30)))
    rest = [x for x in ent if x not in br and x not in vr]
    return dict(ch, entity=ranked + rest)


def entity_blend(ch: dict[str, list[int]], recency_mult: float) -> dict[str, list[int]]:
    """折中：实体路按 min(相关度名次, 时间名次 × recency_mult) 排。recency_mult 越大越接近纯相关度；两路都没捞到的按时间名次 × mult。"""
    br = {x: i + 1 for i, x in enumerate(ch["bm25"])}
    vr = {x: i + 1 for i, x in enumerate(ch["vector"])}
    ent = ch.get("entity", [])
    def key(x, i):
        rel = min(br.get(x, 1 << 30), vr.get(x, 1 << 30))
        return min(rel, (i + 1) * recency_mult)
    return dict(ch, entity=[x for _, x in sorted(((key(x, i), x) for i, x in enumerate(ent)), key=lambda t: t[0])])


def scaled(w: dict[str, float], f: dict[str, float]) -> dict[str, float]:
    return {n: v * f.get(n, 1.0) for n, v in w.items()}


VARIANTS = {
    "V0 current": lambda ch, it: rrf(ch, _WEIGHTS[it]),
    "V7 drop entity": lambda ch, it: rrf({n: v for n, v in ch.items() if n != "entity"}, _WEIGHTS[it]),
    "V11 entity+literal x0.5": lambda ch, it: rrf(ch, scaled(_WEIGHTS[it], {"entity": .5, "literal": .5})),
    "V14 entity by relevance (latest keeps recency)": lambda ch, it: rrf(ch if it == "latest" else entity_by_relevance(ch), _WEIGHTS[it]),
    "V15 V14 + entity x0.5": lambda ch, it: rrf(ch if it == "latest" else entity_by_relevance(ch), scaled(_WEIGHTS[it], {"entity": .5})),
    "V17 V14 + entity+literal x0.5": lambda ch, it: rrf(ch if it == "latest" else entity_by_relevance(ch), scaled(_WEIGHTS[it], {"entity": .5, "literal": .5})),
    "V18 V14 but temporal keeps recency too": lambda ch, it: rrf(ch if it in ("latest", "temporal") else entity_by_relevance(ch), _WEIGHTS[it]),
    "V19 V17 but temporal keeps recency too": lambda ch, it: rrf(ch if it in ("latest", "temporal") else entity_by_relevance(ch), scaled(_WEIGHTS[it], {"entity": .5, "literal": .5})),
    "V20 entity blend min(rel, 3*recency)": lambda ch, it: rrf(ch if it == "latest" else entity_blend(ch, 3.0), _WEIGHTS[it]),
    "V21 entity blend min(rel, 5*recency)": lambda ch, it: rrf(ch if it == "latest" else entity_blend(ch, 5.0), _WEIGHTS[it]),
    "V22 V21 + entity+literal x0.5": lambda ch, it: rrf(ch if it == "latest" else entity_blend(ch, 5.0), scaled(_WEIGHTS[it], {"entity": .5, "literal": .5})),
}


def seq_of_key(key) -> int:
    return key if isinstance(key, int) else int(str(key).split(".")[0])


def judge(order: list, rec: dict, window: int) -> dict[str, bool]:
    pos: dict[int, int] = {}
    for i, x in enumerate(order):          # 同一 seq 的几个 part 取最靠前的
        pos.setdefault(seq_of_key(x), i + 1)
    out = {}
    gold = rec["gold"]
    out["turn_all@w"] = bool(gold) and all(pos.get(s, 1 << 30) <= window for s in gold)
    out["turn_all@100"] = bool(gold) and all(pos.get(s, 1 << 30) <= 100 for s in gold)
    out["turn_any@100"] = any(pos.get(s, 1 << 30) <= 100 for s in gold)
    gs = rec.get("gold_sessions")
    if gs:
        out["sess_all@w"] = all(any(pos.get(s, 1 << 30) <= window for s in seqs) for seqs in gs.values())
        out["sess_all@100"] = all(any(pos.get(s, 1 << 30) <= 100 for s in seqs) for seqs in gs.values())
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--window", type=int, default=200)
    ap.add_argument("--split", action="store_true", help="LoCoMo 按对话分两半（conv 0-4 / 5-9）分别报，挑改法看前一半、验后一半")
    args = ap.parse_args()

    recs = [json.loads(l) for f in args.files for l in open(f, encoding="utf-8") if l.strip()]
    recs = [r for r in recs if r["gold"]]
    bad = [r for r in recs if VARIANTS["V0 current"](r["channels"], r["intent"]) != r["fused"]]
    print(f"records: {len(recs)}   V0 reproduces fused order: {len(recs) - len(bad)}/{len(recs)}")
    if bad:
        print("  mismatch on:", [(r.get('dataset'), r.get('conv', r.get('qid')), r.get('qi')) for r in bad[:10]])
    print("intents:", dict(Counter(r["intent"] for r in recs)))
    if args.split:
        for r in recs:
            if r["dataset"] == "locomo":
                r["category"] = f"{r['category']}{'a' if r['conv'] < 5 else 'b'}"
    groups = sorted({(r["dataset"], str(r["category"])) for r in recs})

    results: dict[str, dict[tuple, dict[str, bool]]] = {}
    for name, fn in VARIANTS.items():
        results[name] = {}
        for r in recs:
            key = (r["dataset"], r.get("conv", r.get("qid")), r.get("qi"))
            results[name][key] = judge(fn(r["channels"], r["intent"]), r, args.window)
    base = results["V0 current"]
    for metric in ("turn_all@w", "turn_all@100", "sess_all@w"):
        print(f"\n=== {metric.replace('@w', f'@{args.window}')} : count of questions (paired vs V0: lost/gained) ===")
        header = f"{'variant':48s}" + "".join(f"{d[:3]}/{c[:14]}".rjust(24) for d, c in groups) + f"{'ALL':>12s}"
        print(header)
        for name, per in results.items():
            cells = []
            allc = alll = allg = 0
            for d, c in groups:
                keys = [k for k, r in zip(per.keys(), recs) if (r["dataset"], str(r["category"])) == (d, c) and metric in per[k]]
                if not keys:
                    cells.append(f"{'-':>24s}"); continue
                n = sum(per[k][metric] for k in keys)
                lost = sum(base[k][metric] and not per[k][metric] for k in keys)
                gained = sum(per[k][metric] and not base[k][metric] for k in keys)
                allc += n; alll += lost; allg += gained
                cells.append(f"{n:5d}/{len(keys):<4d}({lost:2d}/{gained:2d})".rjust(24))
            print(f"{name:48s}" + "".join(cells) + f"{allc:5d} ({alll}/{allg})")

    # 各路规模：看实体路在别的数据集上到底有多大
    print("\nchannel sizes (median) by dataset:")
    by_ds: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for r in recs:
        for n, ids in r["channels"].items():
            by_ds[r["dataset"]][n].append(len(ids))
    for d, chs in by_ds.items():
        print("  ", d, {n: sorted(v)[len(v) // 2] for n, v in chs.items()})


if __name__ == "__main__":
    main()
