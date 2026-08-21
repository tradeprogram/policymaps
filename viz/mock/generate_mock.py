#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""policymap 정적 export 번들의 **가상(mock) 데이터 생성기**.

목적
----
실데이터(F:/policy_maps/system/data, DB 4.5GB · graph/nodes.json 136MB)를 붙이지 않고
프론트엔드 시각화를 개발하기 위한 경량 목업을 만든다.
**필드 구조는 실제 export 스키마와 동일**하므로, 개발이 끝나면 출력 디렉터리를
실데이터 번들로 바꿔 끼우기만 하면 그대로 동작한다.

스키마 출처(실측, 2026-08-21)
-----------------------------
  manifest.json           F:/policy_maps/system/data/manifest.json
  regions/index.json      F:/policy_maps/system/data/regions/index.json
  regions/{sig_cd}.json   F:/policy_maps/system/data/regions/11110.json, 11000.json
  graph/nodes.json        F:/policy_maps/system/data/graph/nodes.json  (label별 필드 실측)
  graph/edges.json        F:/policy_maps/system/data/graph/edges.json  (relation별 필드 실측)
  changes/latest.json     F:/policy_maps/system/data/changes/latest.json
  meta/graph-stats.json   F:/policy_maps/system/data/meta/graph-stats.json
  state/watermarks.json   F:/policy_maps/system/data/state/watermarks.json
  api/*.json (MCP 응답)   F:/policy_maps/system/policymap/mcp_server/server.py
                          F:/policy_maps/system/policymap/analytics/{peers,diffusion}.py
                          F:/policy_maps/system/policymap/rag/{retrieve,index}.py

가상임을 숨기지 않는다
----------------------
모든 산출 JSON 최상위에 `"_mock": true` 와 `"_mock_warning"` 을 넣는다.
실데이터 번들에는 이 키가 없으므로, 프론트는 `data._mock` 하나로 목업 여부를 판별하고
화면에 "가상 데이터" 배지를 띄울 수 있다.

실행
----
  python viz/mock/generate_mock.py                 # → viz/public/data/
  python viz/mock/generate_mock.py --out <DIR>
  python viz/mock/generate_mock.py --seed 42
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------- #
# 0) 상수 — 가상 표식 / 기준일
# --------------------------------------------------------------------------- #
MOCK_FOCUS_SIG = "11110"
SCHEMA = "policymap.static.v1"
MOCK_WARNING = (
    "가상(mock) 데이터입니다. 실제 자치법규·법령·예산·표결 값이 아니며 "
    "어떤 판단 근거로도 사용할 수 없습니다. 시각화 개발 전용."
)
DISCLAIMER = (
    "이 응답은 의사결정 지원을 위한 참고 정보이며 법률 판단·유권해석이 아닙니다. "
    "근거 조문과 law.go.kr 원문을 직접 확인하십시오."
)
AS_OF_DATE = "2026-08-21"          # 실번들과 동일 기준일
GENERATED_AT = "2026-08-21T09:00:00+0900"
MAX_AGE_DAYS = 30
KST = "+0900"

# 실제 카테고리 코드(policymap.db categories 테이블 실측)
CATEGORIES = [
    ("C01", "행정·자치·의회"), ("C02", "재정·세무·회계"), ("C03", "복지·돌봄"),
    ("C04", "인구·출산·양육"), ("C05", "청년·교육"), ("C06", "보건·의료"),
    ("C07", "환경·기후"), ("C08", "안전·재난"), ("C09", "도시·건축·주택"),
    ("C10", "교통"), ("C11", "경제·산업·일자리"), ("C12", "농림·수산"),
    ("C13", "문화·체육·관광"), ("C14", "동물·반려"),
    ("C-BIRTH", "출산장려ㆍ양육지원"), ("C-PET", "반려동물ㆍ동물보호"),
]
CAT_CODES = [c for c, _ in CATEGORIES]

# 실제 시군구(법정동 sig_cd) — 5개 시도 + 20개 시군구
# region_id 규칙(실측): level=1 은 시도 2자리, level>=2 는 sig_cd 5자리
# population / area_km2 는 코로플레스가 엉뚱해 보이지 않도록 실제 규모에 맞춘 근사치다
# (가상 데이터이므로 정확한 통계값이 아니다 — 배분·집계값은 모두 난수 생성).
REGIONS_SEED = [
    # (sig_cd, name, level, rtype, sido_cd, parent_sig_cd, population, area_km2)
    ("11000", "서울특별시",   1, "특별·광역시", "11", None,      None,     605.2),
    ("11110", "종로구",       2, "자치구",     "11", "11000",  138_000,   23.91),
    ("11140", "중구",         2, "자치구",     "11", "11000",  118_000,    9.96),
    ("11170", "용산구",       2, "자치구",     "11", "11000",  210_000,   21.87),
    ("11200", "성동구",       2, "자치구",     "11", "11000",  275_000,   16.86),
    ("11215", "광진구",       2, "자치구",     "11", "11000",  334_000,   17.06),
    ("11230", "동대문구",     2, "자치구",     "11", "11000",  335_000,   14.20),
    ("26000", "부산광역시",   1, "특별·광역시", "26", None,      None,     771.3),
    ("26110", "중구",         2, "자치구",     "26", "26000",   39_000,    2.83),
    ("26140", "서구",         2, "자치구",     "26", "26000",  105_000,   13.93),
    ("26170", "동구",         2, "자치구",     "26", "26000",   85_000,    9.78),
    ("26200", "영도구",       2, "자치구",     "26", "26000",  104_000,   14.20),
    ("41000", "경기도",       1, "도",         "41", None,      None,   10199.0),
    ("41110", "수원시",       2, "시",         "41", "41000", 1_190_000, 121.10),
    ("41130", "성남시",       2, "시",         "41", "41000",  915_000,  141.80),
    ("41150", "의정부시",     2, "시",         "41", "41000",  464_000,   81.54),
    ("41170", "안양시",       2, "시",         "41", "41000",  542_000,   58.46),
    ("41190", "부천시",       2, "시",         "41", "41000",  770_000,   53.44),
    ("47000", "경상북도",     1, "도",         "47", None,      None,   19034.0),
    ("47110", "포항시",       2, "시",         "47", "47000",  490_000, 1130.00),
    ("47130", "경주시",       2, "시",         "47", "47000",  246_000, 1324.40),
    ("47170", "안동시",       2, "시",         "47", "47000",  154_000, 1521.30),
    ("48000", "경상남도",     1, "도",         "48", None,      None,   10540.0),
    ("48120", "창원시",       2, "시",         "48", "48000", 1_010_000, 748.10),
    ("48170", "진주시",       2, "시",         "48", "48000",  342_000,  712.60),
]
SIDO_FULLNAME = {"11": "서울특별시", "26": "부산광역시", "41": "경기도",
                 "47": "경상북도", "48": "경상남도"}

# 그래프에만 존재하는 지역(폐지/승계 이력용).
# 실번들도 graph Region 556 vs regions/*.json 284 로, 그래프가 이력 지역을 더 갖는다.
# 실제 개편 사건: 강원도(42) → 강원특별자치도(51), 2023-06-11 (reference/reorg_events.json)
LEGACY_REGIONS = [
    {"region_id": "42", "sig_cd": "42000", "name": "강원도", "full_name": "강원도",
     "level": 1, "status": "renamed", "valid_to": "2023-06-11"},
    {"region_id": "51", "sig_cd": "51000", "name": "강원특별자치도",
     "full_name": "강원특별자치도", "level": 1, "status": "active", "valid_to": None},
]

# 상위법 소관부처(LegalInstrument.competent_authority) — 법령명 키워드로 결정한다.
# (난수로 뽑으면 '지방공무원법 → 보건복지부' 같은 비현실적 조합이 나온다.)
AUTHORITY_RULES = [
    (("지방공무원", "지방자치", "지방재정", "지방세", "지방회계", "공유재산", "민원",
      "정보공개", "전자정부", "공직자윤리", "재난", "비영리민간단체", "기금관리", "계약"),
     "행정안전부"),
    (("복지", "기초생활", "한부모", "장애인", "아동", "의료", "식품위생", "보육",
      "사회복지"), "보건복지부"),
    (("건축", "국토", "도시", "주거환경", "산업입지", "감정평가", "주택", "빈집"),
     "국토교통부"),
    (("교육", "학교", "유아교육"), "교육부"),
    (("청소년", "양성평등", "다문화"), "여성가족부"),
    (("관광",), "문화체육관광부"),
    (("도로교통",), "경찰청"),
    (("산업집적", "공장설립"), "산업통상자원부"),
    (("공공기관의 운영",), "기획재정부"),
    (("민법",), "법무부"),
]
DEFAULT_AUTHORITY = "행정안전부"


def authority_of(law_name: str) -> str:
    for keys, ministry in AUTHORITY_RULES:
        if any(k in law_name for k in keys):
            return ministry
    return DEFAULT_AUTHORITY

# 실제 상위법(자치법규 위임 상위 40개 실측) — instrument_id 는 실제 MST
STATUTES = [
    ("statute:286499", "지방공무원법", 1), ("statute:284005", "지방자치법", 1),
    ("statute:283145", "지방재정법", 1), ("statute:258477", "공유재산 및 물품 관리법", 1),
    ("statute:241935", "비영리민간단체 지원법", None), ("statute:283851", "재난 및 안전관리 기본법", 1),
    ("statute:276653", "국민기초생활 보장법", 1), ("statute:266687", "한부모가족지원법", 1),
    ("statute:281941", "장애인복지법", 1), ("statute:277149", "식품위생법", 1),
    ("statute:273437", "건축법", 1), ("statute:251019", "공공기관의 정보공개에 관한 법률", 1),
    ("statute:239293", "민원 처리에 관한 법률", 1), ("statute:282559", "지방세법", 1),
    ("statute:285713", "청소년 기본법", None), ("statute:281929", "아동복지법", 1),
    ("statute:285327", "의료법", 1), ("statute:270351", "개인정보 보호법", 1),
    ("statute:268103", "전자정부법", 1), ("statute:286057", "영유아보육법", None),
    ("statute:285437", "지방자치단체 기금관리기본법", 1), ("statute:284065", "도시 및 주거환경정비법", 1),
    ("statute:281959", "양성평등기본법", None), ("statute:281953", "다문화가족지원법", None),
    ("statute:270405", "사회복지사업법", None), ("statute:284013", "국토의 계획 및 이용에 관한 법률", 1),
    ("statute:279659", "관광진흥법", 1), ("statute:281875", "도로교통법", 1),
    ("statute:276363", "지방회계법", None), ("statute:268513", "고등교육법", 1),
    ("statute:286065", "초ㆍ중등교육법", None), ("statute:276057", "공공기관의 운영에 관한 법률", 1),
    ("statute:277139", "공직자윤리법", None), ("statute:277001", "산업입지 및 개발에 관한 법률", 1),
    ("statute:284085", "산업집적활성화 및 공장설립에 관한 법률", 1),
    ("statute:283431", "국가유공자 등 예우 및 지원에 관한 법률", None),
    ("statute:253973", "지방자치단체를 당사자로 하는 계약에 관한 법률", 1),
    ("statute:250715", "감정평가 및 감정평가사에 관한 법률", 1),
    ("statute:284415", "민법", 1), ("statute:284065b", "빈집 및 소규모주택 정비에 관한 특례법", 1),
]

# 조례 정책명(어미 없이) — 카테고리 코드와 짝지어 둔다
POLICY_TEMPLATES = [
    ("행정기구 및 정원", "C01"), ("공무원 후생복지", "C01"), ("의회 회의 운영", "C01"),
    ("주민참여예산제 운영", "C02"), ("지방보조금 관리", "C02"), ("기금 설치 및 운용", "C02"),
    ("저소득주민 생활안정 지원", "C03"), ("장애인 자립생활 지원", "C03"),
    ("노인 일자리 창출 지원", "C03"), ("한부모가족 지원", "C03"),
    ("출산장려 지원", "C-BIRTH"), ("난임부부 시술비 지원", "C04"),
    ("영유아 보육 지원", "C04"), ("다자녀 가정 지원", "C04"),
    ("청년 기본", "C05"), ("청년 월세 지원", "C05"), ("교육경비 보조", "C05"),
    ("평생학습 진흥", "C05"), ("감염병 예방 및 관리", "C06"), ("정신건강 증진", "C06"),
    ("치매 관리 지원", "C06"), ("탄소중립 녹색성장 기본", "C07"),
    ("자원순환 촉진", "C07"), ("미세먼지 저감 및 관리", "C07"),
    ("재난안전 기금 운용", "C08"), ("소규모 노후시설 안전점검", "C08"),
    ("공동주택 관리 지원", "C09"), ("빈집 정비 및 활용", "C09"),
    ("경관 관리", "C09"), ("대중교통 활성화 지원", "C10"),
    ("자전거 이용 활성화", "C10"), ("교통약자 이동편의 증진", "C10"),
    # 표기 흔들림(띄어쓰기) 쌍 — 실제 자치법규에도 흔하다. 격차분석의
    # likely_variant_of_mine / closest_own(변이 경고) 경로를 UI 에서 확인할 수 있게 둔다.
    ("자전거이용 활성화", "C10"),
    ("소상공인 육성 지원", "C11"), ("전통시장 활성화 지원", "C11"),
    ("사회적경제 육성 지원", "C11"), ("일자리 창출 촉진", "C11"),
    ("농업인 경영안정 지원", "C12"), ("로컬푸드 육성 지원", "C12"),
    ("생활체육 진흥", "C13"), ("작은도서관 설치 및 운영", "C13"),
    ("문화예술 진흥", "C13"), ("지역축제 육성 지원", "C13"),
    ("반려동물 보호 및 복지", "C-PET"), ("길고양이 급식소 설치 운영", "C14"),
    ("유기동물 입양 지원", "C14"),
]

# 폐지가 몰리는 정책 — 실데이터의 폐지는 정책별로 **군집**한다(상위법 개정에 따른 일괄 폐지).
# 실측 사례: '저탄소 녹색성장 기본 조례' 186곳이 탄소중립기본법 시행으로 일제히 폐지.
# 조례마다 독립적으로 20% 확률을 굴리면 거의 모든 정책에 폐지 peer 가 하나씩 생겨
# 격차분석의 '폐지 경고' 배지가 상시 점등돼 신호가 죽는다(전체 폐지율만 맞고 분포가 틀림).
REPEAL_HEAVY = {"탄소중립 녹색성장 기본", "자원순환 촉진", "지방보조금 관리",
                "평생학습 진흥", "지역축제 육성 지원", "일자리 창출 촉진"}
REPEAL_P_HEAVY, REPEAL_P_BASE = 0.62, 0.11   # 가중평균 ≈ 0.19 (실측 20.2%)

# (정당명, poly_cd, 의석수) — 의석 배분이 실제 제22대 국회 구도와 비슷해야
# 정당별 찬반 막대가 그럴듯하다. 균등 난수로 뽑으면 소수정당이 원내1당보다 커진다.
PARTIES = [("더불어민주당", "100001", 170), ("국민의힘", "100002", 108),
           ("조국혁신당", "101218", 12), ("개혁신당", "101219", 3),
           ("진보당", "101220", 3)]
PARTY_NAMES = [p[0] for p in PARTIES]
PARTY_SEATS = {p[0]: p[2] for p in PARTIES}
LEGISLATOR_SURNAMES = list("김이박최정강조윤장임한오서신권황안송류전홍고문양손배")
LEGISLATOR_GIVEN = ["민준", "서연", "도윤", "지우", "예준", "하윤", "시우", "지호",
                    "수아", "지훈", "채원", "건우", "다은", "현우", "유진"]
COMMITTEES = ["행정안전위원회", "보건복지위원회", "국토교통위원회", "교육위원회",
              "환경노동위원회", "기획재정위원회", "여성가족위원회", "농림축산식품해양수산위원회"]
DEPARTMENTS = ["기획예산과", "자치행정과", "복지정책과", "건강증진과", "환경과",
               "도시계획과", "교통행정과", "일자리경제과", "문화체육과", "안전총괄과"]
ORD_KINDS = ["조례", "규칙"]
RR_CLS = ["제정", "일부개정", "전부개정"]

LAW_URL = ("https://www.law.go.kr/DRF/lawService.do?OC=MOCK&target={t}"
           "&MST={mst}&type=HTML&mobileYn=")


# --------------------------------------------------------------------------- #
# 1) 유틸
# --------------------------------------------------------------------------- #
def mock_header(extra: dict | None = None) -> dict:
    """모든 산출물 공통 머리(가상 표식)."""
    h = {"_mock": True, "_mock_warning": MOCK_WARNING,
         "_mock_generator": "viz/mock/generate_mock.py"}
    if extra:
        h.update(extra)
    return h


def write_json(path: str, obj: dict) -> dict:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, indent=2)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)
    raw = txt.encode("utf-8")
    return {"bytes": len(raw), "hash": "sha256:" + hashlib.sha256(raw).hexdigest()}


def hexid(rng: random.Random) -> str:
    """change_id 형식(실측): 32자리 hex."""
    return "".join(rng.choice("0123456789abcdef") for _ in range(32))


def ts_kst(rng: random.Random, days_back: int = 10) -> str:
    base = datetime(2026, 8, 19, 9, 0, 0)
    d = base - timedelta(days=rng.randint(0, days_back),
                         hours=rng.randint(0, 8), minutes=rng.randint(0, 59),
                         seconds=rng.randint(0, 59))
    return d.strftime("%Y-%m-%dT%H:%M:%S") + KST


def ymd(rng: random.Random, y0: int, y1: int) -> str:
    y = rng.randint(y0, y1)
    m = rng.randint(1, 12)
    d = rng.randint(1, 28)
    return f"{y:04d}{m:02d}{d:02d}"


def rate(num, den):
    try:
        n, d = float(num or 0), float(den or 0)
    except (TypeError, ValueError):
        return None
    return round(n / d, 4) if d else None


# --------------------------------------------------------------------------- #
# 2) 지역 특성(peers 지표와 동일 축) — region_features 실측 컬럼과 동형
# --------------------------------------------------------------------------- #
def build_regions(rng: random.Random) -> list[dict]:
    out = []
    for sig_cd, name, level, rtype, sido_cd, parent, pop, area in REGIONS_SEED:
        region_id = sido_cd if level == 1 else sig_cd
        full_name = name if level == 1 else f"{SIDO_FULLNAME[sido_cd]} {name}"
        if level == 1:
            pop = None                       # 실측: 시도 population = null
            budget_total = rng.randint(20_000, 120_000) * 1_000_000_000
            ord_total = rng.randint(900, 1700)
        else:
            # 예산총액은 인구에 대략 비례하게(1인당 200~380만원) — 코로플레스가 그럴듯해진다
            budget_total = int(pop * rng.uniform(2.0e6, 3.8e6))
            ord_total = rng.randint(420, 900)
        fiscal = round(rng.uniform(0.18, 0.78), 6)
        welfare = round(rng.uniform(0.15, 0.45), 6)
        kinds_rule = int(ord_total * rng.uniform(0.18, 0.26))
        out.append({
            "sig_cd": sig_cd, "region_id": region_id, "name": name,
            "full_name": full_name, "level": level, "rtype": rtype,
            "sido_cd": sido_cd, "parent_sig_cd": parent, "status": "active",
            "population": pop, "area_km2": area,
            "pop_density": round(pop / area, 6) if pop else None,
            "budget_total": budget_total,
            "fiscal_self_ratio": fiscal, "welfare_ratio": welfare,
            "ordinance_total": ord_total,
            "ordinance_kinds": {"조례": ord_total - kinds_rule, "규칙": kinds_rule},
        })
    return out


def region_top_categories(rng: random.Random, total: int) -> list[dict]:
    """실측 형식: [{"code": "C01", "count": 190}, ...] count 내림차순."""
    codes = CAT_CODES[:14]                    # C01~C14 (실데이터 top_categories 도 C01~C13 관측)
    weights = [rng.random() ** 1.6 for _ in codes]
    s = sum(weights)
    covered = int(total * 0.94)               # 실측 카테고리 커버리지 94.1%
    rows = [{"code": c, "count": max(1, int(covered * w / s))}
            for c, w in zip(codes, weights)]
    rows.sort(key=lambda r: -r["count"])
    return [r for r in rows if r["count"] > 0]


# --------------------------------------------------------------------------- #
# 3) 조례 / 법령 / 의안 / 의원 엔티티
# --------------------------------------------------------------------------- #
def build_ordinances(rng: random.Random, regions: list[dict], n: int) -> list[dict]:
    """graph/nodes.json 의 Ordinance 노드 필드와 동일한 dict 를 만든다."""
    out = []
    mst = 2_000_000
    for i in range(n):
        reg = regions[i % len(regions)]
        pol, cat = POLICY_TEMPLATES[rng.randrange(len(POLICY_TEMPLATES))]
        kind = "조례" if rng.random() < 0.78 else "규칙"
        mst += rng.randint(37, 4200)
        oid = f"ordin:{mst}"
        enacted = ymd(rng, 2009, 2026)
        # 폐지 비중은 실측(40,406/199,858 ≈ 20.2%)에 맞추되 정책별로 군집시킨다.
        repealed = rng.random() < (REPEAL_P_HEAVY if pol in REPEAL_HEAVY else REPEAL_P_BASE)
        out.append({
            "id": f"ordinance:{oid}",
            "name": f"{reg['full_name']} {pol}에 관한 {kind}",
            "org_name": reg["full_name"],
            "ord_kind": kind,
            "local_tier": "L1" if reg["level"] == 2 else "L0",
            "delegation_type": rng.choice(["law-delegated", "autonomous", "mandatory"]),
            "department": rng.choice(DEPARTMENTS),
            "enacted_on": enacted,
            "effective_on": enacted,
            "rr_cls_cd": rng.choice(RR_CLS),
            "article_count": rng.randint(5, 42),
            "status": "repealed" if repealed else "active",
            # _clean_attrs 는 None 을 버린다 → 현행 조례엔 repealed_on 키가 아예 없다(실번들 동일)
            **({"repealed_on": ymd(rng, 2018, 2026)} if repealed else {}),
            "verification_status": "source-linked",
            "label": "Ordinance",
            "kind": "ordinance",
            "region_id": reg["region_id"],
            "src_id": oid,
            # 그래프 노드에는 없지만 목업 조립에 쓰는 보조 필드(노드 직렬화 시 제거)
            "_policy_key": pol,
            "_category": cat,
            "_sig_cd": reg["sig_cd"],
            "_official_url": LAW_URL.format(t="ordin", mst=mst),
        })
    return out


def inject_variant_case(rng: random.Random, corpus: list[dict], regions: list[dict],
                        base_sig: str, peer_ids: list[str]) -> list[dict]:
    """표기변이 격차 사례를 한 건 보장한다.

    기준 지자체는 '자전거 이용 활성화', peer 는 '자전거이용 활성화'(띄어쓰기만 다름)를
    갖게 해 peer_policy_gap 의 likely_variant_of_mine / closest_own 경로가 항상
    데이터에 존재하도록 만든다. 난수에 맡기면 시드마다 사라져 UI 의 '변이 의심' 배지를
    개발·검수할 수 없다. (실제로도 자치법규 제명의 띄어쓰기 흔들림은 흔하다.)
    """
    by_id = {r["region_id"]: r for r in regions}
    base = next((r for r in regions if r["sig_cd"] == base_sig), None)
    if base is None:
        return corpus
    plan = [(base["region_id"], "자전거 이용 활성화")]
    plan += [(rid, "자전거이용 활성화") for rid in peer_ids[:4] if rid in by_id]
    mst = 2_900_000
    for rid, pol in plan:
        reg = by_id[rid]
        mst += rng.randint(31, 900)
        oid = f"ordin:{mst}"
        enacted = ymd(rng, 2015, 2024)
        corpus.append({
            "id": f"ordinance:{oid}",
            "name": f"{reg['full_name']} {pol}에 관한 조례",
            "org_name": reg["full_name"], "ord_kind": "조례",
            "local_tier": "L1" if reg["level"] == 2 else "L0",
            "delegation_type": "law-delegated", "department": "교통행정과",
            "enacted_on": enacted, "effective_on": enacted,
            "rr_cls_cd": "제정", "article_count": rng.randint(8, 24),
            "status": "active", "verification_status": "source-linked",
            "label": "Ordinance", "kind": "ordinance",
            "region_id": rid, "src_id": oid,
            "_policy_key": pol, "_category": "C10", "_sig_cd": reg["sig_cd"],
            "_official_url": LAW_URL.format(t="ordin", mst=mst),
        })
    return corpus


def build_instruments(rng: random.Random, n: int) -> list[dict]:
    out = []
    for i in range(n):
        iid, nm, tier = STATUTES[i % len(STATUTES)]
        if i >= len(STATUTES):
            iid = f"{iid}#{i}"
        eff = ymd(rng, 2018, 2026)
        out.append({
            "id": f"instrument:{iid}",
            "name": nm,
            "source_type": "statute",
            "national_tier": tier if tier is not None else 1,
            "competent_authority": authority_of(nm),
            "status": "active",
            "current_history": "현행",
            "effective_on": eff,
            "verification_status": "source-linked",
            "label": "LegalInstrument",
            "kind": "instrument",
            "instrument_kind": "법률",
            "src_id": iid,
            "_official_url": LAW_URL.format(t="law", mst=iid.split(":")[-1].split("#")[0]),
        })
    return out


def build_legislators(rng: random.Random, n: int) -> list[dict]:
    out = []
    used = set()
    for i in range(n):
        while True:
            nm = rng.choice(LEGISLATOR_SURNAMES) + rng.choice(LEGISLATOR_GIVEN)
            if nm not in used:
                used.add(nm)
                break
        lid = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(8))
        # 앞의 len(PARTIES) 명은 정당당 1명씩 배정(고아 Party 노드 방지),
        # 나머지는 의석수에 비례해 뽑는다(원내1당이 실제로 가장 많아진다).
        if i < len(PARTY_NAMES):
            party = PARTY_NAMES[i]
        else:
            party = rng.choices(PARTY_NAMES,
                                weights=[PARTY_SEATS[p] for p in PARTY_NAMES])[0]
        prop = rng.random() < 0.18
        out.append({
            "id": f"legislator:{lid}",
            "name": nm,
            "district": "비례대표" if prop else f"{rng.choice(list(SIDO_FULLNAME.values()))} {rng.choice(['갑','을','병'])}",
            "elect_type": "비례대표" if prop else "지역구",
            "units": "제22대",
            "sex": rng.choice(["남", "여"]),
            "label": "Legislator",
            "kind": "legislator",
            "current_party": party,
            "src_id": lid,
        })
    return out


def build_bills(rng: random.Random, n: int) -> list[dict]:
    out = []
    themes = ["지방자치법", "지방재정법", "재난 및 안전관리 기본법", "아동복지법",
              "청년기본법", "탄소중립기본법", "동물보호법", "지방공무원법"]
    for i in range(n):
        bid = "PRC_" + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
                               for _ in range(32))
        theme = rng.choice(themes)
        yes = rng.randint(120, 260)
        no = rng.randint(0, 60)
        blank = rng.randint(0, 25)
        pd_ = date(2024, 6, 1) + timedelta(days=rng.randint(0, 780))
        result = rng.choice(["원안가결", "수정가결", "부결", "대안반영폐기"])
        # 실번들 Bill 노드 19,847건 중 표결집계(yes_tcnt 등)를 가진 것은 1,058건뿐이고
        # 14,227건은 proc_dt 조차 없다(계류). None 필드는 export 가 버리므로 노드가
        # 희소해진다 — 프론트가 '항상 있는 필드'로 착각하지 않도록 목업도 희소하게 만든다.
        pending = rng.random() < 0.35
        out.append({
            "id": f"bill:{bid}",
            "name": f"{theme} 일부개정법률안({'대안' if rng.random() < 0.4 else rng.choice(LEGISLATOR_SURNAMES) + '의원 대표발의'})",
            "bill_no": str(2_200_000 + rng.randint(1, 99_999)),
            "age": 22,
            "committee": rng.choice(COMMITTEES),
            "propose_dt": (pd_ - timedelta(days=rng.randint(30, 400))).isoformat(),
            "proc_dt": None if pending else pd_.isoformat(),
            "proc_result": None if pending else result,
            "proc_result_cd": None if pending else result,
            "yes_tcnt": None if pending else yes,
            "no_tcnt": None if pending else no,
            "blank_tcnt": None if pending else blank,
            "label": "Bill", "kind": "bill", "src_id": bid,
            "_pending": pending,
            "_member_tcnt": 300, "_vote_tcnt": None if pending else yes + no + blank,
        })
    return out


# --------------------------------------------------------------------------- #
# 4) 그래프 노드/엣지 (경량: 500 노드 / 1500 엣지)
# --------------------------------------------------------------------------- #
# graph/build.py::_clean_attrs 는 값이 None 인 속성을 버린다 → 필드가 희소(sparse)하다.
# 아래 목록은 '값이 있을 때 내보내는' 필드 전체 집합(실번들 union 과 동일).
NODE_FIELDS = {
    "Region": ["id", "name", "full_name", "level", "sig_cd", "has_legislation",
               "population", "status", "valid_to", "label", "kind", "src_id"],
    "Ordinance": ["id", "name", "org_name", "ord_kind", "local_tier", "delegation_type",
                  "department", "enacted_on", "effective_on", "repealed_on", "rr_cls_cd",
                  "article_count", "status", "verification_status", "label", "kind",
                  "region_id", "src_id"],
    "LegalInstrument": ["id", "name", "source_type", "national_tier",
                        "competent_authority", "status",
                        "current_history", "effective_on", "verification_status",
                        "label", "kind", "instrument_kind", "src_id"],
    "Bill": ["id", "name", "bill_no", "age", "committee", "propose_dt", "proc_dt",
             "proc_result", "proc_result_cd",
             "yes_tcnt", "no_tcnt", "blank_tcnt", "label", "kind", "src_id"],
    "Legislator": ["id", "name", "district", "elect_type", "units", "sex",
                   "label", "kind", "current_party", "src_id"],
    "Party": ["id", "name", "poly_cd", "label", "kind", "src_id"],
    "Category": ["id", "name", "level", "label", "kind", "src_id"],
}


def project(node: dict) -> dict:
    """실제 export 가 내보내는 필드만, 실제 순서대로 남긴다(보조 '_' 필드 제거).

    graph/build.py 와 동일하게 None 값은 내보내지 않는다.
    """
    fields = NODE_FIELDS[node["label"]]
    return {k: node[k] for k in fields if k in node and node[k] is not None}


def build_graph(rng, regions, ordinances, instruments, legislators, bills):
    # ---- 노드 -------------------------------------------------------------
    region_nodes = [project({
        "id": f"region:{r['region_id']}", "name": r["name"], "full_name": r["full_name"],
        "level": r["level"], "sig_cd": r["sig_cd"], "has_legislation": 1,
        "population": r["population"],
        "status": "active", "label": "Region", "kind": "region",
        "src_id": r["region_id"],
    }) for r in regions]
    # 이력 지역(폐지·승계) — regions/*.json 에는 없고 그래프에만 있다(실번들과 동형)
    region_nodes += [project({
        "id": f"region:{r['region_id']}", "name": r["name"], "full_name": r["full_name"],
        "level": r["level"], "sig_cd": r["sig_cd"], "has_legislation": 1,
        "population": None, "status": r["status"], "valid_to": r["valid_to"],
        "label": "Region", "kind": "region", "src_id": r["region_id"],
    }) for r in LEGACY_REGIONS]

    party_nodes = [{
        "id": f"party:{nm}", "name": nm, "poly_cd": cd,
        "label": "Party", "kind": "party", "src_id": nm,
    } for nm, cd, _seats in PARTIES]

    category_nodes = [{
        "id": f"category:{code}", "name": nm, "level": 1,
        "label": "Category", "kind": "category", "src_id": code,
    } for code, nm in CATEGORIES]

    nodes = (region_nodes + [project(o) for o in ordinances]
             + [project(i) for i in instruments] + [project(b) for b in bills]
             + [project(l) for l in legislators] + party_nodes + category_nodes)

    # ---- 엣지 -------------------------------------------------------------
    edges: list[dict] = []
    counts: dict[str, int] = {}

    def add(e):
        edges.append(e)
        counts[e["relation"]] = counts.get(e["relation"], 0) + 1

    # HAS_ORDINANCE (조례 수만큼)
    for o in ordinances:
        add({"source": f"region:{o['region_id']}", "target": o["id"],
             "relation": "HAS_ORDINANCE", "ord_kind": o["ord_kind"]})

    # CONTAINS (시도 → 시군구)
    by_sig = {r["sig_cd"]: r for r in regions}
    for r in regions:
        if r["parent_sig_cd"]:
            p = by_sig[r["parent_sig_cd"]]
            add({"source": f"region:{p['region_id']}", "target": f"region:{r['region_id']}",
                 "relation": "CONTAINS"})

    # ADJACENT_TO (같은 시도 내 시군구 쌍 + 시도 간 일부)
    l2 = [r for r in regions if r["level"] == 2]
    adj_pairs = set()
    for a in l2:
        for b in l2:
            if a is b:
                continue
            same = a["sido_cd"] == b["sido_cd"]
            if not same and rng.random() > 0.06:
                continue
            if same and rng.random() > 0.55:
                continue
            key = tuple(sorted((a["region_id"], b["region_id"])))
            if key in adj_pairs:
                continue
            adj_pairs.add(key)
    adj_pairs = sorted(adj_pairs)[:30]
    for a, b in adj_pairs:                        # 실데이터는 양방향으로 들어있다
        sa, sb = by_sig.get(a) or by_sig[a], by_sig.get(b) or by_sig[b]
        same = sa["sido_cd"] == sb["sido_cd"]
        add({"source": f"region:{a}", "target": f"region:{b}",
             "relation": "ADJACENT_TO", "contiguity_type": "queen",
             "same_province": 1 if same else 0})
        add({"source": f"region:{b}", "target": f"region:{a}",
             "relation": "ADJACENT_TO", "contiguity_type": "queen",
             "same_province": 1 if same else 0})

    # SUCCEEDED_BY — 실제 개편 사건(강원도 → 강원특별자치도, 2023-06-11)
    add({"source": "region:42", "target": "region:51", "relation": "SUCCEEDED_BY",
         "succession_type": "명칭변경", "effective_date": "2023-06-11"})

    # DELEGATED_FROM / CITES (조례 → 상위법)
    for o in ordinances:
        for _ in range(rng.randint(0, 2)):
            inst = rng.choice(instruments)
            add({"source": o["id"], "target": inst["id"], "relation": "DELEGATED_FROM",
                 "delegation_type": o["delegation_type"],
                 "source_path": rng.choice(["article-parse", "citation-backfill"]),
                 "inferred": rng.choice([0, 1])})
        if rng.random() < 0.45:
            inst = rng.choice(instruments)
            add({"source": o["id"], "target": inst["id"], "relation": "CITES",
                 "citation_type": rng.choice(["reference", "amendment", "delegation"])})

    # AMENDED_BY (조례 → 개정 조례) — 정책 생애주기(제정→개정→폐지) 시각화용. 속성 없음.
    for o in ordinances:
        if rng.random() < 0.12:
            t = rng.choice(ordinances)
            if t["id"] != o["id"]:
                add({"source": o["id"], "target": t["id"], "relation": "AMENDED_BY"})

    # IN_CATEGORY
    for o in ordinances:
        cats = {o["_category"]}
        while len(cats) < rng.randint(1, 2):
            cats.add(rng.choice(CAT_CODES))
        for c in sorted(cats):        # set 순회는 PYTHONHASHSEED 에 따라 순서가 변한다 → 정렬 필수
            add({"source": o["id"], "target": f"category:{c}", "relation": "IN_CATEGORY",
                 "confidence": round(rng.uniform(0.40, 0.78), 4), "method": "rule"})

    # SIMILAR_TO (조례 간)
    for o in ordinances:
        if rng.random() < 0.35:
            for k in range(1, rng.randint(2, 3)):
                t = rng.choice(ordinances)
                if t["id"] == o["id"]:
                    continue
                sim = round(rng.uniform(0.55, 0.93), 6)
                add({"source": o["id"], "target": t["id"], "relation": "SIMILAR_TO",
                     "rank": k, "model_name": "char-ngram-tf",
                     "weight": sim, "cosine_sim": sim})

    # MEMBER_OF / PROPOSED_BY / VOTED
    for l in legislators:
        add({"source": l["id"], "target": f"party:{l['current_party']}",
             "relation": "MEMBER_OF"})
    for b in bills:
        for l in rng.sample(legislators, rng.randint(1, 3)):
            add({"source": b["id"], "target": l["id"], "relation": "PROPOSED_BY",
                 "role": rng.choice(["RST", "CO"])})
        if b.get("_pending"):
            continue          # 계류 의안은 표결 자체가 없다
        for l in rng.sample(legislators, rng.randint(3, 8)):
            add({"source": l["id"], "target": b["id"], "relation": "VOTED",
                 "result_vote_mod": rng.choice(["찬성", "찬성", "찬성", "반대", "기권", "불참"]),
                 "party_at_vote": l["current_party"],
                 "vote_date": b["proc_dt"].replace("-", "") + " " +
                              f"{rng.randint(9,18):02d}{rng.randint(0,59):02d}{rng.randint(0,59):02d}"})

    return nodes, edges, counts


# graph/build.py 는 relation 원명이 아니라 **집계 라벨**로 edge_counts 를 낸다(실측).
#   DELEGATED_FROM/SUBORDINATE_TO  ← 위임계열 합
#   CITES(+relations)              ← CITES + AMENDED_BY 등 instrument_relations 계열 합
#   FUNDED_BY / ENACTS             ← 실번들도 0 이지만 키는 존재한다
EXPORT_EDGE_LABELS = [
    ("HAS_ORDINANCE", ["HAS_ORDINANCE"]),
    ("DELEGATED_FROM/SUBORDINATE_TO", ["DELEGATED_FROM", "SUBORDINATE_TO"]),
    ("CITES(+relations)", ["CITES", "AMENDED_BY"]),
    ("ADJACENT_TO", ["ADJACENT_TO"]),
    ("SUCCEEDED_BY", ["SUCCEEDED_BY"]),
    ("CONTAINS", ["CONTAINS"]),
    ("SIMILAR_TO", ["SIMILAR_TO"]),
    ("IN_CATEGORY", ["IN_CATEGORY"]),
    ("FUNDED_BY", ["FUNDED_BY"]),
    ("PROPOSED_BY", ["PROPOSED_BY"]),
    ("VOTED", ["VOTED"]),
    ("MEMBER_OF", ["MEMBER_OF"]),
    ("ENACTS", ["ENACTS"]),
]


def export_edge_counts(counts: dict[str, int]) -> dict[str, int]:
    """relation 별 원 카운트 → 실번들 graph_stats.edge_counts 표기."""
    return {label: sum(counts.get(r, 0) for r in rels)
            for label, rels in EXPORT_EDGE_LABELS}


# 구조 엣지 — 줄이면 노드가 고아가 되므로 다운샘플에서 제외한다.
PROTECTED_RELATIONS = {"HAS_ORDINANCE", "CONTAINS", "ADJACENT_TO",
                       "SUCCEEDED_BY", "MEMBER_OF"}


def downsample_edges(edges: list[dict], target: int) -> list[dict]:
    """엣지를 목표 개수로 줄인다.

    - 구조 엣지(PROTECTED_RELATIONS)는 전량 보존 → 고아 노드가 생기지 않는다.
    - 나머지는 관계별 비중을 보존해 균등 간격 샘플링하고, 각 관계 최소 1개는 남긴다.
      (단순 stride 샘플링은 리스트 뒤쪽 관계(PROPOSED_BY/VOTED)를 잘라먹는다.)
    """
    if len(edges) <= target:
        return edges
    protected = [e for e in edges if e["relation"] in PROTECTED_RELATIONS]
    rest = [e for e in edges if e["relation"] not in PROTECTED_RELATIONS]
    budget = max(0, target - len(protected))
    if budget <= 0:
        return protected[:target]

    groups: dict[str, list[dict]] = {}
    for e in rest:
        groups.setdefault(e["relation"], []).append(e)
    total = len(rest)
    target = budget
    quota = {r: max(1, int(round(len(g) * target / total))) for r, g in groups.items()}
    # 반올림 오차 보정: 가장 큰 그룹부터 ±1
    order = sorted(groups, key=lambda r: -len(groups[r]))
    while sum(quota.values()) > target:
        for r in order:
            if quota[r] > 1:
                quota[r] -= 1
                if sum(quota.values()) == target:
                    break
    while sum(quota.values()) < target:
        for r in order:
            if quota[r] < len(groups[r]):
                quota[r] += 1
                if sum(quota.values()) == target:
                    break
    out = list(protected)
    for r, g in groups.items():
        n = min(quota[r], len(g))
        step = max(1, len(g) // n)
        picked = g[::step][:n]
        if len(picked) < n:                    # 균등 간격으로 모자라면 앞에서 보충
            chosen = {id(e) for e in picked}
            picked.extend([e for e in g if id(e) not in chosen][:n - len(picked)])
        out.extend(picked)
    return out


def reconnect_orphans(nodes: list[dict], edges: list[dict],
                      dropped: list[dict]) -> list[dict]:
    """다운샘플로 고립된 노드에 버려진 엣지 하나씩을 되살린다(총 개수는 유지).

    맞바꿀 대상은 '양끝 모두 차수 2 이상' 인 엣지 중에서 고른다.
    """
    deg: dict[str, int] = {}
    for e in edges:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    orphans = {n["id"] for n in nodes if deg.get(n["id"], 0) == 0}
    if not orphans:
        return edges
    by_node: dict[str, list[dict]] = {}
    for e in dropped:
        by_node.setdefault(e["source"], []).append(e)
        by_node.setdefault(e["target"], []).append(e)

    edges = list(edges)
    for oid in sorted(orphans):
        cand = by_node.get(oid)
        if not cand:
            continue
        victim = None
        for i in range(len(edges) - 1, -1, -1):
            e = edges[i]
            if (e["relation"] not in PROTECTED_RELATIONS
                    and deg.get(e["source"], 0) > 1 and deg.get(e["target"], 0) > 1):
                victim = i
                break
        if victim is None:
            break
        v = edges.pop(victim)
        deg[v["source"]] -= 1
        deg[v["target"]] -= 1
        new = cand[0]
        edges.append(new)
        deg[new["source"]] = deg.get(new["source"], 0) + 1
        deg[new["target"]] = deg.get(new["target"], 0) + 1
    return edges


# --------------------------------------------------------------------------- #
# 5) 변경 이력
# --------------------------------------------------------------------------- #
CHANGE_EVENTS = ["created", "updated", "repealed", "budget_changed", "vote_recorded"]


def build_changes(rng, regions, ordinances, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        kind = rng.random()
        if kind < 0.55:
            o = rng.choice(ordinances)
            out.append({
                "change_id": hexid(rng), "ts": ts_kst(rng),
                "source": "ordinance", "scope": f"region:{o['region_id']}",
                "entity_type": "ordinance", "entity_id": o["src_id"],
                "entity_name": o["name"],
                "event": "repealed" if o["status"] == "repealed" else rng.choice(["created", "updated"]),
                "before": "", "after": "sha256:" + hashlib.sha256(o["id"].encode()).hexdigest(),
                "region_code": o["region_id"][:2],
                "official_url": o["_official_url"],
            })
        elif kind < 0.85:
            r = rng.choice(regions)
            laf = f"laf:{r['sido_cd']}{'0' * 5}"
            scope = f"{laf}|fyr:2025"
            out.append({
                "change_id": hexid(rng), "ts": ts_kst(rng),
                "source": "budget", "scope": scope,
                "entity_type": "budget", "entity_id": scope,
                "entity_name": f"{r['sido_cd']}00000 2025 세출({rng.randint(300, 6000)}건)",
                "event": "budget_changed", "before": "",
                "after": "sha256:" + hashlib.sha256(scope.encode()).hexdigest(),
                "region_code": r["sido_cd"], "official_url": None,
            })
        else:
            r = rng.choice(regions)
            out.append({
                "change_id": hexid(rng), "ts": ts_kst(rng),
                "source": "geo", "scope": f"sig:{r['sig_cd']}",
                "entity_type": "boundary", "entity_id": r["sig_cd"],
                "entity_name": r["name"], "event": "created", "before": "",
                "after": "sha256:" + hashlib.sha256(r["sig_cd"].encode()).hexdigest(),
                "region_code": r["sido_cd"], "official_url": None,
            })
    out.sort(key=lambda c: c["ts"], reverse=True)
    return out


def build_watermarks(rng, regions) -> list[dict]:
    out = []
    for r in regions:
        for src, scope, cursor in (
            ("budget", f"laf:{r['sido_cd']}00000|fyr:2025", "exe:20251231"),
            ("ordinance", f"region:{r['region_id']}", f"page:{rng.randint(1, 40)}"),
        ):
            out.append({
                "source": src, "scope": scope, "cursor": cursor,
                "last_run": ts_kst(rng), "last_success": ts_kst(rng),
                "last_hash": "sha256:" + hashlib.sha256((src + scope).encode()).hexdigest(),
                "status": "ok", "rows_seen": rng.randint(50, 6000),
                "changed": rng.randint(0, 4000), "retry_count": 0,
                "run_id": f"{src}-mock-2025", "note": None,
            })
    return out


# --------------------------------------------------------------------------- #
# 6) MCP tool 응답 목업 (api/*.json)
# --------------------------------------------------------------------------- #
def envelope(payload: dict, **extra) -> dict:
    """server.py::_envelope 와 동일한 봉투 + 가상 표식.

    extra 는 실데이터 fixture(make_gap_fixtures.py)가 최상위에 두는 키를 맞추기 위한 것.
    예: regions=[sig] — 이걸 빼면 DATA_BASE 를 실데이터로 바꿀 때 구조가 어긋난다.
    """
    env = mock_header()
    env.update({
        "data": payload,
        "as_of_date": AS_OF_DATE,
        "stale": False,
        "execution_allowed": False,
        "disclaimer": DISCLAIMER,
    })
    env.update(extra)
    return env


INDICATOR_KEYS = ["population", "area_km2", "fiscal_self_ratio",
                  "welfare_ratio", "budget_total"]
DEFAULT_WEIGHTS = {"population": 0.25, "area_km2": 0.13, "fiscal_self_ratio": 0.30,
                   "welfare_ratio": 0.07, "budget_total": 0.25}
WEIGHT_PROVENANCE = {"population": "ours", "area_km2": "ours", "budget_total": "ours",
                     "fiscal_self_ratio": "mois_public", "welfare_ratio": "mois_public"}
MISSING_MOIS_INDICATORS = ["인구증감률", "고령인구비율", "조출생률"]
PEERS_NOTE = "지표는 동일 유형 내 z-표준화 후 가중 유클리드. sim=1/(1+d)."


def indicators_of(r: dict) -> dict:
    return {k: r.get(k) for k in INDICATOR_KEYS}


def build_peers(rng, regions, base_sig="11110", k=10) -> dict:
    base = next(r for r in regions if r["sig_cd"] == base_sig)
    pool = [r for r in regions if r["level"] == 2 and r["sig_cd"] != base_sig]
    peers = []
    for r in pool:
        d = round(rng.uniform(0.15, 1.9), 6)
        peers.append({
            "region_id": r["region_id"], "sig_cd": r["sig_cd"], "name": r["full_name"],
            "rtype": r["rtype"],
            "similarity": round(1.0 / (1.0 + d), 6),
            "weighted_distance": d,
            "policy_profile_cosine": None,
            "indicator_gap_sd": {key: round(rng.uniform(0.02, 2.4), 4)
                                 for key in INDICATOR_KEYS},
            "indicators": indicators_of(r),
        })
    peers.sort(key=lambda p: -p["similarity"])
    return {
        "base_region": {"region_id": base["region_id"], "sig_cd": base["sig_cd"],
                        "name": base["full_name"]},
        "k": k,
        "peers": peers[:k],
        "method": {
            "partition_by_type": True,
            "candidate_pool": len(pool),
            "weights": DEFAULT_WEIGHTS,
            "weight_provenance": WEIGHT_PROVENANCE,
            "policy_profile_weight": 0.0,
            "min_indicator_coverage": 0.6,
            "missing_mois_indicators": MISSING_MOIS_INDICATORS,
            "note": PEERS_NOTE,
        },
        "target": {"region_id": base["region_id"], "sig_cd": base["sig_cd"],
                   "name": base["full_name"], "rtype": base["rtype"],
                   "indicators": indicators_of(base)},
        "_engine": "analytics.peers.find_similar_governments",
    }


def _dice(a: str, b: str) -> float:
    """analytics.peers.dice 와 같은 취지의 bigram Dice 계수(변이 판정용)."""
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not ga or not gb:
        return 1.0 if a == b else 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def build_gap(rng, regions, corpus, peers_payload, limit=25) -> dict:
    """analytics.peers.peer_policy_gap 반환형(격차분석 + 폐지경고).

    난수로 추천을 지어내지 않고 **조례 코퍼스에서 실제로 집계**한다. 그래야
    아래 두 규율이 데이터 수준에서 보장된다.
      - 선례(peers[])는 status='active' AND ord_kind='조례' 만 (폐지 조례를 추천하지 않는다)
      - 폐지 사례는 repealed_peers/caution 으로 **분리 표기**한다
    """
    base = peers_payload["target"]
    peer_rows = peers_payload["peers"]
    peer_ids = [p["region_id"] for p in peer_rows]
    peer_name = {p["region_id"]: p["name"] for p in peer_rows}
    tid = base["region_id"]

    def is_ord(o):                      # 규칙(ord_kind='규칙')은 조례 격차 대상이 아니다
        return o["ord_kind"] == "조례"

    mine = {o["_policy_key"] for o in corpus
            if o["region_id"] == tid and is_ord(o) and o["status"] == "active"}
    holders: dict[str, dict[str, dict]] = {}
    repealed: dict[str, dict[str, dict]] = {}
    for o in corpus:
        rid = o["region_id"]
        if rid == tid or rid not in peer_ids or not is_ord(o):
            continue
        bucket = holders if o["status"] == "active" else repealed
        bucket.setdefault(o["_policy_key"], {}).setdefault(rid, o)

    min_peers = 3
    dup_threshold = 0.8
    recs, exact_dup, variant = [], 0, 0
    for pol in sorted(holders):
        hold = holders[pol]
        if len(hold) < min_peers:
            continue
        if pol in mine:
            exact_dup += 1
            continue
        best_d, best_k = 0.0, None
        for mk in mine:
            d = _dice(pol, mk)
            if mk in pol or pol in mk:
                d = max(d, min(len(mk), len(pol)) / max(len(mk), len(pol)))
            if d > best_d:
                best_d, best_k = d, mk
        is_variant = best_d >= dup_threshold
        if is_variant:
            variant += 1
        rep = repealed.get(pol, {})
        recs.append({
            "policy_key": pol,
            "peer_count": len(hold),
            "peer_share": round(len(hold) / len(peer_ids), 4),
            "repealed_peer_count": len(rep),
            "repealed_peers": [{"region_id": rid, "name": peer_name.get(rid),
                                "repealed_on": o.get("repealed_on")}
                               for rid, o in sorted(
                                   rep.items(), key=lambda kv: str(kv[1].get("repealed_on") or ""))][:3],
            "caution": ("유사 지자체 중 폐지 사례 있음 — 상위법 개정 등으로 대체되었을 수 있으니 "
                        "제정 전 확인 필요") if rep else None,
            "likely_variant_of_mine": is_variant,
            "closest_own": ({"policy_key": best_k, "similarity": round(best_d, 3)}
                            if best_k else None),
            "peers": [{"region_id": rid, "name": peer_name.get(rid),
                       "ordinance_id": o["src_id"], "ordinance_name": o["name"],
                       "enacted_on": o["enacted_on"], "url": o["_official_url"]}
                      for rid, o in sorted(hold.items(),
                                           key=lambda kv: str(kv[1].get("enacted_on") or ""))][:5],
        })
    recs.sort(key=lambda x: (x["likely_variant_of_mine"], -x["peer_count"], x["policy_key"]))
    return {
        "target": base,
        "peers": [{"region_id": p["region_id"], "name": p["name"]} for p in peer_rows],
        "peer_pool_size": len(peer_ids),
        "my_policy_count": len(mine),
        "suppressed_exact_duplicate": exact_dup,
        "flagged_as_variant": variant,
        "recommendations": recs[:limit],
        "method": {
            **peers_payload["method"],
            "min_peers": min_peers, "same_sido_boost": 0.0,
            "dup_threshold": dup_threshold, "exclude_variants": False,
            "policy_key": ("조례명에서 지자체명·법형식 접미 제거 → canon_key(어절 단위 "
                           "연결어 제거) → 정규형 완전일치는 '보유'로 제외, 잔여 근사는 "
                           "closest_own 과 함께 likely_variant 로 표시"),
        },
        "_engine": "analytics.peers.peer_policy_gap",
    }


DIFFUSION_CAVEAT = (
    "확산 곡선(로지스틱 적합)은 방어 가능하나, '이웃을 보고 따라 했다'는 "
    "수평확산 해석은 우리 데이터에서 지지되지 않는다 — 이산시간 EHA 20개 "
    "사양에서 이웃노출 계수가 BH-FDR 통과 0개(중앙 OR 0.969)이고, 채택연도 "
    "공간군집은 시도 고정효과 제거 시 소멸한다(평균 I 0.053→-0.108). "
    "지배 요인은 광역·전국 공통충격(연도추세 중앙 OR 2.47)이다.")


def build_diffusion(rng, template="출산장려", y0=2009, y1=2026, universe=226) -> dict:
    span = list(range(y0, y1 + 1))
    K = int(universe * rng.uniform(0.62, 0.80))
    r_ = rng.uniform(0.35, 0.55)
    t0 = y0 + (y1 - y0) * 0.45
    curve, prev = [], 0
    for y in span:
        cum = int(K / (1 + math.exp(-r_ * (y - t0))))
        cum = max(cum, prev)
        curve.append({"year": y, "new": cum - prev, "cumulative": cum,
                      "adoption_rate": round(cum / universe, 4)})
        prev = cum
    total = prev

    innovators = []
    yr = y0
    for i in range(6):
        innovators.append({"region_id": f"4{rng.randint(1000, 8999)}",
                           "name": f"{rng.choice(list(SIDO_FULLNAME.values()))} "
                                   f"{rng.choice(['수원시','포항시','진주시','창원시','성남시'])}",
                           "rtype": "시", "year": yr})
        if rng.random() < 0.6:
            yr += 1

    rogers, prev_i = {}, 0
    for frac, label in [(0.025, "innovators"), (0.16, "early_adopters"),
                        (0.50, "early_majority"), (0.84, "late_majority"),
                        (1.00, "laggards")]:
        upto = int(round(frac * total))
        n = max(0, upto - prev_i)
        lo = span[min(len(span) - 1, int(prev_i / max(total, 1) * len(span)))]
        hi = span[min(len(span) - 1, int(upto / max(total, 1) * len(span)))]
        rogers[label] = {"n": n, "year_range": [lo, hi] if n else None}
        prev_i = upto
    never = universe - total
    rogers["never_adopted"] = {"n": never, "share_of_universe": round(never / universe, 4)}

    paths = {"neighbor_first": int(total * 0.41), "upper_first": int(total * 0.17),
             "both": int(total * 0.28), "neither": 0}
    paths["neither"] = total - sum(paths.values())
    obs = (paths["neighbor_first"] + paths["both"]) / total
    null_mean = round(obs + rng.uniform(0.01, 0.06), 4)

    detail = [{"region_id": i["region_id"], "name": i["name"], "year": i["year"],
               "n_prior_neighbors": rng.randint(0, 5), "n_neighbors": rng.randint(3, 9),
               "upper_adopted_first": rng.random() < 0.4,
               "path": rng.choice(list(paths))} for i in innovators]

    return {
        "template": template, "mode": "enactment", "level": 2,
        "universe": universe, "adopters": total,
        "final_adoption_rate": round(total / universe, 4),
        "window": [y0, y1],
        "curve": curve,
        "logistic": {
            "K_fixed_universe": {"K": float(universe), "r": round(r_ * 0.8, 4),
                                 "t0": round(t0 + 1.4, 2), "r2": round(rng.uniform(0.95, 0.99), 4),
                                 "rmse": round(rng.uniform(1.5, 5.0), 2),
                                 "t_10_90_years": round(math.log(81) / (r_ * 0.8), 1)},
            "K_free": {"K": float(K), "r": round(r_, 4), "t0": round(t0, 2),
                       "r2": round(rng.uniform(0.97, 0.999), 4),
                       "rmse": round(rng.uniform(0.8, 3.0), 2),
                       "t_10_90_years": round(math.log(81) / r_, 1)},
        },
        "innovators": innovators,
        "rogers_categories": rogers,
        "path_decomposition": {
            "counts": paths,
            "shares": {k: round(v / total, 4) for k, v in paths.items()},
            "definition": ("neighbor_first=채택 이전에 인접 지자체 중 채택자 존재, "
                           "upper_first=소속 광역이 먼저 채택, both=둘 다, neither=선행 신호 없음"),
        },
        "path_null_test": {
            "statistic": "prior_adopting_neighbor_share",
            "observed": round(obs, 4), "null_mean": null_mean,
            "null_sd": round(rng.uniform(0.02, 0.05), 4),
            "z": round(rng.uniform(-1.4, -0.2), 3),
            "p_sim": round(rng.uniform(0.55, 0.95), 5),
            "permutations": 999,
            "note": ("귀무: 채택연도를 지자체에 무작위 재배정(연도분포 보존). "
                     "observed <= null_mean 이면 '선행 이웃' 은 확산 증거가 아니라 "
                     "높은 채택률의 부산물이다."),
        },
        "path_detail": detail,
        "adoption_meta": {"mode": "enactment", "level": 2,
                          "filter": "rr_cls_cd='제정' AND ord_kind='조례'",
                          "matched_ordinances": total},
        "_engine": "analytics.diffusion.diffusion_profile",
        "interpretation_caveat": DIFFUSION_CAVEAT,
    }


EFF_VERIFICATION_NOTE = (
    "조례↔예산 링크는 도메인명사 교집합·분야게이트·부서가중 3채널 자동매칭 "
    "결과다(graph.analysis.link_ordinance_budget). verified=0 링크는 "
    "수작업 검증을 거치지 않았으므로 집행률은 참고치다.")
EFF_CAVEAT = (
    "집행률은 링크된 세부사업의 지출액/예산액 비율이며 조례의 정책효과가 아니다. "
    "회계연도 진행 중(당해년도) 스냅샷은 낮게 나오는 것이 정상이다.")


def build_effectiveness(rng, regions, ordinances, sig_cd="11110") -> dict:
    reg = next(r for r in regions if r["sig_cd"] == sig_cd)
    pool = [o for o in ordinances if o["region_id"] == reg["region_id"]] or ordinances[:12]
    by_ord, totals = [], {"alloc_amt": 0, "budget_now": 0, "exe_amt": 0}
    verified_links = auto_links = 0
    by_fyr = {}
    for o in pool[:18]:
        n_lines = rng.randint(1, 6)
        alloc = budget_now = exe = 0
        programs, methods = [], {}
        for _ in range(n_lines):
            fyr = rng.choice([2023, 2024, 2025])
            a = rng.randint(30, 4000) * 1_000_000
            bn = int(a * rng.uniform(0.95, 1.35))
            ex = int(bn * rng.uniform(0.35, 0.99))
            conf = round(rng.uniform(0.45, 0.97), 4)
            ver = 1 if rng.random() < 0.06 else 0
            mm = rng.choice(["noun-overlap", "field-gate", "dept-weighted"])
            methods[mm] = methods.get(mm, 0) + 1
            if ver:
                verified_links += 1
            else:
                auto_links += 1
            alloc += a; budget_now += bn; exe += ex
            bid = f"bud:{reg['sig_cd']}:{fyr}:{rng.randint(1000, 9999)}"
            programs.append({
                "budget_id": bid, "fyr": fyr,
                "dbiz_nm": f"{o['_policy_key']} 사업", "field": rng.choice(
                    ["사회복지", "일반공공행정", "환경", "문화및관광", "산업·중소기업및에너지"]),
                "alloc_amt": a, "budget_now": bn, "exe_amt": ex,
                "exec_rate": rate(ex, bn), "confidence": conf,
                "match_method": mm, "verified": bool(ver),
            })
            fy = by_fyr.setdefault(fyr, {"fyr": fyr, "lines": 0, "alloc_amt": 0,
                                         "budget_now": 0, "exe_amt": 0,
                                         "exe_ymd": f"{fyr}1231"})
            fy["lines"] += 1
            fy["alloc_amt"] += a; fy["budget_now"] += bn; fy["exe_amt"] += ex
        totals["alloc_amt"] += alloc
        totals["budget_now"] += budget_now
        totals["exe_amt"] += exe
        by_ord.append({
            "ordinance_id": o["src_id"], "lines": n_lines,
            "alloc_amt": alloc, "budget_now": budget_now, "exe_amt": exe,
            "verified_links": sum(1 for p in programs if p["verified"]),
            "auto_links": sum(1 for p in programs if not p["verified"]),
            "methods": methods, "programs": programs,
            "exec_rate_vs_now": rate(exe, budget_now),
            "exec_rate_vs_alloc": rate(exe, alloc),
            "name": o["name"], "region_id": o["region_id"],
            "official_url": o["_official_url"],
            "verification_status": o["verification_status"], "status": o["status"],
        })
    by_ord.sort(key=lambda o: -o["exe_amt"])
    for fy in by_fyr.values():
        fy["exec_rate_vs_now"] = rate(fy["exe_amt"], fy["budget_now"])
        fy["exec_rate_vs_alloc"] = rate(fy["exe_amt"], fy["alloc_amt"])

    return {
        "link_count": verified_links + auto_links,
        "budget_lines": sum(o["lines"] for o in by_ord),
        "fyr_filter": None, "min_confidence": 0.0,
        "totals": {**totals,
                   "exec_rate_vs_now": rate(totals["exe_amt"], totals["budget_now"]),
                   "exec_rate_vs_alloc": rate(totals["exe_amt"], totals["alloc_amt"])},
        "by_fiscal_year": sorted(by_fyr.values(), key=lambda f: str(f["fyr"])),
        "by_ordinance": by_ord,
        "verification": {
            "verified_links": verified_links, "auto_links": auto_links,
            "status": ("verified" if auto_links == 0 and verified_links > 0
                       else "partially-verified" if verified_links else "unverified"),
            "note": EFF_VERIFICATION_NOTE,
        },
        "caveat": EFF_CAVEAT,
        "_engine": "sql:ordinance_budget_link⋈budget_lines",
        "scope": {"mode": "region", "region_id": reg["region_id"], "sig_cd": reg["sig_cd"],
                  "name": reg["full_name"],
                  "linked_ordinances": len(by_ord)},
        "region_budget_baseline": {
            "fyr": 2025, "lines": rng.randint(1500, 9000),
            "alloc_amt": reg["budget_total"],
            "budget_now": int(reg["budget_total"] * 1.12),
            "exe_amt": int(reg["budget_total"] * 0.61),
            "exec_rate_vs_now": round(0.61 / 1.12, 4),
        },
    }


def build_votes(rng, bills, legislators) -> dict:
    """법안별 정당 찬반. 정당은 '당론'이 있으므로 의원별 난수가 아니라
    정당별 찬성 성향을 뽑고 그 안에서 이탈표를 준다(실제 표결 패턴)."""
    b = next((x for x in bills if not x.get("_pending")), bills[0])
    votes = {}
    totals = {"찬성": 0, "반대": 0, "기권": 0, "기타": 0, "합계": 0}
    for name, _cd, seats in PARTIES:
        stance = rng.random()                       # 당론 찬성 확률
        slot = {"party": name, "찬성": 0, "반대": 0, "기권": 0, "기타": 0, "합계": 0}
        present = int(seats * rng.uniform(0.82, 0.97))   # 불참 제외
        for _ in range(present):
            r = rng.random()
            if r < stance * 0.94:
                slot["찬성"] += 1
            elif r < stance * 0.94 + (1 - stance) * 0.85:
                slot["반대"] += 1
            elif rng.random() < 0.7:
                slot["기권"] += 1
            else:
                slot["기타"] += 1
        votes[name] = slot
    for slot in votes.values():
        slot["합계"] = slot["찬성"] + slot["반대"] + slot["기권"] + slot["기타"]
        for k in ("찬성", "반대", "기권", "기타", "합계"):
            totals[k] += slot[k]
    party_breakdown = sorted(votes.values(), key=lambda x: -x["합계"])
    for p in party_breakdown:
        p["찬성률"] = round(p["찬성"] / (p["합계"] or 1), 4)

    proposers = [{"role": "RST" if i == 0 else "CO",
                  "legislator_id": l["src_id"], "name": l["name"],
                  "current_party": l["current_party"], "district": l["district"]}
                 for i, l in enumerate(rng.sample(legislators, 6))]
    return {
        "bill": {"bill_id": b["src_id"], "bill_no": b["bill_no"], "age": b["age"],
                 "name": b["name"], "committee": b["committee"],
                 "propose_dt": b["propose_dt"], "proc_dt": b["proc_dt"],
                 "proc_result": b["proc_result"], "proc_result_cd": b["proc_result_cd"]},
        # 실제 tool 은 '의안별 표결현황 집계값'과 '의원별 표결 재집계'를 나란히 보여준다.
        # 목업에서도 둘을 정합하게 만든다(어긋나면 프론트가 버그로 오인한다).
        "tally_reported": {"member_tcnt": b["_member_tcnt"],
                           "vote_tcnt": totals["합계"],
                           "yes_tcnt": totals["찬성"], "no_tcnt": totals["반대"],
                           "blank_tcnt": totals["기권"] + totals["기타"]},
        "tally_from_votes": totals,
        "yes_ratio": round(totals["찬성"] / (totals["합계"] or 1), 4),
        "party_breakdown": party_breakdown,
        "proposers": proposers,
    }


def build_search(rng, ordinances, instruments, query="청년 월세 지원", k=10) -> dict:
    """RAG 검색 결과 목업. 질의와 무관한 조례가 상위에 뜨면 검색 UI 데모가 무의미하므로,
    질의 토큰이 조례명에 실제로 걸리는 것만 상위에 놓고 모자라면 무작위로 채운다."""
    results = []
    tokens = [t for t in query.split() if len(t) >= 2]
    def n_match(o):
        return sum(1 for t in tokens if t in o["name"] or t in o["_policy_key"])

    hit = [o for o in ordinances if n_match(o)]
    rng.shuffle(hit)
    hit.sort(key=n_match, reverse=True)      # 토큰을 많이 맞춘 조례가 상위
    pool = hit[:k]
    if len(pool) < k:
        chosen = {o["id"] for o in pool}
        pool += [o for o in rng.sample(ordinances, len(ordinances))
                 if o["id"] not in chosen][:k - len(pool)]
    for i, o in enumerate(pool, start=1):
        n_art = rng.randint(1, 4)
        # RRF 점수는 랭크가 내려갈수록 단조 감소해야 한다(관련도 순서 = 점수 순서).
        base = 1.0 / (60 + i)
        arts = []
        for j in range(n_art):
            arts.append({
                "doc_key": f"{o['src_id']}#a{j+1}",
                "article_no": f"제{j+1}조",
                "article_title": rng.choice(["목적", "정의", "적용범위", "지원대상",
                                             "지원내용", "위원회 구성", "재정지원"]),
                "score": round(base * (1.0 - 0.12 * j), 8),
            })
        arts.sort(key=lambda a: -a["score"])
        results.append({
            "doc_key": arts[0]["doc_key"],
            "doc_kind": "ordinance_article",
            "parent_id": o["src_id"],
            "parent_name": o["name"],
            "article_no": arts[0]["article_no"],
            "article_title": arts[0]["article_title"],
            "region_id": o["region_id"],
            "org_name": o["org_name"],
            "official_url": o["_official_url"],
            "content_hash": "sha256:" + hashlib.sha256(o["id"].encode()).hexdigest(),
            "doc_len": rng.randint(120, 1400),
            "bm25_score": round(rng.uniform(3.0, 18.0), 6),
            "bm25_rank": i,
            "dense_score": round(rng.uniform(0.30, 0.85), 6),
            "dense_rank": rng.randint(1, 30),
            "score": round(arts[0]["score"], 8),
            "method": "hybrid-rrf",
            "text": f"제{arts[0]['article_no']}({arts[0]['article_title']}) 이 조례는 "
                    f"{o['_policy_key']}에 필요한 사항을 규정함을 목적으로 한다. "
                    f"[가상 본문 — 실제 조문이 아님]",
            "matched_articles": arts,
            "rank": i,
            "article_hits": n_art,
            "node_type": "ordinance",
            "id": o["src_id"],
            "verification_status": o["verification_status"],
            "status": o["status"],
            "verified": True,
        })
    results.sort(key=lambda h: -h["score"])
    for i, h in enumerate(results, start=1):
        h["rank"] = i
    summary = {}
    for h in results:
        key = h["verification_status"] or "unknown"
        summary[key] = summary.get(key, 0) + 1
    verified = sum(v for kk, v in summary.items() if kk in ("verified", "source-linked"))
    return {
        "query": query, "k": k, "scope": "all", "hops": 1,
        "count": len(results), "results": results,
        "verification_summary": {"by_status": summary, "verified": verified,
                                 "unverified": len(results) - verified},
        "_engine": "rag.hybrid_graph_search(BM25+dense+graph)",
    }


# --------------------------------------------------------------------------- #
# 7) 메인
# --------------------------------------------------------------------------- #
def main() -> int:
    try:                                    # Windows 콘솔(cp949)에서도 한글 요약이 깨지지 않게
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    default_out = os.path.abspath(os.path.join(here, "..", "public", "data"))

    ap = argparse.ArgumentParser(description="policymap 가상 데이터 생성기")
    ap.add_argument("--out", default=default_out, help=f"출력 디렉터리(기본 {default_out})")
    ap.add_argument("--seed", type=int, default=20260821, help="난수 시드(재현성)")
    ap.add_argument("--nodes", type=int, default=500, help="그래프 노드 목표 수")
    ap.add_argument("--edges", type=int, default=1500, help="그래프 엣지 목표 수")
    ap.add_argument("--corpus", type=int, default=1000,
                    help="격차분석 집계용 조례 코퍼스 크기(그래프에는 이 중 일부만 싣는다)")
    args = ap.parse_args()

    out = args.out
    rng = random.Random(args.seed)
    files: list[dict] = []

    def emit(rel: str, obj: dict, rows: int):
        meta = write_json(os.path.join(out, rel.replace("/", os.sep)), obj)
        files.append({"file": rel, "bytes": meta["bytes"], "rows": rows,
                      "hash": meta["hash"]})

    # --- 엔티티 --------------------------------------------------------------
    regions = build_regions(rng)
    n_fixed = (len(regions) + len(LEGACY_REGIONS) + len(PARTIES) + len(CATEGORIES))
    n_inst, n_bill, n_leg = 80, 40, 30
    n_ord = max(1, args.nodes - n_fixed - n_inst - n_bill - n_leg)

    # 조례 코퍼스: 격차분석은 '유사 지자체 N곳이 가진 정책'을 세는 집계이므로
    # 경량 그래프(302건)만으로는 peer 보유 수가 min_peers 에 못 미쳐 추천이 비어버린다.
    # 실번들에서도 gap 은 DB 전량(199,858건)을 훑고 그래프는 그 표현일 뿐이다.
    # → 코퍼스를 따로 만들고, 그래프에는 그 부분집합만 싣는다.
    corpus = build_ordinances(rng, regions, max(args.corpus, n_ord))
    ordinances = sorted(rng.sample(corpus, n_ord), key=lambda o: o["src_id"])
    instruments = build_instruments(rng, n_inst)
    legislators = build_legislators(rng, n_leg)
    bills = build_bills(rng, n_bill)

    # --- 그래프 --------------------------------------------------------------
    nodes, edges, edge_counts = build_graph(
        rng, regions, ordinances, instruments, legislators, bills)
    all_edges = edges
    edges = downsample_edges(all_edges, args.edges)
    kept = {id(e) for e in edges}
    edges = reconnect_orphans(nodes, edges, [e for e in all_edges if id(e) not in kept])
    edge_counts = {}
    for e in edges:
        edge_counts[e["relation"]] = edge_counts.get(e["relation"], 0) + 1

    node_counts = {}
    for nd in nodes:
        node_counts[nd["label"]] = node_counts.get(nd["label"], 0) + 1
    node_counts.setdefault("BudgetLine", 0)    # 실번들도 BudgetLine=0

    # --- 지역 상세 ------------------------------------------------------------
    changes = build_changes(rng, regions, ordinances, 200)
    changes_by_region: dict[str, list[dict]] = {}
    for c in changes:
        changes_by_region.setdefault(str(c.get("region_code") or ""), []).append(c)

    index_items = []
    budget_lines_total = 0
    for r in regions:
        recent = [{"change_id": c["change_id"], "ts": c["ts"],
                   "entity_type": c["entity_type"], "entity_id": c["entity_id"],
                   "entity_name": c["entity_name"], "event": c["event"],
                   "official_url": c["official_url"]}
                  for c in changes_by_region.get(r["sido_cd"], [])[:6]]
        doc = mock_header()
        doc.update({
            "sig_cd": r["sig_cd"], "region_id": r["region_id"], "name": r["name"],
            "full_name": r["full_name"], "level": r["level"], "status": r["status"],
            "population": r["population"],
            "ordinance_kinds": r["ordinance_kinds"],
            "ordinance_total": r["ordinance_total"],
            "top_categories": region_top_categories(rng, r["ordinance_total"]),
            "budget": {"lines": rng.randint(800, 9000),
                       "exe_amt": int(r["budget_total"] * rng.uniform(0.55, 0.78)),
                       "budget_now": int(r["budget_total"] * rng.uniform(1.05, 1.25))},
            "recent_changes": recent,
            "as_of_date": AS_OF_DATE, "stale": False,
        })
        budget_lines_total += doc["budget"]["lines"]
        emit(f"regions/{r['sig_cd']}.json", doc, 1)
        index_items.append({"sig_cd": r["sig_cd"], "name": r["name"], "level": r["level"],
                            "ordinance_total": r["ordinance_total"],
                            "file": f"regions/{r['sig_cd']}.json"})

    idx = mock_header()
    idx.update({"as_of_date": AS_OF_DATE, "stale": False,
                "count": len(index_items), "items": index_items})
    emit("regions/index.json", idx, len(index_items))

    # --- 그래프 파일 ----------------------------------------------------------
    gnodes = mock_header()
    gnodes.update({"as_of_date": AS_OF_DATE, "stale": False,
                   "count": len(nodes), "nodes": nodes})
    emit("graph/nodes.json", gnodes, len(nodes))

    gedges = mock_header()
    gedges.update({"as_of_date": AS_OF_DATE, "stale": False,
                   "count": len(edges), "edges": edges})
    emit("graph/edges.json", gedges, len(edges))

    gstats = mock_header()
    gstats.update({
        "as_of_date": AS_OF_DATE, "backend": "networkx",
        "node_counts": node_counts,
        "edge_counts": export_edge_counts(edge_counts),
        "skipped_edges": {"DELEGATED_FROM": 0, "CITES": 0, "FUNDED_BY": 0},
        "total_nodes": len(nodes), "total_edges": len(edges),
    })
    emit("meta/graph-stats.json", gstats, 1)

    # --- 변경 이력 / 워터마크 -------------------------------------------------
    latest = mock_header()
    latest.update({"as_of_date": AS_OF_DATE, "stale": False,
                   "count": len(changes), "changes": changes})
    emit("changes/latest.json", latest, len(changes))

    # 월별 피드는 latest 와 컬럼이 다르다(실측): stale 없음, scope/before/after 없음.
    FEED_FIELDS = ("change_id", "ts", "source", "entity_type", "entity_id",
                   "entity_name", "event", "region_code", "official_url")
    month = AS_OF_DATE[:7]
    feed = mock_header()
    feed.update({"as_of_date": AS_OF_DATE, "month": month,
                 "count": len(changes),
                 "changes": [{k: c[k] for k in FEED_FIELDS} for c in changes]})
    emit(f"changes/feed-{month}.json", feed, len(changes))

    wms = build_watermarks(rng, regions)
    wdoc = mock_header()
    wdoc.update({"generatedAt": GENERATED_AT, "count": len(wms), "watermarks": wms})
    emit("state/watermarks.json", wdoc, len(wms))

    # --- 기능별 목업(MCP tool 응답 봉투) --------------------------------------
    peers_payload = build_peers(rng, regions)
    fixtures = {
        "peers": ("api/peers.json", peers_payload),
        "gap": ("api/gap.json", build_gap(
            rng, regions,
            inject_variant_case(rng, corpus, regions, "11110",
                                [p["region_id"] for p in peers_payload["peers"]]),
            peers_payload)),
        "diffusion": ("api/diffusion.json", build_diffusion(rng)),
        "effectiveness": ("api/effectiveness.json",
                          build_effectiveness(rng, regions, ordinances)),
        "votes": ("api/votes.json", build_votes(rng, bills, legislators)),
        "search": ("api/search.json", build_search(rng, ordinances, instruments)),
    }
    # [실데이터 정합] system/make_gap_fixtures.py 는 gap/peers 를 {sig_cd: 결과} 맵으로
    # 내보내고 최상위에 regions 목록을 둔다. 목업이 단일 지역 객체로 감싸면 DATA_BASE 를
    # 실데이터로 바꾸는 순간 프론트가 깨진다. 두 fixture 만 실데이터 형식에 맞춘다. [실측]
    MAPPED = {"gap", "peers"}
    fixture_map = {}
    for name, (rel, payload) in fixtures.items():
        if name in MAPPED:
            sig = (payload.get("target") or {}).get("sig_cd") or MOCK_FOCUS_SIG
            emit(rel, envelope({sig: payload}, regions=[sig]), 1)
        else:
            emit(rel, envelope(payload), 1)
        fixture_map[name] = rel

    # --- manifest -------------------------------------------------------------
    manifest = mock_header()
    manifest.update({
        "schema": SCHEMA,
        "generated_at": GENERATED_AT,
        "as_of_date": AS_OF_DATE,
        "stale": False,
        "stale_days": MAX_AGE_DAYS,
        "counts": {
            # 실번들 규칙: counts.regions == graph node_counts.Region (이력 지역 포함)
            "regions": node_counts.get("Region", len(regions)),
            "legal_instrument": len(instruments),
            "ordinances": len(ordinances),
            "delegations": edge_counts.get("DELEGATED_FROM", 0),
            "bills": len(bills),
            # 지역 shard 의 budget.lines 합과 일치시킨다. 0 으로 두면 전국 요약 카드가
            # '예산 0건' 인데 지역 카드는 수천 건이 되어 프론트가 버그로 오인한다.
            # (graph node_counts.BudgetLine 은 실번들과 동일하게 0 이다 — 예산은
            #  그래프 노드로 싣지 않고 지역/조례 집계로만 내보내기 때문이다.)
            "budget_lines": budget_lines_total,
            "change_log": len(changes),
            "graph_nodes": len(nodes),
            "graph_edges": len(edges),
        },
        "graph_stats": {
            "backend": "networkx",
            "node_counts": node_counts,
            "edge_counts": export_edge_counts(edge_counts),
            "skipped_edges": {"DELEGATED_FROM": 0, "CITES": 0, "FUNDED_BY": 0},
        },
        "watermarks": [{"source": w["source"], "scope": w["scope"], "cursor": w["cursor"],
                        "status": w["status"], "last_success": w["last_success"],
                        "changed": w["changed"]} for w in wms],
        "region_index": "regions/index.json",
        "changes_latest": "changes/latest.json",
        "changes_months": [{"month": month, "count": len(changes),
                            "file": f"changes/feed-{month}.json"}],
        "files": sorted(files, key=lambda f: f["file"]),
        # 실번들에는 없는 목업 전용 키 — 기능별 픽스처 위치를 프론트에 알린다.
        "_mock_fixtures": fixture_map,
    })
    write_json(os.path.join(out, "manifest.json"), manifest)

    # --- 요약 -----------------------------------------------------------------
    print(f"[mock] out       = {out}")
    print(f"[mock] seed      = {args.seed}")
    print(f"[mock] regions   = {len(regions)}  (level1={sum(1 for r in regions if r['level']==1)})")
    print(f"[mock] nodes     = {len(nodes)}  {node_counts}")
    print(f"[mock] edges     = {len(edges)}  {edge_counts}")
    print(f"[mock] changes   = {len(changes)}")
    print(f"[mock] files     = {len(files) + 1} (manifest 포함)")
    print(f"[mock] fixtures  = {', '.join(fixture_map.values())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
