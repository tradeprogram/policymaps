"""policymap.collectors.assembly — 열린국회정보 Open API 수집기.

명세[assembly] + CONTRACTS.md §1.2 계약을 그대로 구현한다.

수집 대상 서비스(slug):
    발의법률안        nzmimeepazxkubdpn  (AGE 필수) → bills / bill_proposers / legislators(최소행)
    의안별 표결(집계)  ncocpgfiaoituanbr  (AGE 필수) → bills(member/vote/yes/no/blank_tcnt, proc_result_cd)
    본회의 표결(의원)  nojepdqqaweusdfbi  (AGE+BILL_ID 필수) → votes(party_at_vote=POLY_NM)
    의원 인적사항(현직) nwvrqwxyaytdsfvhu  (필수 없음) → legislators / parties

조인키(§6 빠른참조): BILL_ID=bills.bill_id, MONA_CD=legislators.legislator_id,
POLY_NM=votes.party_at_vote, AGE=파티션.

공통 규율:
  * 코어는 표준라이브러리만으로 동작(무거운 의존성 없음).
  * ASSEMBLY_KEY 없으면 collect_* 진입부에서 cfg.require() 로 즉시 RuntimeError.
  * envelope 해석·행 매핑·비율계산은 순수함수(_parse_envelope/_map_*/bill_vote_ratio)로
    분리 → 실키 없이 픽스처로 단위테스트 가능.
  * INFO-200(없음)→NotFound(정상적 없음, 재시도·오류예산 제외).
    ERROR-290(키)→PermanentError. ERROR-336(>1000)→TransientError(pSize 축소 신호).
  * 파티션 완전 영속화 후에만 db.advance_cursor 호출(멱등성).
  * 무변경(unchanged)일 때 log_change 호출 금지(무변경=무이벤트).

FK 주의(PRAGMA foreign_keys=ON):
  bill_proposers/votes 는 bills·legislators 를 참조하므로, 상위 행을 먼저 보장한다
  (_ensure_bill / _ensure_legislator = "없으면 최소행 삽입, 있으면 보존").
  legislators.current_party → parties FK 이므로 정당행을 먼저 upsert 한 뒤 현재정당을 세팅.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from .. import db, util
from ..util import NotFound, PermanentError, TransientError

log = util.get_logger("policymap.collectors.assembly")

# --------------------------------------------------------------------------- #
# 서비스 slug (명세[assembly] §2, 카탈로그 infId 로 확정)
# --------------------------------------------------------------------------- #
SVC_BILLS = "nzmimeepazxkubdpn"          # 국회의원 발의법률안
SVC_VOTE_TALLIES = "ncocpgfiaoituanbr"   # 의안별 표결현황(집계)
SVC_MEMBER_VOTES = "nojepdqqaweusdfbi"   # 국회의원 본회의 표결정보(의원 단위)
SVC_LEGISLATORS = "nwvrqwxyaytdsfvhu"    # 국회의원 인적사항(현직 명부)

_PAGE_SIZE = 1000                         # pSize 상한(초과 시 ERROR-336)

# 개인 코드/이름 목록 구분자(공동발의자코드 형식 불명 → 관용 구분자 방어)
_SPLIT_RE = re.compile(r"[,，;/\|\s·・]+")
_NUM_RE = re.compile(r"^\d+$")


# =========================================================================== #
# 순수 헬퍼 (네트워크·DB 무관 — 픽스처 테스트 가능)
# =========================================================================== #
def _int(value: Any) -> Optional[int]:
    """API 문자열 수치 → int. 빈값/None/파싱실패 → None. 콤마 방어."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s == "" or s == "-":
        return None
    try:
        return int(float(s)) if ("." in s) else int(s)
    except (ValueError, TypeError):
        return None


def _clean(value: Any) -> Optional[str]:
    """공백 트림. 빈문자/None → None."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _bill_no_key(bill_no: Any) -> int:
    """BILL_NO 하이워터 비교용 정수 키. 숫자 아니면 -1."""
    s = "" if bill_no is None else str(bill_no).strip()
    return int(s) if _NUM_RE.match(s) else -1


def _split_codes(value: Any) -> list[str]:
    """PUBL_MONA_CD 등 구분자 목록(형식 불명 — 관용 구분자 분할) → 중복제거 코드 리스트.

    (확인 필요) 공동발의자코드가 단일값인지 구분자목록인지 실키 미확정 →
    구분자 분할로 방어하고, 전체 공동발의자 정밀명단은 MEMBER_LIST 링크 파싱으로 보강(미구현).
    """
    if not value:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in _SPLIT_RE.split(str(value)):
        t = tok.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _committees_json(cmits: Any) -> Optional[str]:
    """CMITS(소속 위원회 목록 문자열) → JSON 배열 문자열(schema: committees=JSON)."""
    if not cmits:
        return None
    parts = [p.strip() for p in _SPLIT_RE.split(str(cmits)) if p.strip()]
    return json.dumps(parts, ensure_ascii=False) if parts else None


def _is_passed(*texts: Any) -> Optional[int]:
    """본회의 처리결과 텍스트/코드로 가결 여부 추정. 1=가결, 0=부결·폐기류, None=불명.

    (확인 필요) PROC_RESULT_CD 코드값 사전 미확보 → 한글 결과문 휴리스틱. 숫자코드는 불명 처리.
    """
    blob = " ".join(str(t) for t in texts if t)
    if not blob.strip():
        return None
    if "가결" in blob:                       # 원안가결/수정가결
        return 1
    if any(k in blob for k in ("부결", "폐기", "철회", "부결", "대안반영")):
        return 0
    return None


def bill_vote_ratio(bill: dict) -> Optional[dict]:
    """의안 1건의 찬반비율 파생(순수). bills 행(dict) 입력. 표결집계 없으면 None.

    "법안별 찬반비율"(태스크) 파생. 저장 컬럼이 없어 뷰/질의 대용의 순수함수로 제공한다.
    반환: yes_ratio/no_ratio/blank_ratio(=투표수 대비), turnout(=투표/재적), absent(=재적-투표), passed.
    """
    vote = _int(bill.get("vote_tcnt"))
    if not vote:
        return None
    yes = _int(bill.get("yes_tcnt")) or 0
    no = _int(bill.get("no_tcnt")) or 0
    blank = _int(bill.get("blank_tcnt")) or 0
    member = _int(bill.get("member_tcnt"))
    return {
        "bill_id": bill.get("bill_id"),
        "member_tcnt": member,
        "vote_tcnt": vote,
        "yes_tcnt": yes,
        "no_tcnt": no,
        "blank_tcnt": blank,
        "yes_ratio": round(yes / vote, 4),
        "no_ratio": round(no / vote, 4),
        "blank_ratio": round(blank / vote, 4),
        "turnout": round(vote / member, 4) if member else None,
        "absent": (member - vote) if member is not None else None,
        "passed": _is_passed(bill.get("proc_result_cd"), bill.get("proc_result")),
    }


# --- API row → 테이블 row 매퍼(순수) --------------------------------------- #
def _map_bill_row(r: dict, age: int) -> Optional[dict]:
    """발의법률안 API row → bills row(발의 서비스 소유 컬럼만). 필수키 없으면 None.

    content_hash 는 상태변동(PROC_RESULT/PROC_DT 등) 감지용으로 이 서비스가 소유한다.
    표결집계 컬럼(proc_result_cd/*_tcnt/link_url)은 여기서 건드리지 않는다(부분 upsert 보존).
    """
    bill_id = _clean(r.get("BILL_ID"))
    name = _clean(r.get("BILL_NAME"))
    if not bill_id or not name:
        return None
    state_token = "\x1f".join([
        str(r.get("PROC_RESULT") or ""),
        str(r.get("PROC_DT") or ""),
        str(r.get("PROC_RESULT_CD") or ""),
        name,
        str(r.get("COMMITTEE") or ""),
        str(r.get("PROPOSE_DT") or ""),
    ])
    return {
        "bill_id": bill_id,
        "bill_no": _clean(r.get("BILL_NO")),
        "age": int(age),
        "name": name,
        "committee": _clean(r.get("COMMITTEE")),
        "committee_id": _clean(r.get("COMMITTEE_ID")),
        "propose_dt": _clean(r.get("PROPOSE_DT")),
        "proc_dt": _clean(r.get("PROC_DT")),
        "proc_result": _clean(r.get("PROC_RESULT")),
        "detail_link": _clean(r.get("DETAIL_LINK")),
        "content_hash": util.content_hash(state_token),
        "updated_at": util.now_kst_iso(),
    }


def _map_tally_row(r: dict, age: int) -> Optional[dict]:
    """의안별 표결(집계) API row → bills row(표결집계 컬럼만). content_hash 는 소유하지 않음.

    committee 는 발의 서비스(COMMITTEE)와 시점이 달라(CURR_COMMITTEE) 덮어쓰지 않는다.
    """
    bill_id = _clean(r.get("BILL_ID"))
    name = _clean(r.get("BILL_NAME"))
    if not bill_id or not name:
        return None
    return {
        "bill_id": bill_id,
        "bill_no": _clean(r.get("BILL_NO")),
        "age": int(age),
        "name": name,
        "proc_dt": _clean(r.get("PROC_DT")),
        "proc_result_cd": _clean(r.get("PROC_RESULT_CD")),
        "bill_kind_cd": _clean(r.get("BILL_KIND_CD")),
        "member_tcnt": _int(r.get("MEMBER_TCNT")),
        "vote_tcnt": _int(r.get("VOTE_TCNT")),
        "yes_tcnt": _int(r.get("YES_TCNT")),
        "no_tcnt": _int(r.get("NO_TCNT")),
        "blank_tcnt": _int(r.get("BLANK_TCNT")),
        "link_url": _clean(r.get("LINK_URL")),
        "updated_at": util.now_kst_iso(),
    }


_TALLY_COMPARE = ("member_tcnt", "vote_tcnt", "yes_tcnt", "no_tcnt",
                  "blank_tcnt", "proc_result_cd", "bill_kind_cd")


def _map_vote_row(r: dict) -> Optional[dict]:
    """의원별 표결 API row → votes row. BILL_ID·MONA_CD 없으면 None."""
    bill_id = _clean(r.get("BILL_ID"))
    mona = _clean(r.get("MONA_CD"))
    if not bill_id or not mona:
        return None
    return {
        "bill_id": bill_id,
        "legislator_id": mona,
        "result_vote_mod": _clean(r.get("RESULT_VOTE_MOD")),
        "vote_date": _clean(r.get("VOTE_DATE")),
        "party_at_vote": _clean(r.get("POLY_NM")),   # 표결 당시 정당(역사적 정확)
        "party_cd_at_vote": _clean(r.get("POLY_CD")),
        "session_cd": _clean(r.get("SESSION_CD")),
        "currents_cd": _clean(r.get("CURRENTS_CD")),
    }


def _map_legislator_row(r: dict) -> Optional[tuple[Optional[dict], dict]]:
    """의원 인적사항 API row → (party_row|None, legislator_row). MONA_CD 없으면 None.

    party_row 는 (poly_cd 없으면 정규화 정당명 기준) party_id 로 upsert 대상.
    legislator.current_party 는 호출부에서 party upsert 후 party_id 로 세팅한다.
    """
    mona = _clean(r.get("MONA_CD"))
    name = _clean(r.get("HG_NM"))
    if not mona:
        return None
    poly_nm = _clean(r.get("POLY_NM"))
    party_row = _party_row(poly_nm, r.get("POLY_CD")) if poly_nm else None
    leg = {
        "legislator_id": mona,
        "mona_cd": mona,
        "name": name or mona,
        "name_hanja": _clean(r.get("HJ_NM")),
        "name_eng": _clean(r.get("ENG_NM")),
        "current_party": party_row["party_id"] if party_row else None,
        "district": _clean(r.get("ORIG_NM")),
        "elect_type": _clean(r.get("ELECT_GBN_NM")),
        "reelection": _clean(r.get("REELE_GBN_NM")),
        "units": _clean(r.get("UNITS")),
        "sex": _clean(r.get("SEX_GBN_NM")),
        "committees": _committees_json(r.get("CMITS")),
        "email": _clean(r.get("E_MAIL")),
        "homepage": _clean(r.get("HOMEPAGE")),
        "updated_at": util.now_kst_iso(),
    }
    return party_row, leg


def _party_row(poly_nm: Optional[str], poly_cd: Any) -> Optional[dict]:
    """정당명(+선택 POLY_CD) → parties row. party_id = POLY_CD 있으면 그것, 없으면 정규화명."""
    name = _clean(poly_nm)
    if not name:
        return None
    cd = _clean(poly_cd)
    party_id = cd or util.compact(name)      # 정규화 정당명(공백·낫표 제거) 키
    return {"party_id": party_id, "name": name, "poly_cd": cd}


# --------------------------------------------------------------------------- #
# envelope 파싱(순수)
# --------------------------------------------------------------------------- #
def _extract(data: Any, service: str) -> tuple[Optional[str], Optional[str], int, list[dict]]:
    """공통 envelope → (result_code, message, list_total_count, row[]).

    구조(명세[assembly] §1): { slug: [ {head:[{list_total_count},{RESULT:{CODE,MESSAGE}}]},
                                       {row:[{...}]} ] }.
    최상위 {RESULT:{...}} 형태(에러/빈결과 envelope)도 방어.
    """
    if isinstance(data, dict) and isinstance(data.get("RESULT"), dict):
        res = data["RESULT"]
        return _clean(res.get("CODE")), _clean(res.get("MESSAGE")), 0, []

    payload = None
    if isinstance(data, dict):
        if isinstance(data.get(service), list):
            payload = data[service]
        else:
            for v in data.values():
                if isinstance(v, list):
                    payload = v
                    break
    if payload is None:
        return None, None, 0, []

    code = message = None
    total = 0
    rows: list[dict] = []
    for elem in payload:
        if not isinstance(elem, dict):
            continue
        head = elem.get("head")
        if isinstance(head, list):
            for h in head:
                if not isinstance(h, dict):
                    continue
                if "list_total_count" in h:
                    total = _int(h.get("list_total_count")) or 0
                if isinstance(h.get("RESULT"), dict):
                    code = _clean(h["RESULT"].get("CODE"))
                    message = _clean(h["RESULT"].get("MESSAGE"))
        row = elem.get("row")
        if isinstance(row, list):
            rows = [x for x in row if isinstance(x, dict)]
    return code, message, total, rows


def _raise_for_code(code: Optional[str], message: Optional[str]) -> None:
    """결과코드 해석(명세[assembly] §1). 정상이면 None, 그 외 예외."""
    if not code:
        return                                   # 코드 없음 → row 존재로 판정(정상)
    c = code.upper()
    if c in ("INFO-000",):
        return
    if c == "INFO-200":                          # 해당 데이터 없음(정상적 없음)
        raise NotFound(message or "해당 데이터 없음(INFO-200)")
    if c == "ERROR-290":                         # 인증키 오류/미등록
        raise PermanentError(f"ASSEMBLY_KEY 오류(ERROR-290): {message or ''}")
    if c in ("ERROR-300", "ERROR-310", "ERROR-333"):  # 필수인자 누락/타입
        raise PermanentError(f"요청인자 오류({code}): {message or ''}")
    if c == "ERROR-336":                         # 1회 최대 1000건 초과 → pSize 축소 신호
        raise TransientError(f"페이지 상한 초과({code}): {message or ''}")
    if c.startswith("ERROR"):
        raise PermanentError(f"국회 API 오류({code}): {message or ''}")
    return                                        # 기타 INFO-* 는 정상 취급


def _parse_envelope(text: str, service: str) -> tuple[int, list[dict]]:
    """응답 텍스트 → (list_total_count, row[]). 인증/파라미터 이상은 PermanentError.

    잘못된 KEY 실호출은 본문 'Bad Request.'(비-JSON) → PermanentError 로 구분한다.
    (get_text 경로가 이미 5xx/429/rate 토큰을 TransientError 로 정규화함.)
    """
    s = (text or "").strip()
    if not s:
        raise NotFound("빈 응답 본문")
    if not s.lstrip().startswith(("{", "[")):
        # "Bad Request." 등 → 인증/파라미터 이상
        raise PermanentError(f"비정상 응답(인증/파라미터 의심): {s[:120]!r}")
    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        raise PermanentError(f"JSON 파싱 실패: {e} / head={s[:120]!r}") from e
    code, message, total, rows = _extract(data, service)
    _raise_for_code(code, message)
    return total, rows


# =========================================================================== #
# 저수준 호출 (CONTRACTS.md §1.2)
# =========================================================================== #
def _call(http, cfg, service: str, *, pIndex: int = 1, pSize: int = _PAGE_SIZE,
          Type: str = "json", **params) -> tuple[int, list[dict]]:
    """공통 envelope 해석 GET. 반환 (list_total_count, row[]).

    INFO-200→NotFound / ERROR-290→PermanentError / ERROR-336→TransientError(축소신호).
    """
    cfg.require("assembly_key")
    ps = min(int(pSize), _PAGE_SIZE)
    url = f"{cfg.assembly_base}/{service}"
    q: dict[str, Any] = {"KEY": cfg.assembly_key, "Type": Type,
                         "pIndex": int(pIndex), "pSize": ps}
    for k, v in params.items():
        if v is not None and v != "":
            q[k] = v
    text = http.get_text(url, q)
    return _parse_envelope(text, service)


def _iter_rows(http, cfg, service: str, *, pSize: int = _PAGE_SIZE,
               **params) -> Iterable[dict]:
    """전 페이지 순회 제너레이터. list_total_count 도달/빈페이지에서 종료.

    첫 페이지 NotFound(INFO-200)=빈결과 → 즉시 종료(예외 전파 안 함).
    """
    ps = min(int(pSize), _PAGE_SIZE)
    pIndex = 1
    total: Optional[int] = None
    while True:
        try:
            t, rows = _call(http, cfg, service, pIndex=pIndex, pSize=ps, **params)
        except NotFound:
            return
        if total is None:
            total = t
        for row in rows:
            yield row
        if not rows or len(rows) < ps:
            return
        if total and pIndex * ps >= total:
            return
        pIndex += 1


# =========================================================================== #
# DB 상위 행 보장 헬퍼 (FK 충족; 기존 풍부한 행은 보존)
# =========================================================================== #
def _ensure_legislator(conn, mona_cd: Optional[str], name: Optional[str] = None) -> None:
    """legislators 최소행 보장. 이미 있으면 건드리지 않음(명부 enrichment 보존)."""
    mona = _clean(mona_cd)
    if not mona:
        return
    if db.fetchone(conn, "SELECT 1 FROM legislators WHERE legislator_id=?", (mona,)):
        return
    db.upsert(conn, "legislators",
              {"legislator_id": mona, "mona_cd": mona,
               "name": _clean(name) or mona, "updated_at": util.now_kst_iso()},
              "legislator_id")


def _ensure_bill(conn, bill_id: Optional[str], age: int,
                 name: Optional[str] = None) -> bool:
    """bills 최소행 보장. 이미 있으면 보존. 반환 True=행 존재 확정."""
    bid = _clean(bill_id)
    if not bid:
        return False
    if db.fetchone(conn, "SELECT 1 FROM bills WHERE bill_id=?", (bid,)):
        return True
    db.upsert(conn, "bills",
              {"bill_id": bid, "age": int(age), "name": _clean(name) or bid,
               "updated_at": util.now_kst_iso()},
              "bill_id")
    return True


def _ensure_party(conn, party_row: Optional[dict]) -> Optional[str]:
    """parties upsert(있으면 보강). party_id 반환."""
    if not party_row:
        return None
    db.upsert(conn, "parties", party_row, "party_id")
    return party_row["party_id"]


def _result(inserted=0, updated=0, unchanged=0, errors=0, status="ok") -> dict:
    return {
        "inserted": inserted, "updated": updated, "unchanged": unchanged,
        "changed": inserted + updated, "errors": errors, "status": status,
    }


# =========================================================================== #
# 고수준 수집 (CONTRACTS.md §1.2)
# =========================================================================== #
def collect_bills(http, cfg, conn, *, age: int, run_id: Optional[str] = None) -> dict:
    """발의법률안 수집 → bills + bill_proposers(+legislators 최소행).

    커서: BILL_NO 하이워터. source='na_bill', scope='age:{age}',
    cursor='billno:{max}|prop:{max제안일}'. content_hash 가드로 상태변동만 log_change.
    """
    cfg.require("assembly_key")
    source, scope = "na_bill", f"age:{age}"
    ins = upd = unc = err = 0
    max_bn = -1
    max_prop = ""
    rows_seen = 0
    try:
        db.set_watermark(conn, source, scope, last_run=util.now_kst_iso(),
                         status="running", run_id=run_id)
        for r in _iter_rows(http, cfg, SVC_BILLS, AGE=age):
            rows_seen += 1
            row = _map_bill_row(r, age)
            if row is None:
                err += 1
                continue
            try:
                with db.tx(conn):
                    action = db.upsert(conn, "bills", row, "bill_id",
                                       hash_col="content_hash")
                    if action == "inserted":
                        ins += 1
                        db.log_change(conn, entity_type="bill", entity_id=row["bill_id"],
                                      entity_name=row["name"], event="created",
                                      source=source, scope=scope, after=row.get("bill_no"),
                                      run_id=run_id, official_url=row.get("detail_link"))
                    elif action == "updated":
                        upd += 1
                        db.log_change(conn, entity_type="bill", entity_id=row["bill_id"],
                                      entity_name=row["name"], event="amended",
                                      source=source, scope=scope, after=row.get("proc_result"),
                                      run_id=run_id, official_url=row.get("detail_link"))
                    else:
                        unc += 1
                    _persist_proposers(conn, r, row["bill_id"])
            except (TransientError, PermanentError) as exc:
                err += 1
                log.warning("bills upsert 실패 bill_id=%s: %s", row.get("bill_id"), exc)
                continue
            bn = _bill_no_key(row.get("bill_no"))
            if bn > max_bn:
                max_bn = bn
            pd = row.get("propose_dt") or ""
            if pd > max_prop:
                max_prop = pd
    except PermanentError:
        db.mark_partition_status(conn, source, scope, "error", bump_retry=True)
        conn.commit()
        raise
    except TransientError as exc:
        db.mark_partition_status(conn, source, scope, "partial",
                                 note=str(exc)[:200], bump_retry=True)
        conn.commit()
        return _result(ins, upd, unc, err + 1, status="partial")

    cursor = f"billno:{max_bn}|prop:{max_prop}"
    with db.tx(conn):
        db.advance_cursor(conn, source, scope, cursor,
                          last_hash=util.content_hash(f"{rows_seen}|{max_bn}"),
                          status="ok", changed=ins + upd, rows_seen=rows_seen, run_id=run_id)
    return _result(ins, upd, unc, err, status="partial" if err else "ok")


def _persist_proposers(conn, api_row: dict, bill_id: str) -> None:
    """대표(RST)/공동(PUBL) 발의자 → bill_proposers(+legislators 최소행).

    RST_MONA_CD(대표, 통상 단일) / PUBL_MONA_CD(공동, 구분자목록 방어분할).
    RST 이름은 RST_PROPOSER 로 최소행 채움. 공동 이름은 1:1 매핑 불가 → 이후 명부에서 enrich.
    """
    rst_name = _clean(api_row.get("RST_PROPOSER"))
    for cd in _split_codes(api_row.get("RST_MONA_CD")):
        _ensure_legislator(conn, cd, rst_name)
        db.upsert(conn, "bill_proposers",
                  {"bill_id": bill_id, "legislator_id": cd, "role": "RST"},
                  ("bill_id", "legislator_id", "role"))
    for cd in _split_codes(api_row.get("PUBL_MONA_CD")):
        _ensure_legislator(conn, cd, None)
        db.upsert(conn, "bill_proposers",
                  {"bill_id": bill_id, "legislator_id": cd, "role": "PUBL"},
                  ("bill_id", "legislator_id", "role"))


def collect_vote_tallies(http, cfg, conn, *, age: int, run_id: Optional[str] = None) -> dict:
    """의안별 표결(집계) 수집 → bills(member/vote/yes/no/blank_tcnt, proc_result_cd).

    AGE 만으로 대수 전체 벌크 수신(BILL_ID 불필요). content_hash 미소유 → 집계 컬럼
    수동 비교로 변경 판정(무변경=무이벤트). source='na_vote', scope='tally:age:{age}'.
    """
    cfg.require("assembly_key")
    source, scope = "na_vote", f"tally:age:{age}"
    ins = upd = unc = err = 0
    rows_seen = 0
    max_proc = ""
    try:
        db.set_watermark(conn, source, scope, last_run=util.now_kst_iso(),
                         status="running", run_id=run_id)
        for r in _iter_rows(http, cfg, SVC_VOTE_TALLIES, AGE=age):
            rows_seen += 1
            row = _map_tally_row(r, age)
            if row is None:
                err += 1
                continue
            bid = row["bill_id"]
            existing = db.fetchone(
                conn,
                "SELECT member_tcnt,vote_tcnt,yes_tcnt,no_tcnt,blank_tcnt,"
                "proc_result_cd,bill_kind_cd FROM bills WHERE bill_id=?", (bid,))
            changed = existing is None or any(
                (existing.get(k) != row.get(k)) for k in _TALLY_COMPARE)
            if not changed:
                unc += 1
                continue
            try:
                with db.tx(conn):
                    action = db.upsert(conn, "bills", row, "bill_id")
                    if action == "inserted":
                        ins += 1
                    else:
                        upd += 1
                    ratio = bill_vote_ratio({**(existing or {}), **row})
                    db.log_change(conn, entity_type="bill", entity_id=bid,
                                  entity_name=row["name"], event="vote_recorded",
                                  source=source, scope=scope,
                                  after=(str(ratio.get("yes_ratio")) if ratio else None),
                                  fields_changed=json.dumps(list(_TALLY_COMPARE)),
                                  run_id=run_id, official_url=row.get("link_url"))
            except (TransientError, PermanentError) as exc:
                err += 1
                log.warning("tally upsert 실패 bill_id=%s: %s", bid, exc)
                continue
            pd = row.get("proc_dt") or ""
            if pd > max_proc:
                max_proc = pd
    except PermanentError:
        db.mark_partition_status(conn, source, scope, "error", bump_retry=True)
        conn.commit()
        raise
    except TransientError as exc:
        db.mark_partition_status(conn, source, scope, "partial",
                                 note=str(exc)[:200], bump_retry=True)
        conn.commit()
        return _result(ins, upd, unc, err + 1, status="partial")

    cursor = f"procdt:{max_proc}|total:{rows_seen}"
    with db.tx(conn):
        db.advance_cursor(conn, source, scope, cursor,
                          last_hash=util.content_hash(f"{rows_seen}|{max_proc}"),
                          status="ok", changed=ins + upd, rows_seen=rows_seen, run_id=run_id)
    return _result(ins, upd, unc, err, status="partial" if err else "ok")


def collect_member_votes(http, cfg, conn, *, age: int, bill_ids: list[str],
                         run_id: Optional[str] = None) -> dict:
    """의원별 표결 수집 → votes(party_at_vote=POLY_NM). AGE+BILL_ID 필수 → 의안 순회.

    bill_ids 미지정 시 '미의결→의결 전이' 워킹셋을 DB에서 유도(집계 있으나 개인표결 부재).
    votes 는 역사적 불변 → 이미 있는 (bill_id,legislator_id) 는 재기록 생략(무변경).
    source='na_vote', scope='age:{age}'.
    """
    cfg.require("assembly_key")
    source, scope = "na_vote", f"age:{age}"
    ids = [str(b).strip() for b in (bill_ids or []) if str(b).strip()]
    if not ids:
        ids = [r["bill_id"] for r in db.fetchall(
            conn,
            "SELECT bill_id FROM bills WHERE age=? "
            "AND (vote_tcnt IS NOT NULL OR proc_result_cd IS NOT NULL) "
            "AND bill_id NOT IN (SELECT DISTINCT bill_id FROM votes)", (age,))]

    ins = upd = unc = err = 0
    bills_done = 0
    db.set_watermark(conn, source, scope, last_run=util.now_kst_iso(),
                     status="running", run_id=run_id)
    for bid in ids:
        recorded = 0
        try:
            with db.tx(conn):
                got_rows = False
                for r in _iter_rows(http, cfg, SVC_MEMBER_VOTES, AGE=age, BILL_ID=bid):
                    got_rows = True
                    vrow = _map_vote_row(r)
                    if vrow is None:
                        err += 1
                        continue
                    _ensure_bill(conn, bid, age, _clean(r.get("BILL_NAME")))
                    _ensure_legislator(conn, vrow["legislator_id"], _clean(r.get("HG_NM")))
                    _ensure_party(conn, _party_row(vrow.get("party_at_vote"),
                                                   vrow.get("party_cd_at_vote")))
                    if db.fetchone(conn, "SELECT 1 FROM votes WHERE bill_id=? AND legislator_id=?",
                                   (vrow["bill_id"], vrow["legislator_id"])):
                        unc += 1
                        continue
                    db.upsert(conn, "votes", vrow, ("bill_id", "legislator_id"))
                    ins += 1
                    recorded += 1
                if recorded:
                    name_row = db.fetchone(conn, "SELECT name FROM bills WHERE bill_id=?", (bid,))
                    db.log_change(conn, entity_type="bill", entity_id=bid,
                                  entity_name=(name_row or {}).get("name"),
                                  event="vote_recorded", source=source, scope=scope,
                                  after=str(recorded), run_id=run_id)
        except NotFound:
            unc += 0  # 표결 없음(정상적 없음)
        except PermanentError:
            db.mark_partition_status(conn, source, scope, "error", bump_retry=True)
            conn.commit()
            raise
        except TransientError as exc:
            err += 1
            log.warning("member_votes 실패 bill_id=%s: %s", bid, exc)
            continue
        bills_done += 1

    cursor = f"bills:{bills_done}|votes:{ins}"
    with db.tx(conn):
        db.advance_cursor(conn, source, scope, cursor, status="ok",
                          changed=ins, rows_seen=ins + unc, run_id=run_id)
    return _result(ins, upd, unc, err, status="partial" if err else "ok")


def collect_legislators(http, cfg, conn, *, run_id: Optional[str] = None) -> dict:
    """현직 의원 명부 수집 → legislators / parties. MONA_CD 조인키.

    parties(current_party FK) 먼저 upsert 후 legislator.current_party 세팅.
    source='na_bill', scope='legislators'.
    """
    cfg.require("assembly_key")
    source, scope = "na_bill", "legislators"
    ins = upd = unc = err = 0
    rows_seen = 0
    try:
        db.set_watermark(conn, source, scope, last_run=util.now_kst_iso(),
                         status="running", run_id=run_id)
        for r in _iter_rows(http, cfg, SVC_LEGISLATORS):
            rows_seen += 1
            mapped = _map_legislator_row(r)
            if mapped is None:
                err += 1
                continue
            party_row, leg = mapped
            try:
                with db.tx(conn):
                    _ensure_party(conn, party_row)
                    existed = db.fetchone(
                        conn, "SELECT 1 FROM legislators WHERE legislator_id=?",
                        (leg["legislator_id"],))
                    db.upsert(conn, "legislators", leg, "legislator_id")
                    if existed:
                        upd += 1
                    else:
                        ins += 1
                        db.log_change(conn, entity_type="legislator",
                                      entity_id=leg["legislator_id"], entity_name=leg["name"],
                                      event="created", source=source, scope=scope, run_id=run_id)
            except (TransientError, PermanentError) as exc:
                err += 1
                log.warning("legislator upsert 실패 mona=%s: %s", leg.get("legislator_id"), exc)
                continue
    except PermanentError:
        db.mark_partition_status(conn, source, scope, "error", bump_retry=True)
        conn.commit()
        raise
    except TransientError as exc:
        db.mark_partition_status(conn, source, scope, "partial",
                                 note=str(exc)[:200], bump_retry=True)
        conn.commit()
        return _result(ins, upd, unc, err + 1, status="partial")

    with db.tx(conn):
        db.advance_cursor(conn, source, scope, f"count:{rows_seen}", status="ok",
                          changed=ins + upd, rows_seen=rows_seen, run_id=run_id)
    return _result(ins, upd, unc, err, status="partial" if err else "ok")


# =========================================================================== #
# 파생 집계 (뷰 대용 순수/질의 헬퍼) — "정당별 찬반집계"(태스크)
# =========================================================================== #
def aggregate_party_votes(conn, *, bill_id: Optional[str] = None,
                          age: Optional[int] = None) -> list[dict]:
    """정당(POLY_NM)×표결결과(RESULT_VOTE_MOD) 집계. votes.party_at_vote 직접 사용.

    반환: [{party, result, n}]. bill_id/age 로 스코프. 표결 API가 정당을 이미 포함하므로
    명부 조인 없이도 정당비율 계산 가능(명세[assembly] §3-B).
    """
    where, params = [], []
    if bill_id:
        where.append("v.bill_id=?")
        params.append(bill_id)
    if age is not None:
        where.append("b.age=?")
        params.append(age)
    sql = ("SELECT v.party_at_vote AS party, v.result_vote_mod AS result, COUNT(*) AS n "
           "FROM votes v JOIN bills b ON b.bill_id=v.bill_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY v.party_at_vote, v.result_vote_mod ORDER BY v.party_at_vote, v.result_vote_mod"
    return db.fetchall(conn, sql, params)


def party_breakdown(conn, bill_id: str) -> dict:
    """단일 의안의 정당별 찬반 요약: {party: {찬성,반대,기권,total}}."""
    out: dict[str, dict] = {}
    for r in aggregate_party_votes(conn, bill_id=bill_id):
        party = r.get("party") or "(무소속/미상)"
        slot = out.setdefault(party, {})
        slot[r.get("result") or "(미상)"] = r.get("n")
        slot["total"] = slot.get("total", 0) + int(r.get("n") or 0)
    return out


__all__ = [
    "SVC_BILLS", "SVC_VOTE_TALLIES", "SVC_MEMBER_VOTES", "SVC_LEGISLATORS",
    "_call",
    "collect_bills", "collect_vote_tallies", "collect_member_votes", "collect_legislators",
    "bill_vote_ratio", "aggregate_party_votes", "party_breakdown",
    "_parse_envelope",   # 픽스처 테스트 공개
]
