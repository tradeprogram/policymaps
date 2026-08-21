"""policymap.mcp_server — 순수 파이썬 JSON-RPC stdio MCP 서버.

빌드 에이전트가 구현할 모듈(CONTRACTS.md §mcp_server 계약 준수):
  server.py  : stdio JSON-RPC 루프. initialize / tools/list / tools/call / resources/* 처리.
               MCP 규약 호환하되 SDK 의존성 없이 표준라이브러리(sys.stdin/stdout + json)로 구현.

노출 tool(도메인 분석 전면화 — 명세[mcp_survey] §8~9, 검색은 기존 서버에 위임):
  find_peer_governments        : 재정·인구·구조 유사 지자체 클러스터
  compare_ordinance_coverage   : 동일 상위법 위임 대비 지자체별 제정/미제정 매트릭스
  trace_ordinance_diffusion    : rrClsCd 제정일 + 연혁 시계열 확산 추적
  get_delegation_gap           : 위임 있으나 조례 부재(격차)
  compute_spatial_autocorrelation : sig_cd 조인 Moran's I / LISA 핫스팟
  get_region_profile           : 지역 프로파일(조례/예산/인접/변경)
  read_graph_cypher (read-only): 저수준 질의(감춤; 안전 게이트)

리소스: ordinance-graph://status (신선도·안전정책),
        ordinance-graph://peers/{sig_cd}, ordinance-graph://changes?since=

안전 게이트(korea100 MCP 승계):
  * 부팅 시 데이터 신선도 불변식 검증(as_of_date 만료 시 판단성 tool 중단, 검색은 허용).
  * 모든 판단성 응답에 근거조문 + law.go.kr 직링크 + "의사결정 지원, 법률판단 아님" 고지.
  * execution_allowed:false 고정(읽기전용).
  * tool 수 6~12로 억제(chrisryugj 89→14→9 압축 교훈).
"""

__all__ = []
