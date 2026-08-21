"""policymap.collectors — 외부 공식 API 수집 계층.

빌드 에이전트가 구현할 모듈(CONTRACTS.md §collectors 계약 준수):
  law.py     : 국가법령정보 Open API (law/eflaw/ordin/admrul/lsStmd/lsDelegated/lnkLsOrd*)
  assembly.py: 열린국회정보 (발의/의안별표결/의원별표결/의원명부)
  geo.py     : V-World 경계 + 행정표준코드(StanReginCd) + 코드 크로스워크
  budget.py  : 지방재정365 QWGJK 세부사업별 세출현황

공통 규약:
  * 모든 collector 함수는 (cfg, http, conn) 또는 그 부분집합을 인자로 받는다.
  * HTTP는 util.HttpClient, 저장은 db.upsert/upsert_many + db.advance_cursor.
  * NOT_FOUND/빈결과는 util.NotFound 로 잡아 "정상적 없음" 처리(재시도·오류예산 제외).
  * 인증실패/파라미터오류 응답 신호는 각 모듈이 해석해 PermanentError 로 변환.
  * 원문 미러링 금지: 조문 텍스트는 저장하되 별표/첨부는 URL만 보존.
"""

__all__ = []
