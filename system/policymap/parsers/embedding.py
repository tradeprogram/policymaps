"""policymap.parsers.embedding — 텍스트→벡터 + 코사인 유사도(순수파이썬 폴백).

CONTRACTS.md §2.4 계약:
    class Embedder:  __init__(model_name='char-ngram-tf'); embed(text)->dict|list; similarity(a,b)->float
    embed_ordinances(conn, *, item_type='ordinance', model=None, run_id=None) -> dict
    build_similarity(conn, *, top_k=20, model_name=None) -> dict

설계(Python 3.14 코어 무의존 원칙):
  * 기본 백엔드 = 문자 n-gram TF sparse 벡터({ngram: tf}) + 코사인. 표준라이브러리만.
  * 선택적 백엔드 = sentence-transformers(model_name='sbert:...') → dense list. 없으면 자동 폴백.
  * numpy 있으면 dense kNN 가속, 없으면 순수파이썬(sparse 는 항상 순수파이썬).
  * embeddings/similarity_edges 저장은 db.upsert 사용. 벡터는 JSON 직렬화.

embed/similarity 는 DB 비의존 → 픽스처로 단위테스트 가능.
"""
from __future__ import annotations

import json
import math
from typing import Any, Optional

from .. import db as _db
from .. import util as _util

# 선택적 가속 라이브러리(없으면 graceful fallback)
try:  # pragma: no cover - 환경 의존
    import numpy as _np  # type: ignore
    _HAS_NUMPY = True
except Exception:  # noqa: BLE001
    _np = None  # type: ignore
    _HAS_NUMPY = False


_DEFAULT_MODEL = "char-ngram-tf"


class Embedder:
    """텍스트 임베딩 생성기. 기본은 문자 n-gram TF(sparse dict), 선택적 sbert(dense list)."""

    def __init__(self, model_name: str = _DEFAULT_MODEL, *, ngrams: tuple[int, ...] = (2, 3)):
        self.model_name = model_name or _DEFAULT_MODEL
        self.ngrams = tuple(n for n in ngrams if n >= 1) or (2,)
        self._sbert = None
        if self.model_name.startswith("sbert:"):
            self._try_load_sbert(self.model_name.split(":", 1)[1])

    # --- 백엔드 로드 ---
    def _try_load_sbert(self, hf_name: str) -> None:
        try:  # pragma: no cover - 환경 의존
            from sentence_transformers import SentenceTransformer  # type: ignore
            self._sbert = SentenceTransformer(hf_name)
        except Exception:  # noqa: BLE001 — 부재 시 폴백
            self._sbert = None
            self.model_name = _DEFAULT_MODEL  # 폴백 사실을 model_name 에 반영

    # --- 임베딩 ---
    @staticmethod
    def _prep(text: Any) -> str:
        return " ".join(str(text or "").split())

    def _char_ngram_tf(self, text: str) -> dict[str, float]:
        """문자 n-gram 빈도(TF) sparse 벡터. 공백 제거 후 슬라이딩."""
        s = self._prep(text).replace(" ", "")
        tf: dict[str, float] = {}
        if not s:
            return tf
        for n in self.ngrams:
            if len(s) < n:
                if len(s) >= 1 and n == self.ngrams[0]:
                    tf[s] = tf.get(s, 0.0) + 1.0
                continue
            for i in range(len(s) - n + 1):
                g = s[i:i + n]
                tf[g] = tf.get(g, 0.0) + 1.0
        return tf

    def embed(self, text: str) -> Any:
        """텍스트 → 벡터. sbert 로드 시 dense list[float], 아니면 sparse dict{ngram:tf}."""
        if self._sbert is not None:  # pragma: no cover - 환경 의존
            vec = self._sbert.encode(self._prep(text))
            return [float(x) for x in list(vec)]
        return self._char_ngram_tf(text)

    # --- 유사도 ---
    @staticmethod
    def _norm_sparse(vec: dict) -> float:
        return math.sqrt(sum(v * v for v in vec.values()))

    @staticmethod
    def _norm_dense(vec: list) -> float:
        return math.sqrt(sum(float(v) * float(v) for v in vec))

    def similarity(self, a: Any, b: Any) -> float:
        """코사인 유사도. dict(sparse)/list(dense) 혼용 방어. 빈 벡터는 0.0."""
        if isinstance(a, dict) and isinstance(b, dict):
            if not a or not b:
                return 0.0
            # 작은 쪽을 순회
            small, large = (a, b) if len(a) <= len(b) else (b, a)
            dot = sum(w * large.get(k, 0.0) for k, w in small.items())
            na, nb = self._norm_sparse(a), self._norm_sparse(b)
            return dot / (na * nb) if na and nb else 0.0
        la = list(a.values()) if isinstance(a, dict) else list(a)
        lb = list(b.values()) if isinstance(b, dict) else list(b)
        if not la or not lb or len(la) != len(lb):
            return 0.0
        dot = sum(float(x) * float(y) for x, y in zip(la, lb))
        na, nb = self._norm_dense(la), self._norm_dense(lb)
        return dot / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------------------- #
# 저장 헬퍼
# --------------------------------------------------------------------------- #
def _vector_norm(vec: Any) -> float:
    if isinstance(vec, dict):
        return Embedder._norm_sparse(vec)
    return Embedder._norm_dense(list(vec))


def _ordinance_text(conn, ordinance_id: str, name: str) -> str:
    """조례명 + 조문(제목+본문) 합본 텍스트(임베딩 입력)."""
    parts = [name or ""]
    arts = _db.fetchall(
        conn,
        "SELECT title, body FROM ordinance_articles WHERE ordinance_id=? ORDER BY article_no",
        (ordinance_id,),
    )
    for a in arts:
        if a.get("title"):
            parts.append(str(a["title"]))
        if a.get("body"):
            parts.append(str(a["body"]))
    return "\n".join(p for p in parts if p)


def embed_ordinances(conn, *, item_type: str = "ordinance",
                     model: Optional[Embedder] = None, run_id: Optional[str] = None) -> dict:
    """조례(또는 조문) 텍스트 → embeddings 테이블 적재. item_id=ordinances.ordinance_id.

    반환 {'embedded','unchanged','model','item_type','status'}.
    """
    emb = model or Embedder()
    now = _util.now_kst_iso()
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}

    if item_type == "ordinance_article":
        rows = _db.fetchall(
            conn, "SELECT oa_id AS id, ordinance_id, title, body FROM ordinance_articles")
        def _text(r):  # noqa: E306
            return "\n".join(x for x in (r.get("title"), r.get("body")) if x)
    else:  # 'ordinance'
        rows = _db.fetchall(
            conn, "SELECT ordinance_id AS id, name FROM ordinances "
                  "WHERE status IS NULL OR status='active'")
        def _text(r):  # noqa: E306
            return _ordinance_text(conn, r["id"], r.get("name") or "")

    prepared: list[dict] = []
    for r in rows:
        item_id = r["id"]
        text = _text(r)
        vec = emb.embed(text)
        dim = len(vec) if hasattr(vec, "__len__") else None
        prepared.append({
            "item_id": item_id,
            "item_type": item_type,
            "model_name": emb.model_name,
            "dim": dim,
            "vector": json.dumps(vec, ensure_ascii=False),
            "norm": _vector_norm(vec),
            "computed_at": now,
        })

    if prepared:
        with _db.tx(conn):
            counts = _db.upsert_many(conn, "embeddings", prepared, "item_id")
            # 조례 노드에 embedding_ref 역참조(선택; 조례 임베딩만)
            if item_type == "ordinance":
                for p in prepared:
                    conn.execute(
                        "UPDATE ordinances SET embedding_ref=? WHERE ordinance_id=?",
                        (p["item_id"], p["item_id"]),
                    )
    return {
        "embedded": counts["inserted"] + counts["updated"],
        "unchanged": counts["unchanged"],
        "model": emb.model_name,
        "item_type": item_type,
        "status": "ok",
    }


def _load_vector(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def build_similarity(conn, *, top_k: int = 20, model_name: Optional[str] = None) -> dict:
    """embeddings(item_type='ordinance') → kNN → similarity_edges 적재.

    각 src 기준 코사인 상위 top_k(자기 제외) 저장. 반환 {'nodes','pairs','model','status'}.
    numpy 는 dense 벡터일 때만 가속에 활용, sparse 는 순수파이썬.
    """
    where = "item_type='ordinance'"
    params: tuple = ()
    if model_name:
        where += " AND model_name=?"
        params = (model_name,)
    rows = _db.fetchall(
        conn, f"SELECT item_id, model_name, vector, norm FROM embeddings WHERE {where}", params)
    if not rows:
        return {"nodes": 0, "pairs": 0, "model": model_name, "status": "ok"}

    ids = [r["item_id"] for r in rows]
    vecs = [_load_vector(r["vector"]) for r in rows]
    norms = []
    for r, v in zip(rows, vecs):
        n = r.get("norm")
        norms.append(float(n) if n else _vector_norm(v))
    used_model = model_name or rows[0].get("model_name")
    is_dense = bool(vecs) and isinstance(vecs[0], list)

    now = _util.now_kst_iso()
    edges: list[dict] = []

    if (not is_dense) and _sparse_knn_available():  # pragma: no cover - 환경 의존
        # sparse(dict) 벡터 가속: scipy CSR 행렬곱으로 전쌍 코사인.
        # 결과는 순수파이썬 경로와 동일(동일 코사인·동일 tie-break).
        for src, dst, s, rank in _sparse_knn(ids, vecs, norms, top_k):
            edges.append(_edge(src, dst, s, rank, used_model, now))
    elif is_dense and _HAS_NUMPY:  # pragma: no cover - 환경 의존
        mat = _np.array([[float(x) for x in v] for v in vecs], dtype=float)
        norm_arr = _np.linalg.norm(mat, axis=1)
        norm_arr[norm_arr == 0] = 1.0
        unit = mat / norm_arr[:, None]
        sims = unit @ unit.T
        for i, src in enumerate(ids):
            order = _np.argsort(-sims[i])
            rank = 0
            for j in order:
                if j == i:
                    continue
                s = float(sims[i][j])
                if s <= 0.0:
                    break
                rank += 1
                edges.append(_edge(src, ids[j], s, rank, used_model, now))
                if rank >= top_k:
                    break
    else:
        for i, src in enumerate(ids):
            cand: list[tuple[float, str]] = []
            vi, ni = vecs[i], norms[i]
            if not ni:
                continue
            for j, dst in enumerate(ids):
                if j == i:
                    continue
                nj = norms[j]
                if not nj:
                    continue
                s = _cosine(vi, vecs[j], ni, nj)
                if s > 0.0:
                    cand.append((s, dst))
            cand.sort(key=lambda t: (-t[0], t[1]))
            for rank, (s, dst) in enumerate(cand[:top_k], start=1):
                edges.append(_edge(src, dst, s, rank, used_model, now))

    if edges:
        with _db.tx(conn):
            _db.upsert_many(conn, "similarity_edges", edges, ("src_id", "dst_id"))
    return {"nodes": len(ids), "pairs": len(edges), "model": used_model, "status": "ok"}


def _sparse_knn_available() -> bool:
    """scipy.sparse 가속 사용 가능 여부(없으면 순수파이썬 경로)."""
    if not _HAS_NUMPY:
        return False
    try:  # pragma: no cover - 환경 의존
        import scipy.sparse  # type: ignore  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _sparse_knn(ids: list, vecs: list, norms: list, top_k: int):
    """sparse dict 벡터 전쌍 코사인 → (src, dst, sim, rank) 제너레이터.

    순수파이썬 이중루프와 수학적으로 동일하되 CSR 행렬곱으로 계산한다.
    tie-break 도 동일하게 (-sim, dst_id) 사전순.
    """
    import numpy as np  # type: ignore
    from scipy import sparse as sp  # type: ignore

    vocab: dict[str, int] = {}
    indptr = [0]
    indices: list[int] = []
    data: list[float] = []
    for v in vecs:
        for g, w in (v or {}).items():
            j = vocab.get(g)
            if j is None:
                j = len(vocab)
                vocab[g] = j
            indices.append(j)
            data.append(float(w))
        indptr.append(len(indices))

    X = sp.csr_matrix(
        (np.asarray(data, dtype=np.float64),
         np.asarray(indices, dtype=np.int64),
         np.asarray(indptr, dtype=np.int64)),
        shape=(len(ids), max(1, len(vocab))),
    )
    inv = np.asarray([(1.0 / n) if n else 0.0 for n in norms], dtype=np.float64)
    Xn = sp.diags(inv) @ X                      # 행 단위 L2 정규화
    S = np.asarray((Xn @ Xn.T).todense())       # 코사인 유사도 행렬
    np.fill_diagonal(S, -1.0)                   # 자기 자신 제외

    for i, src in enumerate(ids):
        row = S[i]
        pos = np.nonzero(row > 0.0)[0]
        if pos.size == 0:
            continue
        cand = sorted(((float(row[j]), ids[j]) for j in pos),
                      key=lambda t: (-t[0], t[1]))
        for rank, (s, dst) in enumerate(cand[:top_k], start=1):
            yield src, dst, s, rank


def _cosine(vi: Any, vj: Any, ni: float, nj: float) -> float:
    """사전계산 노름을 활용한 코사인(sparse/dense 공용)."""
    if isinstance(vi, dict) and isinstance(vj, dict):
        small, large = (vi, vj) if len(vi) <= len(vj) else (vj, vi)
        dot = sum(w * large.get(k, 0.0) for k, w in small.items())
    else:
        li = list(vi.values()) if isinstance(vi, dict) else list(vi)
        lj = list(vj.values()) if isinstance(vj, dict) else list(vj)
        if len(li) != len(lj):
            return 0.0
        dot = sum(float(x) * float(y) for x, y in zip(li, lj))
    return dot / (ni * nj) if ni and nj else 0.0


def _edge(src: str, dst: str, sim: float, rank: int, model: Optional[str], now: str) -> dict:
    return {
        "src_id": src,
        "dst_id": dst,
        "cosine_sim": round(float(sim), 6),
        "rank": rank,
        "model_name": model,
        "computed_at": now,
    }


__all__ = ["Embedder", "embed_ordinances", "build_similarity"]
