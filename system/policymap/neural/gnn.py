"""policymap.neural.gnn — GraphSAGE 스타일 메시지패싱 GNN(numpy 자체구현).

torch 없이 numpy 만으로 순전파·역전파(수동 미분)·Adam 을 직접 구현한다.

구성
  1) 초기 피처(build_node_features)
     * 조례 : parsers.embedding.Embedder(문자 n-gram TF) 재사용 → 부호해싱 투영
              + 카테고리 원핫 + 제정연도 + 조문수
     * 지역 : 인구 / 조례수 / 예산(편성·지출) / 레벨 / 차수 정규화
     * 예산·의안·의원 등 : 명칭 텍스트 해시 + 금액/연도 스칼라
     * 라벨 원핫으로 이종 노드타입을 구분
  2) 인코더(GraphSAGE)
     h^k_v = L2( act( h^{k-1}_v W_self^k + AGG_{u∈N(v)}(h^{k-1}_u) W_neigh^k + b^k )
                 + h^{k-1}_v W_res^k )
     W_self·h_self + W_neigh·AGG 는 원논문의 W·CONCAT(h_self, AGG) 와 수식상 동일.
     AGG ∈ {mean, max}. 층수 2(기본), 층마다 L2 정규화, 잔차(skip) 연결, fanout 표본추출.
     최종 표현은 **JK-Net concat**:
       h = concat( L2(X·Wj), L2(h¹), L2(h²) ) / sqrt(3)
     — 이 그래프의 과평활 원인에 대한 직접 처방이다(아래 3) 참조).

  2-1) 과평활(over-smoothing) 원인과 처방 — 코드/데이터 실측
     원인은 층수도 정규화 누락도 아니고 **그래프 구조 + 목적함수**였다.
       · 조례 노드의 중앙차수 = 1 (Region→Ordinance 한 개뿐).
         → 조례에 대한 AGG(X) 는 소속 지역 벡터 537종으로만 결정되고,
           그 537개 벡터가 span 하는 유효차원은 3.4 다(학습 전 실측).
       · 링크예측 BCE 는 '조례가 자기 지역처럼 보이면' 손실이 줄기 때문에
         최적화가 W_neigh 를 키우고 W_self 를 죽인다 → 조례 임베딩이
         이웃항(rank 3)에 흡수되어 유효차원 128 → 3.0 으로 붕괴.
         (그 결과 유사조례 Top-1 코사인이 74~76% 노드에서 0.999 초과.)
       · 잔차연결(W_res)만으로는 부족했다 — 학습이 그 경로도 같이 죽인다(실측 rank 3.01).
     처방: JK-concat 은 X 블록을 **구조적으로** 최종 벡터에 남기므로 최적화가
     지울 수 없다. 실측 유효차원 3.01 → 23.68, Top-1 코사인 0.9997 → 0.9243,
     0.999 초과 비율 0.76 → 0.000, held-out AUC 0.5901 → 0.6578.
  3) 비지도 목적함수(링크예측)
     연결쌍은 가깝게, negative 는 멀게 — sigmoid(scale·cos + bias) + BCE.
     ∂L/∂s = σ(s) - y 부터 층별로 손으로 미분해 역전파한다.
     negative 는 기본이 **타입정합(type-matched)** 이다. 이종그래프에서 균등 negative 는
     거의 전부 '존재하지 않는 타입조합'이라 노드타입만 구분해도 손실이 줄고 표현이
     붕괴한다(실측: 조례 간 코사인이 전부 ~0.995). train_graphsage 독스트링의
     비교표 참조.
  4) 평가
     held-out 엣지(기본 10%)를 메시지패싱에서 **제거**하고 학습한 뒤 AUC 측정.
     학습 전(랜덤 초기화) AUC 및 랜덤 베이스라인 0.5 와 비교한다.
     evaluate_link_prediction(negative_sampling='type-matched') 는 negative 의 라벨쌍을
     positive 와 맞춘 엄격 평가(타입 구분만으로는 점수가 오르지 않는다).

공개 API:
    build_node_features(conn, ga, *, text_dim=64, ...) -> (X, meta)
    split_edges(ga, *, test_frac=0.1, seed=...) -> dict
    train_graphsage(graph, features=None, dim=128, layers=2, epochs=120, lr=0.01, ...)
        -> GraphSageResult (dict[node_id, vector] + .loss_history / .auc / .auc_init)
    evaluate_link_prediction(H, ga, pos_src, pos_dst, *, negative_sampling=...) -> dict
    sample_negative_pairs(ga, num, rng, *, like_src=, like_dst=) -> (u, v)
    label_pools(ga) -> dict[label_id, node_index_array]
    category_cohesion(result, conn) -> dict      # 같은/다른 카테고리 평균 코사인 대조
    roc_auc(pos_scores, neg_scores) -> float
"""
from __future__ import annotations

import sqlite3
import time
import zlib
from typing import Any, Iterable, Optional, Sequence

from .. import db as _db
from .. import util as _util
from ..parsers import embedding as _pemb
from . import embeddings as _emb
from .embeddings import GraphArrays, _require_numpy, _scatter_add, _sigmoid

try:
    import numpy as _np  # type: ignore
    _HAS_NUMPY = True
except Exception:  # pragma: no cover
    _np = None  # type: ignore
    _HAS_NUMPY = False

LOG = _util.get_logger("policymap.neural")

_DEFAULT_MODEL = "graphsage-numpy"


# --------------------------------------------------------------------------- #
# 초기 피처
# --------------------------------------------------------------------------- #
def _hash_text(text: str, dim: int, embedder: "_pemb.Embedder", out) -> None:
    """문자 n-gram TF(parsers.embedding 재사용) → 부호해싱 dim 차원 투영(L2 정규화).

    crc32 는 결정적(PYTHONHASHSEED 무관)이라 재현 가능하다.
    """
    tf = embedder.embed(text)
    if not isinstance(tf, dict):  # sbert dense 백엔드면 앞쪽 dim 만 사용
        vals = [float(x) for x in tf][:dim]
        out[:len(vals)] = vals
    else:
        for g, w in tf.items():
            h = zlib.crc32(g.encode("utf-8"))
            out[h % dim] += float(w) * (1.0 if (h >> 20) & 1 else -1.0)
    n = float((out * out).sum()) ** 0.5
    if n > 0:
        out /= n


def build_node_features(
    conn: Optional[sqlite3.Connection],
    ga: GraphArrays,
    *,
    text_dim: int = 64,
    embedder: Optional["_pemb.Embedder"] = None,
    article_chars: int = 1200,
    logger: Any = None,
) -> tuple:
    """노드 초기 피처 행렬 X(float32 N×D) 와 블록 메타를 만든다.

    블록: [라벨 원핫 L | 텍스트해시 text_dim | 카테고리 원핫 C | 수치 K(z-score)]
    conn 이 None 이면 그래프 노드 속성만으로 구성한다(DB 비의존 단위테스트 가능).
    """
    _require_numpy()
    np = _np
    log = logger or LOG
    t0 = time.time()
    emb = embedder or _pemb.Embedder()
    n = ga.num_nodes
    L = len(ga.label_names)

    # --- 조례 조문 텍스트(있는 것만) 벌크 로드 ---
    art_text: dict[str, str] = {}
    cat_index: dict[str, int] = {}
    ord_cat: dict[str, list[int]] = {}
    if conn is not None:
        try:
            for r in _db.fetchall(
                    conn, "SELECT ordinance_id, title, body FROM ordinance_articles"):
                oid = r["ordinance_id"]
                cur = art_text.get(oid, "")
                if len(cur) >= article_chars:
                    continue
                piece = " ".join(x for x in (r.get("title"), r.get("body")) if x)
                art_text[oid] = (cur + " " + piece)[:article_chars]
        except sqlite3.OperationalError:
            pass
        try:
            for r in _db.fetchall(conn, "SELECT code FROM categories ORDER BY code"):
                cat_index[r["code"]] = len(cat_index)
            for r in _db.fetchall(
                    conn, "SELECT ordinance_id, category_code FROM ordinance_category"):
                j = cat_index.get(r["category_code"])
                if j is not None:
                    ord_cat.setdefault(r["ordinance_id"], []).append(j)
        except sqlite3.OperationalError:
            pass
    C = len(cat_index)

    # --- 수치 피처 열 정의 ---
    num_cols = ["log_degree", "year", "log_article_count", "log_population",
                "log_budget_now", "log_exe_amt", "region_level", "has_legislation"]
    K = len(num_cols)

    D = L + text_dim + C + K
    X = np.zeros((n, D), dtype=np.float32)
    off_text = L
    off_cat = L + text_dim
    off_num = off_cat + C

    def _year(val: Any) -> float:
        s = str(val or "")
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) >= 4:
            try:
                y = int(digits[:4])
                if 1900 <= y <= 2100:
                    return float(y)
            except ValueError:
                return 0.0
        return 0.0

    for i in range(n):
        attr = ga.attrs[i]
        lab = int(ga.labels[i])
        X[i, lab] = 1.0

        nid = ga.nodes[i]
        name = str(attr.get("name") or attr.get("full_name") or "")
        text = name
        if lab < L and ga.label_names[lab] == "Ordinance":
            src = attr.get("src_id") or nid.split(":", 1)[-1]
            extra = art_text.get(str(src)) or art_text.get(str(nid).split(":", 1)[-1])
            if extra:
                text = name + " " + extra
            for j in ord_cat.get(str(src), ()):  # 카테고리 원핫
                X[i, off_cat + j] = 1.0
        if text:
            _hash_text(text, text_dim, emb, X[i, off_text:off_text + text_dim])

        X[i, off_num + 0] = float(np.log1p(float(ga.degree[i])))
        X[i, off_num + 1] = _year(attr.get("enacted_on") or attr.get("effective_on")
                                  or attr.get("propose_dt") or attr.get("fyr"))
        X[i, off_num + 2] = float(np.log1p(max(0.0, float(attr.get("article_count") or 0))))
        X[i, off_num + 3] = float(np.log1p(max(0.0, float(attr.get("population") or 0))))
        X[i, off_num + 4] = float(np.log1p(max(0.0, float(attr.get("budget_now") or 0))))
        X[i, off_num + 5] = float(np.log1p(max(0.0, float(attr.get("exe_amt") or 0))))
        X[i, off_num + 6] = float(attr.get("level") or 0)
        X[i, off_num + 7] = 1.0 if attr.get("has_legislation") else 0.0

    # 수치 블록만 z-score(원핫/텍스트는 이미 스케일이 정돈됨)
    blk = X[:, off_num:off_num + K]
    mu = blk.mean(axis=0)
    sd = blk.std(axis=0)
    sd[sd < 1e-6] = 1.0
    X[:, off_num:off_num + K] = (blk - mu) / sd

    meta = {
        "dim": int(D),
        "blocks": {"label_onehot": L, "text_hash": text_dim,
                   "category_onehot": C, "numeric": K},
        "numeric_cols": num_cols,
        "label_names": list(ga.label_names),
        "categories": list(cat_index.keys()),
        "text_model": emb.model_name,
        "ordinances_with_articles": len(art_text),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    log.info("초기 피처: X=%s (라벨%d + 텍스트%d + 카테고리%d + 수치%d), %.1fs",
             X.shape, L, text_dim, C, K, meta["elapsed_sec"])
    return X, meta


# --------------------------------------------------------------------------- #
# 메시지패싱 구조(train 엣지만으로 구성 → 평가 누수 차단)
# --------------------------------------------------------------------------- #
class _MessagePassing:
    """대칭화 CSR + 청크 reduceat 기반 이웃 집계(mean/max) 및 그 역전파.

    fanout: 노드당 이웃 표본 상한(원논문 GraphSAGE 의 neighbor sampling).
        None 이면 전체 이웃을 쓴다(대칭 CSR → 빠른 역전파 경로).
        정수를 주면 차수가 fanout 을 넘는 허브(이 그래프에서는 Region·Legislator)의
        이웃을 결정적으로 표본추출한다. 이때 인접관계가 **비대칭**이 되므로
        mean 역전파는 역방향 CSR(rev_*)로 정확히 계산한다(대칭 가정 금지).
    """

    def __init__(self, num_nodes: int, src, dst, *, chunk_edges: int = 250_000,
                 fanout: Optional[int] = None, seed: int = 20260819):
        np = _np
        n = int(num_nodes)
        s = np.concatenate([np.asarray(src, dtype=np.int64),
                            np.asarray(dst, dtype=np.int64)])
        d = np.concatenate([np.asarray(dst, dtype=np.int64),
                            np.asarray(src, dtype=np.int64)])
        order = np.argsort(s, kind="stable")
        self.n = n
        self.indices = d[order].astype(np.int32)
        counts = np.bincount(s, minlength=n)
        self.indptr = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
        self.deg = counts.astype(np.int64)
        self.symmetric = True
        self.fanout = int(fanout) if fanout else None
        if self.fanout:
            self._apply_fanout(self.fanout, seed)
        self.nz = np.flatnonzero(self.deg > 0)
        self.inv_deg = np.zeros(n, dtype=np.float32)
        self.inv_deg[self.nz] = 1.0 / self.deg[self.nz]
        self.chunk_edges = int(chunk_edges)
        self._blocks = self._make_blocks()
        if not self.symmetric:
            self._build_reverse()

    def _apply_fanout(self, fanout: int, seed: int) -> None:
        """차수 > fanout 인 행의 이웃을 fanout 개로 표본추출(비복원, 결정적 시드)."""
        np = _np
        hubs = np.flatnonzero(self.deg > fanout)
        if hubs.size == 0:
            return
        rng = np.random.default_rng(seed)
        keep_lists = []
        new_deg = self.deg.copy()
        for r in range(self.n):
            e0, e1 = int(self.indptr[r]), int(self.indptr[r + 1])
            if e1 - e0 > fanout:
                sel = rng.choice(e1 - e0, size=fanout, replace=False)
                sel.sort()
                keep_lists.append(self.indices[e0:e1][sel])
                new_deg[r] = fanout
            else:
                keep_lists.append(self.indices[e0:e1])
        self.indices = np.concatenate(keep_lists).astype(np.int32)
        self.deg = new_deg
        self.indptr = np.concatenate([[0], np.cumsum(self.deg)]).astype(np.int64)
        self.symmetric = False

    def _build_reverse(self) -> None:
        """역방향 CSR: rev_indices[rev_indptr[v]:...] = {u | v ∈ N(u)}."""
        np = _np
        rows = np.repeat(np.arange(self.n, dtype=np.int64), self.deg)
        cols = self.indices.astype(np.int64)
        order = np.argsort(cols, kind="stable")
        self.rev_indices = rows[order].astype(np.int32)
        rcounts = np.bincount(cols, minlength=self.n)
        self.rev_indptr = np.concatenate([[0], np.cumsum(rcounts)]).astype(np.int64)
        self.rev_deg = rcounts.astype(np.int64)

    def _make_blocks(self) -> list[tuple[int, int]]:
        np = _np
        out: list[tuple[int, int]] = []
        start = 0
        while start < self.n:
            target = self.indptr[start] + self.chunk_edges
            end = int(np.searchsorted(self.indptr, target, side="right")) - 1
            end = min(max(end, start + 1), self.n)
            out.append((start, end))
            start = end
        return out

    def _rows_of(self, r0: int, r1: int):
        np = _np
        rows = np.arange(r0, r1, dtype=np.int64)
        return rows[self.deg[r0:r1] > 0]

    # --- 순전파 ---
    def aggregate(self, H, mode: str = "mean"):
        np = _np
        out = np.zeros((self.n, H.shape[1]), dtype=np.float32)
        for r0, r1 in self._blocks:
            nz = self._rows_of(r0, r1)
            if nz.size == 0:
                continue
            e0 = int(self.indptr[r0])
            e1 = int(self.indptr[r1])
            gathered = H[self.indices[e0:e1]]
            starts = (self.indptr[nz] - e0).astype(np.intp)
            if mode == "max":
                out[nz] = np.maximum.reduceat(gathered, starts, axis=0)
            else:
                out[nz] = np.add.reduceat(gathered, starts, axis=0)
        if mode != "max":
            out *= self.inv_deg[:, None]
        return out

    # --- 역전파 ---
    def aggregate_backward(self, grad_out, H=None, agg_out=None, mode: str = "mean"):
        """grad_out(=∂L/∂agg) → ∂L/∂H.

        mean: 엣지집합이 대칭이면 ∂L/∂H[u] = Σ_{v∈N(u)} grad_out[v]/deg[v]
              → 같은 CSR 로 reduceat 재사용(scatter 불필요, 빠름).
              fanout 표본추출로 비대칭이면 역방향 CSR 로 정확히 계산한다.
        max : 최댓값을 낸 이웃에게만 흘린다(동률은 균등분배). scatter-add 사용.
        """
        np = _np
        grad_H = np.zeros((self.n, grad_out.shape[1]), dtype=np.float32)
        if mode != "max" and not self.symmetric:
            gd = (grad_out * self.inv_deg[:, None]).astype(np.float32)
            nzv = np.flatnonzero(self.rev_deg > 0)
            starts = self.rev_indptr[nzv].astype(np.intp)
            grad_H[nzv] = np.add.reduceat(gd[self.rev_indices], starts, axis=0)
            return grad_H
        if mode != "max":
            gd = (grad_out * self.inv_deg[:, None]).astype(np.float32)
            for r0, r1 in self._blocks:
                nz = self._rows_of(r0, r1)
                if nz.size == 0:
                    continue
                e0 = int(self.indptr[r0])
                e1 = int(self.indptr[r1])
                starts = (self.indptr[nz] - e0).astype(np.intp)
                grad_H[nz] = np.add.reduceat(gd[self.indices[e0:e1]], starts, axis=0)
            return grad_H
        for r0, r1 in self._blocks:
            nz = self._rows_of(r0, r1)
            if nz.size == 0:
                continue
            e0 = int(self.indptr[r0])
            e1 = int(self.indptr[r1])
            idx = self.indices[e0:e1].astype(np.int64)
            rows = np.repeat(nz, self.deg[nz])
            gathered = H[idx]
            mask = (gathered == agg_out[rows]).astype(np.float32)
            starts = (self.indptr[nz] - e0).astype(np.intp)
            cnt = np.add.reduceat(mask, starts, axis=0)
            cnt[cnt < 1.0] = 1.0
            contrib = mask * (grad_out[rows] / cnt[np.searchsorted(nz, rows)])
            _scatter_add(grad_H, idx, contrib.astype(np.float32))
        return grad_H


# --------------------------------------------------------------------------- #
# 수치 유틸(수동 미분)
# --------------------------------------------------------------------------- #
def _l2norm_forward(Z):
    np = _np
    nrm = np.linalg.norm(Z, axis=1, keepdims=True).astype(np.float32)
    nrm[nrm < 1e-8] = 1e-8
    return (Z / nrm).astype(np.float32), nrm


def _l2norm_backward(grad_H, H, nrm):
    """h = z/||z||  →  ∂L/∂z = (g - (g·h)h)/||z||."""
    np = _np
    proj = np.einsum("nd,nd->n", grad_H, H)[:, None]
    return ((grad_H - proj * H) / nrm).astype(np.float32)


class _Adam:
    """numpy Adam 옵티마이저(파라미터별 1·2차 모멘트)."""

    def __init__(self, params: dict, lr: float = 0.01,
                 b1: float = 0.9, b2: float = 0.999, eps: float = 1e-8):
        np = _np
        self.lr, self.b1, self.b2, self.eps = lr, b1, b2, eps
        self.m = {k: np.zeros_like(v) for k, v in params.items()}
        self.v = {k: np.zeros_like(v) for k, v in params.items()}
        self.t = 0

    def step(self, params: dict, grads: dict) -> None:
        np = _np
        self.t += 1
        bc1 = 1.0 - self.b1 ** self.t
        bc2 = 1.0 - self.b2 ** self.t
        for k, g in grads.items():
            if g is None:
                continue
            self.m[k] = self.b1 * self.m[k] + (1 - self.b1) * g
            self.v[k] = self.b2 * self.v[k] + (1 - self.b2) * (g * g)
            step = self.lr * (self.m[k] / bc1) / (np.sqrt(self.v[k] / bc2) + self.eps)
            params[k] -= step.astype(params[k].dtype)


# --------------------------------------------------------------------------- #
# 링크예측 평가
# --------------------------------------------------------------------------- #
def _rankdata(a):
    """평균 순위(동률 평균). scipy 없이 numpy 만으로."""
    np = _np
    order = np.argsort(a, kind="stable")
    ranks = np.empty(a.shape[0], dtype=np.float64)
    sorted_a = a[order]
    i = 0
    n = a.shape[0]
    while i < n:
        j = i + 1
        while j < n and sorted_a[j] == sorted_a[i]:
            j += 1
        ranks[order[i:j]] = 0.5 * (i + j - 1) + 1.0
        i = j
    return ranks


def roc_auc(pos_scores, neg_scores) -> float:
    """Mann-Whitney U 기반 ROC-AUC(동률 평균순위 반영). 랜덤 베이스라인 = 0.5."""
    _require_numpy()
    np = _np
    pos = np.asarray(pos_scores, dtype=np.float64).reshape(-1)
    neg = np.asarray(neg_scores, dtype=np.float64).reshape(-1)
    if pos.size == 0 or neg.size == 0:
        return float("nan")
    allv = np.concatenate([pos, neg])
    r = _rankdata(allv)
    rs = r[:pos.size].sum()
    return float((rs - pos.size * (pos.size + 1) / 2.0) / (pos.size * neg.size))


def average_precision(pos_scores, neg_scores) -> float:
    """AP(PR-AUC). 양성 비율이 베이스라인."""
    _require_numpy()
    np = _np
    pos = np.asarray(pos_scores, dtype=np.float64).reshape(-1)
    neg = np.asarray(neg_scores, dtype=np.float64).reshape(-1)
    y = np.concatenate([np.ones(pos.size), np.zeros(neg.size)])
    s = np.concatenate([pos, neg])
    order = np.argsort(-s, kind="stable")
    y = y[order]
    tp = np.cumsum(y)
    prec = tp / np.arange(1, y.size + 1)
    denom = max(1.0, float(y.sum()))
    return float((prec * y).sum() / denom)


def label_pools(ga: GraphArrays) -> dict:
    """라벨 ID → 해당 라벨 노드 인덱스 배열."""
    np = _np
    return {int(v): np.flatnonzero(ga.labels == v) for v in np.unique(ga.labels)}


def sample_negative_pairs(ga: GraphArrays, num: int, rng, *,
                          src_pool=None, dst_pool=None, max_tries: int = 12,
                          like_src=None, like_dst=None, pools: Optional[dict] = None):
    """실제 엣지가 아닌 (u,v) 쌍 샘플(기각 반복). 자기루프 제외.

    like_src/like_dst 를 주면 **타입정합(type-matched) negative** 를 만든다:
    negative 양끝의 라벨을 positive 와 같게 맞춘다. 이종그래프에서 균등 negative 는
    거의 전부 '존재하지 않는 타입조합'이라 모델이 노드타입만 구분해도 손실이 줄어든다
    (실측: 균등 negative 로 학습한 GraphSAGE 는 조례끼리 코사인이 전부 ~0.995 로 붕괴).
    타입정합 negative 는 같은 타입쌍 안에서 실제 연결을 가려내도록 강제한다.
    """
    np = _np
    n = ga.num_nodes
    typed = (like_src is not None) or (like_dst is not None)
    if typed:
        pools = pools or label_pools(ga)

        def _draw(like, size):
            lab = ga.labels[np.asarray(like, dtype=np.int64)]
            reps = int(np.ceil(size / max(1, lab.shape[0])))
            lab = np.tile(lab, reps)[:size]
            out = np.empty(size, dtype=np.int64)
            for lv in np.unique(lab):
                sel = np.flatnonzero(lab == lv)
                out[sel] = rng.choice(pools[int(lv)], size=sel.shape[0])
            return out

        u = _draw(like_src if like_src is not None else np.arange(n), num)
        v = _draw(like_dst if like_dst is not None else np.arange(n), num)
    else:
        src_pool = np.arange(n, dtype=np.int64) if src_pool is None else np.asarray(src_pool)
        dst_pool = np.arange(n, dtype=np.int64) if dst_pool is None else np.asarray(dst_pool)
        u = rng.choice(src_pool, size=num)
        v = rng.choice(dst_pool, size=num)
    for _ in range(max_tries):
        bad = (u == v) | ga.has_edge(u, v)
        k = int(bad.sum())
        if k == 0:
            break
        if typed:
            idxb = np.flatnonzero(bad)
            lu, lv2 = ga.labels[u[idxb]], ga.labels[v[idxb]]
            for lv in np.unique(lu):
                sel = idxb[lu == lv]
                u[sel] = rng.choice(pools[int(lv)], size=sel.shape[0])
            for lv in np.unique(lv2):
                sel = idxb[lv2 == lv]
                v[sel] = rng.choice(pools[int(lv)], size=sel.shape[0])
        else:
            u[bad] = rng.choice(src_pool, size=k)
            v[bad] = rng.choice(dst_pool, size=k)
    return u.astype(np.int64), v.astype(np.int64)


def split_edges(ga: GraphArrays, *, test_frac: float = 0.1, seed: int = 20260819) -> dict:
    """엣지를 train/test 로 분할. test 엣지는 메시지패싱에서 제외한다(누수 차단)."""
    _require_numpy()
    np = _np
    rng = np.random.default_rng(seed)
    m = ga.num_edges
    perm = rng.permutation(m)
    n_test = int(round(m * float(test_frac)))
    test_idx = perm[:n_test]
    train_idx = perm[n_test:]
    train_mask = np.zeros(m, dtype=bool)
    train_mask[train_idx] = True
    return {
        "train_src": ga.edge_src[train_idx].astype(np.int64),
        "train_dst": ga.edge_dst[train_idx].astype(np.int64),
        "test_src": ga.edge_src[test_idx].astype(np.int64),
        "test_dst": ga.edge_dst[test_idx].astype(np.int64),
        "train_idx": train_idx,
        "test_idx": test_idx,
        "train_mask": train_mask,   # ga.subset_edges(train_mask) 로 누수 없는 워크 그래프 생성
        "num_train": int(train_idx.shape[0]),
        "num_test": int(test_idx.shape[0]),
        "test_frac": float(test_frac),
    }


def evaluate_link_prediction(H, ga: GraphArrays, pos_src, pos_dst, *,
                             negatives: int = 1, seed: int = 7,
                             scale: float = 1.0, bias: float = 0.0,
                             negative_sampling: str = "uniform") -> dict:
    """held-out 엣지 AUC/AP. 점수 = scale·cos(H[u],H[v]) + bias (단조 → AUC 는 scale 무관).

    negative_sampling='type-matched' 면 negative 의 라벨쌍을 positive 와 맞춘다
    (노드타입 구분만으로 점수가 오르는 것을 막는 엄격 평가).
    """
    _require_numpy()
    np = _np
    rng = np.random.default_rng(seed)
    U = H / np.maximum(np.linalg.norm(H, axis=1, keepdims=True), 1e-8)
    pos = np.einsum("nd,nd->n", U[pos_src], U[pos_dst])
    m = int(pos_src.shape[0]) * int(negatives)
    if negative_sampling == "type-matched":
        nu, nv = sample_negative_pairs(ga, m, rng, like_src=pos_src, like_dst=pos_dst)
    else:
        nu, nv = sample_negative_pairs(ga, m, rng)
    neg = np.einsum("nd,nd->n", U[nu], U[nv])
    return {
        "auc": round(roc_auc(pos * scale + bias, neg * scale + bias), 4),
        "ap": round(average_precision(pos, neg), 4),
        "n_pos": int(pos.shape[0]), "n_neg": int(neg.shape[0]),
        "negative_sampling": negative_sampling,
        "pos_cos_mean": round(float(pos.mean()), 4),
        "neg_cos_mean": round(float(neg.mean()), 4),
        "random_baseline_auc": 0.5,
    }


# --------------------------------------------------------------------------- #
# GraphSAGE
# --------------------------------------------------------------------------- #
class GraphSageResult(dict):
    """dict[node_id, np.ndarray] + .loss_history/.auc/.auc_init/.stats/.matrix."""

    __slots__ = ("matrix", "nodes", "index", "labels", "label_names", "dim",
                 "model_name", "stats", "loss_history", "auc", "auc_init",
                 "params", "feature_meta")

    def __init__(self, matrix, nodes, *, labels=None, label_names=None,
                 model_name: str = _DEFAULT_MODEL, stats=None, loss_history=None,
                 auc=None, auc_init=None, params=None, feature_meta=None):
        super().__init__(zip(nodes, matrix))
        self.matrix = matrix
        self.nodes = list(nodes)
        self.index = {n: i for i, n in enumerate(self.nodes)}
        self.labels = labels
        self.label_names = label_names or []
        self.dim = int(matrix.shape[1])
        self.model_name = model_name
        self.stats = stats or {}
        self.loss_history = loss_history or []
        self.auc = auc
        self.auc_init = auc_init
        self.params = params or {}
        self.feature_meta = feature_meta or {}

    unit_matrix = _emb.EmbeddingResult.unit_matrix
    kind_mask = _emb.EmbeddingResult.kind_mask


def _init_params(dims: Sequence[int], rng, *, residual: bool = True,
                 jk_dim: int = 0) -> dict:
    """Glorot 초기화. W_self / W_neigh / b (+ 잔차투영 Wr) + 학습가능 스케일·바이어스.

    W_self·h_self + W_neigh·AGG 는 원논문의 W·CONCAT(h_self, AGG) 와 **수식상 동일**하다
    (블록분할 W = [W_self ; W_neigh]). 별도 행렬로 두면 두 경로의 그래디언트가 섞이지
    않아 self 경로가 살아남는지 진단하기 쉽다.

    residual=True 면 층마다 h_in 을 선형투영해 활성화 출력에 더한다(skip connection).
    이 그래프는 조례 노드의 **중앙차수가 1**(Region→Ordinance 한 개)이라, 2층에서
    조례의 이웃항이 '소속 지역의 조례 평균'으로 수렴해 같은 지역 조례가 서로
    구분되지 않는다. 잔차 경로가 조례 자신의 피처(제목 텍스트 해시 포함)를
    출력까지 직접 실어 나른다.
    """
    np = _np
    params: dict = {}
    for k in range(len(dims) - 1):
        din, dout = dims[k], dims[k + 1]
        lim = float(np.sqrt(6.0 / (din + dout)))
        params[f"Ws{k}"] = rng.uniform(-lim, lim, size=(din, dout)).astype(np.float32)
        params[f"Wn{k}"] = rng.uniform(-lim, lim, size=(din, dout)).astype(np.float32)
        params[f"b{k}"] = np.zeros(dout, dtype=np.float32)
        if residual:
            params[f"Wr{k}"] = rng.uniform(-lim, lim, size=(din, dout)).astype(np.float32)
    if jk_dim:
        din = dims[0]
        lim = float(np.sqrt(6.0 / (din + jk_dim)))
        params["Wj"] = rng.uniform(-lim, lim, size=(din, jk_dim)).astype(np.float32)
    params["scale"] = np.array([5.0], dtype=np.float32)
    params["bias"] = np.array([0.0], dtype=np.float32)
    return params


def _forward(X, params, mp: _MessagePassing, n_layers: int,
             aggregators: Sequence[str], residual: bool = False,
             jumping_knowledge: bool = False):
    """full-batch 순전파. 캐시(층 입력/집계/사전활성/노름) 반환.

    층 k:  P = h_in·Ws + AGG(h_in)·Wn + b
           Z = σ(P) [+ h_in·Wr]          ← 잔차/스킵
           h_out = Z / ||Z||             ← 층마다 L2 정규화(원논문 규약)

    jumping_knowledge=True 면 최종 표현을
        h = concat( L2(X·Wj), L2(h¹), …, L2(h^L) ) / sqrt(L+1)
    로 만든다(JK-Net concat). 블록마다 이미 단위벡터이므로 결과도 단위벡터이고
    cos(u,v) = 블록별 코사인의 평균이다. **최적화가 self 경로를 0 으로 눌러도
    X 블록의 랭크가 구조적으로 남는다** — 이 그래프의 과평활 핵심 원인
    (조례 중앙차수 1 → 이웃항이 소속 지역 벡터뿐 → 조례에 대한 AGG(X) 유효차원 3.4)
    에 대한 직접적 처방.
    """
    np = _np
    cache: list[dict] = []
    H = X
    for k in range(n_layers):
        A = mp.aggregate(H, aggregators[k])
        P = H @ params[f"Ws{k}"] + A @ params[f"Wn{k}"] + params[f"b{k}"]
        Z = np.maximum(P, 0.0) if k < n_layers - 1 else P
        if residual:
            Z = Z + H @ params[f"Wr{k}"]
        Hn, nrm = _l2norm_forward(Z)
        cache.append({"Hin": H, "A": A, "P": P, "Z": Z, "Hout": Hn, "nrm": nrm})
        H = Hn
    if not jumping_knowledge:
        return H, cache
    B0, nrm0 = _l2norm_forward(X @ params["Wj"])
    blocks = [B0] + [c["Hout"] for c in cache]
    s = float(1.0 / np.sqrt(len(blocks)))
    Hcat = (np.concatenate(blocks, axis=1) * s).astype(np.float32)
    cache.append({"jk": True, "X": X, "B0": B0, "nrm0": nrm0,
                  "scale": s, "widths": [b.shape[1] for b in blocks]})
    return Hcat, cache


def _backward(grad_H, cache, params, mp, n_layers, aggregators,
              residual: bool = False, jumping_knowledge: bool = False) -> dict:
    """층별 수동 역전파. dict[param_name] = grad."""
    np = _np
    grads: dict = {}
    extra: list = [None] * n_layers      # JK concat 에서 각 층 출력으로 직접 들어오는 항
    g = grad_H
    if jumping_knowledge:
        cjk = cache[n_layers]
        w = cjk["widths"]
        cuts = np.cumsum(w)[:-1]
        parts = [p * cjk["scale"] for p in np.split(grad_H, cuts, axis=1)]
        gZ0 = _l2norm_backward(np.ascontiguousarray(parts[0]), cjk["B0"], cjk["nrm0"])
        grads["Wj"] = (cjk["X"].T @ gZ0).astype(np.float32)
        extra = [np.ascontiguousarray(p) for p in parts[1:]]
        g = extra[n_layers - 1]
    for k in range(n_layers - 1, -1, -1):
        c = cache[k]
        gZ = _l2norm_backward(g, c["Hout"], c["nrm"])
        gP = gZ if k == n_layers - 1 else (gZ * (c["P"] > 0).astype(np.float32))
        grads[f"Ws{k}"] = (c["Hin"].T @ gP).astype(np.float32)
        grads[f"Wn{k}"] = (c["A"].T @ gP).astype(np.float32)
        grads[f"b{k}"] = gP.sum(axis=0).astype(np.float32)
        if residual:
            grads[f"Wr{k}"] = (c["Hin"].T @ gZ).astype(np.float32)
        if k == 0:
            break  # 층0 입력은 상수 피처 X → 입력 그래디언트 불필요(계산 생략)
        gHin = (gP @ params[f"Ws{k}"].T).astype(np.float32)
        if residual:
            gHin += (gZ @ params[f"Wr{k}"].T).astype(np.float32)
        gA = (gP @ params[f"Wn{k}"].T).astype(np.float32)
        gHin += mp.aggregate_backward(gA, H=c["Hin"], agg_out=c["A"],
                                      mode=aggregators[k])
        if jumping_knowledge and extra[k - 1] is not None:
            gHin = gHin + extra[k - 1]   # concat 블록으로 들어온 층 k-1 출력 그래디언트
        g = gHin
    return grads


def _collapse_loss_and_grad(H, idx, gamma: Optional[float] = None, eps: float = 1e-6):
    """차원붕괴(dimensional collapse) 억제 정규화 — VICReg 의 variance+covariance 항.

    링크예측 BCE 만으로 학습하면 이 그래프에서는 임베딩이 rank 2~3 으로 붕괴한다
    (실측: 조례 임베딩 유효차원 128 → 3.0, node2vec 은 91.8). BCE 는 '점수 하나'만
    맞추면 되므로 표현을 넓게 펼칠 유인이 없고, 과제가 어려우면 모든 쌍의 점수를
    상수로 만드는 퇴화해로 수렴한다. 그 결과 조례 Top-5 코사인이 전부 0.999 로
    붙어 순위가 무의미해진다.

    f = (1/d)·Σ_j max(0, γ - std_j)  +  (1/d)·Σ_{i≠j} C_ij²
    γ 기본값 = 1/sqrt(d)(행이 L2 정규화되어 총분산≈1 → 등방일 때의 축별 표준편차).
    수동 미분(정확):
      var : std_j < γ 인 축만  ∂f/∂B[:,j] = -(1/d)·Bc[:,j]/(n·std_j)
      cov : ∂f/∂B = (4/(n·d))·Bc·offdiag(C)   (중심화 보정으로 열평균 제거)
    """
    np = _np
    B = H[idx]
    n, d = B.shape
    if gamma is None:
        gamma = 1.0 / float(np.sqrt(d))
    mu = B.mean(axis=0)
    Bc = (B - mu).astype(np.float32)
    C = (Bc.T @ Bc) / float(n)
    var = np.diag(C)
    std = np.sqrt(np.maximum(var, 0.0) + eps)
    hinge = np.maximum(0.0, gamma - std)
    loss_var = float(hinge.sum()) / d
    off = C - np.diag(var)
    loss_cov = float((off * off).sum()) / d
    gB = (4.0 / (float(n) * d)) * (Bc @ off)
    active = (hinge > 0).astype(np.float32)
    gB -= (active / (float(n) * d * std))[None, :] * Bc
    gB -= gB.mean(axis=0)          # 중심화(B - mean)의 그래디언트 보정
    grad = np.zeros_like(H)
    grad[idx] = gB.astype(np.float32)
    return loss_var + loss_cov, grad, {"loss_var": loss_var, "loss_cov": loss_cov,
                                       "std_mean": float(std.mean()), "gamma": float(gamma)}


def _edge_loss_and_grad(H, params, pos_u, pos_v, neg_u, neg_v):
    """sigmoid + BCE 링크예측 손실과 ∂L/∂H, ∂L/∂scale, ∂L/∂bias(수동 미분).

    s = scale·(h_u·h_v) + bias,  L = -[y log σ(s) + (1-y) log(1-σ(s))]
    ∂L/∂s = σ(s) - y   →   ∂L/∂h_u = (∂L/∂s)·scale·h_v (그 역도 동일)
    """
    np = _np
    scale = float(params["scale"][0])
    bias = float(params["bias"][0])
    grad_H = np.zeros_like(H)

    def _side(u, v, y):
        dot = np.einsum("nd,nd->n", H[u], H[v])
        s = scale * dot + bias
        sig = _sigmoid(s)
        loss = -(np.log(np.maximum(sig, 1e-10)) if y == 1
                 else np.log(np.maximum(1.0 - sig, 1e-10)))
        gs = (sig - float(y)).astype(np.float32)
        _scatter_add(grad_H, u, (gs[:, None] * scale * H[v]).astype(np.float32))
        _scatter_add(grad_H, v, (gs[:, None] * scale * H[u]).astype(np.float32))
        return float(loss.sum()), float((gs * dot).sum()), float(gs.sum())

    lp, gs_p, gb_p = _side(pos_u, pos_v, 1)
    ln, gs_n, gb_n = _side(neg_u, neg_v, 0)
    m = float(pos_u.shape[0] + neg_u.shape[0])
    grad_H /= m
    total = (lp + ln) / m
    grad_scale = np.array([(gs_p + gs_n) / m], dtype=np.float32)
    grad_bias = np.array([(gb_p + gb_n) / m], dtype=np.float32)
    return total, grad_H, grad_scale, grad_bias


def train_graphsage(
    graph: Any,
    features: Any = None,
    dim: int = 128,
    layers: int = 2,
    epochs: int = 120,
    lr: float = 0.01,
    *,
    aggregator: str | Sequence[str] = "mean",
    residual: bool = True,
    fanout: Optional[int] = None,
    jumping_knowledge: bool = True,
    decorrelation: float = 0.0,
    decorrelation_nodes: int = 20_000,
    negatives: int = 1,
    negative_sampling: str = "type-matched",
    test_frac: float = 0.1,
    split: Optional[dict] = None,
    hidden_dim: Optional[int] = None,
    edge_batch: Optional[int] = 400_000,
    conn: Optional[sqlite3.Connection] = None,
    text_dim: int = 64,
    edge_weights: Optional[dict] = None,
    min_degree: int = 1,
    keep_kinds: Optional[Iterable[str]] = None,
    weight_decay: float = 0.0,
    eval_every: int = 10,
    model_name: Optional[str] = None,
    seed: int = 20260819,
    logger: Any = None,
    graph_arrays: Optional[GraphArrays] = None,
) -> GraphSageResult:
    """GraphSAGE 비지도 학습(numpy 수동 미분). 임베딩 + 학습곡선 + held-out AUC 반환.

    graph    : build_graph() 결과 또는 GraphArrays.
    features : (N, D) 초기 피처. None 이면 build_node_features(conn, ga) 로 생성.
    layers   : 메시지패싱 층 수(2~3 권장).
    aggregator: 'mean' | 'max' | 층별 리스트.
    edge_batch: 에폭당 손실에 쓸 positive 엣지 샘플 수(None 이면 전량).
    split    : split_edges() 결과. 주면 그대로 쓴다(다른 모델과 동일 분할로 AUC 비교).
    residual : 층마다 h_in 선형투영을 활성화 출력에 더한다(skip).
    fanout   : 노드당 이웃 표본 상한(허브 Region/Legislator 억제 + 연산 절감).
    jumping_knowledge: 최종표현 = concat(L2(X·Wj), L2(h¹), …)/sqrt(L+1).
        **과평활 해소의 핵심 스위치**. dim 은 (layers+1) 로 나누어 블록당 차원이 된다.
    decorrelation: VICReg 형 variance+covariance 정규화 계수(기본 0).
        실측상 JK 만으로 충분했고, 이 항을 켜면 유효차원은 약간 오르나(23.7→24.7)
        held-out AUC 가 떨어졌다(0.6578→0.6378). 기본 0 을 권장한다.

    동일 그래프(242,891노드/566,725엣지)·동일 분할·동일 시드 실측 비교
    (held-out 10%, negative 는 type-matched, 조례 유효차원은 스펙트럼 엔트로피):
      설정                                   AUC     유효차원  Top1코사인  Top1>0.999
      uniform negative, 60ep                0.4873    5.35        —          —
      type-matched, 잔차없음, 120ep          0.5887    3.07      0.9997      0.76
      type-matched + 잔차 + fanout32, 120ep  0.5901    3.01      0.9997      0.74
      + negatives=10                        0.5580    2.37      0.9989      0.76
      + VICReg 정규화(λ=20/100)              0.556/0.553 2.6/2.5  0.998       0.74
      + JK-concat(권장), 60ep                0.6578   23.68      0.9243      0.000
      참고: node2vec-numpy 유효차원 91.8 / metapath2vec-numpy 51.3

    negative_sampling: 'type-matched'(기본) | 'uniform'(고전 GraphSAGE 설정).
        이 프로젝트 그래프(209,203노드/517,792엣지) 실측 비교 — 동일 분할·피처·시드:
          지표                         uniform   type-matched
          held-out AUC(타입정합 negative) 0.4945     0.6678
          held-out AUC(균등 negative)     0.8230     0.5097  ← 타입 구분만으로 부풀려짐
          카테고리 분리 AUC               0.6695     0.9040
          조례 간 코사인 분산            ~0(0.995 붕괴)  std 0.410
        균등 negative 는 이종그래프에서 거의 전부 '존재하지 않는 타입조합'이라
        노드타입만 구분해도 손실이 줄어 표현이 붕괴한다. 기본값을 타입정합으로 둔다.
    """
    _require_numpy()
    np = _np
    log = logger or LOG
    rng = np.random.default_rng(seed)
    t0 = time.time()

    if isinstance(graph, GraphArrays):
        ga = graph
    elif graph_arrays is not None:
        ga = graph_arrays
    else:
        ga = GraphArrays.from_graph(graph, edge_weights=edge_weights,
                                    min_degree=min_degree, keep_kinds=keep_kinds,
                                    logger=log)

    feature_meta: dict = {}
    if features is None:
        X, feature_meta = build_node_features(conn, ga, text_dim=text_dim, logger=log)
    else:
        X = np.ascontiguousarray(np.asarray(features, dtype=np.float32))
        feature_meta = {"dim": int(X.shape[1]), "source": "caller"}
    if X.shape[0] != ga.num_nodes:
        raise ValueError(f"features 행수 {X.shape[0]} != 노드수 {ga.num_nodes}")

    split = split or split_edges(ga, test_frac=test_frac, seed=seed)
    mp = _MessagePassing(ga.num_nodes, split["train_src"], split["train_dst"],
                        fanout=fanout, seed=seed)
    log.info("메시지패싱 그래프: train 엣지 %d / held-out %d (고립 %d노드)",
             split["num_train"], split["num_test"], int((mp.deg == 0).sum()))

    n_layers = max(1, int(layers))
    jk = bool(jumping_knowledge)
    n_blocks = n_layers + 1 if jk else 1
    # JK: 블록 폭의 합이 정확히 dim 이 되게 나눈다(나머지는 X 투영 블록이 흡수).
    block = max(4, int(dim) // n_blocks) if jk else int(dim)
    jk_first = int(dim) - block * n_layers if jk else 0
    if jk and jk_first < 4:
        jk_first = block
    hid = int(hidden_dim or (block if jk else dim))
    dims = [int(X.shape[1])] + [hid] * (n_layers - 1) + [block]
    aggs = ([aggregator] * n_layers if isinstance(aggregator, str)
            else list(aggregator)[:n_layers])
    while len(aggs) < n_layers:
        aggs.append("mean")

    params = _init_params(dims, rng, residual=bool(residual),
                          jk_dim=(jk_first if jk else 0))
    opt = _Adam(params, lr=lr)
    pools = label_pools(ga) if negative_sampling == "type-matched" else None

    # 학습 전(랜덤 초기화) AUC
    H0, _ = _forward(X, params, mp, n_layers, aggs, residual, jk)
    eval_init = evaluate_link_prediction(H0, ga, split["test_src"], split["test_dst"],
                                         negatives=1, seed=seed + 3,
                                         negative_sampling=negative_sampling)
    eval_init_tm = evaluate_link_prediction(H0, ga, split["test_src"], split["test_dst"],
                                            negatives=1, seed=seed + 3,
                                            negative_sampling="type-matched")
    log.info("학습 전(랜덤 초기화) held-out AUC = %.4f(%s) / %.4f(type-matched) "
             "— 랜덤 베이스라인 0.5",
             eval_init["auc"], negative_sampling, eval_init_tm["auc"])

    last_decorr: dict = {}
    pos_u_all = split["train_src"]
    pos_v_all = split["train_dst"]
    loss_history: list[float] = []
    auc_history: list[tuple[int, float]] = []
    best_auc = eval_init["auc"]

    for ep in range(1, int(epochs) + 1):
        H, cache = _forward(X, params, mp, n_layers, aggs, residual, jk)
        if edge_batch and pos_u_all.shape[0] > edge_batch:
            sel = rng.choice(pos_u_all.shape[0], size=int(edge_batch), replace=False)
            pu, pv = pos_u_all[sel], pos_v_all[sel]
        else:
            pu, pv = pos_u_all, pos_v_all
        if negative_sampling == "type-matched":
            nu, nv = sample_negative_pairs(ga, pu.shape[0] * int(negatives), rng,
                                           like_src=pu, like_dst=pv, pools=pools)
        else:
            nu, nv = sample_negative_pairs(ga, pu.shape[0] * int(negatives), rng)

        loss, gH, g_scale, g_bias = _edge_loss_and_grad(H, params, pu, pv, nu, nv)
        if decorrelation:
            didx = rng.choice(ga.num_nodes, size=min(int(decorrelation_nodes),
                                                     ga.num_nodes), replace=False)
            dl, dg, dmeta = _collapse_loss_and_grad(H, didx)
            loss = loss + float(decorrelation) * dl
            gH = gH + float(decorrelation) * dg
            last_decorr = dmeta
        grads = _backward(gH, cache, params, mp, n_layers, aggs, residual, jk)
        grads["scale"] = g_scale
        grads["bias"] = g_bias
        if weight_decay:
            for k in list(grads.keys()):
                if k.startswith(("Ws", "Wn", "Wr", "Wj")):
                    grads[k] = grads[k] + weight_decay * params[k]
        opt.step(params, grads)
        loss_history.append(round(float(loss), 6))

        if ep % max(1, int(eval_every)) == 0 or ep == epochs or ep == 1:
            ev = evaluate_link_prediction(H, ga, split["test_src"], split["test_dst"],
                                          negatives=1, seed=seed + 3,
                                          negative_sampling=negative_sampling)
            auc_history.append((ep, ev["auc"]))
            best_auc = max(best_auc, ev["auc"])
            log.info("epoch %3d/%d loss=%.4f  held-out AUC=%.4f  (pos_cos=%.3f neg_cos=%.3f, %.0fs)",
                     ep, epochs, loss, ev["auc"], ev["pos_cos_mean"],
                     ev["neg_cos_mean"], time.time() - t0)

    H_final, _ = _forward(X, params, mp, n_layers, aggs, residual, jk)
    eval_final = evaluate_link_prediction(H_final, ga, split["test_src"],
                                          split["test_dst"], negatives=1, seed=seed + 3,
                                          negative_sampling=negative_sampling)
    eval_final_tm = evaluate_link_prediction(H_final, ga, split["test_src"],
                                             split["test_dst"], negatives=1,
                                             seed=seed + 3,
                                             negative_sampling="type-matched")
    log.info("학습 후 held-out AUC = %.4f (학습 전 %.4f / 랜덤 0.5) | "
             "type-matched %.4f (학습 전 %.4f)",
             eval_final["auc"], eval_init["auc"], eval_final_tm["auc"], eval_init_tm["auc"])

    stats = {
        "dims": dims, "layers": n_layers, "aggregators": aggs,
        "residual": bool(residual), "fanout": (int(fanout) if fanout else None),
        "decorrelation": float(decorrelation), "decorrelation_last": last_decorr,
        "jumping_knowledge": jk, "block_dim": int(block), "out_dim": int(H_final.shape[1]),
        "epochs": int(epochs), "lr": float(lr), "negatives": int(negatives),
        "num_nodes": ga.num_nodes, "num_edges": ga.num_edges,
        "train_edges": split["num_train"], "test_edges": split["num_test"],
        "test_frac": float(test_frac),
        "negative_sampling": negative_sampling,
        "eval_init": eval_init, "eval_final": eval_final,
        "eval_init_type_matched": eval_init_tm,
        "eval_final_type_matched": eval_final_tm,
        "auc_history": auc_history,
        "best_auc": float(best_auc),
        "loss_first": loss_history[0] if loss_history else None,
        "loss_last": loss_history[-1] if loss_history else None,
        "elapsed_sec": round(time.time() - t0, 1),
        "graph": dict(ga.meta),
        "features": feature_meta,
    }
    return GraphSageResult(
        H_final, ga.nodes, labels=ga.labels, label_names=ga.label_names,
        model_name=model_name or _DEFAULT_MODEL, stats=stats,
        loss_history=loss_history, auc=eval_final["auc"], auc_init=eval_init["auc"],
        params=params, feature_meta=feature_meta)


# --------------------------------------------------------------------------- #
# 카테고리 군집성 평가
# --------------------------------------------------------------------------- #
def category_cohesion(result, conn: sqlite3.Connection, *,
                      max_pairs: int = 400_000, seed: int = 11) -> dict:
    """같은 카테고리 조례쌍 평균 코사인 vs 다른 카테고리쌍 평균 코사인.

    ordinance_category(룰기반 분류 결과)를 정답 라벨로 쓴다.
    반환 {'same_mean','diff_mean','gap','separation_auc','n_labeled','by_category'}.
    """
    _require_numpy()
    np = _np
    rows = _db.fetchall(conn, "SELECT ordinance_id, category_code FROM ordinance_category")
    lab: dict[int, str] = {}
    for r in rows:
        nid = f"ordinance:{r['ordinance_id']}"
        i = result.index.get(nid)
        if i is not None:
            lab[i] = r["category_code"]
    if len(lab) < 4:
        return {"n_labeled": len(lab), "status": "insufficient-labels"}

    idx = np.array(sorted(lab.keys()), dtype=np.int64)
    codes = np.array([lab[int(i)] for i in idx])
    U = result.unit_matrix()[idx]
    S = U @ U.T
    same = (codes[:, None] == codes[None, :])
    iu = np.triu_indices(idx.shape[0], k=1)
    sim = S[iu]
    is_same = same[iu]
    rng = np.random.default_rng(seed)
    if sim.shape[0] > max_pairs:
        sel = rng.choice(sim.shape[0], size=max_pairs, replace=False)
        sim, is_same = sim[sel], is_same[sel]
    same_v = sim[is_same]
    diff_v = sim[~is_same]
    by_cat: dict[str, float] = {}
    for c in sorted(set(codes.tolist())):
        m = codes == c
        sub = U[m]
        if sub.shape[0] > 1:
            SS = sub @ sub.T
            k = np.triu_indices(sub.shape[0], k=1)
            by_cat[c] = round(float(SS[k].mean()), 4)
    return {
        "n_labeled": int(idx.shape[0]),
        "same_mean": round(float(same_v.mean()), 4),
        "diff_mean": round(float(diff_v.mean()), 4),
        "gap": round(float(same_v.mean() - diff_v.mean()), 4),
        "separation_auc": round(roc_auc(same_v, diff_v), 4),
        "n_same_pairs": int(same_v.shape[0]),
        "n_diff_pairs": int(diff_v.shape[0]),
        "by_category": by_cat,
        "status": "ok",
    }


__all__ = [
    "build_node_features", "split_edges", "sample_negative_pairs",
    "evaluate_link_prediction", "roc_auc", "average_precision",
    "train_graphsage", "GraphSageResult", "category_cohesion",
]
