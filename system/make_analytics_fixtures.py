#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""실 DB → **공간통계·EHA·커뮤니티** 시각화 shard 생성.

기존 생성기(make_gap_fixtures / make_more_fixtures / make_nationwide)는 지역·확산·표결·
검색을 덮었지만, 화면 어디에도 붙지 않은 분석엔진이 셋 남아 있었다.

    policymap.analytics.spatial   moran()                  → 전역 Moran's I + LISA
    policymap.analytics.eha       estimate_diffusion_hazard() → 이산시간 위험모형
    policymap.rag.community       build_community_report() → GraphRAG 커뮤니티 요약

이 스크립트는 그 셋을 **그대로 호출해서** 정적 shard 로 굽는다. 통계 계산은 한 줄도
다시 구현하지 않는다(엔진 재구현 금지). 봉투·키살균·원자적 쓰기도 기존 생성기 것을 쓴다.

    api/spatial/{slug}.json     지표별 Moran's I 전역값 + LISA 국지값(sig_cd 별 → 코로플레스)
    api/eha/{slug}.json         확산 템플릿별 이산시간 위험모형(계수·SE·p·OR·위험집합)
    api/community/summary.json  커뮤니티 탐지 요약(대표조례·지자체·카테고리·modularity)
    api/analytics.json          위 세 종의 카탈로그(경로·바이트·핵심 수치·경고)

엔진 재사용:
  - 봉투     : make_gap_fixtures.envelope
  - 쓰기     : make_nationwide.write_json (내부에서 make_more_fixtures._sanitize_keys 호출)
  - 통계     : policymap.analytics.spatial / eha, policymap.rag.community

카테고리 지표에 대하여
  spatial.metric_values 는 'category_share:*' 를 모른다. 엔진 파일을 고치는 대신
  이 생성기가 **지표 공급 함수만** 얇게 감싼다(install_category_metric). Moran/LISA/
  순열검정/FDR 는 전부 엔진 원본이 계산한다. 이렇게 만든 지표에는 결과에
  metric_source="generator_extension" 를 붙여 출처를 남긴다.

재개 가능: 이미 만들어진 shard 는 건너뛴다(--force 로 강제 재생성).
한 지표/템플릿이 실패해도 나머지는 계속 만든다(실패 사유는 analytics.json 의 errors 에).

사용:
  cd system
  python make_analytics_fixtures.py --permutations 99 --only spatial   # 빠른 검증
  python make_analytics_fixtures.py                                    # 전체(기본 999회)
  python make_analytics_fixtures.py --only eha,community --force
"""
import argparse
import json
import sqlite3
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:  # 콘솔이 cp949 여도 한글 출력에서 죽지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from policymap import db as D                              # noqa: E402
from policymap.analytics import base as B                  # noqa: E402
from policymap.analytics import eha as E                   # noqa: E402
from policymap.analytics import spatial as S               # noqa: E402
from policymap.rag import community as C                   # noqa: E402
from policymap.analytics import peers as P              # noqa: E402

# 중복 구현 금지 — 봉투·원자적 쓰기·키 살균·용량 표기는 기존 생성기 것을 그대로 쓴다.
from make_gap_fixtures import envelope                     # noqa: E402
from make_nationwide import existing, human, write_json    # noqa: E402
from make_graph_fixtures import write_shard                # noqa: E402

KINDS = ("spatial", "eha", "community", "peer_methods")

# --------------------------------------------------------------------------- #
# 목록
# --------------------------------------------------------------------------- #
# (metric, slug, 화면 설명). slug 는 ASCII 로 둔다(정적 호스팅 URL 안전).
DEFAULT_METRICS = [
    # ── 기본 지표 ────────────────────────────────────────────────────────
    ("ordinance_count", "ordinance-count",
     "현행 조례·규칙 수 — 입법 활동량의 공간 군집"),
    ("budget_per_capita", "budget-per-capita",
     "1인당 예산(총예산/인구) — 재정 여력의 공간 군집"),
    ("welfare_ratio", "welfare-ratio",
     "사회복지 예산 비중(C03 복지 영역의 예산측 지표)"),

    # ── 분야별 조례 비중 14종 ────────────────────────────────────────────
    # 전 분야를 굽는다. 예전에는 C03·C04 두 개뿐이라 "지표를 골라 본다"는
    # 화면의 취지가 살지 않았다. 분야마다 군집 구조가 다르다는 것 자체가 결과다
    # (실측: 복지 C03 은 Moran I=0.52 로 강한 군집, 인구 C04 는 -0.008 로 무작위와 구별 안 됨).
    ("category_share:C01", "category-share-c01",
     "행정·자치·의회(C01) 조례 비중 = C01 조례 수 / 현행 조례 수"),
    ("category_share:C02", "category-share-c02",
     "재정·세무·회계(C02) 조례 비중 = C02 조례 수 / 현행 조례 수"),
    ("category_share:C03", "category-share-c03",
     "복지·돌봄(C03) 조례 비중 = C03 조례 수 / 현행 조례 수"),
    ("category_share:C04", "category-share-c04",
     "인구·출산·양육(C04) 조례 비중 = C04 조례 수 / 현행 조례 수"),
    ("category_share:C05", "category-share-c05",
     "청년·교육(C05) 조례 비중 = C05 조례 수 / 현행 조례 수"),
    ("category_share:C06", "category-share-c06",
     "보건·의료(C06) 조례 비중 = C06 조례 수 / 현행 조례 수"),
    ("category_share:C07", "category-share-c07",
     "환경·기후(C07) 조례 비중 = C07 조례 수 / 현행 조례 수"),
    ("category_share:C08", "category-share-c08",
     "안전·재난(C08) 조례 비중 = C08 조례 수 / 현행 조례 수"),
    ("category_share:C09", "category-share-c09",
     "도시·건축·주택(C09) 조례 비중 = C09 조례 수 / 현행 조례 수"),
    ("category_share:C10", "category-share-c10",
     "교통(C10) 조례 비중 = C10 조례 수 / 현행 조례 수"),
    ("category_share:C11", "category-share-c11",
     "경제·산업·일자리(C11) 조례 비중 = C11 조례 수 / 현행 조례 수"),
    ("category_share:C12", "category-share-c12",
     "농림·수산(C12) 조례 비중 = C12 조례 수 / 현행 조례 수"),
    ("category_share:C13", "category-share-c13",
     "문화·체육·관광(C13) 조례 비중 = C13 조례 수 / 현행 조례 수"),
    ("category_share:C14", "category-share-c14",
     "동물·반려(C14) 조례 비중 = C14 조례 수 / 현행 조례 수"),

    # ── 확산 템플릿의 채택연도 공간군집 ──────────────────────────────────
    # 채택 "연도" 자체의 군집은 수평확산(이웃 학습) 가설의 직접 검정이다.
    # _resid 는 시도 고정효과를 뺀 잔차로, 광역 공통충격과 이웃학습을 분리한다.
    ("adoption_year:맨발걷기", "adoption-year-barefoot-walking",
     "맨발걷기 조례 채택 연도의 공간군집 — 수평확산 가설의 직접 검정"),
    ("adoption_year_resid:맨발걷기", "adoption-year-resid-barefoot-walking",
     "위와 같되 시도 고정효과(광역 공통충격)를 뺀 잔차 — 이웃학습 vs 광역충격 분리"),
    ("adoption_year:안전보안관", "adoption-year-safety-sheriff",
     "안전보안관 조례 채택 연도의 공간군집 — 수평확산 가설의 직접 검정"),
    ("adoption_year_resid:안전보안관", "adoption-year-resid-safety-sheriff",
     "위와 같되 시도 고정효과(광역 공통충격)를 뺀 잔차 — 이웃학습 vs 광역충격 분리"),
    ("adoption_year:청년", "adoption-year-youth",
     "청년 조례 채택 연도의 공간군집 — 수평확산 가설의 직접 검정"),
    ("adoption_year_resid:청년", "adoption-year-resid-youth",
     "위와 같되 시도 고정효과(광역 공통충격)를 뺀 잔차 — 이웃학습 vs 광역충격 분리"),
]

# 확산 커버리지가 높은 템플릿만. 커버리지가 낮으면 채택시점이 관측되지 않아 위험모형이
# 무너진다. 아래는 rr_cls_cd LIKE '%제정%' 기준 실측(2026-08-24, level=2 기초자치단체):
#
#   맨발걷기 93.8%(130곳) · 안전보안관 91.4%(186) · 청년 86.8%(228)
#   ── 이하 채택 안 함 ──
#   반려동물 72.6% · 1인가구 70.1% · 스마트도시 55.3% · 도시재생 42.5% · 탄소중립 40.6%
#   기후위기 38.8% · 치매 38.5% · 생활임금 25.6% · 마을공동체 25.5% · 자전거 24.3% · 고향사랑 12.8%
#
# 탄소중립·기후위기는 보유 지자체가 200곳을 넘어 매력적으로 보이지만 제정본이 40%뿐이라
# 채택 '시점' 이 관측되지 않는다. 그대로 넣으면 확산이 늦게 시작한 것처럼 보이는 편의가 생긴다.
#
# 자원봉사(87.7%, 228곳)는 커버리지만 보면 채택할 만했으나 **모형이 발산했다** —
# 관측창이 2006-2026 으로 길고 채택이 초기에 몰려 있어 완전분리에 빠진다
# (실측: McFadden R2 = -7.2469, coef ~ 1e9, OR = inf). 제정본 커버리지는 필요조건이지
# 충분조건이 아니다. 템플릿을 추가할 때는 반드시 diagnose_convergence 를 통과하는지 본다.
DEFAULT_TEMPLATES = [
    ("맨발걷기", "barefoot-walking"),
    ("안전보안관", "safety-sheriff"),
    ("청년", "youth"),
]

# 모형 사양. 주모형 1 + 민감도 3.
#   role, mode, link, 추가 공변량, 설명
MODEL_SPECS = [
    ("primary", "enactment", "logit", (),
     "주모형 — 제정본 기준 채택시점, 로짓 링크, Berry&Berry(1990) 통합모형 공변량"),
    ("sensitivity_mode", "upper_bound", "logit", (),
     "민감도① 채택시점 정의를 '보유 판본 중 최초 시행일 상한'으로 바꿔도 부호·유의성이 "
     "유지되는지(base.adoption_years 의 선택편의 경고 대응)"),
    ("sensitivity_link", "enactment", "cloglog", (),
     "민감도② 보완로그로그 링크(이산시간 비례위험) — 링크 선택에 결과가 좌우되는지"),
    ("peer_vs_neighbor", "enactment", "logit", ("peer_exposure",),
     "확장 — 지리적 인접 노출(neighbor_exposure)과 구조적 유사 노출(peer_exposure, "
     "행안부 유사자치단체 Top-20)을 한 모형에 같이 넣어 head-to-head 비교"),
    ("three_channel", "enactment", "logit", ("peer_exposure", "neural_exposure"),
     "확장② 확산 경로 3종을 한 모형에서 동시 비교 — 지리적 인접(neighbor_exposure) vs "
     "통계적 유사(peer_exposure, 행안부 기준) vs 구조적 유사(neural_exposure, 그래프 "
     "신경망 임베딩 Top-20). 셋을 같이 넣으면 서로를 통제한 뒤 어느 경로가 남는지 보인다. "
     "Region 임베딩이 없으면 neural_exposure 가 전부 결측이라 모형에서 자동 제외된다"),
]

COVARIATE_GLOSSARY = {
    "(intercept)": "절편(기저 위험)",
    "neighbor_exposure": "t-1 까지 채택한 지리적 인접 지자체 비율 (Valente 1996 exposure)",
    "peer_exposure": "t-1 까지 채택한 행안부 유사자치단체 Top-20 비율(통계적 유사 — 인구·재정)",
    "neural_exposure": "t-1 까지 채택한 그래프 신경망 임베딩 Top-20 유사 지자체 비율"
                       "(구조적 유사 — 조례 구성·상위법 연결·이웃 관계에서 학습)",
    "sido_exposure": "t-1 까지 채택한 동일 광역 내 타 기초 비율",
    "upper_adopted": "t-1 까지 상위 광역이 같은 이름의 조례를 채택했는지(0/1, 수직확산)",
    "log_pop": "log(주민등록 인구)",
    "welfare_ratio": "사회복지 예산 비중(FY2025, 시불변)",
    "fiscal_self_ratio": "자체재원 비율(FY2025, 시불변) — 재정자립도 근사",
    "year_trend": "t - y0 (선형 시간추세, 기저위험 근사)",
}

CAVEAT = (
    "해석 주의 — 이 추정은 '이웃이 채택하면 따라 채택한다'는 수평확산 가설을 **지지하지 "
    "않는다**. 우리 실측에서 neighbor_exposure(지리적 인접 노출)는 유의하지 않거나 오히려 "
    "음(-)의 방향으로 나왔고, 일관되게 유의한 것은 인구 규모(log_pop)와 시간추세였다. "
    "즉 관측된 S자 확산곡선을 '이웃 학습'의 증거로 읽으면 안 된다 — 상위법·중앙 지침·규모 "
    "효과로 설명될 여지가 크다. 계수는 1표준편차 변화당 로그오즈이며(연속변수 z-표준화), "
    "표준오차는 지자체 클러스터 로버스트(Liang&Zeger 1986)다. 채택시점은 rr_cls_cd='제정' "
    "판본에서만 관측되므로 선택편의가 있다(enactment_date_coverage 및 sensitivity_mode "
    "모형으로 확인하라). 인과추론이 아니라 관측자료의 조건부 연관이다."
)


def _neighbor_note(term: dict | None) -> str:
    """주모형의 neighbor_exposure 계수를 실제 수치대로 서술한다(과장 금지)."""
    if not term or term.get("p_value") is None:
        return "neighbor_exposure 계수가 추정되지 않았다(공변량 결측 또는 상수열 제거)."
    coef, p = float(term["coef"]), float(term["p_value"])
    direction = "양(+)" if coef > 0 else "음(-)"
    if p >= 0.05:
        return (f"neighbor_exposure 계수 {coef:+.4f} (p={p:.4f}) — 유의수준 5%에서 "
                f"0과 구별되지 않는다. 이웃효과 **미지지**.")
    if coef < 0:
        return (f"neighbor_exposure 계수 {coef:+.4f} (p={p:.4f}) — 유의하지만 부호가 "
                f"{direction}이다. 이웃이 먼저 채택할수록 채택 위험이 낮아지는 방향이므로 "
                f"수평확산(학습·모방) 가설과 반대다. 인접 지자체가 이미 채택해 남은 "
                f"위험집합이 선택적으로 구성된 결과일 수 있다.")
    return (f"neighbor_exposure 계수 {coef:+.4f} (p={p:.4f}) — 유의한 {direction} 효과. "
            f"다만 단일 템플릿·단일 사양의 결과이므로 민감도 모형(sensitivity_mode / "
            f"sensitivity_link)에서도 부호·유의성이 유지되는지 함께 볼 것.")

SPATIAL_GUIDE = (
    "전역 I>0 이면 유사한 값이 공간적으로 뭉쳐 있다는 뜻이며, p_sim 은 순열분포 기준이다. "
    "국지 LISA 는 다중비교 때문에 반드시 significant(BH-FDR 통과)인 곳만 색칠·해석하라 — "
    "미보정 시 위양성이 n×0.05 곳 발생한다. quadrant 는 HH(고-고 군집) / LL(저-저 군집) / "
    "HL·LH(공간 이상치)다."
)


# --------------------------------------------------------------------------- #
# 카테고리 지표 확장 (엔진 파일 수정 없이 지표 공급만 감싼다)
# --------------------------------------------------------------------------- #
_ORIG_METRIC_VALUES = S.metric_values
_CATEGORY_METRICS = ("category_share:", "category_count:")


def _category_values(conn: sqlite3.Connection, code: str, *, share: bool,
                     level: int = 2) -> dict[str, float]:
    """지자체별 특정 카테고리 조례 수(또는 현행 조례 대비 비중).

    분모는 region_covariates 의 ordinance_count(현행 조례·규칙 수)와 같은 정의를 쓴다.
    """
    cov = B.region_covariates(conn, level=level)
    ids = list(cov)
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    rows = D.fetchall(
        conn,
        "SELECT o.region_id AS region_id, COUNT(*) AS n "
        "FROM ordinance_category oc JOIN ordinances o ON o.ordinance_id = oc.ordinance_id "
        f"WHERE oc.category_code = ? AND o.status = 'active' AND o.region_id IN ({ph}) "
        "GROUP BY o.region_id",
        [code] + ids)
    cnt = {r["region_id"]: float(r["n"]) for r in rows}
    out: dict[str, float] = {}
    for rid, c in cov.items():
        n = cnt.get(rid, 0.0)
        if not share:
            out[rid] = n
            continue
        tot = c.get("ordinance_count") or 0
        if tot > 0:
            out[rid] = n / float(tot)
    return out


def install_category_metric() -> None:
    """spatial.metric_values 를 감싸 'category_share:CXX' / 'category_count:CXX' 지원.

    Moran/LISA/순열검정/FDR 는 손대지 않는다 — 엔진 원본이 그대로 계산한다.
    """
    def _wrapped(conn, metric, *, level=2, fyr=2025):
        if metric.startswith(_CATEGORY_METRICS):
            kind, code = metric.split(":", 1)
            return _category_values(conn, code.strip(), share=kind.endswith("share"),
                                    level=level)
        return _ORIG_METRIC_VALUES(conn, metric, level=level, fyr=fyr)

    S.metric_values = _wrapped


def _is_extended(metric: str) -> bool:
    return metric.startswith(_CATEGORY_METRICS)


# --------------------------------------------------------------------------- #
# 1) 공간통계 shard
# --------------------------------------------------------------------------- #
def spatial_shards(conn, out: Path, args, report: dict) -> None:
    if "spatial" not in args.only:
        return
    d = out / "spatial"
    for metric, slug, desc in DEFAULT_METRICS:
        path = d / f"{slug}.json"
        item = {"slug": slug, "metric": metric, "path": f"spatial/{path.name}",
                "description": desc}
        if not args.force and existing(path):
            item.update({"bytes": path.stat().st_size, "reused": True})
            try:
                dd = json.loads(path.read_text(encoding="utf-8")).get("data") or {}
                item.update({"moran_i": dd.get("moran_i"), "p_sim": dd.get("p_sim"),
                             "n": dd.get("n")})
            except Exception:  # noqa: BLE001
                pass
            report["spatial"].append(item)
            print(f"  [재사용] spatial/{path.name}  {human(item['bytes'])}", flush=True)
            continue
        t0 = time.time()
        try:
            res = S.moran(conn, metric, level=args.level, fyr=args.fyr,
                          permutations=args.permutations, lisa=True)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {str(exc)[:160]}"
            item["error"] = msg
            report["spatial"].append(item)
            report["errors"].append({"kind": "spatial", "slug": slug, "message": msg})
            print(f"  [건너뜀] spatial/{slug}.json — {msg}", flush=True)
            if args.traceback:
                traceback.print_exc()
            continue
        payload = dict(res)
        payload["_engine"] = "analytics.spatial.moran"
        payload["slug"] = slug
        payload["metric_label"] = desc
        payload["metric_source"] = ("generator_extension" if _is_extended(metric)
                                    else "analytics.spatial.metric_values")
        payload["reading_guide"] = SPATIAL_GUIDE
        payload["choropleth_key"] = "sig_cd"
        payload["choropleth_hint"] = (
            "lisa[] 의 sig_cd 를 지도 폴리곤 키로, value 를 연속 색상으로 칠하고 "
            "significant=True 인 곳만 quadrant 색(HH/LL/HL/LH)으로 덧칠하라.")
        n_bytes = write_json(path, envelope(payload))
        ls = res.get("lisa_summary") or {}
        item.update({
            "bytes": n_bytes, "seconds": round(time.time() - t0, 1),
            "n": res.get("n"), "universe": res.get("universe"),
            "moran_i": res.get("moran_i"), "expected_i": res.get("expected_i"),
            "z_sim": res.get("z_sim"), "p_sim": res.get("p_sim"),
            "n_significant_fdr": ls.get("n_significant_fdr"),
            "by_quadrant": ls.get("by_quadrant"),
            "interpretation": res.get("interpretation"),
        })
        report["spatial"].append(item)
        print("  [OK] " + S.format_moran(res).replace("\n", "\n       ")
              + f"\n       → spatial/{path.name} {human(n_bytes)} "
                f"{item['seconds']}s", flush=True)


# --------------------------------------------------------------------------- #
# 2) EHA shard
# --------------------------------------------------------------------------- #
# 로지스틱/보완로그로그 추정이 완전분리(separation)에 빠지면 예외를 던지지 않고
# 계수가 발산한 채로 '성공' 을 돌려준다. 실측 사례:
#   자원봉사 primary               McFadden R2 = -7.2469, coef ~ 1e9, OR = inf
#   청년 three_channel_cloglog     McFadden R2 = -6.2101, coef ~ 1e8
# 그대로 화면에 실으면 유의성 별표까지 붙은 쓰레기가 표로 나간다. 걸러 낸다.
DIVERGENCE_COEF_ABS = 50.0     # 표준화 공변량의 로짓 계수가 이 값을 넘으면 비정상
DIVERGENCE_MIN_R2 = -0.5       # McFadden R2 는 음수가 될 수 있으나 이 아래는 미수렴


def diagnose_convergence(res: dict):
    """미수렴이면 사유 문자열, 정상이면 None."""
    m = res.get("model") or {}
    r2 = m.get("mcfadden_r2")
    if r2 is not None and r2 < DIVERGENCE_MIN_R2:
        return f"McFadden R2={r2:.4f} (< {DIVERGENCE_MIN_R2}) — 귀무모형보다 나쁘다"
    for t in m.get("terms") or []:
        c = t.get("coef")
        if c is None or c != c:
            return f"{t.get('term')} 계수가 NaN"
        if abs(c) > DIVERGENCE_COEF_ABS:
            return f"{t.get('term')} 계수 {c:.3g} 가 |{DIVERGENCE_COEF_ABS}| 를 넘음 — 완전분리 의심"
        orv = t.get("odds_ratio")
        if orv is not None and (orv != orv or orv in (float("inf"), float("-inf"))):
            return f"{t.get('term')} 의 OR 이 inf/NaN"
    return None


def eha_shards(conn, out: Path, args, report: dict) -> None:
    if "eha" not in args.only:
        return
    d = out / "eha"
    for template, slug in DEFAULT_TEMPLATES:
        path = d / f"{slug}.json"
        item = {"slug": slug, "template": template, "path": f"eha/{path.name}"}
        if not args.force and existing(path):
            item.update({"bytes": path.stat().st_size, "reused": True})
            report["eha"].append(item)
            print(f"  [재사용] eha/{path.name}  {human(item['bytes'])}", flush=True)
            continue
        t0 = time.time()
        try:
            # 관측창은 두 정의의 합집합으로 잡되(upper_bound 가 더 넓다),
            # 화면에 띄울 커버리지는 주모형이 쓰는 'enactment' 기준이어야 한다.
            ad = B.adoption_years(conn, template, level=args.level, mode="upper_bound")
            ad_enact = B.adoption_years(conn, template, level=args.level, mode="enactment")
        except Exception as exc:  # noqa: BLE001
            msg = f"adoption_years 실패 {type(exc).__name__}: {str(exc)[:140]}"
            item["error"] = msg
            report["eha"].append(item)
            report["errors"].append({"kind": "eha", "slug": slug, "message": msg})
            print(f"  [건너뜀] eha/{slug}.json — {msg}", flush=True)
            continue
        years = list(ad["years"].values())
        if not years:
            msg = "채택 관측치 0건"
            item["error"] = msg
            report["eha"].append(item)
            report["errors"].append({"kind": "eha", "slug": slug, "message": msg})
            continue
        y0 = args.y0 or min(years)
        y1 = args.y1 or max(years)

        models, errors = [], []
        for role, mode, link, extra, note in MODEL_SPECS:
            covs = tuple(E.DEFAULT_COVARIATES) + tuple(extra)
            m0 = time.time()
            try:
                res = E.estimate_diffusion_hazard(
                    conn, template, y0=y0, y1=y1, level=args.level, mode=mode,
                    covariates=covs, link=link, standardize=True, fyr=args.fyr)
            except Exception as exc:  # noqa: BLE001
                msg = f"{role}: {type(exc).__name__}: {str(exc)[:140]}"
                errors.append(msg)
                report["errors"].append({"kind": "eha", "slug": slug, "message": msg})
                print(f"       [모형 실패] {msg}", flush=True)
                if args.traceback:
                    traceback.print_exc()
                continue
            res["role"] = role
            res["link"] = link
            res["spec_note"] = note
            res["covariates"] = list(covs)
            res["console_table"] = E.format_table(res)
            res["seconds"] = round(time.time() - m0, 1)
            bad = diagnose_convergence(res)
            if bad:
                # 버리지 않고 표시만 한다 — 어떤 사양이 왜 실패했는지가 정보다.
                # 화면은 diverged=True 인 모형을 표에서 빼고 사유만 적는다.
                res["diverged"] = True
                res["divergence_reason"] = bad
                errors.append(f"{role}: 미수렴 — {bad}")
                report["errors"].append({"kind": "eha", "slug": slug,
                                         "message": f"{role} 미수렴: {bad}"})
                print(f"       [미수렴] {role}: {bad}", flush=True)
            models.append(res)
            if role == "primary" and not bad:
                print("  [OK] " + res["console_table"].replace("\n", "\n       "), flush=True)

        if not models:
            item["error"] = "모든 모형 추정 실패"
            report["eha"].append(item)
            continue

        ok = [m for m in models if not m.get("diverged")]
        if not ok:
            item["error"] = "모든 모형이 미수렴(완전분리 의심)"
            report["eha"].append(item)
            print(f"       [건너뜀] {template}: 모든 모형 미수렴", flush=True)
            continue
        primary = next((m for m in ok if m["role"] == "primary"), ok[0])
        pm = primary.get("model") or {}
        terms = {t["term"]: t for t in pm.get("terms", [])}
        nb = terms.get("neighbor_exposure") or {}
        payload = {
            "_engine": "analytics.eha.estimate_diffusion_hazard",
            "slug": slug,
            "template": template,
            "window": [y0, y1],
            "level": args.level,
            "adoption_meta": {"enactment": ad_enact["meta"], "upper_bound": ad["meta"]},
            "enactment_date_coverage": ad_enact["meta"].get("enactment_date_coverage"),
            "coverage_note": (
                f"주모형(mode=enactment)의 제정본 커버리지 "
                f"{ad_enact['meta'].get('enactment_date_coverage')} — 나머지는 개정 이력만 "
                f"남아 채택시점이 관측되지 않는다(선택편의). 관측창은 "
                f"upper_bound 정의까지 포함한 합집합 {min(years)}–{max(years)} 이다."),
            "models": models,
            "primary_role": primary.get("role"),
            "covariate_glossary": {k: v for k, v in COVARIATE_GLOSSARY.items()
                                   if k in terms or k == "peer_exposure"},
            "risk_set": {
                "n_obs": pm.get("n_obs"),
                "n_events": pm.get("n_events"),
                "n_clusters": pm.get("n_clusters"),
                "universe": (primary.get("panel_meta") or {}).get("universe"),
                "left_truncated": (primary.get("panel_meta") or {}).get("left_truncated"),
                "right_censored": (primary.get("panel_meta") or {}).get("right_censored"),
                "note": "위험집합 = 아직 채택하지 않은 지자체 × 연도. 채택한 해에 "
                        "event=1 행을 남기고 이탈한다(Allison 1982).",
            },
            "significance_legend": {"***": "p<0.001", "**": "p<0.01", "*": "p<0.05",
                                    "†": "p<0.10"},
            "neighbor_effect_supported": bool(
                nb.get("p_value") is not None and nb["p_value"] < 0.05 and nb.get("coef", 0) > 0),
            "neighbor_effect_note": _neighbor_note(nb),
            "interpretation_caveat": CAVEAT,
            "verification_status": "engine_computed_unreviewed",
            "model_errors": errors,
            "references": [
                "Berry & Berry (1990) APSR 84(2):395-415",
                "Shipan & Volden (2008) AJPS 52(4):840-857",
                "Valente (1996) Social Networks 18(1):69-89",
                "Allison (1982) Sociological Methodology 13:61-98",
                "Liang & Zeger (1986) Biometrika 73(1):13-22",
            ],
        }
        n_bytes = write_json(path, envelope(payload))
        item.update({
            "bytes": n_bytes, "seconds": round(time.time() - t0, 1),
            "window": [y0, y1], "n_models": len(models),
            "enactment_date_coverage": ad_enact["meta"].get("enactment_date_coverage"),
            "n_obs": pm.get("n_obs"), "n_events": pm.get("n_events"),
            "mcfadden_r2": pm.get("mcfadden_r2"),
            "neighbor_exposure_p": nb.get("p_value"),
            "neighbor_effect_supported": payload["neighbor_effect_supported"],
            "neighbor_effect_note": payload["neighbor_effect_note"],
            "model_errors": errors,
        })
        report["eha"].append(item)
        print(f"       → eha/{path.name} {human(n_bytes)} {item['seconds']}s "
              f"(모형 {len(models)}종)", flush=True)


# --------------------------------------------------------------------------- #
# 3) 커뮤니티 shard
# --------------------------------------------------------------------------- #
def community_shard(conn, out: Path, args, report: dict) -> None:
    if "community" not in args.only:
        return
    path = out / "community" / "summary.json"
    item = {"path": "community/summary.json"}
    if not args.force and existing(path):
        item.update({"bytes": path.stat().st_size, "reused": True})
        report["community"].append(item)
        print(f"  [재사용] community/summary.json  {human(item['bytes'])}", flush=True)
        return
    t0 = time.time()
    scopes: dict[str, dict] = {}
    for scope in ("ordinance_similarity", "region_adjacency"):
        s0 = time.time()
        try:
            if args.reuse_report:
                rep = C.load_community_report(scope)
                if rep is None:
                    rep = C.build_community_report(conn, scope=scope, seed=args.seed)
            else:
                rep = C.build_community_report(conn, scope=scope, seed=args.seed)
        except Exception as exc:  # noqa: BLE001
            msg = f"{scope}: {type(exc).__name__}: {str(exc)[:160]}"
            report["errors"].append({"kind": "community", "scope": scope, "message": msg})
            print(f"  [건너뜀] community {scope} — {msg}", flush=True)
            if args.traceback:
                traceback.print_exc()
            continue
        rep = dict(rep)
        rep.pop("path", None)
        rep.pop("bytes", None)
        scopes[scope] = rep
        print(f"  [OK] community {scope}: modularity="
              f"{rep.get('modularity')} 커뮤니티 {rep.get('num_communities_summarized')}/"
              f"{rep.get('num_communities_detected')} backend={rep.get('backend')} "
              f"{time.time() - s0:.1f}s", flush=True)
    if not scopes:
        item["error"] = "모든 scope 실패"
        report["community"].append(item)
        return
    payload = {
        "_engine": "rag.community.build_community_report",
        "scopes": scopes,
        "scope_guide": {
            "ordinance_similarity": "조례 본문 유사도 그래프의 주제 군집 — "
                                    "'전국에서 어떤 패턴으로 퍼졌나' 전역질의용",
            "region_adjacency": "지자체 인접 그래프의 권역 군집 — 벤치마킹 그룹용",
        },
        "coverage_caveat": (
            "조례 유사도 그래프는 **본문을 확보한 조례**만 덮는다. 커뮤니티 size 는 전국 "
            "조례 총수가 아니라 본문 확보분 기준이며, 각 커뮤니티의 nationwide 항목이 "
            "앵커 키워드로 전체 조례를 역조회한 전국 투영이다. 두 수를 섞어 읽지 말 것."),
        "verification_status": "engine_computed_unreviewed",
    }
    n_bytes = write_json(path, envelope(payload))
    item.update({
        "bytes": n_bytes, "seconds": round(time.time() - t0, 1),
        "scopes": {k: {"modularity": v.get("modularity"),
                       "backend": v.get("backend"),
                       "num_communities_detected": v.get("num_communities_detected"),
                       "num_communities_summarized": v.get("num_communities_summarized")}
                   for k, v in scopes.items()},
    })
    report["community"].append(item)
    print(f"       → community/summary.json {human(n_bytes)} {item['seconds']}s", flush=True)


# --------------------------------------------------------------------------- #
# 4) 유사 지자체 방법 비교 shard (analytics.peers.compare_peer_methods)
# --------------------------------------------------------------------------- #
#  왜 필요한가 — 격차분석의 근거는 "누구와 비교했나"에 통째로 달려 있다.
#  같은 지자체라도 선정 방식이 다르면 Top-10 이 갈린다. 그 사실을 감추지 않고 화면에 낸다.
#
#  주의: compare_peer_methods 내부의 legacy 경로(graph.analysis.find_peer_governments)는
#  sqlite3.Row 를 요구한다. policymap.db.connect() 는 row_factory 를 설정하므로 그대로 쓴다.
#  (raw sqlite3.connect 로 넘기면 legacy_error 만 남고 비교가 2개 방식으로 쪼그라든다 - 실측)

PEER_METHOD_LABELS = {
    "mois_aligned": "행안부 유사자치단체 기준 정렬(지표 z-표준화 가중 유클리드)",
    "hybrid_policy": "위 지표 + 조례명 TF-IDF 정책 프로파일 코사인 30% 혼합",
    "legacy": "구 방식(graph.analysis) - 예산·인구·카테고리 구조 코사인",
}


def _slim_peer(p: dict, rank: int, feats: dict) -> dict:
    """화면에 필요한 필드만 남긴다(indicator_gap_sd 는 gap 화면이 이미 보여준다).

    구 방식(legacy)은 rtype 을 돌려주지 않고 이름도 시도 접두어가 없다("광주시").
    같은 화면에서 세 방식을 나란히 놓으므로 region_features 로 표기를 맞춘다
    (경기도 광주시 / 광주광역시 광산구 처럼 동명 지자체를 구분하기 위해서다).
    """
    rid = p.get("region_id") or p.get("sig_cd")
    f = feats.get(str(rid)) or {}
    return {
        "rank": rank,
        "region_id": rid,
        "name": f.get("full_name") or p.get("full_name") or p.get("name") or f.get("name"),
        "rtype": p.get("rtype") or f.get("rtype"),
        "similarity": p.get("similarity"),
        "weighted_distance": p.get("weighted_distance"),
        "policy_profile_cosine": p.get("policy_profile_cosine"),
    }


def _slim_method(res, label: str, feats: dict) -> dict:
    peers = (res.get("peers") if isinstance(res, dict) else res) or []
    return {"label": label, "k": len(peers),
            "peers": [_slim_peer(p, i + 1, feats) for i, p in enumerate(peers)]}


def peer_method_shards(conn, out: Path, args, report: dict) -> None:
    if "peer_methods" not in args.only:
        return
    d = out / "peer_methods"
    feats = P.load_region_features(conn)
    tfidf = B.ordinance_name_tfidf(conn, level=args.level)   # 한 번만 짓는다(호출당 재빌드 방지)
    targets = sorted(feats.keys())
    if args.peer_limit:
        targets = targets[: args.peer_limit]
    print(f"  대상 {len(targets)}곳 · tfidf {len(tfidf)}곳", flush=True)

    done = 0
    for i, sig in enumerate(targets, 1):
        path = d / f"{sig}.json"
        item = {"sig_cd": sig, "path": f"peer_methods/{path.name}"}
        if not args.force and existing(path):
            item.update({"bytes": path.stat().st_size, "reused": True})
            report["peer_methods"].append(item)
            continue
        t0 = time.time()
        try:
            res = P.compare_peer_methods(conn, sig, k=args.peer_k,
                                         features=feats, tfidf=tfidf)
        except Exception as exc:  # noqa: BLE001
            msg = f"{sig}: {type(exc).__name__}: {str(exc)[:140]}"
            item["error"] = msg
            report["peer_methods"].append(item)
            report["errors"].append({"kind": "peer_methods", "sig_cd": sig, "message": msg})
            print(f"  [건너뜀] peer_methods/{sig}.json - {msg}", flush=True)
            if args.traceback:
                traceback.print_exc()
            continue

        tgt = (res.get("mois_aligned") or {}).get("target") or {}
        methods = {}
        for key, label in PEER_METHOD_LABELS.items():
            if res.get(key):
                methods[key] = _slim_method(res[key], label, feats)
        payload = {
            "_engine": "analytics.peers.compare_peer_methods",
            "sig_cd": sig,
            "target": {"region_id": tgt.get("region_id"), "name": tgt.get("name"),
                       "full_name": tgt.get("full_name"), "rtype": res.get("target_type"),
                       "level": tgt.get("level")},
            "k": args.peer_k,
            "methods": methods,
            "overlap": res.get("overlap") or {},
            "type_composition": res.get("type_composition") or {},
            "legacy_error": res.get("legacy_error"),
            "guide": "peer_methods/_guide.json",
            "verification_status": "engine_computed_unreviewed",
        }
        item["bytes"] = write_shard(path, envelope(payload))
        item["seconds"] = round(time.time() - t0, 2)
        for pair, v in (res.get("overlap") or {}).items():
            item[f"jaccard:{pair}"] = v.get("jaccard")
        report["peer_methods"].append(item)
        done += 1
        if i % 40 == 0 or i == len(targets):
            print(f"  [{i}/{len(targets)}] {sig} {human(item['bytes'])}", flush=True)
    # 공통 안내문은 227번 복사하지 않고 한 파일로 뺀다(용량 절감).
    write_json(d / "_guide.json", envelope({
        "_engine": "analytics.peers.compare_peer_methods",
        "method_labels": PEER_METHOD_LABELS,
        "k": args.peer_k,
        "weight_provenance": P.WEIGHT_PROVENANCE,
        "reading_guide": (
            "같은 지자체라도 유사 지자체 선정 방식이 다르면 Top-k 가 갈린다. "
            "overlap.jaccard 가 낮을수록 '누구와 비교하느냐'가 결론을 좌우한다는 뜻이다. "
            "격차분석 화면(#/gap)이 쓰는 것은 mois_aligned 이며, 여기서 그 선택의 "
            "영향 범위를 공시한다."),
        "same_type_note": (
            "same_type_rate = Top-k 중 대상과 같은 유형(자치구/시/군)의 비율. "
            "자치구와 군을 섞어 비교하면 조례 보유 구성이 달라 격차 목록이 왜곡된다."),
        "policy_profile_note": (
            "policy_profile_cosine 은 hybrid_policy 방식에서만 채워진다"
            "(mois_aligned 는 지표만 쓴다)."),
        "verification_status": "engine_computed_unreviewed",
    }))
    print(f"  생성 {done} · 재사용 {len(targets) - done}", flush=True)


# --------------------------------------------------------------------------- #
# 카탈로그
# --------------------------------------------------------------------------- #
def _read_data(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("data") or {}
    except Exception:  # noqa: BLE001
        return {}


def _harvest(kind: str, path: Path) -> dict:
    """이번 실행이 만들지 않은 shard 에서도 카탈로그용 핵심 수치를 읽어 온다."""
    d = _read_data(path)
    if kind == "spatial":
        ls = d.get("lisa_summary") or {}
        return {"metric": d.get("metric"), "description": d.get("metric_label"),
                "n": d.get("n"), "universe": d.get("universe"),
                "moran_i": d.get("moran_i"), "expected_i": d.get("expected_i"),
                "z_sim": d.get("z_sim"), "p_sim": d.get("p_sim"),
                "n_significant_fdr": ls.get("n_significant_fdr"),
                "by_quadrant": ls.get("by_quadrant"),
                "interpretation": d.get("interpretation")}
    if kind == "eha":
        prim = next((m for m in d.get("models", []) if m.get("role") == "primary"), None)
        pm = (prim or {}).get("model") or {}
        nb = next((t for t in pm.get("terms", []) if t["term"] == "neighbor_exposure"), {})
        return {"template": d.get("template"), "window": d.get("window"),
                "n_models": len(d.get("models") or []),
                "enactment_date_coverage": d.get("enactment_date_coverage"),
                "n_obs": pm.get("n_obs"), "n_events": pm.get("n_events"),
                "mcfadden_r2": pm.get("mcfadden_r2"),
                "neighbor_exposure_p": nb.get("p_value"),
                "neighbor_effect_supported": d.get("neighbor_effect_supported"),
                "neighbor_effect_note": d.get("neighbor_effect_note")}
    return {}


def build_catalog(out: Path, report: dict) -> int:
    """디스크를 훑어 analytics.json 을 확정한다(부분 실행이 카탈로그를 지우지 않게)."""
    for kind, key in (("spatial", "slug"), ("eha", "slug")):
        known = {it.get(key): it for it in report[kind]}
        items = []
        for f in sorted((out / kind).glob("*.json")):
            it = known.get(f.stem)
            if it is None:
                it = {key: f.stem, "path": f"{kind}/{f.name}"}
                it.update(_harvest(kind, f))
            elif it.get("reused") or it.get("moran_i") is None and kind == "spatial":
                merged = _harvest(kind, f)
                merged.update({k: v for k, v in it.items() if v is not None})
                it = merged
            it = dict(it)
            it["bytes"] = f.stat().st_size
            items.append(it)
        report[kind] = items
    cpath = out / "community" / "summary.json"
    if cpath.exists():
        it = report["community"][0] if report["community"] else {
            "path": "community/summary.json"}
        it = dict(it)
        it["bytes"] = cpath.stat().st_size
        if not it.get("scopes"):
            d = _read_data(cpath)
            it["scopes"] = {k: {"modularity": v.get("modularity"),
                                "backend": v.get("backend"),
                                "num_communities_detected": v.get("num_communities_detected"),
                                "num_communities_summarized": v.get(
                                    "num_communities_summarized")}
                            for k, v in (d.get("scopes") or {}).items()}
        report["community"] = [it]
    else:
        report["community"] = [it for it in report["community"] if it.get("error")]

    # peer_methods 는 지역 수가 많아 카탈로그에는 색인·요약만 싣는다.
    pdir = out / "peer_methods"
    if pdir.exists():
        known = {it.get("sig_cd"): it for it in report["peer_methods"]}
        rows, jac = [], []
        for f in sorted(pdir.glob("*.json")):
            if f.stem.startswith("_"):      # _guide.json 은 색인 대상이 아니다
                continue
            it = dict(known.get(f.stem) or {"sig_cd": f.stem,
                                            "path": f"peer_methods/{f.name}"})
            it["bytes"] = f.stat().st_size
            if it.get("jaccard:mois_aligned|legacy") is None:
                d = _read_data(f)
                ov = (d.get("overlap") or {}).get("mois_aligned|legacy") or {}
                it["jaccard:mois_aligned|legacy"] = ov.get("jaccard")
                it["name"] = (d.get("target") or {}).get("name")
            v = it.get("jaccard:mois_aligned|legacy")
            if v is not None:
                jac.append(v)
            rows.append(it)
        report["peer_methods"] = rows
        report["peer_methods_summary"] = {
            "shards": len(rows),
            "mean_jaccard_mois_vs_legacy": round(sum(jac) / len(jac), 4) if jac else None,
            "n_with_legacy": len(jac),
            "note": "mois_aligned 와 구 방식의 Top-k 겹침(Jaccard) 평균. "
                    "낮을수록 방법 선택이 결론을 크게 바꾼다.",
        }

    total = sum(it.get("bytes", 0) for k in KINDS for it in report[k])
    report["total_bytes"] = total
    report["total_human"] = human(total)
    path = out / "analytics.json"
    return write_json(path, envelope(report, kind="analytics_catalog"))


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="공간통계·EHA·커뮤니티·유사방법비교 shard 생성")
    ap.add_argument("--out", default=str(ROOT / "data"), help="출력 루트(기본 system/data)")
    ap.add_argument("--only", default=",".join(KINDS),
                    help=f"생성 종류(쉼표). {'|'.join(KINDS)}")
    ap.add_argument("--force", action="store_true", help="기존 shard 를 덮어쓴다")
    ap.add_argument("--permutations", type=int, default=999, help="순열검정 횟수(기본 999)")
    ap.add_argument("--level", type=int, default=2, help="분석 레벨(기본 2=기초)")
    ap.add_argument("--fyr", type=int, default=2025, help="예산 회계연도(기본 2025)")
    ap.add_argument("--y0", type=int, default=0, help="EHA 관측창 시작(0=자동)")
    ap.add_argument("--y1", type=int, default=0, help="EHA 관측창 끝(0=자동)")
    ap.add_argument("--seed", type=int, default=2026, help="커뮤니티 탐지 시드")
    ap.add_argument("--peer-k", type=int, default=10, help="유사방법비교 Top-k(기본 10)")
    ap.add_argument("--peer-limit", type=int, default=0,
                    help="유사방법비교 대상 지자체 수 상한(0=전체)")
    ap.add_argument("--reuse-report", action="store_true",
                    help="커뮤니티 리포트가 이미 있으면 재빌드하지 않는다")
    ap.add_argument("--traceback", action="store_true", help="실패 시 스택 출력")
    a = ap.parse_args()
    a.only = {s.strip() for s in a.only.split(",") if s.strip()}
    bad = a.only - set(KINDS)
    if bad:
        print(f"알 수 없는 --only 값: {sorted(bad)}", flush=True)
        return 2

    out = Path(a.out) / "api"
    out.mkdir(parents=True, exist_ok=True)
    install_category_metric()

    t0 = time.time()
    conn = D.connect()
    report: dict = {
        "generator": "make_analytics_fixtures.py",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "params": {"only": sorted(a.only), "permutations": a.permutations,
                   "level": a.level, "fyr": a.fyr, "seed": a.seed},
        "spatial": [], "eha": [], "community": [], "peer_methods": [], "errors": [],
    }

    print(f"[공간통계] 지표 {len(DEFAULT_METRICS)}종 (순열 {a.permutations}회)", flush=True)
    spatial_shards(conn, out, a, report)
    print(f"[EHA] 템플릿 {len(DEFAULT_TEMPLATES)}종 × 모형 {len(MODEL_SPECS)}종", flush=True)
    eha_shards(conn, out, a, report)
    print("[커뮤니티] scope 2종", flush=True)
    community_shard(conn, out, a, report)
    print("[유사방법비교] compare_peer_methods", flush=True)
    peer_method_shards(conn, out, a, report)

    n = build_catalog(out, report)
    print(f"\n[카탈로그] analytics.json {human(n)} · 합계 {report['total_human']} · "
          f"오류 {len(report['errors'])}건 · 총 {time.time() - t0:.1f}s", flush=True)
    for e in report["errors"]:
        print(f"  - {e}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
