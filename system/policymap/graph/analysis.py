"""policymap.graph.analysis — 도메인 분석(순수파이썬 기본, numpy/networkx 선택 가속).

CONTRACTS.md §3.2 계약 함수(시그니처 준수):
    find_peer_governments(conn, sig_cd, *, k=10, features=('budget','pop','structure')) -> list[dict]
    compare_ordinance_coverage(conn, parent_instrument_id, *, region_level=2) -> dict
    trace_ordinance_diffusion(conn, template, *, since=None) -> dict
    get_delegation_gap(conn, region_id) -> list[dict]
    compute_spatial_autocorrelation(conn, metric, *, method='moran') -> dict
    link_ordinance_budget(conn, *, min_confidence=0.5) -> dict

추가 공개(도메인 편의; MCP/export 재사용):
    detect_communities(conn, *, scope='region_adjacency', ...) -> dict
    build_region_profile(conn, sig_cd) -> dict

전량 표준라이브러리로 동작한다(numpy/networkx 있으면 가속만, 결과 동일).
"""
from __future__ import annotations

import json as _json
import math
import random
import sqlite3
import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

from .. import db as _db
from .. import util as _util

try:  # 선택적: networkx(커뮤니티 탐지 가속)
    import networkx as _nx  # type: ignore
    _HAS_NX = True
except Exception:  # pragma: no cover
    _nx = None  # type: ignore
    _HAS_NX = False


# --------------------------------------------------------------------------- #
# 공용 소형 헬퍼
# --------------------------------------------------------------------------- #
def _fetch(conn, sql, params=()) -> list[dict]:
    try:
        return _db.fetchall(conn, sql, params)
    except sqlite3.OperationalError:
        return []


def _one(conn, sql, params=()) -> Optional[dict]:
    try:
        return _db.fetchone(conn, sql, params)
    except sqlite3.OperationalError:
        return None


def _region_by_sig(conn, sig_cd: str) -> Optional[dict]:
    """sig_cd(5자리)로 현행 region 1건. 광역/기초 모두 sig_cd 저장됨."""
    return _one(conn,
                "SELECT * FROM regions WHERE sig_cd=? AND (valid_to IS NULL OR status='active') "
                "ORDER BY level LIMIT 1", (sig_cd,)) or \
        _one(conn, "SELECT * FROM regions WHERE sig_cd=? LIMIT 1", (sig_cd,))


def _active_ordinance_count(conn) -> dict[str, int]:
    """region_id → 현행 조례/규칙 수."""
    out: dict[str, int] = {}
    for r in _fetch(conn,
                    "SELECT region_id, COUNT(*) AS n FROM ordinances "
                    "WHERE region_id IS NOT NULL AND status='active' GROUP BY region_id"):
        out[r["region_id"]] = int(r["n"])
    return out


def _budget_total(conn) -> dict[str, float]:
    """region_id → 지출액(exe_amt) 합계(최신 회계연도 우선). 없으면 편성/현액 폴백."""
    out: dict[str, float] = {}
    rows = _fetch(conn,
                  "SELECT region_id, "
                  "COALESCE(SUM(exe_amt),0) AS exe, COALESCE(SUM(alloc_amt),0) AS alloc, "
                  "COALESCE(SUM(budget_now),0) AS now "
                  "FROM budget_lines WHERE region_id IS NOT NULL GROUP BY region_id")
    for r in rows:
        val = float(r["exe"] or 0) or float(r["alloc"] or 0) or float(r["now"] or 0)
        out[r["region_id"]] = val
    return out


def _category_vector(conn) -> dict[str, dict[str, int]]:
    """region_id → {category_code: 조례수}. 구조(정책분포) 특성용."""
    out: dict[str, dict[str, int]] = {}
    for r in _fetch(conn,
                    "SELECT o.region_id AS rid, oc.category_code AS cc, COUNT(*) AS n "
                    "FROM ordinance_category oc JOIN ordinances o "
                    "ON oc.ordinance_id=o.ordinance_id "
                    "WHERE o.region_id IS NOT NULL AND o.status='active' "
                    "GROUP BY o.region_id, oc.category_code"):
        out.setdefault(r["rid"], {})[r["cc"]] = int(r["n"])
    return out


def _znorm(values: dict[str, float]) -> dict[str, float]:
    """z-정규화(표준편차 0이면 0). 결측은 호출부에서 제외."""
    if not values:
        return {}
    xs = list(values.values())
    mu = sum(xs) / len(xs)
    var = sum((x - mu) ** 2 for x in xs) / len(xs)
    sd = math.sqrt(var)
    if sd == 0:
        return {k: 0.0 for k in values}
    return {k: (v - mu) / sd for k, v in values.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# --------------------------------------------------------------------------- #
# 1) 유사 지자체 Top-N
# --------------------------------------------------------------------------- #
def find_peer_governments(
    conn: sqlite3.Connection,
    sig_cd: str,
    *,
    k: int = 10,
    features: Iterable[str] = ("budget", "pop", "structure"),
) -> list[dict]:
    """대상 지자체와 특성(예산·인구·정책구조)이 유사한 Top-k 지자체.

    features:
      'budget'    : 지출액 합계(z-정규화 스칼라)
      'pop'       : 인구(z-정규화 스칼라)
      'structure' : 카테고리별 조례수 분포(코사인 유사도 성분)
    스칼라 특성은 유클리드 거리 → 유사도 1/(1+d), 구조는 코사인. 가중 평균으로 종합.
    결측 특성은 해당 지자체에서 제외하고 존재 특성만으로 비교(관대).
    반환: [{sig_cd, region_id, name, similarity, distance, features:{...}}], 유사도 내림차순.
    """
    feats = set(features)
    target = _region_by_sig(conn, sig_cd)
    if not target:
        return []
    level = target.get("level")
    tgt_region_id = target["region_id"]

    # 후보: 동일 level 현행 지자체(자기 제외)
    candidates = _fetch(conn,
                        "SELECT region_id, sig_cd, name, full_name, level, population "
                        "FROM regions WHERE level=? AND status='active'", (level,))
    if not candidates:
        candidates = _fetch(conn,
                            "SELECT region_id, sig_cd, name, full_name, level, population "
                            "FROM regions WHERE level=?", (level,))

    ord_cnt = _active_ordinance_count(conn) if ("structure" in feats or True) else {}
    bud = _budget_total(conn) if "budget" in feats else {}
    catvec = _category_vector(conn) if "structure" in feats else {}
    pop = {r["region_id"]: float(r["population"])
           for r in candidates if r.get("population") is not None} if "pop" in feats else {}

    bud_z = _znorm(bud) if "budget" in feats else {}
    pop_z = _znorm(pop) if "pop" in feats else {}
    cnt_z = _znorm({rid: float(v) for rid, v in ord_cnt.items()}) if "structure" in feats else {}

    def scalar_vec(rid: str) -> dict[str, float]:
        v: dict[str, float] = {}
        if "budget" in feats and rid in bud_z:
            v["budget"] = bud_z[rid]
        if "pop" in feats and rid in pop_z:
            v["pop"] = pop_z[rid]
        if "structure" in feats and rid in cnt_z:
            v["ord_count"] = cnt_z[rid]
        return v

    tv = scalar_vec(tgt_region_id)
    tcat = catvec.get(tgt_region_id, {})

    results = []
    for c in candidates:
        rid = c["region_id"]
        if rid == tgt_region_id:
            continue
        cv = scalar_vec(rid)
        # 공유 성분만 유클리드
        shared = set(tv) & set(cv)
        if shared:
            d = math.sqrt(sum((tv[k] - cv[k]) ** 2 for k in shared))
            scal_sim = 1.0 / (1.0 + d)
        else:
            scal_sim = None
        struct_sim = _cosine(tcat, catvec.get(rid, {})) if "structure" in feats else None

        parts, weights = [], []
        if scal_sim is not None:
            parts.append(scal_sim); weights.append(2.0)
        if struct_sim is not None:
            parts.append(struct_sim); weights.append(1.0)
        if not parts:
            continue
        sim = sum(p * w for p, w in zip(parts, weights)) / sum(weights)

        results.append({
            "sig_cd": c.get("sig_cd"),
            "region_id": rid,
            "name": c.get("name") or c.get("full_name"),
            "similarity": round(sim, 6),
            "distance": round((1.0 / sim - 1.0) if sim > 0 else float("inf"), 6),
            "features": {
                "budget": round(bud.get(rid, 0.0), 2) if "budget" in feats else None,
                "population": int(pop.get(rid)) if ("pop" in feats and rid in pop) else None,
                "ordinance_count": ord_cnt.get(rid, 0),
                "scalar_similarity": round(scal_sim, 6) if scal_sim is not None else None,
                "structure_similarity": round(struct_sim, 6) if struct_sim is not None else None,
            },
        })

    results.sort(key=lambda r: r["similarity"], reverse=True)
    return results[:k]


# --------------------------------------------------------------------------- #
# 2) 조례 커버리지(격차) — 위임 상위법 대비 지자체별 제정/미제정
# --------------------------------------------------------------------------- #
def compare_ordinance_coverage(
    conn: sqlite3.Connection,
    parent_instrument_id: str,
    *,
    region_level: int = 2,
) -> dict:
    """동일 상위법(parent_instrument_id) 위임에 대해 지자체별 조례 제정 여부 매트릭스.

    delegations(child_kind='ordinance', parent_id=parent) → 제정 지자체 집합.
    region_level 전체 지자체 대비 [제정/미제정] 2분.
    반환: {parent_instrument_id, parent_name, region_level, total_regions,
           enacted_count, missing_count, coverage_rate, enacted:[...], missing:[...]}.
    """
    parent = _one(conn, "SELECT instrument_id, name, national_tier FROM legal_instrument "
                        "WHERE instrument_id=?", (parent_instrument_id,))
    parent_name = parent["name"] if parent else None

    enacted_rows = _fetch(conn,
                          "SELECT DISTINCT o.region_id AS region_id, o.ordinance_id AS ordinance_id, "
                          "o.name AS ord_name, o.rr_cls_cd AS rr_cls_cd "
                          "FROM delegations d JOIN ordinances o ON d.child_id=o.ordinance_id "
                          "WHERE d.child_kind='ordinance' AND d.parent_id=? AND o.region_id IS NOT NULL",
                          (parent_instrument_id,))
    enacted_by_region: dict[str, dict] = {}
    for r in enacted_rows:
        enacted_by_region.setdefault(r["region_id"], r)

    all_regions = _fetch(conn,
                         "SELECT region_id, sig_cd, name, full_name FROM regions "
                         "WHERE level=? AND status='active' AND has_legislation=1",
                         (region_level,))
    if not all_regions:
        all_regions = _fetch(conn,
                             "SELECT region_id, sig_cd, name, full_name FROM regions WHERE level=?",
                             (region_level,))

    enacted, missing = [], []
    for reg in all_regions:
        rid = reg["region_id"]
        base = {"region_id": rid, "sig_cd": reg.get("sig_cd"),
                "name": reg.get("name") or reg.get("full_name")}
        if rid in enacted_by_region:
            e = enacted_by_region[rid]
            base.update({"ordinance_id": e["ordinance_id"], "ordinance_name": e["ord_name"],
                         "rr_cls_cd": e.get("rr_cls_cd")})
            enacted.append(base)
        else:
            missing.append(base)

    total = len(all_regions)
    return {
        "parent_instrument_id": parent_instrument_id,
        "parent_name": parent_name,
        "region_level": region_level,
        "total_regions": total,
        "enacted_count": len(enacted),
        "missing_count": len(missing),
        "coverage_rate": round(len(enacted) / total, 4) if total else 0.0,
        "enacted": enacted,
        "missing": missing,
        "as_of_date": _util.today_kst(),
    }


# --------------------------------------------------------------------------- #
# 3) 조례 확산 타임라인
# --------------------------------------------------------------------------- #
def _yyyymmdd_year(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    digits = "".join(ch for ch in str(s) if ch.isdigit())
    if len(digits) >= 4:
        try:
            return int(digits[:4])
        except ValueError:
            return None
    return None


def trace_ordinance_diffusion(
    conn: sqlite3.Connection,
    template: str,
    *,
    since: Optional[str] = None,
    rr_cls_cd: Optional[str] = None,
) -> dict:
    """템플릿(조례명 키워드)에 해당하는 조례들의 지자체별 제정 시계열(확산).

    ordinances.name LIKE %template% → enacted_on(공포일) 오름차순 타임라인.
    since(YYYYMMDD/YYYY-MM-DD) 지정 시 그 이후 제정만. 연도별·누적 곡선 포함.

    주의: enacted_on 은 법령API 자치법규 목록의 '공포일자'로 **현행본 기준**이다.
    일부개정본은 최종 개정 공포일이 들어가므로 무필터 곡선은 '최초 제정 확산'이 아니라
    '현행본 공포일 분포(개정 활동 포함)'를 뜻한다. 최초 제정 확산 곡선이 필요하면
    rr_cls_cd='제정' 을 지정해 제정본만 남겨라(선택 인자, 기본 None=기존 동작 유지).

    반환: {template, count, first_adopter, latest_adopter, by_year, cumulative, timeline:[...]}.
    """
    like = f"%{template}%"
    sql = ("SELECT ordinance_id, region_id, org_name, name, ord_kind, "
           "enacted_on, rr_cls_cd, status FROM ordinances WHERE name LIKE ?")
    params: list[Any] = [like]
    if rr_cls_cd:
        sql += " AND rr_cls_cd=?"
        params.append(rr_cls_cd)
    rows = _fetch(conn, sql + " ORDER BY enacted_on ASC", tuple(params))
    since_digits = "".join(ch for ch in str(since) if ch.isdigit()) if since else None

    timeline = []
    for r in rows:
        ed = r.get("enacted_on")
        ed_digits = "".join(ch for ch in str(ed) if ch.isdigit()) if ed else ""
        if since_digits and ed_digits and ed_digits < since_digits:
            continue
        # 지역명 보강
        reg = _one(conn, "SELECT sig_cd, name FROM regions WHERE region_id=?",
                   (r["region_id"],)) if r.get("region_id") else None
        timeline.append({
            "ordinance_id": r["ordinance_id"],
            "region_id": r.get("region_id"),
            "sig_cd": reg.get("sig_cd") if reg else None,
            "region_name": (reg.get("name") if reg else None) or r.get("org_name"),
            "name": r.get("name"),
            "ord_kind": r.get("ord_kind"),
            "enacted_on": ed,
            "rr_cls_cd": r.get("rr_cls_cd"),
            "status": r.get("status"),
        })

    # 정렬(제정일 숫자 기준; 결측은 뒤로)
    timeline.sort(key=lambda t: ("".join(ch for ch in str(t["enacted_on"] or "") if ch.isdigit()) or "99999999"))

    by_year: dict[str, int] = {}
    for t in timeline:
        y = _yyyymmdd_year(t["enacted_on"])
        if y is not None:
            by_year[str(y)] = by_year.get(str(y), 0) + 1

    cumulative = []
    run = 0
    for y in sorted(by_year):
        run += by_year[y]
        cumulative.append({"year": y, "count": by_year[y], "cumulative": run})

    return {
        "template": template,
        "since": since,
        "rr_cls_cd": rr_cls_cd,
        "count": len(timeline),
        "first_adopter": timeline[0] if timeline else None,
        "latest_adopter": timeline[-1] if timeline else None,
        "by_year": by_year,
        "cumulative": cumulative,
        "timeline": timeline,
        "as_of_date": _util.today_kst(),
    }


# --------------------------------------------------------------------------- #
# 4) 위임 격차 — 위임 있으나 조례 부재
# --------------------------------------------------------------------------- #
def get_delegation_gap(conn: sqlite3.Connection, region_id: str) -> list[dict]:
    """해당 지자체가 미이행한 위임(다른 지자체는 제정했으나 이 지자체엔 조례 없음).

    mandatory(필수위임) 우선 정렬 + 전국 채택 지자체 수로 규범성 신호 제공.
    반환: [{parent_instrument_id, parent_name, delegation_type, mandatory,
            adopted_by_regions, national_tier}], 우선순위 내림차순.
    """
    # 조례로 위임되는 상위법 전수(+ mandatory 여부 + 채택 지자체 수)
    parents = _fetch(conn,
                     "SELECT d.parent_id AS parent_id, "
                     "MAX(CASE WHEN d.delegation_type='mandatory' THEN 1 ELSE 0 END) AS mandatory, "
                     "COUNT(DISTINCT o.region_id) AS adopters "
                     "FROM delegations d JOIN ordinances o ON d.child_id=o.ordinance_id "
                     "WHERE d.child_kind='ordinance' AND o.region_id IS NOT NULL "
                     "GROUP BY d.parent_id")
    if not parents:
        return []

    have = {r["parent_id"] for r in _fetch(conn,
            "SELECT DISTINCT d.parent_id AS parent_id "
            "FROM delegations d JOIN ordinances o ON d.child_id=o.ordinance_id "
            "WHERE d.child_kind='ordinance' AND o.region_id=?", (region_id,))}

    gaps = []
    for p in parents:
        pid = p["parent_id"]
        if pid in have:
            continue
        inst = _one(conn, "SELECT name, national_tier, kind FROM legal_instrument "
                          "WHERE instrument_id=?", (pid,))
        gaps.append({
            "parent_instrument_id": pid,
            "parent_name": inst["name"] if inst else None,
            "instrument_kind": inst["kind"] if inst else None,
            "national_tier": inst["national_tier"] if inst else None,
            "delegation_type": "mandatory" if int(p["mandatory"] or 0) else "law-delegated",
            "mandatory": bool(int(p["mandatory"] or 0)),
            "adopted_by_regions": int(p["adopters"] or 0),
        })

    gaps.sort(key=lambda g: (g["mandatory"], g["adopted_by_regions"]), reverse=True)
    return gaps


# --------------------------------------------------------------------------- #
# 5) 공간 자기상관 — Moran's I / LISA (순수파이썬)
# --------------------------------------------------------------------------- #
def _spatial_weights(conn) -> dict[str, set[str]]:
    """region_id → 인접 region_id 집합(양방향 저장 전제, 대칭 보정)."""
    W: dict[str, set[str]] = {}
    for r in _fetch(conn, "SELECT region_id, neighbor_id FROM region_adjacency"):
        a, b = r["region_id"], r["neighbor_id"]
        if not a or not b or a == b:
            continue
        W.setdefault(a, set()).add(b)
        W.setdefault(b, set()).add(a)  # 대칭 보정
    return W


def _metric_values(conn, metric: str) -> dict[str, float]:
    """지원 metric → region_id:값.
    'ordinance_count' | 'budget_total' | 'budget_per_capita' | 'population' | 'category:CXX'.
    """
    if metric == "ordinance_count":
        return {k: float(v) for k, v in _active_ordinance_count(conn).items()}
    if metric == "budget_total":
        return _budget_total(conn)
    if metric == "population":
        return {r["region_id"]: float(r["population"])
                for r in _fetch(conn, "SELECT region_id, population FROM regions "
                                      "WHERE population IS NOT NULL")}
    if metric == "budget_per_capita":
        bud = _budget_total(conn)
        pop = {r["region_id"]: float(r["population"])
               for r in _fetch(conn, "SELECT region_id, population FROM regions "
                                     "WHERE population IS NOT NULL AND population>0")}
        return {rid: bud[rid] / pop[rid] for rid in bud if rid in pop}
    if metric.startswith("category:"):
        code = metric.split(":", 1)[1]
        out: dict[str, float] = {}
        for r in _fetch(conn,
                        "SELECT o.region_id AS rid, COUNT(*) AS n FROM ordinance_category oc "
                        "JOIN ordinances o ON oc.ordinance_id=o.ordinance_id "
                        "WHERE oc.category_code=? AND o.region_id IS NOT NULL "
                        "AND o.status='active' GROUP BY o.region_id", (code,)):
            out[r["rid"]] = float(r["n"])
        return out
    # 미지원 metric → 빈
    return {}


def _moran_i(values: dict[str, float], W: dict[str, set[str]]) -> tuple[float, list[str], float, float]:
    """전역 Moran's I 계산. 반환 (I, 노드순서, 평균, S0)."""
    nodes = [n for n in values if n in W and W[n]]
    n = len(nodes)
    if n < 3:
        return float("nan"), nodes, 0.0, 0.0
    idx = {nd: i for i, nd in enumerate(nodes)}
    x = [values[nd] for nd in nodes]
    mean = sum(x) / n
    z = [xi - mean for xi in x]
    denom = sum(zi * zi for zi in z)
    if denom == 0:
        return float("nan"), nodes, mean, 0.0
    s0 = 0.0
    num = 0.0
    nodeset = set(nodes)
    for a in nodes:
        za = z[idx[a]]
        for b in W[a]:
            if b in nodeset:
                s0 += 1.0
                num += za * z[idx[b]]
    if s0 == 0:
        return float("nan"), nodes, mean, 0.0
    I = (n / s0) * (num / denom)
    return I, nodes, mean, s0


def compute_spatial_autocorrelation(
    conn: sqlite3.Connection,
    metric: str,
    *,
    method: str = "moran",
    permutations: int = 999,
    seed: int = 2026,
) -> dict:
    """region_adjacency 가중 공간자기상관.

    method='moran': 전역 Moran's I + 순열 검정(pseudo p, z_sim).
    method='lisa' : 전역 + 지역 Moran's I_i(사분면 HH/LL/HL/LH) 포함.
    numpy 없이 순수파이썬으로 동작. permutations 순열로 유의성 근사(결정적 seed).
    반환: {method, metric, n, moran_i, expected_i, mean, p_sim, z_sim, permutations, [lisa]}.
    """
    values = _metric_values(conn, metric)
    W = _spatial_weights(conn)
    I, nodes, mean, s0 = _moran_i(values, W)
    n = len(nodes)
    base = {
        "method": method,
        "metric": metric,
        "n": n,
        "moran_i": None if (isinstance(I, float) and math.isnan(I)) else round(I, 6),
        "expected_i": round(-1.0 / (n - 1), 6) if n > 1 else None,
        "mean": round(mean, 6),
        "as_of_date": _util.today_kst(),
    }
    if n < 3 or (isinstance(I, float) and math.isnan(I)):
        base.update({"p_sim": None, "z_sim": None, "permutations": 0,
                     "note": "표본/인접 부족 또는 분산 0 → I 계산 불가"})
        return base

    # 순열 검정
    rng = random.Random(seed)
    x = [values[nd] for nd in nodes]
    idx = {nd: i for i, nd in enumerate(nodes)}
    nodeset = set(nodes)

    def moran_from(vec: list[float]) -> float:
        m = sum(vec) / n
        z = [v - m for v in vec]
        denom = sum(zi * zi for zi in z)
        if denom == 0:
            return float("nan")
        num = 0.0
        for a in nodes:
            za = z[idx[a]]
            for b in W[a]:
                if b in nodeset:
                    num += za * z[idx[b]]
        return (n / s0) * (num / denom)

    perm_Is = []
    for _ in range(max(0, permutations)):
        shuffled = x[:]
        rng.shuffle(shuffled)
        pi = moran_from(shuffled)
        if not math.isnan(pi):
            perm_Is.append(pi)

    if perm_Is:
        E = base["expected_i"] or 0.0
        if I >= E:
            extreme = sum(1 for pi in perm_Is if pi >= I)
        else:
            extreme = sum(1 for pi in perm_Is if pi <= I)
        p_sim = (extreme + 1) / (len(perm_Is) + 1)
        pmean = sum(perm_Is) / len(perm_Is)
        pvar = sum((pi - pmean) ** 2 for pi in perm_Is) / len(perm_Is)
        psd = math.sqrt(pvar)
        z_sim = (I - pmean) / psd if psd > 0 else None
        base.update({"p_sim": round(p_sim, 5),
                     "z_sim": round(z_sim, 4) if z_sim is not None else None,
                     "permutations": len(perm_Is)})
    else:
        base.update({"p_sim": None, "z_sim": None, "permutations": 0})

    if method == "lisa":
        # 지역 Moran's I_i = z_i * Σ_j w_ij z_j / m2  (m2 = Σ z^2 / n)
        z = [values[nd] - mean for nd in nodes]
        m2 = sum(zi * zi for zi in z) / n
        lisa = []
        for a in nodes:
            za = z[idx[a]]
            neigh = [b for b in W[a] if b in nodeset]
            if not neigh:
                continue
            lag = sum(z[idx[b]] for b in neigh) / len(neigh)  # 표준화 공간지연(평균)
            local_i = (za / m2) * sum(z[idx[b]] for b in neigh) if m2 > 0 else 0.0
            quad = ("HH" if za >= 0 and lag >= 0 else
                    "LL" if za < 0 and lag < 0 else
                    "HL" if za >= 0 and lag < 0 else "LH")
            reg = _one(conn, "SELECT sig_cd, name FROM regions WHERE region_id=?", (a,))
            lisa.append({
                "region_id": a,
                "sig_cd": reg.get("sig_cd") if reg else None,
                "name": reg.get("name") if reg else None,
                "value": round(values[a], 4),
                "local_i": round(local_i, 6),
                "spatial_lag": round(lag, 6),
                "quadrant": quad,
            })
        lisa.sort(key=lambda d: abs(d["local_i"]), reverse=True)
        base["lisa"] = lisa
    return base


# --------------------------------------------------------------------------- #
# 6) 조례 ↔ 예산 링크
#    채널 3종(이름유사 / 조문근거 / 도메인주제) × 게이트 3종(도메인명사·분야·부서)
# --------------------------------------------------------------------------- #
def _norm_name(s: Optional[str]) -> str:
    return _util.compact(s or "")


# 조례명/사업명 정규화 — 한국어 자치법규 명명 관행 반영.
# 조례명은 "<지자체명> <핵심> (에 관한|을 위한)? 조례" 꼴이라 접두·접미를 걷어내야
# 세부사업명(dbiz_nm)과 의미 있는 비교가 된다. [실측: 접두 미제거 시 유사도 전건 0.5 미만]
_ORD_SUFFIX = re.compile(
    r"(에\s*관한|을\s*위한|에\s*대한)?\s*(설치\s*및\s*운영\s*)?(조례|규칙)\s*$")
# 세부사업명 꼬리표: 재원·사업구분 표기. 연속 반복 제거("…(보조)(전환사업)").
_BUDGET_SUFFIX = re.compile(
    r"\((보조|자체|국비|시비|도비|구비|기금|균특|전환사업|계속|신규|주민참여예산|"
    r"주민참여|공모|위탁|민간위탁|정부보조)\)\s*$")
_ORD_STOPWORD = re.compile(r"(지원|운영|관리|사업|증진|육성|촉진|장려)$")


def _norm_ordinance_name(name: Optional[str], region_name: Optional[str] = None) -> str:
    """조례명에서 지자체 접두와 '…에 관한 조례' 접미를 제거해 핵심어만 남긴다."""
    s = _util.compact(name or "")
    if not s:
        return ""
    if region_name:
        for tok in _util.compact(region_name).split():
            if tok and s.startswith(tok):
                s = s[len(tok):].strip()
    # 지자체 접두가 region_name 으로 안 걸린 경우의 보정(시/군/구/도/특별시 등으로 끝나는 선두 토큰)
    parts = s.split()
    while parts and re.search(r"(특별자치도|특별자치시|광역시|특별시|직할시|[시군구도])$", parts[0]) \
            and len(parts) > 1:
        parts.pop(0)
    s = " ".join(parts)
    return _util.compact(_ORD_SUFFIX.sub("", s))


def _norm_budget_name(name: Optional[str]) -> str:
    """세부사업명에서 재원/사업구분 표기 '(보조)(전환사업)' 등을 반복 제거한다."""
    s = _util.compact(name or "")
    for _ in range(4):
        s2 = _BUDGET_SUFFIX.sub("", s)
        if s2 == s:
            break
        s = s2
    return _util.compact(s)


def _bigrams(s: str) -> set:
    t = s.replace(" ", "")
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else ({t} if t else set())


def name_similarity(ord_core: str, budget_core: str) -> float:
    """조례 핵심어 ↔ 사업명 유사도(0~1).

    한국어 복합어 특성상 순수 difflib 만으로는 어순·조사 차이에 취약하므로
    (a) difflib 비율, (b) 문자 bigram Jaccard, (c) 포함도(짧은 쪽 기준 커버리지)
    의 최댓값을 쓴다. 접미 상투어(지원/운영/사업 등)만 겹치는 허위매칭은
    핵심 명사 교집합이 없으면 감점한다.
    """
    if not ord_core or not budget_core:
        return 0.0
    ratio = SequenceMatcher(None, ord_core, budget_core).ratio()
    ga, gb = _bigrams(ord_core), _bigrams(budget_core)
    if not ga or not gb:
        return ratio
    inter = ga & gb
    jacc = len(inter) / len(ga | gb)
    cover = len(inter) / min(len(ga), len(gb))
    score = max(ratio, jacc, cover * 0.95)
    # 상투어만 공유하면 감점(예: '…지원' vs '…지원')
    a_core = _ORD_STOPWORD.sub("", ord_core.replace(" ", ""))
    b_core = _ORD_STOPWORD.sub("", budget_core.replace(" ", ""))
    if not (_bigrams(a_core) & _bigrams(b_core)):
        score *= 0.4
    return round(min(score, 1.0), 4)


# --------------------------------------------------------------------------- #
# 6-a) 정책 도메인 명사 사전
#   key   = 표면형(부분문자열로 탐지)
#   value = 상위(hypernym) 토큰들. 함께 방출되어 하위어↔상위어 매칭을 성립시킨다.
#           예) '반려견' → ('반려동물','동물') 이므로 "반려견 놀이터" 사업이
#               "동물 보호 조례"와 연결된다. 반대로 '야생동물' → ('동물',) 만이라
#               조문에 '반려동물'만 있는 조례와는 커버리지가 낮게 나온다.
#   ※ '지원/관리/사업/운영' 같은 기능어는 의도적으로 제외 — 이것만으로는 매칭 불가.
# --------------------------------------------------------------------------- #
_DOMAIN_LEXICON: dict[str, tuple[str, ...]] = {
    # 출산·임신·양육
    "출산": (), "저출산": ("출산",), "산후": ("출산",),
    "산모": ("출산",), "신생아": ("출산",), "임산부": ("출산",), "임신": ("출산",),
    "난임": ("출산",), "모자보건": ("출산",), "다태아": ("출산",),
    "양육": (), "육아": ("양육",), "돌봄": (), "아이돌봄": ("돌봄",),
    "보육": (), "어린이집": ("보육",), "유치원": ("보육",),
    "영유아": (), "아동": (), "어린이": ("아동",), "청소년": (), "학생": (),
    # 대상집단
    "장애": ("장애인",), "장애인": (), "노인": (), "어르신": ("노인",),
    "치매": ("노인",), "청년": (), "여성": (), "한부모": (), "다문화": (),
    "북한이탈": (), "국가유공자": ("보훈",), "보훈": (), "노숙인": (),
    "기초생활": (), "저소득": (), "취약계층": (), "자활": (), "일자리": (),
    "소상공인": (), "중소기업": (), "전통시장": (), "창업": (), "농업": (),
    "임업": (), "산림": ("임업",), "어업": (), "축산": (),
    # 동물
    "동물": (), "반려동물": ("동물",), "반려견": ("반려동물", "동물"),
    "반려묘": ("반려동물", "동물"), "유기동물": ("동물",), "유실동물": ("동물",),
    "길고양이": ("동물",), "야생동물": ("동물",),
    # 보건·의료
    "보건": (), "의료": ("보건",), "건강": (), "정신건강": ("건강",),
    "감염병": ("보건",), "방역": ("보건",), "금연": ("건강",), "예방접종": ("보건",),
    "응급": ("보건",), "한의약": ("보건",),
    # 환경·에너지·안전
    "환경": (), "폐기물": ("환경",), "재활용": ("환경",), "청소": ("환경",),
    "대기": ("환경",), "미세먼지": ("환경",), "수질": ("환경",), "상수도": (),
    "하수도": (), "기후": ("환경",), "탄소": ("환경",), "에너지": (),
    "공원": (), "녹지": ("공원",), "하천": (), "가로수": (),
    "재난": (), "안전": (), "소방": ("재난",), "방재": ("재난",), "민방위": ("재난",),
    # 도시·교통
    "교통": (), "대중교통": ("교통",), "버스": ("교통",), "주차": ("교통",),
    "도로": (), "자전거": ("교통",), "보행": ("교통",), "주택": (), "임대주택": ("주택",),
    "도시재생": (), "재개발": (), "재건축": (), "한옥": (), "빈집": ("주택",),
    # 문화·교육
    "문화": (), "예술": ("문화",), "체육": (), "관광": (), "축제": ("문화",),
    "도서관": (), "박물관": ("문화",), "문화재": (), "국가유산": ("문화재",),
    "교육": (), "평생교육": ("교육",), "급식": (), "장학": ("교육",),
    # 행정·기타
    "자원봉사": (), "마을": (), "주민자치": (), "인권": (), "정보화": (),
    "데이터": ("정보화",), "복지": (),
}
_DOMAIN_KEYS: tuple[str, ...] = tuple(
    sorted(_DOMAIN_LEXICON, key=len, reverse=True))

# 조례 카테고리 → 허용 예산 '분야(field)'. 불일치 시 하드배제가 아니라 감점(계수).
_CATEGORY_FIELD_MAP: dict[str, frozenset[str]] = {
    "C-BIRTH": frozenset({"사회복지", "보건", "교육", "기타"}),
    "C-PET": frozenset({"농림해양수산", "환경", "보건", "기타"}),
}
# 카테고리가 없거나 미등록일 때의 폴백 — 조례 핵심 도메인명사 → 허용 분야.
_NOUN_FIELD_MAP: dict[str, frozenset[str]] = {
    "출산": frozenset({"사회복지", "보건", "교육", "기타"}),
    "양육": frozenset({"사회복지", "보건", "교육", "기타"}),
    "보육": frozenset({"사회복지", "교육", "기타"}),
    "아동": frozenset({"사회복지", "보건", "교육", "기타"}),
    "청소년": frozenset({"사회복지", "문화및관광", "교육", "기타"}),
    "노인": frozenset({"사회복지", "보건", "기타"}),
    "장애인": frozenset({"사회복지", "보건", "교육", "문화및관광", "기타"}),
    "동물": frozenset({"농림해양수산", "환경", "보건", "기타"}),
    "환경": frozenset({"환경", "농림해양수산", "국토및지역개발", "기타"}),
    "교통": frozenset({"교통및물류", "국토및지역개발", "기타"}),
    "문화": frozenset({"문화및관광", "교육", "기타"}),
    "체육": frozenset({"문화및관광", "교육", "기타"}),
    "교육": frozenset({"교육", "문화및관광", "사회복지", "기타"}),
    "보건": frozenset({"보건", "사회복지", "기타"}),
    "건강": frozenset({"보건", "사회복지", "기타"}),
    "일자리": frozenset({"사회복지", "산업ㆍ중소기업및에너지", "기타"}),
    "소상공인": frozenset({"산업ㆍ중소기업및에너지", "기타"}),
    "재난": frozenset({"공공질서및안전", "국토및지역개발", "기타"}),
    "안전": frozenset({"공공질서및안전", "교통및물류", "국토및지역개발", "기타"}),
    "주택": frozenset({"국토및지역개발", "사회복지", "기타"}),
}

_NOUN_CACHE: dict[str, frozenset[str]] = {}


def domain_nouns(text: Optional[str]) -> frozenset[str]:
    """문자열에서 정책 도메인 명사 집합을 뽑는다(상위어 동반 방출, 중첩 허용).

    '지원/관리/사업' 등 기능어는 사전에 없으므로 결코 반환되지 않는다 →
    기능어만 겹치는 조합은 도메인 게이트에서 탈락한다.
    """
    if not text:
        return frozenset()
    key = _util.compact(text)
    hit = _NOUN_CACHE.get(key)
    if hit is not None:
        return hit
    found: set[str] = set()
    for noun in _DOMAIN_KEYS:
        if noun in key:
            found.add(noun)
            found.update(_DOMAIN_LEXICON[noun])
    hit = frozenset(found)
    if len(_NOUN_CACHE) < 200_000:
        _NOUN_CACHE[key] = hit
    return hit


_HEAD_CACHE: dict[frozenset, frozenset] = {}


def _head_nouns(expanded: frozenset) -> frozenset:
    """확장 명사집합에서 '대표(최대) 명사'만 남긴다 — 커버리지 분모용.

    domain_nouns() 는 부분문자열 탐지 + 상위어 동반 방출이라 한 개념이 여러 토큰으로
    중복 계상된다(예 '장애인' → {장애, 장애인}, '반려견' → {반려견, 반려동물, 동물}).
    분모에 중복이 들어가면 커버리지가 부풀려져 오매칭을 살려준다(실측: 장애인 사례
    ord_cov 0.44 → 0.62). 그래서 ① 다른 명사의 진부분문자열이거나 ② 다른 명사의
    상위어로 방출된 토큰은 제거한다.
    """
    if len(expanded) <= 1:
        return expanded
    hit = _HEAD_CACHE.get(expanded)
    if hit is not None:
        return hit
    drop: set[str] = set()
    for y in expanded:
        for anc in _DOMAIN_LEXICON.get(y, ()):
            # y 가 상위어의 부분문자열이면 y 쪽이 오히려 파생 표면형('장애'→'장애인')
            # 이므로 상위어를 지우면 안 된다. 이 경우는 아래 부분문자열 규칙이 처리.
            if anc in expanded and y not in anc:
                drop.add(anc)
        for x in expanded:
            if x != y and len(x) < len(y) and x in y:
                drop.add(x)
    hit = frozenset(expanded - drop) or expanded
    if len(_HEAD_CACHE) < 100_000:
        _HEAD_CACHE[expanded] = hit
    return hit


def _idf(df: dict[str, int], n_docs: int) -> dict[str, float]:
    """스무딩 IDF. 희귀 명사(=변별력 큰 명사)에 큰 가중."""
    return {k: math.log((n_docs + 1) / (v + 1)) + 1.0 for k, v in df.items()}


def _idf_of(idf: dict[str, float], key: str, n_docs: int) -> float:
    return idf.get(key, math.log(n_docs + 1) + 1.0)


_HYPERNYM_CREDIT = 0.6   # 상위어로만 맞은 경우의 부분점수(예 산후→출산)


def _noun_cov(head: Iterable[str], other_expanded: frozenset,
              idf: dict[str, float], n_docs: int) -> float:
    """IDF 가중 도메인명사 커버리지.

    head 의 각 대표명사가 상대편 확장집합에 (a) 그대로 있으면 1.0,
    (b) 상위어 경로로만 닿으면 _HYPERNYM_CREDIT, (c) 아니면 0 을 받는다.
    """
    tot = 0.0
    hit = 0.0
    for k in set(head):
        w = _idf_of(idf, k, n_docs)
        tot += w
        if k in other_expanded:
            hit += w
        elif any(a in other_expanded for a in _DOMAIN_LEXICON.get(k, ())):
            hit += w * _HYPERNYM_CREDIT
    return (hit / tot) if tot > 0 else 0.0


def _weighted_cov(sub: Iterable[str], full: Iterable[str],
                  idf: dict[str, float], n_docs: int) -> float:
    """IDF 가중 커버리지 = Σidf(교집합) / Σidf(전체). full 이 비면 0."""
    tot = 0.0
    hit = 0.0
    subset = set(sub)
    for k in set(full):
        w = _idf_of(idf, k, n_docs)
        tot += w
        if k in subset:
            hit += w
    return (hit / tot) if tot > 0 else 0.0


def _lcs_len(needle: str, haystack: str, *, cap: int = 18) -> int:
    """needle 의 부분문자열 중 haystack 에 등장하는 최장 길이(문자 수).

    파이썬 `in` (C 구현 substring search)만 사용 → 조문 코퍼스(수천자)에도 충분히 빠르다.
    """
    if not needle or not haystack:
        return 0
    top = min(len(needle), cap)
    for length in range(top, 2, -1):
        for i in range(0, len(needle) - length + 1):
            if needle[i:i + length] in haystack:
                return length
    return 0


def _ensure_link_columns(conn) -> bool:
    """ordinance_budget_link.evidence 컬럼 보장(스키마 무변경 배포 호환)."""
    try:
        cols = set(_db.table_columns(conn, "ordinance_budget_link"))
    except sqlite3.OperationalError:
        return False
    if "evidence" in cols:
        return True
    try:
        conn.execute("ALTER TABLE ordinance_budget_link ADD COLUMN evidence TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        return False
    _db._COL_CACHE.pop(id(conn), None)
    return "evidence" in set(_db.table_columns(conn, "ordinance_budget_link"))


# --------------------------------------------------------------------------- #
# 6-b) 지역 단위 예산 통계(도메인명사 DF/IDF, bigram IDF, 부서 프로파일)
# --------------------------------------------------------------------------- #
class _RegionBudgetIndex:
    """한 지자체 예산행 집합에 대한 색인. 조례 1건당 재계산하지 않도록 캐시."""

    __slots__ = ("rows", "n_docs", "noun_idf", "bg_idf", "dept_noun_mass",
                 "noun_total_mass", "cores", "nouns")

    def __init__(self, rows: list[dict]):
        self.rows = rows
        names: list[str] = []
        self.cores: dict[str, str] = {}
        self.nouns: dict[str, frozenset[str]] = {}
        seen: set[str] = set()
        noun_df: dict[str, int] = {}
        bg_df: dict[str, int] = {}
        dept_mass: dict[str, dict[str, float]] = {}
        for r in rows:
            raw = r.get("dbiz_nm") or ""
            core = _norm_budget_name(raw)
            self.cores[r["budget_id"]] = core
            ns = domain_nouns(core)
            self.nouns[r["budget_id"]] = ns
            if core in seen:
                continue
            seen.add(core)
            names.append(core)
            for n in ns:
                noun_df[n] = noun_df.get(n, 0) + 1
            for g in _bigrams(core):
                bg_df[g] = bg_df.get(g, 0) + 1
        self.n_docs = max(len(names), 1)
        self.noun_idf = _idf(noun_df, self.n_docs)
        self.bg_idf = _idf(bg_df, self.n_docs)
        # 부서(dept_cd) 프로파일: dept_cd 별 도메인명사 질량(IDF 가중 등장 수)
        noun_total: dict[str, float] = {}
        for r in rows:
            d = r.get("dept_cd")
            if not d:
                continue
            bucket = dept_mass.setdefault(d, {})
            for n in self.nouns[r["budget_id"]]:
                w = _idf_of(self.noun_idf, n, self.n_docs)
                bucket[n] = bucket.get(n, 0.0) + w
                noun_total[n] = noun_total.get(n, 0.0) + w
        self.dept_noun_mass = dept_mass
        self.noun_total_mass = noun_total

    def infer_dept_cd(self, dept_name: Optional[str],
                      *, margin: float = 1.5) -> Optional[str]:
        """조례 담당부서명(예 '동물보호팀') → 같은 지자체 dept_cd 추정.

        LOFIN 은 dept_cd(코드)만 주고 부서명을 주지 않는다. 그래서 부서명을 직접
        대조할 수 없다 → dept_cd 별 세부사업명 군집의 '도메인명사 질량 점유율'로
        역추정한다. 예: '동물보호팀' → 명사 {동물} → 동물 질량의 83%를 가진
        dept_cd 를 담당부서로 본다. 1·2위 격차가 margin 배 미만이면 추정 포기(None).
        """
        ns = domain_nouns(dept_name)
        if not ns or not self.dept_noun_mass:
            return None
        scores: dict[str, float] = {}
        for dept, bucket in self.dept_noun_mass.items():
            s = 0.0
            for n in ns:
                tot = self.noun_total_mass.get(n, 0.0)
                if tot > 0:
                    s += bucket.get(n, 0.0) / tot
            if s > 0:
                scores[dept] = s
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best, best_s = ranked[0]
        if best_s < 0.25:
            return None
        if len(ranked) > 1 and ranked[1][1] > 0 and best_s < ranked[1][1] * margin:
            return None
        return best


def _allowed_fields(category_code: Optional[str],
                    ord_nouns: Iterable[str]) -> Optional[frozenset[str]]:
    """조례 카테고리(우선) 또는 핵심 도메인명사(폴백) → 허용 예산 분야 집합."""
    if category_code and category_code in _CATEGORY_FIELD_MAP:
        return _CATEGORY_FIELD_MAP[category_code]
    acc: set[str] = set()
    for n in ord_nouns:
        fs = _NOUN_FIELD_MAP.get(n)
        if fs:
            acc |= fs
    return frozenset(acc) if acc else None


# --------------------------------------------------------------------------- #
# 6-c) 페어 스코어링
# --------------------------------------------------------------------------- #
# 튜닝 상수(종로구 정답셋 24쌍 기준 조정, RUNBOOK 재현 가능)
_DOM_FLOOR = 0.55          # 도메인 커버리지 0일 때의 계수 하한(게이트 통과 전제)
_FIELD_PENALTY = 0.60      # 분야 게이트 불일치 감점
_DEPT_BONUS = 1.12         # 부서 추정 일치 가산
_TOPIC_BASE = 0.62         # 도메인주제 단독 채널 기본점
_TOPIC_CAP = 0.55          # 도메인주제 단독 채널 상한(이름·조문 근거보다 항상 약함)
_ART_MIN_LCS = 3           # 조문 근거로 인정하는 최소 연속 일치 길이
_TOPIC_MIN_BI = 0.50       # 주제채널 최소 조문 bigram 근거(하위개념이 아닐 때)


def score_ordinance_budget(
    *,
    ord_core: str,
    ord_title_nouns: frozenset,
    ord_scope_nouns: frozenset,
    article_text: str,
    article_bigrams: frozenset,
    budget_core: str,
    budget_nouns: frozenset,
    budget_field: Optional[str],
    budget_dept_cd: Optional[str],
    allowed_fields: Optional[frozenset],
    inferred_dept_cd: Optional[str],
    idx: "_RegionBudgetIndex",
) -> Optional[dict]:
    """조례-예산 1쌍 점수. 게이트 탈락 시 None.

    채널
      name    : 명칭 유사도(name_similarity)
      article : 조문 본문에 사업명 표현이 실재하는가(연속일치 + IDF가중 bigram 커버)
      topic   : 도메인 명사만으로 성립하는 약한 주제 연결(상한 _TOPIC_CAP)
    게이트
      domain  : 조례 제목 도메인명사 ∩ 사업명 도메인명사 = ∅ 이면 즉시 배제
      field   : 카테고리/명사→허용분야 불일치 시 감점(_FIELD_PENALTY)
      dept    : dept_cd 추정 일치 시 가산(_DEPT_BONUS, 감점 없음 — 추정치이므로)
    """
    if not ord_core or not budget_core:
        return None
    shared = ord_title_nouns & budget_nouns
    if not shared:
        return None  # 게이트 1: 핵심 도메인명사 필수 교집합

    n = idx.n_docs
    # ord_cov: 조례가 다루는 대상을 그 사업이 감당하는가 (분모=조례 제목 대표명사)
    # bud_cov: 그 사업의 대상이 조례 적용범위(제목+조문) 안에 있는가 (분모=사업명 대표명사)
    ord_head = _head_nouns(ord_title_nouns)
    bud_head = _head_nouns(budget_nouns)
    ord_cov = _noun_cov(ord_head, budget_nouns, idx.noun_idf, n)
    bud_cov = _noun_cov(bud_head, ord_scope_nouns, idx.noun_idf, n)
    dom = min(ord_cov, bud_cov)
    domf = _DOM_FLOOR + (1.0 - _DOM_FLOOR) * dom

    fldf = 1.0
    field_ok = True
    if allowed_fields is not None and budget_field:
        if budget_field not in allowed_fields:
            fldf = _FIELD_PENALTY
            field_ok = False

    # 부서 일치는 '추정'이므로 가산만 하고, 주제 일치도(dom)에 비례해서만 준다.
    # (dom 이 낮은데 부서만 같아서 문턱을 넘는 오매칭 방지 — 장애인복지팀 사례)
    dept_ok = bool(inferred_dept_cd and budget_dept_cd
                   and inferred_dept_cd == budget_dept_cd)
    deptf = (1.0 + (_DEPT_BONUS - 1.0) * dom) if dept_ok else 1.0

    s_name_raw = name_similarity(ord_core, budget_core)
    s_name = s_name_raw * domf * fldf * deptf

    lcs = _lcs_len(budget_core, article_text) if article_text else 0
    art_bi = 0.0
    if article_bigrams:
        bg = _bigrams(budget_core)
        if bg:
            art_bi = _weighted_cov(article_bigrams, bg, idx.bg_idf, n)
    s_art_raw = 0.0
    if lcs >= _ART_MIN_LCS:
        lcs_f = min(1.0, (lcs - 2) / 6.0)
        # bi 를 볼록(^1.6)하게 쓰는 이유: 사업명의 '변별력 있는 부분'이 조문에 거의 다
        # 나와야 근거로 인정한다. 일부만 걸치는 경우(예 '고위험임산부의료비' — 조례는
        # 고위험 임산부 가사도우미만 규정, '의료비'는 조문에 없음)를 떨어뜨린다.
        s_art_raw = (art_bi ** 1.6) * (0.45 + 0.55 * lcs_f)
    s_art = s_art_raw * domf * fldf * deptf

    # 주제 채널: 이름·조문 근거가 없어도 '같은 도메인 + 담당부서 일치'면 약한 연결을
    # 인정한다. 단 부서 추정이 일치하거나 도메인이 거의 완전 일치할 때만 발화한다.
    # (예: 동물보호조례 ↔ '찾아가는 반려동물 이동 목욕 서비스' 는 이름·조문 근거가
    #  약하지만 같은 부서·같은 도메인이라 실제로 관련 사업이다. 반면 '유해야생동물
    #  관리'는 다른 부서 + 조문에 없는 대상이라 여기서 걸러진다.)
    # 추가 조건: 사업 대상이 조례 '제목 주제어의 하위개념'이거나(반려견 ⊂ 동물),
    # 사업명 표현이 조문에 충분히 등장할 것.
    #  - 상위개념 그대로 + 조문 흔적 없음  → 배제('동물질병에 의한 피해 예방')
    #  - 형제개념(임신 ↔ 산후, 둘 다 출산 하위) → 하위개념이 아니므로 배제
    #    ('임신 사전건강관리' vs 산후건강관리 조례)
    specialized = any(
        set(_DOMAIN_LEXICON.get(h, ())) & ord_head for h in bud_head)
    s_topic = 0.0
    if (dept_ok or dom >= 0.95) and (specialized or art_bi >= _TOPIC_MIN_BI):
        s_topic = min(_TOPIC_CAP, _TOPIC_BASE * domf) * fldf

    channels = (("name+domain", s_name), ("article-evidence", s_art),
                ("domain-topic", s_topic))
    method, conf = max(channels, key=lambda t: t[1])
    if s_name >= 0.5 and s_art >= 0.5:
        conf = min(1.0, conf + 0.05)
        method = "name+article"
    if method != "domain-topic":
        if dept_ok:
            method += "+dept"
        if not field_ok:
            method += "-fieldpenalty"
    elif not field_ok:
        method += "-fieldpenalty"

    return {
        "method": method,
        "confidence": round(min(conf, 1.0), 4),
        "evidence": {
            "shared_nouns": sorted(shared),
            "ord_cov": round(ord_cov, 3),
            "bud_cov": round(bud_cov, 3),
            "dom": round(dom, 3),
            "name": round(s_name_raw, 3),
            "art_lcs": lcs,
            "art_bigram_cov": round(art_bi, 3),
            "art": round(s_art_raw, 3),
            "topic": round(s_topic, 3),
            "field": budget_field,
            "field_ok": field_ok,
            "dept_cd": budget_dept_cd,
            "dept_match": dept_ok,
        },
    }


def _audit_block(evidence_json: Optional[str]) -> Optional[dict]:
    """기존 evidence JSON 에서 수작업 판정 기록(audit)만 뽑아낸다.

    재계산은 점수·근거를 새로 쓰지만 사람이 남긴 판정 기록은 재생성할 수 없으므로
    반드시 이월해야 한다. 파싱 불가한 evidence 는 None(이월할 것 없음).
    """
    if not evidence_json:
        return None
    try:
        ev = _json.loads(evidence_json)
    except Exception:
        return None
    a = ev.get("audit") if isinstance(ev, dict) else None
    return a if isinstance(a, dict) else None


def _existing_links(conn, scanned_ids: list[str], *, with_evidence: bool) -> dict:
    """스캔 대상 조례의 기존 링크를 (ordinance_id, budget_id) → 튜플로 적재."""
    ev = ", evidence" if with_evidence else ""
    out: dict[tuple, tuple] = {}
    for i in range(0, len(scanned_ids), 300):
        chunk = scanned_ids[i:i + 300]
        ph = ",".join("?" * len(chunk))
        rows = _fetch(conn,
                      "SELECT ordinance_id, budget_id, match_method, confidence, "
                      f"COALESCE(verified,0) AS verified{ev} FROM ordinance_budget_link "
                      f"WHERE ordinance_id IN ({ph})", tuple(chunk))
        for r in rows:
            out[(r["ordinance_id"], r["budget_id"])] = (
                r["match_method"], r["confidence"], int(r["verified"] or 0),
                r.get("evidence") if with_evidence else None)
    return out


def _prune_stale_links(conn, existing: dict, kept: set) -> int:
    """이번 계산에서 살아남지 못한 자동링크 제거.

    upsert 만 하면 알고리즘 개선 전의 오매칭이 테이블에 영구히 남는다(실측: 개선 후에도
    구 'name-similarity' 오매칭이 종로구 4건 조례에만 7건 잔존). 사람이 확인한
    링크(verified≠0 — 정답 1 과 오답 -1 모두)는 건드리지 않고, 이번에 스캔한 조례의
    자동링크(verified=0) 중 결과에 없는 것만 지운다. 오답 판정(-1)을 지우면 다음
    빌드가 같은 오매칭을 무라벨로 재생성해 재판정이 무한 반복된다.
    """
    stale = [k for k, v in existing.items() if int(v[2] or 0) == 0 and k not in kept]
    for i in range(0, len(stale), 500):
        with _db.tx(conn):
            conn.executemany(
                "DELETE FROM ordinance_budget_link "
                "WHERE ordinance_id=? AND budget_id=? AND COALESCE(verified,0)=0",
                stale[i:i + 500])
    return len(stale)


def link_ordinance_budget(
    conn: sqlite3.Connection,
    *,
    min_confidence: float = 0.5,
    per_ordinance_top: int = 5,
    region_id: Optional[str] = None,
    prune: bool = True,
    dry_run: bool = False,
) -> dict:
    """조례 ↔ 세부사업(dbiz_nm) 매칭 → ordinance_budget_link 적재.

    동일 region_id 안에서만 후보 비교(조합폭발 방지). 3채널 점수의 최댓값을 쓰되
    ① 도메인명사 필수 교집합(하드 게이트) ② 분야 게이트 감점 ③ 부서 추정 가산
    ④ 조문 근거(ordinance_articles 본문에 사업명 표현 실재) 를 적용한다.

    region_id 지정 시 그 지자체만(평가·부분 재계산용). dry_run 이면 DB 미기록.
    prune=True 면 이번 계산에서 탈락한 자동링크(verified=0)를 삭제한다.
    반환: {scanned_ordinances, candidate_pairs, gated_pairs, linked, updated,
           unchanged, removed, by_method, min_confidence}.
    """
    where = "WHERE o.region_id IS NOT NULL AND o.status='active'"
    params: tuple = ()
    if region_id:
        where += " AND o.region_id=?"
        params = (region_id,)
    ords = _fetch(conn,
                  "SELECT o.ordinance_id, o.region_id, o.name, o.department, "
                  "       COALESCE(r.full_name, r.name) AS region_name "
                  "FROM ordinances o "
                  "LEFT JOIN regions r ON r.region_id = o.region_id " + where, params)
    if not ords:
        return {"scanned_ordinances": 0, "candidate_pairs": 0, "gated_pairs": 0,
                "linked": 0, "updated": 0, "unchanged": 0, "by_method": {},
                "min_confidence": min_confidence}

    ord_ids = {o["ordinance_id"] for o in ords}
    regions = sorted({o["region_id"] for o in ords})

    # region → 예산행(해당 지자체에 조례가 있는 경우만 로드)
    budgets_by_region: dict[str, list[dict]] = {}
    for rid in regions:
        rows = _fetch(conn,
                      "SELECT budget_id, region_id, dbiz_nm, dept_cd, field, sector, fyr "
                      "FROM budget_lines WHERE region_id=? AND dbiz_nm IS NOT NULL", (rid,))
        if rows:
            budgets_by_region[rid] = rows

    # 조례 → 대표 카테고리
    top_cat: dict[str, str] = {}
    for r in _fetch(conn,
                    "SELECT ordinance_id, category_code FROM ordinance_category "
                    "ORDER BY confidence DESC"):
        top_cat.setdefault(r["ordinance_id"], r["category_code"])

    # 조례 → 조문 본문 코퍼스(compact). 대상 조례만.
    corpus: dict[str, list[str]] = {}
    for r in _fetch(conn,
                    "SELECT ordinance_id, title, body FROM ordinance_articles"):
        oid = r["ordinance_id"]
        if oid not in ord_ids:
            continue
        corpus.setdefault(oid, []).append(
            _util.compact((r.get("title") or "") + (r.get("body") or "")))
    article_text: dict[str, str] = {k: "".join(v) for k, v in corpus.items()}
    # 조문 bigram 집합은 조례당 1회만 만든다(후보쌍마다 만들면 수백만 회 재계산됨).
    article_bg: dict[str, frozenset] = {
        k: frozenset(_bigrams(v)) for k, v in article_text.items()}
    del corpus

    has_evidence_col = False if dry_run else _ensure_link_columns(conn)

    index_cache: dict[str, _RegionBudgetIndex] = {}
    dept_cache: dict[tuple, Optional[str]] = {}
    scanned = 0
    candidate_pairs = 0
    gated_pairs = 0
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    scanned_ids: list[str] = []
    kept: set[tuple] = set()
    by_method: dict[str, int] = {}
    preview: list[dict] = []
    pending: list[dict] = []

    for o in ords:
        rid = o["region_id"]
        cands = budgets_by_region.get(rid)
        if not cands:
            continue
        scanned += 1
        scanned_ids.append(o["ordinance_id"])
        ord_core = _norm_ordinance_name(o["name"], o.get("region_name"))
        if not ord_core:
            continue
        idx = index_cache.get(rid)
        if idx is None:
            idx = index_cache[rid] = _RegionBudgetIndex(cands)
        title_nouns = domain_nouns(ord_core)
        if not title_nouns:
            candidate_pairs += len(cands)
            continue  # 게이트 1: 조례 제목에 도메인 명사가 없으면 매칭 불가
        atext = article_text.get(o["ordinance_id"], "")
        abg = article_bg.get(o["ordinance_id"]) or frozenset()
        scope_nouns = title_nouns | domain_nouns(atext)
        cat = top_cat.get(o["ordinance_id"])
        allowed = _allowed_fields(cat, title_nouns)
        dkey = (rid, o.get("department") or "")
        if dkey not in dept_cache:
            dept_cache[dkey] = idx.infer_dept_cd(o.get("department"))
        inferred_dept = dept_cache[dkey]

        scored: list[tuple[float, dict, dict]] = []
        for b in cands:
            candidate_pairs += 1
            bid = b["budget_id"]
            bn = idx.nouns.get(bid) or frozenset()
            if not (title_nouns & bn):
                continue  # 하드 게이트 — 여기서 대부분 탈락(성능 이점도 큼)
            gated_pairs += 1
            res = score_ordinance_budget(
                ord_core=ord_core,
                ord_title_nouns=title_nouns,
                ord_scope_nouns=scope_nouns,
                article_text=atext,
                article_bigrams=abg,
                budget_core=idx.cores.get(bid, ""),
                budget_nouns=bn,
                budget_field=b.get("field"),
                budget_dept_cd=b.get("dept_cd"),
                allowed_fields=allowed,
                inferred_dept_cd=inferred_dept,
                idx=idx,
            )
            if res and res["confidence"] >= min_confidence:
                scored.append((res["confidence"], b, res))

        scored.sort(key=lambda t: t[0], reverse=True)
        for conf, b, res in scored[:per_ordinance_top]:
            ev = dict(res["evidence"])
            ev["ordinance_core"] = ord_core
            ev["budget_core"] = idx.cores.get(b["budget_id"], "")
            row = {
                "ordinance_id": o["ordinance_id"],
                "budget_id": b["budget_id"],
                "match_method": res["method"],
                "confidence": conf,
                "verified": 0,
                "category_gate": cat,
                "source_fyr": b.get("fyr"),
                "computed_at": _util.now_kst_iso(),
            }
            if has_evidence_col:
                row["evidence"] = _json.dumps(ev, ensure_ascii=False)
            by_method[res["method"]] = by_method.get(res["method"], 0) + 1
            kept.add((o["ordinance_id"], b["budget_id"]))
            pending.append(row)
            preview.append({
                "ordinance_id": o["ordinance_id"],
                "ordinance_name": o["name"],
                "budget_id": b["budget_id"],
                "dbiz_nm": b.get("dbiz_nm"),
                "match_method": res["method"],
                "confidence": conf,
                "evidence": ev,
            })

    removed = 0
    kept_verdicts = 0
    if not dry_run and scanned_ids:
        existing = _existing_links(conn, scanned_ids, with_evidence=has_evidence_col)
        # 값이 그대로면 UPDATE 를 생략한다(computed_at 만 바뀌는 무의미한 쓰기 제거 —
        # 실측: 전수 재계산 시 23,805건이 매번 no-op UPDATE 였다. 동시 쓰기 부하 감소).
        todo = []
        for row in pending:
            prev = existing.get((row["ordinance_id"], row["budget_id"]))
            # 사람이 판정한 링크는 재계산이 덮어쓰면 안 된다. 기본 row 는 verified=0 과
            # 새 evidence 를 담고 있어, 그대로 upsert 하면 수작업 판정(verified=±1)과
            # 판정근거(evidence.audit)가 통째로 소실된다. 실측 사고: build 1회로
            # verified=1 421건→2건, evidence.audit 584건→3건으로 증발했다.
            # → 판정(verified)과 audit 키만 이월하고 점수·근거는 새 값으로 갱신한다.
            if prev is not None and int(prev[2] or 0) != 0:
                row["verified"] = int(prev[2])
                kept_verdicts += 1
                if has_evidence_col:
                    audit = _audit_block(prev[3])
                    if audit is not None:
                        ev_new = _json.loads(row["evidence"])
                        ev_new["audit"] = audit
                        row["evidence"] = _json.dumps(ev_new, ensure_ascii=False)
            if (prev and prev[0] == row["match_method"]
                    and prev[1] is not None and abs(prev[1] - row["confidence"]) < 1e-9
                    and int(prev[2] or 0) == int(row["verified"] or 0)
                    and (not has_evidence_col or prev[3] == row.get("evidence"))):
                counts["unchanged"] += 1
                continue
            todo.append(row)
        # 배치 커밋(다른 에이전트와 동시 쓰기 → 긴 트랜잭션 금지)
        for i in range(0, len(todo), 500):
            with _db.tx(conn):
                for row in todo[i:i + 500]:
                    counts[_db.upsert(conn, "ordinance_budget_link", row,
                                      ("ordinance_id", "budget_id"))] += 1
        if prune:
            removed = _prune_stale_links(conn, existing, kept)

    out = {
        "scanned_ordinances": scanned,
        "candidate_pairs": candidate_pairs,
        "gated_pairs": gated_pairs,
        "linked": counts["inserted"],
        "updated": counts["updated"],
        "unchanged": counts["unchanged"],
        "removed": removed,
        "kept_verdicts": kept_verdicts,
        "by_method": by_method,
        "min_confidence": min_confidence,
    }
    if dry_run:
        out["matches"] = preview
        out["dry_run"] = True
    return out


# --------------------------------------------------------------------------- #
# 7) 커뮤니티 탐지 (networkx greedy_modularity → 순수파이썬 label propagation 폴백)
# --------------------------------------------------------------------------- #
def _load_undirected_edges(conn, scope: str) -> tuple[list[str], dict[str, set[str]], dict[tuple, float]]:
    """scope별 무방향 인접 로드. 반환 (노드리스트, 인접, 가중치)."""
    adj: dict[str, set[str]] = {}
    wt: dict[tuple, float] = {}

    def add(a, b, w=1.0):
        if not a or not b or a == b:
            return
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
        key = (a, b) if a < b else (b, a)
        wt[key] = wt.get(key, 0.0) + w

    if scope == "region_adjacency":
        for r in _fetch(conn, "SELECT region_id, neighbor_id FROM region_adjacency"):
            add(r["region_id"], r["neighbor_id"], 1.0)
    elif scope == "ordinance_similarity":
        for r in _fetch(conn, "SELECT src_id, dst_id, cosine_sim FROM similarity_edges"):
            add(r["src_id"], r["dst_id"], float(r["cosine_sim"] or 1.0))
    else:
        raise ValueError(f"미지원 scope: {scope}")
    return list(adj.keys()), adj, wt


def _label_propagation(nodes: list[str], adj: dict[str, set[str]],
                       *, max_iter: int = 50, seed: int = 2026) -> dict[str, int]:
    """순수파이썬 라벨전파(비동기). 결정적 seed. 반환 node→community_int."""
    rng = random.Random(seed)
    label = {n: i for i, n in enumerate(nodes)}
    order = nodes[:]
    for _ in range(max_iter):
        rng.shuffle(order)
        changed = False
        for n in order:
            neigh = adj.get(n)
            if not neigh:
                continue
            tally: dict[int, int] = {}
            for m in neigh:
                tally[label[m]] = tally.get(label[m], 0) + 1
            best = max(tally.values())
            top = sorted(l for l, c in tally.items() if c == best)
            new = top[0]
            if label[n] != new:
                label[n] = new
                changed = True
        if not changed:
            break
    # 라벨 재번호(0..K-1)
    remap: dict[int, int] = {}
    for n in nodes:
        remap.setdefault(label[n], len(remap))
    return {n: remap[label[n]] for n in nodes}


def _modularity(adj, wt, comm: dict[str, int]) -> float:
    """무방향 가중 모듈러리티 Q."""
    m = sum(wt.values())
    if m == 0:
        return 0.0
    deg: dict[str, float] = {}
    for (a, b), w in wt.items():
        deg[a] = deg.get(a, 0.0) + w
        deg[b] = deg.get(b, 0.0) + w
    q = 0.0
    for (a, b), w in wt.items():
        if comm.get(a) == comm.get(b):
            q += w - deg[a] * deg[b] / (2 * m)
    # 자기루프 없음 가정; 대각 항 보정 생략(근사)
    return q / (2 * m)


def detect_communities(
    conn: sqlite3.Connection,
    *,
    scope: str = "region_adjacency",
    seed: int = 2026,
) -> dict:
    """커뮤니티(군집) 탐지. scope: 'region_adjacency' | 'ordinance_similarity'.

    networkx 있으면 greedy_modularity_communities, 없으면 label propagation 폴백.
    반환: {scope, backend, num_communities, modularity, communities:[{id,size,members[]}]}.
    """
    nodes, adj, wt = _load_undirected_edges(conn, scope)
    if not nodes:
        return {"scope": scope, "backend": "none", "num_communities": 0,
                "modularity": 0.0, "communities": []}

    comm: dict[str, int]
    backend: str
    if _HAS_NX:
        try:
            g = _nx.Graph()
            for (a, b), w in wt.items():
                g.add_edge(a, b, weight=w)
            from networkx.algorithms.community import greedy_modularity_communities  # type: ignore
            parts = greedy_modularity_communities(g, weight="weight")
            comm = {}
            for i, part in enumerate(parts):
                for n in part:
                    comm[n] = i
            # 고립 노드 보정
            for n in nodes:
                comm.setdefault(n, len(comm))
            backend = "networkx-greedy-modularity"
        except Exception:
            comm = _label_propagation(nodes, adj, seed=seed)
            backend = "label-propagation(fallback)"
    else:
        comm = _label_propagation(nodes, adj, seed=seed)
        backend = "label-propagation"

    groups: dict[int, list[str]] = {}
    for n, cid in comm.items():
        groups.setdefault(cid, []).append(n)

    communities = []
    for cid in sorted(groups, key=lambda c: len(groups[c]), reverse=True):
        members = sorted(groups[cid])
        communities.append({"id": cid, "size": len(members), "members": members})

    return {
        "scope": scope,
        "backend": backend,
        "num_communities": len(communities),
        "modularity": round(_modularity(adj, wt, comm), 6),
        "communities": communities,
        "as_of_date": _util.today_kst(),
    }


# --------------------------------------------------------------------------- #
# 8) 지역 프로파일(export/MCP 재사용 편의)
# --------------------------------------------------------------------------- #
def build_region_profile(conn: sqlite3.Connection, sig_cd: str) -> dict:
    """지자체 1개의 종합 프로파일(export 지역 shard/MCP get_region_profile 공용)."""
    reg = _region_by_sig(conn, sig_cd)
    if not reg:
        return {"sig_cd": sig_cd, "found": False}

    rid = reg["region_id"]
    ord_kinds = _fetch(conn,
                       "SELECT ord_kind, COUNT(*) AS n FROM ordinances "
                       "WHERE region_id=? AND status='active' GROUP BY ord_kind", (rid,))
    cats = _fetch(conn,
                  "SELECT oc.category_code AS code, COUNT(*) AS n FROM ordinance_category oc "
                  "JOIN ordinances o ON oc.ordinance_id=o.ordinance_id "
                  "WHERE o.region_id=? AND o.status='active' GROUP BY oc.category_code "
                  "ORDER BY n DESC", (rid,))
    bud = _one(conn,
               "SELECT COUNT(*) AS lines, COALESCE(SUM(exe_amt),0) AS exe, "
               "COALESCE(SUM(budget_now),0) AS now FROM budget_lines WHERE region_id=?", (rid,))
    changes = _fetch(conn,
                     "SELECT change_id, ts, entity_type, entity_id, entity_name, event, "
                     "official_url FROM change_log WHERE region_code=? ORDER BY ts DESC LIMIT 20",
                     (reg.get("sig_cd"),))
    return {
        "sig_cd": reg.get("sig_cd"),
        "region_id": rid,
        "name": reg.get("name"),
        "full_name": reg.get("full_name"),
        "level": reg.get("level"),
        "status": reg.get("status"),
        "population": reg.get("population"),
        "ordinance_kinds": {r["ord_kind"]: int(r["n"]) for r in ord_kinds},
        "ordinance_total": sum(int(r["n"]) for r in ord_kinds),
        "top_categories": [{"code": r["code"], "count": int(r["n"])} for r in cats[:12]],
        "budget": {"lines": int(bud["lines"]) if bud else 0,
                   "exe_amt": int(bud["exe"]) if bud else 0,
                   "budget_now": int(bud["now"]) if bud else 0},
        "recent_changes": changes,
        "as_of_date": reg.get("as_of_date") or _util.today_kst(),
    }


__all__ = [
    "find_peer_governments", "compare_ordinance_coverage", "trace_ordinance_diffusion",
    "get_delegation_gap", "compute_spatial_autocorrelation", "link_ordinance_budget",
    "detect_communities", "build_region_profile",
    # 조례↔예산 매칭 보조(테스트/튜닝용 공개)
    "name_similarity", "domain_nouns", "score_ordinance_budget",
]
