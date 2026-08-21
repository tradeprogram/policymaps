"""policymap.neural.embeddings — 구조 임베딩(node2vec / DeepWalk / metapath2vec), numpy 자체구현.

torch/gensim 없이 numpy 만으로:
  1) 그래프 → CSR 배열(엣지타입 가중치 반영, 라벨별 CSR 은 메타패스용 지연 생성)
  2) 2차(2nd-order) 랜덤워크 — node2vec p(return)/q(in-out) 를 **벡터화 기각샘플링**으로 구현
     (p=q=1 이면 기각 없이 DeepWalk 와 동일 경로로 자동 축약)
  3) skip-gram with negative sampling(SGNS) — forward/backward/SGD 를 직접 미분해 구현
     (분산 scatter-add 는 argsort+reduceat 로 중복 인덱스까지 정확히 누적)

공개 API:
    train_node2vec(graph, dim=128, walks_per_node=10, walk_len=40, window=5,
                   epochs=5, negatives=5, *, p=1.0, q=1.0, ...) -> EmbeddingResult
    GraphArrays.from_graph(G, ...)             # 그래프 → numpy 배열 뷰
    ensure_neural_tables(conn)                 # node_embeddings / neural_similarity DDL
    save_node_embeddings(conn, result, ...)    # 임베딩 영속화
    load_node_embeddings(conn, model_name, ...)
    build_neural_similarity(conn, result, ...) # 임베딩 kNN → neural_similarity
    most_similar(result, node_id, k=10)        # 메모리 내 Top-k
    most_similar_db(conn, node_id, k=10, ...)  # 저장된 Top-k 조회

설계 규율(CONTRACTS.md 공통):
  * numpy 는 선택적 import — 부재 시 모듈 import 는 성공, 학습 호출만 RuntimeError.
  * DB 접점은 db.py 공개 API(upsert_many/tx/fetchall/execute)만 사용.
  * 진행률은 util.get_logger 로 로깅(대량 학습 가시성).
"""
from __future__ import annotations

import base64
import json

import sqlite3
from typing import Any, Iterable, Optional, Sequence

from .. import db as _db
from .. import util as _util
from ..graph import build as _build

try:  # 선택적 가속(사실상 필수 — 학습 경로에서만 요구)
    import numpy as _np  # type: ignore
    _HAS_NUMPY = True
except Exception:  # pragma: no cover - 폴백 경로
    _np = None  # type: ignore
    _HAS_NUMPY = False


LOG = _util.get_logger("policymap.neural")

# --------------------------------------------------------------------------- #
# 엣지타입 가중치 (이종그래프 전이확률 가중)
# --------------------------------------------------------------------------- #
# 워크가 어떤 관계를 얼마나 자주 타는지를 정한다. 허브(의원 320명 × VOTED 57k)는
# 낮게, 정책 신호가 강한 관계(SIMILAR_TO/FUNDED_BY/DELEGATED_FROM)는 높게.
DEFAULT_EDGE_WEIGHTS: dict[str, float] = {
    "HAS_ORDINANCE": 1.0,
    "CONTAINS": 1.0,
    "ADJACENT_TO": 2.0,
    "SUCCEEDED_BY": 1.0,
    "DELEGATED_FROM": 2.0,
    "SUBORDINATE_TO": 2.0,
    "CITES": 1.5,
    "INCORPORATES_STANDARD": 1.5,
    "PREVAILS_OVER": 1.0,
    "AMENDED_BY": 1.0,
    "SIMILAR_TO": 3.0,
    "IN_CATEGORY": 2.0,
    "FUNDED_BY": 2.0,
    "PROPOSED_BY": 0.5,
    "VOTED": 0.25,
    "MEMBER_OF": 0.5,
    "ENACTS": 1.0,
}
_DEFAULT_EDGE_WEIGHT = 1.0

# 메타패스 스키마(metapath2vec). 라벨 시퀀스이며 마지막=처음이면 순환 반복.
METAPATHS: dict[str, list[str]] = {
    # 지역-조례-지역: 같은 정책수단을 가진 지자체를 가깝게
    "R-O-R": ["Region", "Ordinance", "Region"],
    # 지역 인접/포함 체인
    "R-R": ["Region", "Region"],
    # 조례-상위법-조례: 동일 위임 상위법을 공유하는 조례를 가깝게
    "O-I-O": ["Ordinance", "LegalInstrument", "Ordinance"],
    # 조례-예산-조례: 동일 재정사업으로 이어지는 조례
    "O-B-O": ["Ordinance", "BudgetLine", "Ordinance"],
    # 조례-분류-조례
    "O-C-O": ["Ordinance", "Category", "Ordinance"],
    # 조례-지역-조례: 같은 지자체 조례 묶음
    "O-R-O": ["Ordinance", "Region", "Ordinance"],
    # 의안-의원-의안
    "B-L-B": ["Bill", "Legislator", "Bill"],
}

_DEFAULT_MODEL = "node2vec-numpy"


def _require_numpy() -> None:
    if not _HAS_NUMPY:  # pragma: no cover - 환경 의존
        raise RuntimeError(
            "policymap.neural 학습에는 numpy 가 필요하다 (pip install numpy). "
            "torch/sklearn/gensim 은 사용하지 않는다."
        )


# --------------------------------------------------------------------------- #
# 그래프 → numpy 배열(CSR)
# --------------------------------------------------------------------------- #
class GraphArrays:
    """graph.build 그래프 → numpy CSR 뷰(무방향 대칭화, 엣지타입 가중).

    속성:
      nodes        list[str]                노드 ID(인덱스 순)
      index        dict[str,int]            노드 ID → 인덱스
      labels       int32[N]                 라벨 ID
      label_names  list[str]                라벨 ID → 라벨명('Region','Ordinance',...)
      attrs        list[dict]               노드 속성(name/kind 등)
      indptr       int64[N+1]               CSR 행 포인터(대칭화된 인접)
      indices      int32[E2]                이웃 노드 인덱스
      weights      float32[E2]              전이 가중치
      wcum_ex      float64[E2+1]            weights 의 exclusive prefix sum(가중샘플링용)
      rowsum       float64[N]               행 가중치 합
      degree       int64[N]                 이웃 수
      edge_src/edge_dst/edge_rel            원본(방향 무시·중복 제거) 엣지 리스트
      edge_keys    int64[E]                 정렬된 (min*N+max) 키 — 2차 워크 인접 판정용
    """

    __slots__ = ("nodes", "index", "labels", "label_names", "attrs",
                 "indptr", "indices", "weights", "wcum_ex", "rowsum", "degree",
                 "edge_src", "edge_dst", "edge_rel", "edge_weight", "edge_keys",
                 "_label_csr", "meta")

    def __init__(self) -> None:
        self._label_csr: dict[int, tuple] = {}
        self.meta: dict[str, Any] = {}

    # --- 크기 ---
    @property
    def num_nodes(self) -> int:
        return len(self.nodes)

    @property
    def num_edges(self) -> int:
        return int(self.edge_src.shape[0])

    # --- 생성 ---
    @classmethod
    def from_graph(
        cls,
        G: Any,
        *,
        edge_weights: Optional[dict[str, float]] = None,
        min_degree: int = 1,
        keep_kinds: Optional[Iterable[str]] = None,
        drop_relations: Optional[Iterable[str]] = None,
        logger: Any = None,
    ) -> "GraphArrays":
        """그래프 → GraphArrays.

        min_degree: 이 차수 미만 노드 제거(기본 1 = 고립노드 제거). 고립노드는 학습신호가
                    전혀 없어 임베딩이 초기값 그대로 남으므로 기본적으로 뺀다.
        keep_kinds: 유지할 노드 kind 집합(None 이면 전부).
        drop_relations: 제외할 관계명 집합.
        """
        _require_numpy()
        log = logger or LOG
        np = _np
        weights_map = dict(DEFAULT_EDGE_WEIGHTS)
        if edge_weights:
            weights_map.update({str(k): float(v) for k, v in edge_weights.items()})
        drop = set(drop_relations or ())
        keep = set(keep_kinds) if keep_kinds else None

        raw_nodes = _build.graph_nodes(G)
        raw_edges = _build.graph_edges(G)

        # 1차 인덱스(전체) — 차수 계산용
        pre_index: dict[str, int] = {}
        pre_attrs: list[dict] = []
        for nid, attr in raw_nodes:
            if keep is not None and attr.get("kind") not in keep:
                continue
            pre_index[nid] = len(pre_attrs)
            pre_attrs.append(attr)

        # 엣지 수집(자기루프 제거, 무방향 중복 제거, 관계별 최대가중 채택)
        pair_w: dict[tuple[int, int], float] = {}
        pair_rel: dict[tuple[int, int], str] = {}
        for u, v, key, attr in raw_edges:
            rel = str(attr.get("relation") or key or "REL")
            if rel in drop:
                continue
            iu = pre_index.get(u)
            iv = pre_index.get(v)
            if iu is None or iv is None or iu == iv:
                continue
            w = float(weights_map.get(rel, _DEFAULT_EDGE_WEIGHT))
            if w <= 0.0:
                continue
            pk = (iu, iv) if iu < iv else (iv, iu)
            if w > pair_w.get(pk, 0.0):
                pair_w[pk] = w
                pair_rel[pk] = rel
            elif pk not in pair_w:
                pair_w[pk] = w
                pair_rel[pk] = rel

        if not pair_w:
            raise ValueError("GraphArrays.from_graph: 사용 가능한 엣지가 없다")

        e_src = np.fromiter((a for a, _ in pair_w.keys()), dtype=np.int64,
                            count=len(pair_w))
        e_dst = np.fromiter((b for _, b in pair_w.keys()), dtype=np.int64,
                            count=len(pair_w))
        e_w = np.fromiter(pair_w.values(), dtype=np.float32, count=len(pair_w))
        rel_list = list(pair_rel.values())

        # 차수 필터 반복 적용(핵심 그래프 추출)
        n_pre = len(pre_attrs)
        alive = np.ones(n_pre, dtype=bool)
        e_alive = np.ones(e_src.shape[0], dtype=bool)
        if min_degree > 1:
            for _ in range(20):
                deg = np.bincount(e_src[e_alive], minlength=n_pre) + \
                    np.bincount(e_dst[e_alive], minlength=n_pre)
                new_alive = alive & (deg >= min_degree)
                if bool((new_alive == alive).all()):
                    break
                alive = new_alive
                e_alive = alive[e_src] & alive[e_dst]
        else:
            deg = np.bincount(e_src, minlength=n_pre) + np.bincount(e_dst, minlength=n_pre)
            alive = deg >= max(1, int(min_degree))
            e_alive = alive[e_src] & alive[e_dst]

        old_ids = np.flatnonzero(alive)
        remap = np.full(n_pre, -1, dtype=np.int64)
        remap[old_ids] = np.arange(old_ids.shape[0], dtype=np.int64)

        e_src = remap[e_src[e_alive]]
        e_dst = remap[e_dst[e_alive]]
        e_w = e_w[e_alive]
        rel_list = [r for r, ok in zip(rel_list, e_alive.tolist()) if ok]

        node_ids = [nid for nid, i in sorted(pre_index.items(), key=lambda kv: kv[1])]
        ga = cls()
        ga.nodes = [node_ids[i] for i in old_ids.tolist()]
        ga.attrs = [pre_attrs[i] for i in old_ids.tolist()]
        ga.index = {nid: i for i, nid in enumerate(ga.nodes)}

        label_names: list[str] = []
        label_ids: dict[str, int] = {}
        lab = np.zeros(len(ga.nodes), dtype=np.int32)
        for i, attr in enumerate(ga.attrs):
            name = str(attr.get("label") or attr.get("kind") or "Node")
            j = label_ids.get(name)
            if j is None:
                j = len(label_names)
                label_ids[name] = j
                label_names.append(name)
            lab[i] = j
        ga.labels = lab
        ga.label_names = label_names

        ga._set_edges(e_src, e_dst, e_w, rel_list)
        ga.meta.update({
            "min_degree": int(min_degree),
            "labels": {label_names[i]: int(c)
                       for i, c in enumerate(np.bincount(lab, minlength=len(label_names)))},
            "dropped_nodes": int(n_pre - len(ga.nodes)),
        })
        log.info("GraphArrays: nodes=%d edges=%d (원본 %d노드에서 %d 제거) labels=%s",
                 len(ga.nodes), ga.num_edges, n_pre, n_pre - len(ga.nodes),
                 ga.meta["labels"])
        return ga

    # --- 내부: 엣지 배열 세팅(CSR/키/메타) ---
    def _set_edges(self, e_src, e_dst, e_w, rel_list) -> None:
        np = _np
        n = len(self.nodes)
        e_src = np.asarray(e_src, dtype=np.int64)
        e_dst = np.asarray(e_dst, dtype=np.int64)
        e_w = np.asarray(e_w, dtype=np.float32)
        self.edge_src = e_src.astype(np.int32)
        self.edge_dst = e_dst.astype(np.int32)
        self.edge_weight = e_w
        self.edge_rel = list(rel_list)

        # 대칭화 CSR
        s = np.concatenate([e_src, e_dst])
        d = np.concatenate([e_dst, e_src])
        w = np.concatenate([e_w, e_w]).astype(np.float32)
        order = np.argsort(s, kind="stable")
        s, d, w = s[order], d[order], w[order]
        counts = np.bincount(s, minlength=n)
        self.indptr = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
        self.indices = d.astype(np.int32)
        self.weights = np.maximum(w, 1e-6)
        self.wcum_ex = np.concatenate([[0.0], np.cumsum(self.weights, dtype=np.float64)])
        self.rowsum = self.wcum_ex[self.indptr[1:]] - self.wcum_ex[self.indptr[:-1]]
        self.degree = counts.astype(np.int64)

        # 2차 워크용 무방향 엣지 키(정렬)
        keys = e_src * n + e_dst
        keys2 = e_dst * n + e_src
        self.edge_keys = np.sort(np.concatenate([keys, keys2]))
        self._label_csr = {}
        self.meta.update({
            "num_nodes": n,
            "num_edges": int(e_src.shape[0]),
            "relations": _count_relations(self.edge_rel),
        })

    def subset_edges(self, keep) -> "GraphArrays":
        """같은 노드집합·같은 인덱스를 유지한 채 엣지 부분집합만 남긴 새 GraphArrays.

        링크예측 평가에서 held-out 엣지를 **워크/메시지패싱 그래프에서 제거**해
        누수 없이 학습하기 위해 쓴다(transductive leakage 차단).
        """
        np = _np
        keep = np.asarray(keep)
        if keep.dtype != bool:
            m = np.zeros(self.num_edges, dtype=bool)
            m[keep] = True
            keep = m
        out = GraphArrays()
        out.nodes = self.nodes
        out.index = self.index
        out.labels = self.labels
        out.label_names = self.label_names
        out.attrs = self.attrs
        out.meta = dict(self.meta)
        rel = [r for r, k in zip(self.edge_rel, keep.tolist()) if k]
        out._set_edges(self.edge_src[keep], self.edge_dst[keep],
                       self.edge_weight[keep], rel)
        out.meta["subset_of"] = int(self.num_edges)
        return out

    # --- 조회 ---
    def has_edge(self, u: Any, v: Any) -> Any:
        """벡터화 무방향 인접 판정(u,v 는 int 배열)."""
        np = _np
        key = np.asarray(u, dtype=np.int64) * self.num_nodes + \
            np.asarray(v, dtype=np.int64)
        pos = np.searchsorted(self.edge_keys, key)
        pos_c = np.clip(pos, 0, self.edge_keys.shape[0] - 1)
        return (pos < self.edge_keys.shape[0]) & (self.edge_keys[pos_c] == key)

    def label_csr(self, label_id: int) -> tuple:
        """라벨 제한 CSR(메타패스용). (indptr, indices, wcum_ex, rowsum) 지연 생성."""
        cached = self._label_csr.get(label_id)
        if cached is not None:
            return cached
        np = _np
        n = self.num_nodes
        keep = self.labels[self.indices] == label_id
        rows = np.repeat(np.arange(n, dtype=np.int64), self.degree)
        counts = np.bincount(rows[keep], minlength=n)
        indptr = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
        indices = self.indices[keep]
        weights = self.weights[keep]
        wcum_ex = np.concatenate([[0.0], np.cumsum(weights, dtype=np.float64)])
        rowsum = wcum_ex[indptr[1:]] - wcum_ex[indptr[:-1]]
        out = (indptr, indices, wcum_ex, rowsum)
        self._label_csr[label_id] = out
        return out

    def nodes_of_label(self, label: str) -> Any:
        """라벨명 → 노드 인덱스 배열."""
        if label not in self.label_names:
            return _np.zeros(0, dtype=_np.int64)
        return _np.flatnonzero(self.labels == self.label_names.index(label))

    def name_of(self, i: int) -> str:
        a = self.attrs[i]
        return str(a.get("name") or a.get("full_name") or self.nodes[i])


def _count_relations(rel_list: Sequence[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rel_list:
        out[r] = out.get(r, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------- #
# 랜덤워크
# --------------------------------------------------------------------------- #
def _sample_neighbor_pos(indptr, wcum_ex, rowsum, cur, rng):
    """가중 이웃 1개를 CSR 위치(offset)로 샘플. rowsum==0 인 노드는 -1."""
    np = _np
    lo = indptr[cur]
    tot = rowsum[cur]
    r = rng.random(cur.shape[0]) * tot
    target = wcum_ex[lo] + r
    pos = np.searchsorted(wcum_ex, target, side="right") - 1
    hi = indptr[cur + 1]
    dead = tot <= 0.0
    np.clip(pos, lo, np.maximum(lo, hi - 1), out=pos)
    pos[dead] = -1
    return pos


def generate_walks(
    ga: GraphArrays,
    *,
    walks_per_node: int = 10,
    walk_len: int = 40,
    p: float = 1.0,
    q: float = 1.0,
    start_nodes: Any = None,
    metapath: Optional[Sequence[str]] = None,
    rng: Any = None,
    chunk_walks: int = 400_000,
    max_tries: int = 12,
    logger: Any = None,
):
    """2차 랜덤워크 생성기. (walks 배열 청크) 를 순차 yield.

    p(return parameter) / q(in-out parameter) 는 **벡터화 기각샘플링**으로 반영한다:
      후보 x 를 이웃 가중치대로 뽑고 d(prev,x) 에 따라 1/p(=prev 로 회귀),
      1(=prev 의 이웃), 1/q(그 외) 의 확률로 수락. 최대 확률로 나눠 정규화한다.
      p=q=1 이면 수락확률이 항상 1 → 기각 없이 1차 워크(DeepWalk)로 축약된다.

    metapath 가 주어지면 각 스텝의 다음 노드 라벨을 스키마로 강제한다(metapath2vec).
    반환 청크는 int32 [num_walks, walk_len]; 조기 종료(막다른 길)는 -1 로 패딩된다.

    max_tries: 기각샘플링 재시도 상한. 초과분은 그대로 수락한다. 최소 수락확률은
        min(1, 1/p, 1/q)/max(1, 1/p, 1/q) 이므로 p=0.5·q=2.0 이면 0.25 →
        12회 연속 기각 확률 0.75^12 ≈ 3.2%(그만큼만 근사). 늘리면 더 정확해진다.
    """
    _require_numpy()
    np = _np
    log = logger or LOG
    rng = rng if rng is not None else np.random.default_rng(20260819)

    if start_nodes is None:
        starts_all = np.arange(ga.num_nodes, dtype=np.int64)
    else:
        starts_all = np.asarray(start_nodes, dtype=np.int64)

    mp_ids: Optional[list[int]] = None
    if metapath:
        mp = list(metapath)
        missing = [m for m in mp if m not in ga.label_names]
        if missing:
            raise ValueError(f"metapath 라벨 없음: {missing} (가능: {ga.label_names})")
        mp_ids = [ga.label_names.index(m) for m in mp]
        starts_all = starts_all[ga.labels[starts_all] == mp_ids[0]]
        if starts_all.size == 0:
            raise ValueError(f"metapath 시작 라벨 노드 없음: {mp[0]}")

    inv_p, inv_q = 1.0 / float(p), 1.0 / float(q)
    max_prob = max(1.0, inv_p, inv_q)
    unbiased = abs(inv_p - 1.0) < 1e-12 and abs(inv_q - 1.0) < 1e-12

    total_walks = int(starts_all.shape[0]) * int(walks_per_node)
    done = 0
    log.info("walk 생성: 시작노드 %d × %d회 × 길이 %d = %d walks (p=%.2f q=%.2f%s)",
             starts_all.shape[0], walks_per_node, walk_len, total_walks, p, q,
             ", metapath=" + "-".join(metapath) if metapath else "")

    seeds = np.repeat(starts_all, walks_per_node)
    rng.shuffle(seeds)
    for off in range(0, seeds.shape[0], chunk_walks):
        batch = seeds[off:off + chunk_walks]
        m = batch.shape[0]
        walks = np.full((m, walk_len), -1, dtype=np.int32)
        walks[:, 0] = batch
        cur = batch.copy()
        prev = batch.copy()
        active = np.ones(m, dtype=bool)
        for t in range(1, walk_len):
            idx = np.flatnonzero(active)
            if idx.size == 0:
                break
            c = cur[idx]
            if mp_ids is not None:
                need = mp_ids[1 + (t - 1) % (len(mp_ids) - 1)]
                indptr, indices, wcum_ex, rowsum = ga.label_csr(need)
            else:
                indptr, indices, wcum_ex, rowsum = (
                    ga.indptr, ga.indices, ga.wcum_ex, ga.rowsum)

            pos = _sample_neighbor_pos(indptr, wcum_ex, rowsum, c, rng)
            ok = pos >= 0
            nxt = np.where(ok, indices[np.maximum(pos, 0)], -1).astype(np.int64)

            if not unbiased:
                pr = prev[idx]
                need_resample = ok.copy()
                for _ in range(max_tries):
                    sel = np.flatnonzero(need_resample)
                    if sel.size == 0:
                        break
                    cand = nxt[sel]
                    prv = pr[sel]
                    acc = np.where(
                        cand == prv, inv_p,
                        np.where(ga.has_edge(prv, cand), 1.0, inv_q)) / max_prob
                    keep = rng.random(sel.shape[0]) < acc
                    need_resample[sel[keep]] = False
                    again = sel[~keep]
                    if again.size == 0:
                        break
                    pos2 = _sample_neighbor_pos(indptr, wcum_ex, rowsum,
                                                c[again], rng)
                    ok2 = pos2 >= 0
                    nxt[again] = np.where(ok2, indices[np.maximum(pos2, 0)], -1)
                    need_resample[again[~ok2]] = False
                    ok[again] = ok2

            walks[idx[ok], t] = nxt[ok].astype(np.int32)
            prev[idx[ok]] = cur[idx[ok]]
            cur[idx[ok]] = nxt[ok]
            active[idx[~ok]] = False

        done += m
        log.info("  walk 청크 %d/%d (%.1f%%)", done, total_walks,
                 100.0 * done / max(1, total_walks))
        yield walks


# --------------------------------------------------------------------------- #
# SGNS (skip-gram with negative sampling) — 수동 미분 SGD
# --------------------------------------------------------------------------- #
def _sigmoid(x):
    return 1.0 / (1.0 + _np.exp(-_np.clip(x, -30.0, 30.0)))


def _scatter_add(target, idx, vals, *, max_row_norm: Optional[float] = None) -> None:
    """target[idx] += vals — 중복 인덱스를 정확히 누적(argsort+reduceat).

    np.add.at 은 정확하지만 매우 느리다. 정렬 후 구간합(reduceat)으로 중복을 미리
    합치면 동일 결과를 수십 배 빠르게 얻는다.

    max_row_norm 을 주면 **노드별 누적 갱신량**의 L2 노름을 그 값으로 제한한다.
    SGNS 를 미니배치로 벡터화하면 허브 노드는 한 배치에 수천 번 등장하는데,
    축차 SGD 와 달리 중간 갱신이 반영되지 않아 그대로 더하면 발산한다
    (실측: 클리핑 없이 batch=16384 에서 loss 4.16 → 92 로 폭주).
    희소 노드는 축차 SGD 와 동일한 스텝을 받고 허브만 상한에 걸린다.
    """
    np = _np
    if idx.size == 0:
        return
    order = np.argsort(idx, kind="stable")
    idx_s = idx[order]
    vals_s = vals[order]
    bounds = np.flatnonzero(np.concatenate(([True], idx_s[1:] != idx_s[:-1])))
    summed = np.add.reduceat(vals_s, bounds, axis=0)
    if max_row_norm:
        summed = _clip_rows(summed, float(max_row_norm))
    target[idx_s[bounds]] += summed


def _clip_rows(grad, max_norm: float):
    """행 L2 노름을 max_norm 이하로 스케일링."""
    np = _np
    n = np.linalg.norm(grad, axis=1, keepdims=True)
    factor = np.minimum(1.0, max_norm / np.maximum(n, 1e-12))
    return grad * factor


def _neg_sampler(counts, exponent: float = 0.75):
    """빈도^exponent 분포 누적표(searchsorted 로 O(logN) 벡터 샘플링)."""
    np = _np
    pw = np.power(np.maximum(counts.astype(np.float64), 0.0), exponent)
    tot = pw.sum()
    if tot <= 0:
        pw = np.ones_like(pw)
        tot = pw.sum()
    return np.cumsum(pw) / tot


def _make_pairs(walks, window: int, rng, *, keep_mask=None):
    """워크 배열 → (center, context) 페어. 창 크기는 word2vec 식 랜덤 축소.

    오프셋 o 인 페어를 확률 (window-o+1)/window 로 유지 = 창 크기를 1..window 에서
    균등 추출한 것과 동일한 기대효과. 양방향 페어를 모두 생성한다.
    """
    np = _np
    cs: list = []
    ct: list = []
    for o in range(1, window + 1):
        if o >= walks.shape[1]:
            break
        a = walks[:, :-o].reshape(-1)
        b = walks[:, o:].reshape(-1)
        valid = (a >= 0) & (b >= 0)
        if keep_mask is not None:
            # 고빈도 노드 드롭(위치는 유지 → 창 거리 왜곡 없음)
            valid &= keep_mask[np.maximum(a, 0)] & keep_mask[np.maximum(b, 0)]
        keep_p = (window - o + 1) / float(window)
        if keep_p < 1.0:
            valid &= rng.random(valid.shape[0]) < keep_p
        a, b = a[valid], b[valid]
        cs.append(a)
        ct.append(b)
        cs.append(b)
        ct.append(a)
    if not cs:
        return (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64))
    center = np.concatenate(cs).astype(np.int64)
    context = np.concatenate(ct).astype(np.int64)
    perm = rng.permutation(center.shape[0])
    return center[perm], context[perm]


class EmbeddingResult(dict):
    """dict[node_id, np.ndarray] + 학습 메타(.stats/.matrix/.nodes/.index/.model_name)."""

    __slots__ = ("matrix", "nodes", "index", "labels", "label_names",
                 "dim", "model_name", "stats", "loss_history")

    def __init__(self, matrix, nodes, *, labels=None, label_names=None,
                 model_name: str = _DEFAULT_MODEL, stats: Optional[dict] = None,
                 loss_history: Optional[list] = None):
        super().__init__(zip(nodes, matrix))
        self.matrix = matrix
        self.nodes = list(nodes)
        self.index = {n: i for i, n in enumerate(self.nodes)}
        self.labels = labels
        self.label_names = label_names or []
        self.dim = int(matrix.shape[1]) if matrix is not None else 0
        self.model_name = model_name
        self.stats = stats or {}
        self.loss_history = loss_history or []

    def unit_matrix(self):
        """행 L2 정규화 행렬(코사인 계산용)."""
        np = _np
        nrm = np.linalg.norm(self.matrix, axis=1, keepdims=True)
        nrm[nrm == 0] = 1.0
        return self.matrix / nrm

    def kind_mask(self, kinds: Iterable[str]):
        """라벨명 집합 → bool 마스크."""
        np = _np
        want = {k for k in kinds}
        ids = {i for i, nm in enumerate(self.label_names) if nm in want}
        if self.labels is None or not ids:
            return np.ones(len(self.nodes), dtype=bool)
        return _np.isin(self.labels, list(ids))


def train_sgns(
    walk_chunks,
    num_nodes: int,
    *,
    dim: int = 128,
    window: int = 5,
    epochs: int = 5,
    negatives: int = 5,
    lr: float = 0.025,
    min_lr: float = 1e-4,
    batch_size: int = 16384,
    subsample: float = 1e-3,
    ns_exponent: float = 0.75,
    clip_grad: float = 5.0,
    max_pairs_per_epoch: Optional[int] = None,
    seed: int = 20260819,
    logger: Any = None,
) -> tuple:
    """SGNS 학습(순전파·역전파·SGD 직접 구현). (W_in, stats, loss_history) 반환.

    목적함수: -log σ(v_c·u_o) - Σ_k E[log σ(-v_c·u_k)]
    미분:  g_pos = σ(v_c·u_o) - 1,  g_neg = σ(v_c·u_k)
           ∂L/∂v_c = g_pos·u_o + Σ_k g_neg_k·u_k
           ∂L/∂u_o = g_pos·v_c,  ∂L/∂u_k = g_neg_k·v_c
    """
    _require_numpy()
    np = _np
    log = logger or LOG
    rng = np.random.default_rng(seed)

    walks = list(walk_chunks) if not isinstance(walk_chunks, list) else walk_chunks
    if not walks:
        raise ValueError("train_sgns: 워크가 비어 있다")

    counts = np.zeros(num_nodes, dtype=np.int64)
    tokens = 0
    for w in walks:
        flat = w.reshape(-1)
        flat = flat[flat >= 0]
        counts += np.bincount(flat, minlength=num_nodes)
        tokens += int(flat.shape[0])
    log.info("SGNS 코퍼스: 토큰 %d, 방문노드 %d/%d (%.1f%%)",
             tokens, int((counts > 0).sum()), num_nodes,
             100.0 * float((counts > 0).sum()) / max(1, num_nodes))

    # 고빈도(허브) 노드 서브샘플링 — word2vec t-subsampling
    keep_mask = None
    if subsample and subsample > 0 and tokens > 0:
        freq = counts / float(tokens)
        with np.errstate(divide="ignore", invalid="ignore"):
            keep_p = np.where(freq > 0,
                              (np.sqrt(freq / subsample) + 1.0) * (subsample / np.maximum(freq, 1e-12)),
                              1.0)
        keep_p = np.clip(keep_p, 0.0, 1.0)
        keep_mask = keep_p  # 확률 → 아래에서 난수 비교
    neg_cum = _neg_sampler(counts, ns_exponent)

    rs = np.random.default_rng(seed + 1)
    W_in = (rs.random((num_nodes, dim), dtype=np.float32) - 0.5).astype(np.float32) / dim
    W_out = np.zeros((num_nodes, dim), dtype=np.float32)

    # 총 페어 수 추정(학습률 선형감쇠용)
    est_pairs = 0
    for w in walks:
        valid = int((w >= 0).sum())
        est_pairs += int(valid * 2 * (window + 1) / 2.0)
    if max_pairs_per_epoch:
        est_pairs = min(est_pairs, int(max_pairs_per_epoch))
    total_pairs = max(1, est_pairs * epochs)

    loss_history: list[float] = []
    seen = 0
    import time as _time
    t0 = _time.time()
    for ep in range(epochs):
        ep_loss, ep_batches, ep_pairs = 0.0, 0, 0
        # 에폭마다 청크 순서를 섞는다 — max_pairs_per_epoch 로 조기 종료할 때
        # 매 에폭 같은 워크만 학습되는 편향을 없앤다.
        order_chunks = list(range(len(walks)))
        rng.shuffle(order_chunks)
        for ci in order_chunks:
            w = walks[ci]
            km = None
            if keep_mask is not None:
                km = rng.random(num_nodes) < keep_mask
            center, context = _make_pairs(w, window, rng, keep_mask=km)
            if max_pairs_per_epoch and ep_pairs + center.shape[0] > max_pairs_per_epoch:
                room = max(0, int(max_pairs_per_epoch) - ep_pairs)
                center, context = center[:room], context[:room]
            if center.shape[0] == 0:
                continue
            for s in range(0, center.shape[0], batch_size):
                c = center[s:s + batch_size]
                o = context[s:s + batch_size]
                b = c.shape[0]
                cur_lr = max(min_lr, lr * (1.0 - seen / float(total_pairs)))

                neg = np.searchsorted(neg_cum, rng.random((b, negatives))).astype(np.int64)
                np.clip(neg, 0, num_nodes - 1, out=neg)

                v = W_in[c]                                  # (b,d)
                u_pos = W_out[o]                             # (b,d)
                u_neg = W_out[neg]                           # (b,k,d)

                s_pos = np.einsum("bd,bd->b", v, u_pos)
                s_neg = np.einsum("bd,bkd->bk", v, u_neg)
                sig_pos = _sigmoid(s_pos)
                sig_neg = _sigmoid(s_neg)

                ep_loss += float(
                    -np.log(np.maximum(sig_pos, 1e-10)).sum()
                    - np.log(np.maximum(1.0 - sig_neg, 1e-10)).sum()
                ) / b
                ep_batches += 1

                g_pos = (sig_pos - 1.0).astype(np.float32)          # (b,)
                g_neg = sig_neg.astype(np.float32)                  # (b,k)

                grad_v = g_pos[:, None] * u_pos + np.einsum("bk,bkd->bd", g_neg, u_neg)
                grad_upos = g_pos[:, None] * v
                grad_uneg = g_neg[:, :, None] * v[:, None, :]
                # 노드별 누적 스텝 상한(학습률에 비례해 함께 감쇠)
                cap = (cur_lr * clip_grad) if (clip_grad and clip_grad > 0) else None
                _scatter_add(W_in, c, (-cur_lr * grad_v).astype(np.float32),
                             max_row_norm=cap)
                _scatter_add(W_out, o, (-cur_lr * grad_upos).astype(np.float32),
                             max_row_norm=cap)
                _scatter_add(W_out, neg.reshape(-1),
                             (-cur_lr * grad_uneg).reshape(-1, dim).astype(np.float32),
                             max_row_norm=cap)
                seen += b
                ep_pairs += b
            if max_pairs_per_epoch and ep_pairs >= max_pairs_per_epoch:
                break
        mean_loss = ep_loss / max(1, ep_batches)
        loss_history.append(round(mean_loss, 6))
        el = _time.time() - t0
        log.info("SGNS epoch %d/%d loss=%.4f pairs=%d lr=%.5f (%.1fs, %.0f pairs/s)",
                 ep + 1, epochs, mean_loss, ep_pairs,
                 max(min_lr, lr * (1.0 - seen / float(total_pairs))), el,
                 seen / max(1e-9, el))

    stats = {
        "tokens": tokens,
        "pairs_trained": int(seen),
        "visited_nodes": int((counts > 0).sum()),
        "num_nodes": int(num_nodes),
        "dim": int(dim),
        "window": int(window),
        "negatives": int(negatives),
        "epochs": int(epochs),
        "elapsed_sec": round(_time.time() - t0, 1),
    }
    return W_in, stats, loss_history


def train_node2vec(
    graph: Any,
    dim: int = 128,
    walks_per_node: int = 10,
    walk_len: int = 40,
    window: int = 5,
    epochs: int = 5,
    negatives: int = 5,
    *,
    p: float = 1.0,
    q: float = 1.0,
    lr: float = 0.025,
    min_lr: float = 1e-4,
    batch_size: int = 16384,
    subsample: float = 1e-3,
    clip_grad: float = 5.0,
    edge_weights: Optional[dict[str, float]] = None,
    metapath: Optional[Sequence[str] | str] = None,
    metapaths: Optional[Sequence[Sequence[str] | str]] = None,
    min_degree: int = 1,
    keep_kinds: Optional[Iterable[str]] = None,
    max_start_nodes: Optional[int] = None,
    max_pairs_per_epoch: Optional[int] = None,
    model_name: Optional[str] = None,
    seed: int = 20260819,
    logger: Any = None,
    graph_arrays: Optional[GraphArrays] = None,
) -> EmbeddingResult:
    """node2vec / DeepWalk / metapath2vec 학습 → EmbeddingResult(dict[node_id, vector]).

    graph: graph.build.build_graph() 결과(networkx 또는 폴백) 또는 GraphArrays.
    p, q : node2vec 2차 워크 파라미터. p=q=1 이면 DeepWalk 와 동일.
    metapath / metapaths: 라벨 시퀀스(또는 METAPATHS 키). 지정 시 해당 스키마를 따르는
        워크만 생성한다(이종그래프 metapath2vec). 여러 개면 코퍼스를 합친다.
    max_start_nodes: 워크 시작노드 샘플 상한(그래프 자체는 그대로 유지 — 구조 보존 샘플링).
    max_pairs_per_epoch: 에폭당 학습 페어 상한(대규모 그래프 시간 제어).
    """
    _require_numpy()
    np = _np
    log = logger or LOG
    rng = np.random.default_rng(seed)

    if isinstance(graph, GraphArrays):
        ga = graph
    elif graph_arrays is not None:
        ga = graph_arrays
    else:
        ga = GraphArrays.from_graph(graph, edge_weights=edge_weights,
                                    min_degree=min_degree, keep_kinds=keep_kinds,
                                    logger=log)

    schemas: list[Optional[list[str]]] = []
    raw_paths: list = []
    if metapaths:
        raw_paths = list(metapaths)
    elif metapath:
        raw_paths = [metapath]
    for mp in raw_paths:
        if isinstance(mp, str):
            if mp not in METAPATHS:
                raise ValueError(f"알 수 없는 metapath 키: {mp} (가능: {sorted(METAPATHS)})")
            schemas.append(list(METAPATHS[mp]))
        else:
            schemas.append(list(mp))
    if not schemas:
        schemas = [None]

    starts = np.arange(ga.num_nodes, dtype=np.int64)
    if max_start_nodes and max_start_nodes < starts.shape[0]:
        starts = np.sort(rng.choice(starts, size=int(max_start_nodes), replace=False))
        log.info("시작노드 샘플링: %d → %d (그래프 구조는 전량 유지)",
                 ga.num_nodes, starts.shape[0])

    all_walks: list = []
    for sc in schemas:
        for chunk in generate_walks(ga, walks_per_node=walks_per_node,
                                    walk_len=walk_len, p=p, q=q,
                                    start_nodes=starts, metapath=sc,
                                    rng=rng, logger=log):
            all_walks.append(chunk)

    W, stats, loss_history = train_sgns(
        all_walks, ga.num_nodes, dim=dim, window=window, epochs=epochs,
        negatives=negatives, lr=lr, min_lr=min_lr, batch_size=batch_size,
        subsample=subsample, clip_grad=clip_grad,
        max_pairs_per_epoch=max_pairs_per_epoch, seed=seed, logger=log)

    name = model_name or (
        "metapath2vec-numpy" if raw_paths else
        ("deepwalk-numpy" if (p == 1.0 and q == 1.0) else "node2vec-numpy"))
    stats.update({
        "p": float(p), "q": float(q),
        "walks_per_node": int(walks_per_node), "walk_len": int(walk_len),
        "metapaths": ["-".join(s) if s else "free" for s in schemas],
        "start_nodes": int(starts.shape[0]),
        "graph": dict(ga.meta),
    })
    return EmbeddingResult(W, ga.nodes, labels=ga.labels, label_names=ga.label_names,
                           model_name=name, stats=stats, loss_history=loss_history)


# --------------------------------------------------------------------------- #
# 메모리 내 Top-k
# --------------------------------------------------------------------------- #
def most_similar(result: EmbeddingResult, node_id: str, *, k: int = 10,
                 kinds: Optional[Iterable[str]] = None,
                 candidates: Optional[Sequence[str]] = None) -> list[dict]:
    """임베딩 코사인 Top-k(자기 제외). kinds 로 후보 라벨 제한 가능."""
    _require_numpy()
    np = _np
    i = result.index.get(node_id)
    if i is None:
        return []
    U = result.unit_matrix()
    if candidates is not None:
        cand_idx = np.array([result.index[c] for c in candidates if c in result.index],
                            dtype=np.int64)
    elif kinds:
        cand_idx = np.flatnonzero(result.kind_mask(kinds))
    else:
        cand_idx = np.arange(len(result.nodes), dtype=np.int64)
    cand_idx = cand_idx[cand_idx != i]
    if cand_idx.size == 0:
        return []
    sims = U[cand_idx] @ U[i]
    kk = min(int(k), cand_idx.shape[0])
    top = np.argpartition(-sims, kk - 1)[:kk]
    top = top[np.argsort(-sims[top])]
    out = []
    for rank, t in enumerate(top.tolist(), start=1):
        j = int(cand_idx[t])
        out.append({"rank": rank, "node_id": result.nodes[j],
                    "cosine": round(float(sims[t]), 6)})
    return out


def top_k_matrix(U, idx, cand_idx, k: int, *, block: int = 512):
    """블록 행렬곱 kNN. (rows, cols, sims) 반환 — 대규모 전쌍 비교 메모리 제어."""
    np = _np
    rows_out, cols_out, sims_out = [], [], []
    Uc = U[cand_idx]
    for s in range(0, idx.shape[0], block):
        blk = idx[s:s + block]
        S = U[blk] @ Uc.T
        # 자기 자신 제외
        self_pos = np.searchsorted(cand_idx, blk)
        valid = (self_pos < cand_idx.shape[0]) & (cand_idx[np.minimum(self_pos, cand_idx.shape[0] - 1)] == blk)
        S[np.flatnonzero(valid), self_pos[valid]] = -2.0
        kk = min(int(k), S.shape[1])
        part = np.argpartition(-S, kk - 1, axis=1)[:, :kk]
        take = np.take_along_axis(S, part, axis=1)
        order = np.argsort(-take, axis=1)
        part = np.take_along_axis(part, order, axis=1)
        take = np.take_along_axis(take, order, axis=1)
        rows_out.append(np.repeat(blk, kk))
        cols_out.append(cand_idx[part].reshape(-1))
        sims_out.append(take.reshape(-1))
    if not rows_out:
        return (np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0, np.float32))
    return (np.concatenate(rows_out), np.concatenate(cols_out),
            np.concatenate(sims_out))


# --------------------------------------------------------------------------- #
# DB 영속화
# --------------------------------------------------------------------------- #
_DDL_NODE_EMB = """
CREATE TABLE IF NOT EXISTS node_embeddings (
  node_id     TEXT NOT NULL,
  model_name  TEXT NOT NULL,
  node_kind   TEXT,
  dim         INTEGER,
  encoding    TEXT,
  vector      TEXT,
  norm        REAL,
  run_id      TEXT,
  computed_at TEXT,
  PRIMARY KEY (node_id, model_name)
)
"""
_DDL_NODE_EMB_IX = (
    "CREATE INDEX IF NOT EXISTS ix_nodeemb_model "
    "ON node_embeddings(model_name, node_kind)"
)
_DDL_NEUR_SIM = """
CREATE TABLE IF NOT EXISTS neural_similarity (
  src_id      TEXT NOT NULL,
  dst_id      TEXT NOT NULL,
  model_name  TEXT NOT NULL,
  cosine_sim  REAL NOT NULL,
  rank        INTEGER,
  node_kind   TEXT,
  computed_at TEXT,
  PRIMARY KEY (src_id, dst_id, model_name)
)
"""
_DDL_NEUR_SIM_IX = (
    "CREATE INDEX IF NOT EXISTS ix_neursim_src "
    "ON neural_similarity(model_name, src_id, rank)",
    "CREATE INDEX IF NOT EXISTS ix_neursim_dst "
    "ON neural_similarity(model_name, dst_id)",
)


def ensure_neural_tables(conn: sqlite3.Connection) -> None:
    """node_embeddings / neural_similarity 테이블 생성(멱등).

    기존 db/schema.sql 은 수정하지 않는다(병렬 작업 충돌 방지). 신경망 산출물 전용
    테이블만 CREATE TABLE IF NOT EXISTS 로 추가한다.
    """
    conn.execute(_DDL_NODE_EMB)
    conn.execute(_DDL_NODE_EMB_IX)
    conn.execute(_DDL_NEUR_SIM)
    for ddl in _DDL_NEUR_SIM_IX:
        conn.execute(ddl)
    conn.commit()


def encode_vector(vec, encoding: str = "f32b64") -> str:
    """벡터 직렬화. 'f32b64'=float32 리틀엔디안 base64(기본, 컴팩트) | 'json'=JSON 배열."""
    if encoding == "json":
        return json.dumps([round(float(x), 6) for x in list(vec)], ensure_ascii=False)
    arr = _np.asarray(vec, dtype="<f4") if _HAS_NUMPY else None
    if arr is None:  # pragma: no cover
        raise RuntimeError("f32b64 인코딩에는 numpy 가 필요하다")
    return base64.b64encode(arr.tobytes()).decode("ascii")


def decode_vector(raw: Any, encoding: Optional[str] = None) -> list[float]:
    """node_embeddings.vector → list[float]. encoding 미지정 시 자동 판별."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    text = str(raw)
    enc = encoding or ("json" if text.lstrip().startswith("[") else "f32b64")
    if enc == "json":
        try:
            return [float(x) for x in json.loads(text)]
        except (ValueError, TypeError, json.JSONDecodeError):
            return []
    try:
        buf = base64.b64decode(text)
    except Exception:  # noqa: BLE001
        return []
    if _HAS_NUMPY:
        return _np.frombuffer(buf, dtype="<f4").astype(float).tolist()
    import struct  # pragma: no cover - numpy 부재 폴백
    return list(struct.unpack("<" + "f" * (len(buf) // 4), buf))


def save_node_embeddings(
    conn: sqlite3.Connection,
    result: EmbeddingResult,
    *,
    model_name: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
    node_ids: Optional[Iterable[str]] = None,
    encoding: str = "f32b64",
    run_id: Optional[str] = None,
    batch_size: int = 2000,
    logger: Any = None,
) -> dict:
    """학습 임베딩 → node_embeddings 적재(배치 커밋, 동시쓰기 안전).

    kinds/node_ids 로 저장 대상을 좁힐 수 있다(대량 노드의 DB 팽창 제어).
    반환 {'saved','inserted','updated','unchanged','model','dim','status'}.
    """
    _require_numpy()
    np = _np
    log = logger or LOG
    ensure_neural_tables(conn)
    model = model_name or result.model_name
    now = _util.now_kst_iso()

    if node_ids is not None:
        sel = np.array([result.index[n] for n in node_ids if n in result.index],
                       dtype=np.int64)
    elif kinds:
        sel = np.flatnonzero(result.kind_mask(kinds))
    else:
        sel = np.arange(len(result.nodes), dtype=np.int64)

    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    rows: list[dict] = []
    total = 0
    for pos, i in enumerate(sel.tolist()):
        vec = result.matrix[i]
        kind = (result.label_names[int(result.labels[i])]
                if result.labels is not None and result.label_names else None)
        rows.append({
            "node_id": result.nodes[i],
            "model_name": model,
            "node_kind": kind,
            "dim": int(vec.shape[0]),
            "encoding": encoding,
            "vector": encode_vector(vec, encoding),
            "norm": float(np.linalg.norm(vec)),
            "run_id": run_id,
            "computed_at": now,
        })
        if len(rows) >= batch_size:
            with _db.tx(conn):
                c = _db.upsert_many(conn, "node_embeddings", rows,
                                    ("node_id", "model_name"))
            for k in counts:
                counts[k] += c[k]
            total += len(rows)
            rows = []
            if total % (batch_size * 10) == 0:
                log.info("  node_embeddings 저장 %d/%d", total, sel.shape[0])
    if rows:
        with _db.tx(conn):
            c = _db.upsert_many(conn, "node_embeddings", rows, ("node_id", "model_name"))
        for k in counts:
            counts[k] += c[k]
        total += len(rows)

    log.info("node_embeddings 저장 완료: %d행 (%s)", total, model)
    return {"saved": total, **counts, "model": model,
            "dim": int(result.dim), "status": "ok"}


def load_node_embeddings(conn: sqlite3.Connection, model_name: str, *,
                         node_kind: Optional[str] = None,
                         limit: Optional[int] = None) -> dict[str, list[float]]:
    """node_embeddings → {node_id: vector}."""
    sql = "SELECT node_id, encoding, vector FROM node_embeddings WHERE model_name=?"
    params: list = [model_name]
    if node_kind:
        sql += " AND node_kind=?"
        params.append(node_kind)
    if limit:
        sql += f" LIMIT {int(limit)}"
    out: dict[str, list[float]] = {}
    for r in _db.fetchall(conn, sql, params):
        out[r["node_id"]] = decode_vector(r["vector"], r.get("encoding"))
    return out


def _flush_sim(conn, payload: list, counts: dict, fast_insert: bool) -> int:
    """neural_similarity 배치 적재. fast_insert=True 면 executemany INSERT OR REPLACE."""
    if not payload:
        return 0
    if fast_insert:
        cols = ("src_id", "dst_id", "model_name", "cosine_sim",
                "rank", "node_kind", "computed_at")
        sql = ("INSERT OR REPLACE INTO neural_similarity "
               "(src_id, dst_id, model_name, cosine_sim, rank, node_kind, computed_at) "
               "VALUES (?,?,?,?,?,?,?)")
        with _db.tx(conn):
            conn.executemany(sql, [tuple(r[c] for c in cols) for r in payload])
        counts["inserted"] += len(payload)
        return len(payload)
    with _db.tx(conn):
        cc = _db.upsert_many(conn, "neural_similarity", payload,
                             ("src_id", "dst_id", "model_name"))
    for k in counts:
        counts[k] += cc[k]
    return len(payload)


def build_neural_similarity(
    conn: sqlite3.Connection,
    result: EmbeddingResult,
    *,
    top_k: int = 10,
    kinds: Iterable[str] = ("Ordinance",),
    node_ids: Optional[Iterable[str]] = None,
    candidate_ids: Optional[Iterable[str]] = None,
    model_name: Optional[str] = None,
    max_items: Optional[int] = 6000,
    min_cosine: float = 0.0,
    replace: bool = True,
    fast_insert: bool = True,
    block: int = 256,
    batch_size: int = 5000,
    seed: int = 20260819,
    logger: Any = None,
) -> dict:
    """임베딩 코사인 kNN → neural_similarity 적재.

    max_items: 전쌍 비교 대상 상한(N^2 폭발 방지). 초과 시 결정적 샘플링.
              None 이면 상한 없음(candidate_ids 를 전량 쓰고 싶을 때).
              **상한이 낮으면 most_similar_db 조회가 빈 결과를 돌려준다** —
              대상에 뽑히지 못한 노드에는 행 자체가 없기 때문이다. 조회 커버리지가
              필요하면 None(전량) 으로 두라.
    fast_insert: PK 가 (src_id,dst_id,model_name) 이므로 executemany
              INSERT OR REPLACE 로 일괄 적재(행별 upsert 대비 수십 배 빠름).
              inserted/updated/unchanged 구분은 포기하고 written 만 센다.
    block   : kNN 블록 행렬곱의 행 블록 크기(메모리 제어).
    replace : 같은 model_name 의 기존 행을 먼저 지운다(스테일 링크 잔존 방지).
    """
    _require_numpy()
    np = _np
    log = logger or LOG
    ensure_neural_tables(conn)
    model = model_name or result.model_name
    now = _util.now_kst_iso()

    if node_ids is not None:
        idx = np.array([result.index[n] for n in node_ids if n in result.index],
                       dtype=np.int64)
    else:
        idx = np.flatnonzero(result.kind_mask(kinds))
    if candidate_ids is not None:
        cand = np.array([result.index[n] for n in candidate_ids if n in result.index],
                        dtype=np.int64)
    else:
        cand = idx
    if max_items and idx.shape[0] > max_items:
        rng = np.random.default_rng(seed)
        idx = np.sort(rng.choice(idx, size=int(max_items), replace=False))
        log.info("neural_similarity 대상 샘플링: %d개로 축소", idx.shape[0])
    if max_items and cand.shape[0] > max_items:
        rng = np.random.default_rng(seed)
        cand = np.sort(rng.choice(cand, size=int(max_items), replace=False))
    cand = np.sort(cand)

    if idx.size == 0 or cand.size == 0:
        return {"nodes": 0, "pairs": 0, "model": model, "status": "ok"}

    U = result.unit_matrix()
    rows_i, cols_i, sims = top_k_matrix(U, idx, cand, top_k, block=block)

    if replace:
        with _db.tx(conn):
            conn.execute("DELETE FROM neural_similarity WHERE model_name=?", (model,))

    payload: list[dict] = []
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    written = 0
    rank = 0
    prev_row = -1
    for r, c, s in zip(rows_i.tolist(), cols_i.tolist(), sims.tolist()):
        if r != prev_row:
            rank = 0
            prev_row = r
        rank += 1
        if s < min_cosine or s <= -1.5:
            continue
        kind = (result.label_names[int(result.labels[r])]
                if result.labels is not None and result.label_names else None)
        payload.append({
            "src_id": result.nodes[r], "dst_id": result.nodes[c],
            "model_name": model, "cosine_sim": round(float(s), 6),
            "rank": rank, "node_kind": kind, "computed_at": now,
        })
        if len(payload) >= batch_size:
            written += _flush_sim(conn, payload, counts, fast_insert)
            payload = []
    if payload:
        written += _flush_sim(conn, payload, counts, fast_insert)

    log.info("neural_similarity: nodes=%d pairs=%d (%s)", idx.shape[0], written, model)
    return {"nodes": int(idx.shape[0]), "pairs": written, **counts,
            "model": model, "top_k": int(top_k), "status": "ok"}


def most_similar_db(conn: sqlite3.Connection, node_id: str, *, k: int = 10,
                    model_name: Optional[str] = None) -> list[dict]:
    """저장된 neural_similarity 에서 Top-k 조회."""
    sql = ("SELECT dst_id, cosine_sim, rank, model_name FROM neural_similarity "
           "WHERE src_id=?")
    params: list = [node_id]
    if model_name:
        sql += " AND model_name=?"
        params.append(model_name)
    sql += " ORDER BY rank LIMIT ?"
    params.append(int(k))
    return _db.fetchall(conn, sql, params)


__all__ = [
    "GraphArrays", "EmbeddingResult", "DEFAULT_EDGE_WEIGHTS", "METAPATHS",
    "generate_walks", "train_sgns", "train_node2vec",
    "most_similar", "top_k_matrix",
    "ensure_neural_tables", "encode_vector", "decode_vector",
    "save_node_embeddings", "load_node_embeddings",
    "build_neural_similarity", "most_similar_db",
]
