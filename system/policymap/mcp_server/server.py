"""policymap.mcp_server.server — 순수 파이썬 stdio JSON-RPC MCP 서버.

설계 근거(단일 진실원천):
  * CONTRACTS.md §4.1  : tool 카탈로그·안전 게이트·응답 봉투 규율
  * db/schema.sql       : 조회 대상 테이블/컬럼
  * policymap/db.py     : fetchone/fetchall/count 만 사용(단일 DB 접점)
  * 명세[mcp_survey] §7~9 : korea100 안전관례(부팅 신선도 불변식·disclaimer·
                            execution_allowed:false·law.go.kr 직링크) 승계,
                            도메인 분석 tool 전면화(검색은 기존 서버에 위임하되
                            로컬 DB 검색/조회는 우리 그래프의 진입점으로 제공).

노출 tool 기본 7종(부모 오케스트레이터 임무 명세):
  search_ordinance      : 로컬 조례 DB 검색(지역·종류·상태 필터)
  get_ordinance         : 조례 원문(조문)·연혁 메타·근거 상위법 조회
  similar_regions       : 행안부 유사자치단체 기준 peer(→ analytics.peers.find_similar_governments,
                          미탑재 시 구 analysis.find_peer_governments 폴백)
  gap_analysis          : 위임 있으나 조례 부재(→ analysis.get_delegation_gap /
                                                 compare_ordinance_coverage)
  diffusion_timeline    : 조례 확산 곡선(→ analytics.diffusion.diffusion_profile,
                          미탑재 시 구 analysis.trace_ordinance_diffusion 폴백)
  region_profile        : 지역 프로파일(조례/예산/인접/승계/최근변경)
  bill_vote_breakdown   : 법안 찬반·정당별 표결 비율

신경망/RAG tool 5종(2단계 확장 — policymap.rag / policymap.neural 위임):
  semantic_search_ordinance : GraphRAG 하이브리드 조문 의미검색(BM25+dense+그래프확장)
  similar_ordinances        : 그래프 임베딩 코사인 유사 조례(neural_similarity)
  neural_similar_regions    : 그래프 임베딩 유사 지자체(통계 기반 similar_regions 와 구분)
  ordinance_effectiveness   : 조례↔예산 링크 집행률(verified/unverified 명시)
  explain_path              : 두 노드 간 그래프 경로 설명(양방향 BFS, 근거 관계 나열)

정책확산 계량 tool 2종(3단계 확장 — policymap.analytics 위임):
  recommend_ordinances      : "비슷한 지자체엔 있고 우리엔 없는 조례"(핵심 실무 산출물)
  spatial_autocorrelation   : 전역 Moran's I + LISA(조건부 순열검정 + BH-FDR)

계약 준수 원칙:
  * 판단성 로직은 graph.analysis 의 계약 함수(§3.2)에 위임. analysis 모듈이
    미탑재(병렬 빌드 중)이거나 무거운 의존성 부재 시 순수 SQL 폴백으로 강등하되,
    응답에 _engine 표시로 provenance 를 남긴다.
  * DB 는 policymap.db 헬퍼로만 접근(읽기 전용 SELECT). write 계열 tool 없음.
  * 모든 응답 봉투에 execution_allowed:false + disclaimer + as_of_date + 신선도
    (stale) 표기. 근거 상위법에는 official_url(law.go.kr) 동봉.

기동:
  python -m policymap.mcp_server.server
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Any, Optional

from .. import config as _config
from .. import db
from .. import util
from ..util import KST

# --------------------------------------------------------------------------- #
# 상수
# --------------------------------------------------------------------------- #
PROTOCOL_VERSION = "2024-11-05"          # MCP stdio 기본 협상 버전(클라이언트 값 우선 에코)
SERVER_NAME = "policymap-ordinance-graph"
SERVER_VERSION = "0.1.0"

DISCLAIMER = (
    "이 응답은 의사결정 지원을 위한 참고 정보이며 법률 판단·유권해석이 아닙니다. "
    "근거 조문과 law.go.kr 원문을 직접 확인하십시오."
)
STALE_WARNING = (
    "데이터 기준일이 신선도 임계값을 초과했습니다(stale). 판단성 결과는 보수적으로 "
    "해석하고 최신 원문을 확인하십시오."
)

# JSON-RPC 오류 코드
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


class ToolError(Exception):
    """tool 실행 중 사용자 입력/데이터 문제(프로토콜 오류 아님 → result.isError)."""


# --------------------------------------------------------------------------- #
# 날짜 파싱(신선도 판정)
# --------------------------------------------------------------------------- #
def _parse_date(value: Any):
    """ISO8601 / 'YYYY-MM-DD' / 'YYYYMMDD' 를 date 로. 실패 시 None."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        pass
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").date()
        except Exception:
            return None
    return None


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
class Server:
    """stdio JSON-RPC MCP 서버(읽기 전용 도메인 분석)."""

    def __init__(self, conn, cfg):
        self.conn = conn
        self.cfg = cfg
        self.log = util.get_logger("policymap.mcp", getattr(cfg, "log_level", "INFO"))
        # 신선도 임계값(korea100 KOREA100_MCP_MAX_LEGAL_CHECK_AGE_DAYS=30 승계)
        self.max_age_days = _as_int(os.environ.get("POLICYMAP_MCP_MAX_AGE_DAYS"), 30) or 30
        self._analysis = None
        self._analysis_loaded = False
        self._rag = None
        self._rag_loaded = False
        self._neural = None
        self._neural_loaded = False
        self._analytics = None
        self._analytics_loaded = False
        self._neural_models_cache: Optional[list[str]] = None
        # RAG 인덱스 경로 override(테스트/다중 코퍼스). 미지정 시 config.out_dir/index.
        self.index_dir = os.environ.get("POLICYMAP_RAG_INDEX_DIR") or None
        self._tools = self._build_tool_catalog()

    # ---- graph.analysis lazy 위임(미탑재 시 폴백) ---------------------------
    def analysis(self):
        """graph.analysis 를 지연 로드. 미탑재/의존성 부재 시 None(→ SQL 폴백)."""
        if not self._analysis_loaded:
            self._analysis_loaded = True
            try:
                from ..graph import analysis as a  # 병렬 빌드 대상
                self._analysis = a
            except Exception as exc:  # ImportError/의존성 부재 포함
                self.log.info("graph.analysis 미탑재 → SQL 폴백 사용: %s", exc)
                self._analysis = None
        return self._analysis

    # ---- policymap.analytics lazy 위임(미탑재 시 구 엔진 폴백) --------------
    def analytics(self):
        """policymap.analytics 지연 로드(numpy 필요). 부재 시 None(→ 구 엔진 폴백)."""
        if not self._analytics_loaded:
            self._analytics_loaded = True
            try:
                from ..analytics import peers as _pe, spatial as _sp, diffusion as _di
                self._analytics = {"peers": _pe, "spatial": _sp, "diffusion": _di}
            except Exception as exc:
                self.log.info("policymap.analytics 미탑재 → 구 엔진 폴백: %s", exc)
                self._analytics = None
        return self._analytics

    # ---- 신선도 -----------------------------------------------------------
    def _data_as_of(self) -> Optional[str]:
        """DB 전반의 최신 기록시각(as_of/last_success/ts) 중 최대값."""
        candidates: list[str] = []
        for sql in (
            "SELECT MAX(last_success) AS v FROM watermarks",
            "SELECT MAX(as_of_date) AS v FROM ordinances",
            "SELECT MAX(as_of_date) AS v FROM legal_instrument",
            "SELECT MAX(as_of_date) AS v FROM regions",
            "SELECT MAX(ts) AS v FROM change_log",
        ):
            try:
                row = db.fetchone(self.conn, sql)
                if row and row.get("v"):
                    candidates.append(str(row["v"]))
            except Exception:
                continue
        return max(candidates) if candidates else None

    def _freshness(self) -> dict:
        as_of = self._data_as_of()
        age_days = None
        stale = True
        if as_of:
            d = _parse_date(as_of)
            if d is not None:
                age_days = (datetime.now(KST).date() - d).days
                stale = age_days > self.max_age_days
            else:
                stale = True
        return {
            "as_of_date": as_of,
            "age_days": age_days,
            "stale": stale,
            "max_age_days": self.max_age_days,
        }

    def _envelope(self, payload: Any, *, official_url: Optional[str] = None) -> dict:
        """모든 tool 응답 공통 봉투(korea100 안전 규율)."""
        fresh = self._freshness()
        env = {
            "data": payload,
            "as_of_date": fresh["as_of_date"],
            "stale": fresh["stale"],
            "execution_allowed": False,
            "disclaimer": DISCLAIMER,
        }
        if fresh["stale"]:
            env["warning"] = STALE_WARNING
            env["freshness"] = fresh
        if official_url:
            env["official_url"] = official_url
        return env

    # ---- 지역 해석 헬퍼 ---------------------------------------------------
    def _resolve_region(self, *, region_id: Optional[str] = None,
                        sig_cd: Optional[str] = None) -> Optional[dict]:
        """region_id 또는 sig_cd(5자리)로 regions 행 조회.

        입력이 애매하면(둘 다 시도) 가장 상위 계층 1건 반환.
        """
        if region_id:
            row = db.fetchone(self.conn, "SELECT * FROM regions WHERE region_id=?", (region_id,))
            if row:
                return row
        if sig_cd:
            row = db.fetchone(
                self.conn,
                "SELECT * FROM regions WHERE sig_cd=? ORDER BY level LIMIT 1", (sig_cd,)
            )
            if row:
                return row
        # region_id 인자가 실은 sig_cd 였을 수 있음
        if region_id:
            row = db.fetchone(
                self.conn,
                "SELECT * FROM regions WHERE sig_cd=? ORDER BY level LIMIT 1", (region_id,)
            )
            if row:
                return row
        return None

    def _region_budget_summary(self, region_id: str, laf_cd: Optional[str]) -> dict:
        """해당 지역 최신 회계연도 세출 요약(예산현액/국비/시도비/지출)."""
        laf = laf_cd or ""
        row = db.fetchone(
            self.conn,
            "SELECT MAX(fyr) AS mf FROM budget_lines WHERE region_id=? OR laf_cd=?",
            (region_id, laf),
        )
        if not row or not row.get("mf"):
            return {"fyr": None, "lines": 0, "budget_now": 0, "exe_amt": 0,
                    "gov_fund": 0, "sido_fund": 0, "sigungu_fund": 0}
        fyr = row["mf"]
        agg = db.fetchone(
            self.conn,
            """SELECT COUNT(*) AS lines,
                      COALESCE(SUM(budget_now),0)   AS budget_now,
                      COALESCE(SUM(exe_amt),0)      AS exe_amt,
                      COALESCE(SUM(gov_fund),0)     AS gov_fund,
                      COALESCE(SUM(sido_fund),0)    AS sido_fund,
                      COALESCE(SUM(sigungu_fund),0) AS sigungu_fund
               FROM budget_lines
               WHERE (region_id=? OR laf_cd=?) AND fyr=?""",
            (region_id, laf, fyr),
        ) or {}
        agg["fyr"] = fyr
        return agg

    # ======================================================================= #
    # tool 카탈로그(JSON-Schema inputSchema)
    # ======================================================================= #
    def _build_tool_catalog(self) -> list[dict]:
        s_str = {"type": "string"}
        return [
            {
                "name": "search_ordinance",
                "description": (
                    "로컬 조례 그래프 DB에서 자치법규(조례·규칙)를 이름·지역·종류·상태로 "
                    "검색한다. 원문 전량 검색이 아니라 우리 그래프에 적재된 조례의 진입점."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "조례명 부분일치 키워드"},
                        "region_id": {"type": "string", "description": "지역 정규 ID(법정동 앞자리)"},
                        "sig_cd": {"type": "string", "description": "시군구코드 5자리"},
                        "ord_kind": {"type": "string",
                                     "description": "'조례'|'규칙'|'교육규칙' 등"},
                        "status": {"type": "string",
                                   "description": "'active'(기본)|'repealed'|'all'"},
                        "limit": {"type": "integer", "description": "최대 건수(기본 30, 최대 200)"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_ordinance",
                "description": (
                    "조례 1건의 원문(조문)·연혁 메타(제개정 구분/공포일/시행일)·근거 상위법"
                    "(위임 4경로 합집합 + 낫표 인용)을 조회한다."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ordinance_id": {"type": "string", "description": "'ordin:{mst}'"},
                        "mst": {"type": "string", "description": "자치법규일련번호"},
                        "include_articles": {"type": "boolean",
                                             "description": "조문 원문 포함(기본 true)"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "similar_regions",
                "description": (
                    "재정규모·인구·산업/조례 구조가 유사한 지자체(peer)를 반환한다. "
                    "'비슷한 지자체는 이 조례를 뒀는데 우리는 없다' 판단의 기준집합."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sig_cd": {"type": "string", "description": "기준 지자체 시군구코드 5자리"},
                        "region_id": {"type": "string", "description": "기준 지역 정규 ID(대체키)"},
                        "k": {"type": "integer", "description": "반환 peer 수(기본 10)"},
                        "features": {"type": "array", "items": s_str,
                                     "description": "유사도 축(기본 budget,pop,structure)"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "gap_analysis",
                "description": (
                    "특정 지역의 조례 격차를 분석한다. 상위법 위임이 존재하나 해당 지역에 "
                    "조례가 부재한 항목(의무위임 미이행 포함)을 반환. parent_instrument_id "
                    "지정 시 동일 상위법에 대한 지자체별 제정/미제정 커버리지 매트릭스."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "region_id": {"type": "string", "description": "대상 지역 정규 ID"},
                        "sig_cd": {"type": "string", "description": "대상 지역 시군구코드(대체키)"},
                        "parent_instrument_id": {
                            "type": "string",
                            "description": "상위법 instrument_id('statute:{mst}'). 지정 시 커버리지 매트릭스",
                        },
                        "region_level": {"type": "integer",
                                         "description": "커버리지 매트릭스 지역 계층(기본 2=기초)"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "diffusion_timeline",
                "description": (
                    "특정 조례 유형(이름 패턴)의 확산을 제정일 시계열로 추적한다. 어느 지자체가 "
                    "먼저 도입했고 언제 어디로 퍼졌는지(선도/후발 지자체)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "template": {"type": "string",
                                     "description": "조례명 패턴(예 '성별영향평가', '기후위기')"},
                        "since": {"type": "string",
                                  "description": "이 날짜 이후만(YYYY-MM-DD 또는 YYYYMMDD)"},
                    },
                    "required": ["template"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "region_profile",
                "description": (
                    "지역 프로파일: 조례 종류별 보유 수, 최신 회계연도 세출 요약, 인접 지자체, "
                    "행정구역 승계 상태, 최근 변경 이력을 한 번에 반환."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "region_id": {"type": "string", "description": "지역 정규 ID"},
                        "sig_cd": {"type": "string", "description": "시군구코드 5자리(대체키)"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "bill_vote_breakdown",
                "description": (
                    "국회 의안 1건의 본회의 표결을 정당별 찬반으로 분해한다. 표결 당시 정당"
                    "(POLY_NM) 스냅샷 기준의 찬성/반대/기권 카운트와 비율, 대표·공동 발의자."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "bill_id": {"type": "string", "description": "의안 BILL_ID(PRC_…)"},
                        "bill_no": {"type": "string", "description": "의안번호(대체키)"},
                    },
                    "additionalProperties": False,
                },
            },
            # ---- 신경망 / RAG 계층(2단계 확장) --------------------------------
            {
                "name": "semantic_search_ordinance",
                "description": (
                    "조문 본문 의미검색(GraphRAG 하이브리드). BM25 어휘검색 + Dense 벡터검색을 "
                    "RRF 로 융합하고, 위임 상위법·유사조례·인접 지자체 경로로 그래프 확장해 "
                    "전문검색만으로는 도달 불가능한 근거 법령까지 끌어온다. "
                    "search_ordinance 가 조례'명' 부분일치라면 이 tool 은 조문 '내용' 검색이다."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "자연어 질의(조문 내용 기준)"},
                        "k": {"type": "integer", "description": "반환 건수(기본 10, 최대 50)"},
                        "scope": {"type": "string",
                                  "description": "'all'(기본)|'ordinance'|'statute'|'sig:XXXXX'|'region:ID'"},
                        "hops": {"type": "integer", "description": "그래프 확장 홉(0~2, 기본 1)"},
                        "use_graph": {"type": "boolean",
                                      "description": "그래프 확장 사용(기본 true). false 면 순수 하이브리드"},
                        "graph_weight": {"type": "number",
                                         "description": "그래프 랭크 가중(기본 0.5 — 실측 최적)"},
                        "with_text": {"type": "boolean", "description": "조문 원문 포함(기본 true)"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "similar_ordinances",
                "description": (
                    "그래프 신경망 임베딩(GraphSAGE/node2vec) 코사인 기준 유사 조례 Top-k. "
                    "조문 텍스트가 아니라 '그래프 구조(지자체·상위법·분야·예산 연결)'에서 학습된 "
                    "표현이라 제목이 달라도 같은 정책수단인 조례를 회수한다. 파생 추정치(unverified)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ordinance_id": {"type": "string",
                                         "description": "'ordin:{mst}' 또는 자치법규일련번호"},
                        "mst": {"type": "string", "description": "자치법규일련번호(대체키)"},
                        "k": {"type": "integer", "description": "반환 건수(기본 10, 최대 50)"},
                        "model": {"type": "string",
                                  "description": "'graphsage-numpy'(기본)|'node2vec-numpy'|'metapath2vec-numpy'"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "neural_similar_regions",
                "description": (
                    "그래프 임베딩 기준 유사 지자체 Top-k. 재정·인구·조례수 통계 유사도를 쓰는 "
                    "similar_regions 와 명시적으로 구분된다 — 이쪽은 '어떤 조례를 어떤 상위법 "
                    "아래 두고 어떤 이웃과 붙어 있는가'라는 구조 유사도다. 두 결과의 차이 자체가 "
                    "분석 재료이므로 similar_regions 와 함께 호출해 교차 확인하라."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "region_id": {"type": "string", "description": "지역 정규 ID"},
                        "sig_cd": {"type": "string", "description": "시군구코드 5자리(대체키)"},
                        "k": {"type": "integer", "description": "반환 건수(기본 10, 최대 50)"},
                        "model": {"type": "string", "description": "임베딩 모델명(기본 graphsage-numpy)"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "ordinance_effectiveness",
                "description": (
                    "조례 ↔ 연계 세부사업 예산의 집행률(지출액/예산현액, 지출액/편성액)을 집계한다. "
                    "조례가 예산으로 실제 뒷받침되는지, 편성만 되고 집행이 안 되는지를 본다. "
                    "링크는 3채널 자동매칭 결과이므로 verified/unverified 를 반드시 함께 읽어라."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ordinance_id": {"type": "string", "description": "조례 단위 분석"},
                        "mst": {"type": "string", "description": "자치법규일련번호(대체키)"},
                        "region_id": {"type": "string", "description": "지자체 단위 분석"},
                        "sig_cd": {"type": "string", "description": "시군구코드 5자리(대체키)"},
                        "fyr": {"type": "integer", "description": "회계연도 한정(미지정 시 전연도)"},
                        "min_confidence": {"type": "number",
                                           "description": "링크 신뢰도 하한(기본 0.0)"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "explain_path",
                "description": (
                    "두 노드(조례·법령·지자체·예산·의안) 사이의 그래프 경로를 찾아 '왜 연결/유사한가'를 "
                    "관계별 근거와 함께 설명한다. 양방향 BFS. 조례 쌍이면 공통 상위법·공통 분야·"
                    "직접 유사도(통계/신경망)까지 shared_context 로 덧붙인다. "
                    "similar_ordinances 결과의 근거 확인용으로 쓰라."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "from_id": {"type": "string",
                                    "description": "출발 노드('ordin:123'|'region:11110'|'statute:1'|mst 등)"},
                        "to_id": {"type": "string", "description": "도착 노드(동일 형식)"},
                        "max_hops": {"type": "integer", "description": "최대 홉(1~6, 기본 4)"},
                    },
                    "required": ["from_id", "to_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "recommend_ordinances",
                "description": (
                    "'우리와 비슷한 지자체는 이미 뒀는데 우리에겐 없는 조례'를 반환한다. "
                    "이 시스템의 핵심 실무 산출물. peer 집합은 행안부 유사자치단체 기준"
                    "(인구·재정·복지비 등)으로 잡고 동일 광역을 우선 포함한다. 보유 판정은 "
                    "지자체명·법형식 접미를 뗀 정규형(policy_key→canon_key) 비교라 "
                    "'구세 감면' vs '구세 감면에 관한' 같은 표기변이를 중복으로 처리하며, "
                    "애매한 근사일치는 숨기지 않고 likely_variant + closest_own 으로 표시한다. "
                    "similar_regions 로 peer 집합을 먼저 확인한 뒤 호출하면 근거가 명확해진다."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sig_cd": {"type": "string", "description": "우리 지자체 시군구코드 5자리"},
                        "region_id": {"type": "string", "description": "우리 지역 정규 ID(대체키)"},
                        "k": {"type": "integer", "description": "peer 수(기본 15)"},
                        "min_peers": {"type": "integer",
                                      "description": "몇 개 peer 이상 보유해야 추천할지(기본 3)"},
                        "limit": {"type": "integer", "description": "최대 추천 건수(기본 30)"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "spatial_autocorrelation",
                "description": (
                    "지표의 공간 군집을 전역 Moran's I + LISA(국지 군집)로 검정한다. "
                    "조건부 순열검정(999회)과 BH-FDR 다중비교 보정을 적용하므로, "
                    "지도에 색을 칠할 때는 significant=True 인 사분면(HH/LL/HL/LH)만 칠하라. "
                    "metric 에 'adoption_year:<키워드>' 를 주면 특정 조례의 '채택 시점'이 "
                    "공간적으로 군집하는지 검정한다(수평확산 가설의 가장 직접적 검정). "
                    "'adoption_year_resid:<키워드>' 는 시도 고정효과를 뺀 잔차로 같은 검정을 "
                    "수행해 '이웃 학습'과 '광역 공통충격'을 가른다."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "metric": {
                            "type": "string",
                            "description": ("'ordinance_count'|'budget_per_capita'|'population'"
                                            "|'welfare_ratio'|'fiscal_self_ratio'|'pop_density'"
                                            "|'template:<키워드>'|'adoption_year:<키워드>'"
                                            "|'adoption_year_resid:<키워드>'"),
                        },
                        "fyr": {"type": "integer", "description": "예산 회계연도(기본 2025)"},
                        "permutations": {"type": "integer", "description": "순열 횟수(기본 999)"},
                        "lisa": {"type": "boolean", "description": "국지 LISA 포함(기본 true)"},
                    },
                    "required": ["metric"],
                    "additionalProperties": False,
                },
            },
        ]

    # ======================================================================= #
    # tool 구현
    # ======================================================================= #
    def _tool_search_ordinance(self, args: dict) -> dict:
        query = args.get("query")
        limit = _as_int(args.get("limit"), 30) or 30
        limit = max(1, min(limit, 200))
        status = (args.get("status") or "active").strip()

        where = ["1=1"]
        params: list[Any] = []
        if query:
            where.append("o.name LIKE ?")
            params.append(f"%{query}%")
        region_id = args.get("region_id")
        sig_cd = args.get("sig_cd")
        if region_id or sig_cd:
            region = self._resolve_region(region_id=region_id, sig_cd=sig_cd)
            if not region:
                raise ToolError(f"지역을 찾을 수 없음: region_id={region_id!r} sig_cd={sig_cd!r}")
            where.append("o.region_id=?")
            params.append(region["region_id"])
        if args.get("ord_kind"):
            where.append("o.ord_kind=?")
            params.append(args["ord_kind"])
        if status and status.lower() != "all":
            where.append("COALESCE(o.status,'active')=?")
            params.append(status)

        sql = (
            "SELECT o.ordinance_id, o.mst, o.name, o.ord_kind, o.org_name, o.region_id, "
            "       r.full_name AS region_name, r.sig_cd, o.enacted_on, o.effective_on, "
            "       o.rr_cls_cd, o.article_count, o.status, o.verification_status, o.official_url "
            "FROM ordinances o LEFT JOIN regions r ON r.region_id=o.region_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY (o.enacted_on IS NULL), o.enacted_on DESC, o.name ASC "
            f"LIMIT {limit}"
        )
        rows = db.fetchall(self.conn, sql, params)
        payload = {
            "query": query,
            "count": len(rows),
            "limit": limit,
            "results": rows,
        }
        return self._envelope(payload)

    def _tool_get_ordinance(self, args: dict) -> dict:
        ordinance_id = args.get("ordinance_id")
        mst = args.get("mst")
        include_articles = args.get("include_articles", True)

        ordn = None
        if ordinance_id:
            ordn = db.fetchone(
                self.conn, "SELECT * FROM ordinances WHERE ordinance_id=?", (ordinance_id,)
            )
        if ordn is None and mst:
            ordn = db.fetchone(self.conn, "SELECT * FROM ordinances WHERE mst=?", (str(mst),))
        if ordn is None and ordinance_id:
            # ordinance_id 인자가 실은 mst 였을 수 있음
            ordn = db.fetchone(
                self.conn, "SELECT * FROM ordinances WHERE mst=?", (str(ordinance_id),)
            )
        if ordn is None:
            raise ToolError(
                f"조례를 찾을 수 없음: ordinance_id={ordinance_id!r} mst={mst!r}"
            )
        oid = ordn["ordinance_id"]

        region = None
        if ordn.get("region_id"):
            region = db.fetchone(
                self.conn,
                "SELECT region_id, name, full_name, sig_cd FROM regions WHERE region_id=?",
                (ordn["region_id"],),
            )

        articles: list[dict] = []
        if include_articles:
            articles = db.fetchall(
                self.conn,
                "SELECT oa_id, article_no, title, body FROM ordinance_articles "
                "WHERE ordinance_id=? ORDER BY article_no",
                (oid,),
            )

        # 근거 상위법(위임 4경로 합집합) + law.go.kr 직링크
        legal_basis = db.fetchall(
            self.conn,
            """SELECT d.parent_id, li.name AS parent_name, li.kind AS parent_kind,
                      li.national_tier, li.official_url,
                      d.parent_article, d.child_article, d.relation, d.delegation_type,
                      d.source_path, d.trigger_text, d.citation_text
               FROM delegations d
               LEFT JOIN legal_instrument li ON li.instrument_id=d.parent_id
               WHERE d.child_kind='ordinance' AND d.child_id=?
               ORDER BY (d.child_article IS NULL), d.child_article""",
            (oid,),
        )
        # 명시 인용(CITES)
        citations = db.fetchall(
            self.conn,
            """SELECT ir.dst_id, ir.citation_text, ir.citation_type, ir.src_article,
                      li.name AS cited_name, li.official_url
               FROM instrument_relations ir
               LEFT JOIN legal_instrument li ON li.instrument_id=ir.dst_id
               WHERE ir.src_kind='ordinance' AND ir.src_id=? AND ir.relation='CITES'""",
            (oid,),
        )

        payload = {
            "ordinance": ordn,
            "region": region,
            "history": {
                "rr_cls_cd": ordn.get("rr_cls_cd"),
                "enacted_on": ordn.get("enacted_on"),
                "promulgation_no": ordn.get("promulgation_no"),
                "effective_on": ordn.get("effective_on"),
                "repealed_on": ordn.get("repealed_on"),
                "status": ordn.get("status"),
                "succession_status": ordn.get("succession_status"),
            },
            "article_count": len(articles),
            "articles": articles,
            "legal_basis": legal_basis,
            "citations": citations,
            "verification_status": ordn.get("verification_status"),
        }
        return self._envelope(payload, official_url=ordn.get("official_url"))

    def _tool_similar_regions(self, args: dict) -> dict:
        sig_cd = args.get("sig_cd")
        region_id = args.get("region_id")
        k = _as_int(args.get("k"), 10) or 10
        features = args.get("features") or ["budget", "pop", "structure"]

        region = self._resolve_region(region_id=region_id, sig_cd=sig_cd)
        if region is None:
            raise ToolError(f"기준 지역 없음: sig_cd={sig_cd!r} region_id={region_id!r}")
        eff_sig = region.get("sig_cd") or sig_cd

        # 1순위: analytics.peers(행안부 유사자치단체 기준 정렬, 검증 완료).
        # 구 graph.analysis.find_peer_governments 는 '정책구조' 축이
        # ordinance_category 커버리지 0.68%(1,087/159,452) 위에서 계산돼
        # 후보 226곳의 구조유사도 평균 0.9402 로 변별력이 사실상 0이었고,
        # 일반구(level=3)에 similarity 0.0 인 peer 를 반환했다. 신 엔진은
        # 유형 사전분할·재정지표 가중·level=3 가드를 갖는다(문서 16 참조).
        an = self.analytics()
        if an is not None and eff_sig:
            try:
                res = an["peers"].find_similar_governments(self.conn, eff_sig, k=k)
                payload = {
                    "base_region": {"region_id": region["region_id"],
                                    "sig_cd": eff_sig, "name": region.get("full_name")},
                    "k": k, "peers": res.get("peers") or [],
                    "method": res.get("method"),
                    "_engine": "analytics.peers.find_similar_governments",
                }
                if not res.get("peers") and res.get("reason"):
                    # level=3(일반구) 등 비교 불가 사유는 숨기지 않고 그대로 전달한다.
                    payload["reason"] = res["reason"]
                    payload["parent_region"] = res.get("parent_region")
                return self._envelope(payload)
            except Exception as exc:
                self.log.warning("analytics.peers 실패 → 구 엔진 폴백: %s", exc)

        analysis = self.analysis()
        if analysis is not None and hasattr(analysis, "find_peer_governments") and eff_sig:
            try:
                peers = analysis.find_peer_governments(
                    self.conn, eff_sig, k=k, features=tuple(features)
                )
                payload = {
                    "base_region": {"region_id": region["region_id"],
                                    "sig_cd": eff_sig, "name": region.get("full_name")},
                    "k": k, "features": features, "peers": peers,
                    "_engine": "graph.analysis.find_peer_governments",
                    "caveat": "구 엔진(정책구조 축 변별력 낮음). analytics 미탑재 폴백.",
                }
                return self._envelope(payload)
            except Exception as exc:
                self.log.warning("find_peer_governments 실패 → 폴백: %s", exc)

        payload = self._peer_fallback(region, k, features)
        return self._envelope(payload)

    def _peer_fallback(self, region: dict, k: int, features: list) -> dict:
        """순수 SQL/파이썬 peer 근접(정규화 유클리드). analysis 미탑재 폴백."""
        level = region["level"]
        rows = db.fetchall(
            self.conn,
            "SELECT region_id, sig_cd, name, full_name, population, lofin_laf_cd "
            "FROM regions WHERE level=? AND COALESCE(status,'active')='active'",
            (level,),
        )
        feats: dict[str, dict] = {}
        for r in rows:
            rid = r["region_id"]
            budget = self._region_budget_summary(rid, r.get("lofin_laf_cd"))
            ord_count = db.count(
                self.conn, "ordinances",
                "region_id=? AND COALESCE(status,'active')='active'", (rid,)
            )
            feats[rid] = {
                "row": r,
                "pop": float(r.get("population") or 0),
                "budget": float(budget.get("exe_amt") or budget.get("budget_now") or 0),
                "structure": float(ord_count),
            }
        target = feats.get(region["region_id"])
        if target is None:
            return {"base_region": {"region_id": region["region_id"]},
                    "peers": [], "note": "기준 지역 특성 없음", "_engine": "fallback-sql"}

        axes = [a for a in ("pop", "budget", "structure") if a in features] or \
               ["pop", "budget", "structure"]
        # 축별 최대값(정규화)
        maxv = {a: max((f[a] for f in feats.values()), default=0.0) or 1.0 for a in axes}

        scored = []
        for rid, f in feats.items():
            if rid == region["region_id"]:
                continue
            dist = 0.0
            for a in axes:
                dv = (f[a] - target[a]) / maxv[a]
                dist += dv * dv
            dist = dist ** 0.5
            similarity = 1.0 / (1.0 + dist)
            r = f["row"]
            scored.append({
                "region_id": rid,
                "sig_cd": r.get("sig_cd"),
                "name": r.get("full_name") or r.get("name"),
                "population": int(f["pop"]) if f["pop"] else None,
                "budget_exe_amt": int(f["budget"]) if f["budget"] else None,
                "ordinance_count": int(f["structure"]),
                "distance": round(dist, 4),
                "similarity": round(similarity, 4),
            })
        scored.sort(key=lambda x: x["distance"])
        return {
            "base_region": {"region_id": region["region_id"],
                            "sig_cd": region.get("sig_cd"),
                            "name": region.get("full_name"),
                            "population": target["pop"], "budget": target["budget"],
                            "ordinance_count": target["structure"]},
            "k": k, "features": axes, "peers": scored[:k],
            "_engine": "fallback-sql",
        }

    def _tool_gap_analysis(self, args: dict) -> dict:
        parent_instrument_id = args.get("parent_instrument_id")
        analysis = self.analysis()

        # 커버리지 매트릭스 모드
        if parent_instrument_id:
            region_level = _as_int(args.get("region_level"), 2) or 2
            if analysis is not None and hasattr(analysis, "compare_ordinance_coverage"):
                try:
                    result = analysis.compare_ordinance_coverage(
                        self.conn, parent_instrument_id, region_level=region_level
                    )
                    result = dict(result) if isinstance(result, dict) else {"coverage": result}
                    result["_engine"] = "graph.analysis.compare_ordinance_coverage"
                    parent = db.fetchone(
                        self.conn, "SELECT official_url FROM legal_instrument WHERE instrument_id=?",
                        (parent_instrument_id,)
                    ) or {}
                    return self._envelope(result, official_url=parent.get("official_url"))
                except Exception as exc:
                    self.log.warning("compare_ordinance_coverage 실패 → 폴백: %s", exc)
            payload = self._coverage_fallback(parent_instrument_id, region_level)
            return self._envelope(payload)

        # 지역 격차 모드
        region = self._resolve_region(
            region_id=args.get("region_id"), sig_cd=args.get("sig_cd")
        )
        if region is None:
            raise ToolError("region_id/sig_cd 또는 parent_instrument_id 중 하나가 필요")
        if analysis is not None and hasattr(analysis, "get_delegation_gap"):
            try:
                gaps = analysis.get_delegation_gap(self.conn, region["region_id"])
                payload = {"region_id": region["region_id"],
                           "region_name": region.get("full_name"),
                           "gaps": gaps, "gap_count": len(gaps) if hasattr(gaps, "__len__") else None,
                           "_engine": "graph.analysis.get_delegation_gap"}
                return self._envelope(payload)
            except Exception as exc:
                self.log.warning("get_delegation_gap 실패 → 폴백: %s", exc)
        payload = self._delegation_gap_fallback(region)
        return self._envelope(payload)

    def _delegation_gap_fallback(self, region: dict) -> dict:
        """상위법 위임이 있으나 이 지역에 조례 부재인 항목. analysis 미탑재 폴백."""
        rid = region["region_id"]
        # 위임 원천이 되는 상위법 + 전국 커버 지역 수 + 의무위임 여부
        parents = db.fetchall(
            self.conn,
            """SELECT d.parent_id,
                      li.name AS parent_name, li.official_url, li.national_tier,
                      COUNT(DISTINCT o.region_id) AS region_cover,
                      MAX(CASE WHEN d.delegation_type='mandatory' THEN 1 ELSE 0 END) AS mandatory
               FROM delegations d
               JOIN ordinances o
                 ON o.ordinance_id=d.child_id AND d.child_kind='ordinance'
               LEFT JOIN legal_instrument li ON li.instrument_id=d.parent_id
               GROUP BY d.parent_id""",
        )
        gaps = []
        for p in parents:
            has = db.fetchone(
                self.conn,
                """SELECT 1 FROM delegations d
                   JOIN ordinances o ON o.ordinance_id=d.child_id
                   WHERE d.parent_id=? AND d.child_kind='ordinance' AND o.region_id=?
                   LIMIT 1""",
                (p["parent_id"], rid),
            )
            if not has:
                gaps.append({
                    "parent_instrument_id": p["parent_id"],
                    "parent_name": p.get("parent_name"),
                    "official_url": p.get("official_url"),
                    "national_tier": p.get("national_tier"),
                    "peer_region_cover": p.get("region_cover"),
                    "mandatory": bool(p.get("mandatory")),
                })
        # 의무위임 미이행 우선, 그다음 전국 채택 많은 순
        gaps.sort(key=lambda g: (not g["mandatory"], -(g.get("peer_region_cover") or 0)))
        return {
            "region_id": rid,
            "region_name": region.get("full_name"),
            "gap_count": len(gaps),
            "mandatory_unmet": sum(1 for g in gaps if g["mandatory"]),
            "gaps": gaps,
            "_engine": "fallback-sql",
        }

    def _coverage_fallback(self, parent_instrument_id: str, region_level: int) -> dict:
        """특정 상위법의 지자체별 제정/미제정 매트릭스. analysis 미탑재 폴백."""
        parent = db.fetchone(
            self.conn, "SELECT instrument_id, name, official_url FROM legal_instrument "
            "WHERE instrument_id=?", (parent_instrument_id,)
        )
        adopters = db.fetchall(
            self.conn,
            """SELECT DISTINCT o.region_id, r.full_name AS region_name, r.sig_cd,
                      o.ordinance_id, o.name AS ordinance_name, o.enacted_on
               FROM delegations d
               JOIN ordinances o ON o.ordinance_id=d.child_id AND d.child_kind='ordinance'
               LEFT JOIN regions r ON r.region_id=o.region_id
               WHERE d.parent_id=?""",
            (parent_instrument_id,),
        )
        adopter_ids = {a["region_id"] for a in adopters if a.get("region_id")}
        universe = db.fetchall(
            self.conn,
            "SELECT region_id, full_name, sig_cd FROM regions "
            "WHERE level=? AND COALESCE(status,'active')='active' AND has_legislation=1",
            (region_level,),
        )
        missing = [
            {"region_id": r["region_id"], "region_name": r.get("full_name"), "sig_cd": r.get("sig_cd")}
            for r in universe if r["region_id"] not in adopter_ids
        ]
        total = len(universe)
        return {
            "parent_instrument_id": parent_instrument_id,
            "parent_name": parent.get("name") if parent else None,
            "official_url": parent.get("official_url") if parent else None,
            "region_level": region_level,
            "universe_count": total,
            "adopted_count": len(adopter_ids),
            "missing_count": len(missing),
            "coverage_ratio": round(len(adopter_ids) / total, 4) if total else None,
            "adopters": adopters,
            "missing": missing,
            "_engine": "fallback-sql",
        }

    def _tool_diffusion_timeline(self, args: dict) -> dict:
        template = args.get("template")
        if not template:
            raise ToolError("template(조례명 패턴)이 필요")
        since = args.get("since")

        # 1순위: analytics.diffusion(제정 코호트 + 로지스틱 성장모형 + 경로분해).
        # 구 trace_ordinance_diffusion 은 enacted_on 이 '현행 판본의 공포일'이라
        # 개정본이 그 해 신규 채택으로 잡히는 좌측절단·생존자편향이 있었다
        # (실측: '출산장려' 119건 중 rr_cls_cd='제정' 은 7건, 무필터 최초연도 2013년).
        an = self.analytics()
        if an is not None and str(args.get("engine") or "analytics") != "legacy":
            try:
                mode = str(args.get("mode") or "enactment")
                prof = an["diffusion"].diffusion_profile(
                    self.conn, template, mode=mode,
                    y0=_as_int(args.get("since"), None))
                prof = dict(prof)
                prof["_engine"] = "analytics.diffusion.diffusion_profile"
                prof["interpretation_caveat"] = (
                    "확산 곡선(로지스틱 적합)은 방어 가능하나, '이웃을 보고 따라 했다'는 "
                    "수평확산 해석은 우리 데이터에서 지지되지 않는다 — 이산시간 EHA 20개 "
                    "사양에서 이웃노출 계수가 BH-FDR 통과 0개(중앙 OR 0.969)이고, 채택연도 "
                    "공간군집은 시도 고정효과 제거 시 소멸한다(평균 I 0.053→-0.108). "
                    "지배 요인은 광역·전국 공통충격(연도추세 중앙 OR 2.47)이다.")
                return self._envelope(prof)
            except Exception as exc:
                self.log.warning("analytics.diffusion 실패 → 구 엔진 폴백: %s", exc)

        analysis = self.analysis()
        if analysis is not None and hasattr(analysis, "trace_ordinance_diffusion"):
            try:
                result = analysis.trace_ordinance_diffusion(self.conn, template, since=since)
                result = dict(result) if isinstance(result, dict) else {"timeline": result}
                result["_engine"] = "graph.analysis.trace_ordinance_diffusion"
                return self._envelope(result)
            except Exception as exc:
                self.log.warning("trace_ordinance_diffusion 실패 → 폴백: %s", exc)

        payload = self._diffusion_fallback(template, since)
        return self._envelope(payload)

    def _diffusion_fallback(self, template: str, since: Optional[str]) -> dict:
        """제정일 시계열 확산. analysis 미탑재 폴백(인접·유사 경로 없이 순수 시계열)."""
        rows = db.fetchall(
            self.conn,
            """SELECT o.ordinance_id, o.name, o.region_id, r.full_name AS region_name,
                      r.sig_cd, o.enacted_on, o.rr_cls_cd, o.status
               FROM ordinances o LEFT JOIN regions r ON r.region_id=o.region_id
               WHERE o.name LIKE ? AND o.enacted_on IS NOT NULL AND o.enacted_on<>''
               ORDER BY o.enacted_on ASC""",
            (f"%{template}%",),
        )
        since_digits = "".join(ch for ch in str(since) if ch.isdigit())[:8] if since else None
        timeline = []
        for r in rows:
            ev = "".join(ch for ch in str(r.get("enacted_on") or "") if ch.isdigit())[:8]
            if since_digits and ev and ev < since_digits:
                continue
            timeline.append({
                "ordinance_id": r["ordinance_id"],
                "name": r.get("name"),
                "region_id": r.get("region_id"),
                "region_name": r.get("region_name"),
                "sig_cd": r.get("sig_cd"),
                "enacted_on": r.get("enacted_on"),
                "rr_cls_cd": r.get("rr_cls_cd"),
                "status": r.get("status"),
            })
        return {
            "template": template,
            "since": since,
            "adopter_count": len(timeline),
            "first_adopter": timeline[0] if timeline else None,
            "latest_adopter": timeline[-1] if timeline else None,
            "timeline": timeline,
            "note": "폴백: 제정일 순 시계열. 인접·유사 확산 경로는 graph.analysis 필요.",
            "_engine": "fallback-sql",
        }

    def _tool_region_profile(self, args: dict) -> dict:
        region = self._resolve_region(
            region_id=args.get("region_id"), sig_cd=args.get("sig_cd")
        )
        if region is None:
            raise ToolError(
                f"지역 없음: region_id={args.get('region_id')!r} sig_cd={args.get('sig_cd')!r}"
            )
        rid = region["region_id"]
        sig = region.get("sig_cd")

        by_kind = db.fetchall(
            self.conn,
            "SELECT ord_kind, COUNT(*) AS n FROM ordinances "
            "WHERE region_id=? AND COALESCE(status,'active')='active' GROUP BY ord_kind",
            (rid,),
        )
        total_ord = sum(int(x["n"]) for x in by_kind)
        budget = self._region_budget_summary(rid, region.get("lofin_laf_cd"))

        neighbors = db.fetchall(
            self.conn,
            """SELECT a.neighbor_id, r.full_name AS name, r.sig_cd, a.same_province
               FROM region_adjacency a LEFT JOIN regions r ON r.region_id=a.neighbor_id
               WHERE a.region_id=? ORDER BY a.same_province DESC, r.full_name""",
            (rid,),
        )
        succession = db.fetchall(
            self.conn,
            """SELECT old_region_id, new_region_id, succession_type, effective_date, status_note
               FROM region_succession WHERE old_region_id=? OR new_region_id=?""",
            (rid, rid),
        )
        recent_changes = []
        if sig:
            recent_changes = db.fetchall(
                self.conn,
                "SELECT change_id, ts, entity_type, entity_id, entity_name, event, official_url "
                "FROM change_log WHERE region_code=? ORDER BY ts DESC LIMIT 20",
                (sig,),
            )

        payload = {
            "region": {
                "region_id": rid, "sig_cd": sig,
                "name": region.get("name"), "full_name": region.get("full_name"),
                "level": region.get("level"), "population": region.get("population"),
                "has_legislation": region.get("has_legislation"),
                "status": region.get("status"),
                "lofin_laf_cd": region.get("lofin_laf_cd"),
            },
            "ordinance_count": total_ord,
            "ordinance_by_kind": by_kind,
            "budget_latest": budget,
            "adjacency_count": len(neighbors),
            "neighbors": neighbors,
            "succession": succession,
            "recent_changes": recent_changes,
        }
        return self._envelope(payload)

    def _tool_bill_vote_breakdown(self, args: dict) -> dict:
        bill_id = args.get("bill_id")
        bill_no = args.get("bill_no")
        bill = None
        if bill_id:
            bill = db.fetchone(self.conn, "SELECT * FROM bills WHERE bill_id=?", (bill_id,))
        if bill is None and bill_no:
            bill = db.fetchone(
                self.conn, "SELECT * FROM bills WHERE bill_no=? ORDER BY age DESC LIMIT 1",
                (str(bill_no),)
            )
        if bill is None and bill_id:
            bill = db.fetchone(
                self.conn, "SELECT * FROM bills WHERE bill_no=? ORDER BY age DESC LIMIT 1",
                (str(bill_id),)
            )
        if bill is None:
            raise ToolError(f"의안 없음: bill_id={bill_id!r} bill_no={bill_no!r}")
        bid = bill["bill_id"]

        # 정당별 표결(표결 당시 정당 POLY_NM 기준)
        rows = db.fetchall(
            self.conn,
            "SELECT COALESCE(party_at_vote,'무소속/미상') AS party, "
            "       COALESCE(result_vote_mod,'미상') AS vote, COUNT(*) AS n "
            "FROM votes WHERE bill_id=? GROUP BY party, vote",
            (bid,),
        )
        parties: dict[str, dict] = {}
        totals = {"찬성": 0, "반대": 0, "기권": 0, "기타": 0, "합계": 0}
        for r in rows:
            party = r["party"]
            vote = str(r["vote"])
            n = int(r["n"])
            slot = parties.setdefault(
                party, {"party": party, "찬성": 0, "반대": 0, "기권": 0, "기타": 0, "합계": 0}
            )
            if "찬성" in vote:
                key = "찬성"
            elif "반대" in vote:
                key = "반대"
            elif "기권" in vote:
                key = "기권"
            else:
                key = "기타"
            slot[key] += n
            slot["합계"] += n
            totals[key] += n
            totals["합계"] += n

        party_breakdown = sorted(parties.values(), key=lambda x: -x["합계"])
        for p in party_breakdown:
            tot = p["합계"] or 1
            p["찬성률"] = round(p["찬성"] / tot, 4)

        proposers = db.fetchall(
            self.conn,
            """SELECT bp.role, l.legislator_id, l.name, l.current_party, l.district
               FROM bill_proposers bp
               LEFT JOIN legislators l ON l.legislator_id=bp.legislator_id
               WHERE bp.bill_id=? ORDER BY (bp.role<>'RST'), l.name""",
            (bid,),
        )

        overall_total = totals["합계"]
        payload = {
            "bill": {
                "bill_id": bid, "bill_no": bill.get("bill_no"), "age": bill.get("age"),
                "name": bill.get("name"), "committee": bill.get("committee"),
                "propose_dt": bill.get("propose_dt"), "proc_dt": bill.get("proc_dt"),
                "proc_result": bill.get("proc_result"), "proc_result_cd": bill.get("proc_result_cd"),
            },
            "tally_reported": {  # 의안별 표결현황(ncocpgfiaoituanbr) 집계값
                "member_tcnt": bill.get("member_tcnt"), "vote_tcnt": bill.get("vote_tcnt"),
                "yes_tcnt": bill.get("yes_tcnt"), "no_tcnt": bill.get("no_tcnt"),
                "blank_tcnt": bill.get("blank_tcnt"),
            },
            "tally_from_votes": totals,  # 의원별 표결(votes) 재집계
            "yes_ratio": round(totals["찬성"] / overall_total, 4) if overall_total else None,
            "party_breakdown": party_breakdown,
            "proposers": proposers,
        }
        return self._envelope(payload, official_url=bill.get("link_url") or bill.get("detail_link"))

    # ======================================================================= #
    # 신경망 / RAG 계층 tool (8~12번)
    # ======================================================================= #
    # ---- 지연 로더(무거운 계층은 선택적, 부재 시 강등) ---------------------
    def rag(self):
        """policymap.rag 지연 로드. 미탑재/인덱스 부재 시 None(→ SQL LIKE 폴백)."""
        if not self._rag_loaded:
            self._rag_loaded = True
            try:
                from .. import rag as _r
                self._rag = _r
            except Exception as exc:  # ImportError/의존성 부재 포함
                self.log.info("policymap.rag 미탑재 → SQL 폴백 사용: %s", exc)
                self._rag = None
        return self._rag

    def neural(self):
        """policymap.neural 지연 로드(numpy 필요). 부재 시 None(→ 테이블 직접조회)."""
        if not self._neural_loaded:
            self._neural_loaded = True
            try:
                from .. import neural as _n
                self._neural = _n
            except Exception as exc:
                self.log.info("policymap.neural 미탑재 → neural_similarity 직접조회: %s", exc)
                self._neural = None
        return self._neural

    def _neural_models(self) -> list[str]:
        """node_embeddings 에 적재된 모델명(많이 적재된 순). 미적재면 빈 목록."""
        if self._neural_models_cache is None:
            try:
                rows = db.fetchall(
                    self.conn,
                    "SELECT model_name, COUNT(*) AS n FROM node_embeddings "
                    "GROUP BY model_name ORDER BY n DESC",
                )
                self._neural_models_cache = [r["model_name"] for r in rows]
            except Exception:  # 테이블 미생성(신경망 미학습)
                self._neural_models_cache = []
        return self._neural_models_cache

    def _pick_model(self, requested: Optional[str]) -> Optional[str]:
        models = self._neural_models()
        if requested:
            return requested if requested in models else None
        for pref in ("graphsage-numpy", "node2vec-numpy", "metapath2vec-numpy"):
            if pref in models:
                return pref
        return models[0] if models else None

    # ---- 노드 식별자 해석(graph.build.node_id 네임스페이스) -----------------
    _NODE_KINDS = ("region", "instrument", "ordinance", "bill", "legislator",
                   "party", "category", "budget", "article")

    def _node_meta(self, kind: str, key: str) -> Optional[dict]:
        """kind + 자연키 → 노드 메타. 대체키(sig_cd/mst/bill_no)도 관용 허용."""
        if kind == "region":
            r = (db.fetchone(self.conn, "SELECT * FROM regions WHERE region_id=?", (key,))
                 or db.fetchone(self.conn,
                                "SELECT * FROM regions WHERE sig_cd=? ORDER BY level LIMIT 1",
                                (key,)))
            if r:
                return {"node_id": f"region:{r['region_id']}", "kind": "region",
                        "key": r["region_id"], "name": r.get("full_name") or r.get("name"),
                        "region_id": r["region_id"], "official_url": None,
                        "verification_status": r.get("status")}
        elif kind == "ordinance":
            r = (db.fetchone(self.conn, "SELECT * FROM ordinances WHERE ordinance_id=?", (key,))
                 or db.fetchone(self.conn, "SELECT * FROM ordinances WHERE mst=?", (str(key),)))
            if r:
                return {"node_id": f"ordinance:{r['ordinance_id']}", "kind": "ordinance",
                        "key": r["ordinance_id"], "name": r.get("name"),
                        "region_id": r.get("region_id"), "official_url": r.get("official_url"),
                        "verification_status": r.get("verification_status")}
        elif kind == "instrument":
            r = (db.fetchone(self.conn,
                             "SELECT * FROM legal_instrument WHERE instrument_id=?", (key,))
                 or db.fetchone(self.conn,
                                "SELECT * FROM legal_instrument WHERE mst=? LIMIT 1", (str(key),)))
            if r:
                return {"node_id": f"instrument:{r['instrument_id']}", "kind": "instrument",
                        "key": r["instrument_id"], "name": r.get("name"),
                        "region_id": None, "official_url": r.get("official_url"),
                        "verification_status": r.get("verification_status")}
        elif kind == "bill":
            r = (db.fetchone(self.conn, "SELECT * FROM bills WHERE bill_id=?", (key,))
                 or db.fetchone(self.conn, "SELECT * FROM bills WHERE bill_no=?", (str(key),)))
            if r:
                return {"node_id": f"bill:{r['bill_id']}", "kind": "bill", "key": r["bill_id"],
                        "name": r.get("name"), "region_id": None,
                        "official_url": r.get("link_url") or r.get("detail_link"),
                        "verification_status": None}
        elif kind == "budget":
            r = db.fetchone(self.conn, "SELECT * FROM budget_lines WHERE budget_id=?", (key,))
            if r:
                return {"node_id": f"budget:{r['budget_id']}", "kind": "budget",
                        "key": r["budget_id"], "name": r.get("dbiz_nm"),
                        "region_id": r.get("region_id"), "official_url": None,
                        "verification_status": None}
        elif kind == "legislator":
            r = db.fetchone(self.conn, "SELECT * FROM legislators WHERE legislator_id=?", (key,))
            if r:
                return {"node_id": f"legislator:{r['legislator_id']}", "kind": "legislator",
                        "key": r["legislator_id"], "name": r.get("name"), "region_id": None,
                        "official_url": None, "verification_status": None}
        elif kind == "party":
            r = db.fetchone(self.conn, "SELECT * FROM parties WHERE party_id=?", (key,))
            if r:
                return {"node_id": f"party:{r['party_id']}", "kind": "party",
                        "key": r["party_id"], "name": r.get("name"), "region_id": None,
                        "official_url": None, "verification_status": None}
        elif kind == "category":
            r = db.fetchone(self.conn, "SELECT * FROM categories WHERE code=?", (key,))
            if r:
                return {"node_id": f"category:{r['code']}", "kind": "category",
                        "key": r["code"], "name": r.get("name"), "region_id": None,
                        "official_url": None, "verification_status": None}
        return None

    def _resolve_node(self, ident: Any) -> Optional[dict]:
        """느슨한 식별자 → 노드 메타(graph.build.node_id 규약 'kind:key').

        허용: 'ordinance:ordin:123' | 'ordin:123' | mst 숫자 | 'region:11110' | '11110'
              | sig_cd | 'instrument:statute:1' | 'statute:1' | 'bill:PRC_…' | 'PRC_…'
              | 'budget:{budget_id}' | 'category:C-PET'
        """
        if ident is None:
            return None
        s = str(ident).strip()
        if not s:
            return None
        # 1) 완전 네임스페이스 형태
        for kind in self._NODE_KINDS:
            if s.startswith(kind + ":"):
                meta = self._node_meta(kind, s[len(kind) + 1:])
                if meta:
                    return meta
        # 2) 자연키 접두로 kind 추정
        if s.startswith("ordin:"):
            meta = self._node_meta("ordinance", s)
            if meta:
                return meta
        if s.split(":", 1)[0] in ("statute", "admrul", "treaty", "ordinrule", "constitution"):
            meta = self._node_meta("instrument", s)
            if meta:
                return meta
        if s.startswith("PRC_") or s.startswith("ARC_"):
            meta = self._node_meta("bill", s)
            if meta:
                return meta
        # 3) 접두 없는 자연키 — 비용 낮은 순으로 탐색
        for kind in ("region", "ordinance", "instrument", "category", "bill",
                     "budget", "legislator", "party"):
            meta = self._node_meta(kind, s)
            if meta:
                return meta
        return None

    # ---- tool 8: semantic_search_ordinance --------------------------------
    def _tool_semantic_search_ordinance(self, args: dict) -> dict:
        query = (args.get("query") or "").strip()
        if not query:
            raise ToolError("query(자연어 질의)가 필요")
        k = _as_int(args.get("k"), 10) or 10
        k = max(1, min(k, 50))
        scope = (args.get("scope") or "all").strip() or "all"
        hops = _as_int(args.get("hops"), 1) or 1
        hops = max(0, min(hops, 2))
        use_graph = args.get("use_graph", True)
        with_text = bool(args.get("with_text", True))

        rag = self.rag()
        if rag is not None:
            try:
                if use_graph and hops > 0:
                    hits = rag.hybrid_graph_search(
                        self.conn, query, k=k, scope=scope, hops=hops,
                        graph_weight=float(args.get("graph_weight") or 0.5),
                        index_dir=self.index_dir, with_text=with_text)
                    engine = "rag.hybrid_graph_search(BM25+dense+graph)"
                else:
                    hits = rag.hybrid_search(self.conn, query, k=k, scope=scope,
                                             index_dir=self.index_dir,
                                             group_by="parent", with_text=with_text)
                    engine = "rag.hybrid_search(BM25+dense RRF)"
                results = [self._decorate_hit(h) for h in hits]
                payload = {
                    "query": query, "k": k, "scope": scope, "hops": hops,
                    "count": len(results), "results": results,
                    "verification_summary": self._verification_summary(results),
                    "_engine": engine,
                }
                return self._envelope(payload)
            except Exception as exc:
                self.log.warning("rag 하이브리드 검색 실패 → 폴백: %s", exc)

        # 폴백: 조례명 LIKE + 조문 LIKE(전문검색 없이 도달 가능한 최소 기능)
        payload = self._semantic_fallback(query, k)
        return self._envelope(payload)

    def _decorate_hit(self, hit: dict) -> dict:
        """검색 히트에 verification_status/공식 URL 을 보강(검증상태 명시 규율)."""
        out = dict(hit)
        node_kind = hit.get("node_type") or (
            "ordinance" if hit.get("doc_kind") == "ordinance_article" else "instrument")
        pid = hit.get("id") or hit.get("parent_id")
        out["node_type"] = node_kind
        out["id"] = pid
        if pid and not out.get("verification_status"):
            table, col = (("ordinances", "ordinance_id") if node_kind == "ordinance"
                          else ("legal_instrument", "instrument_id"))
            row = db.fetchone(
                self.conn,
                f"SELECT verification_status, official_url, status FROM {table} WHERE {col}=?",
                (pid,),
            )
            if row:
                out["verification_status"] = row.get("verification_status")
                out.setdefault("official_url", row.get("official_url"))
                out["official_url"] = out.get("official_url") or row.get("official_url")
                out["status"] = row.get("status")
        out["verified"] = out.get("verification_status") in ("verified", "source-linked")
        return out

    @staticmethod
    def _verification_summary(items: list[dict]) -> dict:
        """검증상태 집계(verified/unverified 구분을 응답에 명시)."""
        summary: dict[str, int] = {}
        for it in items:
            key = it.get("verification_status") or "unknown"
            summary[key] = summary.get(key, 0) + 1
        verified = sum(v for k, v in summary.items() if k in ("verified", "source-linked"))
        return {"by_status": summary, "verified": verified,
                "unverified": len(items) - verified}

    def _semantic_fallback(self, query: str, k: int) -> dict:
        """rag 미탑재/인덱스 부재 폴백: 조례명·조문 LIKE 매칭(토큰 AND 스코어)."""
        tokens = [t for t in query.replace("　", " ").split() if len(t) >= 2][:5] or [query]
        rows: dict[str, dict] = {}
        for tok in tokens:
            for r in db.fetchall(
                self.conn,
                "SELECT o.ordinance_id AS id, o.name, o.region_id, o.org_name, "
                "       o.official_url, o.verification_status, o.status "
                "FROM ordinances o WHERE o.name LIKE ? "
                "  AND COALESCE(o.status,'active')='active' LIMIT ?",
                (f"%{tok}%", k * 5),
            ):
                cur = rows.setdefault(r["id"], {**r, "node_type": "ordinance", "hits": 0})
                cur["hits"] += 1
        out = sorted(rows.values(), key=lambda r: (-r["hits"], str(r["name"])))[:k]
        for i, r in enumerate(out, start=1):
            r["rank"] = i
            r["score"] = round(r["hits"] / len(tokens), 4)
            r["method"] = "like-token-and"
        out = [self._decorate_hit(r) for r in out]
        return {"query": query, "k": k, "count": len(out), "results": out,
                "verification_summary": self._verification_summary(out),
                "tokens": tokens,
                "_engine": "fallback-sql-like",
                "note": "RAG 인덱스 미탑재 — 조례명 부분일치로 강등(조문 의미검색 아님)"}

    # ---- tool 9: similar_ordinances ---------------------------------------
    def _tool_similar_ordinances(self, args: dict) -> dict:
        ident = args.get("ordinance_id") or args.get("mst")
        if not ident:
            raise ToolError("ordinance_id(또는 mst)가 필요")
        node = self._resolve_node(ident)
        if node is None or node["kind"] != "ordinance":
            raise ToolError(f"조례를 찾을 수 없음: {ident!r}")
        k = max(1, min(_as_int(args.get("k"), 10) or 10, 50))
        model = self._pick_model(args.get("model"))

        rows = self._neural_topk(node["node_id"], k, model)
        engine = f"neural_similarity[{model}]" if rows else None
        if not rows:
            # 폴백 1: 통계 임베딩(char-ngram TF) similarity_edges
            rows = self._statistical_topk(node["key"], k)
            engine = "similarity_edges(char-ngram-tf)" if rows else "none"

        results = []
        for r in rows:
            oid = r["dst_key"]
            meta = db.fetchone(
                self.conn,
                "SELECT o.ordinance_id, o.name, o.region_id, o.org_name, o.enacted_on, "
                "       o.official_url, o.verification_status, o.status, r.full_name AS region_name "
                "FROM ordinances o LEFT JOIN regions r ON r.region_id=o.region_id "
                "WHERE o.ordinance_id=?",
                (oid,),
            ) or {"ordinance_id": oid}
            meta["cosine_sim"] = r["cosine_sim"]
            meta["rank"] = r["rank"]
            meta["model_name"] = r.get("model_name")
            meta["verified"] = meta.get("verification_status") in ("verified", "source-linked")
            results.append(meta)

        payload = {
            "ordinance": {"ordinance_id": node["key"], "name": node.get("name"),
                          "region_id": node.get("region_id"),
                          "verification_status": node.get("verification_status")},
            "k": k, "model": model, "count": len(results), "similar": results,
            "verification_summary": self._verification_summary(results),
            "interpretation": (
                "코사인 유사도는 그래프 구조·조문 텍스트에서 학습된 임베딩 기반 추정치이며 "
                "법적 동등성을 뜻하지 않는다. 원문 대조 필요."),
            "_engine": engine,
        }
        return self._envelope(payload, official_url=None)

    def _neural_topk(self, node_id: str, k: int, model: Optional[str]) -> list[dict]:
        """neural_similarity 저장 Top-k(없으면 빈 목록). dst_key = 접두 제거 자연키."""
        if not model:
            return []
        try:
            rows = db.fetchall(
                self.conn,
                "SELECT dst_id, cosine_sim, rank, model_name FROM neural_similarity "
                "WHERE src_id=? AND model_name=? ORDER BY rank LIMIT ?",
                (node_id, model, k),
            )
        except Exception:
            return []
        out = []
        for r in rows:
            dst = str(r["dst_id"])
            key = dst.split(":", 1)[1] if ":" in dst else dst
            out.append({"dst_id": dst, "dst_key": key, "cosine_sim": r["cosine_sim"],
                        "rank": r["rank"], "model_name": r["model_name"]})
        return out

    def _statistical_topk(self, ordinance_id: str, k: int) -> list[dict]:
        """similarity_edges(통계 임베딩) Top-k 폴백."""
        rows = db.fetchall(
            self.conn,
            "SELECT dst_id, cosine_sim, rank, model_name FROM similarity_edges "
            "WHERE src_id=? ORDER BY rank LIMIT ?",
            (ordinance_id, k),
        )
        return [{"dst_id": r["dst_id"], "dst_key": r["dst_id"],
                 "cosine_sim": r["cosine_sim"], "rank": r["rank"],
                 "model_name": r.get("model_name")} for r in rows]

    # ---- tool 10: neural_similar_regions ----------------------------------
    def _tool_neural_similar_regions(self, args: dict) -> dict:
        region = self._resolve_region(region_id=args.get("region_id"),
                                      sig_cd=args.get("sig_cd"))
        if region is None:
            raise ToolError(
                f"지역을 찾을 수 없음: region_id={args.get('region_id')!r} "
                f"sig_cd={args.get('sig_cd')!r}")
        k = max(1, min(_as_int(args.get("k"), 10) or 10, 50))
        model = self._pick_model(args.get("model"))
        node_id = f"region:{region['region_id']}"

        rows = self._neural_topk(node_id, k, model)
        results = []
        for r in rows:
            rid = r["dst_key"]
            meta = db.fetchone(
                self.conn,
                "SELECT region_id, sig_cd, name, full_name, level, population, status "
                "FROM regions WHERE region_id=?", (rid,),
            ) or {"region_id": rid}
            meta["cosine_sim"] = r["cosine_sim"]
            meta["rank"] = r["rank"]
            meta["model_name"] = r.get("model_name")
            results.append(meta)

        payload = {
            "base_region": {"region_id": region["region_id"], "sig_cd": region.get("sig_cd"),
                            "name": region.get("full_name") or region.get("name"),
                            "level": region.get("level")},
            "k": k, "model": model, "count": len(results), "peers": results,
            "similarity_basis": "graph-embedding",
            "contrast": (
                "이 tool 은 그래프 임베딩(구조 학습) 기반이다. 재정규모·인구·조례구조의 "
                "통계 유사도는 similar_regions tool 을 사용하라 — 두 결과는 다를 수 있고, "
                "다르다는 사실 자체가 '구조적 이웃'과 '규모적 이웃'의 차이를 드러낸다."),
            "verification_status": "unverified",
            "verification_note": (
                "학습 임베딩은 검증 대상 원천이 아니라 파생 추정치다(unverified). "
                "정책 판단 시 similar_regions 통계 결과와 교차 확인하라."),
            "_engine": f"neural_similarity[{model}]" if results else "unavailable",
        }
        if not results:
            payload["fallback"] = self._peer_fallback(region, k, ["pop", "budget", "structure"])
            payload["note"] = ("그래프 임베딩 미학습(node_embeddings 부재) → 통계 폴백 결과를 "
                               "fallback 필드에 첨부. 'python -m policymap.run neural' 로 학습.")
        return self._envelope(payload)

    # ---- tool 11: ordinance_effectiveness ---------------------------------
    def _tool_ordinance_effectiveness(self, args: dict) -> dict:
        ordinance_ident = args.get("ordinance_id") or args.get("mst")
        region_ident = args.get("region_id") or args.get("sig_cd")
        fyr = _as_int(args.get("fyr"), None)
        min_confidence = float(args.get("min_confidence") or 0.0)
        if not ordinance_ident and not region_ident:
            raise ToolError("ordinance_id 또는 region_id 중 하나가 필요")

        if ordinance_ident:
            node = self._resolve_node(ordinance_ident)
            if node is None or node["kind"] != "ordinance":
                raise ToolError(f"조례를 찾을 수 없음: {ordinance_ident!r}")
            payload = self._effectiveness_for_ordinances(
                [node["key"]], fyr=fyr, min_confidence=min_confidence)
            payload["scope"] = {"mode": "ordinance", "ordinance_id": node["key"],
                                "name": node.get("name"),
                                "verification_status": node.get("verification_status")}
            return self._envelope(payload, official_url=node.get("official_url"))

        region = self._resolve_region(region_id=args.get("region_id"),
                                      sig_cd=args.get("sig_cd"))
        if region is None:
            raise ToolError(f"지역을 찾을 수 없음: {region_ident!r}")
        ord_ids = [r["ordinance_id"] for r in db.fetchall(
            self.conn,
            "SELECT DISTINCT l.ordinance_id FROM ordinance_budget_link l "
            "JOIN ordinances o ON o.ordinance_id=l.ordinance_id "
            "WHERE o.region_id=? AND l.confidence>=?",
            (region["region_id"], min_confidence),
        )]
        payload = self._effectiveness_for_ordinances(
            ord_ids, fyr=fyr, min_confidence=min_confidence)
        payload["scope"] = {"mode": "region", "region_id": region["region_id"],
                            "sig_cd": region.get("sig_cd"),
                            "name": region.get("full_name") or region.get("name"),
                            "linked_ordinances": len(ord_ids)}
        payload["region_budget_baseline"] = self._region_budget_baseline(
            region["region_id"], region.get("lofin_laf_cd"), fyr)
        return self._envelope(payload)

    def _effectiveness_for_ordinances(self, ordinance_ids: list[str], *,
                                      fyr: Optional[int],
                                      min_confidence: float) -> dict:
        """조례↔예산 링크를 따라 편성액·예산현액·지출액·집행률을 집계.

        집행률 정의를 두 가지로 함께 제시한다(원천 데이터 그대로, 가공 없음):
          exec_rate_vs_now   = 지출액 / 예산현액(budget_now)
          exec_rate_vs_alloc = 지출액 / 편성액(alloc_amt)
        링크는 자동매칭(verified=0)과 수작업 검증(verified=1)을 분리해 보고한다.
        """
        if not ordinance_ids:
            return {"link_count": 0, "budget_lines": 0, "totals": {}, "by_ordinance": [],
                    "verification": {"verified_links": 0, "auto_links": 0,
                                     "status": "no-link"},
                    "_engine": "sql:ordinance_budget_link⋈budget_lines"}
        placeholders = ",".join("?" for _ in ordinance_ids)
        params: list[Any] = list(ordinance_ids) + [min_confidence]
        fyr_clause = ""
        if fyr:
            fyr_clause = " AND b.fyr=?"
            params.append(fyr)
        rows = db.fetchall(
            self.conn,
            f"""SELECT l.ordinance_id, l.budget_id, l.match_method, l.confidence, l.verified,
                       b.fyr, b.dbiz_nm, b.field, b.sector, b.dept_cd,
                       COALESCE(b.alloc_amt,0)  AS alloc_amt,
                       COALESCE(b.budget_now,0) AS budget_now,
                       COALESCE(b.exe_amt,0)    AS exe_amt,
                       b.exe_ymd, b.as_of_date
                FROM ordinance_budget_link l
                JOIN budget_lines b ON b.budget_id=l.budget_id
                WHERE l.ordinance_id IN ({placeholders}) AND l.confidence>=?{fyr_clause}""",
            params,
        )
        totals = {"alloc_amt": 0, "budget_now": 0, "exe_amt": 0}
        by_ord: dict[str, dict] = {}
        by_fyr: dict[Any, dict] = {}
        verified_links = auto_links = 0
        seen_links: set[tuple] = set()
        for r in rows:
            key = (r["ordinance_id"], r["budget_id"])
            if key not in seen_links:
                seen_links.add(key)
                if r.get("verified"):
                    verified_links += 1
                else:
                    auto_links += 1
            for f in ("alloc_amt", "budget_now", "exe_amt"):
                totals[f] += int(r[f] or 0)
            o = by_ord.setdefault(r["ordinance_id"], {
                "ordinance_id": r["ordinance_id"], "lines": 0,
                "alloc_amt": 0, "budget_now": 0, "exe_amt": 0,
                "verified_links": 0, "auto_links": 0, "methods": {}, "programs": []})
            o["lines"] += 1
            for f in ("alloc_amt", "budget_now", "exe_amt"):
                o[f] += int(r[f] or 0)
            if r.get("verified"):
                o["verified_links"] += 1
            else:
                o["auto_links"] += 1
            m = r.get("match_method") or "unknown"
            o["methods"][m] = o["methods"].get(m, 0) + 1
            if len(o["programs"]) < 20:
                o["programs"].append({
                    "budget_id": r["budget_id"], "fyr": r["fyr"], "dbiz_nm": r["dbiz_nm"],
                    "field": r.get("field"), "alloc_amt": int(r["alloc_amt"] or 0),
                    "budget_now": int(r["budget_now"] or 0),
                    "exe_amt": int(r["exe_amt"] or 0),
                    "exec_rate": self._rate(r["exe_amt"], r["budget_now"]),
                    "confidence": r.get("confidence"),
                    "match_method": r.get("match_method"),
                    "verified": bool(r.get("verified")),
                })
            fy = by_fyr.setdefault(r["fyr"], {"fyr": r["fyr"], "lines": 0, "alloc_amt": 0,
                                              "budget_now": 0, "exe_amt": 0,
                                              "exe_ymd": r.get("exe_ymd")})
            fy["lines"] += 1
            for f in ("alloc_amt", "budget_now", "exe_amt"):
                fy[f] += int(r[f] or 0)

        for o in by_ord.values():
            o["exec_rate_vs_now"] = self._rate(o["exe_amt"], o["budget_now"])
            o["exec_rate_vs_alloc"] = self._rate(o["exe_amt"], o["alloc_amt"])
            meta = db.fetchone(
                self.conn,
                "SELECT name, region_id, official_url, verification_status, status "
                "FROM ordinances WHERE ordinance_id=?", (o["ordinance_id"],)) or {}
            o.update({k: meta.get(k) for k in
                      ("name", "region_id", "official_url", "verification_status", "status")})
        for fy in by_fyr.values():
            fy["exec_rate_vs_now"] = self._rate(fy["exe_amt"], fy["budget_now"])
            fy["exec_rate_vs_alloc"] = self._rate(fy["exe_amt"], fy["alloc_amt"])

        ordered = sorted(by_ord.values(), key=lambda o: -o["exe_amt"])
        return {
            "link_count": len(seen_links),
            "budget_lines": len(rows),
            "fyr_filter": fyr,
            "min_confidence": min_confidence,
            "totals": {
                **totals,
                "exec_rate_vs_now": self._rate(totals["exe_amt"], totals["budget_now"]),
                "exec_rate_vs_alloc": self._rate(totals["exe_amt"], totals["alloc_amt"]),
            },
            "by_fiscal_year": sorted(by_fyr.values(), key=lambda f: str(f["fyr"])),
            "by_ordinance": ordered[:50],
            "verification": {
                "verified_links": verified_links,
                "auto_links": auto_links,
                "status": ("verified" if auto_links == 0 and verified_links > 0
                           else "partially-verified" if verified_links
                           else "unverified"),
                "note": (
                    "조례↔예산 링크는 도메인명사 교집합·분야게이트·부서가중 3채널 자동매칭 "
                    "결과다(graph.analysis.link_ordinance_budget). verified=0 링크는 "
                    "수작업 검증을 거치지 않았으므로 집행률은 참고치다."),
            },
            "caveat": (
                "집행률은 링크된 세부사업의 지출액/예산액 비율이며 조례의 정책효과가 아니다. "
                "회계연도 진행 중(당해년도) 스냅샷은 낮게 나오는 것이 정상이다."),
            "_engine": "sql:ordinance_budget_link⋈budget_lines",
        }

    @staticmethod
    def _rate(num: Any, den: Any) -> Optional[float]:
        try:
            n, d = float(num or 0), float(den or 0)
        except (TypeError, ValueError):
            return None
        return round(n / d, 4) if d else None

    def _region_budget_baseline(self, region_id: str, laf_cd: Optional[str],
                                fyr: Optional[int]) -> dict:
        """지자체 전체 세출 대비 비교 기준선(링크된 사업이 전체의 몇 %인지)."""
        params: list[Any] = [region_id, laf_cd or ""]
        clause = ""
        if fyr:
            clause = " AND fyr=?"
            params.append(fyr)
        row = db.fetchone(
            self.conn,
            "SELECT COUNT(*) AS lines, COALESCE(SUM(alloc_amt),0) AS alloc_amt, "
            "       COALESCE(SUM(budget_now),0) AS budget_now, "
            "       COALESCE(SUM(exe_amt),0) AS exe_amt, MAX(fyr) AS max_fyr, "
            "       MAX(exe_ymd) AS exe_ymd "
            f"FROM budget_lines WHERE (region_id=? OR laf_cd=?){clause}",
            params,
        ) or {}
        row["exec_rate_vs_now"] = self._rate(row.get("exe_amt"), row.get("budget_now"))
        row["exec_rate_vs_alloc"] = self._rate(row.get("exe_amt"), row.get("alloc_amt"))
        return row

    # ---- tool 12: explain_path --------------------------------------------
    # 관계별 이웃 조회(무방향 탐색). (SQL, 파라미터빌더, 이웃 kind, 관계명, 방향)
    _PATH_MAX_FANOUT = 200         # 관계당 이웃 상한(허브 노드 폭발 차단)
    _PATH_MAX_VISITS = 20000       # 총 방문 노드 상한

    def _neighbors(self, node: str) -> list[tuple[str, str, dict]]:
        """노드 1홉 이웃 [(이웃 node_id, relation, evidence)]. 방향 무시(양쪽 조회)."""
        kind, _, key = node.partition(":")
        cap = self._PATH_MAX_FANOUT
        out: list[tuple[str, str, dict]] = []

        def add(nid: str, rel: str, ev: dict) -> None:
            if nid and nid != node:
                out.append((nid, rel, ev))

        try:
            if kind == "region":
                for r in db.fetchall(self.conn,
                                     "SELECT ordinance_id, name FROM ordinances "
                                     "WHERE region_id=? AND COALESCE(status,'active')='active' "
                                     "LIMIT ?", (key, cap)):
                    add(f"ordinance:{r['ordinance_id']}", "HAS_ORDINANCE", {"name": r["name"]})
                for r in db.fetchall(self.conn,
                                     "SELECT neighbor_id, contiguity_type FROM region_adjacency "
                                     "WHERE region_id=? LIMIT ?", (key, cap)):
                    add(f"region:{r['neighbor_id']}", "ADJACENT_TO",
                        {"contiguity_type": r.get("contiguity_type")})
                for r in db.fetchall(self.conn,
                                     "SELECT region_id FROM region_adjacency "
                                     "WHERE neighbor_id=? LIMIT ?", (key, cap)):
                    add(f"region:{r['region_id']}", "ADJACENT_TO", {})
                for r in db.fetchall(self.conn,
                                     "SELECT region_id, parent_region FROM regions "
                                     "WHERE region_id=? OR parent_region=? LIMIT ?",
                                     (key, key, cap)):
                    if r["region_id"] == key and r.get("parent_region"):
                        add(f"region:{r['parent_region']}", "CONTAINS", {"direction": "up"})
                    elif r.get("parent_region") == key:
                        add(f"region:{r['region_id']}", "CONTAINS", {"direction": "down"})
                for r in db.fetchall(self.conn,
                                     "SELECT old_region_id, new_region_id, succession_type "
                                     "FROM region_succession WHERE old_region_id=? OR new_region_id=?",
                                     (key, key)):
                    other = (r["new_region_id"] if r["old_region_id"] == key
                             else r["old_region_id"])
                    add(f"region:{other}", "SUCCEEDED_BY",
                        {"succession_type": r.get("succession_type")})
            elif kind == "ordinance":
                row = db.fetchone(self.conn,
                                  "SELECT region_id FROM ordinances WHERE ordinance_id=?", (key,))
                if row and row.get("region_id"):
                    add(f"region:{row['region_id']}", "HAS_ORDINANCE", {"direction": "up"})
                for r in db.fetchall(self.conn,
                                     "SELECT parent_id, parent_article, child_article, relation, "
                                     "       delegation_type, citation_text FROM delegations "
                                     "WHERE child_kind='ordinance' AND child_id=? LIMIT ?",
                                     (key, cap)):
                    add(f"instrument:{r['parent_id']}", "DELEGATED_FROM",
                        {"parent_article": r.get("parent_article"),
                         "child_article": r.get("child_article"),
                         "delegation_type": r.get("delegation_type"),
                         "citation_text": r.get("citation_text")})
                for r in db.fetchall(self.conn,
                                     "SELECT dst_kind, dst_id, relation, citation_text "
                                     "FROM instrument_relations WHERE src_kind='ordinance' "
                                     "AND src_id=? LIMIT ?", (key, cap)):
                    nk = "ordinance" if r["dst_kind"] == "ordinance" else "instrument"
                    add(f"{nk}:{r['dst_id']}", r.get("relation") or "CITES",
                        {"citation_text": r.get("citation_text")})
                for r in db.fetchall(self.conn,
                                     "SELECT dst_id, cosine_sim, rank FROM similarity_edges "
                                     "WHERE src_id=? ORDER BY rank LIMIT ?", (key, min(cap, 20))):
                    add(f"ordinance:{r['dst_id']}", "SIMILAR_TO",
                        {"cosine_sim": r.get("cosine_sim"), "rank": r.get("rank")})
                for r in db.fetchall(self.conn,
                                     "SELECT src_id, cosine_sim FROM similarity_edges "
                                     "WHERE dst_id=? LIMIT ?", (key, min(cap, 20))):
                    add(f"ordinance:{r['src_id']}", "SIMILAR_TO",
                        {"cosine_sim": r.get("cosine_sim")})
                for r in db.fetchall(self.conn,
                                     "SELECT category_code, confidence FROM ordinance_category "
                                     "WHERE ordinance_id=? LIMIT ?", (key, cap)):
                    add(f"category:{r['category_code']}", "IN_CATEGORY",
                        {"confidence": r.get("confidence")})
                for r in db.fetchall(self.conn,
                                     "SELECT budget_id, confidence, match_method, verified "
                                     "FROM ordinance_budget_link WHERE ordinance_id=? "
                                     "ORDER BY confidence DESC LIMIT ?", (key, min(cap, 20))):
                    add(f"budget:{r['budget_id']}", "FUNDED_BY",
                        {"confidence": r.get("confidence"),
                         "match_method": r.get("match_method"),
                         "verified": bool(r.get("verified"))})
                for nid, rel, ev in self._neural_neighbors(node, min(cap, 10)):
                    add(nid, rel, ev)
            elif kind == "instrument":
                for r in db.fetchall(self.conn,
                                     "SELECT child_kind, child_id, parent_article, "
                                     "       delegation_type FROM delegations "
                                     "WHERE parent_id=? LIMIT ?", (key, cap)):
                    nk = "ordinance" if r["child_kind"] == "ordinance" else "instrument"
                    add(f"{nk}:{r['child_id']}", "DELEGATED_FROM",
                        {"direction": "down", "parent_article": r.get("parent_article"),
                         "delegation_type": r.get("delegation_type")})
                for r in db.fetchall(self.conn,
                                     "SELECT src_kind, src_id, relation, citation_text "
                                     "FROM instrument_relations WHERE dst_id=? LIMIT ?",
                                     (key, cap)):
                    nk = "ordinance" if r["src_kind"] == "ordinance" else "instrument"
                    add(f"{nk}:{r['src_id']}", r.get("relation") or "CITES",
                        {"citation_text": r.get("citation_text"), "direction": "in"})
                for r in db.fetchall(self.conn,
                                     "SELECT dst_kind, dst_id, relation FROM instrument_relations "
                                     "WHERE src_kind!='ordinance' AND src_id=? LIMIT ?",
                                     (key, cap)):
                    nk = "ordinance" if r["dst_kind"] == "ordinance" else "instrument"
                    add(f"{nk}:{r['dst_id']}", r.get("relation") or "CITES", {"direction": "out"})
                for r in db.fetchall(self.conn,
                                     "SELECT bill_id FROM bills WHERE enacted_instrument_id=? "
                                     "LIMIT ?", (key, cap)):
                    add(f"bill:{r['bill_id']}", "ENACTS", {"direction": "in"})
            elif kind == "category":
                for r in db.fetchall(self.conn,
                                     "SELECT ordinance_id, confidence FROM ordinance_category "
                                     "WHERE category_code=? ORDER BY confidence DESC LIMIT ?",
                                     (key, cap)):
                    add(f"ordinance:{r['ordinance_id']}", "IN_CATEGORY",
                        {"confidence": r.get("confidence")})
            elif kind == "budget":
                for r in db.fetchall(self.conn,
                                     "SELECT ordinance_id, confidence, match_method "
                                     "FROM ordinance_budget_link WHERE budget_id=? LIMIT ?",
                                     (key, cap)):
                    add(f"ordinance:{r['ordinance_id']}", "FUNDED_BY",
                        {"confidence": r.get("confidence"),
                         "match_method": r.get("match_method"), "direction": "in"})
                row = db.fetchone(self.conn,
                                  "SELECT region_id, dbiz_nm FROM budget_lines WHERE budget_id=?",
                                  (key,))
                if row and row.get("region_id"):
                    add(f"region:{row['region_id']}", "BUDGET_OF", {"dbiz_nm": row.get("dbiz_nm")})
            elif kind == "bill":
                for r in db.fetchall(self.conn,
                                     "SELECT legislator_id, role FROM bill_proposers "
                                     "WHERE bill_id=? LIMIT ?", (key, cap)):
                    add(f"legislator:{r['legislator_id']}", "PROPOSED_BY",
                        {"role": r.get("role")})
                row = db.fetchone(self.conn,
                                  "SELECT enacted_instrument_id FROM bills WHERE bill_id=?", (key,))
                if row and row.get("enacted_instrument_id"):
                    add(f"instrument:{row['enacted_instrument_id']}", "ENACTS", {})
            elif kind == "legislator":
                for r in db.fetchall(self.conn,
                                     "SELECT bill_id, role FROM bill_proposers "
                                     "WHERE legislator_id=? LIMIT ?", (key, cap)):
                    add(f"bill:{r['bill_id']}", "PROPOSED_BY", {"role": r.get("role")})
                row = db.fetchone(self.conn,
                                  "SELECT current_party FROM legislators WHERE legislator_id=?",
                                  (key,))
                if row and row.get("current_party"):
                    p = db.fetchone(self.conn, "SELECT party_id FROM parties WHERE name=?",
                                    (row["current_party"],))
                    if p:
                        add(f"party:{p['party_id']}", "MEMBER_OF", {})
            elif kind == "party":
                for r in db.fetchall(self.conn,
                                     "SELECT legislator_id FROM legislators WHERE current_party="
                                     "(SELECT name FROM parties WHERE party_id=?) LIMIT ?",
                                     (key, cap)):
                    add(f"legislator:{r['legislator_id']}", "MEMBER_OF", {})
        except Exception as exc:  # 테이블 부재 등은 이웃 없음으로 강등
            self.log.debug("이웃 조회 실패(%s): %s", node, exc)
        return out

    def _neural_neighbors(self, node: str, k: int) -> list[tuple[str, str, dict]]:
        """neural_similarity 이웃(학습 임베딩 근접). 미학습이면 빈 목록."""
        model = self._pick_model(None)
        if not model:
            return []
        out = []
        for r in self._neural_topk(node, k, model):
            out.append((r["dst_id"], "NEURAL_SIMILAR",
                        {"cosine_sim": r["cosine_sim"], "rank": r["rank"], "model": model}))
        return out

    def _tool_explain_path(self, args: dict) -> dict:
        src = self._resolve_node(args.get("from_id") or args.get("src_id"))
        dst = self._resolve_node(args.get("to_id") or args.get("dst_id"))
        if src is None:
            raise ToolError(f"출발 노드를 찾을 수 없음: {args.get('from_id')!r}")
        if dst is None:
            raise ToolError(f"도착 노드를 찾을 수 없음: {args.get('to_id')!r}")
        max_hops = max(1, min(_as_int(args.get("max_hops"), 4) or 4, 6))

        if src["node_id"] == dst["node_id"]:
            payload = {"from": src, "to": dst, "found": True, "hops": 0, "path": [],
                       "explanation": "동일 노드다.", "_engine": "bfs-sql"}
            return self._envelope(payload)

        path, visits = self._bidirectional_bfs(src["node_id"], dst["node_id"], max_hops)
        shared = self._shared_context(src, dst)
        if path is None:
            payload = {
                "from": src, "to": dst, "found": False, "max_hops": max_hops,
                "visited_nodes": visits, "path": [], "shared_context": shared,
                "explanation": (
                    f"{max_hops}홉 이내(방문 {visits}노드)에 연결 경로가 없다. 관계당 이웃 상한 "
                    f"{self._PATH_MAX_FANOUT}개를 적용한 탐색이므로 '경로 없음'은 "
                    "'상한 내에서 찾지 못함'을 뜻한다."),
                "verification_status": "unverified",
                "_engine": "bfs-sql",
            }
            return self._envelope(payload)

        steps = self._describe_path(path)
        payload = {
            "from": src, "to": dst, "found": True, "hops": len(steps),
            "max_hops": max_hops, "visited_nodes": visits,
            "path": steps,
            "path_string": " → ".join(
                [self._node_label(path[0][0])]
                + [f"-[{s['relation']}]-> {s['to_label']}" for s in steps]),
            "relations": [s["relation"] for s in steps],
            "shared_context": shared,
            "explanation": self._path_narrative(src, dst, steps, shared),
            "verification_status": self._path_verification(steps),
            "verification_note": (
                "SIMILAR_TO / NEURAL_SIMILAR / FUNDED_BY(verified=0) 는 자동 추정 관계다. "
                "DELEGATED_FROM / CITES / HAS_ORDINANCE / ADJACENT_TO 는 원천 데이터 관계다."),
            "_engine": "bfs-sql(bidirectional)",
        }
        return self._envelope(payload)

    def _bidirectional_bfs(self, src: str, dst: str, max_hops: int):
        """양방향 BFS. 반환 (경로 [(node, rel_from_prev, evidence)] | None, 방문수)."""
        from collections import deque
        fwd: dict[str, tuple] = {src: (None, None, None)}   # node -> (prev, rel, ev)
        bwd: dict[str, tuple] = {dst: (None, None, None)}
        fq, bq = deque([(src, 0)]), deque([(dst, 0)])
        visits = 2
        limit = self._PATH_MAX_VISITS
        half_f = (max_hops + 1) // 2
        half_b = max_hops // 2

        while (fq or bq) and visits < limit:
            for queue, seen, other, depth_cap in (
                (fq, fwd, bwd, half_f), (bq, bwd, fwd, half_b),
            ):
                if not queue:
                    continue
                node, depth = queue.popleft()
                if depth >= depth_cap:
                    continue
                for nb, rel, ev in self._neighbors(node):
                    if nb in seen:
                        continue
                    seen[nb] = (node, rel, ev)
                    visits += 1
                    if nb in other:
                        return self._join_paths(nb, fwd, bwd), visits
                    queue.append((nb, depth + 1))
                    if visits >= limit:
                        break
        return None, visits

    @staticmethod
    def _join_paths(mid: str, fwd: dict, bwd: dict) -> list[tuple]:
        """만난 노드에서 앞/뒤 경로를 이어 [(node, rel_from_prev, evidence)] 로."""
        left: list[tuple] = []
        node = mid
        while node is not None:
            prev, rel, ev = fwd[node]
            left.append((node, rel, ev))
            node = prev
        left.reverse()
        right: list[tuple] = []
        node = mid
        while True:
            nxt, rel, ev = bwd[node]
            if nxt is None:
                break
            right.append((nxt, rel, ev))
            node = nxt
        return left + right

    def _node_label(self, node_id: str) -> str:
        kind, _, key = node_id.partition(":")
        meta = self._node_meta(kind, key)
        name = (meta or {}).get("name")
        return f"{name}({node_id})" if name else node_id

    def _describe_path(self, path: list[tuple]) -> list[dict]:
        steps = []
        for i in range(1, len(path)):
            node, rel, ev = path[i]
            prev = path[i - 1][0]
            steps.append({
                "hop": i,
                "from": prev, "from_label": self._node_label(prev),
                "relation": rel or "UNKNOWN",
                "to": node, "to_label": self._node_label(node),
                "evidence": ev or {},
                "inferred": (rel in ("SIMILAR_TO", "NEURAL_SIMILAR", "FUNDED_BY")),
            })
        return steps

    _REL_KO = {
        "HAS_ORDINANCE": "지자체가 제정한 조례",
        "DELEGATED_FROM": "상위법 위임 근거",
        "CITES": "조문 명시 인용",
        "SIMILAR_TO": "조문 텍스트 통계 유사(추정)",
        "NEURAL_SIMILAR": "그래프 임베딩 근접(추정)",
        "IN_CATEGORY": "정책분야 분류",
        "FUNDED_BY": "연계 집행예산(자동매칭)",
        "ADJACENT_TO": "행정구역 인접",
        "CONTAINS": "행정 계층 포함",
        "SUCCEEDED_BY": "행정구역 승계",
        "BUDGET_OF": "해당 지자체 예산",
        "PROPOSED_BY": "의안 발의",
        "MEMBER_OF": "정당 소속",
        "ENACTS": "의안 → 제정 법령",
    }

    def _path_narrative(self, src: dict, dst: dict, steps: list[dict],
                        shared: dict) -> str:
        parts = [f"'{src.get('name') or src['node_id']}' 와(과) "
                 f"'{dst.get('name') or dst['node_id']}' 는 {len(steps)}홉으로 연결된다."]
        for s in steps:
            ko = self._REL_KO.get(s["relation"], s["relation"])
            tail = ""
            ev = s.get("evidence") or {}
            if ev.get("citation_text"):
                tail = f" (인용 '{ev['citation_text']}')"
            elif ev.get("parent_article"):
                tail = f" ({ev['parent_article']})"
            elif ev.get("cosine_sim") is not None:
                tail = f" (코사인 {ev['cosine_sim']})"
            elif ev.get("confidence") is not None:
                tail = f" (신뢰도 {ev['confidence']})"
            parts.append(f"{s['hop']}. {ko}: {s['from_label']} → {s['to_label']}{tail}")
        if shared.get("shared_parent_instruments"):
            names = [p.get("name") for p in shared["shared_parent_instruments"][:3]]
            parts.append("공통 상위법: " + ", ".join(str(n) for n in names if n))
        if shared.get("shared_categories"):
            parts.append("공통 분야: " + ", ".join(
                str(c.get("name") or c.get("code")) for c in shared["shared_categories"]))
        for key, label in (("direct_similarity", "직접 통계유사도"),
                           ("direct_neural_similarity", "직접 신경망유사도")):
            info = shared.get(key)
            if info:
                parts.append(f"{label}: {info.get('cosine_sim')}"
                             f"({info.get('model_name')})")
        return " ".join(parts)

    @staticmethod
    def _path_verification(steps: list[dict]) -> str:
        if not steps:
            return "unverified"
        return "unverified" if any(s["inferred"] for s in steps) else "verified"

    def _shared_context(self, src: dict, dst: dict) -> dict:
        """두 노드의 공통 근거(왜 비슷한가). 조례 쌍일 때 가장 풍부."""
        out: dict[str, Any] = {}
        if src["kind"] != "ordinance" or dst["kind"] != "ordinance":
            return out
        a, b = src["key"], dst["key"]
        out["shared_parent_instruments"] = db.fetchall(
            self.conn,
            """SELECT li.instrument_id, li.name, li.kind, li.official_url
               FROM legal_instrument li
               WHERE li.instrument_id IN (
                     SELECT parent_id FROM delegations
                     WHERE child_kind='ordinance' AND child_id=?
                 INTERSECT
                     SELECT parent_id FROM delegations
                     WHERE child_kind='ordinance' AND child_id=?)
               LIMIT 10""",
            (a, b),
        )
        out["shared_categories"] = db.fetchall(
            self.conn,
            """SELECT c.code, c.name FROM categories c
               WHERE c.code IN (
                     SELECT category_code FROM ordinance_category WHERE ordinance_id=?
                 INTERSECT
                     SELECT category_code FROM ordinance_category WHERE ordinance_id=?)""",
            (a, b),
        )
        sim = db.fetchone(
            self.conn,
            "SELECT cosine_sim, model_name FROM similarity_edges "
            "WHERE (src_id=? AND dst_id=?) OR (src_id=? AND dst_id=?) LIMIT 1",
            (a, b, b, a),
        )
        if sim:
            out["direct_similarity"] = {"cosine_sim": sim["cosine_sim"],
                                        "model_name": sim.get("model_name"),
                                        "basis": "statistical(char-ngram-tf)"}
        nsim = db.fetchone(
            self.conn,
            "SELECT cosine_sim, model_name FROM neural_similarity "
            "WHERE (src_id=? AND dst_id=?) OR (src_id=? AND dst_id=?) LIMIT 1",
            (src["node_id"], dst["node_id"], dst["node_id"], src["node_id"]),
        )
        if nsim:
            out["direct_neural_similarity"] = {"cosine_sim": nsim["cosine_sim"],
                                               "model_name": nsim.get("model_name"),
                                               "basis": "graph-embedding"}
        ra = db.fetchone(self.conn, "SELECT region_id FROM ordinances WHERE ordinance_id=?", (a,))
        rb = db.fetchone(self.conn, "SELECT region_id FROM ordinances WHERE ordinance_id=?", (b,))
        if ra and rb and ra.get("region_id") and rb.get("region_id"):
            if ra["region_id"] == rb["region_id"]:
                out["same_region"] = ra["region_id"]
            else:
                adj = db.fetchone(
                    self.conn,
                    "SELECT 1 AS x FROM region_adjacency "
                    "WHERE (region_id=? AND neighbor_id=?) OR (region_id=? AND neighbor_id=?)",
                    (ra["region_id"], rb["region_id"], rb["region_id"], ra["region_id"]),
                )
                out["adjacent_regions"] = bool(adj)
        return out

    def _tool_recommend_ordinances(self, args: dict) -> dict:
        region = self._resolve_region(
            region_id=args.get("region_id"), sig_cd=args.get("sig_cd"))
        if region is None:
            raise ToolError(
                f"기준 지역 없음: sig_cd={args.get('sig_cd')!r} "
                f"region_id={args.get('region_id')!r}")
        eff_sig = region.get("sig_cd") or args.get("sig_cd")
        if not eff_sig:
            raise ToolError("시군구코드(sig_cd)를 확정할 수 없다")

        an = self.analytics()
        if an is None:
            raise ToolError(
                "policymap.analytics 미탑재(numpy 필요) — recommend_ordinances 는 폴백이 없다")
        res = an["peers"].recommend_ordinances(
            self.conn, eff_sig,
            k=_as_int(args.get("k"), 15) or 15,
            min_peers=_as_int(args.get("min_peers"), 3) or 3,
            limit=max(1, min(_as_int(args.get("limit"), 30) or 30, 200)))
        payload = dict(res)
        payload["_engine"] = "analytics.peers.recommend_ordinances"
        # 추천은 '없는 조례'의 존재 판정이라 정규화 실패 시 오탐이 난다. 미검증 명시.
        payload["verification_status"] = "unverified"
        payload["caveat"] = (
            "보유 여부는 조례 '명칭' 정규형 비교로 판정한다. 명칭이 다르나 같은 사항을 "
            "규율하는 조례가 이미 있을 수 있으므로, 채택 전 likely_variant 와 "
            "closest_own 을 반드시 확인하라.")
        return self._envelope(payload)

    def _tool_spatial_autocorrelation(self, args: dict) -> dict:
        metric = args.get("metric")
        if not metric:
            raise ToolError("metric 이 필요")
        an = self.analytics()
        if an is None:
            raise ToolError(
                "policymap.analytics 미탑재(numpy 필요) — spatial_autocorrelation 은 폴백이 없다")
        lisa = args.get("lisa")
        res = an["spatial"].moran(
            self.conn, str(metric),
            fyr=_as_int(args.get("fyr"), 2025) or 2025,
            permutations=max(99, min(_as_int(args.get("permutations"), 999) or 999, 9999)),
            lisa=True if lisa is None else bool(lisa))
        payload = dict(res)
        payload["_engine"] = "analytics.spatial.moran"
        payload["reading_guide"] = (
            "전역 I>0 이면 유사한 값이 공간적으로 뭉쳐 있다는 뜻이며, p_sim 은 "
            "순열분포 기준이다. 국지 LISA 는 다중비교 때문에 반드시 significant"
            "(BH-FDR 통과) 인 곳만 해석하라 — 미보정 시 위양성이 10곳 안팎 발생한다.")
        return self._envelope(payload)

    # tool 이름 → 핸들러
    def _dispatch_tool(self, name: str, args: dict) -> dict:
        handlers = {
            "search_ordinance": self._tool_search_ordinance,
            "get_ordinance": self._tool_get_ordinance,
            "similar_regions": self._tool_similar_regions,
            "gap_analysis": self._tool_gap_analysis,
            "diffusion_timeline": self._tool_diffusion_timeline,
            "region_profile": self._tool_region_profile,
            "bill_vote_breakdown": self._tool_bill_vote_breakdown,
            # 신경망 / RAG 계층
            "semantic_search_ordinance": self._tool_semantic_search_ordinance,
            "similar_ordinances": self._tool_similar_ordinances,
            "neural_similar_regions": self._tool_neural_similar_regions,
            "ordinance_effectiveness": self._tool_ordinance_effectiveness,
            "explain_path": self._tool_explain_path,
            # 정책확산 계량 계층(analytics)
            "recommend_ordinances": self._tool_recommend_ordinances,
            "spatial_autocorrelation": self._tool_spatial_autocorrelation,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ToolError(f"알 수 없는 tool: {name}")
        return handler(args or {})

    # ======================================================================= #
    # 리소스
    # ======================================================================= #
    def _resource_list(self) -> list[dict]:
        return [
            {
                "uri": "ordinance-graph://status",
                "name": "데이터 신선도·안전정책",
                "description": "수집 기준일·stale 여부·엔터티 규모·안전 게이트 상태",
                "mimeType": "application/json",
            },
        ]

    def _resource_templates(self) -> list[dict]:
        return [
            {
                "uriTemplate": "ordinance-graph://peers/{sig_cd}",
                "name": "유사 지자체",
                "description": "sig_cd 기준 similar_regions 결과",
                "mimeType": "application/json",
            },
            {
                "uriTemplate": "ordinance-graph://changes?since={since}",
                "name": "변경 피드",
                "description": "change_log(since 이후) 최근 변경 이력",
                "mimeType": "application/json",
            },
        ]

    def _read_resource(self, uri: str) -> dict:
        if uri == "ordinance-graph://status":
            fresh = self._freshness()
            counts = {}
            for t in ("ordinances", "legal_instrument", "delegations", "regions",
                      "bills", "votes", "budget_lines", "change_log"):
                try:
                    counts[t] = db.count(self.conn, t)
                except Exception:
                    counts[t] = None
            body = {
                "server": SERVER_NAME, "version": SERVER_VERSION,
                "freshness": fresh,
                "execution_allowed": False,
                "read_only": True,
                "analysis_engine": "graph.analysis" if self.analysis() else "fallback-sql",
                "rag_engine": "policymap.rag" if self.rag() else "fallback-sql-like",
                "neural_models": self._neural_models(),
                "tool_count": len(self._tools),
                "entity_counts": counts,
                "disclaimer": DISCLAIMER,
            }
            return {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps(body, ensure_ascii=False, indent=2)}

        if uri.startswith("ordinance-graph://peers/"):
            sig = uri[len("ordinance-graph://peers/"):].split("?")[0].strip("/")
            env = self._tool_similar_regions({"sig_cd": sig})
            return {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps(env, ensure_ascii=False, indent=2)}

        if uri.startswith("ordinance-graph://changes"):
            since = None
            if "since=" in uri:
                since = uri.split("since=", 1)[1].split("&")[0]
            if since:
                rows = db.fetchall(
                    self.conn,
                    "SELECT change_id, ts, entity_type, entity_id, entity_name, event, "
                    "region_code, official_url FROM change_log WHERE ts>=? "
                    "ORDER BY ts DESC LIMIT 200", (since,),
                )
            else:
                rows = db.fetchall(
                    self.conn,
                    "SELECT change_id, ts, entity_type, entity_id, entity_name, event, "
                    "region_code, official_url FROM change_log ORDER BY ts DESC LIMIT 200",
                )
            body = self._envelope({"since": since, "count": len(rows), "changes": rows})
            return {"uri": uri, "mimeType": "application/json",
                    "text": json.dumps(body, ensure_ascii=False, indent=2)}

        raise ToolError(f"알 수 없는 리소스 URI: {uri}")

    # ======================================================================= #
    # JSON-RPC 라우팅
    # ======================================================================= #
    def handle_request(self, msg: dict) -> Optional[dict]:
        """단일 JSON-RPC 메시지 처리. 응답 dict 반환(notification 이면 None)."""
        msg_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        try:
            if method == "initialize":
                result = self._on_initialize(params)
            elif method in ("notifications/initialized", "initialized"):
                return None  # 알림 — 응답 없음
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._tools}
            elif method == "tools/call":
                result = self._on_tools_call(params)
            elif method == "resources/list":
                result = {"resources": self._resource_list()}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": self._resource_templates()}
            elif method == "resources/read":
                result = self._on_resources_read(params)
            elif method in ("prompts/list",):
                result = {"prompts": []}
            else:
                if is_notification:
                    return None
                return _error_response(msg_id, _METHOD_NOT_FOUND, f"미지원 메서드: {method}")
        except ToolError as exc:
            if is_notification:
                return None
            return _error_response(msg_id, _INVALID_PARAMS, str(exc))
        except Exception as exc:  # 내부 오류
            self.log.exception("내부 오류: %s", exc)
            if is_notification:
                return None
            return _error_response(msg_id, _INTERNAL_ERROR, f"내부 오류: {exc}")

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _on_initialize(self, params: dict) -> dict:
        client_proto = params.get("protocolVersion") or PROTOCOL_VERSION
        return {
            "protocolVersion": client_proto,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _on_tools_call(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not name:
            raise ToolError("tools/call: name 누락")
        try:
            envelope = self._dispatch_tool(name, args)
        except ToolError as exc:
            # tool 수준 오류는 프로토콜 오류가 아니라 result.isError(MCP 규약)
            return {
                "content": [{"type": "text",
                             "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}],
                "isError": True,
            }
        text = json.dumps(envelope, ensure_ascii=False, indent=2)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def _on_resources_read(self, params: dict) -> dict:
        uri = params.get("uri")
        if not uri:
            raise ToolError("resources/read: uri 누락")
        content = self._read_resource(uri)
        return {"contents": [content]}

    # ======================================================================= #
    # stdio 루프
    # ======================================================================= #
    def serve_stdio(self) -> None:
        """sys.stdin 줄단위 JSON-RPC 루프. stdout 는 응답 전용(로그는 stderr)."""
        try:
            sys.stdin.reconfigure(encoding="utf-8")       # type: ignore[attr-defined]
            sys.stdout.reconfigure(encoding="utf-8")      # type: ignore[attr-defined]
        except Exception:
            pass
        self.log.info("MCP 서버 시작(%s v%s). tool %d종, stdio 대기.",
                      SERVER_NAME, SERVER_VERSION, len(self._tools))
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                self._write(_error_response(None, _PARSE_ERROR, f"JSON 파싱 실패: {exc}"))
                continue
            if isinstance(msg, list):  # JSON-RPC 배치
                for item in msg:
                    resp = self._safe_handle(item)
                    if resp is not None:
                        self._write(resp)
                continue
            if not isinstance(msg, dict):
                self._write(_error_response(None, _INVALID_REQUEST, "요청은 객체여야 함"))
                continue
            resp = self._safe_handle(msg)
            if resp is not None:
                self._write(resp)
        self.log.info("stdin EOF — MCP 서버 종료.")

    def _safe_handle(self, msg: Any) -> Optional[dict]:
        if not isinstance(msg, dict):
            return _error_response(None, _INVALID_REQUEST, "요청은 객체여야 함")
        return self.handle_request(msg)

    @staticmethod
    def _write(resp: dict) -> None:
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------- #
# JSON-RPC 오류 헬퍼
# --------------------------------------------------------------------------- #
def _error_response(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


# --------------------------------------------------------------------------- #
# 엔트리포인트
# --------------------------------------------------------------------------- #
def main() -> None:
    """부팅 시 신선도 불변식 검증(korea100 승계) 후 stdio 루프 진입.

    - DB 미초기화(schema_meta 부재) 시 명확한 예외.
    - 신선도 만료(stale)는 부팅 실패가 아니라 stderr 경고 + 응답 봉투 stale 표기로
      강등(우리 서버는 판단성 액션을 수행하지 않는 읽기전용 자문 서버이므로,
      korea100 의 '경로 선택 중단' 대신 '결과에 stale 플래그' 보수 표기를 채택).
    """
    cfg = _config.get_config()
    log = util.get_logger("policymap.mcp", getattr(cfg, "log_level", "INFO"))
    try:
        conn = db.connect()
    except Exception as exc:
        log.error("DB 연결 실패: %s", exc)
        raise

    # 부팅 불변식: 스키마 존재 확인
    meta = db.fetchone(conn, "SELECT value FROM schema_meta WHERE key='schema_version'")
    if meta is None:
        raise RuntimeError(
            "DB 스키마 미초기화(schema_meta 없음). 먼저 'python -m policymap.run init' 실행 필요."
        )
    log.info("schema_version=%s", meta.get("value"))

    server = Server(conn, cfg)
    fresh = server._freshness()
    if fresh["stale"]:
        log.warning(
            "데이터 신선도 만료(as_of=%s, age=%s일 > %s일). 판단성 응답은 stale 표기로 강등.",
            fresh.get("as_of_date"), fresh.get("age_days"), fresh.get("max_age_days"),
        )
    server.serve_stdio()


if __name__ == "__main__":
    main()
