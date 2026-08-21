"""policymap.reference — Gather 단계 실데이터(reference/) → SQLite 실적재.

이 모듈은 data/reference/ 아래에 Gather 서브에이전트가 저장한 **실데이터 파일**을
policymap 스키마(regions/region_geometry/region_succession/code_crosswalk + population)에
적재한다. 외부 API 호출 없음(파일 기반). db.py 공개 API 만 사용하며 멱등(재실행 unchanged).

입력 파일(data/reference/):
  * regions_bjd.json         : code.go.kr 법정동코드 전체자료 파생 시도·시군구 스파인(537행)
  * skorea-municipalities-2018-geo.json : southkorea-maps 2018 시군구 경계(250 features, KOSTAT code)
  * kostat_to_bjd.json       : KOSTAT/SGIS 통계코드 → 법정동 sig_cd 크로스워크(250)
  * reorg_events.json        : 검증된 행정구역 개편 이벤트(2023~2026, 5건)
  * population_sigungu.csv   : 주민등록인구 2026-07 자치단체 레벨(239행, sig_cd 조인)

공개 함수:
    load_regions_bjd(conn)        -> dict   # regions 실적재(시도16+시군구+일반구)
    attach_geometry(conn)         -> dict   # region_geometry(중심점·bbox·ring_signature)
    apply_verified_reorgs(conn)   -> dict   # region_succession + 폐지 툼스톤(verified만)
    load_population(conn)         -> dict   # regions.population 백필(sig_cd 조인)

규율(CONTRACTS.md 승계):
  * db.py 공개 API(upsert/soft_delete/log_change/fetchall/execute/tx)만 사용.
  * 폐지/통합은 하드삭제 금지 → soft_delete 툼스톤.
  * 무변경=무이벤트 → 변경 없으면 change_log 미기록, upsert unchanged.
  * 지어내기 금지 → 미확정(reorg successors 공란 등)은 보류(pending), 추정값 삽입 안 함.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Optional

from . import db
from .collectors import geo
from .util import content_hash, get_logger, now_kst_iso, today_kst

# 선택적 정밀 지오메트리(중심점·bbox). 없으면 geo.py 순수파이썬 폴백.
try:  # pragma: no cover - 환경 의존
    from shapely.geometry import shape as _shapely_shape  # type: ignore
    _HAS_SHAPELY = True
except Exception:  # pragma: no cover
    _shapely_shape = None  # type: ignore
    _HAS_SHAPELY = False

# reference 파일 디렉터리(= 이 저장소의 data/reference).
REFERENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"

_F_REGIONS = "regions_bjd.json"
_F_GEOJSON = "skorea-municipalities-2018-geo.json"
_F_KOSTAT = "kostat_to_bjd.json"
_F_REORG = "reorg_events.json"
_F_POP = "population_sigungu.csv"

# regions 무변경 판정 추적 컬럼(무변경=무이벤트). 제외 항목:
#   * population/centroid/geometry_ref : 별도 단계가 채움(되돌리지 않게).
#   * status/valid_to : apply_verified_reorgs 가 tombstone 을 정밀화(merged/renamed +
#     시행일)하므로 여기서 재판정하면 오실레이션. 활성↔폐지 전이만 별도 처리(_newly_abolished).
_REGION_TRACK = (
    "name", "full_name", "level", "region_cd", "sig_cd", "sido_cd", "sgg_cd",
    "parent_region", "has_legislation",
)


def _result(**extra: Any) -> dict:
    r = {"inserted": 0, "updated": 0, "unchanged": 0,
         "changed": 0, "errors": 0, "status": "ok"}
    r.update(extra)
    return r


def _reference_dir(base: Optional[str | Path]) -> Path:
    return Path(base) if base else REFERENCE_DIR


# --------------------------------------------------------------------------- #
# 1. load_regions_bjd — regions 스파인 실적재
# --------------------------------------------------------------------------- #
def _region_id(sig_cd: str, level: int) -> str:
    """정규 region_id: 시도(level1)=sig_cd 앞2자리, 시군구·일반구=sig_cd 5자리."""
    sig_cd = str(sig_cd)
    return sig_cd[:2] if level == 1 else sig_cd[:5]


def _short_name(full: Optional[str]) -> Optional[str]:
    if not full:
        return full
    parts = str(full).split()
    return parts[-1] if parts else full


def _status_of(is_abolished: bool) -> str:
    return "abolished" if is_abolished else "active"


def region_rows_from_bjd(records: list[dict]) -> list[dict]:
    """regions_bjd.json regions[] → regions upsert 행[]. 순수(테스트 가능)."""
    # parent_cd(10자리 region_cd) → region_id 해석용 맵.
    recmap = {
        str(r.get("region_cd")): _region_id(r.get("sig_cd") or "", int(r.get("level") or 2))
        for r in records
    }
    asof = today_kst()
    out: list[dict] = []
    for r in records:
        level = int(r.get("level") or 2)
        sig = str(r.get("sig_cd") or "")
        rid = _region_id(sig, level)
        parent_cd = r.get("parent_cd")
        parent_region = recmap.get(str(parent_cd)) if parent_cd else None
        full = r.get("name")
        is_ab = bool(r.get("is_abolished"))
        out.append({
            "region_id": rid,
            "region_cd": r.get("region_cd"),
            "sig_cd": sig,
            "sido_cd": sig[:2],
            "sgg_cd": sig[2:5],
            "name": _short_name(full),
            "full_name": full,
            "level": level,
            "parent_region": parent_region,
            "has_legislation": 0 if level == 3 else 1,
            "status": _status_of(is_ab),
            "valid_from": None,
            "valid_to": r.get("abolished_date") if is_ab else None,
            "as_of_date": asof,
            "source": "code.go.kr:StanReginCd",
        })
    return out


def _region_diff(prev: dict, row: dict) -> list[str]:
    return [k for k in _REGION_TRACK if str(prev.get(k)) != str(row.get(k))]


def load_regions_bjd(conn, *, reference_dir: Optional[str | Path] = None,
                     run_id: Optional[str] = None) -> dict:
    """regions_bjd.json → regions 실적재. 시도/시군구/일반구 계층·툼스톤·FK 보존.

    멱등: 추적 컬럼 무변경 시 upsert 생략(unchanged), change_log 미기록.
    """
    log = get_logger("policymap.reference")
    res = _result()
    path = _reference_dir(reference_dir) / _F_REGIONS
    if not path.exists():
        res["status"] = "error"
        res["errors"] = 1
        res["note"] = f"파일 없음: {path}"
        return res

    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("regions") if isinstance(payload, dict) else payload
    rows = region_rows_from_bjd(list(records or []))
    all_ids = {r["region_id"] for r in rows}

    existing = {
        r["region_id"]: r
        for r in db.fetchall(
            conn,
            "SELECT region_id, name, full_name, level, region_cd, sig_cd, sido_cd, "
            "sgg_cd, parent_region, has_legislation, status, valid_to FROM regions",
        )
    }
    # FK(parent self-ref) 충족: level 오름차순(1→2→3) 삽입.
    rows.sort(key=lambda r: (r.get("level", 9), r["region_id"]))
    now = now_kst_iso()

    with db.tx(conn):
        for row in rows:
            parent = row.get("parent_region")
            if parent and parent not in all_ids and parent not in existing:
                row["parent_region"] = None  # 미확보 상위 → FK 회피
            prev = existing.get(row["region_id"])
            if prev is None:
                row["updated_at"] = now
                db.upsert(conn, "regions", row, "region_id")
                res["inserted"] += 1
                db.log_change(
                    conn, entity_type="boundary", entity_id=row["region_id"],
                    event="created", source="boundary", scope="reference:regions_bjd",
                    entity_name=row.get("name"), region_code=row.get("sig_cd"),
                    run_id=run_id,
                )
            else:
                diff = _region_diff(prev, row)
                # 활성→폐지 최초 전이만 status/valid_to 갱신. 이미 tombstone 인 지역은
                # apply_verified_reorgs 의 정밀화(merged/renamed·시행일)를 보존(오실레이션 방지).
                newly_abolished = (
                    str(prev.get("status")) == "active" and row["status"] != "active"
                )
                if diff or newly_abolished:
                    if not newly_abolished:
                        row.pop("status", None)
                        row.pop("valid_to", None)
                    else:
                        diff = diff + ["status"]
                    row["updated_at"] = now
                    db.upsert(conn, "regions", row, "region_id")
                    res["updated"] += 1
                    db.log_change(
                        conn, entity_type="boundary", entity_id=row["region_id"],
                        event="amended", source="boundary",
                        scope="reference:regions_bjd", entity_name=row.get("name"),
                        region_code=row.get("sig_cd"),
                        fields_changed=json.dumps(diff, ensure_ascii=False),
                        run_id=run_id,
                    )
                else:
                    res["unchanged"] += 1

    res["changed"] = res["inserted"] + res["updated"]
    res["regions"] = len(rows)
    res["active"] = sum(1 for r in rows if r["status"] == "active")
    res["abolished"] = sum(1 for r in rows if r["status"] != "active")
    log.info("load_regions_bjd: 총 %d(활성 %d/폐지 %d) 신규 %d 갱신 %d 무변경 %d",
             res["regions"], res["active"], res["abolished"],
             res["inserted"], res["updated"], res["unchanged"])
    return res


# --------------------------------------------------------------------------- #
# 2. attach_geometry — region_geometry(2018 경계 + KOSTAT→sig_cd 크로스워크)
# --------------------------------------------------------------------------- #
def _shapely_centroid_bbox(geom: dict):
    """shapely 로 (centroid_lon, centroid_lat, bbox[minx,miny,maxx,maxy]). 실패 시 폴백."""
    if _HAS_SHAPELY:
        try:
            g = _shapely_shape(geom)
            minx, miny, maxx, maxy = g.bounds
            c = g.representative_point()  # 폴리곤 내부 보장(오목형 안전)
            return float(c.x), float(c.y), [float(minx), float(miny), float(maxx), float(maxy)]
        except Exception:  # pragma: no cover
            pass
    bbox = geo.bbox_of(geom)
    lon, lat = geo.centroid_of(geom)
    return lon, lat, bbox


def attach_geometry(conn, *, reference_dir: Optional[str | Path] = None,
                    run_id: Optional[str] = None) -> dict:
    """2018 시군구 GeoJSON + kostat_to_bjd 매핑으로 region_geometry 적재.

    각 feature.properties.code(KOSTAT) → kostat_to_bjd[code].bjd_sig_cd → regions.sig_cd 조인.
    shapely 로 중심점·bbox 계산, ring_signature(Queen 인접용) 저장. 멱등(geojson 해시 가드).
    """
    log = get_logger("policymap.reference")
    res = _result(skipped=0, unmapped=0)
    base = _reference_dir(reference_dir)
    geo_path, kostat_path = base / _F_GEOJSON, base / _F_KOSTAT
    if not geo_path.exists() or not kostat_path.exists():
        res["status"] = "error"
        res["errors"] = 1
        res["note"] = f"파일 없음: {geo_path if not geo_path.exists() else kostat_path}"
        return res

    mapping = json.loads(kostat_path.read_text(encoding="utf-8")).get("mapping") or {}
    fc = json.loads(geo_path.read_text(encoding="utf-8"))
    feats = fc.get("features") or []

    # sig_cd → region_id(활성 우선). level 2/3 는 region_id==sig_cd 이지만 안전하게 조회.
    sig_to_rid: dict[str, str] = {}
    for r in db.fetchall(conn, "SELECT region_id, sig_cd, level, status FROM regions"):
        sig = r.get("sig_cd")
        if not sig:
            continue
        cur = sig_to_rid.get(sig)
        if cur is None or r.get("status") == "active":
            sig_to_rid[sig] = r["region_id"]

    existing_geo = {
        r["region_id"]: r["geojson"]
        for r in db.fetchall(conn, "SELECT region_id, geojson FROM region_geometry")
    }
    now = now_kst_iso()

    with db.tx(conn):
        for feat in feats:
            props = feat.get("properties") or {}
            geom = feat.get("geometry")
            code = str(props.get("code") or "").strip()
            if geom is None or not code:
                res["errors"] += 1
                continue
            m = mapping.get(code)
            bjd_sig = (m or {}).get("bjd_sig_cd")
            if not bjd_sig:
                res["unmapped"] += 1
                continue
            rid = sig_to_rid.get(str(bjd_sig))
            if not rid:
                res["skipped"] += 1  # regions 스파인에 sig_cd 부재
                continue

            geojson_str = json.dumps(geom, ensure_ascii=False, separators=(",", ":"))
            prev = existing_geo.get(rid)
            if prev is not None and content_hash(prev) == content_hash(geojson_str):
                res["unchanged"] += 1
                continue
            lon, lat, bbox = _shapely_centroid_bbox(geom)
            grow = {
                "region_id": rid,
                "crs": "EPSG:4326",
                "geojson": geojson_str,
                "bbox": json.dumps(bbox) if bbox else None,
                "ring_signature": json.dumps(geo.ring_signature_of(geom)),
                "fetched_at": now,
            }
            db.upsert(conn, "region_geometry", grow, "region_id")
            if prev is None:
                res["inserted"] += 1
            else:
                res["updated"] += 1
            db.execute(
                conn,
                "UPDATE regions SET geometry_ref=?, centroid_lon=?, centroid_lat=?, "
                "updated_at=? WHERE region_id=?",
                (rid, lon, lat, now, rid),
            )

    res["changed"] = res["inserted"] + res["updated"]
    res["features"] = len(feats)
    res["source_note"] = ("southkorea-maps 2018 경계 — 2026 개편(전남광주통합·인천·화성 등) "
                          "미반영, 좌표는 2018 형상(확인 필요)")
    log.info("attach_geometry: features %d 적재 %d(신규 %d/갱신 %d) 무변경 %d 미조인 %d 미매핑 %d",
             res["features"], res["changed"], res["inserted"], res["updated"],
             res["unchanged"], res["skipped"], res["unmapped"])
    return res


# --------------------------------------------------------------------------- #
# 3. apply_verified_reorgs — 검증된 개편 → region_succession + 툼스톤
# --------------------------------------------------------------------------- #
# 개편 유형 문자열 → succession_type 통제어휘('통합'|'분리'|'개칭'|'승계').
def _succession_type(raw: Optional[str]) -> str:
    t = str(raw or "")
    if "통합" in t:
        return "통합"
    if "분리" in t or "분구" in t:
        return "분리"
    if "명칭변경" in t or "개칭" in t:
        return "개칭"
    return "승계"


def _tombstone_status(raw: Optional[str]) -> str:
    t = str(raw or "")
    if "통합" in t or "편입" in t or "분구" in t or "신설" in t:
        return "merged"
    if "명칭변경" in t or "개칭" in t:
        return "renamed"
    return "abolished"


def apply_verified_reorgs(conn, *, reference_dir: Optional[str | Path] = None,
                          run_id: Optional[str] = None) -> dict:
    """reorg_events.json 중 verified 이벤트만 region_succession + 폐지 툼스톤 적용.

    predecessors/successors 는 region_id(시도 2자리 또는 시군구 5자리) 그대로 사용.
    successors 공란(신설 코드 미확정) 이벤트는 승계 엣지 보류(pending)하고 툼스톤만.
    멱등: succession upsert·soft_delete 재실행 무변경.
    """
    log = get_logger("policymap.reference")
    res = _result(successions=0, tombstones=0, pending=0, events=0, skipped_events=0)
    path = _reference_dir(reference_dir) / _F_REORG
    if not path.exists():
        res["status"] = "error"
        res["errors"] = 1
        res["note"] = f"파일 없음: {path}"
        return res

    events = json.loads(path.read_text(encoding="utf-8"))
    present = {r["region_id"] for r in db.fetchall(conn, "SELECT region_id FROM regions")}
    detail: list[dict] = []

    with db.tx(conn):
        for ev in events:
            if not ev.get("verified"):
                res["skipped_events"] += 1
                continue
            res["events"] += 1
            eid = ev.get("event_id")
            eff = ev.get("effective_date")
            styp = _succession_type(ev.get("type"))
            tstat = _tombstone_status(ev.get("type"))
            basis = ev.get("legal_basis")
            snote = ev.get("successors_note") or ev.get("note")
            preds = [str(p) for p in (ev.get("predecessors") or [])]
            succs = [str(s) for s in (ev.get("successors") or [])]
            ev_succ = 0

            for old in preds:
                if old not in present:
                    continue
                # 승계 엣지(신규 코드 확정된 이벤트만).
                for new in succs:
                    if new in present:
                        action = db.upsert(
                            conn, "region_succession",
                            {
                                "old_region_id": old,
                                "new_region_id": new,
                                "succession_type": styp,
                                "effective_date": eff,
                                "legal_basis": basis,
                                "status_note": snote,
                            },
                            ("old_region_id", "new_region_id"),
                        )
                        res["successions"] += 1
                        ev_succ += 1
                # 폐지 지자체 툼스톤(하드삭제 금지).
                db.soft_delete(
                    conn, "regions", {"region_id": old},
                    status=tstat, valid_to_col="valid_to", valid_to=eff,
                )
                res["tombstones"] += 1
                db.log_change(
                    conn, entity_type="boundary", entity_id=old,
                    event="boundary_reorg", source="boundary",
                    scope="reference:reorg", entity_name=ev.get("name"),
                    before=old, after=(succs[0] if succs else None), run_id=run_id,
                )
            if not succs:
                res["pending"] += len(preds)
            detail.append({"event": eid, "successions": ev_succ,
                           "pending": (0 if succs else len(preds)),
                           "verified": True})

    res["changed"] = res["successions"] + res["tombstones"]
    res["detail"] = detail
    log.info("apply_verified_reorgs: 이벤트 %d 승계엣지 %d 툼스톤 %d 보류(pending) %d",
             res["events"], res["successions"], res["tombstones"], res["pending"])
    return res


# --------------------------------------------------------------------------- #
# 4. load_population — regions.population 백필(sig_cd 조인)
# --------------------------------------------------------------------------- #
def load_population(conn, *, reference_dir: Optional[str | Path] = None,
                    run_id: Optional[str] = None) -> dict:
    """population_sigungu.csv(주민등록 2026-07) → regions.population 백필.

    sig_cd 5자리 조인(활성 지역만). 멱등: 동일값이면 갱신 생략.
    스키마에 regions.population 컬럼 존재(db/schema.sql). 스키마 변경 불필요.
    """
    log = get_logger("policymap.reference")
    res = _result(matched=0, missing=0)
    path = _reference_dir(reference_dir) / _F_POP
    if not path.exists():
        res["status"] = "error"
        res["errors"] = 1
        res["note"] = f"파일 없음: {path}"
        return res

    if "population" not in db.table_columns(conn, "regions"):
        res["status"] = "error"
        res["errors"] = 1
        res["note"] = "regions.population 컬럼 부재 — schema.sql 확인 필요"
        return res

    with open(path, encoding="utf-8", newline="") as f:
        pop_rows = list(csv.DictReader(f))

    # sig_cd → (region_id, 현재 population) 활성 우선.
    cur: dict[str, dict] = {}
    for r in db.fetchall(
        conn, "SELECT region_id, sig_cd, population, status FROM regions"
    ):
        sig = r.get("sig_cd")
        if not sig:
            continue
        if sig not in cur or r.get("status") == "active":
            cur[sig] = r
    now = now_kst_iso()
    base_ym = None

    with db.tx(conn):
        for row in pop_rows:
            sig = str(row.get("sig_cd") or "").strip()
            base_ym = row.get("base_ym") or base_ym
            try:
                pop = int(row.get("population"))
            except (TypeError, ValueError):
                res["errors"] += 1
                continue
            target = cur.get(sig)
            if not target:
                res["missing"] += 1
                continue
            res["matched"] += 1
            if str(target.get("population")) == str(pop):
                res["unchanged"] += 1
                continue
            db.execute(
                conn,
                "UPDATE regions SET population=?, updated_at=? WHERE region_id=?",
                (pop, now, target["region_id"]),
            )
            res["updated"] += 1

    res["changed"] = res["inserted"] + res["updated"]
    res["rows"] = len(pop_rows)
    res["base_ym"] = base_ym
    log.info("load_population: csv %d행 매칭 %d(갱신 %d/무변경 %d) 미조인 %d base_ym=%s",
             res["rows"], res["matched"], res["updated"], res["unchanged"],
             res["missing"], base_ym)
    return res


__all__ = [
    "load_regions_bjd", "attach_geometry", "apply_verified_reorgs", "load_population",
    "region_rows_from_bjd", "REFERENCE_DIR",
]
