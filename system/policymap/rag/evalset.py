"""policymap.rag.evalset — 검색 품질 회귀 평가셋과 측정기.

왜 코드로 고정하는가:
  직전 라운드의 교훈은 "측정하지 않으면 char n-gram TF 를 dense 라 부르며 계속 간다"는
  것이었다. 실제로 meta.json 의 dense_kind=sparse / dense_dim=0 을 아무도 보지 않은 채
  '하이브리드 검색'이 수개월 운영되었다. 그래서 평가셋을 문서가 아니라 **코드**에 두고,
  릴리스 게이트(pytest)로 쓴다.

평가 설계:
  · 질의는 실무자가 실제로 칠 법한 자연어이고, 공식 조례명과 어휘를 **일부러 어긋나게**
    썼다("밥 주는 곳"↔"급식소", "몰래 낳는"↔"보호출산"). 어휘 매칭만으로 풀리면
    의미 검색의 기여를 측정할 수 없기 때문이다.
  · 정답은 ordinances.name 에 대한 정규식으로 근사한다. 정답 문서 = 그 조례의 모든 조문.
  · 지표는 P@1 / P@5 / Success@5. **MRR 은 쓰지 않는다** — 직전 라운드 실측에서 4개 방식
    전부 1.000 으로 포화되어 변별력이 0 이었다.

한계(반드시 같이 읽을 것):
  조례명 정규식 근사이므로, 이름에 안 드러나지만 내용상 맞는 조문은 오답 처리되고
  (과소평가), 이름만 맞고 내용이 다른 조례는 정답 처리된다(과대평가).
  36건은 표본이 작아 1건이 P@1 을 0.028 움직인다 — **소수점 셋째 자리는 신뢰하지 말 것.**
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

# 직전 라운드(문서15 §5.5) 28질의 — 회귀 비교를 위해 한 글자도 바꾸지 않는다.
EVAL28: tuple[tuple[str, str], ...] = (
    ("길고양이 밥 주는 곳 설치 근거",          r"길고양이|고양이"),
    ("개 물림 사고 막으려면 무슨 규정이 있나",   r"동물보호|반려동물.*(학대|보호)|맹견"),
    ("유기견 안락사 관련 규정",               r"동물보호|유기동물|동물.*관리"),
    ("멧돼지가 농작물 망쳤을 때 보상",         r"야생동물|유해야생"),
    ("아기 낳으면 주는 지원금",               r"출산|출생|산모|모자보건"),
    ("장애인 부모가 출산할 때 받는 돈",        r"장애인가정\s*출산"),
    ("탄소 배출 줄이기 위한 지자체 계획",       r"탄소중립|기후위기|녹색성장|온실가스"),
    ("동네 가게 사장님 대출 도와주는 제도",     r"소상공인|중소기업.*육성|전통시장"),
    ("차 댈 곳 만들고 관리하는 규정",          r"주차장"),
    ("건물 지을 때 지켜야 하는 지자체 규정",    r"건축"),
    ("공무원 출장비 얼마 주나",               r"여비"),
    ("시청 직원 몇 명까지 뽑을 수 있나",       r"정원"),
    ("남녀 차별 없애기 위한 기본 규정",        r"양성평등|성평등|여성"),
    ("재난 났을 때 대응 조직",               r"재난|안전대책본부|통합방위|민방위"),
    ("나라 땅이나 건물 빌려쓰는 규정",         r"공유재산|국유재산"),
    ("반려견이랑 같이 여행 가는 거 지원",      r"반려동물.*동반|반려견.*순찰|반려동물산업"),
    ("아이 키우기 힘든 임산부 몰래 낳는 제도",  r"보호출산|위기\s*임신"),
    ("세금 제대로 냈는지 조사하는 절차",       r"세무조사|지방세"),
    ("결재를 누가 대신 하는지 정한 규정",       r"전결|사무위임"),
    ("돈 모아두는 기금 운용 규정",            r"기금"),
    ("상 주는 규정",                        r"포상|표창"),
    ("공무원 인사 관련 세부 규칙",            r"인사\s*규칙|임용"),
    ("도서관 짓고 운영하는 규정",             r"도서관"),
    ("어르신 복지 지원",                     r"노인|고령|경로"),
    ("청년들 정착 지원",                     r"청년"),
    ("학교 밥값 지원",                       r"급식"),
    ("상수도 요금 규정",                     r"수도|급수|상수도"),
    ("주민이 예산 짜는 데 참여",              r"주민참여예산|주민참여"),
)

# 이번 라운드 신규 8질의. 직전 라운드가 놓친 영역(재해예방·인프라·정주여건)을 채우고,
# 문서15 가 dense 실패 사례로 든 '산사태'·'공공 와이파이'를 회귀 항목으로 고정한다.
NEW8: tuple[tuple[str, str], ...] = (
    ("산사태 나기 전에 미리 손보는 근거",      r"산사태|사면|급경사|재해예방|풍수해"),
    ("동네에 공짜 인터넷 깔아주는 사업",       r"와이파이|정보화|통신|스마트도시|디지털"),
    ("빈집 오래 방치된 거 정비하는 규정",      r"빈집|폐가|도시재생|주거환경"),
    ("농사짓는 분들께 매년 주는 수당",         r"농민수당|농업인수당|기본소득|공익수당|농어업인"),
    ("탈시설 장애인 자립 도와주는 제도",       r"장애인.*자립|자립생활|탈시설"),
    ("담배 못 피우게 정한 구역",              r"금연|간접흡연|흡연"),
    ("전기차 충전기 설치 지원",               r"전기자동차|친환경자동차|충전|수소차"),
    ("혼자 사는 어르신 안부 확인 서비스",      r"고독사|독거|1인\s*가구|돌봄"),
)

EVAL36: tuple[tuple[str, str], ...] = EVAL28 + NEW8


def _name_of(hit: dict) -> str:
    return str(hit.get("parent_name") or hit.get("name") or "")


def measure(conn, run: Callable[[Any, str, int], list[dict]], *,
            evalset: tuple[tuple[str, str], ...] = EVAL36,
            k: int = 5, keep_detail: bool = False) -> dict:
    """검색 함수 하나를 평가셋으로 채점한다.

    run(conn, query, k) 는 조례 단위 히트 리스트를 반환해야 한다
    (parent_name 또는 name 키를 가진 dict). 예:
        measure(conn, lambda c,q,k: retrieve.search(c,q,k))
        measure(conn, lambda c,q,k: retrieve.bm25_search(c,q,k,group_by='parent'))

    반환 {'n','P@1','P@5','Success@5','sec_per_query'[,'detail']}.
    """
    n = len(evalset)
    if not n:
        raise ValueError("빈 평가셋")
    p1 = p5 = s5 = 0.0
    detail: list[dict] = []
    t0 = time.time()
    for query, pattern in evalset:
        rx = re.compile(pattern)
        names = [_name_of(h) for h in run(conn, query, k)[:k]]
        good = [bool(rx.search(nm)) for nm in names]
        p1 += 1.0 if (good and good[0]) else 0.0
        p5 += sum(good) / float(k)
        s5 += 1.0 if any(good) else 0.0
        if keep_detail:
            detail.append({"query": query, "hit": good, "names": names})
    out = {
        "n": n,
        "P@1": round(p1 / n, 4),
        "P@5": round(p5 / n, 4),
        "Success@5": round(s5 / n, 4),
        "sec_per_query": round((time.time() - t0) / n, 3),
    }
    if keep_detail:
        out["detail"] = detail
    return out


# 릴리스 게이트 하한(EVAL36 기준). 2026-08-20 실측값은
#   retrieve.search(candidates=30) + BAAI/bge-reranker-v2-m3, 62,460문서 인덱스에서
#   P@1 0.6667 / P@5 0.5611 / Success@5 0.8056 이었다.
# 게이트는 여기서 2~3질의(약 0.06~0.08)의 표본잡음 여유를 빼고 잡았다 —
# 회귀를 잡되 한 질의 흔들림으로 빌드가 깨지지 않게 하기 위함이다.
# 주의: 재랭커 모델이 없는 환경은 BM25 순서를 보존하므로(rerank.py 참조)
#       P@1 0.5278 수준이고 이 게이트를 통과하지 못한다 → 그 경우 테스트는 skip 할 것.
GATE = {"P@1": 0.58, "P@5": 0.48, "Success@5": 0.72}

# 참고 기준선(같은 조건에서 실측). 회귀 원인 분리에 쓴다 —
# 새 방식이 이 값 아래로 내려가면 재랭킹이 아니라 후보 생성이 깨진 것이다.
BASELINE_BM25 = {"P@1": 0.5278, "P@5": 0.4333, "Success@5": 0.6111}

__all__ = ["EVAL28", "NEW8", "EVAL36", "measure", "GATE", "BASELINE_BM25"]
