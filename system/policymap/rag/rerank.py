"""policymap.rag.rerank — 검색 후보 재랭킹(cross-encoder; 모델 없으면 무해 폴백).

왜 필요한가(실측 근거, 2026-08-20):
  data/index/all/meta.json 실측상 이 시스템의 'dense' 채널은 dense 가 아니다 —
  model=char-ngram-tf, dense_kind=sparse, dense_dim=0. 즉 BM25 와 **동일한 문자 n-gram
  자질 공간**에서 IDF 없이 raw TF 코사인을 계산하는 BM25 의 열화 복제본이다.
  그 결과 고빈도 무정보 n-gram(예방/관리/및)이 유사도를 지배해, 융합해도 BM25 단독을
  이기지 못했다(28질의 실측: BM25 P@5 0.4786 > hybrid 0.4429 > dense 0.3143).

  bi-encoder 를 고쳐도 한계가 있다. 질의와 문서를 **따로** 인코딩하기 때문이다.
  cross-encoder 는 (질의, 문서)를 한 시퀀스로 넣어 토큰 간 상호작용을 직접 보므로
  "밥 주는 곳" ↔ "급식소", "몰래 낳는" ↔ "보호출산" 같은 어휘 불일치를 해소한다.
  대신 O(후보수) 추론이라 전수 검색에는 못 쓴다 → **BM25 로 후보를 좁힌 뒤 재정렬**한다.

설계 원칙:
  · 후보 풀은 BM25 단독. (BM25+Dense 합집합은 실측상 더 나빴다 — 잡음 후보가 섞인다.)
  · 인덱스·DB 스키마를 건드리지 않는다. 따라서 본문 수집으로 코퍼스가 수십 배가 되어도
    재랭킹 비용은 후보수에만 비례해 **불변**이다.
  · 모델 부재/로드 실패 시 **재정렬하지 않고 BM25 순서를 보존**한다. 규칙 기반 재랭킹도
    구현해 두었으나(force_rule=True) 실측상 BM25 보다 나빠서 기본 폴백에서 뺐다 —
    폴백은 품질을 깎지 않는 것이 우선이다.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Optional

from .. import util as _util

_LOG = _util.get_logger("policymap.rag.rerank")

# 우선순위 순. 앞의 것이 로드되면 뒤는 시도하지 않는다.
DEFAULT_MODELS = (
    "BAAI/bge-reranker-v2-m3",   # 다국어 cross-encoder(XLM-R large). 한국어 법령에서 최고 성능 실측.
    "Dongjin-kr/ko-reranker",    # 한국어 특화 폴백.
)
# 아래 세 값이 정확도와 CPU 지연을 함께 지배한다. EVAL28 실측 스윕(2026-08-20,
# torch 16스레드, CPU 전용):
#
#   candidates  max_length  body   P@1      P@5      Success@5   s/query
#       20         256       250   0.7143   0.6143   0.7857       4.4
#       20         320       500   0.7143   0.6000   0.8214       6.8
#       30         256       250   0.6786   0.6286   0.7857       5.9
#       30         384       700   0.6786   0.6357   0.8214      11.0   ← 현재 기본값
#       50         256       250   0.6071   0.6214   0.8214       9.1
#
# 읽는 법: 후보를 늘리면 Success@5(재현율)는 오르지만 P@1 은 오히려 **떨어진다** —
# 풀이 커질수록 재랭커가 상위로 끌어올리는 그럴듯한 오답도 함께 늘기 때문이다.
# 차이는 대부분 1~2질의(±0.036) 규모라 표본잡음과 구분하기 어렵다. 반면 지연 차이는
# 실재한다. 지연이 문제라면 (20, 256, 250) 이 2.5배 빠르면서 P@1 은 오히려 높다.
DEFAULT_CANDIDATES = 30          # 후보 풀 크기
DEFAULT_MAX_LENGTH = 384         # 토큰 상한. 지연에 가장 크게 기여한다
DEFAULT_BATCH = 16
_MAX_DOC_CHARS = 700             # cross-encoder 입력용 본문 절단 길이

_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]+")

_lock = threading.Lock()
_model: Any = None
_model_name: Optional[str] = None
_load_failed = False


# --------------------------------------------------------------------------- #
# 1) 모델 로드 (지연·1회·스레드 안전)
# --------------------------------------------------------------------------- #
def available() -> bool:
    """cross-encoder 재랭킹이 가능한 환경인지. 부작용으로 모델을 로드한다."""
    return _load() is not None


def model_name() -> str:
    """현재 재랭커 식별자. 모델이 없으면 'none(bm25-order)'."""
    return _model_name or "none(bm25-order)"


def _load(models: tuple[str, ...] = DEFAULT_MODELS) -> Any:
    global _model, _model_name, _load_failed
    if _model is not None or _load_failed:
        return _model
    with _lock:
        if _model is not None or _load_failed:
            return _model
        if os.environ.get("POLICYMAP_RERANK") == "off":
            _load_failed = True
            return None
        try:
            from sentence_transformers import CrossEncoder  # type: ignore
        except Exception as exc:  # noqa: BLE001 — 미설치 환경은 정상 시나리오
            _LOG.info("cross-encoder 미사용(sentence-transformers 부재: %s) → BM25 순서 보존", exc)
            _load_failed = True
            return None
        override = os.environ.get("POLICYMAP_RERANK_MODEL")
        for name in ((override,) if override else models):
            try:
                _model = CrossEncoder(name, max_length=DEFAULT_MAX_LENGTH)
                _model_name = name
                _LOG.info("재랭커 로드: %s", name)
                return _model
            except Exception as exc:  # noqa: BLE001 — 캐시 부재/오프라인
                _LOG.info("재랭커 로드 실패(%s): %s", name, exc)
        _load_failed = True
        return None


# --------------------------------------------------------------------------- #
# 2) 재랭킹 입력 텍스트
# --------------------------------------------------------------------------- #
def _doc_text(hit: dict) -> str:
    """재랭킹에 넣을 문서 표현 = 조례명 + 조번호/조제목 + 본문 앞부분.

    조례명을 맨 앞에 두는 것이 중요하다. 우리 코퍼스는 표준 조례 복제로 본문이
    지자체 간 거의 동일해(문서14 의 DRM 문제) 본문만으로는 변별이 안 되기 때문이다.
    """
    parts = [str(hit.get("parent_name") or hit.get("name") or "")]
    art_no, art_t = hit.get("article_no"), hit.get("article_title")
    if art_no:
        parts.append(f"제{art_no}조" + (f"({art_t})" if art_t else ""))
    elif art_t:
        parts.append(str(art_t))
    body = str(hit.get("text") or "")
    if body:
        parts.append(body[:_MAX_DOC_CHARS])
    return " ".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# 3) 규칙 재랭커(옵션. 기본 폴백 아님 — _rule_score 도크스트링 참조)
# --------------------------------------------------------------------------- #
def _terms(text: Any) -> list[str]:
    return [w for w in _WORD_RE.findall(str(text or "").lower()) if len(w) >= 2]


def _rule_score(query: str, hit: dict, base_rank: int) -> float:
    """규칙 재랭킹 점수. 조례명 일치를 본문 일치보다 크게 본다.

    **기본 폴백이 아니다** — force_rule=True 로 명시할 때만 쓴다. 36질의 실측(2026-08-20)
    결과 이 규칙 재랭킹은 BM25 원순위보다 **나빴다**:

        BM25 원순위      P@1 0.5278  P@5 0.4333  Success@5 0.6111
        규칙 재랭킹      P@1 0.4722  P@5 0.4278  Success@5 0.5556

    이유는 분명하다. 질의는 실무자 구어("밥 주는 곳")이고 조례명은 법령 문어("급식소")라
    어휘가 겹치지 않게 설계돼 있는데, 규칙 점수는 결국 **어휘 겹침**을 재는 지표라
    BM25 가 이미 쓰는 신호를 IDF 없이 조악하게 다시 쓰는 꼴이 된다.
    그래서 모델이 없을 때의 기본 동작은 규칙 재정렬이 아니라 **BM25 순서 보존**이다.
    """
    q = _terms(query)
    if not q:
        return 1.0 / (1 + base_rank)
    name = str(hit.get("parent_name") or hit.get("name") or "").lower()
    title = str(hit.get("article_title") or "").lower()
    body = str(hit.get("text") or "").lower()
    # 부분문자열 매칭 — 한국어 조사·어미 변형을 어절 완전일치보다 잘 잡는다.
    n_hit = sum(1 for w in q if w in name)
    t_hit = sum(1 for w in q if w in title)
    b_hit = sum(1 for w in q if w in body)
    nq = float(len(q))
    score = 1.00 * (n_hit / nq) + 0.35 * (t_hit / nq) + 0.25 * (b_hit / nq)
    # 제1조(목적)는 조례 전체 주제를 담아 조회형 질의의 대표 조문으로 적합하다.
    if str(hit.get("article_no") or "") == "1":
        score += 0.05
    score += 0.20 / (1.0 + base_rank)      # 원 순위를 완전히 버리지는 않는다
    return score


# --------------------------------------------------------------------------- #
# 4) 공개 API
# --------------------------------------------------------------------------- #
def rerank(query: str, hits: list[dict], k: Optional[int] = None, *,
           candidates: int = DEFAULT_CANDIDATES,
           batch_size: int = DEFAULT_BATCH,
           force_rule: bool = False) -> list[dict]:
    """후보 히트를 재정렬해 상위 k 개를 반환한다(입력 리스트는 변경하지 않는다).

    각 히트에 다음 필드를 채워 넣는다:
      rerank_score  재랭커 점수(모델 로짓 / 순위역수 / 규칙 점수)
      base_rank     재랭킹 이전 순위(감사·디버깅용)
      reranker      'BAAI/bge-reranker-v2-m3' 등 / 'none(bm25-order)' / 'rule-based'
      rank          재랭킹 후 최종 순위(1부터)

    candidates 를 넘는 꼬리 후보는 재랭킹하지 않고 원 순위 뒤에 그대로 붙인다
    (비용 상한을 보장하되 재현율을 깎지 않기 위함).
    """
    if not hits:
        return []
    k = int(k or len(hits))
    pool = [dict(h) for h in hits[:max(1, int(candidates))]]
    tail = [dict(h) for h in hits[max(1, int(candidates)):]]
    for i, h in enumerate(pool, start=1):
        h["base_rank"] = h.get("rank") or i

    model = None if force_rule else _load()
    if model is not None:
        pairs = [(query, _doc_text(h)) for h in pool]
        try:
            scores = model.predict(pairs, batch_size=batch_size,
                                   show_progress_bar=False)
            for h, s in zip(pool, scores):
                h["rerank_score"] = float(s)
                h["reranker"] = _model_name
        except Exception as exc:  # noqa: BLE001 — 추론 실패 시에도 검색은 살려둔다
            _LOG.warning("재랭킹 추론 실패 → BM25 순서 보존: %s", exc)
            model = None
    if model is None:
        if force_rule:
            for h in pool:
                h["rerank_score"] = _rule_score(query, h, h["base_rank"])
                h["reranker"] = "rule-based"
        else:
            # 모델 부재 시의 안전한 기본값: 재정렬하지 않는다(BM25 순서 보존).
            # 규칙 재랭킹은 실측상 BM25 보다 나빴으므로, 폴백이 품질을 **깎지 않게** 한다.
            # 'first, do no harm' — 재랭커가 없는 환경은 BM25 기준선 그대로 동작해야 한다.
            for h in pool:
                h["rerank_score"] = 1.0 / (1.0 + h["base_rank"])
                h["reranker"] = "none(bm25-order)"

    # 꼬리 후보도 필드 형태를 맞춰 둔다 — 호출부가 rerank_score/reranker 유무로
    # 분기하지 않아도 되게(부분적으로만 채워진 dict 는 조용한 버그의 온상이다).
    for h in tail:
        h.setdefault("base_rank", h.get("rank"))
        h["rerank_score"] = None
        h["reranker"] = "not-reranked(tail)"

    pool.sort(key=lambda h: (-h["rerank_score"], h["base_rank"]))
    out = (pool + tail)[:k]
    for i, h in enumerate(out, start=1):
        h["rank"] = i
    return out


__all__ = ["rerank", "available", "model_name", "DEFAULT_CANDIDATES", "DEFAULT_MODELS"]
