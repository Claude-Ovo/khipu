"""把回放用户的段从一个库原样拷到另一个库（只读源库，目标库里同名用户先删再插）。
诊断实验用：评测用的库 aml 只读，实验在 aml2 上跑。
用法：python tests/copy_replay_users.py --src ...aml --dst ...aml2 --like 'replay:bm25-v03:locomo:%'"""
from __future__ import annotations

import argparse

import psycopg
from pgvector.psycopg import register_vector

COLS = ("user_id, id, session_id, request_id, seq, part, total, role, speaker_name, ts_value, ts_granularity, "
        "ts_provenance, text, content_sha, is_rule, embedding, created_at")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--like", required=True)
    args = ap.parse_args()
    assert args.src != args.dst
    with psycopg.connect(args.src, options="-c default_transaction_read_only=on") as src, psycopg.connect(args.dst) as dst:
        register_vector(src)
        register_vector(dst)
        # 一个用户一批：LME 的 200 个用户共 12.7 万段带向量，一次全读进内存会把 4G 的机器吃光
        users = [r[0] for r in src.execute("SELECT DISTINCT user_id FROM segments WHERE user_id LIKE %s ORDER BY 1", (args.like,))]
        total = 0
        with dst.transaction():
            dst.execute("DELETE FROM segments WHERE user_id LIKE %s", (args.like,))
            with dst.cursor() as cur:
                for u in users:
                    rows = src.execute(f"SELECT {COLS} FROM segments WHERE user_id = %s ORDER BY session_id, seq, part", (u,)).fetchall()
                    cur.executemany(f"INSERT INTO segments ({COLS}) VALUES ({', '.join(['%s'] * 17)})", rows)
                    total += len(rows)
        n = dst.execute("SELECT count(*), count(embedding) FROM segments WHERE user_id LIKE %s", (args.like,)).fetchone()
    print(f"copied {total} rows for {len(users)} users; dst now has {n[0]} rows, {n[1]} with vectors")


if __name__ == "__main__":
    main()
