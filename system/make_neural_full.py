"""신경망 유사도 전량 재계산 — 세 모델의 kNN 커버리지를 같은 노드 집합으로 맞춘다.

문제:
  세 모델(graphsage/metapath2vec/node2vec)은 **같은 154,310개 조례의 임베딩**을 갖고 있는데
  neural_similarity(kNN 결과)만 서로 다른 부분집합에서 멈춰 있었다. [실측 2026-08-23]
      graphsage-numpy      154,310 건 (77.21%)
      metapath2vec-numpy    30,000 건 (15.01%)
      node2vec-numpy         3,432 건 ( 1.72%)
      세 모델 전부 가진 조례      653 건 ( 0.33%)
  원인은 build_neural_similarity(max_items=6000) 의 전쌍비교 상한이다. 상한에 뽑히지 못한
  노드에는 행 자체가 생기지 않아, 화면의 '모델 간 일치도'가 비교할 게 없어 Jaccard 0 을 그린다.
  (그 함수 docstring 이 정확히 이 함정을 경고하고 있다.)

해결:
  임베딩은 이미 전량 있으므로 **재학습 없이 kNN 만** max_items=None 으로 다시 돌린다.
  세 모델의 src 집합이 같아지므로 모델 간 일치도가 비로소 의미를 갖는다.

사용:
  python system/make_neural_full.py                 # metapath2vec + node2vec 재계산
  python system/make_neural_full.py --all           # graphsage 까지 셋 다
  python system/make_neural_full.py --top-k 10
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from policymap import db as D
from policymap.config import load_config
from policymap.neural.embeddings import (EmbeddingResult, build_neural_similarity,
                                         load_node_embeddings)

# graphsage 는 이미 154,310 전량이라 기본 대상에서 뺀다(--all 로 포함 가능).
DEFAULT_MODELS = ("metapath2vec-numpy", "node2vec-numpy")
ALL_MODELS = ("graphsage-numpy",) + DEFAULT_MODELS


def coverage(conn: sqlite3.Connection) -> dict[str, int]:
    rows = D.fetchall(
        conn,
        "SELECT model_name, COUNT(DISTINCT src_id) AS n FROM neural_similarity "
        "WHERE node_kind='Ordinance' GROUP BY model_name")
    return {r["model_name"]: r["n"] for r in rows}


def rebuild(conn: sqlite3.Connection, model: str, *, top_k: int, block: int) -> dict:
    t0 = time.time()
    emb = load_node_embeddings(conn, model, node_kind="Ordinance")
    if not emb:
        print(f"  [{model}] 임베딩 없음 — 건너뜀", flush=True)
        return {}
    nodes = list(emb)
    matrix = np.asarray([emb[n] for n in nodes], dtype=np.float32)
    print(f"  [{model}] 임베딩 {len(nodes):,}개 dim={matrix.shape[1]} "
          f"({matrix.nbytes/2**20:.0f}MB) 로드 {time.time()-t0:.0f}s", flush=True)

    result = EmbeddingResult(matrix, nodes, model_name=model)
    t1 = time.time()
    stats = build_neural_similarity(
        conn, result,
        top_k=top_k,
        kinds=("Ordinance",),
        max_items=None,        # ★ 상한 해제 — 이것이 이번 수정의 핵심이다
        replace=True,          # 같은 model_name 의 부분 결과를 먼저 지운다
        fast_insert=True,
        block=block,
    )
    print(f"  [{model}] kNN 완료 {time.time()-t1:.0f}s  {stats}", flush=True)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="graphsage 까지 셋 다 재계산")
    ap.add_argument("--model", action="append", default=None,
                    help="특정 모델만 재계산(반복 지정 가능). 한 번에 하나씩 돌리면 "
                         "중단돼도 나머지 모델의 기존 결과가 남는다.")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--block", type=int, default=512,
                    help="행 블록 크기. 512 면 블록당 약 512x154310x4B=316MB")
    args = ap.parse_args()

    cfg = load_config()
    conn = D.connect(cfg.db_path)
    conn.execute("PRAGMA busy_timeout = 120000")

    before = coverage(conn)
    print("=== 재계산 전 커버리지(조례 src 수) ===")
    for m in ALL_MODELS:
        print(f"  {m:24s} {before.get(m, 0):>9,}")

    models = tuple(args.model) if args.model else (ALL_MODELS if args.all else DEFAULT_MODELS)
    print(f"\n=== 재계산 대상: {', '.join(models)} ===", flush=True)
    t0 = time.time()
    for m in models:
        rebuild(conn, m, top_k=args.top_k, block=args.block)

    after = coverage(conn)
    print(f"\n=== 재계산 후 커버리지 (총 {time.time()-t0:.0f}s) ===")
    for m in ALL_MODELS:
        b, a = before.get(m, 0), after.get(m, 0)
        print(f"  {m:24s} {b:>9,} -> {a:>9,}")

    n3 = D.fetchone(conn,
                    "SELECT COUNT(*) AS n FROM (SELECT src_id FROM neural_similarity "
                    "WHERE node_kind='Ordinance' GROUP BY src_id "
                    "HAVING COUNT(DISTINCT model_name)=3)")["n"]
    print(f"\n  세 모델 전부 가진 조례: {n3:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
