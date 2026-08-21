"""policy_maps — 조례 그래프 분석 시스템 (policymap 패키지).

레이어:
  config   : 환경설정(os.environ + .env) 로드
  util     : HTTP 클라이언트(httpx/urllib 폴백)·재시도·해시·로깅
  db       : SQLite 연결·schema 적용·upsert·watermark·change_log
  collectors : 외부 API 수집(law/assembly/geo/budget)
  parsers  : 조문·위임·분류·임베딩 파싱
  graph    : networkx 그래프 빌드·분석·export
  mcp_server : 순수 파이썬 JSON-RPC stdio MCP 서버

코어는 표준라이브러리만으로 동작. 무거운 의존성은 전부 선택적 import + graceful fallback.
단일 진실원천: db/schema.sql, CONTRACTS.md
"""

__version__ = "0.1.0"
__all__ = ["config", "util", "db"]
