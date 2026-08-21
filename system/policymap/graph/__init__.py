"""policymap.graph — 그래프 빌드·분석·export 계층.

빌드 에이전트가 구현할 모듈(CONTRACTS.md §graph 계약 준수):
  build.py   : SQLite → networkx 그래프(노드18/엣지18 스키마). 선택적 networkx 폴백.
  analysis.py: 유사지자체 클러스터링·조례격차·확산패턴·공간자기상관(Moran's I/LISA)·중심성
  export.py  : 정적 JSON 번들(manifest + 지역 shard, klocal 패턴) + GraphML/node·edge JSON

공통 규약:
  * networkx 있으면 사용, 없으면 인접리스트 dict 폴백(핵심 알고리즘은 순수파이썬).
  * shapely 있으면 Queen contiguity 정밀, 없으면 GeoJSON 링 좌표공유 폴백.
  * numpy/scikit-learn 있으면 클러스터링 가속, 없으면 순수파이썬 k-means 폴백.
  * export는 모든 응답에 as_of_date 동봉, 만료 시 stale:true(명세[realtime] §7).
  * 저수준 그래프는 read-only, 도메인 분석 결과만 전면화(명세[mcp_survey] §9).
"""

__all__ = []
