"""policymap.collectors.budget — 지방재정365 QWGJK 세부사업별 세출현황 수집.

계약(CONTRACTS.md §1.4):
    budget_list(http, cfg, *, fyr, laf_cd=None, dbiz_nm=None,
                pIndex=1, pSize=100) -> tuple[int, list[dict]]
        # /lf/hub/QWGJK. Type=json(대문자). ERROR-300/310 해석.
    collect_budget(http, cfg, conn, *, laf_cd: str, fyr: int, run_id=None) -> dict
        # 파티션(laf_cd×fyr) 단위. 행 정렬 후 파티션 sha256 → last_hash 다를 때만 upsert.
        # budget_id='ehojo-{laf_cd}-{fyr}-{seq}'. 자연키 (laf_cd, fyr, dbiz_cd).
        # source='budget', scope=f'laf:{laf_cd}|fyr:{fyr}', cursor='exe:{max exe_ymd}'.
        # 확정연도(현재-2 이하)는 분기 1회만 재확인(동결).
    collect_budget_nationwide(http, cfg, conn, *, fyr, laf_cds=None, resume=True) -> dict
        # regions.lofin_laf_cd(243개) 전수 순회. 지자체별 watermark 로 재개 가능.
        # source='budget', scope=f'nationwide|fyr:{fyr}' 에 진행 커서 기록.
        # 실패 지자체는 격리(건너뛰고 집계). 호출수 상한·QPS 스로틀 내장.

대상 테이블: budget_lines. (조례↔예산 매칭은 graph/analysis 의 link_ordinance_budget)

설계 규율(지방재정365 공식 명세 + 실키 실측, 2026-08-19 확정):
  * 엔드포인트: https://www.lofin365.go.kr/lf/hub/QWGJK  — 경로 끝 세그먼트가 SERVICE.
  * 기본인자(전부 필수): Key(인증키) · Type(xml|json) · pIndex · pSize. [공식 명세]
  * 검색 요청인자: fyr(회계연도, **필수**) · exe_ymd(집행일자 YYYYMMDD, **필수**)
      / wa_laf_cd(지역코드, 선택) · laf_cd(자치단체코드 7자리, 선택) · dbiz_nm(세부사업명, 선택).
      → exe_ymd 누락이 기존 ERROR-300 의 원인이었다. [실측]
  * 샘플URL: /lf/hub/QWGJK?fyr=2020&exe_ymd=20200405 [공식 명세]
  * pSize 최대 1000 (초과 시 ERROR-336). [실측]
  * laf_cd 는 7자리(예 서울종로구 1111000). 5자리 행정표준코드는 INFO-200. [실측]
  * 데이터 없음(미래일자·미적재일)은 INFO-200 → NotFound. 일간 적재라 당일자는
    아직 없을 수 있어 exe_ymd 미지정 시 앵커일부터 과거로 최대 EXE_YMD_LOOKBACK 일 탐색.
  * 오류코드: ERROR-300(필수값 누락)·ERROR-310(SERVICE 미존재)·ERROR-33x(요청량)·
    ERROR-29x(키). [실측]
  * 출력 25필드 영문 필드명 확정(_FIELD_MAP 의 첫 후보키). [실측]
  * 요청제한 없음. 대량 수집은 laf_cd×fyr 파티션 루프(run.py 오케스트레이션).
  * 확정연도 동결: 회계연도가 (현재연도-2) 이하이면 분기(90일) 1회만 재확인.

주요업무계획 HWP 파서(parse_business_plan_hwp)는 스텁이다:
  olefile/pyhwp 가 없으면 skip(빈 리스트). 인터페이스만 노출하며 collect_budget 와
  분리되어 있다(예산 API 가 정본, HWP 는 보조 근거).

코어는 표준라이브러리만으로 동작한다. 네트워크 키가 없으면 cfg.require 가 명확히 예외를
던지지만, 파싱·정규화(parse_budget_row / _lofin_envelope)는 순수함수라 픽스처로 단위테스트
가능하다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from .. import db
from ..util import (
    NotFound,
    PermanentError,
    RateLimiter,
    compact,
    content_hash,
    get_logger,
    now_kst_iso,
    stable_id,
    today_kst,
)

log = get_logger("policymap.collectors.budget")

# QWGJK 서비스 식별자 = 엔드포인트 경로 끝 세그먼트.
SERVICE = "QWGJK"
SOURCE = "budget"

# 확정연도 동결 임계(현재연도 - 이 값 이하이면 동결) 및 재확인 주기(일).
FROZEN_OFFSET = 2
FROZEN_RECHECK_DAYS = 90

# 1회 요청 최대 건수(초과 시 ERROR-336). [실측]
MAX_PSIZE = 1000
# exe_ymd 미지정 시 앵커일에서 과거로 되짚어 볼 최대 일수(일간 적재 지연 대비).
EXE_YMD_LOOKBACK = 10

# --- 전국 전수 수집(collect_budget_nationwide) 기본값 -------------------------- #
# 초당 요청 상한(예의상 스로틀). HttpClient 자체 limiter 위에 한 겹 더 씌운다.
NATIONWIDE_QPS = 3.0
# 1회 전수 수집의 총 HTTP 호출 상한(초과 시 partial 로 안전 중단 → 재개 가능).
NATIONWIDE_MAX_CALLS = 2000
# resume=True 일 때 "이미 끝난 지자체"로 보고 건너뛸 최근 성공 유효시간(시간).
RESUME_FRESH_HOURS = 12.0
# 진행 로그·커서 저장 주기(지자체 수).
PROGRESS_EVERY = 10

# --------------------------------------------------------------------------- #
# 출력필드 매핑(컬럼 → 후보 원천키). 실키로 25필드 영문명 확정 시 여기만 보강.
#   - 키는 대소문자 무시 매칭. 확정 파악분(§2.2)의 한글 헤더도 후보에 포함.
# --------------------------------------------------------------------------- #
#   [실측 2026-08-19] 첫 후보키가 QWGJK 실제 출력 필드명(25필드). 나머지는 방어용 폴백.
_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "dbiz_cd":      ("dbiz_cd", "DBIZ_CD", "detailBizCode", "sbizCd", "세부사업코드"),
    "dbiz_nm":      ("dbiz_nm", "DBIZ_NM", "detailBizName", "sbizNm", "세부사업명"),
    "dept_cd":      ("dept_cd", "DEPT_CD", "deptCode", "부서코드"),
    "acct_name":    ("acnt_dv_nm", "ACNT_DV_NM", "acct_nm", "acnutSeNm",
                     "회계구분명", "회계명"),
    "field":        ("fld_nm", "FLD_NM", "fld", "realmNm", "분야명", "분야"),
    "sector":       ("part_nm", "PART_NM", "part", "sectorNm", "부문명", "부문"),
    "budget_now":   ("bdg_cash_amt", "BDG_CASH_AMT", "budget_now", "bdg_amt",
                     "예산현액", "현액"),
    "gov_fund":     ("bdg_ntep", "BDG_NTEP", "gov_fund", "natl_amt", "국비"),
    "sido_fund":    ("capep", "CAPEP", "sido_fund", "ctpv_amt", "시도비"),
    "sigungu_fund": ("sggep", "SGGEP", "sigungu_fund", "signgu_amt", "시군구비"),
    "alloc_amt":    ("cpl_amt", "CPL_AMT", "alloc_amt", "cmpsng_amt",
                     "편성액", "예산액"),
    "exe_amt":      ("ep_amt", "EP_AMT", "exe_amt", "expend_amt", "지출액", "집행액"),
    "exe_ymd":      ("exe_ymd", "EXE_YMD", "expendYmd", "집행일자"),
}
# 정수(원 단위) 금액 컬럼 — 콤마 제거 후 int 변환.
_INT_COLS = ("budget_now", "gov_fund", "sido_fund", "sigungu_fund",
             "alloc_amt", "exe_amt")
# 파티션 해시에서 제외할 휘발성 컬럼(적재시각·조인 파생).
_VOLATILE_COLS = ("as_of_date", "updated_at", "region_id", "source")


# --------------------------------------------------------------------------- #
# 순수 헬퍼(네트워크 불요 — 단위테스트 대상)
# --------------------------------------------------------------------------- #
def _pick(raw: dict, keys: Iterable[str]) -> Any:
    """후보키 순서대로 첫 비어있지 않은 값. 대소문자 무시 폴백 포함."""
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    low = {str(k).lower(): v for k, v in raw.items()}
    for k in keys:
        v = low.get(str(k).lower())
        if v not in (None, ""):
            return v
    return None


def _to_int(v: Any) -> Optional[int]:
    """금액 문자열(콤마·공백·부호) → int. 빈값/파싱실패 → None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = str(v).strip().replace(",", "").replace(" ", "")
    if s in ("", "-", "null", "None"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def exe_ymd_candidates(fyr: int | str, *, lookback: int = EXE_YMD_LOOKBACK) -> list[str]:
    """exe_ymd(필수) 미지정 시 시도할 집행일자 후보(YYYYMMDD, 최신→과거).

    * 당해연도: 오늘(KST) 기준. 일간 적재라 당일자는 미적재일 수 있어 과거로 되짚는다.
    * 과거연도: 해당 회계연도 12/31(연말 누적 스냅샷) 기준.
    회계연도 밖 날짜는 INFO-200 이므로 fyr 범위를 벗어나면 후보에서 제외한다.
    """
    try:
        yr = int(str(fyr))
    except (TypeError, ValueError):
        return []
    today = today_kst()
    cur_year = int(today[:4])
    if yr >= cur_year:
        anchor = datetime.strptime(today, "%Y-%m-%d")
        if yr > cur_year:  # 미래 회계연도는 조회 대상 아님
            return []
    else:
        anchor = datetime(yr, 12, 31)
    out: list[str] = []
    for i in range(max(1, lookback)):
        d = anchor - timedelta(days=i)
        if d.year != yr:
            break
        out.append(d.strftime("%Y%m%d"))
    return out


def _budget_id(laf_cd: str, fyr: int | str, dbiz_cd: Any, *fallback: Any) -> str:
    """'ehojo-{laf_cd}-{fyr}-{seq}'. seq=세부사업코드(정규화), 없으면 결정적 해시.

    자연키 (laf_cd, fyr, dbiz_cd) 와 1:1이 되도록 seq 를 자연키에서 유도한다
    → 재실행 멱등성 보장(순번을 실행순서에 의존시키지 않음).
    """
    if dbiz_cd not in (None, ""):
        seq = compact(str(dbiz_cd)) or stable_id(dbiz_cd, length=10)
    else:
        seq = stable_id(laf_cd, fyr, *fallback, length=10)
    return f"ehojo-{laf_cd}-{fyr}-{seq}"


def parse_budget_row(
    raw: dict,
    *,
    laf_cd: Optional[str] = None,
    fyr: Optional[int | str] = None,
    as_of_date: Optional[str] = None,
) -> dict:
    """QWGJK 원천 레코드 1건 → budget_lines row dict(순수함수).

    laf_cd/fyr 미지정 시 raw 에서 유도(자치단체코드/회계연도 후보키).
    db.upsert 가 실존 컬럼만 취하므로 여분 키는 무해하나 최소 컬럼으로 구성한다.
    """
    laf = laf_cd if laf_cd not in (None, "") else _pick(
        raw, ("laf_cd", "LAF_CD", "자치단체코드", "orgCd"))
    yr = fyr if fyr not in (None, "") else _pick(
        raw, ("fyr", "FYR", "회계연도", "acntYr"))
    try:
        yr_int = int(str(yr)) if yr not in (None, "") else None
    except (TypeError, ValueError):
        yr_int = None

    dbiz_cd = _pick(raw, _FIELD_MAP["dbiz_cd"])
    dbiz_nm = _pick(raw, _FIELD_MAP["dbiz_nm"])
    dept_cd = _pick(raw, _FIELD_MAP["dept_cd"])

    row: dict[str, Any] = {
        "budget_id": _budget_id(laf, yr_int, dbiz_cd, dbiz_nm, dept_cd),
        "fyr": yr_int,
        "laf_cd": str(laf) if laf is not None else None,
        "dbiz_cd": str(dbiz_cd) if dbiz_cd is not None else None,
        "dbiz_nm": dbiz_nm,
        "dept_cd": dept_cd,
        "acct_name": _pick(raw, _FIELD_MAP["acct_name"]),
        "field": _pick(raw, _FIELD_MAP["field"]),
        "sector": _pick(raw, _FIELD_MAP["sector"]),
        "exe_ymd": _pick(raw, _FIELD_MAP["exe_ymd"]),
        "as_of_date": as_of_date or today_kst(),
        "source": "e호조",
        "updated_at": now_kst_iso(),
    }
    for col in _INT_COLS:
        row[col] = _to_int(_pick(raw, _FIELD_MAP[col]))
    return row


def _deep_find(obj: Any, key: str) -> Any:
    """중첩 dict/list 를 DFS 하여 첫 매칭 key 의 값 반환(없으면 None)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _deep_find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for it in obj:
            r = _deep_find(it, key)
            if r is not None:
                return r
    return None


def _lofin_envelope(payload: Any) -> tuple[int, list[dict]]:
    """QWGJK 응답 envelope 해석(순수함수) → (list_total_count, row[]).

    허브 표준의 여러 shape 를 방어적으로 흡수한다:
      * 정상(assembly/서울 허브형): {SERVICE:[{head:[{list_total_count},{RESULT:{CODE}}]},
                                              {row:[...]}]}
      * 평면형: {SERVICE:{list_total_count, RESULT:{CODE}, row:[...]}}
      * 오류형: {"RESULT":[{CODE,MESSAGE}]} 또는 {"RESULT":{CODE,MESSAGE}}

    코드 해석:
      ERROR-30x → 필수/요청인자 오류(PermanentError)
      ERROR-31x → SERVICE 미존재(PermanentError)
      ERROR-29x → 인증키(PermanentError)
      기타 ERROR- → PermanentError
      데이터 없음(INFO-200 등, row 빈값) → NotFound(정상적 없음)
    """
    code = _deep_find(payload, "CODE") or _deep_find(payload, "resultCode")
    msg = _deep_find(payload, "MESSAGE") or _deep_find(payload, "resultMsg") or ""
    if code is not None:
        cu = str(code).strip().upper()
        if cu.startswith("ERROR"):
            if cu.startswith("ERROR-30"):
                raise PermanentError(f"QWGJK 요청인자 오류 {cu}: {msg}")
            if cu.startswith("ERROR-31"):
                raise PermanentError(f"QWGJK SERVICE 오류 {cu}: {msg}")
            if cu.startswith("ERROR-29"):
                raise PermanentError(f"QWGJK 인증키 오류 {cu}: {msg}")
            raise PermanentError(f"QWGJK 오류 {cu}: {msg}")

    rows_raw = _deep_find(payload, "row")
    rows = rows_raw if isinstance(rows_raw, list) else ([rows_raw] if rows_raw else [])
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        raise NotFound(f"QWGJK 데이터 없음 (code={code}, msg={msg!r})")

    total = _deep_find(payload, "list_total_count")
    if total is None:
        total = _deep_find(payload, "total_count")
    try:
        total_int = int(total) if total is not None else len(rows)
    except (TypeError, ValueError):
        total_int = len(rows)
    return total_int, rows


# --------------------------------------------------------------------------- #
# 저수준 호출(계약 시그니처)
# --------------------------------------------------------------------------- #
def budget_list(
    http,
    cfg,
    *,
    fyr: int | str,
    exe_ymd: Optional[str] = None,
    laf_cd: Optional[str] = None,
    wa_laf_cd: Optional[str] = None,
    dbiz_nm: Optional[str] = None,
    pIndex: int = 1,
    pSize: int = 100,
) -> tuple[int, list[dict]]:
    """QWGJK 세부사업별 세출현황 1페이지 조회 → (총건수, row[]).

    필수: Key·Type·pIndex·pSize·fyr·exe_ymd. exe_ymd 미지정 시 exe_ymd_candidates()
    의 첫 후보(당해연도=오늘, 과거연도=12/31)를 사용한다 — 여러 날짜 탐색이 필요하면
    _fetch_all_rows 가 담당한다.
    NotFound(빈결과=INFO-200) / PermanentError(인증·인자·서비스)로 정규화.
    """
    cfg.require("lofin_key")
    url = f"{cfg.lofin_base.rstrip('/')}/{SERVICE}"
    if not exe_ymd:
        cands = exe_ymd_candidates(fyr)
        if not cands:
            raise NotFound(f"QWGJK exe_ymd 후보 없음 (fyr={fyr})")
        exe_ymd = cands[0]
    params: dict[str, Any] = {
        "Key": cfg.lofin_key,        # 인증키 파라미터명은 Key [공식 명세·실측]
        "Type": "json",              # 대문자 Type 만 JSON 전환됨[실측]
        "pIndex": pIndex,
        "pSize": min(int(pSize), MAX_PSIZE),  # 1000 초과 시 ERROR-336[실측]
        "fyr": fyr,
        "exe_ymd": exe_ymd,          # 필수[실측] — 누락이 ERROR-300 원인이었다
    }
    if laf_cd:
        params["laf_cd"] = laf_cd
    if wa_laf_cd:
        params["wa_laf_cd"] = wa_laf_cd
    if dbiz_nm:
        params["dbiz_nm"] = dbiz_nm
    payload = http.get_json(url, params)
    return _lofin_envelope(payload)


def search_budget_by_keyword(
    http,
    cfg,
    *,
    keyword: str,
    fyr: int | str,
    laf_cd: Optional[str] = None,
    exe_ymd: Optional[str] = None,
    pSize: int = 100,
    max_pages: int = 20,
) -> list[dict]:
    """dbiz_nm 유사검색(카테고리 키워드 조회 지원). 파싱된 budget_lines row[] 반환(비영속).

    graph/analysis 의 조례↔예산 매칭·카테고리 게이트가 후보를 좁힐 때 사용.
    """
    rows = _fetch_all_rows(http, cfg, fyr=fyr, laf_cd=laf_cd, exe_ymd=exe_ymd,
                           dbiz_nm=keyword, page_size=pSize, max_pages=max_pages)
    as_of = today_kst()
    return [parse_budget_row(r, laf_cd=laf_cd, fyr=fyr, as_of_date=as_of) for r in rows]


def _ordered_exe_days(
    fyr: int | str,
    *,
    exe_ymd: Optional[str] = None,
    hint: Optional[str] = None,
    lookback: int = EXE_YMD_LOOKBACK,
) -> list[str]:
    """탐색할 집행일자 목록. exe_ymd 지정 시 그것만, 아니면 후보(hint 우선 재정렬).

    전수 수집에서 직전 지자체가 성공한 적재일(hint)을 먼저 시도하면 미적재일
    탐색 호출(지자체당 1~2콜)이 사라진다. hint 가 후보에 없으면 맨 앞에 덧붙인다.
    """
    if exe_ymd:
        return [str(exe_ymd)]
    days = exe_ymd_candidates(fyr, lookback=lookback)
    if hint:
        h = str(hint)
        days = [h] + [d for d in days if d != h]
    return days


def _fetch_all_rows(
    http,
    cfg,
    *,
    fyr: int | str,
    laf_cd: Optional[str],
    exe_ymd: Optional[str] = None,
    exe_ymd_hint: Optional[str] = None,
    dbiz_nm: Optional[str] = None,
    page_size: int = 100,
    max_pages: int = 200,
) -> list[dict]:
    """페이지네이션으로 파티션 전체 원천 row 수집(NotFound=종료).

    exe_ymd(필수인자) 미지정 시 후보일자를 최신→과거로 훑어 첫 적재일을 채택한다.
    exe_ymd_hint 가 주어지면 그 일자를 최우선으로 시도한다(전수 수집 콜 절약).
    """
    page_size = min(int(page_size), MAX_PSIZE)
    days = _ordered_exe_days(fyr, exe_ymd=exe_ymd, hint=exe_ymd_hint)
    out: list[dict] = []
    for day in days:
        page = 1
        while page <= max_pages:
            try:
                total, chunk = budget_list(
                    http, cfg, fyr=fyr, exe_ymd=day, laf_cd=laf_cd, dbiz_nm=dbiz_nm,
                    pIndex=page, pSize=page_size,
                )
            except NotFound:
                break
            out.extend(chunk)
            if len(chunk) < page_size or (total and len(out) >= total):
                break
            page += 1
        if out:  # 적재된 집행일자를 찾았으면 그 날짜 스냅샷으로 확정
            break
    return out


# --------------------------------------------------------------------------- #
# 동결(확정연도) 판정
# --------------------------------------------------------------------------- #
def _is_frozen_year(fyr: int) -> bool:
    """회계연도가 (현재연도 - FROZEN_OFFSET) 이하이면 확정연도(동결 대상)."""
    try:
        current = int(today_kst()[:4])
    except (ValueError, IndexError):
        return False
    return int(fyr) <= current - FROZEN_OFFSET


def _days_since(iso_str: Optional[str]) -> Optional[float]:
    """ISO8601(Asia/Seoul) 문자열로부터 경과일. 파싱 실패 시 None."""
    if not iso_str:
        return None
    txt = str(iso_str)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(txt, fmt)
            break
        except ValueError:
            dt = None
    if dt is None:
        return None
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return (now - dt).total_seconds() / 86400.0


def _partition_signature(rows: list[dict]) -> str:
    """budget_id 정렬 후 휘발성 컬럼 제외 canonical JSON → content_hash."""
    canon = []
    for r in sorted(rows, key=lambda x: str(x.get("budget_id", ""))):
        canon.append({k: v for k, v in r.items() if k not in _VOLATILE_COLS})
    return content_hash(json.dumps(canon, ensure_ascii=False, sort_keys=True))


def _resolve_region_id(conn, laf_cd: str) -> Optional[str]:
    """laf_cd → regions.region_id 크로스워크(regions.lofin_laf_cd). 없으면 None.

    FK(budget_lines.region_id→regions) 위반 방지: 실존 region_id 일 때만 반환.
    """
    if not laf_cd:
        return None
    try:
        hit = db.fetchone(
            conn,
            "SELECT region_id FROM regions WHERE lofin_laf_cd=? LIMIT 1",
            (laf_cd,),
        )
    except Exception:  # regions 미초기화 등 — 조인 생략
        return None
    return hit["region_id"] if hit else None


# --------------------------------------------------------------------------- #
# 고수준 수집(계약 시그니처)
# --------------------------------------------------------------------------- #
def collect_budget(
    http,
    cfg,
    conn,
    *,
    laf_cd: str,
    fyr: int,
    exe_ymd: Optional[str] = None,
    exe_ymd_hint: Optional[str] = None,
    run_id: Optional[str] = None,
) -> dict:
    """지방재정365 QWGJK 파티션(laf_cd×fyr) 수집 → budget_lines upsert.

    동작:
      1) 확정연도(동결) & 90일 내 재확인 이력 → fetch 생략(status ok, note='frozen-skip').
      2) 파티션 전체 fetch → parse → budget_id 정렬 → 파티션 sha256.
      3) 워터마크 last_hash 와 동일하면 upsert 생략(무변경=무이벤트).
      4) 다르면 tx 로 upsert_many → 파티션 change_log 1건 → advance_cursor.

    반환: {'inserted','updated','unchanged','changed','errors','status'[,'note']}.
    source='budget', scope=f'laf:{laf_cd}|fyr:{fyr}', cursor='exe:{max exe_ymd}'.
    """
    cfg.require("lofin_key")
    result = {"inserted": 0, "updated": 0, "unchanged": 0,
              "changed": 0, "errors": 0, "rows_seen": 0, "status": "ok"}
    scope = f"laf:{laf_cd}|fyr:{fyr}"

    wm = db.get_watermark(conn, SOURCE, scope) or {}

    # 1) 확정연도 동결: 90일 내 성공 이력 있으면 재확인 생략.
    if _is_frozen_year(int(fyr)):
        since = _days_since(wm.get("last_success"))
        if since is not None and since < FROZEN_RECHECK_DAYS:
            log.info("동결 스킵 laf=%s fyr=%s (%.0f일 전 확인)", laf_cd, fyr, since)
            result["note"] = "frozen-skip"
            return result

    db.set_watermark(conn, SOURCE, scope, last_run=now_kst_iso(),
                     run_id=run_id, status="running")
    conn.commit()

    # 2) 파티션 전체 fetch + parse
    try:
        raw_rows = _fetch_all_rows(http, cfg, fyr=fyr, laf_cd=laf_cd,
                                   exe_ymd=exe_ymd, exe_ymd_hint=exe_ymd_hint,
                                   page_size=MAX_PSIZE)
    except PermanentError as exc:
        log.error("QWGJK 영구오류 laf=%s fyr=%s: %s", laf_cd, fyr, exc)
        db.mark_partition_status(conn, SOURCE, scope, "error",
                                 note=str(exc)[:200], bump_retry=True)
        conn.commit()
        result["errors"] = 1
        result["status"] = "error"
        return result

    if not raw_rows:
        # 정상적 없음 — 커서만 전진(빈 파티션 영속 확정).
        db.advance_cursor(conn, SOURCE, scope, cursor=f"fyr:{fyr}|rows:0",
                          last_hash=content_hash("[]"), status="ok",
                          changed=0, rows_seen=0, run_id=run_id, note="empty")
        conn.commit()
        return result

    as_of = today_kst()
    rows = [parse_budget_row(r, laf_cd=laf_cd, fyr=fyr, as_of_date=as_of)
            for r in raw_rows]
    result["unchanged"] = len(rows)  # 기본값(무변경 파티션 가정), 변경 시 아래서 재설정
    result["rows_seen"] = len(rows)
    exe_vals = [str(r.get("exe_ymd")) for r in rows if r.get("exe_ymd")]
    max_exe = max(exe_vals) if exe_vals else None
    if max_exe:
        # 전수 수집이 다음 지자체 탐색에 재사용할 적재일 힌트.
        result["exe_ymd"] = max_exe

    # region_id 크로스워크(파티션당 1회 조회).
    region_id = _resolve_region_id(conn, laf_cd)
    if region_id:
        for r in rows:
            r["region_id"] = region_id

    # 3) 파티션 서명 비교
    sig = _partition_signature(rows)
    if wm.get("last_hash") == sig:
        # 무변경 파티션: upsert·log_change 생략, last_success 만 갱신.
        db.advance_cursor(conn, SOURCE, scope, cursor=str(wm.get("cursor") or ""),
                          last_hash=sig, status="ok", changed=0,
                          rows_seen=len(rows), run_id=run_id, note="unchanged")
        conn.commit()
        log.info("무변경 laf=%s fyr=%s rows=%d", laf_cd, fyr, len(rows))
        return result

    # 4) 변경 파티션: upsert + 파티션 change_log 1건 + 커서 전진
    cursor = f"exe:{max_exe}" if max_exe else f"fyr:{fyr}|rows:{len(rows)}"

    with db.tx(conn):
        counts = db.upsert_many(conn, "budget_lines", rows, pk="budget_id")
        result["inserted"] = counts["inserted"]
        result["updated"] = counts["updated"]
        result["unchanged"] = counts["unchanged"]
        result["changed"] = counts["inserted"] + counts["updated"]
        # 파티션 단위 변경 이벤트(무변경=무이벤트 규율: 서명이 달라진 경우만 도달).
        db.log_change(
            conn,
            entity_type="budget",
            entity_id=scope,
            entity_name=f"{laf_cd} {fyr} 세출({len(rows)}건)",
            event="budget_changed",
            source=SOURCE,
            scope=scope,
            before=str(wm.get("last_hash") or ""),
            after=sig,
            fields_changed=json.dumps(
                {"inserted": counts["inserted"], "updated": counts["updated"]},
                ensure_ascii=False),
            region_code=(region_id or laf_cd),
            run_id=run_id,
        )
        db.advance_cursor(conn, SOURCE, scope, cursor=cursor, last_hash=sig,
                          status="ok", changed=result["changed"],
                          rows_seen=len(rows), run_id=run_id)

    log.info("수집 laf=%s fyr=%s inserted=%d updated=%d cursor=%s",
             laf_cd, fyr, result["inserted"], result["updated"], cursor)
    return result


# --------------------------------------------------------------------------- #
# 전국 전수 수집(243개 지자체 × 회계연도)
# --------------------------------------------------------------------------- #
class CallBudgetExceeded(Exception):
    """전수 수집 호출수 상한 초과 — 안전 중단(다음 실행에서 재개) 신호."""


class _ThrottledCounter:
    """http 래퍼: get_json 호출수를 세고 QPS·총콜 상한을 강제한다.

    HttpClient 를 감싸는 얇은 프록시. 나머지 속성은 그대로 위임하므로
    budget_list/_fetch_all_rows 는 원본 클라이언트와 구분 없이 동작한다.
    상한 초과 시 CallBudgetExceeded 를 던져 호출부가 partial 로 중단하게 한다
    (TransientError 가 아니므로 util.retry 가 재시도하지 않는다).
    """

    def __init__(self, http, *, qps: float = NATIONWIDE_QPS,
                 max_calls: Optional[int] = NATIONWIDE_MAX_CALLS):
        self._http = http
        self._limiter = RateLimiter(qps) if qps and qps > 0 else None
        self.max_calls = int(max_calls) if max_calls else None
        self.calls = 0

    def get_json(self, url: str, params: Optional[dict] = None) -> Any:
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise CallBudgetExceeded(f"호출수 상한 {self.max_calls} 도달")
        if self._limiter is not None:
            self._limiter.wait()
        self.calls += 1
        return self._http.get_json(url, params)

    def __getattr__(self, name: str) -> Any:  # get_text/close 등 위임
        return getattr(self._http, name)


def nationwide_laf_cds(conn, *, active_only: bool = False) -> list[str]:
    """regions.lofin_laf_cd 전체(243개) 목록. 코드 오름차순, 중복 제거.

    LOFIN ACEXBG 가 부여한 자치단체코드 집합이 곧 재정 공시 대상 전수이므로
    기본값은 regions.status 로 거르지 않는다(승계·통합 플래그와 무관하게 조회 가능).
    active_only=True 면 활성 level 1·2 만(run.py 파티션 열거와 동일 기준).
    """
    sql = ("SELECT DISTINCT lofin_laf_cd FROM regions "
           "WHERE lofin_laf_cd IS NOT NULL AND lofin_laf_cd <> ''")
    if active_only:
        sql += " AND level IN (1,2) AND (status IS NULL OR status='active')"
    sql += " ORDER BY lofin_laf_cd"
    try:
        rows = db.fetchall(conn, sql)
    except Exception as exc:  # regions 미초기화
        log.warning("laf_cd 목록 조회 실패: %s", exc)
        return []
    return [str(r["lofin_laf_cd"]) for r in rows if r.get("lofin_laf_cd")]


def _laf_done_recently(conn, laf_cd: str, fyr: int, *, hours: float) -> bool:
    """resume 판정: 해당 파티션이 hours 내에 status='ok' 로 끝났는가."""
    wm = db.get_watermark(conn, SOURCE, f"laf:{laf_cd}|fyr:{fyr}") or {}
    if str(wm.get("status") or "") != "ok":
        return False
    since = _days_since(wm.get("last_success"))
    return since is not None and since * 24.0 < hours


def collect_budget_nationwide(
    http,
    cfg,
    conn,
    *,
    fyr: int,
    laf_cds: Optional[Iterable[str]] = None,
    resume: bool = True,
    exe_ymd: Optional[str] = None,
    run_id: Optional[str] = None,
    qps: float = NATIONWIDE_QPS,
    max_calls: Optional[int] = NATIONWIDE_MAX_CALLS,
    resume_hours: float = RESUME_FRESH_HOURS,
    progress_every: int = PROGRESS_EVERY,
    active_only: bool = False,
) -> dict:
    """전국 지자체(regions.lofin_laf_cd) × 회계연도 세출예산 전수 수집.

    동작:
      1) 대상 laf_cd 열거(laf_cds 미지정 시 regions 에서 243개).
      2) resume=True 면 최근 hours 내 status='ok' 인 파티션은 건너뛴다
         (지자체별 watermark = 재개 지점).
      3) 지자체마다 collect_budget 호출. 실패는 격리(errors 집계 후 계속).
      4) 직전 성공 적재일(exe_ymd)을 힌트로 물려 미적재일 탐색 호출을 줄인다.
      5) progress_every 마다 진행 로그 + nationwide 커서 저장(중단 내성).
      6) 총 호출수 상한 초과 시 status='partial' 로 안전 중단 → 재실행하면 이어짐.

    반환: {'fyr','regions_total','regions_done','regions_skipped','regions_empty',
           'regions_error','inserted','updated','unchanged','changed','errors',
           'rows_seen','calls','elapsed_sec','exe_ymd','status','failures'}
    """
    cfg.require("lofin_key")
    targets = [str(c) for c in laf_cds] if laf_cds else \
        nationwide_laf_cds(conn, active_only=active_only)
    total = len(targets)
    scope_all = f"nationwide|fyr:{fyr}"
    started = datetime.now()

    result: dict[str, Any] = {
        "fyr": int(fyr), "regions_total": total, "regions_done": 0,
        "regions_skipped": 0, "regions_empty": 0, "regions_error": 0,
        "inserted": 0, "updated": 0, "unchanged": 0, "changed": 0,
        "errors": 0, "rows_seen": 0, "calls": 0, "elapsed_sec": 0.0,
        "exe_ymd": None, "status": "ok", "failures": [],
    }
    if not total:
        log.warning("전수 수집 대상 laf_cd 없음 (fyr=%s)", fyr)
        result["status"] = "empty"
        return result

    proxy = _ThrottledCounter(http, qps=qps, max_calls=max_calls)
    hint = exe_ymd
    db.set_watermark(conn, SOURCE, scope_all, last_run=now_kst_iso(),
                     run_id=run_id, status="running",
                     cursor=f"0/{total}")
    conn.commit()
    log.info("전수 수집 시작 fyr=%s 대상=%d resume=%s qps=%.1f max_calls=%s",
             fyr, total, resume, qps, max_calls)

    for idx, laf in enumerate(targets, start=1):
        if resume and _laf_done_recently(conn, laf, int(fyr), hours=resume_hours):
            result["regions_skipped"] += 1
            continue
        try:
            r = collect_budget(proxy, cfg, conn, laf_cd=laf, fyr=int(fyr),
                               exe_ymd=exe_ymd, exe_ymd_hint=hint,
                               run_id=run_id) or {}
        except CallBudgetExceeded as exc:
            log.warning("호출수 상한 도달 — 중단(재개 가능): %s (idx=%d/%d)",
                        exc, idx, total)
            result["status"] = "partial"
            result["note"] = f"call-limit at {idx}/{total}"
            break
        except Exception as exc:  # 파티션 격리 — 한 지자체 실패가 전체를 멈추지 않는다
            log.error("laf=%s fyr=%s 실패: %s", laf, fyr, exc)
            result["regions_error"] += 1
            result["errors"] += 1
            result["failures"].append({"laf_cd": laf, "error": str(exc)[:200]})
            try:
                db.mark_partition_status(conn, SOURCE, f"laf:{laf}|fyr:{fyr}",
                                         "error", note=str(exc)[:200],
                                         bump_retry=True)
                conn.commit()
            except Exception:  # 격리 실패도 전체를 멈추지 않는다
                pass
            continue

        for k in ("inserted", "updated", "unchanged", "changed", "errors", "rows_seen"):
            result[k] += int(r.get(k, 0) or 0)
        if r.get("exe_ymd"):
            hint = str(r["exe_ymd"])
            result["exe_ymd"] = hint
        if str(r.get("status") or "ok") != "ok":
            result["regions_error"] += 1
            result["failures"].append({"laf_cd": laf,
                                       "error": str(r.get("note") or r.get("status"))})
        else:
            result["regions_done"] += 1
            if not int(r.get("rows_seen", 0) or 0):
                result["regions_empty"] += 1

        if progress_every and idx % progress_every == 0:
            elapsed = (datetime.now() - started).total_seconds()
            log.info("진행 %d/%d fyr=%s rows=%d calls=%d err=%d %.0fs",
                     idx, total, fyr, result["rows_seen"], proxy.calls,
                     result["regions_error"], elapsed)
            try:
                db.set_watermark(conn, SOURCE, scope_all, status="running",
                                 cursor=f"{idx}/{total}|laf:{laf}",
                                 rows_seen=result["rows_seen"],
                                 changed=result["changed"], run_id=run_id)
                conn.commit()
            except Exception:
                pass

    result["calls"] = proxy.calls
    result["elapsed_sec"] = round((datetime.now() - started).total_seconds(), 1)
    if result["regions_error"]:
        result["status"] = "partial" if result["status"] == "ok" else result["status"]

    db.advance_cursor(
        conn, SOURCE, scope_all,
        cursor=f"{result['regions_done'] + result['regions_skipped']}/{total}"
               + (f"|exe:{result['exe_ymd']}" if result["exe_ymd"] else ""),
        status=result["status"], changed=result["changed"],
        rows_seen=result["rows_seen"], run_id=run_id,
        note=f"done={result['regions_done']} skip={result['regions_skipped']} "
             f"err={result['regions_error']} calls={result['calls']}",
    )
    conn.commit()
    log.info("전수 수집 종료 fyr=%s done=%d skip=%d err=%d rows=%d calls=%d %.0fs",
             fyr, result["regions_done"], result["regions_skipped"],
             result["regions_error"], result["rows_seen"], result["calls"],
             result["elapsed_sec"])
    return result


# --------------------------------------------------------------------------- #
# 주요업무계획 HWP 파서 (스텁 — 선택적 의존성)
# --------------------------------------------------------------------------- #
try:  # olefile: HWP5(OLE 복합문서) 저수준 접근
    import olefile as _olefile  # type: ignore
    _HAS_OLEFILE = True
except Exception:  # pragma: no cover - 폴백 경로
    _olefile = None  # type: ignore
    _HAS_OLEFILE = False

try:  # pyhwp: HWP 텍스트 추출(선택)
    import hwp5  # type: ignore  # noqa: F401
    _HAS_PYHWP = True
except Exception:  # pragma: no cover
    _HAS_PYHWP = False

HWP_AVAILABLE = _HAS_OLEFILE or _HAS_PYHWP


def parse_business_plan_hwp(path: str, *, laf_cd: Optional[str] = None,
                            fyr: Optional[int] = None) -> list[dict]:
    """주요업무계획 HWP → 예산 보조 근거 레코드[](스텁).

    olefile/pyhwp 가 없으면 skip(빈 리스트 + 경고 로그). 예산 API(QWGJK)가 정본이며
    이 파서는 보조 근거용 인터페이스만 제공한다. 실제 셀·표 추출 구현은 실키/샘플
    확보 후 별도 라운드에서 채운다(확인 필요).

    반환(구현 시): [{'laf_cd','fyr','dbiz_nm','source':'business-plan','raw':...}, ...]
    """
    if not HWP_AVAILABLE:
        log.warning("HWP 파서 의존성(olefile/pyhwp) 없음 → skip: %s", path)
        return []
    # 인터페이스 스텁: OLE 컨테이너 유효성만 확인하고 추출은 미구현.
    try:
        if _HAS_OLEFILE and not _olefile.isOleFile(path):  # type: ignore
            raise NotFound(f"HWP(OLE) 아님: {path}")
    except NotFound:
        raise
    except Exception as exc:  # pragma: no cover - 방어
        log.warning("HWP 열기 실패 %s: %s", path, exc)
        return []
    log.info("HWP 파서 스텁: 표 추출 미구현(보조 근거) — %s", path)
    return []


__all__ = [
    "SERVICE", "SOURCE",
    "MAX_PSIZE", "EXE_YMD_LOOKBACK", "exe_ymd_candidates",
    "budget_list", "search_budget_by_keyword", "collect_budget",
    "collect_budget_nationwide", "nationwide_laf_cds", "CallBudgetExceeded",
    "NATIONWIDE_QPS", "NATIONWIDE_MAX_CALLS", "RESUME_FRESH_HOURS",
    "parse_budget_row", "parse_business_plan_hwp", "HWP_AVAILABLE",
]
