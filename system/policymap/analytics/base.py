"""policymap.analytics.base — 정책확산 계량분석 공용 데이터 계층.

여기서 만드는 것(모두 DB 읽기전용):
  * 위험집합(risk set) 모집단: 조례 제정권을 가진 현행 기초자치단체
  * 공간가중행렬 W: region_adjacency(Queen) + 일반구 인접의 모시(母市) 승격 +
    도서지역 kNN 보정 + 행표준화
  * 지자체 공변량: 인구/면적/예산총액/재정자립(자체재원비율)/사회복지비비율/조례수
  * 조례명 TF-IDF 프로파일(정책구조 특성의 대체재)
  * 템플릿별 채택연도(adoption year) — 제정본 기준 / 상한(upper bound) 기준 2종

설계 원칙
---------
1. **읽기전용**: 본 모듈은 SELECT 만 한다(materialize_region_features 만 예외이며
   짧은 단일 트랜잭션으로 쓴다). 대용량 수집 잡과의 WAL 경합을 피하기 위함.
2. **결측을 숨기지 않는다**: 모든 빌더는 excluded/coverage 메타를 함께 돌려준다.
3. 표준라이브러리 + numpy 만으로 동작한다(shapely 있으면 면적 정확, 없으면 폴백).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Any, Iterable, Optional

try:  # 선택: 면적 계산 정밀화
    from shapely.geometry import shape as _shp_shape  # type: ignore
    _HAS_SHAPELY = True
except Exception:  # pragma: no cover
    _shp_shape = None  # type: ignore
    _HAS_SHAPELY = False

EARTH_R_KM = 6371.0088

# --------------------------------------------------------------------------- #
# 소형 헬퍼
# --------------------------------------------------------------------------- #


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    cur = conn.execute(sql, tuple(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# 프로세스 내 캐시
#   전 지자체 집계(공변량·가중행렬·TF-IDF)는 한 번 계산하면 세션 내내 불변이다.
#   기존 find_peer_governments 의 콜드 18.2초는 이 재계산 때문이었다.
# --------------------------------------------------------------------------- #
_CACHE: dict[tuple, Any] = {}


def clear_cache() -> None:
    _CACHE.clear()


def _cached(key: tuple, builder):
    if key not in _CACHE:
        _CACHE[key] = builder()
    return _CACHE[key]


def year_of(s: Optional[str]) -> Optional[int]:
    """YYYYMMDD / YYYY-MM-DD → 연도 int."""
    if not s:
        return None
    d = "".join(ch for ch in str(s) if ch.isdigit())
    if len(d) < 4:
        return None
    try:
        y = int(d[:4])
    except ValueError:
        return None
    return y if 1900 <= y <= 2100 else None


def region_type(name: Optional[str], level: int = 2) -> str:
    """행안부 유사자치단체 유형 분류의 1차 축(자치구/시/군/광역).

    행정안전부 지방재정365(lofin365) 공개 설명 기준으로 유사자치단체는 먼저
    '특별·광역시 자치구 / 시 / 군' 을 나눈 뒤 인구 등으로 세분한다.
    본 함수는 그 1차 축만 구현한다(세부 13유형은 인구구간까지 필요).
    """
    if level == 1:
        return "광역"
    if level == 3:
        return "일반구"
    n = (name or "").strip()
    if n.endswith("구"):
        return "자치구"
    if n.endswith("군"):
        return "군"
    if n.endswith("시"):
        return "시"
    return "기타"


# --------------------------------------------------------------------------- #
# 1) 모집단 (위험집합 모수)
# --------------------------------------------------------------------------- #
def active_local_governments(conn: sqlite3.Connection, *, level: int = 2) -> list[dict]:
    return _cached((id(conn), "govs", level), lambda: _active_local_governments(conn, level))


def _active_local_governments(conn: sqlite3.Connection, level: int = 2) -> list[dict]:
    """조례 제정권이 있는 현행 지자체(기본 level=2 기초자치단체 227곳).

    level=3(일반구)은 지방자치법상 자치권이 없어 조례 제정 주체가 아니다 →
    호출 자체를 막지는 않되 has_legislation 조건으로 자연 배제된다.
    """
    rows = _rows(
        conn,
        "SELECT region_id, sig_cd, name, full_name, level, parent_region, "
        "       population, centroid_lon, centroid_lat, has_legislation "
        "FROM regions WHERE level=? AND status='active' AND has_legislation=1 "
        "ORDER BY region_id",
        (level,),
    )
    for r in rows:
        r["rtype"] = region_type(r.get("name"), level=level)
        r["sido_cd"] = (r.get("region_id") or "")[:2]
    return rows


# --------------------------------------------------------------------------- #
# 2) 공간가중행렬
# --------------------------------------------------------------------------- #
def _raw_adjacency(conn: sqlite3.Connection) -> dict[str, set[str]]:
    W: dict[str, set[str]] = {}
    for r in _rows(conn, "SELECT region_id, neighbor_id FROM region_adjacency"):
        a, b = r["region_id"], r["neighbor_id"]
        if not a or not b or a == b:
            continue
        W.setdefault(a, set()).add(b)
        W.setdefault(b, set()).add(a)
    return W


def _haversine_km(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))


def build_spatial_weights(conn: sqlite3.Connection, **kw) -> dict:
    """캐시 래퍼. 인자 조합별로 1회만 계산한다."""
    key = (id(conn), "W", tuple(sorted(kw.items())))
    return _cached(key, lambda: _build_spatial_weights(conn, **kw))


def _build_spatial_weights(
    conn: sqlite3.Connection,
    *,
    level: int = 2,
    lift_sub_districts: bool = True,
    island_policy: str = "knn",
    k_island: int = 2,
    standardize: str = "row",
) -> dict:
    """분석 단위(기본 기초자치단체) 기준 공간가중행렬 W 를 만든다.

    region_adjacency 는 일반구를 둔 대도시(수원·성남·창원 등)의 인접을 **일반구 쪽에**
    저장하고 있어, 모시(母市) 자신은 인접이 0이 되어 Moran's I 표본에서 통째로 빠진다
    (실측: 활성 기초 227곳 중 23곳 인접 0). 그중 14곳이 이 구조적 누락이다.

    lift_sub_districts=True 면 level=3 인접을 parent_region 으로 승격해 이 누락을 복구한다.
    island_policy='knn' 이면 그래도 이웃이 없는 도서지역을 중심점 최근접 k개와 잇는다
    (Anselin·Rey 등 공간계량 관행: 빈 이웃집합은 W 정의를 훼손하므로 kNN 보정).

    standardize='row' → W = D^-1 A (행표준화, 다접경 지자체의 과대 영향력 제거)
    standardize='binary' → 이진 인접 그대로.

    반환: {"W": {rid: {nid: w}}, "cardinality": {rid: 이웃수}, "meta": {...}}
    """
    pop = active_local_governments(conn, level=level)
    universe = {r["region_id"]: r for r in pop}
    raw = _raw_adjacency(conn)

    A: dict[str, set[str]] = {rid: set() for rid in universe}
    for rid in universe:
        A[rid] |= {n for n in raw.get(rid, ()) if n in universe}

    lifted: list[dict] = []
    if lift_sub_districts:
        subs = _rows(
            conn,
            "SELECT region_id, parent_region, full_name FROM regions "
            "WHERE level=3 AND status='active' AND parent_region IS NOT NULL",
        )
        sub_parent = {s["region_id"]: s["parent_region"] for s in subs}
        gained: dict[str, int] = {}
        for sub, parent in sub_parent.items():
            if parent not in universe:
                continue
            for nb in raw.get(sub, ()):
                tgt = sub_parent.get(nb, nb)   # 이웃이 일반구면 그 모시로 치환
                if tgt in universe and tgt != parent and tgt not in A[parent]:
                    A[parent].add(tgt)
                    A[tgt].add(parent)
                    gained[parent] = gained.get(parent, 0) + 1
        for rid, n in sorted(gained.items()):
            lifted.append({"region_id": rid,
                           "name": universe[rid].get("full_name"),
                           "edges_recovered": n})

    islands: list[dict] = []
    if island_policy == "knn":
        cent = {rid: (r["centroid_lon"], r["centroid_lat"]) for rid, r in universe.items()
                if r.get("centroid_lon") is not None and r.get("centroid_lat") is not None}
        # 일반구를 둔 대도시는 centroid 가 자식(일반구)에만 있다 → 자식 평균으로 보완.
        missing = [rid for rid in universe if rid not in cent]
        if missing:
            kids = _rows(conn,
                         "SELECT parent_region, centroid_lon, centroid_lat FROM regions "
                         "WHERE level=3 AND status='active' AND parent_region IS NOT NULL "
                         "AND centroid_lon IS NOT NULL")
            agg: dict[str, list[tuple[float, float]]] = {}
            for kr in kids:
                agg.setdefault(kr["parent_region"], []).append(
                    (float(kr["centroid_lon"]), float(kr["centroid_lat"])))
            for rid in missing:
                pts = agg.get(rid)
                if pts:
                    cent[rid] = (sum(p[0] for p in pts) / len(pts),
                                 sum(p[1] for p in pts) / len(pts))
        for rid in list(A):
            if A[rid] or rid not in cent:
                continue
            lo, la = cent[rid]
            d = sorted(((_haversine_km(lo, la, c[0], c[1]), other)
                        for other, c in cent.items() if other != rid))
            picked = [o for _, o in d[:max(1, k_island)]]
            for o in picked:
                A[rid].add(o)
                A[o].add(rid)
            islands.append({"region_id": rid, "name": universe[rid].get("full_name"),
                            "linked_to": picked,
                            "nearest_km": round(d[0][0], 1) if d else None})

    _has_cent = set(cent) if island_policy == "knn" else set()
    excluded = [{"region_id": rid, "name": universe[rid].get("full_name"),
                 "reason": ("no_neighbor" if rid in _has_cent
                            else "no_adjacency_and_no_centroid")}
                for rid in A if not A[rid]]

    W: dict[str, dict[str, float]] = {}
    for rid, nbs in A.items():
        if not nbs:
            continue
        if standardize == "row":
            w = 1.0 / len(nbs)
            W[rid] = {n: w for n in nbs}
        else:
            W[rid] = {n: 1.0 for n in nbs}

    return {
        "W": W,
        "cardinality": {rid: len(nbs) for rid, nbs in A.items()},
        "adjacency": {rid: set(nbs) for rid, nbs in A.items()},
        "meta": {
            "level": level,
            "universe": len(universe),
            "n_with_neighbors": len(W),
            "standardize": standardize,
            "lift_sub_districts": lift_sub_districts,
            "lifted": lifted,
            "island_policy": island_policy,
            "islands_linked": islands,
            "excluded": excluded,
            "mean_cardinality": round(
                sum(len(v) for v in A.values()) / max(1, len([v for v in A.values() if v])), 3),
        },
    }


# --------------------------------------------------------------------------- #
# 3) 공변량
# --------------------------------------------------------------------------- #
def _area_km2_from_geojson(gj: str) -> Optional[float]:
    """EPSG:4326 GeoJSON → 근사 면적(km²). 중심위도 등적 원통투영."""
    try:
        obj = json.loads(gj)
    except Exception:
        return None
    coords_lat: list[float] = []

    def _walk(c):
        if isinstance(c, (list, tuple)):
            if c and isinstance(c[0], (int, float)) and len(c) >= 2:
                coords_lat.append(float(c[1]))
            else:
                for x in c:
                    _walk(x)

    _walk(obj.get("coordinates"))
    if not coords_lat:
        return None
    lat0 = sum(coords_lat) / len(coords_lat)
    kx = EARTH_R_KM * math.cos(math.radians(lat0)) * math.pi / 180.0
    ky = EARTH_R_KM * math.pi / 180.0

    def _proj(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            return [c[0] * kx, c[1] * ky]
        return [_proj(x) for x in c]

    obj2 = {"type": obj.get("type"), "coordinates": _proj(obj.get("coordinates"))}
    if _HAS_SHAPELY:
        try:
            return float(_shp_shape(obj2).area)
        except Exception:
            return None
    # 폴백: 외곽링 shoelace 합(내부 링 무시 → 과대추정 가능)
    total = 0.0

    def _ring_area(ring):
        s = 0.0
        for i in range(len(ring)):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % len(ring)]
            s += x1 * y2 - x2 * y1
        return abs(s) / 2.0

    def _acc(c, depth):
        nonlocal total
        if depth == 2:
            total += _ring_area(c)
            return
        for x in c:
            _acc(x, depth - 1)

    t = obj.get("type")
    try:
        if t == "Polygon":
            total += _ring_area(obj2["coordinates"][0])
        elif t == "MultiPolygon":
            for poly in obj2["coordinates"]:
                total += _ring_area(poly[0])
    except Exception:
        return None
    return total or None


def region_covariates(conn: sqlite3.Connection, *, level: int = 2, fyr: int = 2025) -> dict[str, dict]:
    return _cached((id(conn), "cov", level, fyr),
                   lambda: _region_covariates(conn, level, fyr))


def _region_covariates(conn: sqlite3.Connection, level: int = 2, fyr: int = 2025) -> dict[str, dict]:
    """지자체별 구조 공변량. 반환 {region_id: {...}} + 각 키의 결측은 None.

    지표 정의
      population        regions.population (행안부 주민등록 인구, DB 적재값)
      area_km2          region_geometry(EPSG:4326) 근사 면적
      pop_density       인구/면적
      budget_total      budget_lines.budget_now 합(해당 회계연도)
      fiscal_self_ratio 1 - (gov_fund+sido_fund)/budget_now  → 자체재원 비율.
                        재정자립도의 근사치이며 원 지표(지방재정365)와 다르다.
      welfare_ratio     field='사회복지' 예산 / 총예산
      ordinance_count   현행 조례·규칙 수
    """
    govs = active_local_governments(conn, level=level)
    out: dict[str, dict] = {
        g["region_id"]: {
            "region_id": g["region_id"],
            "sig_cd": g.get("sig_cd"),
            "name": g.get("name"),
            "full_name": g.get("full_name"),
            "rtype": g.get("rtype"),
            "sido_cd": g.get("sido_cd"),
            "parent_region": g.get("parent_region"),
            "population": float(g["population"]) if g.get("population") is not None else None,
            "area_km2": None, "pop_density": None,
            "budget_total": None, "fiscal_self_ratio": None, "welfare_ratio": None,
            "ordinance_count": 0,
        }
        for g in govs
    }
    if not out:
        return out
    ids = list(out)
    ph = ",".join("?" for _ in ids)

    for r in _rows(conn,
                   f"SELECT region_id, COUNT(*) n FROM ordinances "
                   f"WHERE status='active' AND region_id IN ({ph}) GROUP BY region_id", ids):
        out[r["region_id"]]["ordinance_count"] = int(r["n"])

    for r in _rows(conn,
                   f"SELECT region_id, "
                   f"  COALESCE(SUM(budget_now),0) tot, "
                   f"  COALESCE(SUM(gov_fund),0) gov, "
                   f"  COALESCE(SUM(sido_fund),0) sido, "
                   f"  COALESCE(SUM(CASE WHEN field='사회복지' THEN budget_now ELSE 0 END),0) wf "
                   f"FROM budget_lines WHERE fyr=? AND region_id IN ({ph}) GROUP BY region_id",
                   [fyr] + ids):
        rec = out[r["region_id"]]
        tot = float(r["tot"] or 0)
        if tot > 0:
            rec["budget_total"] = tot
            rec["fiscal_self_ratio"] = max(
                0.0, min(1.0, 1.0 - (float(r["gov"] or 0) + float(r["sido"] or 0)) / tot))
            rec["welfare_ratio"] = float(r["wf"] or 0) / tot

    for r in _rows(conn,
                   f"SELECT region_id, geojson FROM region_geometry WHERE region_id IN ({ph})", ids):
        a = _area_km2_from_geojson(r["geojson"])
        if a and a > 0:
            out[r["region_id"]]["area_km2"] = a

    for rec in out.values():
        if rec["area_km2"] and rec["population"]:
            rec["pop_density"] = rec["population"] / rec["area_km2"]
    return out


# --------------------------------------------------------------------------- #
# 4) 조례명 TF-IDF 프로파일 (정책구조 특성의 대체재)
# --------------------------------------------------------------------------- #
_NAME_STOP = {
    "조례", "규칙", "시행규칙", "시행", "관한", "관하여", "등에", "등의", "및", "의",
    "에", "위한", "위하여", "대한", "관리", "운영", "설치", "규정",
}
_SIDO_TOKEN = re.compile(
    r"(특별자치시|특별자치도|광역시|특별시|통합특별시|자치시|자치도|[가-힣]{1,6}(시|군|구|도))$")


def _name_tokens(name: str, region_name: Optional[str], full_name: Optional[str]) -> list[str]:
    s = name or ""
    for pref in (full_name or "", region_name or ""):
        if pref and s.startswith(pref):
            s = s[len(pref):]
    s = re.sub(r"[^\w가-힣]+", " ", s)
    toks = []
    for t in s.split():
        if len(t) < 2 or t in _NAME_STOP:
            continue
        if _SIDO_TOKEN.fullmatch(t):
            continue
        toks.append(t)
    return toks


def ordinance_name_tfidf(conn: sqlite3.Connection, *, level: int = 2,
                         min_df: int = 3, max_df_ratio: float = 0.9) -> dict[str, dict[str, float]]:
    return _cached((id(conn), "tfidf", level, min_df, max_df_ratio),
                   lambda: _ordinance_name_tfidf(conn, level, min_df, max_df_ratio))


def _ordinance_name_tfidf(conn: sqlite3.Connection, level: int = 2,
                          min_df: int = 3, max_df_ratio: float = 0.9) -> dict[str, dict[str, float]]:
    """지자체별 조례명 토큰 TF-IDF 벡터(L2 정규화).

    ordinance_category 는 159,452건 중 1,087건(0.68%)·코드 2종만 덮어 '정책구조'
    특성으로 쓸 수 없다(구조유사도 평균 0.94, 변별력 0). 조례명은 100% 존재하므로
    전수 적용 가능한 대체 특성이다. 본문 수집이 끝나면 Linder et al.(2020) 식
    텍스트 재사용 지표로 교체하는 것이 다음 단계.
    """
    govs = {g["region_id"]: g for g in active_local_governments(conn, level=level)}
    if not govs:
        return {}
    ids = list(govs)
    ph = ",".join("?" for _ in ids)
    tf: dict[str, dict[str, int]] = {rid: {} for rid in ids}
    for r in _rows(conn,
                   f"SELECT region_id, name FROM ordinances "
                   f"WHERE status='active' AND region_id IN ({ph})", ids):
        g = govs[r["region_id"]]
        for t in _name_tokens(r["name"] or "", g.get("name"), g.get("full_name")):
            d = tf[r["region_id"]]
            d[t] = d.get(t, 0) + 1

    n_docs = len([1 for v in tf.values() if v])
    df: dict[str, int] = {}
    for v in tf.values():
        for t in v:
            df[t] = df.get(t, 0) + 1
    keep = {t for t, c in df.items()
            if c >= min_df and c <= max_df_ratio * max(1, n_docs)}

    out: dict[str, dict[str, float]] = {}
    for rid, v in tf.items():
        vec: dict[str, float] = {}
        for t, c in v.items():
            if t not in keep:
                continue
            vec[t] = (1.0 + math.log(c)) * math.log(n_docs / df[t])
        nrm = math.sqrt(sum(x * x for x in vec.values()))
        out[rid] = {k: x / nrm for k, x in vec.items()} if nrm > 0 else {}
    return out


def cosine(a: dict[str, float], b: dict[str, float]) -> Optional[float]:
    """공유 성분 코사인. 한쪽이라도 비면 None(=특성 없음)을 돌려 0.0 오인을 막는다."""
    if not a or not b:
        return None
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    dot = sum(v * large.get(k, 0.0) for k, v in small.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return None
    return dot / (na * nb)


# --------------------------------------------------------------------------- #
# 5) 템플릿별 채택연도
# --------------------------------------------------------------------------- #
def adoption_years(conn: sqlite3.Connection, template: str, **kw) -> dict:
    """캐시 래퍼."""
    key = (id(conn), "adopt", template, tuple(sorted(kw.items())))
    return _cached(key, lambda: _adoption_years(conn, template, **kw))


def _adoption_years(
    conn: sqlite3.Connection,
    template: str,
    *,
    level: int = 2,
    mode: str = "enactment",
    ord_kind: Optional[str] = "조례",
) -> dict:
    """조례명 LIKE %template% 인 조례의 지자체별 채택연도.

    **좌측절단·측정오차 경고**
    ordinances.enacted_on 은 법령API 자치법규 목록의 '공포일자'이며 **현행본 기준**이다.
    따라서 rr_cls_cd 별로 의미가 다르다.

      mode='enactment'   rr_cls_cd='제정' 인 행만 사용.
                         → enacted_on 이 진짜 최초 제정일이다(정확).
                         단, '제정'으로 남아 있다는 것은 '한 번도 개정되지 않았다'는 뜻이므로
                         표본이 미개정 조례로 선택된다(선택편의). 커버리지를 함께 보고한다.
      mode='upper_bound' 모든 rr_cls_cd 사용. enacted_on 은 최초 제정일의 **상한**이다
                         (개정본이면 실제 제정은 그보다 이르다). 구간중도절단 자료의
                         보수적 상한 대입에 해당한다.

    반환 {"years": {rid: year}, "meta": {...}}
    """
    govs = {g["region_id"]: g for g in active_local_governments(conn, level=level)}
    if not govs:
        return {"years": {}, "meta": {"template": template, "mode": mode, "universe": 0}}
    ids = list(govs)
    ph = ",".join("?" for _ in ids)
    sql = (f"SELECT region_id, name, enacted_on, rr_cls_cd FROM ordinances "
           f"WHERE status='active' AND region_id IN ({ph}) AND name LIKE ?")
    params: list[Any] = ids + [f"%{template}%"]
    if ord_kind:
        sql += " AND ord_kind=?"
        params.append(ord_kind)
    if mode == "enactment":
        sql += " AND rr_cls_cd='제정'"
    rows = _rows(conn, sql, params)

    years: dict[str, int] = {}
    for r in rows:
        y = year_of(r.get("enacted_on"))
        if y is None:
            continue
        rid = r["region_id"]
        if rid not in years or y < years[rid]:
            years[rid] = y

    # 같은 템플릿의 전체(개정 포함) 보유 지자체 수 → 제정본 모드의 커버리지 진단
    all_rows = _rows(conn,
                     f"SELECT DISTINCT region_id FROM ordinances "
                     f"WHERE status='active' AND region_id IN ({ph}) AND name LIKE ?"
                     + (" AND ord_kind=?" if ord_kind else ""),
                     ids + [f"%{template}%"] + ([ord_kind] if ord_kind else []))
    holders = {r["region_id"] for r in all_rows}

    return {
        "years": years,
        "meta": {
            "template": template,
            "mode": mode,
            "ord_kind": ord_kind,
            "universe": len(govs),
            "adopters_observed": len(years),
            "holders_any_version": len(holders),
            "enactment_date_coverage": round(len(years) / len(holders), 4) if holders else None,
            "year_min": min(years.values()) if years else None,
            "year_max": max(years.values()) if years else None,
            "warning": (
                "rr_cls_cd='제정' 만 사용 → 개정 이력이 있는 조례는 제외되어 채택 시점이 "
                "관측되지 않는다(선택편의). enactment_date_coverage 로 정도를 확인하라."
                if mode == "enactment" else
                "enacted_on 은 현행본 공포일이므로 최초 제정일의 상한이다(측정오차 상향)."
            ),
        },
    }


__all__ = [
    "active_local_governments", "region_type", "year_of",
    "build_spatial_weights", "region_covariates",
    "ordinance_name_tfidf", "cosine", "adoption_years", "clear_cache",
]
