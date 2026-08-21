"""policymap.parsers — 원문·구조 파싱 및 파생 계층.

빌드 에이전트가 구현할 모듈(CONTRACTS.md §parsers 계약 준수):
  article.py   : 법령 조문단위(항/호/목) + 자치법규 조 단위 파싱 → articles/ordinance_articles
  delegation.py: 위임 4경로 합집합 + 낫표(「」) 인용 파싱 → delegations/instrument_relations
  category.py  : 조례 정책분야 분류(룰기반 1차 + 선택적 LLM) → ordinance_category
  embedding.py : 임베딩 생성(폴백=char n-gram TF 코사인, 선택적 sbert) → embeddings/similarity_edges

공통 규약:
  * 파서는 원천 JSON(dict) 또는 DB 레코드를 입력받아 정규화 dict(row)를 산출.
  * 낫표 인용 파싱은 korea100 article-citations.mjs 규칙 승계(제N조제M항제K호O목).
  * 배열/스칼라 혼재 필드는 util.as_list 로 방어.
  * 임베딩은 무거운 의존성 없이 동작(sentence-transformers 부재 시 순수파이썬 폴백).
"""

__all__ = []
