"""policymap.rag.retrieve — 후보 생성 + 재랭킹 검색 + 그래프 확장(GraphRAG 핵심).

권장 파이프라인(2026-08-20 개편):
  1) 후보 생성   search() → bm25_search  (index.py 의 역색인)
  2) 재랭킹      rerank.py 의 cross-encoder 가 (질의,문서)를 함께 인코딩해 재정렬
  3) 관계 패널   related_context — 검색 결과에 '연결된 것들'을 근거 경로와 함께 덧붙임
                 · DELEGATED_FROM  상위법(위임 근거) + 그 조문
                 · CITES           규범 간 인용(instrument_relations)
                 · FUNDED_BY       실제 집행 예산사업(ordinance_budget_link)
                 · ADJACENT_REGION 인접 지자체의 동일 주제 조례(확산·벤치마킹)
                 · SIMILAR_TO      임베딩 유사 조례(similarity_edges)
                 · SAME_REGION     같은 지자체의 동일 주제 조례
  4) 조립        answer_context — 조문 원문 + 출처 메타 + 관계 근거 경로

무엇이 바뀌었고 왜인가(36질의 실측, evalset.py 로 재현 가능):
  · hybrid_search(BM25+dense RRF)를 기본에서 내렸다. 이 인덱스의 'dense' 채널은
    dense 가 아니라 char n-gram TF(meta.json: dense_kind=sparse, dense_dim=0)여서
    BM25 의 열화 복제본이고, 융합하면 오히려 P@1 이 떨어진다(0.5278 → 0.5000).
  · hybrid_graph_search 도 기본에서 내렸다. 그래프를 랭킹에 섞어도 순위가 바뀌지 않고
    (RRF 산식상 그래프 단독 노드는 top-k 진입이 불가능), 비용만 70배 든다(0.70s/q).
    그래프는 랭킹이 아니라 related_context 패널로 재배치했다 — 타깃 과업
    "우리와 비슷한 지역이 이미 만든 조례 찾기"에는 이쪽이 정확히 부합한다.
  · 두 함수 모두 비교·회귀용으로 **남겨 두었다**. 지우면 이 판단을 재검증할 수 없다.

이 DB의 실제 제약(성능 천장을 지배하는 요인):
  ordinances 159,452건 중 본문이 확보된 조례는 아직 일부다(수집 진행 중).
  36질의 진단 결과 실패의 주원인은 검색 알고리즘이 아니라 **코퍼스 미수집**이었다 —
  정답 조례가 인덱스에 아예 없는 질의가 2/36 이었다. 그래프 확장은 본문이 없는
  조례를 지자체·인접성·위임 관계로 끌어와 이 공백을 부분적으로 메운다.

생성(LLM)은 하지 않는다. 컨텍스트 묶음만 반환하고 생성은 MCP 클라이언트/LLM 몫.
DB 는 읽기 전용으로만 접근한다(다른 수집 에이전트와 동시 실행 안전).

생성(LLM)은 하지 않는다. 컨텍스트 묶음만 반환하고 생성은 MCP 클라이언트/LLM 몫.
DB 는 읽기 전용으로만 접근한다(다른 수집 에이전트와 동시 실행 안전).
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Iterable, Optional

from .. import db as _db
from .. import util as _util
from ..graph import build as _gbuild
from . import index as _index

_LOG = _util.get_logger("policymap.rag.retrieve")

# mcp_server.server 와 동일 문구(레이어 역전을 피하려 상수만 복제).
DISCLAIMER = (
    "이 응답은 의사결정 지원을 위한 참고 정보이며 법률 판단·유권해석이 아닙니다. "
    "근거 조문과 law.go.kr 원문을 직접 확인하십시오."
)

DEFAULT_RRF_K = 60
DEFAULT_ALPHA = 0.5          # weighted 융합 시 BM25 비중

# 그래프 확장 관계별 가중(근거 강도 순). 상위법 위임 > 유사조례 > 예산 > 지역 이웃.
REL_WEIGHT = {
    "DELEGATED_FROM": 1.00,
    "SIMILAR_TO": 0.85,
    "FUNDED_BY": 0.75,
    "SAME_REGION": 0.70,
    "CITES": 0.65,
    "ADJACENT_REGION": 0.60,
    "HAS_ORDINANCE": 0.50,
}
DEFAULT_RELATIONS = ("DELEGATED_FROM", "SIMILAR_TO", "SAME_REGION",
                     "ADJACENT_REGION", "FUNDED_BY", "CITES")
HOP_DECAY = 0.55

# 관계의 성격 구분 — 확장 결과를 어떻게 취급할지가 달라진다.
#  구조(structural): 위임 상위법·집행예산·인용. 명칭이 질의와 안 닮아도 '근거'로서 정당하다
#    (예: "산후조리 지원 근거 법령" ↔ 「모자보건법」은 어휘가 하나도 안 겹친다).
#  측면(lateral)  : 같은/인접 지자체, 유사 조례. '비슷한 것 더 보기'이므로 질의와 주제가
#    어긋나면 컨텍스트를 오염시킨다 → 질의 어휘 관련성 게이트를 건다.
STRUCTURAL_RELATIONS = frozenset({"DELEGATED_FROM", "FUNDED_BY", "CITES"})
LATERAL_RELATIONS = frozenset({"SIMILAR_TO", "SAME_REGION", "ADJACENT_REGION"})
LATERAL_MIN_RELEVANCE = 0.15     # 질의 토큰 커버리지 하한(측면 관계에만 적용)
EMPTY_NODE_PENALTY = 0.35        # 본문/실체가 없는 placeholder 노드 감점


# --------------------------------------------------------------------------- #
# 0) 공용 헬퍼
# --------------------------------------------------------------------------- #
def _fetch(conn, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    if conn is None:
        return []
    try:
        return _db.fetchall(conn, sql, params)
    except sqlite3.OperationalError:  # 스키마 부분 부재 방어(analysis.py 규율)
        return []


def _one(conn, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    if conn is None:
        return None
    try:
        return _db.fetchone(conn, sql, params)
    except sqlite3.OperationalError:
        return None


def _in_clause(n: int) -> str:
    return ",".join("?" * n)


def _get_index(scope: str, index_dir, index) -> _index.HybridIndex:
    if index is not None:
        return index
    return _index.load_index(scope, index_dir=index_dir)


# 조례명에서 주제 앵커를 뽑을 때 버릴 기능어(도메인 무관 → 앵커가 되면 오확장).
_ANCHOR_STOP = {
    "조례", "규칙", "시행", "관한", "관하여", "대한", "위한", "위하여", "및", "등",
    "지원", "운영", "관리", "설치", "촉진", "육성", "진흥", "보호", "지도", "감독",
    "사무", "위임", "조성", "이용", "제한", "특별", "기본", "일부", "전부", "개정",
    "폐지", "제정", "지방", "지역", "주민", "시민", "군민", "구민", "행정", "사업",
    "기금", "수수료", "부담금", "위원회", "협의회", "센터", "재단", "공단", "공사",
    "조성및지원", "지원조례", "설치및운영", "관리조례",
    # 정책 주제를 가리키지 않는 서술어·형식어(앵커가 되면 전 분야로 오확장)
    "기준", "지급", "근거", "법령", "법률", "규정", "계획", "신청", "대상", "방법",
    "절차", "금액", "조치", "실시", "추진", "확대", "강화", "개선", "활성화", "제공",
    # 질의문에 섞여 들어오는 일상어
    "우리", "없는", "있는", "어떤", "무엇", "관련", "경우", "이상", "이하", "필요",
    # 연결어(‘야생동물에 의한 피해보상’처럼 조례명 중간에 자주 끼어 앵커로 오인된다)
    "의한", "인한", "따른", "대한", "관하여", "대하여", "위해", "통한", "함께",
}
# 지자체 명칭 접미(앵커에서 제외 — '종로구 반려동물 조례'에서 '종로구'가 앵커가 되면
# 같은 지자체 전 조례가 딸려온다).
_REGION_SUFFIX = ("특별자치시", "특별자치도", "광역시", "특별시", "자치시", "자치도",
                  "교육청", "의회", "도", "시", "군", "구")
_NAME_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")


# 4글자 이상 토큰의 꼬리에서만 떼는 조사. '로/도/가/이/을/의' 는 제외했다 —
# '자전거도로'·'자연마을'·'민주주의'·'농가'처럼 정상 어휘를 잘라먹기 때문이다.
_TAIL_PARTICLES = ("에", "를", "은", "는", "와", "과")


def _strip_particle(word: str) -> str:
    """조사가 붙은 토큰을 원형으로 되돌린다('지원에'→'지원', '야생동물에'→'야생동물').

    ① 어간이 기능어 사전에 있으면 무조건 절단(‘지원에’),
    ② 그 밖에는 4글자 이상 + 안전한 조사 꼬리일 때만 절단(‘야생동물에’).
    """
    for cut in (1, 2):
        if len(word) > cut and word[:-cut] in _ANCHOR_STOP:
            return word[:-cut]
    if len(word) >= 4 and word.endswith(_TAIL_PARTICLES):
        return word[:-1]
    return word


def _looks_like_region(word: str, drop_text: str) -> bool:
    """지자체명으로 보이는 토큰인지(원천 org_name 대조 + 행정구역 접미 휴리스틱)."""
    if drop_text and word in drop_text:
        return True
    return len(word) >= 3 and word.endswith(_REGION_SUFFIX)


def anchor_terms(text: Optional[str], *, top: int = 3, min_len: int = 2,
                 drop: Optional[str] = None) -> list[str]:
    """법규명/질의문에서 '주제 앵커' 토큰을 추출(기능어·지자체명 제거, 긴 토큰 우선).

    예) '서울특별시 종로구 반려동물 보호 및 지원에 관한 조례'(drop='서울특별시 종로구')
          → ['반려동물']
        '청년 주거 지원' → ['주거', '청년']
    LIKE 확장 질의의 씨앗이 되므로 과다 추출은 오확장을 부른다 → top 개로 제한.
    """
    if not text:
        return []
    drop_text = str(drop or "")
    seen: dict[str, int] = {}
    for w in _NAME_TOKEN_RE.findall(str(text)):
        if len(w) < min_len or w.isdigit():
            continue
        stem = _strip_particle(w)
        if stem in _ANCHOR_STOP:
            continue
        if _looks_like_region(stem, drop_text):
            continue
        seen[stem] = max(seen.get(stem, 0), len(stem))
    # 다른 앵커의 부분문자열은 제거(반려 ⊂ 반려동물)
    words = sorted(seen, key=lambda w: (-len(w), w))
    kept: list[str] = []
    for w in words:
        if any(w != o and w in o for o in kept):
            continue
        kept.append(w)
        if len(kept) >= top:
            break
    return kept


_REL_TOKENIZER = _index.Tokenizer(ngrams=(2,))


def lexical_relevance(query: Optional[str], text: Optional[str]) -> float:
    """질의 토큰이 대상 명칭에 얼마나 덮이는지(0~1). 확장 노드의 주제 이탈 판정용.

    형태소 분석기가 없으므로 어절 토큰 + char 2-gram 집합의 커버리지를 쓴다.
    """
    if not query or not text:
        return 0.0
    q = set(_REL_TOKENIZER.tokens(query))
    if not q:
        return 0.0
    t = set(_REL_TOKENIZER.tokens(text))
    return len(q & t) / len(q)


def _has_content(node_id: Optional[str]) -> bool:
    """실체가 있는 노드인지. 'lawname:…' 은 위임 파싱이 만든 이름-only placeholder다."""
    return bool(node_id) and not str(node_id).startswith("lawname:")


# --------------------------------------------------------------------------- #
# 1) 채널 검색 (+ 본문 보강 / 조례 단위 그룹화)
# --------------------------------------------------------------------------- #
def _fetch_bodies(conn, hits: list[dict], *, max_chars: int = 1200) -> None:
    """hits 에 조문 원문(text)을 인플레이스로 채운다. 인덱스는 본문을 저장하지 않는다."""
    if conn is None or not hits:
        return
    oa = [h["doc_key"] for h in hits if h.get("doc_kind") == "ordinance_article"]
    art = [h["doc_key"] for h in hits if h.get("doc_kind") == "statute_article"]
    body: dict[str, str] = {}
    for keys, sql in ((oa, "SELECT oa_id AS k, title AS t, body AS b FROM ordinance_articles "
                           "WHERE oa_id IN ({})"),
                      (art, "SELECT article_id AS k, title AS t, body AS b FROM articles "
                            "WHERE article_id IN ({})")):
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            for r in _fetch(conn, sql.format(_in_clause(len(chunk))), tuple(chunk)):
                txt = r.get("b") or r.get("t") or ""
                body[r["k"]] = txt[:max_chars]
    for h in hits:
        h["text"] = body.get(h["doc_key"], "")


def _group_by_parent(hits: list[dict], k: int) -> list[dict]:
    """조문 히트를 소속 법규(조례/법령) 단위로 접는다. 대표 = 최고점 조문."""
    best: dict[str, dict] = {}
    for h in hits:
        pid = h.get("parent_id") or h["doc_key"]
        cur = best.get(pid)
        if cur is None:
            top = dict(h)
            top["matched_articles"] = [{"doc_key": h["doc_key"], "article_no": h.get("article_no"),
                                        "article_title": h.get("article_title"),
                                        "score": h.get("score")}]
            best[pid] = top
        else:
            cur["matched_articles"].append({"doc_key": h["doc_key"],
                                            "article_no": h.get("article_no"),
                                            "article_title": h.get("article_title"),
                                            "score": h.get("score")})
    out = sorted(best.values(), key=lambda h: -float(h.get("score") or 0.0))[:k]
    for i, h in enumerate(out, start=1):
        h["rank"] = i
        h["article_hits"] = len(h["matched_articles"])
    return out


def bm25_search(conn, query: str, k: int = 10, *, scope: str = "all",
                index: Optional[_index.HybridIndex] = None, index_dir=None,
                group_by: Optional[str] = None, with_text: bool = False,
                max_df_ratio: Optional[float] = None) -> list[dict]:
    """BM25 단일 채널 검색(비교·진단용).

    max_df_ratio 는 '이 비율 넘게 등장하는 흔한 term 은 버린다'는 컷오프다(index 기본 0.6).
    코퍼스가 작을수록 이 컷오프는 위험하다 — 문서 3건짜리 scope 에서는 질의어가
    3건 모두에 나오는 순간 df/N=1.0 > 0.6 이라 **전 term 이 탈락해 결과가 0건**이 된다.
    소규모 scope(region:*, sig:*)에서 이를 완화하려면 1.0 을 넘겨 컷오프를 사실상 끈다.
    """
    idx = _get_index(scope, index_dir, index)
    kw = {} if max_df_ratio is None else {"max_df_ratio": float(max_df_ratio)}
    hits = idx.bm25_search(query, k if not group_by else max(k * 8, 40), **kw)
    if group_by == "parent":
        hits = _group_by_parent(hits, k)
    if with_text:
        _fetch_bodies(conn, hits)
    return hits


def dense_search(conn, query: str, k: int = 10, *, scope: str = "all",
                 index: Optional[_index.HybridIndex] = None, index_dir=None,
                 group_by: Optional[str] = None, with_text: bool = False) -> list[dict]:
    """Dense(코사인) 단일 채널 검색(비교·진단용)."""
    idx = _get_index(scope, index_dir, index)
    hits = idx.dense_search(query, k if not group_by else max(k * 8, 40))
    if group_by == "parent":
        hits = _group_by_parent(hits, k)
    if with_text:
        _fetch_bodies(conn, hits)
    return hits


def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [1.0] * len(values)
    span = hi - lo
    return [(v - lo) / span for v in values]


def hybrid_search(conn, query: str, k: int = 10, *, scope: str = "all",
                  method: str = "rrf", alpha: float = DEFAULT_ALPHA,
                  rrf_k: int = DEFAULT_RRF_K, candidate_k: Optional[int] = None,
                  index: Optional[_index.HybridIndex] = None, index_dir=None,
                  group_by: Optional[str] = "parent", with_text: bool = False) -> list[dict]:
    """BM25 + Dense 하이브리드 검색.

    method='rrf'      : Reciprocal Rank Fusion. score = Σ 1/(rrf_k + rank).
                        점수 스케일이 완전히 다른 두 채널(BM25 수십점 vs 코사인 0~1)을
                        스케일 보정 없이 합칠 수 있어 기본값으로 채택.
    method='weighted' : 채널별 min-max 정규화 후 alpha·BM25 + (1-alpha)·Dense.

    group_by='parent' 이면 조문 히트를 소속 조례/법령 단위로 접어 반환한다.
    CONTRACTS.md 관례에 따라 conn 을 첫 인자로 받되, conn=None 이면 본문 보강만 생략된다.
    """
    idx = _get_index(scope, index_dir, index)
    cand = candidate_k or max(50, k * 10)
    t0 = time.time()
    b_hits = idx.bm25_search(query, cand)
    d_hits = idx.dense_search(query, cand)

    fused: dict[str, dict] = {}

    def _slot(h: dict) -> dict:
        s = fused.get(h["doc_key"])
        if s is None:
            s = dict(h)
            s.pop("score", None)
            s["bm25_score"] = 0.0
            s["bm25_rank"] = None
            s["dense_score"] = 0.0
            s["dense_rank"] = None
            fused[h["doc_key"]] = s
        return s

    for rank, h in enumerate(b_hits, start=1):
        s = _slot(h)
        s["bm25_score"] = h["score"]
        s["bm25_rank"] = rank
    for rank, h in enumerate(d_hits, start=1):
        s = _slot(h)
        s["dense_score"] = h["score"]
        s["dense_rank"] = rank

    if method == "weighted":
        keys = list(fused)
        bn = _minmax([fused[x]["bm25_score"] for x in keys])
        dn = _minmax([fused[x]["dense_score"] for x in keys])
        for key, b, d in zip(keys, bn, dn):
            s = fused[key]
            # 채널에 아예 뜨지 않은 문서는 정규화 하한이 아니라 0 으로 취급
            b = b if s["bm25_rank"] else 0.0
            d = d if s["dense_rank"] else 0.0
            s["score"] = round(alpha * b + (1.0 - alpha) * d, 6)
            s["method"] = "hybrid-weighted"
    else:
        for s in fused.values():
            sc = 0.0
            if s["bm25_rank"]:
                sc += 1.0 / (rrf_k + s["bm25_rank"])
            if s["dense_rank"]:
                sc += 1.0 / (rrf_k + s["dense_rank"])
            s["score"] = round(sc, 8)
            s["method"] = "hybrid-rrf"

    hits = sorted(fused.values(), key=lambda h: (-h["score"], h["doc_key"]))
    if group_by == "parent":
        hits = _group_by_parent(hits, k)
    else:
        hits = hits[:k]
        for i, h in enumerate(hits, start=1):
            h["rank"] = i
    if with_text:
        _fetch_bodies(conn, hits)
    for h in hits:
        h["took_ms"] = int((time.time() - t0) * 1000)
    return hits


# 후보 풀 크기의 단일 출처는 rerank.py 다(여기서 재정의하면 두 값이 갈라진다).
# rerank 모듈 임포트는 표준 라이브러리만 끌어오므로 값싸다 — torch/sentence-transformers
# 로드는 _load() 안에서만 일어난다.
from .rerank import DEFAULT_CANDIDATES  # noqa: E402  (섹션 배치상 여기가 의미가 맞다)


def _candidates(conn, query: str, pool: int, *, scope: str, idx, group_by, k: int) -> list[dict]:
    """재랭킹에 넣을 후보를 만든다. BM25 우선, 굶으면 단계적으로 완화한다.

    왜 사다리가 필요한가(실측으로 드러난 함정):
      index.bm25_search 의 max_df_ratio 기본값 0.6 은 대형 코퍼스에서는 불용어 제거로
      잘 동작하지만, **작은 scope 에서는 전 term 을 탈락시켜 0건을 만든다.**
      실제로 문서 3건짜리 샌드박스에서 '주차장 설치 및 관리' 는 k 와 무관하게 0건이었다
      (질의어가 3건 모두에 등장 → df/N=1.0 > 0.6 → 전 term 탈락).
      기존 hybrid_search 는 dense 채널이 이 구멍을 덮어 주어 증상이 보이지 않았을 뿐이다.
      후보 생성을 BM25 단독으로 바꾸는 순간 이 결함이 그대로 노출되므로 여기서 막는다.

    사다리: ① 기본 BM25 → ② df 컷오프 해제 BM25 → ③ dense 로 부족분 보충.
    ③ 은 최후수단이다. 대형 코퍼스에서 dense 후보를 섞으면 오히려 나빠진다는 실측이 있어
    (BM25+Dense 합집합 < BM25 단독), **BM25 가 굶었을 때만** 발동한다.
    """
    hits = bm25_search(conn, query, pool, scope=scope, index=idx, group_by=group_by)
    if len(hits) >= k:
        return hits
    seen = {h["doc_key"] for h in hits}
    for h in bm25_search(conn, query, pool, scope=scope, index=idx,
                         group_by=group_by, max_df_ratio=1.0):
        if h["doc_key"] not in seen:
            seen.add(h["doc_key"])
            hits.append(h)
    if len(hits) >= k:
        return hits
    for h in dense_search(conn, query, pool, scope=scope, index=idx, group_by=group_by):
        if h["doc_key"] not in seen:
            seen.add(h["doc_key"])
            hits.append(h)
    return hits


def search(conn, query: str, k: int = 10, *, scope: str = "all",
           candidates: int = DEFAULT_CANDIDATES, rerank: bool = True,
           text_chars: int = 700, index: Optional[_index.HybridIndex] = None,
           index_dir=None, group_by: Optional[str] = "parent",
           with_text: bool = False) -> list[dict]:
    """**권장 기본 검색 경로** — BM25 후보 생성 → cross-encoder 재랭킹.

    왜 hybrid_search 가 아니라 이것이 기본인가
    (evalset.EVAL36 36질의·조례단위 top-5 실측, 62,460문서 인덱스, 2026-08-20):

        방식                          P@1      P@5      Success@5   s/query
        BM25 단독                     0.5278   0.4333   0.6111      0.01
        dense 단독(char n-gram TF)    0.3056   0.2944   0.5556      0.01
        hybrid RRF(구 기본값)          0.5000   0.4056   0.6667      0.00
        hybrid+graph(구 기본값)        0.4722   0.4222   0.6667      0.70
        **search() CE 재랭킹(이 함수)  0.6667   0.5611   0.8056      9.41**

    BM25 대비 P@1 +26.3%, P@5 +29.5%, Success@5 +31.8%.
    후보 풀(BM25 top-30)에 정답이 들어있는 질의는 36건 중 30건(0.8333)이므로,
    이 재랭킹은 **후보군 천장의 96.7% 를 회수**한다. 남은 손실은 랭킹이 아니라
    코퍼스 미수집(정답 조례가 인덱스에 아예 없는 질의 2/36)이 원인이다.

    코퍼스가 커질수록 재랭킹이 **더** 중요해진다(같은 평가셋, 258,592문서 인덱스 실측):

        방식                  P@1                  P@5                Success@5
        BM25 단독             0.5278 → 0.3889 ↓    0.4333 → 0.4500    0.6111 → 0.6389
        hybrid RRF            0.5000 → 0.4444 ↓    0.4056 → 0.4278    0.6667 → 0.5833
        search() CE cand=30   0.6667 → 0.6667 =    0.5611 → 0.6167 ↑  0.8056 → 0.7222 ↓

    본문 수집이 진행되며 표준조례 복제본이 쏟아지자 BM25 의 P@1 은 26% 무너졌지만
    (동일 주제 조례 수십 건이 1위 자리를 다툰다), 재랭킹은 P@1 을 그대로 지켰다.
    4배 코퍼스에서 BM25 대비 P@1 격차는 +26% 에서 **+71%** 로 벌어진다.
    반면 Success@5 는 0.8056→0.7222 로 떨어졌다 — 후보 풀 30 이 4배 커진 코퍼스를
    담기엔 좁아졌다는 뜻이므로, 수집이 끝나면 candidates 를 함께 키워야 한다.

    현행 'dense' 채널은 dense 가 아니다(meta.json: model=char-ngram-tf,
    dense_kind=sparse, dense_dim=0). BM25 와 같은 자질 공간에서 IDF 없이 raw TF
    코사인을 계산하는 열화 복제본이라, 융합해도 BM25 단독을 못 이긴다.
    따라서 후보 생성은 **BM25 단독**으로 하고(합집합은 실측상 더 나빴다),
    의미 매칭은 질의·문서를 함께 인코딩하는 cross-encoder 에 맡긴다.

    rerank=False 면 순수 BM25 로 동작한다(A/B 비교·재랭커 부재 환경용).
    재랭커 모델이 없으면 rerank.py 가 재정렬을 생략해 BM25 순서를 그대로 보존한다
    (규칙 재랭킹은 실측상 BM25 보다 나빠서 기본 폴백에서 제외했다).
    """
    idx = _get_index(scope, index_dir, index)
    if not rerank:
        # 재랭킹을 꺼도 후보 사다리는 그대로 쓴다 — 소규모 scope 에서 df 컷오프로
        # 0건이 되는 문제는 재랭킹과 무관한 BM25 자체의 결함이기 때문이다.
        hits = _candidates(conn, query, k, scope=scope, idx=idx,
                           group_by=group_by, k=k)[:k]
        for i, h in enumerate(hits, start=1):
            h["rank"] = i
        if with_text:
            _fetch_bodies(conn, hits, max_chars=text_chars)
        return hits
    pool = max(int(candidates), k)
    hits = _candidates(conn, query, pool, scope=scope, idx=idx, group_by=group_by, k=k)
    # 재랭커는 본문을 봐야 한다. 조례명만으로는 표준조례 복제(문서14 DRM)를 못 가른다.
    if conn is not None:
        _fetch_bodies(conn, hits, max_chars=text_chars)
    # rerank 모듈 자체는 위에서 이미 임포트했다(표준 라이브러리만 쓰므로 값싸다).
    # torch/sentence-transformers 로드는 아래 호출 안의 _load() 에서 처음 일어난다 —
    # 즉 재랭킹을 쓰지 않는 경로(MCP 기동, rerank=False)는 모델 비용을 전혀 내지 않는다.
    from .rerank import rerank as _rerank
    out = _rerank(query, hits, k, candidates=pool)
    if not with_text:
        for h in out:
            h.pop("text", None)
    return out


# --------------------------------------------------------------------------- #
# 2) 그래프 확장 (GraphRAG)
# --------------------------------------------------------------------------- #
_ORD_COLS = ("SELECT ordinance_id, name, region_id, org_name, ord_kind, official_url, "
             "enacted_on, status, verification_status FROM ordinances")


def _seed_nodes(hits: list[dict]) -> list[dict]:
    """검색 히트 → 확장 시드(소속 조례/법령 단위, 중복 제거, 순위 가중 부여)."""
    seeds: list[dict] = []
    seen: set[str] = set()
    for i, h in enumerate(hits):
        pid = h.get("parent_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        seeds.append({
            "id": pid,
            "kind": "ordinance" if h.get("doc_kind") == "ordinance_article" else "instrument",
            "name": h.get("parent_name"),
            "region_id": h.get("region_id"),
            "org_name": h.get("org_name"),
            "official_url": h.get("official_url"),
            "seed_rank": i + 1,
            "seed_weight": 1.0 / (0.5 + i + 1),
            "doc_key": h.get("doc_key"),
            "article_no": h.get("article_no"),
        })
    return seeds


def _path_str(src_kind: str, src_id: str, rel: str, dst_kind: str, dst_id: str,
              detail: str = "") -> str:
    """graph.build.node_id 네임스페이스로 근거 경로 1홉을 문자열화."""
    u = _gbuild.node_id(src_kind, src_id)
    v = _gbuild.node_id(dst_kind, dst_id)
    arrow = f"-[{rel}{(':' + detail) if detail else ''}]->"
    return f"{u} {arrow} {v}"


def _expand_delegated_from(conn, seed: dict, limit: int) -> list[dict]:
    if seed["kind"] != "ordinance":
        return []
    rows = _fetch(conn,
                  "SELECT d.parent_id, d.parent_article, d.child_article, d.relation, "
                  "       d.delegation_type, d.citation_text, d.source_path, "
                  "       l.name AS parent_name, l.official_url, l.kind AS parent_kind_name "
                  "FROM delegations d "
                  "LEFT JOIN legal_instrument l ON l.instrument_id = d.parent_id "
                  "WHERE d.child_kind='ordinance' AND d.child_id=? "
                  "ORDER BY (d.parent_article IS NULL), d.source_path LIMIT ?",
                  (seed["id"], limit))
    out = []
    for r in rows:
        out.append({
            "node_type": "instrument",
            "id": r["parent_id"],
            "name": r.get("parent_name") or r["parent_id"],
            "via": "DELEGATED_FROM",
            "strength": 1.0,
            "official_url": r.get("official_url"),
            "evidence": {"parent_article": r.get("parent_article"),
                         "child_article": r.get("child_article"),
                         "delegation_type": r.get("delegation_type"),
                         "citation_text": (r.get("citation_text") or "")[:200],
                         "source_path": r.get("source_path")},
            "path": _path_str("ordinance", seed["id"], "DELEGATED_FROM", "instrument",
                              r["parent_id"], r.get("parent_article") or ""),
        })
    return out


def _expand_similar_to(conn, seed: dict, limit: int) -> list[dict]:
    if seed["kind"] != "ordinance":
        return []
    rows = _fetch(conn,
                  "SELECT s.dst_id, s.cosine_sim, s.rank, o.name, o.region_id, o.org_name, "
                  "       o.official_url, o.ord_kind FROM similarity_edges s "
                  "JOIN ordinances o ON o.ordinance_id = s.dst_id "
                  "WHERE s.src_id=? ORDER BY s.cosine_sim DESC LIMIT ?",
                  (seed["id"], limit))
    return [{
        "node_type": "ordinance",
        "id": r["dst_id"],
        "name": r.get("name"),
        "region_id": r.get("region_id"),
        "org_name": r.get("org_name"),
        "official_url": r.get("official_url"),
        "via": "SIMILAR_TO",
        "strength": float(r.get("cosine_sim") or 0.0),
        "evidence": {"cosine_sim": round(float(r.get("cosine_sim") or 0.0), 4),
                     "knn_rank": r.get("rank")},
        "path": _path_str("ordinance", seed["id"], "SIMILAR_TO", "ordinance", r["dst_id"],
                          f"{float(r.get('cosine_sim') or 0):.3f}"),
    } for r in rows]


def _ordinances_by_anchor(conn, region_ids: list[str], anchors: list[str], *,
                          limit: int, exclude: set[str]) -> list[dict]:
    """지역 목록 × 주제 앵커 LIKE 로 조례 조회. 본문 미수집 조례까지 도달하는 경로."""
    if not region_ids or not anchors:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for anc in anchors:
        # `+status` 의 단항 +는 SQLite 가 그 항으로 인덱스를 고르지 못하게 막는 표준 기법이다.
        # 없으면 플래너가 선택도 낮은 ix_ord_status 를 잡아 활성 조례 15만행을 훑는다
        # (실측: 앵커 1건당 ~170ms → 8ms). region_id 로 ix_ord_region 을 타게 강제한다.
        rows = _fetch(conn,
                      f"{_ORD_COLS} WHERE region_id IN ({_in_clause(len(region_ids))}) "
                      f"AND +status='active' AND name LIKE ? "
                      f"ORDER BY (verification_status='source-linked') DESC, enacted_on DESC "
                      f"LIMIT ?",
                      tuple(region_ids) + (f"%{anc}%", limit))
        for r in rows:
            oid = r["ordinance_id"]
            if oid in exclude or oid in seen:
                continue
            seen.add(oid)
            r["_anchor"] = anc
            out.append(r)
        if len(out) >= limit:
            break
    return out[:limit]


def _expand_same_region(conn, seed: dict, anchors: list[str], limit: int,
                        exclude: set[str]) -> list[dict]:
    rid = seed.get("region_id")
    if not rid:
        return []
    rows = _ordinances_by_anchor(conn, [rid], anchors, limit=limit, exclude=exclude)
    return [{
        "node_type": "ordinance",
        "id": r["ordinance_id"],
        "name": r.get("name"),
        "region_id": r.get("region_id"),
        "org_name": r.get("org_name"),
        "official_url": r.get("official_url"),
        "via": "SAME_REGION",
        "strength": 1.0 if r.get("verification_status") == "source-linked" else 0.8,
        "evidence": {"anchor": r.get("_anchor"), "same_region_id": rid,
                     "enacted_on": r.get("enacted_on"),
                     "body_indexed": r.get("verification_status") == "source-linked"},
        "path": (_path_str("region", rid, "HAS_ORDINANCE", "ordinance", r["ordinance_id"])
                 + f"  (앵커 '{r.get('_anchor')}')"),
    } for r in rows]


def _expand_adjacent_region(conn, seed: dict, anchors: list[str], limit: int,
                            exclude: set[str]) -> list[dict]:
    rid = seed.get("region_id")
    if not rid:
        return []
    neigh = [r["neighbor_id"] for r in _fetch(
        conn, "SELECT neighbor_id FROM region_adjacency WHERE region_id=? ORDER BY neighbor_id",
        (rid,))]
    if not neigh:
        return []
    rows = _ordinances_by_anchor(conn, neigh, anchors, limit=limit, exclude=exclude)
    out = []
    for r in rows:
        nb = r.get("region_id")
        out.append({
            "node_type": "ordinance",
            "id": r["ordinance_id"],
            "name": r.get("name"),
            "region_id": nb,
            "org_name": r.get("org_name"),
            "official_url": r.get("official_url"),
            "via": "ADJACENT_REGION",
            "strength": 0.9 if r.get("verification_status") == "source-linked" else 0.7,
            "evidence": {"anchor": r.get("_anchor"), "from_region_id": rid,
                         "neighbor_region_id": nb, "enacted_on": r.get("enacted_on"),
                         "body_indexed": r.get("verification_status") == "source-linked"},
            "path": (_path_str("region", rid, "ADJACENT_TO", "region", nb or "?")
                     + " " + _path_str("region", nb or "?", "HAS_ORDINANCE", "ordinance",
                                       r["ordinance_id"])
                     + f"  (앵커 '{r.get('_anchor')}')"),
        })
    return out


def _expand_funded_by(conn, seed: dict, limit: int) -> list[dict]:
    if seed["kind"] != "ordinance":
        return []
    rows = _fetch(conn,
                  "SELECT l.budget_id, l.confidence, l.match_method, b.dbiz_nm, b.fyr, "
                  "       b.alloc_amt, b.exe_amt, b.field, b.sector, b.laf_cd "
                  "FROM ordinance_budget_link l "
                  "JOIN budget_lines b ON b.budget_id = l.budget_id "
                  "WHERE l.ordinance_id=? ORDER BY l.confidence DESC, b.fyr DESC LIMIT ?",
                  (seed["id"], limit))
    return [{
        "node_type": "budget_line",
        "id": r["budget_id"],
        "name": r.get("dbiz_nm"),
        "via": "FUNDED_BY",
        "strength": float(r.get("confidence") or 0.0),
        "evidence": {"fyr": r.get("fyr"), "alloc_amt": r.get("alloc_amt"),
                     "exe_amt": r.get("exe_amt"), "field": r.get("field"),
                     "sector": r.get("sector"), "match_method": r.get("match_method"),
                     "confidence": round(float(r.get("confidence") or 0.0), 4)},
        "path": _path_str("ordinance", seed["id"], "FUNDED_BY", "budget", r["budget_id"],
                          str(r.get("fyr") or "")),
    } for r in rows]


def _expand_cites(conn, seed: dict, limit: int) -> list[dict]:
    kind = "ordinance" if seed["kind"] == "ordinance" else "instrument"
    rows = _fetch(conn,
                  "SELECT r.dst_id, r.dst_kind, r.relation, r.citation_text, r.citation_type, "
                  "       r.src_article, l.name AS dst_name, l.official_url "
                  "FROM instrument_relations r "
                  "LEFT JOIN legal_instrument l ON l.instrument_id = r.dst_id "
                  "WHERE r.src_kind=? AND r.src_id=? AND r.relation='CITES' LIMIT ?",
                  (kind, seed["id"], limit))
    return [{
        "node_type": "instrument" if r.get("dst_kind") == "instrument" else "ordinance",
        "id": r["dst_id"],
        "name": r.get("dst_name") or r["dst_id"],
        "official_url": r.get("official_url"),
        "via": "CITES",
        "strength": 0.8,
        "evidence": {"citation_text": (r.get("citation_text") or "")[:200],
                     "citation_type": r.get("citation_type"),
                     "src_article": r.get("src_article")},
        "path": _path_str(kind, seed["id"], "CITES",
                          r.get("dst_kind") or "instrument", r["dst_id"]),
    } for r in rows]


def graph_expand(conn, hits: list[dict], hops: int = 1, *, per_relation: int = 4,
                 limit: int = 40, relations: Iterable[str] = DEFAULT_RELATIONS,
                 max_seeds: int = 6, query: Optional[str] = None,
                 include_seeds: bool = False) -> list[dict]:
    """검색 히트 → 그래프 이웃으로 컨텍스트 확장(GraphRAG 핵심).

    hits 의 소속 조례/법령을 시드로 삼아 위임 상위법·유사조례·동일 지자체·인접 지자체·
    집행예산·인용 관계를 따라간다. 반환 항목마다 `via`(관계) / `path`(근거 경로 문자열) /
    `evidence`(관계 근거 필드) / `hop` / `seed` 를 붙여 **왜 딸려왔는지 추적 가능**하게 한다.

    hops>=2 는 1홉 결과 중 조례 노드에서 다시 DELEGATED_FROM/SIMILAR_TO 만 따라간다
    (지역 확장까지 재귀하면 조합폭발 → 관계 축소).
    """
    rels = set(relations or ())
    seeds = _seed_nodes(hits)[:max_seeds]
    if not seeds:
        return []
    seed_ids = {s["id"] for s in seeds}
    # include_seeds=True 면 시드 자신도 확장 결과로 방출한다(is_seed=True 표시).
    # 용도: 랭킹 융합에서 '검색 상위이면서 그래프로도 도달되는' 상호확증 문서를 식별.
    # 기존 호출부(answer_context 등)는 새 컨텍스트만 원하므로 기본값 False 로 동작 불변.
    region_exclude = set() if include_seeds else seed_ids
    seen: set[tuple] = set()
    out: list[dict] = []

    def _emit(items: list[dict], seed: dict, hop: int) -> None:
        for it in items:
            key = (it["node_type"], it["id"])
            if key in seen:
                continue
            if it["id"] in seed_ids:
                if not include_seeds or it["id"] == seed["id"]:
                    continue        # 자기 자신으로의 확장은 언제나 무의미
                it["is_seed"] = True
            rel = lexical_relevance(query, it.get("name"))
            if query and it["via"] in LATERAL_RELATIONS and rel < LATERAL_MIN_RELEVANCE:
                continue                      # 주제 이탈한 측면 확장은 버린다
            seen.add(key)
            w = REL_WEIGHT.get(it["via"], 0.5)
            factor = 1.0 if _has_content(it["id"]) else EMPTY_NODE_PENALTY
            if query and it["via"] in LATERAL_RELATIONS:
                factor *= 0.5 + 0.5 * min(1.0, rel / 0.5)   # 주제 근접도 비례 가중
            it["hop"] = hop
            it["seed"] = seed["id"]
            it["seed_name"] = seed.get("name")
            it["query_relevance"] = round(rel, 4)
            it["score"] = round(seed["seed_weight"] * w * float(it.get("strength") or 0.5)
                                * factor * (HOP_DECAY ** (hop - 1)), 6)
            out.append(it)

    q_anchors = anchor_terms(query, top=4)
    for seed in seeds:
        anchors = anchor_terms(seed.get("name"),
                               drop=f"{seed.get('org_name') or ''} {seed.get('region_id') or ''}")
        if q_anchors:   # 질의 주제와 겹치는 앵커를 앞세워 확장을 온토픽으로 유지
            overlap = [a for a in anchors if any(a in q or q in a for q in q_anchors)]
            anchors = overlap + [a for a in anchors if a not in overlap] if overlap else                 anchors or q_anchors
        anchors = anchors or q_anchors
        if "DELEGATED_FROM" in rels:
            _emit(_expand_delegated_from(conn, seed, per_relation), seed, 1)
        if "SIMILAR_TO" in rels:
            _emit(_expand_similar_to(conn, seed, per_relation), seed, 1)
        if "SAME_REGION" in rels:
            _emit(_expand_same_region(conn, seed, anchors, per_relation, region_exclude), seed, 1)
        if "ADJACENT_REGION" in rels:
            _emit(_expand_adjacent_region(conn, seed, anchors, per_relation, region_exclude), seed, 1)
        if "FUNDED_BY" in rels:
            _emit(_expand_funded_by(conn, seed, per_relation), seed, 1)
        if "CITES" in rels:
            _emit(_expand_cites(conn, seed, per_relation), seed, 1)

    # 2홉: 1홉에서 얻은 조례에서 위임·유사만 재확장
    for hop in range(2, max(1, hops) + 1):
        frontier = [n for n in out if n["hop"] == hop - 1 and n["node_type"] == "ordinance"]
        frontier.sort(key=lambda n: -n["score"])
        for node in frontier[:max_seeds]:
            sub = {"id": node["id"], "kind": "ordinance", "name": node.get("name"),
                   "region_id": node.get("region_id"),
                   "seed_weight": node["score"] / max(HOP_DECAY, 1e-9)}
            if "DELEGATED_FROM" in rels:
                _emit(_expand_delegated_from(conn, sub, max(2, per_relation // 2)), sub, hop)
            if "SIMILAR_TO" in rels:
                _emit(_expand_similar_to(conn, sub, max(2, per_relation // 2)), sub, hop)

    out.sort(key=lambda n: (-n["score"], n["node_type"], str(n["id"])))
    return out[:limit]


# --------------------------------------------------------------------------- #
# 2-b) 그래프 증강 랭킹 — 하이브리드 랭크 ⊕ 그래프 확장 랭크 (RRF)
# --------------------------------------------------------------------------- #
def hybrid_graph_search(conn, query: str, k: int = 10, *, scope: str = "all",
                        hops: int = 1, method: str = "rrf", rrf_k: int = DEFAULT_RRF_K,
                        seed_k: Optional[int] = None, expand_limit: int = 60,
                        per_relation: int = 6, max_seeds: int = 8,
                        graph_weight: float = 0.5,
                        index: Optional[_index.HybridIndex] = None, index_dir=None,
                        with_text: bool = False) -> list[dict]:
    """하이브리드 검색 결과와 그래프 확장 결과를 하나의 랭킹으로 융합(GraphRAG 검색).

    두 랭크 리스트를 RRF 로 합친다:
        score = 1/(rrf_k + 검색랭크) + graph_weight · 1/(rrf_k + 그래프랭크)
    → ① 검색·그래프 양쪽에서 나온 문서가 상위로 올라가고(상호 확증),
      ② 본문이 없어 전문검색으로는 **도달 불가능한** 조례가 그래프 경로로 랭킹에 진입한다.

    graph_weight 기본값 0.5 의 실제 의미(검증 실측, 2026-08-19):
      그래프 '단독' 노드의 최대 점수는 graph_weight/(rrf_k+1)=0.5/61=0.00820 이고,
      검색 seed_k 번째 히트는 1/(rrf_k+seed_k)=1/70=0.01429 다. seed_k=max(k,10)≥k 이므로
      **그래프 단독 노드는 top-k 에 구조적으로 진입할 수 없다**(진입 조건:
      graph_weight > (rrf_k+1)/(rrf_k+seed_k) = 0.871). 즉 0.5 는 그래프 노드가
      검색 순위를 오염시키지 않도록 막는 값이지, 그래프가 랭킹을 개선하는 값이 아니다.
      따라서 이 함수가 hybrid_search 와 달라지는 경로는 **상호확증(origin='both')뿐**이며,
      그것을 가능하게 하려면 graph_expand(include_seeds=True) 가 필요하다(아래 호출부).
      8개 질의 실측에서 상호확증 재정렬은 top-5 구성원을 바꾸지 않아
      MRR/Recall@5/P@5 는 hybrid 와 동일했다 — 이득은 순위가 아니라 근거(path/via) 제공이다.

    반환 항목의 `origin` 은 'search' | 'graph' | 'both', `path`/`via` 는 그래프 근거.
    예산(budget_line) 노드는 문서가 아니므로 랭킹에서 제외하고 컨텍스트로만 쓴다.
    """
    seeds = hybrid_search(conn, query, seed_k or max(k, 10), scope=scope, method=method,
                          index=index, index_dir=index_dir, group_by="parent")
    expanded = graph_expand(conn, seeds, hops=hops, per_relation=per_relation,
                            limit=expand_limit, max_seeds=max_seeds, query=query,
                            include_seeds=True)

    merged: dict[str, dict] = {}
    for rank, h in enumerate(seeds, start=1):
        pid = h.get("parent_id")
        if not pid:
            continue
        merged[pid] = {
            "id": pid, "name": h.get("parent_name"),
            "node_type": ("ordinance" if h.get("doc_kind") == "ordinance_article"
                          else "instrument"),
            "region_id": h.get("region_id"), "org_name": h.get("org_name"),
            "official_url": h.get("official_url"),
            "doc_key": h.get("doc_key"), "article_no": h.get("article_no"),
            "article_title": h.get("article_title"), "doc_kind": h.get("doc_kind"),
            "search_rank": rank, "search_score": h.get("score"),
            "bm25_rank": h.get("bm25_rank"), "dense_rank": h.get("dense_rank"),
            "graph_rank": None, "via": None, "path": None, "evidence": None,
            "origin": "search",
        }
    grank = 0
    for n in expanded:
        if n["node_type"] not in ("ordinance", "instrument"):
            continue
        grank += 1
        cur = merged.get(n["id"])
        if cur is None:
            merged[n["id"]] = {
                "id": n["id"], "name": n.get("name"), "node_type": n["node_type"],
                "region_id": n.get("region_id"), "org_name": n.get("org_name"),
                "official_url": n.get("official_url"),
                "doc_key": None, "article_no": None, "article_title": None,
                "doc_kind": ("ordinance_article" if n["node_type"] == "ordinance"
                             else "statute_article"),
                "search_rank": None, "search_score": None,
                "bm25_rank": None, "dense_rank": None,
                "graph_rank": grank, "via": n["via"], "path": n.get("path"),
                "evidence": n.get("evidence"), "seed": n.get("seed"), "hop": n.get("hop"),
                "origin": "graph",
            }
        else:
            cur["graph_rank"] = grank
            cur["via"] = n["via"]
            cur["path"] = n.get("path")
            cur["evidence"] = n.get("evidence")
            cur["origin"] = "both"

    for m in merged.values():
        sc = 0.0
        if m["search_rank"]:
            sc += 1.0 / (rrf_k + m["search_rank"])
        if m["graph_rank"]:
            sc += graph_weight / (rrf_k + m["graph_rank"])
        m["score"] = round(sc, 8)
        m["method"] = "hybrid+graph-rrf"

    out = sorted(merged.values(), key=lambda m: (-m["score"], str(m["id"])))[:k]
    for i, m in enumerate(out, start=1):
        m["rank"] = i
    if with_text:
        _fetch_bodies(conn, [m for m in out if m.get("doc_key")])
    return out


# --------------------------------------------------------------------------- #
# 2-b) 그래프의 재배치 — 랭킹이 아니라 '연결된 것들' 패널
# --------------------------------------------------------------------------- #
# 검색 랭킹에 그래프를 섞는 것(hybrid_graph_search)은 실측상 무익하다: 16/16 질의에서
# hybrid_search 와 결과가 완전히 동일했고, RRF 산식상 그래프 단독 노드는 top-k 진입이
# 수학적으로 불가능하다(graph_weight 0.5/(60+1)=0.0082 < 검색 최하위 1/(60+10)=0.0143).
# 게다가 우리 주 질의는 "이런 조례 있어?"라는 **단일 홉 조회**다.
# 그래서 그래프를 랭킹에서 빼고, 결과 하단의 근거 패널로 재배치한다 —
# 타깃 과업("우리와 비슷한 지역이 이미 만든 조례 찾기")에는 이쪽이 오히려 정확히 부합한다.
RELATION_LABEL = {
    "DELEGATED_FROM": "상위 위임법령",
    "CITES": "이 조례가 인용하는 규범",
    "FUNDED_BY": "연계 집행예산",
    "SAME_REGION": "같은 지자체의 관련 조례",
    "ADJACENT_REGION": "인접 지자체의 유사 조례",
    "SIMILAR_TO": "내용이 유사한 조례",
}
# 패널 표시 순서 — 근거 강도 순(위임 근거가 가장 강하고, 측면 확산이 가장 약하다).
RELATION_ORDER = ("DELEGATED_FROM", "CITES", "FUNDED_BY",
                  "ADJACENT_REGION", "SIMILAR_TO", "SAME_REGION")


def _is_live_ordinance(conn, ord_ids: list[str]) -> set[str]:
    """폐지·승계된 조례를 걸러 살아있는 ordinance_id 집합만 반환.

    문서14 지적대로 폐지 조례를 '참고하세요'라고 내미는 것은 실무자에게 실질적 해악이다.
    현 스냅샷에서는 전건이 status='active' / repealed_on IS NULL 이라 사실상 통과하지만,
    수집이 진행되면 값이 채워지므로 게이트를 **미리** 걸어 둔다.
    """
    if not ord_ids:
        return set()
    live: set[str] = set()
    for i in range(0, len(ord_ids), 400):     # SQLite 변수 상한 회피 + 짧은 트랜잭션
        chunk = ord_ids[i:i + 400]
        rows = _fetch(conn,
                      "SELECT ordinance_id FROM ordinances "
                      f"WHERE ordinance_id IN ({_in_clause(len(chunk))}) "
                      "  AND repealed_on IS NULL "
                      "  AND (status IS NULL OR status='active') "
                      "  AND (succession_status IS NULL OR succession_status<>'repealed')",
                      chunk)
        live |= {r["ordinance_id"] for r in rows}
    return live


def related_context(conn, hits: list[dict], *, hops: int = 1, per_relation: int = 4,
                    limit: int = 24, query: Optional[str] = None,
                    max_seeds: int = 5, drop_repealed: bool = True) -> list[dict]:
    """검색 결과 → 관계별로 묶인 '이 조례와 연결된 것들' 패널.

    hybrid_graph_search 와 달리 **랭킹을 건드리지 않는다.** 검색 순위는 search() 가
    확정하고, 이 함수는 그 위에 근거(왜 관련 있는지: path)를 얹은 별도 섹션만 만든다.

    반환: [{relation, label, items:[{id,name,region_id,org_name,official_url,
                                    path,evidence,seed,weight}]}] — RELATION_ORDER 순.
    """
    nodes = graph_expand(conn, hits, hops=hops, per_relation=per_relation,
                         limit=limit, max_seeds=max_seeds, query=query)
    if drop_repealed:
        ord_ids = [n["id"] for n in nodes if n.get("node_type") == "ordinance"]
        live = _is_live_ordinance(conn, ord_ids)
        nodes = [n for n in nodes
                 if n.get("node_type") != "ordinance" or n["id"] in live]

    groups: dict[str, list[dict]] = {}
    for n in nodes:
        groups.setdefault(n.get("via") or "OTHER", []).append({
            "id": n.get("id"),
            "name": n.get("name"),
            "node_type": n.get("node_type"),
            "region_id": n.get("region_id"),
            "org_name": n.get("org_name"),
            "official_url": n.get("official_url"),
            "path": n.get("path"),
            "evidence": n.get("evidence"),
            "seed": n.get("seed"),
            "hop": n.get("hop"),
            "weight": n.get("weight"),
        })
    order = list(RELATION_ORDER) + [r for r in groups if r not in RELATION_ORDER]
    return [{"relation": r, "label": RELATION_LABEL.get(r, r), "items": groups[r]}
            for r in order if groups.get(r)]


# --------------------------------------------------------------------------- #
# 3) 최종 컨텍스트 조립
# --------------------------------------------------------------------------- #
MAX_AGE_DAYS = 30      # mcp_server 기본 신선도 임계와 동일 취지


def _fusion_label(rerank: bool, reranker: Optional[str], method: str) -> str:
    """engine.fusion 문자열. 요청한 방식이 아니라 **실제 수행된 방식**을 적는다."""
    if not rerank:
        return "rrf" if method != "weighted" else "weighted"
    if reranker and not reranker.startswith("none"):
        return f"bm25+rerank({reranker})"
    return "bm25(재랭커 부재로 재정렬 없음)"


def _data_as_of(conn) -> Optional[str]:
    row = _one(conn, "SELECT MAX(as_of_date) AS v FROM ordinances")
    return (row or {}).get("v")


def _stale(as_of: Optional[str], *, max_age_days: int = MAX_AGE_DAYS) -> Optional[bool]:
    """수집 기준일이 임계를 넘었는지. 파싱 불가/부재는 판단 보류(None)."""
    from datetime import date
    s = str(as_of or "")[:10].replace("/", "-")
    try:
        y, m, d = (int(x) for x in s.split("-"))
        age = (date.today() - date(y, m, d)).days
    except (TypeError, ValueError):
        return None
    return age > max_age_days


def answer_context(conn, query: str, k: int = 8, *, scope: str = "all", hops: int = 1,
                   method: str = "rrf", index: Optional[_index.HybridIndex] = None,
                   index_dir=None, expand_limit: int = 24, per_relation: int = 4,
                   text_chars: int = 1200, include_community: bool = True,
                   community_top: int = 2, rerank: bool = True,
                   candidates: int = DEFAULT_CANDIDATES) -> dict:
    """질의 → LLM 에 넣을 최종 컨텍스트 묶음(생성은 하지 않음).

    반환 구조:
      query / as_of_date / engine        조회 메타·provenance
      seeds[]                            근거 조문 원문 + 출처 메타(official_url)
      related[]                          관계별 '연결된 것들' 패널(근거 경로 포함) ← 권장
      graph_context[]                    related 의 평탄화 버전(하위호환용, 동일 데이터)
      community_context[]                전역 패턴 요약(community.py; 있을 때만)
      coverage                           몇 개 지자체/조례/법령이 컨텍스트에 들어왔는지
      citations[]                        인용 가능한 출처 목록
      disclaimer / execution_allowed / stale   (mcp_server 안전 규율과 동일)

    검색은 search()(BM25 후보 → cross-encoder 재랭킹)로 수행한다. rerank=False 면
    순수 BM25 로 되돌린다. 그래프는 랭킹에 섞지 않고 related 패널로만 제공한다 —
    실측상 그래프 융합은 순위를 전혀 바꾸지 못했고(16/16 동일) RRF 산식상 바꿀 수도 없다.
    """
    t0 = time.time()
    idx = _get_index(scope, index_dir, index)
    hits = search(conn, query, k, scope=scope, index=idx, group_by="parent",
                  rerank=rerank, candidates=candidates, with_text=False)
    _fetch_bodies(conn, hits, max_chars=text_chars)
    related = related_context(conn, hits, hops=hops, per_relation=per_relation,
                              limit=expand_limit, query=query)
    expanded = [dict(it, via=g["relation"]) for g in related for it in g["items"]]
    _rr_used = (hits[0].get("reranker") if (rerank and hits) else None)

    seeds = []
    for h in hits:
        seeds.append({
            "rank": h.get("rank"),
            "score": h.get("score"),
            "bm25_rank": h.get("base_rank") or h.get("bm25_rank"),
            "dense_rank": h.get("dense_rank"),
            "rerank_score": h.get("rerank_score"),
            "reranker": h.get("reranker"),
            "doc_key": h.get("doc_key"),
            "doc_kind": h.get("doc_kind"),
            "parent_id": h.get("parent_id"),
            "parent_name": h.get("parent_name"),
            "region_id": h.get("region_id"),
            "org_name": h.get("org_name"),
            "article_no": h.get("article_no"),
            "article_title": h.get("article_title"),
            "text": h.get("text", ""),
            "official_url": h.get("official_url"),
            "article_hits": h.get("article_hits"),
            "matched_articles": h.get("matched_articles", [])[:5],
        })

    community_context: list[dict] = []
    if include_community:
        try:
            from .community import global_search as _gs
            community_context = _gs(conn, query, k=community_top)
        except Exception as exc:  # noqa: BLE001 — 커뮤니티 요약 부재는 치명적이지 않음
            _LOG.debug("커뮤니티 컨텍스트 생략: %s", exc)

    regions = {s["region_id"] for s in seeds if s.get("region_id")}
    regions |= {n.get("region_id") for n in expanded if n.get("region_id")}
    citations = []
    for s in seeds:
        if s.get("official_url"):
            citations.append({"label": f"{s['parent_name']} 제{s['article_no']}조",
                              "url": s["official_url"], "kind": s["doc_kind"]})
    for n in expanded:
        if n.get("official_url"):
            citations.append({"label": n.get("name"), "url": n["official_url"],
                              "kind": n["node_type"], "via": n["via"]})

    as_of = _data_as_of(conn) or _util.today_kst()
    return {
        "query": query,
        "as_of_date": as_of,
        "engine": {
            "index_scope": idx.scope,
            "index_docs": idx.n_docs,
            # provenance 는 '의도'가 아니라 '실제로 일어난 일'을 적는다 —
            # rerank=True 로 요청해도 모델이 없으면 BM25 순서 그대로이므로 그렇게 표기한다.
            "fusion": _fusion_label(rerank, _rr_used, method),
            "backend": idx.stats()["backend"],
            "model": idx.model_name,
            "reranker": _rr_used,
            "graph_role": "related-panel",   # 랭킹 미참여(실측상 기여 0)
            "hops": hops,
            "_engine": "policymap.rag.retrieve.answer_context",
        },
        "seeds": seeds,
        "related": related,
        "graph_context": expanded,
        "community_context": community_context,
        "coverage": {
            "seed_docs": len(seeds),
            "graph_nodes": len(expanded),
            "regions": len([r for r in regions if r]),
            "ordinances": len({n["id"] for n in expanded if n["node_type"] == "ordinance"}
                              | {s["parent_id"] for s in seeds
                                 if s["doc_kind"] == "ordinance_article"}),
            "instruments": len({n["id"] for n in expanded if n["node_type"] == "instrument"}
                               | {s["parent_id"] for s in seeds
                                  if s["doc_kind"] == "statute_article"}),
            "budget_lines": len({n["id"] for n in expanded if n["node_type"] == "budget_line"}),
            "relations": sorted({n["via"] for n in expanded}),
        },
        "citations": citations[:40],
        "disclaimer": DISCLAIMER,
        "execution_allowed": False,
        "stale": _stale(as_of),
        "took_ms": int((time.time() - t0) * 1000),
    }


__all__ = [
    "search", "hybrid_search", "hybrid_graph_search", "bm25_search", "dense_search",
    "graph_expand", "related_context", "answer_context", "anchor_terms",
    "DISCLAIMER", "REL_WEIGHT", "RELATION_LABEL", "DEFAULT_CANDIDATES",
]
