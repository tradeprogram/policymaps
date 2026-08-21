"""policymap.collectors.law — 국가법령정보 Open API 수집기.

호출 도메인: www.law.go.kr/DRF (목록 lawSearch.do / 본문·구조 lawService.do).
공통 파라미터: OC(=cfg.law_oc), target, type=JSON. (명세[law_api] 실호출 검증본)

저수준(dict 반환, 에러는 util.NotFound/PermanentError 로 정규화):
    law_list / law_body / eflaw_list
    ordin_list / ordin_body
    admrul_list / admrul_body
    ls_stmd / ls_delegated / lnk_ls_ord_jo / lnk_ls_ord

고수준(증분·저장, run.py 가 호출):
    collect_statutes(...)     # 위임 상위법(유한집합) → legal_instrument(+articles)
    collect_ordinances(...)   # 지자체 파티션 1개 → ordinances(+ordinance_articles)

규율(CONTRACTS.md):
  * 코어는 표준라이브러리만. 파서(parsers.article)는 병렬 구현 중 → 지연 import + 폴백.
  * 인증실패 응답 {"result","msg"} → PermanentError. 본문 미매치(루트 'Law'=str) → NotFound.
  * 목록 정상 resultCode=='00'. 연계/체계도(lnk*/lsStmd/lsDelegated)는 resultCode 없음 → 루트 존재로 판정.
  * 무변경(upsert 'unchanged') 시 log_change 호출 금지. 파티션 완전 영속화 후에만 advance_cursor.
  * 폐지·소멸은 하드삭제 금지 → db.soft_delete(툼스톤).
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from .. import db
from ..util import (
    NotFound,
    PermanentError,
    as_list,
    content_hash,
    get_logger,
    now_kst_iso,
    today_kst,
)

_log = get_logger("policymap.collectors.law")

# 페이징 안전 상한(무한루프 방지). display=100 기준 최대 노출 건수.
_MAX_PAGES = 200
_DEFAULT_DISPLAY = 100

# ordin knd → 자치법규종류 폴백(응답에 자치법규종류 없을 때)
_KND_TO_KIND = {
    "30001": "조례",
    "30002": "규칙",
    "30003": "훈령",
    "30004": "예규",
    "30006": "기타",
    "30010": "고시",
    "30011": "의회규칙",
}


# --------------------------------------------------------------------------- #
# 저수준 요청 헬퍼
# --------------------------------------------------------------------------- #
def _endpoint(cfg, name: str) -> str:
    return cfg.law_base.rstrip("/") + "/" + name


def _domain(cfg) -> str:
    """cfg.law_base 에서 scheme://netloc 추출(official_url 절대경로화용)."""
    parts = urlsplit(cfg.law_base)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return "https://www.law.go.kr"


def _abs_url(cfg, link: Optional[str]) -> Optional[str]:
    if not link:
        return None
    link = str(link)
    if link.startswith("http://") or link.startswith("https://"):
        return link
    if link.startswith("/"):
        return _domain(cfg) + link
    return link


def _clean_params(params: dict) -> dict:
    """None 값 제거(법령API는 빈 파라미터에 민감)."""
    return {k: v for k, v in params.items() if v is not None}


def _request(http, cfg, endpoint: str, params: dict) -> Any:
    """공통 요청. OC/type 주입, 인증실패 감지 → PermanentError.

    http.get_json 은 빈/비-JSON 응답을 NotFound 로 던진다(util). 여기서는
    상위 계층의 인증실패({"result","msg"})만 추가로 해석한다.
    """
    cfg.require("law_oc")
    full = _clean_params({"OC": cfg.law_oc, "type": "JSON", **params})
    resp = http.get_json(_endpoint(cfg, endpoint), full)
    # 인증실패: 루트에 result+msg (정상 래퍼는 resultCode/resultMsg 를 내부에 둠)
    if isinstance(resp, dict) and "result" in resp and "msg" in resp and "resultCode" not in resp:
        raise PermanentError(f"법령API 인증실패: {resp.get('msg') or resp.get('result')}")
    return resp


def _root(resp: Any, key: str, ctx: str) -> dict:
    """래퍼 응답에서 루트 객체 추출. 없으면 NotFound(정상적 없음)."""
    if isinstance(resp, dict):
        root = resp.get(key)
        if isinstance(root, dict):
            return root
    raise NotFound(f"{ctx}: '{key}' 루트 없음")


def _check_body_notfound(resp: Any) -> None:
    """본문 조회 미매치: 루트 키 'Law' 가 문자열이면 에러 신호 → NotFound."""
    if isinstance(resp, dict) and isinstance(resp.get("Law"), str):
        raise NotFound(resp["Law"])


def _list_from(root: dict, item_key: str) -> tuple[int, list[dict]]:
    total = int(root.get("totalCnt") or 0)
    items = [it for it in as_list(root.get(item_key)) if isinstance(it, dict)]
    return total, items


# --------------------------------------------------------------------------- #
# 저수준: 목록(lawSearch.do)
# --------------------------------------------------------------------------- #
def law_list(http, cfg, query, *, display: int = _DEFAULT_DISPLAY, page: int = 1,
             **params) -> tuple[int, list[dict]]:
    """target=law 현행법령 목록. 반환 (totalCnt, law[])."""
    p = {"target": "law", "query": query, "display": display, "page": page, **params}
    root = _root(_request(http, cfg, "lawSearch.do", p), "LawSearch", "law_list")
    return _list_from(root, "law")


def eflaw_list(http, cfg, query, *, display: int = _DEFAULT_DISPLAY, page: int = 1,
               **params) -> tuple[int, list[dict]]:
    """target=eflaw 시행일법령 목록(시행예정 포함). 루트/필드는 law 와 동일."""
    p = {"target": "eflaw", "query": query, "display": display, "page": page, **params}
    root = _root(_request(http, cfg, "lawSearch.do", p), "LawSearch", "eflaw_list")
    return _list_from(root, "law")


def ordin_list(http, cfg, *, query=None, org=None, sborg=None, knd: str = "30001",
               nw: int = 1, rr_cls_cd=None, display: int = _DEFAULT_DISPLAY,
               page: int = 1, **params) -> tuple[int, list[dict]]:
    """target=ordin 자치법규 목록. 루트 OrdinSearch.law[]. MST=자치법규일련번호."""
    p = {
        "target": "ordin", "display": display, "page": page, "nw": nw, "knd": knd,
        "query": query, "org": org, "sborg": sborg, "rrClsCd": rr_cls_cd, **params,
    }
    root = _root(_request(http, cfg, "lawSearch.do", p), "OrdinSearch", "ordin_list")
    return _list_from(root, "law")


def admrul_list(http, cfg, query, *, knd=None, org=None,
                display: int = _DEFAULT_DISPLAY, page: int = 1) -> tuple[int, list[dict]]:
    """target=admrul 행정규칙 목록. 루트 AdmRulSearch.admrul[]. 본문키=행정규칙일련번호."""
    p = {"target": "admrul", "query": query, "display": display, "page": page,
         "knd": knd, "org": org}
    root = _root(_request(http, cfg, "lawSearch.do", p), "AdmRulSearch", "admrul_list")
    return _list_from(root, "admrul")


def lnk_ls_ord_jo(http, cfg, query, *, jo=None, jobr=None,
                  display: int = _DEFAULT_DISPLAY, page: int = 1) -> tuple[int, list[dict]]:
    """target=lnkLsOrdJo 법령↔자치법규 조문단위 연계. 루트 lnkOrdJoSearch.law[]."""
    p = {"target": "lnkLsOrdJo", "query": query, "display": display, "page": page,
         "JO": jo, "JOBR": jobr}
    root = _root(_request(http, cfg, "lawSearch.do", p), "lnkOrdJoSearch", "lnk_ls_ord_jo")
    return _list_from(root, "law")


def lnk_ls_ord(http, cfg, *, query=None, org=None,
               display: int = _DEFAULT_DISPLAY, page: int = 1) -> tuple[int, list[dict]]:
    """target=lnkLsOrd/lnkOrg 법령↔조례 문서단위 연계. 루트 OrdinSearch.law[].

    org 지정 시 지자체별 연계(lnkOrg), 미지정 시 법령명(query)별.
    """
    target = "lnkOrg" if (org is not None and query is None) else "lnkLsOrd"
    p = {"target": target, "query": query, "org": org, "display": display, "page": page}
    root = _root(_request(http, cfg, "lawSearch.do", p), "OrdinSearch", "lnk_ls_ord")
    return _list_from(root, "law")


# --------------------------------------------------------------------------- #
# 저수준: 본문·구조(lawService.do)
# --------------------------------------------------------------------------- #
def law_body(http, cfg, mst) -> dict:
    """target=law 본문. 루트 '법령'(unwrap). 미매치 시 NotFound."""
    resp = _request(http, cfg, "lawService.do", {"target": "law", "MST": str(mst)})
    _check_body_notfound(resp)
    return _root(resp, "법령", "law_body")


def ordin_body(http, cfg, mst) -> tuple[dict, list[dict]]:
    """target=ordin 본문. 반환 (자치법규기본정보, 조[]). 루트 LawService."""
    resp = _request(http, cfg, "lawService.do", {"target": "ordin", "MST": str(mst)})
    _check_body_notfound(resp)
    svc = _root(resp, "LawService", "ordin_body")
    base = svc.get("자치법규기본정보") if isinstance(svc.get("자치법규기본정보"), dict) else {}
    jomun_root = svc.get("조문")
    jomun = []
    if isinstance(jomun_root, dict):
        jomun = [j for j in as_list(jomun_root.get("조")) if isinstance(j, dict)]
    return base, jomun


def admrul_body(http, cfg, serial) -> dict:
    """target=admrul 본문. ID=행정규칙일련번호. 루트 AdmRulService."""
    resp = _request(http, cfg, "lawService.do", {"target": "admrul", "ID": str(serial)})
    _check_body_notfound(resp)
    return _root(resp, "AdmRulService", "admrul_body")


def ls_stmd(http, cfg, mst) -> dict:
    """target=lsStmd 법령체계도(상하위 트리). 루트 법령체계도."""
    resp = _request(http, cfg, "lawService.do", {"target": "lsStmd", "MST": str(mst)})
    _check_body_notfound(resp)
    return _root(resp, "법령체계도", "ls_stmd")


def ls_delegated(http, cfg, mst) -> dict:
    """target=lsDelegated 위임법령(위임조문 단위). 루트 lsDelegated."""
    resp = _request(http, cfg, "lawService.do", {"target": "lsDelegated", "MST": str(mst)})
    _check_body_notfound(resp)
    return _root(resp, "lsDelegated", "ls_delegated")


# --------------------------------------------------------------------------- #
# 순수 변환 함수(픽스처 테스트 가능 — 네트워크 불필요)
# --------------------------------------------------------------------------- #
def classify_national_tier(kind_name: Optional[str]) -> tuple[Optional[int], str, int]:
    """법령구분명 → (national_tier, source_type, tier_disputed).

    명세[hierarchy] §1.1 위계축. 학설대립(조약·헌법기관규칙)은 disputed=1.
    """
    n = (kind_name or "").strip()
    if n == "헌법":
        return 0, "constitution", 0
    if n == "법률":
        return 1, "statute", 0
    if n == "조약":
        return 1, "treaty", 1
    if n == "대통령령":
        return 2, "statute", 0
    # 헌법기관규칙(대법원·헌법재판소·중앙선거관리위원회·국회·감사원 규칙) — 서열 학설대립
    if n.endswith("규칙") and any(
        k in n for k in ("대법원", "헌법재판소", "선거관리위원회", "국회", "감사원")
    ):
        return 2, "statute", 1
    if n == "총리령" or n.endswith("부령"):
        return 3, "statute", 0
    if n in ("행정규칙", "훈령", "예규", "고시", "공고", "지침") or "고시" in n:
        return 4, "admin-rule", 0
    # 미분류 국가법령은 tier 미상(NULL)로 두되 statute 로 취급
    return None, "statute", 0


def _ord_kind_meta(kind: Optional[str]) -> tuple[Optional[str], str]:
    """자치법규종류 → (local_tier, enacted_by)."""
    k = (kind or "").strip()
    if "교육규칙" in k:
        return "L2", "교육감"
    if "의회규칙" in k:
        return "L1", "지방의회"
    if "조례" in k:
        return "L1", "지방의회"
    if k == "규칙" or k.startswith("규칙"):
        return "L2", "단체장"
    # 훈령·예규·고시·기타 = 단체장 발령
    return None, "단체장"


def _is_repeal(rr_text: Optional[str]) -> bool:
    """제개정구분명(또는 코드)에 폐지 신호가 있는지."""
    s = (rr_text or "")
    return "폐지" in s or s in ("300204", "300207", "300209")


def statute_row_from_list(item: dict, cfg) -> Optional[dict]:
    """law/eflaw 목록 항목 → legal_instrument row(순수). mst 없으면 None."""
    mst = str(item.get("법령일련번호") or "").strip()
    if not mst:
        return None
    kind_name = (item.get("법령구분명") or "").strip() or "법률"
    tier, source_type, disputed = classify_national_tier(kind_name)
    return {
        "instrument_id": f"statute:{mst}",
        "kind": kind_name,
        "source_type": source_type,
        "national_tier": tier,
        "tier_disputed": disputed,
        "mst": mst,
        "law_id": item.get("법령ID"),
        "name": (item.get("법령명한글") or item.get("법령명") or "").strip() or f"법령 {mst}",
        "short_name": item.get("법령약칭명") or None,
        "competent_authority": item.get("소관부처명"),
        "competent_authority_cd": item.get("소관부처코드"),
        "enacted_on": item.get("공포일자"),
        "promulgation_no": item.get("공포번호"),
        "effective_on": item.get("시행일자"),
        "rr_cls_cd": item.get("제개정구분명"),   # 목록은 명칭 제공(코드 부재) — 있는 정보 보존
        "current_history": item.get("현행연혁코드"),
        "official_url": _abs_url(cfg, item.get("법령상세링크")),
        "as_of_date": today_kst(),
        "status": "active",
        "verification_status": "needs-review",
        "updated_at": now_kst_iso(),
    }


def ordinance_row_from_list(item: dict, region_id: Optional[str], knd: str, cfg) -> Optional[dict]:
    """ordin 목록 항목 → ordinances row(순수). mst 없으면 None."""
    mst = str(item.get("자치법규일련번호") or "").strip()
    if not mst:
        return None
    kind = (item.get("자치법규종류") or _KND_TO_KIND.get(str(knd), "기타")).strip()
    local_tier, enacted_by = _ord_kind_meta(kind)
    rr_text = item.get("제개정구분명")
    return {
        "ordinance_id": f"ordin:{mst}",
        "mst": mst,
        "ord_id": item.get("자치법규ID"),
        "region_id": region_id,
        "org_name": item.get("지자체기관명"),
        "name": (item.get("자치법규명") or "").strip() or f"자치법규 {mst}",
        "ord_kind": kind,
        "local_tier": local_tier,
        "enacted_by": enacted_by,
        "category_field": item.get("자치법규분야명"),
        "enacted_on": item.get("공포일자"),
        "promulgation_no": item.get("공포번호"),
        "effective_on": item.get("시행일자"),
        "rr_cls_cd": rr_text,
        "repealed_on": item.get("공포일자") if _is_repeal(rr_text) else None,
        "official_url": _abs_url(cfg, item.get("자치법규상세링크")),
        "as_of_date": today_kst(),
        "status": "repealed" if _is_repeal(rr_text) else "active",
        "verification_status": "needs-review",
        "updated_at": now_kst_iso(),
    }


def _law_articles_text(body: dict) -> str:
    """법령 본문 조문단위[]의 조문내용 연결(content_hash 원천)."""
    jomun = as_list((body.get("조문") or {}).get("조문단위") if isinstance(body, dict) else None)
    parts = []
    for u in jomun:
        if not isinstance(u, dict):
            continue
        if u.get("조문여부") not in (None, "조문"):
            continue
        c = u.get("조문내용")
        if isinstance(c, list):
            parts.append(" ".join(str(x) for x in c))
        elif c:
            parts.append(str(c))
    return "\n".join(parts)


def _ordinance_articles_text(jomun: list[dict]) -> str:
    """자치법규 조[]의 조내용 연결(content_hash 원천)."""
    parts = []
    for j in jomun or []:
        if not isinstance(j, dict):
            continue
        c = j.get("조내용")
        if isinstance(c, list):
            parts.append(" ".join(str(x) for x in c))
        elif c:
            parts.append(str(c))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 파서 지연 로드(병렬 구현 중 — 부재 시 폴백)
# --------------------------------------------------------------------------- #
def _article_parser():
    try:
        from ..parsers import article as _a  # type: ignore
        return _a
    except Exception:  # pragma: no cover - 파서 미구현/구현중
        return None


def _save_law_articles(conn, body: dict, instrument_id: str) -> int:
    """parsers.article 로 법령 조문 저장. 파서 부재/실패 시 0(비치명적)."""
    ap = _article_parser()
    if ap is None or not hasattr(ap, "parse_law_articles") or not hasattr(ap, "save_articles"):
        return 0
    try:
        rows = ap.parse_law_articles(body, instrument_id)
        if not rows:
            return 0
        res = ap.save_articles(conn, rows, table="articles")
        return int(res.get("inserted", 0)) + int(res.get("updated", 0))
    except Exception as exc:  # pragma: no cover - 파서 스키마 편차 방어
        _log.warning("법령 조문 파싱 스킵(%s): %s", instrument_id, exc)
        return 0


def _save_ordinance_articles(conn, jomun: list[dict], ordinance_id: str) -> int:
    ap = _article_parser()
    if ap is None or not hasattr(ap, "parse_ordinance_articles") or not hasattr(ap, "save_articles"):
        return 0
    try:
        rows = ap.parse_ordinance_articles(jomun, ordinance_id)
        if not rows:
            return 0
        res = ap.save_articles(conn, rows, table="ordinance_articles")
        return int(res.get("inserted", 0)) + int(res.get("updated", 0))
    except Exception as exc:  # pragma: no cover
        _log.warning("자치법규 조문 파싱 스킵(%s): %s", ordinance_id, exc)
        return 0


# --------------------------------------------------------------------------- #
# 통제어휘 / FK 무결성 헬퍼
# --------------------------------------------------------------------------- #
def _ensure_kind(conn, kind: str, source_type: str, national_tier: Optional[int],
                 tier_disputed: int) -> None:
    """legal_instrument.kind → instrument_kind FK 무결성 보장(멱등 upsert).

    instrument_kind 는 통제어휘지만 법령API가 부처별 부령 등 다양한 명칭을 반환하므로,
    수집 시점에 실제 사용하는 kind 를 결정적으로 시드해 FK 위반을 예방한다.
    """
    if not kind:
        return
    db.upsert(conn, "instrument_kind", {
        "kind": kind,
        "source_type": source_type,
        "national_tier": national_tier,
        "tier_disputed": tier_disputed,
        "api_target": "law",
    }, "kind")


def _ensure_region_stub(conn, region_id: Optional[str], org_name: Optional[str],
                        law_org: Optional[str], law_sborg: Optional[str]) -> None:
    """ordinances.region_id → regions FK 무결성 보장.

    geo.py 가 regions 스파인을 먼저 채우는 것이 정상 경로이나, 병렬/부분 실행에서
    지역 행이 없으면 FK 위반이 발생한다. 없을 때만 최소 스텁을 삽입하고
    (기존 geo 데이터는 덮지 않음), geo 수집이 이후 enrich 한다.
    """
    if not region_id:
        return
    exists = db.fetchone(conn, "SELECT 1 AS x FROM regions WHERE region_id=?", (region_id,))
    if exists:
        return
    name = (org_name or region_id).split()[-1] if org_name else region_id
    db.upsert(conn, "regions", {
        "region_id": region_id,
        "sig_cd": region_id[:5] if region_id and region_id[:1].isdigit() else None,
        "name": name,
        "full_name": org_name,
        "level": 2,
        "law_org": law_org,
        "law_sborg": law_sborg,
        "status": "active",
        "as_of_date": today_kst(),
        "source": "ordin-stub",
        "updated_at": now_kst_iso(),
    }, "region_id")


def _region_sig(conn, region_id: str) -> str:
    row = db.fetchone(conn, "SELECT sig_cd FROM regions WHERE region_id=?", (region_id,))
    if row and row.get("sig_cd"):
        return str(row["sig_cd"])
    return str(region_id)


def _empty_result() -> dict:
    return {"inserted": 0, "updated": 0, "unchanged": 0, "changed": 0,
            "errors": 0, "status": "ok"}


def _finalize_status(res: dict) -> None:
    ok_rows = res["inserted"] + res["updated"] + res["unchanged"]
    if res["errors"] and ok_rows == 0:
        res["status"] = "error"
    elif res["errors"]:
        res["status"] = "partial"
    else:
        res["status"] = "ok"


# --------------------------------------------------------------------------- #
# 고수준: 위임 상위법(법률·시행령·시행규칙) 수집
# --------------------------------------------------------------------------- #
def collect_statutes(http, cfg, conn, *, queries: list[str], run_id=None) -> dict:
    """위임 상위법(유한집합) upsert → legal_instrument(+articles).

    source='law', scope='global'. cursor='ancYd:{최근공포일}'.
    각 query 는 상위법명(예 '주차장법'). 반환 dict 공통형태.
    """
    cfg.require("law_oc")
    res = _empty_result()
    max_anc = ""
    seen: set[str] = set()

    for query in queries or []:
        try:
            total, first = law_list(http, cfg, query, display=_DEFAULT_DISPLAY, page=1)
        except NotFound:
            continue
        except PermanentError:
            raise  # 인증실패 = 소스 전체 치명 → run.py 파티션 격리로 처리
        pages = min(_MAX_PAGES, max(1, math.ceil(total / _DEFAULT_DISPLAY))) if total else 1
        items = list(first)
        for page in range(2, pages + 1):
            try:
                _, more = law_list(http, cfg, query, display=_DEFAULT_DISPLAY, page=page)
            except NotFound:
                break
            if not more:
                break
            items.extend(more)

        for item in items:
            base_row = statute_row_from_list(item, cfg)
            if base_row is None:
                continue
            iid = base_row["instrument_id"]
            if iid in seen:
                continue
            seen.add(iid)
            try:
                _persist_statute(http, cfg, conn, base_row, res, run_id)
            except PermanentError:
                raise
            except Exception as exc:  # 개별 법령 실패 격리
                res["errors"] += 1
                _log.warning("법령 수집 실패(%s): %s", iid, exc)
                continue
            anc = base_row.get("enacted_on") or ""
            if anc > max_anc:
                max_anc = anc

    _finalize_status(res)
    cursor = f"ancYd:{max_anc}" if max_anc else "ancYd:"
    if res["status"] != "error":
        with db.tx(conn):
            db.advance_cursor(conn, "law", "global", cursor,
                              status=res["status"], changed=res["changed"],
                              rows_seen=len(seen), run_id=run_id)
    else:
        with db.tx(conn):
            db.mark_partition_status(conn, "law", "global", "error",
                                     note="collect_statutes 전건 실패", bump_retry=True)
    return res


def _persist_statute(http, cfg, conn, base_row: dict, res: dict, run_id) -> None:
    """법령 1건: 본문 조회 → content_hash → upsert(해시가드) → 조문 저장 + change_log."""
    iid = base_row["instrument_id"]
    mst = base_row["mst"]
    body: Optional[dict] = None
    try:
        body = law_body(http, cfg, mst)
    except NotFound:
        body = None  # 본문 미매치 → 메타데이터만 저장(폴백 해시)

    if body is not None:
        text = _law_articles_text(body)
        base_row["content_hash"] = content_hash(text or base_row["name"])
        base_row["verification_status"] = "source-linked"
    else:
        base_row["content_hash"] = content_hash(
            base_row["name"] + (base_row.get("enacted_on") or ""))

    tier, source_type, disputed = classify_national_tier(base_row["kind"])
    with db.tx(conn):
        _ensure_kind(conn, base_row["kind"], source_type, tier, disputed)
        action = db.upsert(conn, "legal_instrument", base_row, "instrument_id",
                           hash_col="content_hash")
        if action == "unchanged":
            res["unchanged"] += 1
            return
        res[action] += 1
        res["changed"] += 1
        if body is not None:
            _save_law_articles(conn, body, iid)
        db.log_change(
            conn, entity_type="instrument", entity_id=iid,
            event="created" if action == "inserted" else "amended",
            source="law", scope="global", entity_name=base_row["name"],
            after=base_row.get("enacted_on"), region_code=None,
            run_id=run_id, official_url=base_row.get("official_url"),
        )


# --------------------------------------------------------------------------- #
# 고수준: 자치법규 파티션(지자체 1개) 수집
# --------------------------------------------------------------------------- #
def collect_ordinances(http, cfg, conn, *, region_id: str, org: str,
                       sborg: Optional[str] = None, knds: Iterable[str] = ("30001", "30002"),
                       run_id=None) -> dict:
    """지자체 파티션 1개 수집. 1단 목록 워터마크 → 2단 본문 content_hash 가드.

    source='ordin', scope=f'sig:{sig_cd}'. cursor='efYd:..|ancYd:..|maxMST:..'.
    신규/개정(=새 자치법규일련번호)만 본문 재조회. 폐지(rr=폐지)는 soft_delete + log_change.
    """
    cfg.require("law_oc")
    res = _empty_result()
    sig_cd = _region_sig(conn, region_id)
    scope = f"sig:{sig_cd}"
    rows_seen = 0
    max_ef = max_anc = max_mst = ""

    for knd in knds:
        knd = str(knd)
        try:
            total, first = ordin_list(http, cfg, org=org, sborg=sborg, knd=knd,
                                      display=_DEFAULT_DISPLAY, page=1)
        except NotFound:
            continue
        except PermanentError:
            raise
        pages = min(_MAX_PAGES, max(1, math.ceil(total / _DEFAULT_DISPLAY))) if total else 1
        items = list(first)
        for page in range(2, pages + 1):
            try:
                _, more = ordin_list(http, cfg, org=org, sborg=sborg, knd=knd,
                                     display=_DEFAULT_DISPLAY, page=page)
            except NotFound:
                break
            if not more:
                break
            items.extend(more)

        for item in items:
            rows_seen += 1
            row = ordinance_row_from_list(item, region_id, knd, cfg)
            if row is None:
                continue
            try:
                _persist_ordinance(http, cfg, conn, row, item, res, run_id,
                                   sig_cd=sig_cd, scope=scope, org=org, sborg=sborg)
            except PermanentError:
                raise
            except Exception as exc:  # 개별 조례 실패 격리
                res["errors"] += 1
                _log.warning("자치법규 수집 실패(%s): %s", row.get("ordinance_id"), exc)
                continue
            ef, anc, mst = row.get("effective_on") or "", row.get("enacted_on") or "", row["mst"]
            if ef > max_ef:
                max_ef = ef
            if anc > max_anc:
                max_anc = anc
            if mst > max_mst:
                max_mst = mst

    _finalize_status(res)
    cursor = f"efYd:{max_ef}|ancYd:{max_anc}|maxMST:{max_mst}"
    if res["status"] != "error":
        with db.tx(conn):
            db.advance_cursor(conn, "ordin", scope, cursor,
                              status=res["status"], changed=res["changed"],
                              rows_seen=rows_seen, run_id=run_id)
    else:
        with db.tx(conn):
            db.mark_partition_status(conn, "ordin", scope, "error",
                                     note="collect_ordinances 전건 실패", bump_retry=True)
    return res


def _persist_ordinance(http, cfg, conn, row: dict, item: dict, res: dict, run_id,
                       *, sig_cd: str, scope: str, org: str, sborg: Optional[str]) -> None:
    """자치법규 1건: 폐지/신규/개정 분기 → upsert(해시가드) → 조문 저장 + change_log."""
    oid = row["ordinance_id"]
    ord_id = row.get("ord_id")

    # (1) 폐지: 본문 재조회 불필요. 폐지 레코드 저장 + 동일 자치법규 이전본 툼스톤.
    if row["status"] == "repealed":
        row["content_hash"] = content_hash(row["name"] + (row.get("enacted_on") or ""))
        with db.tx(conn):
            _ensure_region_stub(conn, row["region_id"], row.get("org_name"), org, sborg)
            action = db.upsert(conn, "ordinances", row, "ordinance_id",
                               hash_col="content_hash")
            if action == "unchanged":
                res["unchanged"] += 1
                return
            res[action] += 1
            res["changed"] += 1
            _supersede_prior_versions(conn, ord_id, oid, row.get("repealed_on"),
                                      status="repealed")
            db.log_change(
                conn, entity_type="ordinance", entity_id=oid, event="repealed",
                source="ordin", scope=scope, entity_name=row["name"],
                after=row.get("enacted_on"), region_code=sig_cd,
                run_id=run_id, official_url=row.get("official_url"),
            )
        return

    # (2) 이미 확보한 버전인지 확인(새 자치법규일련번호 = 새 버전 규율).
    existing = db.fetchone(
        conn, "SELECT verification_status, status FROM ordinances WHERE ordinance_id=?", (oid,))
    if existing and existing.get("verification_status") == "source-linked" \
            and existing.get("status") == "active":
        res["unchanged"] += 1
        return  # 동일 버전·본문 링크 완료 → 본문 재조회 생략

    # (3) 신규/개정: 본문 재조회 → content_hash → upsert.
    jomun: list[dict] = []
    try:
        base, jomun = ordin_body(http, cfg, row["mst"])
        if isinstance(base, dict):
            if base.get("담당부서명"):
                row["department"] = base.get("담당부서명")
            if base.get("전화번호"):
                row["phone"] = base.get("전화번호")
        row["article_count"] = len(jomun)
        row["content_hash"] = content_hash(_ordinance_articles_text(jomun) or row["name"])
        row["verification_status"] = "source-linked"
    except NotFound:
        row["content_hash"] = content_hash(row["name"] + (row.get("enacted_on") or ""))

    # 개정 여부: 동일 자치법규ID 의 다른 활성 버전 존재 → amended
    prior = _prior_versions(conn, ord_id, oid)
    with db.tx(conn):
        _ensure_region_stub(conn, row["region_id"], row.get("org_name"), org, sborg)
        action = db.upsert(conn, "ordinances", row, "ordinance_id", hash_col="content_hash")
        if action == "unchanged":
            res["unchanged"] += 1
            return
        res[action] += 1
        res["changed"] += 1
        if jomun:
            _save_ordinance_articles(conn, jomun, oid)
        if prior:
            _supersede_prior_versions(conn, ord_id, oid, row.get("enacted_on"),
                                      status="superseded")
        db.log_change(
            conn, entity_type="ordinance", entity_id=oid,
            event="amended" if prior else "created",
            source="ordin", scope=scope, entity_name=row["name"],
            after=row.get("enacted_on"), region_code=sig_cd,
            run_id=run_id, official_url=row.get("official_url"),
        )


def _prior_versions(conn, ord_id: Optional[str], current_oid: str) -> list[dict]:
    """동일 자치법규ID(ord_id)의 다른 활성 버전 목록(개정 감지)."""
    if not ord_id:
        return []
    return db.fetchall(
        conn,
        "SELECT ordinance_id FROM ordinances "
        "WHERE ord_id=? AND ordinance_id<>? AND status='active'",
        (ord_id, current_oid),
    )


def _supersede_prior_versions(conn, ord_id: Optional[str], current_oid: str,
                              valid_to: Optional[str], *, status: str) -> None:
    """동일 자치법규의 이전 활성 버전을 툼스톤(superseded/repealed) 처리."""
    if not ord_id:
        return
    for r in _prior_versions(conn, ord_id, current_oid):
        db.soft_delete(conn, "ordinances", {"ordinance_id": r["ordinance_id"]},
                       status=status, valid_to_col="repealed_on", valid_to=valid_to)


__all__ = [
    # 저수준 목록
    "law_list", "eflaw_list", "ordin_list", "admrul_list",
    "lnk_ls_ord_jo", "lnk_ls_ord",
    # 저수준 본문·구조
    "law_body", "ordin_body", "admrul_body", "ls_stmd", "ls_delegated",
    # 고수준 수집
    "collect_statutes", "collect_ordinances",
    # 순수 변환(테스트용 노출)
    "classify_national_tier", "statute_row_from_list", "ordinance_row_from_list",
]
