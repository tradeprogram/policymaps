"""policymap.rag — GraphRAG 검색 계층(조문 전문검색 + 그래프 확장 + 커뮤니티 요약).

구성:
  index.py      BM25 + Dense 하이브리드 역색인 직접 구현(무거운 의존성 없음).
  retrieve.py   후보 생성(BM25) + 재랭킹 검색 + 그래프 확장(GraphRAG) + 컨텍스트 조립.
  rerank.py     cross-encoder 재랭킹(모델 부재 시 BM25 순서 보존).
  evalset.py    검색 품질 회귀 평가셋(36질의) + measure(). 릴리스 게이트용.
  community.py  커뮤니티 탐지 기반 상위수준 요약(전역 질의 대응, Microsoft GraphRAG 방식).

생성(LLM)은 이 패키지의 책임이 아니다. `answer_context()` 는 근거 조문 원문·출처
메타·그래프 근거 경로만 조립해 반환하고, 생성은 MCP 클라이언트/LLM 이 담당한다.

빠른 사용:
    from policymap import db
    from policymap.rag import build_index, search, related_context, answer_context
    conn = db.connect()
    build_index(conn, scope='all')            # 증분 갱신이 기본(content_hash 비교)
    hits = search(conn, '반려동물 등록 지원 조례', k=5)      # 권장 경로(BM25→재랭킹)
    rel  = related_context(conn, hits)                     # '연결된 것들' 패널
    ctx  = answer_context(conn, '반려동물 등록 지원 조례', k=5, hops=1)

품질 회귀 확인:
    from policymap.rag import evalset
    evalset.measure(conn, lambda c,q,k: search(c,q,k))     # → P@1/P@5/Success@5
"""
from __future__ import annotations

from .index import (
    HybridIndex,
    Tokenizer,
    build_index,
    corpus_signature,
    default_index_root,
    index_stats,
    iter_corpus,
    load_index,
)
from .retrieve import (
    answer_context,
    bm25_search,
    dense_search,
    graph_expand,
    hybrid_graph_search,
    hybrid_search,
    related_context,
    search,
)
# 서브모듈 자체를 노출한다. `from .rerank import rerank` 로 동명의 함수를 올리면
# 패키지 속성 policymap.rag.rerank 가 모듈이 아닌 함수로 덮여 임포트 사고가 난다.
from . import rerank
from . import evalset
from .community import (
    build_community_report,
    community_summaries,
    global_search,
    load_community_report,
)

__all__ = [
    # index
    "build_index", "load_index", "index_stats", "HybridIndex", "Tokenizer",
    "iter_corpus", "corpus_signature", "default_index_root",
    # retrieve
    "hybrid_search", "hybrid_graph_search", "bm25_search", "dense_search",
    "graph_expand", "answer_context", "search", "related_context",
    # rerank (서브모듈. 함수는 rerank.rerank) / evalset (회귀 평가셋)
    "rerank", "evalset",
    # community
    "build_community_report", "load_community_report", "community_summaries", "global_search",
]
