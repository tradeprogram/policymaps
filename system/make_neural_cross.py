"""신경망 유사 조례를 '다른 지자체' 로 한정해 다시 굽는다.

문제 [실측 2026-08-23, 표본 300건]
    top-10 이웃 중 같은 지자체 비율   graphsage 72.2% · node2vec 88.7% · metapath2vec 92.4%
    10개가 전부 같은 지자체인 조례     169/300 · 266/300 · 277/300

  임베딩이 HAS_ORDINANCE(지자체→조례) 엣지에 지배돼, '유사 조례' 를 물으면
  대부분 **같은 시·군의 다른 조례** 가 돌아온다. 예: 가평군 탄소중립 조례의
  이웃이 가평군 기업유치·도로점용료·생활폐기물 조례였다.

  이 시스템의 핵심 질문은 "다른 지자체는 이걸 어떻게 만들었나" 이므로
  같은 지자체 이웃은 답이 되지 않는다(그건 지역 상세 화면이 이미 보여준다).

해결
  임베딩은 그대로 두고 kNN 후보에서 **같은 region_id 를 제외**한 뒤 다시 고른다.
  재학습은 없다. 값은 같은 코사인이고 고르는 범위만 바뀐다.

사용
    python system/make_neural_cross.py                    # 세 모델 전부
    python system/make_neural_cross.py --model node2vec-numpy
    python system/make_neural_cross.py --keep-same-region # 비교용(제외 안 함)
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
from policymap import util as _util
from policymap.config import load_config
from policymap.neural.embeddings import load_node_embeddings

ALL_MODELS = ("graphsage-numpy", "metapath2vec-numpy", "node2vec-numpy")
PREFIX = "ordinance:"


def region_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["ordinance_id"]: r["region_id"] or ""
            for r in D.fetchall(conn, "SELECT ordinance_id, region_id FROM ordinances")}


def cross_region_topk(U, reg_idx, k: int, block: int, log_every: int = 20):
    """행 블록마다 같은 region 후보를 -2 로 눌러 top-k 를 고른다.

    reg_idx: 노드별 지역 정수 id (np.int32). -1 은 지역 미상(마스킹하지 않는다).
    """
    n = U.shape[0]
    # 지역 -> 그 지역에 속한 노드 인덱스
    order = np.argsort(reg_idx, kind="stable")
    bounds = {}
    if n:
        sorted_reg = reg_idx[order]
        edges = np.flatnonzero(np.diff(sorted_reg)) + 1
        starts = np.concatenate(([0], edges))
        ends = np.concatenate((edges, [n]))
        for s, e in zip(starts, ends):
            bounds[int(sorted_reg[s])] = order[s:e]

    rows_o, cols_o, sims_o = [], [], []
    t0 = time.time()
    for bi, s in enumerate(range(0, n, block)):
        blk = np.arange(s, min(s + block, n))
        S = U[blk] @ U.T                       # (b, n) float32
        S[np.arange(len(blk)), blk] = -2.0     # 자기 자신
        # 같은 지역 마스킹 — 블록 안의 행을 지역별로 묶어 한 번에 누른다
        breg = reg_idx[blk]
        for r in np.unique(breg):
            if r < 0:
                continue
            members = bounds.get(int(r))
            if members is None or not len(members):
                continue
            rr = np.flatnonzero(breg == r)
            S[np.ix_(rr, members)] = -2.0
        kk = min(k, S.shape[1] - 1)
        part = np.argpartition(-S, kk - 1, axis=1)[:, :kk]
        take = np.take_along_axis(S, part, axis=1)
        o = np.argsort(-take, axis=1)
        part = np.take_along_axis(part, o, axis=1)
        take = np.take_along_axis(take, o, axis=1)
        keep = take > -1.5                     # 후보가 모자라 눌린 자리는 버린다
        for i in range(len(blk)):
            m = keep[i]
            if not m.any():
                continue
            rows_o.append(np.full(int(m.sum()), blk[i], dtype=np.int64))
            cols_o.append(part[i][m])
            sims_o.append(take[i][m])
        if log_every and bi % log_every == 0:
            done = min(s + block, n)
            rate = done / max(time.time() - t0, 1e-9)
            print(f"    kNN {done:,}/{n:,}  {rate:,.0f}행/s  남은 {max(n-done,0)/max(rate,1e-9)/60:.1f}분",
                  flush=True)
    if not rows_o:
        return (np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0, np.float32))
    return np.concatenate(rows_o), np.concatenate(cols_o), np.concatenate(sims_o)


# 보조 인덱스 2개. 대량 적재 전에 내렸다가 끝나고 다시 만든다.
# PK(src_id,dst_id,model_name) 는 INSERT OR REPLACE 가 쓰므로 유지해야 한다.
SECONDARY_INDEXES = {
    "ix_neursim_src": "CREATE INDEX ix_neursim_src ON neural_similarity(model_name, src_id, rank)",
    "ix_neursim_dst": "CREATE INDEX ix_neursim_dst ON neural_similarity(model_name, dst_id)",
}


def drop_secondary(conn) -> list[str]:
    """존재하는 보조 인덱스만 내리고 이름을 돌려준다."""
    have = {r["name"] for r in D.fetchall(
        conn, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='neural_similarity'")}
    dropped = []
    for name in SECONDARY_INDEXES:
        if name in have:
            conn.execute(f"DROP INDEX {name}")
            dropped.append(name)
    conn.commit()
    return dropped


def rebuild_secondary(conn, names) -> None:
    for name in names:
        conn.execute(SECONDARY_INDEXES[name])
    conn.commit()


def write(conn, model, nodes, rows, cols, sims, kind: str) -> int:
    now = _util.now_kst_iso()
    conn.execute("DELETE FROM neural_similarity WHERE model_name=? AND node_kind=?", (model, kind))
    conn.commit()
    payload, written, rank, last = [], 0, 0, -1
    for r, c_, s in zip(rows.tolist(), cols.tolist(), sims.tolist()):
        rank = 1 if r != last else rank + 1
        last = r
        payload.append((nodes[r], nodes[c_], model, round(float(s), 6), rank, kind, now))
        if len(payload) >= 50000:
            conn.executemany(
                "INSERT OR REPLACE INTO neural_similarity "
                "(src_id,dst_id,model_name,cosine_sim,rank,node_kind,computed_at) VALUES (?,?,?,?,?,?,?)",
                payload)
            conn.commit()
            written += len(payload); payload = []
    if payload:
        conn.executemany(
            "INSERT OR REPLACE INTO neural_similarity "
            "(src_id,dst_id,model_name,cosine_sim,rank,node_kind,computed_at) VALUES (?,?,?,?,?,?,?)",
            payload)
        conn.commit(); written += len(payload)
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", default=None)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--block", type=int, default=1024)
    ap.add_argument("--fast-load", action="store_true",
                    help="적재 전 보조 인덱스를 내렸다가 끝나고 재생성한다. 150만행 기준 크게 빨라진다. "
                         "다른 프로세스가 이 테이블을 읽는 중이면 쓰지 말 것")
    ap.add_argument("--keep-same-region", action="store_true",
                    help="같은 지자체를 제외하지 않는다(기존 동작. 비교용)")
    args = ap.parse_args()

    cfg = load_config()
    conn = D.connect(cfg.db_path)
    conn.execute("PRAGMA busy_timeout = 300000")
    conn.execute("PRAGMA synchronous = NORMAL")

    rmap = region_map(conn)
    models = tuple(args.model) if args.model else ALL_MODELS
    for model in models:
        t0 = time.time()
        emb = load_node_embeddings(conn, model, node_kind="Ordinance")
        if not emb:
            print(f"[{model}] 임베딩 없음 — 건너뜀", flush=True); continue
        nodes = list(emb)
        U = np.asarray([emb[n] for n in nodes], dtype=np.float32)
        U /= (np.linalg.norm(U, axis=1, keepdims=True) + 1e-12)

        rid_of = {}
        reg_idx = np.full(len(nodes), -1, dtype=np.int32)
        if not args.keep_same_region:
            for i, nid in enumerate(nodes):
                rid = rmap.get(nid[len(PREFIX):] if nid.startswith(PREFIX) else nid) or ""
                if rid:
                    reg_idx[i] = rid_of.setdefault(rid, len(rid_of))
        print(f"[{model}] 노드 {len(nodes):,} dim={U.shape[1]} 지역 {len(rid_of):,}곳 "
              f"로드 {time.time()-t0:.0f}s", flush=True)

        t1 = time.time()
        rows, cols, sims = cross_region_topk(U, reg_idx, args.top_k, args.block)
        print(f"[{model}] kNN {time.time()-t1:.0f}s → {len(rows):,}쌍", flush=True)
        t2 = time.time()
        dropped = []
        if args.fast_load:
            dropped = drop_secondary(conn)
            if dropped:
                print(f"[{model}] 보조 인덱스 {len(dropped)}개 내림 — 적재 후 재생성", flush=True)
        try:
            n = write(conn, model, nodes, rows, cols, sims, "Ordinance")
        finally:
            if dropped:
                t3 = time.time()
                rebuild_secondary(conn, dropped)
                print(f"[{model}] 인덱스 재생성 {time.time()-t3:.0f}s", flush=True)
        print(f"[{model}] 적재 {n:,}행 {time.time()-t2:.0f}s (총 {time.time()-t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
