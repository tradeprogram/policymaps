"""policymap.rag.index — 조문 하이브리드 인덱스(BM25 + Dense) 직접 구현.

설계 원칙(CONTRACTS.md 공통 규율 계승):
  * 무거운 의존성 금지. numpy 는 **가속용 선택**이며 없으면 순수파이썬 폴백으로 동일 결과.
  * 벡터 인덱스도 직접 구현(faiss/hnswlib 등 금지). 역색인(CSR) + 코사인.
  * 원문 미러링 금지 규율 준수 → 인덱스에는 **메타데이터만** 저장하고 조문 본문은
    질의 시점에 SQLite 에서 doc_key 로 재조회한다(인덱스 크기·신선도 양립).
  * DB 는 읽기 전용으로만 접근(다른 에이전트가 동시에 쓰는 중 → 쓰기 트랜잭션 금지).

코퍼스: `ordinance_articles`(자치법규 조문) + `articles`(국가법령 조문).
문서 단위 = 조문 1개. doc_key = oa_id | article_id.
색인 텍스트 = 소속 법규명 + 조제목 + 조내용 (법규명 포함이 제목질의 재현율을 크게 올린다).

토크나이저(한국어 형태소분석기 부재 대응):
  공백/기호로 자른 어절 토큰 + 어절 내부 char n-gram(2,3) 혼합.
  - 어절 토큰: '반려동물' 같은 복합어 정확매칭에 강함
  - char n-gram: '반려동물등록' ↔ '반려동물 등록' 같은 띄어쓰기·활용 변이 흡수

세그먼트 구조(증분 갱신):
  data/index/{scope}/
    meta.json            인덱스 전역 메타(세그먼트 목록·전역 통계·토크나이저 설정)
    tombstones.json      삭제/변경으로 무효화된 doc_key 목록
    seg-000/ seg-001/ …  각 세그먼트(불변). 새 문서·변경 문서는 새 세그먼트로 append.
증분 시 변경분만 새 세그먼트로 추가하고 구버전은 툼스톤 처리(Lucene 식).
툼스톤 비율이 크거나 세그먼트가 많아지면 자동 compaction(전체 재빌드).

공개 API:
    build_index(conn, *, scope='all', ...) -> dict
    load_index(scope='all', *, index_dir=None) -> HybridIndex
    index_stats(scope='all', *, index_dir=None) -> dict
    Tokenizer
    HybridIndex
"""
from __future__ import annotations

import array
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .. import config as _config
from .. import db as _db
from .. import util as _util
from ..parsers.embedding import Embedder

try:  # 선택적 가속(없으면 순수파이썬 경로로 동일 결과)
    import numpy as _np  # type: ignore
    _HAS_NUMPY = True
except Exception:  # pragma: no cover - 환경 의존
    _np = None  # type: ignore
    _HAS_NUMPY = False


_LOG = _util.get_logger("policymap.rag.index")

INDEX_VERSION = 1
DEFAULT_NGRAMS = (2, 3)
DEFAULT_MAX_CHARS = 4000
BM25_K1 = 1.2
BM25_B = 0.75
MAX_SEGMENTS = 32           # 초과 시 compaction (분할 빌드로 세그먼트가 늘어나므로 상향)
MAX_DOCS_PER_SEGMENT = 250_000   # 세그먼트당 문서 상한(메모리 보호, 약 1.5GB 포스팅)
COMPACT_TOMBSTONE_RATIO = 0.25

# 문서 종류 코드(용량 절약)
KIND_ORDINANCE_ARTICLE = 0
KIND_STATUTE_ARTICLE = 1
_KIND_NAME = {KIND_ORDINANCE_ARTICLE: "ordinance_article", KIND_STATUTE_ARTICLE: "statute_article"}


# --------------------------------------------------------------------------- #
# 0) 이진 배열 IO (numpy 있으면 numpy, 없으면 array.array — 동일 바이트 레이아웃)
# --------------------------------------------------------------------------- #
_TC_NP = {"i": "<i4", "q": "<i8", "f": "<f4"}
_TC_SIZE = {"i": 4, "q": 8, "f": 4}


def _save_arr(path: Path, values: Any, typecode: str) -> int:
    """정수/실수 배열을 리틀엔디언 raw 바이너리로 저장. 반환 원소 수."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if _HAS_NUMPY:
        arr = _np.asarray(values, dtype=_TC_NP[typecode])
        arr.tofile(str(path))
        return int(arr.size)
    a = values if isinstance(values, array.array) and values.typecode == typecode \
        else array.array(typecode, values)
    if a.itemsize != _TC_SIZE[typecode]:  # pragma: no cover - 비주류 플랫폼 방어
        raise RuntimeError(f"array typecode {typecode} itemsize={a.itemsize} 불일치")
    if sys.byteorder != "little":  # pragma: no cover
        a = array.array(typecode, a)
        a.byteswap()
    with open(path, "wb") as fh:
        a.tofile(fh)
    return len(a)


def _load_arr(path: Path, typecode: str) -> Any:
    """_save_arr 로 쓴 배열 로드. numpy 있으면 ndarray, 없으면 array.array."""
    if _HAS_NUMPY:
        return _np.fromfile(str(path), dtype=_TC_NP[typecode])
    a = array.array(typecode)
    size = path.stat().st_size
    with open(path, "rb") as fh:
        a.fromfile(fh, size // _TC_SIZE[typecode])
    if sys.byteorder != "little":  # pragma: no cover
        a.byteswap()
    return a


# --------------------------------------------------------------------------- #
# 1) 토크나이저
# --------------------------------------------------------------------------- #
_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]+")


class Tokenizer:
    """어절 토큰 + 어절 내부 char n-gram 혼합 토크나이저(한국어 무형태소 대응).

    >>> Tokenizer(ngrams=(2,)).tokens('반려동물 등록')
    ['반려동물', '반려', '려동', '동물', '등록', '등록']
    """

    def __init__(self, *, ngrams: Iterable[int] = DEFAULT_NGRAMS,
                 max_chars: int = DEFAULT_MAX_CHARS):
        self.ngrams = tuple(sorted({int(n) for n in ngrams if int(n) >= 1})) or (2,)
        self.max_chars = int(max_chars)

    def config(self) -> dict:
        return {"ngrams": list(self.ngrams), "max_chars": self.max_chars}

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "Tokenizer":
        cfg = cfg or {}
        return cls(ngrams=cfg.get("ngrams", DEFAULT_NGRAMS),
                   max_chars=int(cfg.get("max_chars", DEFAULT_MAX_CHARS)))

    def tokens(self, text: Any) -> list[str]:
        s = str(text or "")
        if self.max_chars and len(s) > self.max_chars:
            s = s[:self.max_chars]
        out: list[str] = []
        for w in _WORD_RE.findall(s.lower()):
            out.append(w)
            lw = len(w)
            for n in self.ngrams:
                if lw >= n:
                    for i in range(lw - n + 1):
                        out.append(w[i:i + n])
        return out

    def tf(self, text: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.tokens(text):
            counts[t] = counts.get(t, 0) + 1
        return counts


# --------------------------------------------------------------------------- #
# 2) 코퍼스 조회 (scope 해석)
# --------------------------------------------------------------------------- #
_OA_SQL = """
SELECT a.oa_id            AS doc_key,
       a.ordinance_id     AS parent_id,
       a.article_no       AS article_no,
       a.title            AS title,
       a.body             AS body,
       a.content_hash     AS content_hash,
       o.name             AS parent_name,
       o.region_id        AS region_id,
       o.org_name         AS org_name,
       o.official_url     AS official_url
FROM ordinance_articles a
JOIN ordinances o ON o.ordinance_id = a.ordinance_id
"""

_ART_SQL = """
SELECT a.article_id       AS doc_key,
       a.instrument_id    AS parent_id,
       a.article_no       AS article_no,
       a.title            AS title,
       a.body             AS body,
       a.content_hash     AS content_hash,
       l.name             AS parent_name,
       NULL               AS region_id,
       l.competent_authority AS org_name,
       l.official_url     AS official_url
FROM articles a
JOIN legal_instrument l ON l.instrument_id = a.instrument_id
"""


def _scope_sources(scope: str) -> list[tuple[int, str, tuple]]:
    """scope → [(kind, sql, params)]. 지원: all | ordinance | statute | sig:XXXXX | region:ID."""
    scope = (scope or "all").strip()
    if scope in ("all", "*"):
        return [(KIND_ORDINANCE_ARTICLE, _OA_SQL, ()), (KIND_STATUTE_ARTICLE, _ART_SQL, ())]
    if scope in ("ordinance", "ordinances"):
        return [(KIND_ORDINANCE_ARTICLE, _OA_SQL, ())]
    if scope in ("statute", "statutes", "law"):
        return [(KIND_STATUTE_ARTICLE, _ART_SQL, ())]
    if scope.startswith("sig:"):
        sig = scope.split(":", 1)[1]
        sql = _OA_SQL + " WHERE o.region_id IN (SELECT region_id FROM regions WHERE sig_cd=?)"
        return [(KIND_ORDINANCE_ARTICLE, sql, (sig,)), (KIND_STATUTE_ARTICLE, _ART_SQL, ())]
    if scope.startswith("region:"):
        rid = scope.split(":", 1)[1]
        return [(KIND_ORDINANCE_ARTICLE, _OA_SQL + " WHERE o.region_id=?", (rid,)),
                (KIND_STATUTE_ARTICLE, _ART_SQL, ())]
    raise ValueError(f"미지원 scope: {scope}")


def _scope_slug(scope: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", (scope or "all").strip()) or "all"


def iter_corpus(conn, scope: str = "all", *, batch_size: int = 2000) -> Iterator[dict]:
    """scope 코퍼스를 스트리밍(메모리 상수). 각 dict 는 색인용 원자료 1건."""
    for kind, sql, params in _scope_sources(scope):
        cur = conn.execute(sql, params)
        cur.arraysize = batch_size
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            for r in rows:
                d = dict(r)
                d["kind"] = kind
                yield d


def corpus_signature(conn, scope: str = "all") -> dict[str, str]:
    """doc_key → content_hash 맵(증분 갱신 판정용). 본문은 읽지 않는다."""
    sig: dict[str, str] = {}
    for kind, sql, params in _scope_sources(scope):
        # 본문 컬럼 제외한 경량 질의로 치환(대용량 body 를 읽지 않기 위함)
        light = re.sub(r"\s*a\.body\s+AS body,", "", sql)
        cur = conn.execute(light, params)
        cols = [c[0] for c in cur.description]
        ki, hi = cols.index("doc_key"), cols.index("content_hash")
        for row in cur:
            sig[row[ki]] = row[hi] or ""
    return sig


def _index_text(row: dict) -> str:
    """색인 대상 텍스트 = 법규명 + 조제목 + 조내용."""
    return "\n".join(str(x) for x in (row.get("parent_name"), row.get("title"), row.get("body")) if x)


# --------------------------------------------------------------------------- #
# 3) 세그먼트 빌더 — 역색인(CSR) 구축
# --------------------------------------------------------------------------- #
class _PostingsBuilder:
    """(term_id, doc_id, weight) 트리플을 array.array 에 누적 → CSR 로 정렬·압축."""

    def __init__(self) -> None:
        self.vocab: dict[str, int] = {}
        self.terms: array.array = array.array("i")
        self.docs: array.array = array.array("i")
        self.wts: array.array = array.array("f")

    def add_doc(self, doc_id: int, tf: dict[str, float]) -> None:
        vocab, terms, docs, wts = self.vocab, self.terms, self.docs, self.wts
        for tok, w in tf.items():
            tid = vocab.get(tok)
            if tid is None:
                tid = len(vocab)
                vocab[tok] = tid
            terms.append(tid)
            docs.append(doc_id)
            wts.append(float(w))

    def __len__(self) -> int:
        return len(self.terms)

    def finalize(self) -> tuple[list[str], Any, Any, Any, Any]:
        """반환 (terms_sorted, indptr, docids, weights, df).

        term_id 를 사전순 재번호하여 결정적(재빌드 시 동일 바이트) 인덱스를 만든다.
        """
        n_terms = len(self.vocab)
        # 사전순 재번호 맵
        order = sorted(self.vocab.items(), key=lambda kv: kv[0])
        terms_sorted = [t for t, _ in order]
        remap = [0] * n_terms
        for new_id, (_, old_id) in enumerate(order):
            remap[old_id] = new_id

        if _HAS_NUMPY:
            t = _np.frombuffer(memoryview(self.terms), dtype=_np.int32).copy()
            d = _np.frombuffer(memoryview(self.docs), dtype=_np.int32).copy()
            w = _np.frombuffer(memoryview(self.wts), dtype=_np.float32).copy()
            rm = _np.asarray(remap, dtype=_np.int32)
            t = rm[t] if n_terms else t
            # (term, doc) 사전순 안정 정렬 → CSR
            order_idx = _np.lexsort((d, t))
            t, d, w = t[order_idx], d[order_idx], w[order_idx]
            df = (_np.bincount(t, minlength=n_terms).astype(_np.int64)
                  if n_terms else _np.zeros(0, dtype=_np.int64))
            indptr = _np.zeros(n_terms + 1, dtype=_np.int64)
            if n_terms:
                _np.cumsum(df, out=indptr[1:])
            return terms_sorted, indptr, d, w, df.astype(_np.int32)

        # 순수파이썬 경로
        trip = sorted(zip((remap[x] for x in self.terms), self.docs, self.wts),
                      key=lambda x: (x[0], x[1]))
        docids = array.array("i", (x[1] for x in trip))
        weights = array.array("f", (x[2] for x in trip))
        counts = [0] * n_terms
        for tid, _, _ in trip:
            counts[tid] += 1
        df = array.array("i", counts)
        indptr = array.array("q", [0] * (n_terms + 1))
        acc = 0
        for i, c in enumerate(counts):
            acc += c
            indptr[i + 1] = acc
        return terms_sorted, indptr, docids, weights, df


def _write_postings(seg_dir: Path, prefix: str, terms: list[str],
                    indptr: Any, docids: Any, weights: Any, df: Any) -> dict:
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / f"{prefix}.terms").write_text("\n".join(terms), encoding="utf-8")
    _save_arr(seg_dir / f"{prefix}.indptr.bin", indptr, "q")
    n = _save_arr(seg_dir / f"{prefix}.docids.bin", docids, "i")
    _save_arr(seg_dir / f"{prefix}.w.bin", weights, "f")
    _save_arr(seg_dir / f"{prefix}.df.bin", df, "i")
    return {"terms": len(terms), "postings": int(n)}


def _l2_normalize(vec: dict[str, float]) -> tuple[dict[str, float], float]:
    norm = math.sqrt(sum(float(v) * float(v) for v in vec.values()))
    if not norm:
        return {}, 0.0
    return {k: float(v) / norm for k, v in vec.items()}, norm


def _build_segment(rows: Iterable[dict], seg_dir: Path, tok: Tokenizer,
                   embedder: Embedder, *, progress_every: int = 0) -> dict:
    """문서 iterable → 세그먼트 1개 생성. 반환 세그먼트 메타."""
    if seg_dir.exists():
        shutil.rmtree(seg_dir)
    seg_dir.mkdir(parents=True, exist_ok=True)

    bm = _PostingsBuilder()
    dn = _PostingsBuilder()
    doclen = array.array("i")
    dense_norm = array.array("f")
    n_docs = 0
    total_len = 0
    dense_kind = "sparse"
    dense_dim = 0
    dense_mat: Optional[array.array] = None

    t0 = time.time()
    with open(seg_dir / "docs.jsonl", "w", encoding="utf-8") as meta_fh:
        for row in rows:
            text = _index_text(row)
            tf = tok.tf(text)
            dl = sum(tf.values())
            bm.add_doc(n_docs, tf)

            vec = embedder.embed(text)
            if isinstance(vec, dict):
                unit, norm = _l2_normalize({k: float(v) for k, v in vec.items()})
                dn.add_doc(n_docs, unit)
                dense_norm.append(norm)
            else:  # pragma: no cover - neural 백엔드 있을 때만
                dense_kind = "dense"
                vals = [float(x) for x in vec]
                dense_dim = dense_dim or len(vals)
                norm = math.sqrt(sum(v * v for v in vals)) or 1.0
                if dense_mat is None:
                    dense_mat = array.array("f")
                dense_mat.extend([v / norm for v in vals])
                dense_norm.append(norm)

            doclen.append(dl)
            total_len += dl
            meta_fh.write(json.dumps({
                "k": row.get("doc_key"),
                "t": int(row.get("kind", KIND_ORDINANCE_ARTICLE)),
                "p": row.get("parent_id"),
                "n": row.get("parent_name"),
                "a": row.get("article_no"),
                "s": row.get("title"),
                "r": row.get("region_id"),
                "o": row.get("org_name"),
                "u": row.get("official_url"),
                "h": row.get("content_hash") or "",
                "l": dl,
            }, ensure_ascii=False) + "\n")
            n_docs += 1
            if progress_every and n_docs % progress_every == 0:
                _LOG.info("  세그먼트 색인 %s docs (%.1fs, postings=%s)",
                          n_docs, time.time() - t0, len(bm))

    _save_arr(seg_dir / "doclen.bin", doclen, "i")
    _save_arr(seg_dir / "dense.norm.bin", dense_norm, "f")

    bm_terms, bm_ptr, bm_doc, bm_w, bm_df = bm.finalize()
    bm_stat = _write_postings(seg_dir, "bm25", bm_terms, bm_ptr, bm_doc, bm_w, bm_df)

    if dense_kind == "sparse":
        dn_terms, dn_ptr, dn_doc, dn_w, dn_df = dn.finalize()
        dn_stat = _write_postings(seg_dir, "dense", dn_terms, dn_ptr, dn_doc, dn_w, dn_df)
    else:  # pragma: no cover
        _save_arr(seg_dir / "dense.mat.bin", dense_mat or array.array("f"), "f")
        dn_stat = {"terms": dense_dim, "postings": len(dense_mat or ())}

    seg_meta = {
        "n_docs": n_docs,
        "total_len": total_len,
        "bm25": bm_stat,
        "dense": dn_stat,
        "dense_kind": dense_kind,
        "dense_dim": dense_dim,
        "built_at": _util.now_kst_iso(),
        "elapsed_sec": round(time.time() - t0, 2),
    }
    (seg_dir / "seg.json").write_text(json.dumps(seg_meta, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    return seg_meta


# --------------------------------------------------------------------------- #
# 4) build_index — 전체/증분
# --------------------------------------------------------------------------- #
def default_index_root() -> Path:
    return Path(_config.get_config().out_dir) / "index"


def _index_dir(scope: str, index_dir: Optional[str | Path]) -> Path:
    base = Path(index_dir) if index_dir else default_index_root()
    return base / _scope_slug(scope)


def _read_meta(root: Path) -> Optional[dict]:
    p = root / "meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_meta(root: Path, meta: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                    encoding="utf-8")


def _segment_hashes(root: Path, segments: list[str]) -> dict[str, str]:
    """세그먼트들에 담긴 doc_key → content_hash (뒤 세그먼트가 우선)."""
    out: dict[str, str] = {}
    for seg in segments:
        p = root / seg / "docs.jsonl"
        if not p.exists():
            continue
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                out[d["k"]] = d.get("h") or ""
    return out


def build_index(
    conn,
    *,
    scope: str = "all",
    index_dir: Optional[str | Path] = None,
    model_name: Optional[str] = None,
    ngrams: Iterable[int] = DEFAULT_NGRAMS,
    max_chars: int = DEFAULT_MAX_CHARS,
    incremental: bool = True,
    force: bool = False,
    progress_every: int = 20000,
    max_segments: int = MAX_SEGMENTS,
    max_docs_per_segment: int = MAX_DOCS_PER_SEGMENT,
) -> dict:
    """scope 코퍼스로 하이브리드 인덱스를 data/index/{scope}/ 에 구축.

    incremental=True 이면 content_hash 비교로 신규/변경 문서만 새 세그먼트에 추가하고
    구버전은 툼스톤 처리한다. 변경이 없으면 아무 것도 쓰지 않고 reused=True 로 반환.
    툼스톤 비율 초과·세그먼트 과다 시 자동 compaction(전체 재빌드).

    반환 {'scope','status','mode','docs_indexed','added','changed','removed','segments',
          'bytes','elapsed_sec','reused', ...}
    """
    t0 = time.time()
    root = _index_dir(scope, index_dir)
    tok = Tokenizer(ngrams=ngrams, max_chars=max_chars)
    embedder = Embedder(model_name or "char-ngram-tf")

    prev = None if force else _read_meta(root)
    compatible = bool(prev) and prev.get("version") == INDEX_VERSION \
        and prev.get("tokenizer") == tok.config() and prev.get("model") == embedder.model_name

    mode = "full"
    added_keys: set[str] = set()
    changed_keys: set[str] = set()
    removed_keys: set[str] = set()
    tombstones: set[str] = set()
    cur_sig = corpus_signature(conn, scope)
    if not cur_sig:
        raise RuntimeError(
            f"RAG corpus is empty for scope={scope!r}; refusing to build an empty index"
        )

    if incremental and compatible:
        prev_hashes = _segment_hashes(root, prev.get("segments", []))
        prev_tomb = set(prev.get("tombstones", []))
        live_prev = {k: v for k, v in prev_hashes.items() if k not in prev_tomb}
        cur_keys = set(cur_sig)
        prev_keys = set(live_prev)
        added_keys = cur_keys - prev_keys
        removed_keys = prev_keys - cur_keys
        changed_keys = {k for k in (cur_keys & prev_keys) if cur_sig[k] != live_prev[k]}

        n_live = len(prev_keys)
        n_dirty = len(changed_keys) + len(removed_keys)
        too_many_segs = len(prev.get("segments", [])) + 1 > max_segments
        tomb_ratio = (len(prev_tomb) + n_dirty) / max(1, n_live)
        if not (added_keys or changed_keys or removed_keys):
            meta = dict(prev)
            meta["checked_at"] = _util.now_kst_iso()
            _write_meta(root, meta)
            return {"scope": scope, "status": "ok", "mode": "incremental", "reused": True,
                    "docs_indexed": meta.get("n_docs", 0), "added": 0, "changed": 0,
                    "removed": 0, "segments": len(meta.get("segments", [])),
                    "bytes": _dir_bytes(root), "elapsed_sec": round(time.time() - t0, 2)}
        if too_many_segs or tomb_ratio > COMPACT_TOMBSTONE_RATIO:
            mode = "compact"
        else:
            mode = "incremental"
            # 변경 문서는 새 세그먼트에 재수록되므로 툼스톤 대상이 아니다.
            # (동일 doc_key 가 여러 세그먼트에 있으면 '가장 뒤 세그먼트'가 유효 — _live 참조)
            # 툼스톤은 코퍼스에서 사라진 문서만. 부활한 문서는 툼스톤에서 해제.
            tombstones = (prev_tomb | removed_keys) - (added_keys | changed_keys)

    if mode in ("full", "compact"):
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        _LOG.info("[%s] 전체 인덱스 빌드 시작 (mode=%s)", scope, mode)
        # [메모리 보호] _build_segment 는 포스팅을 세그먼트 단위로 메모리에 쌓는다.
        # 62,460 문서 = 3,240만 포스팅이었으므로 문서당 약 519 포스팅이고,
        # 200만 문서를 한 세그먼트로 만들면 10억 포스팅 ≈ 12GB RAM 이라 죽는다. [실측 추정]
        # 따라서 문서 수 상한으로 세그먼트를 쪼갠다(조회는 세그먼트 합집합이라 동등).
        segments, seg_metas, n_docs, total_len = [], [], 0, 0
        src = iter_corpus(conn, scope)
        exhausted = False
        while not exhausted:
            idx_no = len(segments)
            seg_name = f"seg-{idx_no:03d}"
            taken = 0

            def _bounded():
                nonlocal taken, exhausted
                for row in src:
                    taken += 1
                    yield row
                    if taken >= max_docs_per_segment:
                        return
                exhausted = True

            seg_meta = _build_segment(_bounded(), root / seg_name, tok, embedder,
                                      progress_every=progress_every)
            if seg_meta["n_docs"] == 0:
                shutil.rmtree(root / seg_name, ignore_errors=True)
                break
            segments.append(seg_name)
            seg_metas.append(seg_meta)
            n_docs += seg_meta["n_docs"]
            total_len += seg_meta["total_len"]
            _LOG.info("[%s] %s 완료: 누적 문서 %s", scope, seg_name, f"{n_docs:,}")
        tombstones = set()
        added_keys = set()
    else:
        want = added_keys | changed_keys
        seg_name = f"seg-{len(prev['segments']):03d}"
        _LOG.info("[%s] 증분 빌드: +%s 신규 / ~%s 변경 / -%s 삭제 → %s",
                  scope, len(added_keys), len(changed_keys), len(removed_keys), seg_name)
        rows = (r for r in iter_corpus(conn, scope) if r["doc_key"] in want)
        seg_meta = _build_segment(rows, root / seg_name, tok, embedder,
                                  progress_every=progress_every)
        segments = list(prev["segments"]) + [seg_name]
        seg_metas = list(prev.get("segment_meta", [])) + [seg_meta]
        n_docs = len(cur_sig)            # 살아있는 문서 수 = 현재 코퍼스 크기
        total_len = sum(m["total_len"] for m in seg_metas)

    meta = {
        "version": INDEX_VERSION,
        "scope": scope,
        "segments": segments,
        "segment_meta": seg_metas,
        "tombstones": sorted(tombstones),
        "tokenizer": tok.config(),
        "model": embedder.model_name,
        "n_docs": n_docs,
        "avgdl": (total_len / max(1, sum(m["n_docs"] for m in seg_metas))),
        "byteorder": sys.byteorder,
        "bm25": {"k1": BM25_K1, "b": BM25_B},
        "built_at": _util.now_kst_iso(),
        "as_of_date": _util.today_kst(),
    }
    _write_meta(root, meta)
    return {
        "scope": scope,
        "status": "ok",
        "mode": mode,
        "reused": False,
        # 분할 빌드에서 seg_meta 는 마지막 세그먼트만 가리키므로 누적값(n_docs)을 쓴다.
        "docs_indexed": n_docs,
        "n_docs": n_docs,
        "added": len(added_keys),
        "changed": len(changed_keys),
        "removed": len(removed_keys),
        "segments": len(segments),
        "terms_bm25": seg_meta["bm25"]["terms"],
        "postings_bm25": seg_meta["bm25"]["postings"],
        "terms_dense": seg_meta["dense"]["terms"],
        "postings_dense": seg_meta["dense"]["postings"],
        "bytes": _dir_bytes(root),
        "path": str(root),
        "elapsed_sec": round(time.time() - t0, 2),
    }


def _dir_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


# --------------------------------------------------------------------------- #
# 5) 세그먼트 로더 + 스코어링
# --------------------------------------------------------------------------- #
class _Segment:
    """읽기 전용 세그먼트. 사전(term→id)은 최초 사용 시 지연 로드."""

    __slots__ = ("dir", "n_docs", "meta", "_doclen", "_docs", "_bm_vocab", "_dn_vocab",
                 "_bm", "_dn", "_dense_kind", "_dense_dim", "base")

    def __init__(self, seg_dir: Path, base: int):
        self.dir = seg_dir
        self.base = base
        self.meta = json.loads((seg_dir / "seg.json").read_text(encoding="utf-8"))
        self.n_docs = int(self.meta["n_docs"])
        self._dense_kind = self.meta.get("dense_kind", "sparse")
        self._dense_dim = int(self.meta.get("dense_dim") or 0)
        self._doclen = None
        self._docs = None
        self._bm_vocab = None
        self._dn_vocab = None
        self._bm = None
        self._dn = None

    # --- 지연 로드 ---
    @property
    def doclen(self):
        if self._doclen is None:
            self._doclen = _load_arr(self.dir / "doclen.bin", "i")
        return self._doclen

    @property
    def docs(self) -> list[dict]:
        if self._docs is None:
            out = []
            with open(self.dir / "docs.jsonl", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        out.append(json.loads(line))
            self._docs = out
        return self._docs

    def _load_postings(self, prefix: str):
        return (
            _load_arr(self.dir / f"{prefix}.indptr.bin", "q"),
            _load_arr(self.dir / f"{prefix}.docids.bin", "i"),
            _load_arr(self.dir / f"{prefix}.w.bin", "f"),
            _load_arr(self.dir / f"{prefix}.df.bin", "i"),
        )

    def _load_vocab(self, prefix: str) -> dict[str, int]:
        raw = (self.dir / f"{prefix}.terms").read_text(encoding="utf-8")
        terms = raw.split("\n") if raw else []
        return {t: i for i, t in enumerate(terms)}

    @property
    def bm_vocab(self) -> dict[str, int]:
        if self._bm_vocab is None:
            self._bm_vocab = self._load_vocab("bm25")
        return self._bm_vocab

    @property
    def dn_vocab(self) -> dict[str, int]:
        if self._dn_vocab is None:
            self._dn_vocab = self._load_vocab("dense")
        return self._dn_vocab

    @property
    def bm(self):
        if self._bm is None:
            self._bm = self._load_postings("bm25")
        return self._bm

    @property
    def dn(self):
        if self._dn is None:
            self._dn = self._load_postings("dense")
        return self._dn

    def df(self, prefix: str, term: str) -> int:
        vocab = self.bm_vocab if prefix == "bm25" else self.dn_vocab
        tid = vocab.get(term)
        if tid is None:
            return 0
        return int((self.bm if prefix == "bm25" else self.dn)[3][tid])


def _new_scores(n: int):
    if _HAS_NUMPY:
        return _np.zeros(n, dtype=_np.float64)
    return [0.0] * n


class HybridIndex:
    """빌드된 인덱스의 읽기 전용 핸들. BM25 / Dense / 하이브리드 검색 제공."""

    def __init__(self, root: Path, meta: dict):
        self.root = root
        self.meta = meta
        self.scope = meta.get("scope", "all")
        self.tokenizer = Tokenizer.from_config(meta.get("tokenizer"))
        self.model_name = meta.get("model", "char-ngram-tf")
        self._embedder: Optional[Embedder] = None
        self.k1 = float(meta.get("bm25", {}).get("k1", BM25_K1))
        self.b = float(meta.get("bm25", {}).get("b", BM25_B))
        self.avgdl = float(meta.get("avgdl") or 1.0) or 1.0
        self.tombstones = set(meta.get("tombstones", []))
        self.segments: list[_Segment] = []
        base = 0
        for name in meta.get("segments", []):
            seg = _Segment(root / name, base)
            self.segments.append(seg)
            base += seg.n_docs
        self.n_slots = base
        self.n_docs = int(meta.get("n_docs") or base)
        self._doc_cache: dict[int, dict] = {}
        self._key_to_gid: Optional[dict[str, int]] = None

    # --- 임베더(질의 벡터화용, 지연 생성) ---
    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = Embedder(self.model_name)
        return self._embedder

    # --- 문서 메타 접근 ---
    def _seg_of(self, gid: int) -> tuple[_Segment, int]:
        for seg in self.segments:
            if gid < seg.base + seg.n_docs:
                return seg, gid - seg.base
        raise IndexError(gid)

    def doc(self, gid: int) -> dict:
        d = self._doc_cache.get(gid)
        if d is None:
            seg, local = self._seg_of(gid)
            raw = seg.docs[local]
            d = {
                "doc_key": raw["k"],
                "doc_kind": _KIND_NAME.get(raw.get("t", 0), "ordinance_article"),
                "parent_id": raw.get("p"),
                "parent_name": raw.get("n"),
                "article_no": raw.get("a"),
                "article_title": raw.get("s"),
                "region_id": raw.get("r"),
                "org_name": raw.get("o"),
                "official_url": raw.get("u"),
                "content_hash": raw.get("h"),
                "doc_len": raw.get("l"),
            }
            self._doc_cache[gid] = d
        return d

    def gid_of(self, doc_key: str) -> Optional[int]:
        """doc_key → 현재 유효한 전역 슬롯. 툼스톤 처리된 문서는 None.

        같은 doc_key 가 여러 세그먼트에 있으면(증분 갱신으로 재수록) **가장 뒤 세그먼트**가
        이긴다 — 세그먼트 순서대로 덮어쓰므로 마지막 값이 최신본이다.
        """
        if doc_key in self.tombstones:
            return None
        if self._key_to_gid is None:
            m: dict[str, int] = {}
            for seg in self.segments:
                for i, raw in enumerate(seg.docs):
                    m[raw["k"]] = seg.base + i
            self._key_to_gid = m
        return self._key_to_gid.get(doc_key)

    def _live(self, gid: int) -> bool:
        """유효 문서 판정: 툼스톤이 아니고, 동일 doc_key 중 가장 최신 세그먼트 사본일 것."""
        if len(self.segments) <= 1 and not self.tombstones:
            return True                                    # 단일 세그먼트 고속경로
        key = self.doc(gid)["doc_key"]
        if key in self.tombstones:
            return False
        if len(self.segments) <= 1:
            return True
        return self.gid_of(key) == gid                     # 구버전 사본 제거

    # --- 전역 df (세그먼트 합산) ---
    def _global_df(self, prefix: str, terms: Iterable[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in terms:
            out[t] = sum(seg.df(prefix, t) for seg in self.segments)
        return out

    # --- BM25 ---
    def bm25_search(self, query: str, k: int = 10, *, max_df_ratio: float = 0.6,
                    candidates: Optional[set] = None) -> list[dict]:
        qtf = self.tokenizer.tf(query)
        if not qtf:
            return []
        dfs = self._global_df("bm25", qtf)
        N = max(1, self.n_docs)
        scores = _new_scores(self.n_slots)
        used = 0
        for term, qn in qtf.items():
            df = dfs.get(term, 0)
            if df <= 0 or df > N * max_df_ratio:
                continue
            idf = math.log(1.0 + (N - df + 0.5) / (df + 0.5))
            qw = idf * ((self.k1 + 1.0) * qn / (self.k1 + qn))
            used += 1
            for seg in self.segments:
                tid = seg.bm_vocab.get(term)
                if tid is None:
                    continue
                indptr, docids, wts, _dfarr = seg.bm
                lo, hi = int(indptr[tid]), int(indptr[tid + 1])
                if lo >= hi:
                    continue
                if _HAS_NUMPY:
                    d = docids[lo:hi]
                    tf = wts[lo:hi]
                    dl = seg.doclen[d].astype(_np.float64)
                    denom = tf + self.k1 * (1.0 - self.b + self.b * dl / self.avgdl)
                    scores[d + seg.base] += qw * (tf * (self.k1 + 1.0)) / denom
                else:
                    dlen = seg.doclen
                    for p in range(lo, hi):
                        d = docids[p]
                        tf = wts[p]
                        denom = tf + self.k1 * (1.0 - self.b + self.b * dlen[d] / self.avgdl)
                        scores[d + seg.base] += qw * (tf * (self.k1 + 1.0)) / denom
        return self._top(scores, k, used_terms=used, method="bm25", candidates=candidates)

    # --- Dense(코사인) ---
    def dense_search(self, query: str, k: int = 10, *,
                     candidates: Optional[set] = None) -> list[dict]:
        vec = self.embedder.embed(query)
        if isinstance(vec, dict):
            unit, _ = _l2_normalize({kk: float(v) for kk, v in vec.items()})
            if not unit:
                return []
            scores = _new_scores(self.n_slots)
            for term, qw in unit.items():
                for seg in self.segments:
                    if seg._dense_kind != "sparse":  # pragma: no cover
                        continue
                    tid = seg.dn_vocab.get(term)
                    if tid is None:
                        continue
                    indptr, docids, wts, _df = seg.dn
                    lo, hi = int(indptr[tid]), int(indptr[tid + 1])
                    if lo >= hi:
                        continue
                    if _HAS_NUMPY:
                        scores[docids[lo:hi] + seg.base] += qw * wts[lo:hi]
                    else:
                        for p in range(lo, hi):
                            scores[docids[p] + seg.base] += qw * wts[p]
            return self._top(scores, k, used_terms=len(unit), method="dense",
                             candidates=candidates)
        # neural dense 백엔드
        return self._dense_matrix_search(vec, k, candidates=candidates)  # pragma: no cover

    def _dense_matrix_search(self, vec, k, *, candidates=None):  # pragma: no cover
        vals = [float(x) for x in vec]
        norm = math.sqrt(sum(v * v for v in vals)) or 1.0
        q = [v / norm for v in vals]
        scores = _new_scores(self.n_slots)
        for seg in self.segments:
            dim = seg._dense_dim or len(q)
            mat = _load_arr(seg.dir / "dense.mat.bin", "f")
            for i in range(seg.n_docs):
                off = i * dim
                scores[seg.base + i] = sum(q[j] * float(mat[off + j]) for j in range(dim))
        return self._top(scores, k, used_terms=len(q), method="dense", candidates=candidates)

    # --- 상위 k 추출 ---
    def _top(self, scores, k: int, *, used_terms: int, method: str,
             candidates: Optional[set] = None) -> list[dict]:
        limit = max(1, k)
        if candidates is not None:
            pool = min(self.n_slots, max(limit * 50, 5000))   # 사후 필터 여유
        elif self.tombstones or len(self.segments) > 1:
            pool = limit * 6
        else:
            pool = limit
        if _HAS_NUMPY:
            nz = int((scores > 0).sum())
            if nz == 0:
                return []
            take = min(self.n_slots, max(pool, limit), nz)
            idx = _np.argpartition(-scores, take - 1)[:take]
            idx = idx[_np.argsort(-scores[idx], kind="stable")]
            cand = [(int(i), float(scores[i])) for i in idx if scores[i] > 0]
        else:
            cand = sorted(((i, s) for i, s in enumerate(scores) if s > 0),
                          key=lambda t: (-t[1], t[0]))[:max(pool, limit)]
        out: list[dict] = []
        for gid, s in cand:
            if not self._live(gid):
                continue
            d = self.doc(gid)
            if candidates is not None and d["doc_key"] not in candidates:
                continue
            hit = dict(d)
            hit["gid"] = gid
            hit["score"] = round(s, 6)
            hit["method"] = method
            hit["rank"] = len(out) + 1
            out.append(hit)
            if len(out) >= limit:
                break
        return out

    # --- 통계 ---
    def stats(self) -> dict:
        return {
            "scope": self.scope,
            "n_docs": self.n_docs,
            "n_slots": self.n_slots,
            "segments": len(self.segments),
            "tombstones": len(self.tombstones),
            "model": self.model_name,
            "tokenizer": self.tokenizer.config(),
            "avgdl": round(self.avgdl, 2),
            "bm25_terms": sum(s.meta["bm25"]["terms"] for s in self.segments),
            "bm25_postings": sum(s.meta["bm25"]["postings"] for s in self.segments),
            "dense_terms": sum(s.meta["dense"]["terms"] for s in self.segments),
            "dense_postings": sum(s.meta["dense"]["postings"] for s in self.segments),
            "bytes": _dir_bytes(self.root),
            "built_at": self.meta.get("built_at"),
            "as_of_date": self.meta.get("as_of_date"),
            "backend": "numpy" if _HAS_NUMPY else "pure-python",
        }


_CACHE: dict[str, HybridIndex] = {}


def load_index(scope: str = "all", *, index_dir: Optional[str | Path] = None,
               cache: bool = True) -> HybridIndex:
    """빌드된 인덱스 로드. 동일 경로 재요청은 프로세스 캐시 재사용."""
    root = _index_dir(scope, index_dir)
    key = str(root)
    if cache and key in _CACHE:
        return _CACHE[key]
    meta = _read_meta(root)
    if not meta:
        raise FileNotFoundError(f"인덱스가 없습니다: {root} (build_index 먼저 실행)")
    if meta.get("byteorder") and meta["byteorder"] != sys.byteorder:  # pragma: no cover
        raise RuntimeError("인덱스 바이트오더 불일치 — 재빌드 필요")
    idx = HybridIndex(root, meta)
    if cache:
        _CACHE[key] = idx
    return idx


def index_stats(scope: str = "all", *, index_dir: Optional[str | Path] = None) -> dict:
    """인덱스 존재 여부·규모 요약(빌드 없이 조회)."""
    root = _index_dir(scope, index_dir)
    meta = _read_meta(root)
    if not meta:
        return {"scope": scope, "exists": False, "path": str(root)}
    out = {"scope": scope, "exists": True, "path": str(root)}
    out.update(load_index(scope, index_dir=index_dir).stats())
    return out


__all__ = [
    "Tokenizer", "HybridIndex", "build_index", "load_index", "index_stats",
    "iter_corpus", "corpus_signature", "default_index_root",
]
