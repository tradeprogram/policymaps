#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""실 DB(node_embeddings / neural_similarity) → **신경망 시각화 shard** 생성.

어제 학습한 그래프 임베딩 3종(node2vec / metapath2vec / graphsage)이 DB 에만 있고
화면에는 전혀 안 붙어 있었다. 이 스크립트가 그 간극을 메운다.

출력(기본 out = system/data):
    api/neural/ordinance/{ordin-MST}.json  대표 조례별 유사 조례 Top-K (모델 3종 비교)
    api/neural/region/{sig_cd}.json        지자체별 유사 지자체 Top-K (243곳, 모델 3종 비교)
    api/neural/index.json                  커버 목록 + 모델 메타
    api/neural/quality.json                모델 품질 지표(변별력·분리도·모델간 일치도)

설계 결정(중요):
  * **유사 이웃은 node_embeddings 에서 재계산한다.** neural_similarity 에 저장된
    Top-10 은 모델별 커버리지가 제각각이라(실측: graphsage 154,310 src / metapath2vec
    30,000 / node2vec 3,432) 저장본만으로는 3모델 비교가 불가능하다. 재계산은
    후보를 전체 조례 154,310건으로 두므로 커버리지 편향이 없다.
  * 저장본은 버리지 않는다. 재계산 Top-K 와 겹치는 이웃에 stored_rank 를 달고,
    모델별 `stored_agreement`(저장본 Top-10 ∩ 재계산 Top-10 / 10)를 기록한다.
    저장 테이블이 임베딩과 실제로 정합함을 화면에서 보일 근거다.
  * 이웃은 id 만 주지 않는다 — 조례명·지자체명·제정일·원문링크(canonical_url, 키 없음)
    를 붙여 사람이 판단할 수 있게 한다.
  * **폐지 조례는 status/repealed_on 을 명시**한다(선례 오인 방지). 표기 규율상
    신경망 유사도는 '선례 추천'이 아니라 '탐색 보조'이며, 봉투 disclaimer 에 못박는다.

엔진 재사용(중복 구현 금지):
  * 봉투            make_gap_fixtures.envelope
  * 키 살균·원자쓰기 make_more_fixtures._sanitize_keys / make_nationwide.write_json
  * kNN·디코딩       policymap.neural.embeddings.top_k_matrix / decode_vector
  * AUC             policymap.neural.gnn.roc_auc

재개 가능: 이미 만들어진 shard 는 건너뛴다(--force 로 재생성). 원자적 쓰기.

사용:
  cd system
  python make_neural_fixtures.py --limit 20        # 소규모 시험(스키마·용량 확인)
  python make_neural_fixtures.py                   # 대표 조례 400건 + 전국 243곳
  python make_neural_fixtures.py --only quality --force
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:  # 콘솔이 cp949 여도 한글 출력에서 죽지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import numpy as np                                          # noqa: E402

from policymap import db as D                               # noqa: E402
from policymap.config import get_config                     # noqa: E402
from policymap.neural import embeddings as E                # noqa: E402
from policymap.neural import gnn as GNN                     # noqa: E402

# 중복 구현 금지 — 봉투·살균·원자쓰기는 기존 생성기 것을 그대로 쓴다.
from make_gap_fixtures import envelope                      # noqa: E402
from make_nationwide import existing, human, write_json     # noqa: E402

KINDS = ("ordinance", "region", "quality")
SEED = 20260822

NEURAL_DISCLAIMER = (
    "그래프 신경망 임베딩의 코사인 유사도다. 조문 의미가 아니라 그래프 구조"
    "(같은 지자체·같은 분류·같은 상위법·같은 재정사업으로 이어지는지)를 학습한 값이므로, "
    "'선례 추천'이 아니라 '탐색 보조'로만 쓸 것. 폐지 조례(status=repealed)는 선례로 "
    "인용하지 말 것. 인용 전 원문 링크로 확인할 것."
)


# --------------------------------------------------------------------------- #
# 공통 유틸
# --------------------------------------------------------------------------- #
def safe_key(ordinance_id: str) -> str:
    """'ordin:1005446' → 'ordin-1005446'. ':' 는 Windows 파일명·URL 에서 위험하다."""
    return str(ordinance_id).replace(":", "-")


def connect_ro(db_path: str) -> "object":
    """**읽기 전용** 연결.

    db.connect() 는 쓰기 모드로 열고 `PRAGMA journal_mode=WAL` 을 건다. 수집·분석
    에이전트가 동시에 돌고 있으면 그 PRAGMA 가 락을 기다리다 멈춘다(실측: 5분 무응답).
    이 스크립트는 DB 를 읽기만 하므로 mode=ro 로 열어 그 교착을 원천 차단한다.
    row_factory 는 db.fetchall(dict(r)) 가 요구하는 sqlite3.Row 로 맞춘다.
    """
    import sqlite3
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    return conn


def chunked(seq, n: int = 900):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def decode_matrix(rows) -> np.ndarray:
    """[(node_id, vector_b64)] → float32 [N, dim]. base64 는 통짜 디코드가 훨씬 빠르다."""
    if not rows:
        return np.zeros((0, 0), dtype=np.float32)
    raw = b"".join(base64.b64decode(r[1]) for r in rows)
    return np.frombuffer(raw, dtype="<f4").reshape(len(rows), -1)


def unit(V: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(V, axis=1, keepdims=True)
    return V / np.maximum(n, 1e-12)


# --------------------------------------------------------------------------- #
# 모델 메타
# --------------------------------------------------------------------------- #
def model_meta(conn) -> list[dict]:
    """node_embeddings / neural_similarity 를 훑어 모델별 메타를 만든다."""
    emb = D.fetchall(conn,
                     "SELECT model_name, node_kind, COUNT(*) AS n, MAX(dim) AS dim, "
                     "MAX(computed_at) AS computed_at, MAX(encoding) AS encoding "
                     "FROM node_embeddings GROUP BY model_name, node_kind")
    sim = D.fetchall(conn,
                     "SELECT model_name, COUNT(*) AS n, MAX(computed_at) AS computed_at "
                     "FROM neural_similarity GROUP BY model_name")
    sim_by = {r["model_name"]: r for r in sim}
    out: dict[str, dict] = {}
    for r in emb:
        m = out.setdefault(r["model_name"], {
            "model_name": r["model_name"], "dim": None, "encoding": None,
            "nodes_total": 0, "nodes_by_kind": {}, "computed_at": None,
            "similarity_rows": (sim_by.get(r["model_name"]) or {}).get("n", 0),
            "similarity_computed_at": (sim_by.get(r["model_name"]) or {}).get("computed_at"),
            "algorithm": _algo_of(r["model_name"]),
        })
        m["nodes_by_kind"][r["node_kind"]] = int(r["n"])
        m["nodes_total"] += int(r["n"])
        m["dim"] = m["dim"] or r.get("dim")
        m["encoding"] = m["encoding"] or r.get("encoding")
        if r.get("computed_at") and (m["computed_at"] or "") < r["computed_at"]:
            m["computed_at"] = r["computed_at"]
    return [out[k] for k in sorted(out)]


def _algo_of(model_name: str) -> dict:
    """모델명 → 알고리즘 설명(엔진 코드의 실제 구현을 그대로 옮긴다)."""
    if model_name.startswith("graphsage"):
        return {"family": "GraphSAGE", "supervision": "unsupervised (link-based)",
                "impl": "policymap.neural.gnn (numpy 수동 미분)",
                "note": "이웃 집계(mean/pool)로 노드 표현 학습. 학습 시 held-out 엣지를 "
                        "메시지패싱에서 제거해 누수를 막는다."}
    if model_name.startswith("metapath2vec"):
        return {"family": "metapath2vec", "supervision": "unsupervised (SGNS)",
                "impl": "policymap.neural.embeddings (numpy 수동 미분)",
                "note": "이종그래프 메타패스(R-O-R, O-I-O, O-B-O, O-C-O, O-R-O 등)로 "
                        "워크를 강제해 타입 의미를 보존한다."}
    if model_name.startswith("node2vec") or model_name.startswith("deepwalk"):
        return {"family": "node2vec/DeepWalk", "supervision": "unsupervised (SGNS)",
                "impl": "policymap.neural.embeddings (numpy 수동 미분)",
                "note": "2차 랜덤워크 + skip-gram negative sampling. p=q=1 이면 DeepWalk."}
    return {"family": model_name, "supervision": "unknown", "impl": None, "note": None}


# --------------------------------------------------------------------------- #
# 임베딩 적재
# --------------------------------------------------------------------------- #
def load_kind(conn, model: str, kind: str, log=print) -> tuple[list[str], np.ndarray]:
    """model×kind 의 전체 임베딩을 (node_ids, matrix) 로 적재."""
    t0 = time.time()
    rows = conn.execute(
        "SELECT node_id, vector FROM node_embeddings "
        "WHERE model_name=? AND node_kind=? ORDER BY node_id", (model, kind)).fetchall()
    V = decode_matrix(rows)
    ids = [r[0] for r in rows]
    log(f"    {model} / {kind}: {len(ids)}개 × {V.shape[1] if V.size else 0}차원 "
        f"({V.nbytes / 1e6:.0f} MB, {time.time() - t0:.1f}s)")
    return ids, V


def load_by_ids(conn, model: str, node_ids: list[str]) -> dict[str, np.ndarray]:
    """지정 node_id 들의 벡터만 조회(PK 인덱스). {node_id: vec}."""
    out: dict[str, np.ndarray] = {}
    for part in chunked(node_ids):
        ph = ",".join("?" for _ in part)
        for nid, vec in conn.execute(
                f"SELECT node_id, vector FROM node_embeddings "
                f"WHERE model_name=? AND node_id IN ({ph})", [model, *part]):
            out[nid] = np.frombuffer(base64.b64decode(vec), dtype="<f4")
    return out


# --------------------------------------------------------------------------- #
# 메타데이터
# --------------------------------------------------------------------------- #
def region_index(conn) -> dict[str, dict]:
    """region_id → 지자체 메타."""
    return {r["region_id"]: r for r in D.fetchall(
        conn, "SELECT region_id, sig_cd, name, full_name, level, status, "
              "has_legislation FROM regions")}


def ordinance_meta(conn, ord_ids: list[str], regions: dict[str, dict]) -> dict[str, dict]:
    """ordinance_id 집합 → 표시용 메타(조례명·지자체명·제정일·원문링크·폐지여부)."""
    out: dict[str, dict] = {}
    for part in chunked(sorted(set(ord_ids))):
        ph = ",".join("?" for _ in part)
        for r in D.fetchall(conn,
                            f"SELECT ordinance_id, mst, region_id, org_name, name, ord_kind, "
                            f"enacted_on, effective_on, repealed_on, rr_cls_cd, status, "
                            f"lifecycle, article_count, canonical_url, official_url, "
                            f"verification_status FROM ordinances "
                            f"WHERE ordinance_id IN ({ph})", part):
            reg = regions.get(r.get("region_id") or "") or {}
            out[r["ordinance_id"]] = {
                "ordinance_id": r["ordinance_id"],
                "key": safe_key(r["ordinance_id"]),
                "mst": r.get("mst"),
                "name": r.get("name"),
                "ord_kind": r.get("ord_kind"),
                "region_id": r.get("region_id"),
                "sig_cd": reg.get("sig_cd"),
                "region_name": reg.get("full_name") or reg.get("name") or r.get("org_name"),
                "org_name": r.get("org_name"),
                "enacted_on": r.get("enacted_on"),
                "effective_on": r.get("effective_on"),
                "repealed_on": r.get("repealed_on"),
                "rr_cls_cd": r.get("rr_cls_cd"),
                "article_count": r.get("article_count"),
                "status": r.get("status"),
                "lifecycle": r.get("lifecycle"),
                "verification_status": r.get("verification_status"),
                # 원문링크: canonical_url(키 없는 영구 URL) 우선, 없으면 official_url.
                # official_url 은 OC 키가 섞여 있을 수 있으나 write_json 의 _sanitize_keys 가 걷어낸다.
                "url": r.get("canonical_url") or r.get("official_url"),
            }
    return out


def category_map(conn, ord_ids: list[str]) -> dict[str, list[dict]]:
    """ordinance_id → [{code, name, confidence, method}]."""
    names = {r["code"]: r["name"] for r in D.fetchall(conn, "SELECT code, name FROM categories")}
    out: dict[str, list[dict]] = {}
    for part in chunked(sorted(set(ord_ids))):
        ph = ",".join("?" for _ in part)
        for r in D.fetchall(conn,
                            f"SELECT ordinance_id, category_code, confidence, method "
                            f"FROM ordinance_category WHERE ordinance_id IN ({ph})", part):
            out.setdefault(r["ordinance_id"], []).append({
                "code": r["category_code"], "name": names.get(r["category_code"]),
                "confidence": r.get("confidence"), "method": r.get("method")})
    for v in out.values():
        v.sort(key=lambda c: -(c.get("confidence") or 0))
    return out


def stored_topk(conn, node_id: str, model: str, k: int = 10) -> list[dict]:
    """neural_similarity 저장본 Top-k(인덱스 ix_neursim_src 사용)."""
    return D.fetchall(conn,
                      "SELECT dst_id, cosine_sim, rank FROM neural_similarity "
                      "WHERE model_name=? AND src_id=? ORDER BY rank LIMIT ?",
                      (model, node_id, int(k)))


# --------------------------------------------------------------------------- #
# 대표 조례 선정
# --------------------------------------------------------------------------- #
def gap_hits(api_root: Path) -> dict[str, int]:
    """이미 만들어 둔 gap shard 에 등장하는 조례 id → 등장 횟수.

    격차분석 화면에서 실제로 보이는 조례를 우선 커버해야 화면 간 이동이 끊기지 않는다.
    """
    hits: dict[str, int] = {}
    gap_dir = api_root / "gap"
    if not gap_dir.is_dir():
        return hits
    for f in sorted(gap_dir.glob("*.json")):
        try:
            env = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for res in (env.get("data") or {}).values():
            for rec in (res or {}).get("recommendations") or []:
                for p in rec.get("peers") or []:
                    oid = p.get("ordinance_id")
                    if oid:
                        hits[oid] = hits.get(oid, 0) + 1
    return hits


def pick_targets(conn, covered: set[str], api_root: Path, n_target: int,
                 log=print) -> tuple[list[str], dict[str, int]]:
    """대표 조례 선정: gap 등장 조례 우선 + 카테고리별 고르게.

    covered: 임베딩이 존재하는 ordinance_id 집합(임베딩 없는 조례는 화면을 못 만든다).
    """
    hits = gap_hits(api_root)
    ranked = [o for o, _ in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))
              if o in covered]
    log(f"  gap shard 등장 조례 {len(hits)}건 중 임베딩 보유 {len(ranked)}건")

    # 카테고리별 후보(현행 우선 → 그다음 폐지). 결정적 순서를 위해 ordinance_id 정렬.
    by_cat: dict[str, list[str]] = {}
    for r in D.fetchall(conn,
                        "SELECT oc.category_code AS c, oc.ordinance_id AS o, o.status AS s "
                        "FROM ordinance_category oc JOIN ordinances o "
                        "ON o.ordinance_id = oc.ordinance_id "
                        "ORDER BY oc.category_code, (o.status='active') DESC, oc.ordinance_id"):
        if r["o"] in covered:
            by_cat.setdefault(r["c"], []).append(r["o"])

    chosen: list[str] = []
    seen: set[str] = set()
    # 1) gap 우선(전체의 절반까지)
    for o in ranked[: max(1, n_target // 2)]:
        if o not in seen:
            seen.add(o)
            chosen.append(o)
    # 2) 카테고리 라운드로빈으로 채운다
    cats = sorted(by_cat)
    pos = {c: 0 for c in cats}
    while len(chosen) < n_target and cats:
        progressed = False
        for c in list(cats):
            if len(chosen) >= n_target:
                break
            lst = by_cat[c]
            i = pos[c]
            while i < len(lst) and lst[i] in seen:
                i += 1
            pos[c] = i + 1
            if i >= len(lst):
                cats.remove(c)
                continue
            seen.add(lst[i])
            chosen.append(lst[i])
            progressed = True
        if not progressed:
            break
    # 3) 그래도 모자라면 임베딩 보유분에서 결정적으로 채운다
    if len(chosen) < n_target:
        for o in sorted(covered):
            if len(chosen) >= n_target:
                break
            if o not in seen:
                seen.add(o)
                chosen.append(o)
    return chosen, hits


# --------------------------------------------------------------------------- #
# 이웃 계산
# --------------------------------------------------------------------------- #
def neighbors_for(U: np.ndarray, ids: list[str], idx_map: dict[str, int],
                  targets: list[str], cand_idx: np.ndarray, k: int,
                  block: int = 128) -> dict[str, list[tuple[str, float]]]:
    """대상별 Top-k 이웃. embeddings.top_k_matrix(블록 행렬곱) 재사용."""
    tidx = np.array([idx_map[t] for t in targets if t in idx_map], dtype=np.int64)
    tlist = [t for t in targets if t in idx_map]
    if tidx.size == 0:
        return {}
    order = np.argsort(tidx, kind="stable")     # top_k_matrix 는 정렬 전제가 아니지만
    tidx_s = tidx[order]                        # 결과 매핑을 명확히 하려고 정렬해 둔다
    tlist_s = [tlist[i] for i in order.tolist()]
    rows, cols, sims = E.top_k_matrix(U, tidx_s, cand_idx, k, block=block)
    out: dict[str, list[tuple[str, float]]] = {t: [] for t in tlist_s}
    pos_of = {int(v): tlist_s[i] for i, v in enumerate(tidx_s.tolist())}
    for r, c, s in zip(rows.tolist(), cols.tolist(), sims.tolist()):
        t = pos_of.get(int(r))
        if t is not None and s > -1.5:
            out[t].append((ids[int(c)], float(s)))
    return out


def merge_stored(conn, src_node: str, model: str, recomputed: list[tuple[str, float]],
                 k: int) -> tuple[list[dict], dict]:
    """재계산 Top-k 에 저장본(neural_similarity) rank 를 붙이고 일치도를 잰다."""
    stored = stored_topk(conn, src_node, model, k)
    srank = {r["dst_id"]: int(r["rank"]) for r in stored}
    items = []
    for i, (nid, cos) in enumerate(recomputed, start=1):
        items.append({"rank": i, "node_id": nid, "cosine": round(cos, 6),
                      "stored_rank": srank.get(nid)})
    inter = len(set(srank) & {n for n, _ in recomputed})
    info = {
        "stored_covered": bool(stored),
        "stored_rows": len(stored),
        "stored_overlap": inter,
        "stored_agreement": round(inter / len(stored), 4) if stored else None,
    }
    return items, info


def agreement_matrix(per_model: dict[str, list[dict]]) -> dict:
    """모델쌍별 Top-k 겹침(overlap/jaccard). 모델이 서로 다른 답을 내는지 보인다."""
    out = {}
    names = sorted(per_model)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            sa = {x["node_id"] for x in per_model[a]}
            sb = {x["node_id"] for x in per_model[b]}
            if not sa or not sb:
                continue
            inter = len(sa & sb)
            out[f"{a}|{b}"] = {
                "overlap": inter,
                "jaccard": round(inter / len(sa | sb), 4),
                "of": min(len(sa), len(sb)),
            }
    return out


def _enrich_consensus(c: dict, prefix: str, meta: dict, fields, *,
                      id_field: str | None = None) -> dict:
    """consensus 항목에 표시용 메타를 붙인다(id 만 주면 화면에서 판단할 수 없다)."""
    ident = c["node_id"].split(prefix, 1)[-1]
    m = meta.get(ident) or {}
    out = dict(c)
    out[id_field or "ordinance_id"] = ident
    for f in fields:
        if f in m:
            out[f] = m[f]
    if "full_name" in out and not out.get("name"):
        out["name"] = out["full_name"]
    return out


def consensus_of(per_model: dict[str, list[dict]]) -> list[dict]:
    """2개 이상 모델이 공통으로 뽑은 이웃(평균 코사인 내림차순)."""
    agg: dict[str, dict] = {}
    for m, items in per_model.items():
        for it in items:
            a = agg.setdefault(it["node_id"], {"node_id": it["node_id"], "models": [],
                                               "cosines": [], "ranks": []})
            a["models"].append(m)
            a["cosines"].append(it["cosine"])
            a["ranks"].append(it["rank"])
    out = []
    for a in agg.values():
        if len(a["models"]) < 2:
            continue
        out.append({"node_id": a["node_id"], "models": sorted(a["models"]),
                    "model_count": len(a["models"]),
                    "mean_cosine": round(float(np.mean(a["cosines"])), 6),
                    "best_rank": min(a["ranks"])})
    out.sort(key=lambda x: (-x["model_count"], x["best_rank"]))
    return out


# --------------------------------------------------------------------------- #
# 조례 shard
# --------------------------------------------------------------------------- #
def ordinance_shards(conn, out_dir: Path, args, report, log=print) -> None:
    if "ordinance" not in args.only:
        return
    models = args.models
    log("[조례] 임베딩 적재")
    loaded: dict[str, tuple[list[str], dict[str, int], np.ndarray]] = {}
    for m in models:
        ids, V = load_kind(conn, m, "Ordinance", log=log)
        if not ids:
            report["warnings"].append({"kind": "ordinance", "model": m,
                                       "message": "Ordinance 임베딩 없음 — 이 모델은 건너뛴다"})
            continue
        loaded[m] = (ids, {n: i for i, n in enumerate(ids)}, unit(V))
    if not loaded:
        report["errors"].append({"kind": "ordinance", "error": "Ordinance 임베딩이 하나도 없다"})
        log("  [FAIL] Ordinance 임베딩 없음")
        return

    # 전 모델이 공통으로 가진 조례만 대상으로 삼는다(모델 비교의 전제).
    covered = set.intersection(*[set(v[0]) for v in loaded.values()])
    covered_ord = {n.split("ordinance:", 1)[1] for n in covered if n.startswith("ordinance:")}
    log(f"  전 모델 공통 조례 노드 {len(covered_ord)}건")

    # 후보 pool 의 현행/폐지 구성을 실측한다. 화면에서 "폐지 조례가 이웃으로 뜰 수 있는가"를
    # 추측이 아니라 수치로 답해야 한다(표기 규율: 폐지 조례 선례 추천 금지).
    # node_embeddings 와 ordinances 를 SQL 로 조인하면 5분 넘게 걸린다(실측 340s).
    # status 별 id 집합을 인덱스(ix_ord_status)로 뽑아 파이썬에서 교집합하면 몇 초다.
    pool_status: dict[str, int] = {}
    seen_pool = 0
    for st in [r["status"] for r in D.fetchall(
            conn, "SELECT DISTINCT status FROM ordinances")]:
        ids = {r["ordinance_id"] for r in D.fetchall(
            conn, "SELECT ordinance_id FROM ordinances WHERE status IS ?", (st,))}
        n = len(ids & covered_ord)
        if n:
            pool_status[st or "(null)"] = n
            seen_pool += n
    if seen_pool < len(covered_ord):
        pool_status["(unmatched)"] = len(covered_ord) - seen_pool
    n_repealed_pool = int(pool_status.get("repealed", 0))
    report["pool_status"] = pool_status
    log(f"  후보 pool 상태 구성 {pool_status}")

    targets_ord, hits = pick_targets(conn, covered_ord, out_dir.parent, args.targets, log=log)
    if args.limit:
        targets_ord = targets_ord[: args.limit]
    log(f"  대상 조례 {len(targets_ord)}건 (목표 {args.targets}, limit={args.limit or '-'})")

    target_nodes = [f"ordinance:{o}" for o in targets_ord]
    per_model_neighbors: dict[str, dict[str, list[tuple[str, float]]]] = {}
    for m, (ids, imap, U) in loaded.items():
        t0 = time.time()
        cand = np.arange(len(ids), dtype=np.int64)
        per_model_neighbors[m] = neighbors_for(U, ids, imap, target_nodes, cand,
                                               args.k, block=args.block)
        log(f"    {m} kNN 계산 {time.time() - t0:.1f}s")

    # 표시 메타 일괄 조회
    regions = region_index(conn)
    need = set(targets_ord)
    for m, mp in per_model_neighbors.items():
        for lst in mp.values():
            for nid, _ in lst:
                if nid.startswith("ordinance:"):
                    need.add(nid.split("ordinance:", 1)[1])
    meta = ordinance_meta(conn, sorted(need), regions)
    cats = category_map(conn, sorted(need))
    log(f"  메타 조회 {len(meta)}건 / 분류 {len(cats)}건")

    made = reused = failed = 0
    total_bytes = 0
    repealed_total = 0
    for i, oid in enumerate(targets_ord, 1):
        key = safe_key(oid)
        path = out_dir / "ordinance" / f"{key}.json"
        rel = f"ordinance/{key}.json"
        size = None if args.force else existing(path)
        base = dict(meta.get(oid) or {"ordinance_id": oid, "key": key})
        base["categories"] = cats.get(oid, [])
        entry = {"key": key, "ordinance_id": oid, "name": base.get("name"),
                 "region_name": base.get("region_name"), "sig_cd": base.get("sig_cd"),
                 "status": base.get("status"),
                 "categories": [c["code"] for c in base["categories"]],
                 "gap_hits": hits.get(oid, 0), "path": rel}
        if size is not None:
            entry.update({"bytes": size, "reused": True})
            report["ordinances"].append(entry)
            reused += 1
            total_bytes += size
            continue

        src_node = f"ordinance:{oid}"
        models_out: dict[str, dict] = {}
        flat: dict[str, list[dict]] = {}
        n_repealed = 0
        for m in loaded:
            rec = (per_model_neighbors.get(m) or {}).get(src_node) or []
            items, info = merge_stored(conn, src_node, m, rec, args.k)
            rich = []
            for it in items:
                nid = it["node_id"]
                oo = nid.split("ordinance:", 1)[1] if nid.startswith("ordinance:") else nid
                mm = meta.get(oo) or {}
                repealed = (mm.get("status") == "repealed") or bool(mm.get("repealed_on"))
                if repealed:
                    n_repealed += 1
                rich.append({
                    "rank": it["rank"], "cosine": it["cosine"],
                    "stored_rank": it["stored_rank"],
                    "ordinance_id": oo, "key": safe_key(oo),
                    "name": mm.get("name"), "ord_kind": mm.get("ord_kind"),
                    "region_name": mm.get("region_name"), "sig_cd": mm.get("sig_cd"),
                    "enacted_on": mm.get("enacted_on"),
                    "repealed_on": mm.get("repealed_on"),
                    "status": mm.get("status"), "lifecycle": mm.get("lifecycle"),
                    "repealed": repealed,
                    "verification_status": mm.get("verification_status"),
                    "url": mm.get("url"),
                    "categories": [c["code"] for c in cats.get(oo, [])],
                })
            models_out[m] = {
                "model_name": m, "dim": int(loaded[m][2].shape[1]),
                "candidate_pool": int(len(loaded[m][0])),
                "neighbors": rich, **info,
            }
            flat[m] = items

        env = envelope({
            "ordinance": base,
            "models": models_out,
            "model_agreement": agreement_matrix(flat),
            "consensus": [
                _enrich_consensus(c, "ordinance:", meta, (
                    "ordinance_id", "key", "name", "region_name", "sig_cd",
                    "enacted_on", "status", "repealed_on", "url"))
                for c in consensus_of(flat)
            ],
            "method": {
                "top_k": args.k,
                "similarity": "cosine(node_embeddings)",
                "recomputed_over": "전체 Ordinance 노드(모델별 candidate_pool)",
                "why_recomputed": "neural_similarity 저장본은 모델별 커버리지가 달라 "
                                  "3모델 비교가 불가능하다. 저장본은 stored_rank/"
                                  "stored_agreement 로 대조만 한다.",
                "candidate_pool_status": pool_status,
                "repealed_in_pool": n_repealed_pool,
                "repealed_note": (
                    "후보 pool 에 폐지 조례가 0건이면 폐지 조례가 이웃으로 뜨는 일 자체가 "
                    "구조적으로 불가능하다(학습 그래프에 현행 조례만 들어갔다는 뜻). "
                    "0건이 아니면 각 이웃의 repealed/status 를 반드시 화면에 표기할 것."
                    if n_repealed_pool == 0 else
                    "후보 pool 에 폐지 조례가 포함돼 있다. 각 이웃의 repealed/status/"
                    "repealed_on 을 반드시 화면에 표기하고 선례로 추천하지 말 것."),
            },
        }, neural=True, models=sorted(models_out))
        env["disclaimer"] = NEURAL_DISCLAIMER
        try:
            size = write_json(path, env)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {str(e)[:120]}"
            report["errors"].append({"kind": "ordinance", "ordinance_id": oid, "error": msg})
            failed += 1
            log(f"  [{i}/{len(targets_ord)}] {key} FAIL {msg}")
            continue
        made += 1
        total_bytes += size
        repealed_total += n_repealed
        entry.update({"bytes": size, "reused": False, "repealed_neighbors": n_repealed})
        report["ordinances"].append(entry)
        if i % 25 == 0 or i == len(targets_ord):
            log(f"  [{i}/{len(targets_ord)}] {key} · {human(size)} "
                f"· 생성 {made} 재사용 {reused} 실패 {failed}")
    cat_hist: dict[str, int] = {}
    for e in report["ordinances"]:
        for cc in (e.get("categories") or ["(none)"]):
            cat_hist[cc] = cat_hist.get(cc, 0) + 1
    report["target_selection"] = {
        "targets": len(targets_ord),
        "from_gap_shards": sum(1 for e in report["ordinances"] if e.get("gap_hits")),
        "gap_pool": len(hits),
        "category_histogram": dict(sorted(cat_hist.items())),
        "distinct_sig_cd": len({e.get("sig_cd") for e in report["ordinances"]
                                if e.get("sig_cd")}),
        "rule": "gap shard 등장 조례를 등장횟수 내림차순으로 절반까지 채우고, 나머지는 "
                "분류(ordinance_category) 라운드로빈으로 고르게 채운다.",
    }
    report["totals"]["ordinance"] = {
        "files": made + reused, "made": made, "reused": reused, "failed": failed,
        "bytes": total_bytes, "repealed_neighbors": repealed_total,
    }
    log(f"[조례] 생성 {made} · 재사용 {reused} · 실패 {failed} · {human(total_bytes)}")


# --------------------------------------------------------------------------- #
# 지자체 shard
# --------------------------------------------------------------------------- #
def region_shards(conn, out_dir: Path, args, report, log=print) -> None:
    if "region" not in args.only:
        return
    regions = region_index(conn)
    targets = [r for r in D.fetchall(
        conn, "SELECT region_id, sig_cd, name, full_name, level FROM regions "
              "WHERE status='active' AND has_legislation=1 AND level IN (1,2) "
              "ORDER BY level, sig_cd")]
    if args.limit:
        targets = targets[: args.limit]
    log(f"[지자체] 대상 {len(targets)}곳")

    loaded: dict[str, tuple[list[str], dict[str, int], np.ndarray]] = {}
    for m in args.models:
        ids, V = load_kind(conn, m, "Region", log=log)
        if not ids:
            report["warnings"].append({"kind": "region", "model": m,
                                       "message": "Region 임베딩 없음 — 이 모델은 건너뛴다"})
            continue
        loaded[m] = (ids, {n: i for i, n in enumerate(ids)}, unit(V))
    if not loaded:
        report["errors"].append({"kind": "region", "error": "Region 임베딩이 하나도 없다"})
        return

    # 후보 pool 은 **같은 레벨**로 제한한다(광역과 기초를 섞으면 비교가 무의미하다).
    by_level: dict[int, list[dict]] = {}
    for r in D.fetchall(conn, "SELECT region_id, sig_cd, name, full_name, level FROM regions "
                              "WHERE status='active' AND has_legislation=1 AND level IN (1,2)"):
        by_level.setdefault(int(r["level"]), []).append(r)

    per_model: dict[str, dict[str, list[tuple[str, float]]]] = {m: {} for m in loaded}
    for lvl, pool in by_level.items():
        pool_nodes = [f"region:{r['region_id']}" for r in pool]
        tgt_nodes = [f"region:{r['region_id']}" for r in targets if int(r["level"]) == lvl]
        if not tgt_nodes:
            continue
        for m, (ids, imap, U) in loaded.items():
            cand = np.array(sorted(imap[n] for n in pool_nodes if n in imap), dtype=np.int64)
            if cand.size == 0:
                continue
            per_model[m].update(
                neighbors_for(U, ids, imap, tgt_nodes, cand, args.k, block=args.block))

    made = reused = failed = 0
    total_bytes = 0
    for i, r in enumerate(targets, 1):
        sig = r["sig_cd"]
        path = out_dir / "region" / f"{sig}.json"
        rel = f"region/{sig}.json"
        size = None if args.force else existing(path)
        entry = {"sig_cd": sig, "region_id": r["region_id"],
                 "name": r.get("full_name") or r.get("name"),
                 "level": int(r["level"]), "path": rel}
        if size is not None:
            entry.update({"bytes": size, "reused": True})
            report["regions"].append(entry)
            reused += 1
            total_bytes += size
            continue

        src_node = f"region:{r['region_id']}"
        models_out: dict[str, dict] = {}
        flat: dict[str, list[dict]] = {}
        for m in loaded:
            rec = (per_model.get(m) or {}).get(src_node) or []
            items, info = merge_stored(conn, src_node, m, rec, args.k)
            rich = []
            for it in items:
                rid = it["node_id"].split("region:", 1)[-1]
                rm = regions.get(rid) or {}
                rich.append({
                    "rank": it["rank"], "cosine": it["cosine"],
                    "stored_rank": it["stored_rank"],
                    "region_id": rid, "sig_cd": rm.get("sig_cd"),
                    "name": rm.get("full_name") or rm.get("name"),
                    "level": rm.get("level"),
                })
            models_out[m] = {
                "model_name": m, "dim": int(loaded[m][2].shape[1]),
                "candidate_pool": len(by_level.get(int(r["level"]), [])),
                "stored_note": (
                    "neural_similarity 저장본은 전체 Region 노드 537개(레벨3·비활성 포함)를 "
                    "후보로 계산됐다. 여기 재계산본은 같은 레벨의 활성 지자체만 후보로 두므로 "
                    "stored_agreement 가 낮아도 불일치가 아니라 후보집합 차이다."),
                "neighbors": rich, **info}
            flat[m] = items

        env = envelope({
            "region": {"region_id": r["region_id"], "sig_cd": sig,
                       "name": r.get("name"),
                       "full_name": r.get("full_name"), "level": int(r["level"])},
            "models": models_out,
            "model_agreement": agreement_matrix(flat),
            "consensus": [
                _enrich_consensus(c, "region:", regions, ("sig_cd", "name", "full_name",
                                                          "level"), id_field="region_id")
                for c in consensus_of(flat)
            ],
            "method": {
                "top_k": args.k, "similarity": "cosine(node_embeddings)",
                "candidate_restriction": f"같은 level({int(r['level'])}) · status=active · "
                                         "has_legislation=1 지자체로 제한",
                "note": "지역특성(analytics.peers)의 유사지자체와는 다른 축이다. "
                        "이쪽은 그래프 구조(조례·예산·인접·분류 연결)를 학습한 임베딩이다.",
            },
        }, neural=True, models=sorted(models_out))
        env["disclaimer"] = NEURAL_DISCLAIMER
        try:
            size = write_json(path, env)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {str(e)[:120]}"
            report["errors"].append({"kind": "region", "sig_cd": sig, "error": msg})
            failed += 1
            continue
        made += 1
        total_bytes += size
        entry.update({"bytes": size, "reused": False})
        report["regions"].append(entry)
        if i % 50 == 0 or i == len(targets):
            log(f"  [{i}/{len(targets)}] {sig} · {human(size)} "
                f"· 생성 {made} 재사용 {reused} 실패 {failed}")
    report["totals"]["region"] = {"files": made + reused, "made": made, "reused": reused,
                                  "failed": failed, "bytes": total_bytes}
    log(f"[지자체] 생성 {made} · 재사용 {reused} · 실패 {failed} · {human(total_bytes)}")


# --------------------------------------------------------------------------- #
# 품질 지표
# --------------------------------------------------------------------------- #
def random_sample_ids(conn, model: str, kind: str, n: int) -> list[str]:
    """무작위 표본 node_id.

    **ORDER BY RANDOM() 필수.** `LIMIT n` 만 쓰면 삽입순(=그래프 구축순, 즉 지역·분류가
    뭉쳐 있는 순서) 앞부분만 뽑혀 코사인 평균이 과대추정된다 [실측 교훈].
    """
    return [r[0] for r in conn.execute(
        "SELECT node_id FROM node_embeddings WHERE model_name=? AND node_kind=? "
        "ORDER BY RANDOM() LIMIT ?", (model, kind, int(n)))]


def effective_dim(Vn: np.ndarray) -> float:
    """공분산 고유값 엔트로피 기반 유효차원 exp(H). 붕괴한 임베딩은 1 에 가깝다."""
    if Vn.shape[0] < 3:
        return 0.0
    X = Vn - Vn.mean(0, keepdims=True)
    C = (X.T @ X) / max(len(X) - 1, 1)
    w = np.clip(np.linalg.eigvalsh(C.astype(np.float64)), 0, None)
    s = w.sum()
    if s <= 0:
        return 0.0
    p = w[w > 0] / s
    return float(np.exp(-(p * np.log(p)).sum()))


def quality_report(conn, args, meta: list[dict], log=print) -> dict:
    rng = np.random.default_rng(SEED)
    models = [m for m in args.models]
    out: dict = {"models": {}, "pair_sampling": {
        "method": "ORDER BY RANDOM()",
        "why": "LIMIT N 만 쓰면 삽입순(그래프 구축순) 앞부분만 뽑혀 무작위쌍 코사인이 "
               "과대추정된다 — 변별력을 실제보다 좋게 보이게 만든다 [실측 교훈].",
        "sample_size": int(args.sample), "pairs": int(args.pairs), "seed": SEED,
    }, "warnings": []}

    for kind in ("Ordinance", "Region"):
        # 모델 비교가 성립하려면 **같은 노드 집합**을 써야 한다.
        base_model = models[0]
        ids = random_sample_ids(conn, base_model, kind, args.sample)
        if not ids:
            out["warnings"].append(f"{kind}: 표본 없음")
            continue
        vecs: dict[str, dict[str, np.ndarray]] = {}
        for m in models:
            vecs[m] = load_by_ids(conn, m, ids)
        common = sorted(set(ids).intersection(*[set(v) for v in vecs.values()]))
        log(f"  {kind}: 표본 {len(ids)} → 전 모델 공통 {len(common)}")
        if len(common) < 50:
            out["warnings"].append(f"{kind}: 공통 표본 {len(common)}개 — 지표 신뢰 어려움")
            if not common:
                continue

        # 무작위쌍 인덱스(모든 모델 공통 → 모델 간 직접 비교 가능)
        n = len(common)
        npairs = min(int(args.pairs), max(1000, n * 20))
        ii = rng.integers(0, n, npairs)
        jj = rng.integers(0, n, npairs)
        msk = ii != jj
        ii, jj = ii[msk], jj[msk]

        # 라벨(카테고리·지역) — 분리도 AUC 용
        same_cat = same_reg = None
        if kind == "Ordinance":
            oids = [c.split("ordinance:", 1)[-1] for c in common]
            cmap: dict[str, set] = {}
            rmap: dict[str, str] = {}
            for part in chunked(oids):
                ph = ",".join("?" for _ in part)
                for r in conn.execute(
                        f"SELECT ordinance_id, category_code FROM ordinance_category "
                        f"WHERE ordinance_id IN ({ph})", part):
                    cmap.setdefault(r[0], set()).add(r[1])
                for r in conn.execute(
                        f"SELECT ordinance_id, region_id FROM ordinances "
                        f"WHERE ordinance_id IN ({ph})", part):
                    if r[1]:
                        rmap[r[0]] = r[1]
            cats = [cmap.get(o, set()) for o in oids]
            regs = [rmap.get(o) for o in oids]
            same_cat = np.array([bool(cats[a] & cats[b]) for a, b in zip(ii.tolist(), jj.tolist())])
            same_reg = np.array([regs[a] is not None and regs[a] == regs[b]
                                 for a, b in zip(ii.tolist(), jj.tolist())])

        for m in models:
            V = np.stack([vecs[m][c] for c in common]).astype(np.float64)
            Vn = V / np.maximum(np.linalg.norm(V, axis=1, keepdims=True), 1e-12)
            cos = np.einsum("ij,ij->i", Vn[ii], Vn[jj])
            slot = out["models"].setdefault(m, {"model_name": m, "kinds": {}})
            rec = {
                "n_nodes_sampled": int(n),
                "dim": int(V.shape[1]),
                "random_pair_cosine": {
                    "n_pairs": int(cos.shape[0]),
                    "mean": round(float(cos.mean()), 6),
                    "std": round(float(cos.std()), 6),
                    "p50": round(float(np.percentile(cos, 50)), 6),
                    "p90": round(float(np.percentile(cos, 90)), 6),
                    "p99": round(float(np.percentile(cos, 99)), 6),
                    "max": round(float(cos.max()), 6),
                    "frac_gt_0999": round(float((cos > 0.999).mean()), 6),
                },
                "effective_dim": round(effective_dim(Vn[: min(n, 20000)]), 3),
                "interpretation": (
                    "무작위쌍 코사인 평균이 0 근처이고 표준편차가 크며 유효차원이 높을수록 "
                    "변별력이 좋다. 평균이 1 에 붙고 유효차원이 1 에 가까우면 임베딩이 "
                    "붕괴(collapse)한 것이다."),
            }
            if same_cat is not None:
                pos = cos[same_cat]
                neg = cos[~same_cat]
                if pos.size > 20 and neg.size > 20:
                    rec["category_separation_auc"] = {
                        "auc": round(float(GNN.roc_auc(pos, neg)), 4),
                        "n_pos": int(pos.size), "n_neg": int(neg.size),
                        "random_baseline": 0.5,
                        "in_sample": True,
                        "note": "같은 분류를 공유하는 조례쌍 vs 나머지. IN_CATEGORY 는 "
                                "학습 엣지이므로 in-sample 재현 지표다(일반화 성능 아님).",
                    }
                pos = cos[same_reg]
                neg = cos[~same_reg]
                if pos.size > 20 and neg.size > 20:
                    rec["region_separation_auc"] = {
                        "auc": round(float(GNN.roc_auc(pos, neg)), 4),
                        "n_pos": int(pos.size), "n_neg": int(neg.size),
                        "random_baseline": 0.5,
                        "in_sample": True,
                        "note": "같은 지자체 조례쌍 vs 나머지. HAS_ORDINANCE 경유 학습 "
                                "엣지이므로 in-sample 재현 지표다.",
                    }
            slot["kinds"][kind] = rec

    for m in meta:
        slot = out["models"].setdefault(m["model_name"], {"model_name": m["model_name"],
                                                          "kinds": {}})
        slot["meta"] = m
        # 학습 시점의 held-out 분할이 DB 에 남아 있지 않다. 지어내지 않는다.
        slot["held_out_auc"] = None
        slot["held_out_auc_status"] = "unavailable"
        slot["held_out_auc_reason"] = (
            "학습 당시의 held-out 엣지 분할(policymap.neural.gnn.split_edges)이 DB 에 "
            "저장돼 있지 않다. 같은 분할을 복원할 수 없으므로 held-out AUC 를 여기서 "
            "다시 계산하면 학습에 쓰인 엣지가 섞여 과대추정된다. 재학습 없이는 산출 "
            "불가 — 값을 지어내지 않고 null 로 둔다. 재산출하려면 "
            "gnn.train_graphsage(..., split=split_edges(...)) 로 다시 돌리고 "
            "그 결과를 이 파일에 기록해야 한다.")
    return out


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="신경망 임베딩 → 시각화 shard 생성")
    ap.add_argument("--out", default=str(ROOT / "data"), help="출력 루트(기본 system/data)")
    ap.add_argument("--only", default=",".join(KINDS),
                    help=f"생성 대상(쉼표). 선택지: {','.join(KINDS)}")
    ap.add_argument("--models", default=None,
                    help="대상 모델(쉼표). 기본: DB 에 있는 모델 전부")
    ap.add_argument("--targets", type=int, default=400, help="대표 조례 수(기본 400)")
    ap.add_argument("--limit", type=int, default=0,
                    help="시험용 상한(조례·지자체 모두에 적용, 0=제한없음)")
    ap.add_argument("--k", type=int, default=10, help="Top-K(기본 10)")
    ap.add_argument("--block", type=int, default=128, help="kNN 블록 행렬곱 행 블록")
    ap.add_argument("--sample", type=int, default=20000,
                    help="품질지표 무작위 표본 노드 수(ORDER BY RANDOM())")
    ap.add_argument("--pairs", type=int, default=200000, help="품질지표 무작위쌍 수")
    ap.add_argument("--force", action="store_true", help="이미 있는 shard 도 재생성")
    ap.add_argument("--rw", action="store_true",
                    help="db.connect()(쓰기모드)로 연결. 기본은 읽기전용 — 동시 수집 "
                         "에이전트와의 WAL 락 교착을 피한다")
    a = ap.parse_args()

    a.only = {s.strip() for s in a.only.split(",") if s.strip()}
    bad = a.only - set(KINDS)
    if bad:
        print(f"[오류] 알 수 없는 --only 값: {sorted(bad)} (선택지 {list(KINDS)})")
        return 2

    out_dir = Path(a.out) / "api" / "neural"
    for sub in ("ordinance", "region"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)

    cfg = get_config()
    conn = D.connect() if a.rw else connect_ro(cfg.db_path)
    t0 = time.time()

    meta = model_meta(conn)
    if not meta:
        print("[오류] node_embeddings 가 비어 있다. 먼저 임베딩을 학습·저장해야 한다.")
        return 3
    avail = [m["model_name"] for m in meta]
    a.models = ([s.strip() for s in a.models.split(",") if s.strip()]
                if a.models else avail)
    missing = [m for m in a.models if m not in avail]
    if missing:
        print(f"[오류] DB 에 없는 모델: {missing} (있는 것: {avail})")
        return 4

    print(f"DB={cfg.db_path}", flush=True)
    print(f"출력={out_dir}  모델={a.models}  생성={sorted(a.only)}  force={a.force}",
          flush=True)
    for m in meta:
        print(f"  · {m['model_name']}: dim={m['dim']} 노드 {m['nodes_total']}건 "
              f"{m['nodes_by_kind']} / 유사도 {m['similarity_rows']}행", flush=True)

    report = {
        "generator": "make_neural_fixtures.py",
        "as_of_date": time.strftime("%Y-%m-%d"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "params": {"only": sorted(a.only), "models": a.models, "targets": a.targets,
                   "limit": a.limit, "k": a.k, "sample": a.sample, "pairs": a.pairs,
                   "force": bool(a.force)},
        "layout": {"ordinance": "ordinance/{ordinance_id with ':'→'-'}.json",
                   "region": "region/{sig_cd}.json",
                   "quality": "quality.json"},
        "models": meta,
        "ordinances": [], "regions": [],
        "totals": {}, "warnings": [], "errors": [],
        "notes": [
            "이웃은 node_embeddings 코사인 재계산본이다(후보=전체 노드). "
            "neural_similarity 저장본은 stored_rank/stored_agreement 로 대조만 한다.",
            "폐지 조례는 status/repealed/repealed_on 으로 표시된다. 선례 추천 금지.",
        ],
    }

    ordinance_shards(conn, out_dir, a, report)
    region_shards(conn, out_dir, a, report)

    if "quality" in a.only:
        qpath = out_dir / "quality.json"
        if a.force or existing(qpath) is None:
            print("[품질] 지표 계산", flush=True)
            q = quality_report(conn, a, meta)
            env = envelope(q, neural=True)
            env["disclaimer"] = NEURAL_DISCLAIMER
            qb = write_json(qpath, env)
            report["totals"]["quality"] = {"files": 1, "bytes": qb, "reused": False}
            print(f"[품질] quality.json {human(qb)}", flush=True)
        else:
            report["totals"]["quality"] = {"files": 1, "bytes": existing(qpath),
                                           "reused": True}
            print("[품질] quality.json 이미 있음(재사용). --force 로 재계산", flush=True)

    # 디스크 실측으로 합계를 다시 잡는다(부분 실행이 카탈로그를 지우지 않게)
    disk = {}
    for sub in ("ordinance", "region"):
        files = sorted((out_dir / sub).glob("*.json"))
        disk[sub] = {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}
    qb = existing(out_dir / "quality.json") or 0
    report["totals"]["on_disk"] = {
        **disk, "quality": {"files": 1 if qb else 0, "bytes": qb},
        "total_files": disk["ordinance"]["files"] + disk["region"]["files"] + (1 if qb else 0),
        "total_bytes": disk["ordinance"]["bytes"] + disk["region"]["bytes"] + qb,
    }
    report["totals"]["seconds"] = round(time.time() - t0, 1)

    idx = out_dir / "index.json"
    body = json.dumps(report, ensure_ascii=False, indent=1)
    tmp = idx.with_suffix(".json.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, idx)

    print("─" * 70, flush=True)
    od = report["totals"]["on_disk"]
    print(f"  ordinance      {od['ordinance']['files']:>4} 파일  {human(od['ordinance']['bytes'])}")
    print(f"  region         {od['region']['files']:>4} 파일  {human(od['region']['bytes'])}")
    print(f"  quality        {od['quality']['files']:>4} 파일  {human(od['quality']['bytes'])}")
    print(f"  index.json        1 파일  {human(idx.stat().st_size)}")
    print(f"합계 {od['total_files'] + 1} 파일 · "
          f"{human(od['total_bytes'] + idx.stat().st_size)} · "
          f"{report['totals']['seconds']}s · 경고 {len(report['warnings'])} "
          f"· 실패 {len(report['errors'])}", flush=True)
    for w in report["warnings"][:10]:
        print(f"  [경고] {w}", flush=True)
    for e in report["errors"][:10]:
        print(f"  [실패] {e}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
