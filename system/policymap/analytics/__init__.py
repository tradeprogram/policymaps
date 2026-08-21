"""policymap.analytics — 정책확산 계량·공간분석 패키지.

모듈
  base       공용 데이터 계층(위험집합 모집단, 공간가중행렬 W, 공변량, 조례명 TF-IDF,
             템플릿별 채택연도)
  eha        이산시간 사건사분석(위험집합 패널 + logit/cloglog IRLS + 클러스터 로버스트 SE)
  spatial    전역 Moran's I + LISA(조건부 순열검정 + BH-FDR)
  diffusion  로지스틱 성장모형 적합·확산속도·혁신자·수평/수직 경로 분해
  peers      행안부 유사자치단체 기준 정렬 peer group + 방법 비교표 + 특성 물화

모두 SQLite 읽기전용으로 동작한다(peers.materialize_region_features 만 쓰기).
numpy 필요(eha/spatial/diffusion), base/peers 는 표준라이브러리만으로 동작.
"""
from __future__ import annotations

__all__ = ["base", "eha", "spatial", "diffusion", "peers"]
