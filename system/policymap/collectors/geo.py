"""policymap.collectors.geo — 행정구역 스파인 + 경계 + 인접 + 코드 크로스워크.

담당 소스(명세[geo_budget_api]):
  * 행정표준코드 StanReginCd getStanReginCdList → regions(법정동코드 10자리 스파인)
  * V-World data GetFeature(LT_C_ADSIDO/ADSIGG_INFO) → region_geometry(MultiPolygon)
  * region_adjacency(Queen contiguity; shapely 있으면 정밀, 없으면 순수파이썬 폴백)
  * region_succession(SUCCEEDED_BY; 2026-07-01 전남·광주 통합 등 폐지·승계)
  * code_crosswalk(법정동 ↔ sig_cd ↔ 법령API org/sborg ↔ lofin laf_cd)

공개 함수(CONTRACTS.md §1.3 계약):
    collect_regions(http, cfg, conn, *, run_id=None) -> dict
    collect_boundaries(http, cfg, conn, *, reorg_event=None, run_id=None) -> dict
    build_crosswalk(conn, *, overrides=None) -> dict
    compute_adjacency(conn, *, method='auto') -> dict

저수준/순수 함수(픽스처로 단위테스트 가능):
    stan_regin_list / vworld_features               # 1회 HTTP(또는 픽스처)
    region_rows_from_stanregin / parse_vworld_feature
    classify_region / geometry_points / bbox_of / centroid_of / ring_signature_of
    apply_reorg_event / normalize_gov_name / split_full_name

규율:
  * 코어는 표준라이브러리만으로 동작. shapely 는 선택(try/except) → 없으면 ring-share 폴백.
  * VWORLD_KEY/STANREGIN_KEY 미설정 시 번들 픽스처(개발/테스트 전용)로 graceful fallback
    하되, 로그 경고 + 반환 dict의 'fixtures':True 로 "실데이터 아님"을 명시(지어내기 금지).
  * 폐지/통합 지자체는 하드삭제 금지 → db.soft_delete 툼스톤(status='merged', valid_to).
  * 파티션 완전 영속화 후에만 db.advance_cursor.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from .. import db
from ..util import (
    NotFound,
    PermanentError,
    TransientError,
    as_list,
    content_hash,
    get_logger,
    now_kst_iso,
    today_kst,
)

# 선택적 정밀 지오메트리 엔진(없으면 순수파이썬 ring-share 폴백)
try:  # pragma: no cover - 환경 의존
    from shapely.geometry import shape as _shapely_shape  # type: ignore
    _HAS_SHAPELY = True
except Exception:  # pragma: no cover
    _shapely_shape = None  # type: ignore
    _HAS_SHAPELY = False

# --------------------------------------------------------------------------- #
# 상수
# --------------------------------------------------------------------------- #
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# 남한 전역 커버 BBox(명세[geo_budget_api] §1.3) — GetFeature 는 필터 최소 1개 필수.
NATIONAL_BOX = "BOX(124.0,33.0,132.0,43.0,EPSG:4326)"

# (레이어명, level) — 시도(광역) → 시군구(기초) 순으로 수집.
_LAYERS: tuple[tuple[str, int], ...] = (
    ("LT_C_ADSIDO_INFO", 1),
    ("LT_C_ADSIGG_INFO", 2),
)
_LAYER_FIXTURE = {
    "LT_C_ADSIDO_INFO": "vworld_sido_sample.json",
    "LT_C_ADSIGG_INFO": "vworld_sigungu_sample.json",
}

# watermarks 스코프(source='boundary' 아래에서 스파인/지오메트리 파티션 분리).
_SCOPE_REGIONS = "stanregin:global"
_SCOPE_GEOM = "vworld:geometry"

# 좌표 양자화 자릿수(순수파이썬 인접 탐지용; 5자리≈1.1m). 인접 폴리곤은 동일 경계
# 꼭짓점을 공유하므로 양자화 후 문자열 일치로 Queen contiguity 판정.
_QUANT = 5

# regions 변경 감지 추적 컬럼(무변경=무이벤트 규율).
_REGION_TRACK = (
    "name", "full_name", "level", "region_cd", "valid_to",
    "status", "sig_cd", "parent_region", "has_legislation",
)


# --------------------------------------------------------------------------- #
# 지역 개편(승계) 레지스트리 — 확정 사실만 기재(미확정 코드는 pending/None, 지어내기 금지)
# --------------------------------------------------------------------------- #
# 2026-07-01 전남·광주 통합특별시 출범: 광주광역시(29)+전라남도(46) → 통합 신설.
# 신설 지자체의 법정동코드는 미확정(확인 필요) → new=None(pending). 확정 사실인
# '구 광역 폐지(merged, valid_to=2026-07-01)'만 적용하고, 승계 대상 코드가 확정되면
# new_regions/successions[].new 를 채워 재적용한다.
REORG_EVENTS: dict[str, dict[str, Any]] = {
    "jeonnam-gwangju-2026": {
        "tag": "jeonnam-gwangju-2026",
        "effective_date": "2026-07-01",
        "succession_type": "통합",
        "legal_basis": "(확인 필요) 전남·광주 통합특별시 설치에 관한 특별법",
        "status_note": "자치법규 약 2,454건 정비 과도기(폐지 지자체 노드 보존)",
        "pending": True,  # 통합 신설 지자체 코드 미확정 → 승계 엣지 보류
        "new_regions": [],  # 코드 확정 시 신설 지역 regions 행을 여기 추가
        "successions": [
            {"old": "29", "new": None, "old_name": "광주광역시"},
            {"old": "46", "new": None, "old_name": "전라남도"},
        ],
    },
}


# --------------------------------------------------------------------------- #
# 반환 dict 헬퍼(CONTRACTS.md 고수준 collect_* 공통 형태)
# --------------------------------------------------------------------------- #
def _result(**extra: Any) -> dict:
    r = {"inserted": 0, "updated": 0, "unchanged": 0,
         "changed": 0, "errors": 0, "status": "ok"}
    r.update(extra)
    return r


def _finalize(res: dict) -> dict:
    res["changed"] = res.get("inserted", 0) + res.get("updated", 0)
    if res.get("status") == "error":
        return res
    if res.get("errors", 0) > 0:
        res["status"] = "partial" if res["changed"] > 0 else "error"
    return res


# --------------------------------------------------------------------------- #
# 픽스처 로더
# --------------------------------------------------------------------------- #
def _load_fixture(name: str) -> dict:
    path = FIXTURE_DIR / name
    if not path.exists():
        raise RuntimeError(f"번들 픽스처 없음: {path} — 키를 설정하거나 픽스처를 배치하라.")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_fixture_stanregin() -> tuple[int, list[dict]]:
    return _parse_stanregin_envelope(_load_fixture("stanregin_sample.json"))


def _load_fixture_vworld(layer: str) -> tuple[dict, list[dict]]:
    fname = _LAYER_FIXTURE.get(layer)
    if not fname or not (FIXTURE_DIR / fname).exists():
        return {}, []
    return _parse_vworld_envelope(_load_fixture(fname))


# --------------------------------------------------------------------------- #
# 응답 envelope 파서(순수)
# --------------------------------------------------------------------------- #
def _parse_stanregin_envelope(resp: Any) -> tuple[int, list[dict]]:
    """StanReginCd 응답 → (totalCount, row[]).

    오류 신호 해석:
      * OpenAPI_ServiceResponse.cmmMsgHeader(errMsg/returnReasonCode) → PermanentError
      * RESULT.resultCode INFO-2xx(데이터 없음) → NotFound
    """
    if not isinstance(resp, dict):
        raise NotFound("StanReginCd: 비-dict 응답")

    svc = resp.get("OpenAPI_ServiceResponse")
    if isinstance(svc, dict):
        hdr = svc.get("cmmMsgHeader") or {}
        msg = hdr.get("errMsg") or hdr.get("returnAuthMsg") or "service error"
        raise PermanentError(
            f"StanReginCd 오류: {msg} (code={hdr.get('returnReasonCode')})"
        )

    root = resp.get("StanReginCd")
    if root is None:
        res = resp.get("RESULT") or {}
        code = str(res.get("resultCode") or res.get("code") or "")
        if code:
            if code.upper().startswith("INFO-2") or "NODATA" in code.upper():
                raise NotFound(f"StanReginCd 데이터 없음: {res.get('resultMsg')}")
            raise PermanentError(f"StanReginCd 오류: {res.get('resultMsg')} ({code})")
        raise NotFound("StanReginCd: 알 수 없는 빈 응답")

    total, rows = 0, []
    for part in as_list(root):
        if not isinstance(part, dict):
            continue
        if "head" in part:
            for h in as_list(part["head"]):
                if not isinstance(h, dict):
                    continue
                if "totalCount" in h:
                    try:
                        total = int(h["totalCount"])
                    except (TypeError, ValueError):
                        pass
                r = h.get("RESULT")
                if isinstance(r, dict):
                    code = str(r.get("resultCode") or "")
                    if code.upper().startswith("INFO-2"):
                        raise NotFound(f"StanReginCd 데이터 없음: {r.get('resultMsg')}")
        if "row" in part:
            rows.extend(as_list(part["row"]))
    return total, rows


def _parse_vworld_envelope(resp: Any) -> tuple[dict, list[dict]]:
    """V-World 응답 → (page_info, features[]).

    status=='ERROR' 시 code 로 분기: LIMIT/OVER → TransientError, 그 외 → PermanentError.
    """
    r = resp.get("response") if isinstance(resp, dict) else None
    if not isinstance(r, dict):
        raise NotFound("V-World: response 루트 없음")
    status = str(r.get("status") or "").upper()
    if status == "ERROR":
        err = r.get("error") or {}
        code = str(err.get("code") or err.get("level") or "")
        text = err.get("text") or ""
        if any(tok in code.upper() for tok in ("LIMIT", "OVER")):
            raise TransientError(f"V-World rate limit: {code} {text}")
        raise PermanentError(f"V-World 오류: {code} {text}")
    if status in ("NOT_FOUND", "EMPTY"):
        raise NotFound(f"V-World 결과 없음: {status}")
    result = r.get("result") or {}
    fc = result.get("featureCollection") or {}
    feats = fc.get("features") or []
    page = r.get("page") or {}
    return (page if isinstance(page, dict) else {}), as_list(feats)


# --------------------------------------------------------------------------- #
# 저수준 1회 호출(픽스처 지원)
# --------------------------------------------------------------------------- #
def stan_regin_list(
    http, cfg, *, page: int = 1, rows: int = 1000,
    addr: Optional[str] = None, use_fixtures: bool = False,
) -> tuple[int, list[dict]]:
    """StanReginCd getStanReginCdList 1페이지. 반환 (totalCount, row[])."""
    if use_fixtures:
        return _load_fixture_stanregin()
    cfg.require("stanregin_key")
    url = str(cfg.stanregin_base).rstrip("/") + "/getStanReginCdList"
    params: dict[str, Any] = {
        "ServiceKey": cfg.stanregin_key,  # data.go.kr Decoding 키 권장(이중 인코딩 주의)
        "pageNo": page,
        "numOfRows": rows,
        "type": "json",
    }
    if addr:
        params["locatadd_nm"] = addr
    return _parse_stanregin_envelope(http.get_json(url, params))


def vworld_features(
    http, cfg, *, data: str, page: int = 1, size: int = 100,
    geom_filter: str = NATIONAL_BOX, crs: str = "EPSG:4326",
    use_fixtures: bool = False,
) -> tuple[dict, list[dict]]:
    """V-World data GetFeature 1페이지. 반환 (page_info, features[])."""
    if use_fixtures:
        return _load_fixture_vworld(data)
    cfg.require("vworld_key")
    params: dict[str, Any] = {
        "service": "data",
        "request": "GetFeature",
        "version": "2.0",
        "data": data,
        "key": cfg.vworld_key,
        "format": "json",
        "crs": crs,
        "size": size,
        "page": page,
        "geomFilter": geom_filter,
    }
    if getattr(cfg, "vworld_domain", None):
        params["domain"] = cfg.vworld_domain  # 서버 호출은 등록 도메인 일치 필수
    return _parse_vworld_envelope(http.get_json(cfg.vworld_base, params))


def _fetch_all_stanregin(http, cfg, log, *, use_fixtures=False,
                         page_size=1000, max_pages=200) -> list[dict]:
    if use_fixtures:
        log.warning("STANREGIN_KEY 미설정 → 번들 픽스처 사용(개발/테스트 전용, 실데이터 아님)")
        _, rows = _load_fixture_stanregin()
        return rows
    all_rows: list[dict] = []
    page = 1
    while page <= max_pages:
        try:
            total, rows = stan_regin_list(http, cfg, page=page, rows=page_size)
        except NotFound:
            break
        if not rows:
            break
        all_rows.extend(rows)
        if total and len(all_rows) >= total:
            break
        if len(rows) < page_size:
            break
        page += 1
    return all_rows


def _fetch_all_vworld(http, cfg, log, *, layer, use_fixtures=False,
                      size=100, max_pages=100) -> list[dict]:
    if use_fixtures:
        _, feats = _load_fixture_vworld(layer)
        return feats
    all_f: list[dict] = []
    page = 1
    while page <= max_pages:
        try:
            pg, feats = vworld_features(http, cfg, data=layer, page=page, size=size)
        except NotFound:
            break
        if not feats:
            break
        all_f.extend(feats)
        try:
            total_pages = int(pg.get("total") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        if page >= total_pages:
            break
        page += 1
    return all_f


# --------------------------------------------------------------------------- #
# StanReginCd row → regions 행(순수)
# --------------------------------------------------------------------------- #
def classify_region(row: dict) -> Optional[dict]:
    """StanReginCd row 를 시도/시군구/일반구로 분류. 읍면동·리는 None(스킵).

    규칙(명세[geo_budget_api] §3.2):
      * 시도(level1): sgg_cd=='000' & umd_cd=='000' & ri_cd=='00'
      * 시군구(level2): sgg_cd!='000' & umd_cd=='000' & ri_cd=='00'
    보강: locathigh_cd 상위코드가 시군구(sgg≠000)이면 일반구·행정시(level3, 자치입법권 없음).
    반환: {region_id, region_cd, sig_cd, sido_cd, sgg_cd, level, parent_region, has_legislation, ...}
    """
    region_cd = str(row.get("region_cd") or "").strip()
    sido = str(row.get("sido_cd") or (region_cd[:2] if region_cd else "")).zfill(2)
    sgg = str(row.get("sgg_cd") or (region_cd[2:5] if region_cd else "")).zfill(3)
    umd = str(row.get("umd_cd") or (region_cd[5:8] if len(region_cd) >= 8 else "")).zfill(3)
    ri = str(row.get("ri_cd") or (region_cd[8:10] if len(region_cd) >= 10 else "")).zfill(2)
    if not sido or sido == "00":
        return None
    if umd != "000" or ri != "00":
        return None  # 읍면동/리 계층은 스파인 대상 아님

    base = {
        "region_cd": region_cd or (sido + sgg + umd + ri),
        "sido_cd": sido,
        "sgg_cd": sgg,
        "name": row.get("locallow_nm") or row.get("locatadd_nm"),
        "full_name": row.get("locatadd_nm"),
        "locatjumin_cd": row.get("locatjumin_cd"),
        "valid_from": row.get("adpt_de"),
    }

    if sgg == "000":  # 시도
        base.update({
            "region_id": sido,
            "sig_cd": sido + "000",
            "level": 1,
            "parent_region": None,
            "has_legislation": 1,
        })
        return base

    # 시군구(잠정 level2). locathigh_cd 로 일반구·행정시(level3) 판별.
    sig5 = (sido + sgg)[:5]
    high = str(row.get("locathigh_cd") or "").strip()
    high_sgg = high[2:5] if len(high) >= 5 else "000"
    if high and high_sgg != "000":
        # 상위가 시군구(시) → 자치입법권 없는 일반구/행정시
        base.update({
            "region_id": sig5,
            "sig_cd": sig5,
            "level": 3,
            "parent_region": high[:5],
            "has_legislation": 0,
        })
    else:
        base.update({
            "region_id": sig5,
            "sig_cd": sig5,
            "level": 2,
            "parent_region": sido,
            "has_legislation": 1,
        })
    return base


def region_rows_from_stanregin(rows: Iterable[dict]) -> list[dict]:
    """StanReginCd row[] → regions upsert 행[](중복 region_id 는 마지막 승리)."""
    out: dict[str, dict] = {}
    now = now_kst_iso()
    asof = today_kst()
    for raw in rows:
        rr = classify_region(raw)
        if not rr:
            continue
        rr.setdefault("status", "active")
        rr.setdefault("valid_to", None)
        rr["as_of_date"] = asof
        rr["source"] = "StanReginCd"
        rr["updated_at"] = now
        out[rr["region_id"]] = rr
    return list(out.values())


# --------------------------------------------------------------------------- #
# V-World feature → geometry(순수)
# --------------------------------------------------------------------------- #
def parse_vworld_feature(feature: dict, level: int) -> dict:
    """V-World feature → {region_id, sig_cd, name, full_nm, geometry}.

    level=1(시도): ctprvn_cd/ctp_kor_nm. level>=2(시군구): sig_cd/sig_kor_nm/full_nm.
    """
    props = feature.get("properties") or {}
    geom = feature.get("geometry")
    if level == 1:
        code = str(props.get("ctprvn_cd") or props.get("sido_cd")
                   or (props.get("sig_cd") or "")[:2]).strip()
        region_id = code
        sig_cd = (code + "000")[:5] if code else None
        name = props.get("ctp_kor_nm") or props.get("ctp_nm")
        full = props.get("full_nm") or name
    else:
        sig = str(props.get("sig_cd") or "").strip()
        region_id = sig or None
        sig_cd = sig or None
        name = props.get("sig_kor_nm") or props.get("sig_nm")
        full = props.get("full_nm")
    return {
        "region_id": region_id,
        "sig_cd": sig_cd,
        "name": name,
        "full_nm": full,
        "geometry": geom,
    }


# --------------------------------------------------------------------------- #
# 지오메트리 순수 유틸(표준라이브러리만)
# --------------------------------------------------------------------------- #
def geometry_points(geom: Any):
    """(Multi)Polygon geometry 의 모든 경계 꼭짓점을 (lon, lat) 로 순회."""
    if not isinstance(geom, dict):
        return
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if coords is None:
        return
    if gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for pt in ring:
                    if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                        yield float(pt[0]), float(pt[1])
    elif gtype == "Polygon":
        for ring in coords:
            for pt in ring:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    yield float(pt[0]), float(pt[1])


def bbox_of(geom: Any) -> Optional[list]:
    xs, ys = [], []
    for lon, lat in geometry_points(geom):
        xs.append(lon)
        ys.append(lat)
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def centroid_of(geom: Any) -> tuple[Optional[float], Optional[float]]:
    """근사 중심점 = bbox 중심(문서상 근사; 정밀 무게중심 아님)."""
    b = bbox_of(geom)
    if not b:
        return None, None
    return (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0


def ring_signature_of(geom: Any, ndigits: int = _QUANT) -> list[str]:
    """경계 꼭짓점을 양자화한 서명(정렬된 고유 'lon,lat' 목록). Queen 인접 폴백용."""
    seen: set[str] = set()
    for lon, lat in geometry_points(geom):
        seen.add(f"{round(lon, ndigits)},{round(lat, ndigits)}")
    return sorted(seen)


def _bbox_overlap(a: Optional[list], b: Optional[list], eps: float = 1e-9) -> bool:
    if not a or not b:
        return True  # bbox 미상 → 후보로 취급
    return not (a[2] < b[0] - eps or b[2] < a[0] - eps
                or a[3] < b[1] - eps or b[3] < a[1] - eps)


# --------------------------------------------------------------------------- #
# 이름 정규화(코드 크로스워크 name-join 헬퍼)
# --------------------------------------------------------------------------- #
def normalize_gov_name(name: Optional[str]) -> str:
    """지자체명 정규화(공백 제거). 법령API org/sborg name-join 매칭 키."""
    if not name:
        return ""
    return re.sub(r"\s+", "", str(name))


def split_full_name(full: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """전체주소명 → (sido_nm, sgg_nm). '경기도 수원시 장안구' → ('경기도','수원시 장안구')."""
    if not full:
        return None, None
    parts = str(full).split()
    if not parts:
        return None, None
    return parts[0], (" ".join(parts[1:]) or None)


# --------------------------------------------------------------------------- #
# collect_regions — StanReginCd 스파인
# --------------------------------------------------------------------------- #
def _region_diff(prev: dict, row: dict) -> list[str]:
    changed = []
    for k in _REGION_TRACK:
        if str(prev.get(k)) != str(row.get(k)):
            changed.append(k)
    return changed


def collect_regions(http, cfg, conn, *, run_id=None) -> dict:
    """StanReginCd 전량 페이징 → regions(시도 level1 / 시군구 level2 / 일반구 level3).

    source='boundary', scope='stanregin:global'. 무변경=무이벤트(change_log 미기록).
    키 미설정 시 번들 픽스처로 동작(res['fixtures']=True).
    """
    log = get_logger("policymap.geo", getattr(cfg, "log_level", "INFO"))
    res = _result()
    source, scope = "boundary", _SCOPE_REGIONS
    use_fx = not getattr(cfg, "stanregin_key", None)

    db.set_watermark(conn, source, scope, status="partial", run_id=run_id)
    conn.commit()

    try:
        raw_rows = _fetch_all_stanregin(http, cfg, log, use_fixtures=use_fx)
    except PermanentError as exc:
        with db.tx(conn):
            db.mark_partition_status(conn, source, scope, "error", note=str(exc))
        res["errors"] = 1
        res["status"] = "error"
        res["note"] = str(exc)
        return res

    region_rows = region_rows_from_stanregin(raw_rows)
    ids = {r["region_id"] for r in region_rows}
    existing = {
        r["region_id"]: r
        for r in db.fetchall(
            conn,
            "SELECT region_id, name, full_name, level, region_cd, valid_to, "
            "status, sig_cd, parent_region, has_legislation FROM regions",
        )
    }
    # FK(parent_region self-ref) 충족을 위해 level 오름차순(1→2→3)으로 삽입.
    region_rows.sort(key=lambda r: (r.get("level", 9), r["region_id"]))

    with db.tx(conn):
        for row in region_rows:
            parent = row.get("parent_region")
            if parent and parent not in ids and parent not in existing:
                row["parent_region"] = None  # 미확보 상위 → FK 회피(툼스톤 보존과 무관)
            prev = existing.get(row["region_id"])
            if prev is None:
                db.upsert(conn, "regions", row, "region_id")
                res["inserted"] += 1
                db.log_change(
                    conn, entity_type="boundary", entity_id=row["region_id"],
                    event="created", source=source, scope=scope,
                    entity_name=row.get("name"), region_code=row.get("sig_cd"),
                    run_id=run_id,
                )
            else:
                diff = _region_diff(prev, row)
                if diff:
                    db.upsert(conn, "regions", row, "region_id")
                    res["updated"] += 1
                    db.log_change(
                        conn, entity_type="boundary", entity_id=row["region_id"],
                        event="amended", source=source, scope=scope,
                        entity_name=row.get("name"), region_code=row.get("sig_cd"),
                        fields_changed=json.dumps(diff, ensure_ascii=False),
                        run_id=run_id,
                    )
                else:
                    res["unchanged"] += 1

        max_adpt = max((str(r.get("valid_from") or "") for r in region_rows), default="")
        cursor = f"rows:{len(region_rows)}|maxadpt:{max_adpt}"
        res["changed"] = res["inserted"] + res["updated"]
        db.advance_cursor(
            conn, source, scope, cursor, status="ok",
            changed=res["changed"], rows_seen=len(region_rows),
            run_id=run_id, note="fixtures" if use_fx else None,
        )

    res["status"] = "ok"
    res["regions"] = len(region_rows)
    res["fixtures"] = use_fx
    return res


# --------------------------------------------------------------------------- #
# collect_boundaries — V-World 경계 + 승계
# --------------------------------------------------------------------------- #
def collect_boundaries(http, cfg, conn, *, reorg_event=None, run_id=None) -> dict:
    """V-World LT_C_ADSIDO/ADSIGG_INFO(BOX 페이징) → region_geometry(MultiPolygon).

    sig_cd/ctprvn_cd 로 regions 조인(존재하는 지역만). 지오메트리 동일(content_hash)
    시 스킵. reorg_event 지정 시 REORG_EVENTS 적용(승계 + 구 지자체 툼스톤).
    source='boundary', scope='vworld:geometry'.
    """
    log = get_logger("policymap.geo", getattr(cfg, "log_level", "INFO"))
    res = _result(skipped=0)
    source, scope = "boundary", _SCOPE_GEOM
    use_fx = not getattr(cfg, "vworld_key", None)
    if use_fx:
        log.warning("VWORLD_KEY 미설정 → 번들 픽스처 사용(개발/테스트 전용, 실데이터 아님)")

    db.set_watermark(conn, source, scope, status="partial", run_id=run_id)
    conn.commit()

    region_ids = {r["region_id"] for r in db.fetchall(conn, "SELECT region_id FROM regions")}
    existing_geo = {
        r["region_id"]: r["geojson"]
        for r in db.fetchall(conn, "SELECT region_id, geojson FROM region_geometry")
    }
    now = now_kst_iso()
    total_feats = 0
    reorg_summary: Optional[dict] = None

    try:
        with db.tx(conn):
            for layer, level in _LAYERS:
                feats = _fetch_all_vworld(http, cfg, log, layer=layer, use_fixtures=use_fx)
                for feat in feats:
                    pf = parse_vworld_feature(feat, level)
                    rid, geom = pf.get("region_id"), pf.get("geometry")
                    if not rid or geom is None:
                        res["errors"] += 1
                        continue
                    if rid not in region_ids:
                        res["skipped"] += 1  # regions 스파인에 없음(먼저 collect_regions)
                        continue
                    total_feats += 1
                    geojson_str = json.dumps(geom, ensure_ascii=False, separators=(",", ":"))
                    prev = existing_geo.get(rid)
                    if prev is not None and content_hash(prev) == content_hash(geojson_str):
                        res["unchanged"] += 1
                        continue
                    bbox = bbox_of(geom)
                    grow = {
                        "region_id": rid,
                        "crs": "EPSG:4326",
                        "geojson": geojson_str,
                        "bbox": json.dumps(bbox) if bbox else None,
                        "ring_signature": json.dumps(ring_signature_of(geom)),
                        "fetched_at": now,
                    }
                    db.upsert(conn, "region_geometry", grow, "region_id")
                    if prev is None:
                        res["inserted"] += 1
                    else:
                        res["updated"] += 1
                    lon, lat = centroid_of(geom)
                    db.execute(
                        conn,
                        "UPDATE regions SET geometry_ref=?, centroid_lon=?, "
                        "centroid_lat=?, updated_at=? WHERE region_id=?",
                        (rid, lon, lat, now, rid),
                    )

            if reorg_event:
                reorg_summary = _apply_named_reorg(conn, reorg_event, run_id=run_id, log=log)

            res["changed"] = res["inserted"] + res["updated"]
            db.advance_cursor(
                conn, source, scope, f"features:{total_feats}", status="ok",
                changed=res["changed"], rows_seen=total_feats, run_id=run_id,
                note="fixtures" if use_fx else None,
            )
    except PermanentError as exc:
        with db.tx(conn):
            db.mark_partition_status(conn, source, scope, "error", note=str(exc))
        res["errors"] += 1
        res["status"] = "error"
        res["note"] = str(exc)
        return res

    res["fixtures"] = use_fx
    if reorg_summary is not None:
        res["reorg"] = reorg_summary
    return _finalize(res)


# --------------------------------------------------------------------------- #
# 지역 개편(승계) 적용
# --------------------------------------------------------------------------- #
def apply_reorg_event(conn, event: dict, *, run_id=None, log=None) -> dict:
    """개편 이벤트 적용: 신설 지역 upsert → region_succession → 구 지자체 툼스톤.

    successions[].new 가 None/미존재면 승계 엣지는 보류(pending)하되, 구 지자체는
    폐지(merged, valid_to=effective_date)로 툼스톤 처리하고 change_log('boundary_reorg').
    """
    summ = {
        "tag": event.get("tag"),
        "effective_date": event.get("effective_date"),
        "successions": 0,
        "soft_deleted": 0,
        "pending": 0,
        "skipped": 0,
    }
    eff = event.get("effective_date")
    typ = event.get("succession_type")
    basis = event.get("legal_basis")
    note = event.get("status_note")

    for nr in event.get("new_regions", []) or []:
        if nr.get("region_id"):
            db.upsert(conn, "regions", nr, "region_id")

    present = {r["region_id"] for r in db.fetchall(conn, "SELECT region_id FROM regions")}

    for s in event.get("successions", []) or []:
        old, new = s.get("old"), s.get("new")
        if old not in present:
            summ["skipped"] += 1
            continue
        after = None
        if new and new in present:
            db.upsert(
                conn, "region_succession",
                {
                    "old_region_id": old,
                    "new_region_id": new,
                    "succession_type": s.get("type") or typ,
                    "effective_date": s.get("effective_date") or eff,
                    "legal_basis": basis,
                    "status_note": note,
                },
                ("old_region_id", "new_region_id"),
            )
            summ["successions"] += 1
            after = new
        else:
            summ["pending"] += 1  # 승계 대상 코드 미확정 → 엣지 보류(구 지자체만 툼스톤)

        db.soft_delete(
            conn, "regions", {"region_id": old},
            status=s.get("status") or "merged",
            valid_to_col="valid_to", valid_to=eff,
        )
        summ["soft_deleted"] += 1
        db.log_change(
            conn, entity_type="boundary", entity_id=old, event="boundary_reorg",
            source="boundary", scope=_SCOPE_GEOM, entity_name=s.get("old_name"),
            before=old, after=after, run_id=run_id,
        )
    return summ


def _apply_named_reorg(conn, tag: str, *, run_id=None, log=None) -> dict:
    event = REORG_EVENTS.get(tag)
    if not event:
        if log:
            log.warning("알 수 없는 개편 이벤트 태그: %s", tag)
        return {"tag": tag, "error": "unknown reorg event"}
    return apply_reorg_event(conn, event, run_id=run_id, log=log)


# --------------------------------------------------------------------------- #
# build_crosswalk — 코드 크로스워크
# --------------------------------------------------------------------------- #
def _load_crosswalk_overrides() -> dict:
    """수기보정 오버라이드(fixtures/code_crosswalk_overrides.json). 없으면 {}.

    형식: { "<sig_cd 또는 region_id>": {law_org, law_sborg, lofin_laf_cd,
                                        confidence?, verified?, note?} }
    (실 코드 미확보 시 파일 부재가 정상 — 결정적 vworld_sig_cd 만 채운다.)
    """
    path = FIXTURE_DIR / "code_crosswalk_overrides.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_crosswalk(conn, *, overrides: Optional[dict] = None) -> dict:
    """code_crosswalk 구축 + regions 외부코드 백필.

    결정적: vworld_sig_cd = region_cd[:5] = sig_cd (무손실). law_org/law_sborg/
    lofin_laf_cd 는 결정적 매핑표 부재 → overrides(이름조인·수기보정)로만 채움.
    """
    res = _result()
    ov = overrides if overrides is not None else _load_crosswalk_overrides()
    regions = db.fetchall(
        conn,
        "SELECT region_id, region_cd, sig_cd, full_name, name, level FROM regions",
    )

    with db.tx(conn):
        for r in regions:
            sig = r.get("sig_cd") or (str(r.get("region_cd") or "")[:5]) or None
            if not sig:
                continue
            sido_nm, sgg_nm = split_full_name(r.get("full_name"))
            o = ov.get(sig) or ov.get(r["region_id"]) or {}
            has_ov = bool(o)
            cw = {
                "sig_cd": sig,
                "sido_nm": sido_nm,
                "sgg_nm": sgg_nm,
                "vworld_sig_cd": sig,  # 결정적
                "law_org": o.get("law_org"),
                "law_sborg": o.get("law_sborg"),
                "lofin_laf_cd": o.get("lofin_laf_cd"),
                "region_id": r["region_id"],
                "match_method": o.get("match_method", "manual") if has_ov else "deterministic",
                "confidence": float(o.get("confidence", 0.9)) if has_ov else 1.0,
                "verified": 1 if (not has_ov or o.get("verified")) else 0,
                "note": o.get("note"),
            }
            action = db.upsert(conn, "code_crosswalk", cw, "sig_cd")
            res[action] = res.get(action, 0) + 1
            db.execute(
                conn,
                "UPDATE regions SET vworld_sig_cd=?, "
                "law_org=COALESCE(?, law_org), law_sborg=COALESCE(?, law_sborg), "
                "lofin_laf_cd=COALESCE(?, lofin_laf_cd) WHERE region_id=?",
                (sig, o.get("law_org"), o.get("law_sborg"),
                 o.get("lofin_laf_cd"), r["region_id"]),
            )

    res["changed"] = res["inserted"] + res["updated"]
    res["rows"] = len(regions)
    res["overrides"] = len(ov)
    return res


# --------------------------------------------------------------------------- #
# compute_adjacency — region_adjacency(Queen)
# --------------------------------------------------------------------------- #
def _load_geoms(conn) -> list[dict]:
    out = []
    for r in db.fetchall(
        conn, "SELECT region_id, geojson, bbox, ring_signature FROM region_geometry"
    ):
        geojson = None
        if r.get("geojson"):
            try:
                geojson = json.loads(r["geojson"])
            except json.JSONDecodeError:
                geojson = None
        bbox = None
        if r.get("bbox"):
            try:
                bbox = json.loads(r["bbox"])
            except json.JSONDecodeError:
                bbox = None
        if bbox is None and geojson is not None:
            bbox = bbox_of(geojson)
        if r.get("ring_signature"):
            try:
                ring = set(json.loads(r["ring_signature"]))
            except json.JSONDecodeError:
                ring = set()
        elif geojson is not None:
            ring = set(ring_signature_of(geojson))
        else:
            ring = set()
        out.append({
            "region_id": r["region_id"],
            "geojson": geojson,
            "bbox": bbox,
            "ring": ring,
        })
    return out


def _adjacency_ring(geoms: list[dict], level_of: dict) -> tuple[dict, dict]:
    """순수파이썬 Queen: 양자화 경계 꼭짓점 공유 + 동일 level."""
    pt_index: dict[str, set] = defaultdict(set)
    for g in geoms:
        for p in g["ring"]:
            pt_index[p].add(g["region_id"])
    adj: dict[str, set] = defaultdict(set)
    for _p, rids in pt_index.items():
        if len(rids) < 2:
            continue
        rl = list(rids)
        for i in range(len(rl)):
            for j in range(i + 1, len(rl)):
                a, b = rl[i], rl[j]
                if level_of.get(a) != level_of.get(b):
                    continue  # 교차 level(시도↔시군구) 인접 배제
                adj[a].add(b)
                adj[b].add(a)
    return adj, {}


def _adjacency_shapely(geoms: list[dict], level_of: dict) -> tuple[dict, dict]:
    """shapely Queen: 경계 접촉(distance≈0) + 동일 level. shared_len 부수 계산."""
    built = []
    for g in geoms:
        if g["geojson"] is None:
            continue
        try:
            built.append((g["region_id"], _shapely_shape(g["geojson"]), g["bbox"]))
        except Exception:  # pragma: no cover - 손상 지오메트리
            continue
    adj: dict[str, set] = defaultdict(set)
    shared: dict[tuple, float] = {}
    n = len(built)
    for i in range(n):
        ida, ga, ba = built[i]
        for j in range(i + 1, n):
            idb, gb, bb = built[j]
            if level_of.get(ida) != level_of.get(idb):
                continue
            if not _bbox_overlap(ba, bb):
                continue
            try:
                if ga.distance(gb) < 1e-9 and not ga.equals(gb):
                    adj[ida].add(idb)
                    adj[idb].add(ida)
                    inter = ga.intersection(gb)
                    length = float(getattr(inter, "length", 0.0) or 0.0)
                    if length > 0:
                        shared[(ida, idb)] = length
                        shared[(idb, ida)] = length
            except Exception:  # pragma: no cover
                continue
    return adj, shared


def compute_adjacency(conn, *, method: str = "auto") -> dict:
    """region_geometry → region_adjacency(Queen). 무방향을 양방향 2행으로 저장.

    method: 'auto'(shapely 있으면 정밀, 없으면 폴백) | 'shapely' | 'ring-share'/'pure'.
    동일 level(시도끼리·시군구끼리)만 인접 판정(교차 level 배제).
    """
    log = get_logger("policymap.geo")
    res = _result()
    geoms = _load_geoms(conn)
    if not geoms:
        res["regions"] = 0
        res["pairs"] = 0
        return res

    level_of = {
        r["region_id"]: r["level"]
        for r in db.fetchall(conn, "SELECT region_id, level FROM regions")
    }

    want_shapely = method in ("auto", "shapely") and _HAS_SHAPELY
    if method == "shapely" and not _HAS_SHAPELY:
        log.warning("method='shapely' 요청됐으나 shapely 미설치 → ring-share 폴백")
    if want_shapely:
        adj, shared = _adjacency_shapely(geoms, level_of)
        method_lbl = "shapely"
    else:
        adj, shared = _adjacency_ring(geoms, level_of)
        method_lbl = "ring-share"

    now = now_kst_iso()
    pairs = 0
    with db.tx(conn):
        for a, nbrs in adj.items():
            for b in nbrs:
                pairs += 1
                row = {
                    "region_id": a,
                    "neighbor_id": b,
                    "contiguity_type": "queen",
                    "same_province": 1 if str(a)[:2] == str(b)[:2] else 0,
                    "shared_len": shared.get((a, b)),
                    "method": method_lbl,
                    "computed_at": now,
                }
                action = db.upsert(conn, "region_adjacency", row, ("region_id", "neighbor_id"))
                res[action] = res.get(action, 0) + 1

    res["changed"] = res["inserted"] + res["updated"]
    res["regions"] = len(geoms)
    res["pairs"] = pairs
    res["method"] = method_lbl
    return res


__all__ = [
    "collect_regions", "collect_boundaries", "build_crosswalk", "compute_adjacency",
    "apply_reorg_event", "REORG_EVENTS",
    "stan_regin_list", "vworld_features",
    "region_rows_from_stanregin", "classify_region", "parse_vworld_feature",
    "geometry_points", "bbox_of", "centroid_of", "ring_signature_of",
    "normalize_gov_name", "split_full_name",
    "NATIONAL_BOX",
]
