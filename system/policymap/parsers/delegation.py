"""policymap.parsers.delegation — 위임 4경로 합집합 + 낫표(「」) 인용 파싱.

CONTRACTS.md §2.2 계약:
    parse_citations(text) -> list[dict]
    extract_delegations_from_lsdelegated(resp, parent_id) -> list[dict]
    extract_delegations_from_lsstmd(resp, parent_id)      -> list[dict]
    extract_delegations_from_lnkjo(rows, parent_id)       -> list[dict]
    extract_delegations_from_lnkls(rows, parent_id)       -> list[dict]
    merge_delegations(*sources) -> list[dict]
    save_delegations(conn, rows) -> dict[str,int]
    save_citations(conn, rows)  -> dict[str,int]

설계(명세[law_api] §7~10 위임 4경로 + korea100 article-citations 규율):
  * 4경로: lsDelegated(조문·최다) ∪ lsStmd(문서·트리) ∪ lnkLsOrdJo(조문-조문) ∪ lnkLsOrd(문서).
    dedup 키 = (child_id, child_article); 조문정밀(lsDelegated/lnkLsOrdJo) 우선.
  * 상위법(parent_id)은 호출부가 전달('statute:{mst}' 등). 하위 조례 child_id='ordin:{자치법규일련번호}'.
  * 근거법령(상위법) 필드가 없는 ordin 본문은 「」 인용 파싱으로 보완 → instrument_relations(CITES).
    낫표 인용은 검증상태 unverified(needs-review), source_path='citation'.

파싱/추출/merge 는 DB 비의존 순수함수 → 픽스처로 단위테스트 가능.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from .. import db as _db
from .. import util as _util

# 4경로 provenance 우선순위(작을수록 정밀·우선). merge dedup 시 승자 결정.
_SOURCE_PRIORITY = {
    "lsDelegated": 0,   # 위임조문·최다 정밀
    "lnkLsOrdJo": 1,    # 조문-조문 연계
    "lsStmd": 2,        # 체계도(문서 트리)
    "lnkLsOrd": 3,      # 문서 연계
    "lnkOrg": 3,        # 지자체별 문서 연계
    "citation": 4,      # 낫표 추론(최약)
}


# --------------------------------------------------------------------------- #
# 1. 낫표(「」) 인용 파싱
# --------------------------------------------------------------------------- #
# 「법령명」 뒤에 이어지는 제N조(의M) 제K항 제L호 O목 (선택적)
_CITATION_RE = re.compile(
    r"「([^」]{1,60})」\s*"
    r"(제\s*\d+\s*조(?:\s*의\s*\d+)?"
    r"(?:\s*제\s*\d+\s*항)?(?:\s*제\s*\d+\s*호)?(?:\s*[가-힣]\s*목)?)?"
)
_ART_DETAIL_RE = re.compile(
    r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?"
    r"(?:\s*제\s*(\d+)\s*항)?(?:\s*제\s*(\d+)\s*호)?(?:\s*([가-힣])\s*목)?"
)


def _article_label(no: Any, branch: Any = None) -> str:
    n = int(no)
    b = str(branch or "").strip()
    if b.isdigit() and int(b) > 0:
        return f"제{n}조의{int(b)}"
    return f"제{n}조"


def parse_citations(text: str) -> list[dict]:
    """낫표 「법령명」 제N조제M항제K호O목 파싱 → 인용 후보 목록.

    반환 [{'law_name','article','clause','item','subitem','raw'}].
    article 은 '제N조[의M]' 라벨(없으면 None). clause/item=int|None, subitem=한글자|None.
    korea100 parseArticleCitationDetails 규율(항/호/목 단위) 승계.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    norm = re.sub(r"[∼～〜–—]", "~", text)
    out: list[dict] = []
    seen: set[tuple] = set()
    for m in _CITATION_RE.finditer(norm):
        law_name = m.group(1).strip()
        art_span = (m.group(2) or "").strip()
        article = clause = item = subitem = None
        if art_span:
            dm = _ART_DETAIL_RE.match(art_span)
            if dm:
                article = _article_label(dm.group(1), dm.group(2))
                clause = int(dm.group(3)) if dm.group(3) else None
                item = int(dm.group(4)) if dm.group(4) else None
                subitem = dm.group(5) or None
        raw = m.group(0).strip()
        key = (law_name, article, clause, item, subitem)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "law_name": law_name,
            "article": article,
            "clause": clause,
            "item": item,
            "subitem": subitem,
            "raw": raw,
        })
    return out


# --------------------------------------------------------------------------- #
# 2. 위임 4경로 추출
# --------------------------------------------------------------------------- #
def _deleg_row(*, child_id: str, parent_id: str, source_path: str,
               child_article: Optional[str] = None, parent_article: Optional[str] = None,
               relation: str = "DELEGATED_FROM", delegation_type: Optional[str] = "law-delegated",
               trigger_text: Optional[str] = None, citation_text: Optional[str] = None,
               inferred: int = 0) -> dict:
    """delegations 스키마에 맞춘 row 생성(공통). delegation_id/computed_at 는 save 에서 채움."""
    return {
        "child_kind": "ordinance",
        "child_id": child_id,
        "child_article": child_article or None,
        "parent_kind": "instrument",
        "parent_id": parent_id,
        "parent_article": parent_article or None,
        "relation": relation,
        "delegation_type": delegation_type,
        "trigger_text": trigger_text or None,
        "citation_text": citation_text or None,
        "source_path": source_path,
        "inferred": inferred,
        "verification_status": "needs-review",
    }


def extract_delegations_from_lsdelegated(resp: dict, parent_id: str) -> list[dict]:
    """lsDelegated.법령.위임조문정보[] → delegations row[]. 위임구분=='자치법규'만 조례.

    조정보.조문번호 = 상위법 위임 조문(parent_article),
    위임정보[].위임법령일련번호 = 하위 조례 MST(child), 위임법령조문정보.조항호목 = 하위 조문.
    위임정보/위임법령조문정보 는 대상 복수 시 배열/스칼라 혼재 → as_list 방어.
    """
    out: list[dict] = []
    root = resp.get("lsDelegated") if isinstance(resp, dict) else None
    law = root.get("법령") if isinstance(root, dict) else None
    units = _util.as_list(law.get("위임조문정보")) if isinstance(law, dict) else []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        jo = unit.get("조정보") if isinstance(unit.get("조정보"), dict) else {}
        parent_article = str(jo.get("조문번호") or "").strip() or None
        for wi in _util.as_list(unit.get("위임정보")):
            if not isinstance(wi, dict):
                continue
            if wi.get("위임구분") != "자치법규":  # 조례만; 인용법령/시행령 등 제외
                continue
            child_mst = str(wi.get("위임법령일련번호") or "").strip()
            if not child_mst:
                continue
            child_id = f"ordin:{child_mst}"
            infos = _util.as_list(wi.get("위임법령조문정보"))
            if not infos:
                out.append(_deleg_row(child_id=child_id, parent_id=parent_id,
                                      source_path="lsDelegated", parent_article=parent_article,
                                      citation_text=str(wi.get("위임법령제목") or "").strip() or None))
                continue
            for info in infos:
                if not isinstance(info, dict):
                    continue
                out.append(_deleg_row(
                    child_id=child_id, parent_id=parent_id, source_path="lsDelegated",
                    child_article=str(info.get("조항호목") or "").strip() or None,
                    parent_article=parent_article,
                    trigger_text=str(info.get("라인텍스트") or "").strip() or None,
                    citation_text=str(info.get("링크텍스트")
                                      or wi.get("위임법령제목") or "").strip() or None,
                ))
    return out


def extract_delegations_from_lsstmd(resp: dict, parent_id: str) -> list[dict]:
    """lsStmd 체계도(법령체계도.상하위법...) → 자치법규 노드 재귀 수집 → delegations row[](문서단위).

    트리 중첩 구조가 target/버전별로 달라 '기본정보에 자치법규일련번호가 있는 dict'를
    재귀 탐색해 조례 노드만 확보한다(상위 법령·시행령 노드는 자치법규일련번호 없음 → 자연 배제).
    """
    bases: list[dict] = []
    root = resp.get("법령체계도") if isinstance(resp, dict) and "법령체계도" in resp else resp
    _collect_ordinance_bases(root, bases)
    out: list[dict] = []
    seen: set[str] = set()
    for base in bases:
        child_mst = str(base.get("자치법규일련번호") or "").strip()
        if not child_mst or child_mst in seen:
            continue
        seen.add(child_mst)
        out.append(_deleg_row(child_id=f"ordin:{child_mst}", parent_id=parent_id,
                              source_path="lsStmd"))
    return out


def _collect_ordinance_bases(obj: Any, acc: list[dict]) -> None:
    """중첩 dict/list 를 재귀 순회하며 자치법규 기본정보 dict 를 수집."""
    if isinstance(obj, dict):
        base = obj.get("기본정보")
        if isinstance(base, dict) and str(base.get("자치법규일련번호") or "").strip():
            acc.append(base)
        for v in obj.values():
            _collect_ordinance_bases(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _collect_ordinance_bases(v, acc)


def extract_delegations_from_lnkjo(rows: list[dict], parent_id: str) -> list[dict]:
    """lnkLsOrdJo law[] → delegations row[](조문-조문 정밀).

    법령조번호 = 상위 조문(parent_article), 자치법규조번호 = 하위 조문(child_article),
    자치법규일련번호 = 하위 조례 MST.
    """
    out: list[dict] = []
    for r in _util.as_list(rows):
        if not isinstance(r, dict):
            continue
        child_mst = str(r.get("자치법규일련번호") or "").strip()
        if not child_mst:
            continue
        out.append(_deleg_row(
            child_id=f"ordin:{child_mst}", parent_id=parent_id, source_path="lnkLsOrdJo",
            child_article=str(r.get("자치법규조번호") or "").strip() or None,
            parent_article=str(r.get("법령조번호") or "").strip() or None,
        ))
    return out


def extract_delegations_from_lnkls(rows: list[dict], parent_id: str,
                                   *, source_path: str = "lnkLsOrd") -> list[dict]:
    """lnkLsOrd/lnkOrg law[] → delegations row[](문서단위). 동일 조례 중복 → dedup.

    source_path 로 lnkLsOrd(법령별)·lnkOrg(지자체별) 구분 가능.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for r in _util.as_list(rows):
        if not isinstance(r, dict):
            continue
        child_mst = str(r.get("자치법규일련번호") or "").strip()
        if not child_mst or child_mst in seen:
            continue
        seen.add(child_mst)
        out.append(_deleg_row(child_id=f"ordin:{child_mst}", parent_id=parent_id,
                              source_path=source_path))
    return out


# --------------------------------------------------------------------------- #
# 3. 합집합 dedup
# --------------------------------------------------------------------------- #
def merge_delegations(*sources: Iterable[dict]) -> list[dict]:
    """4경로 합집합 dedup. 키=(child_id, child_article); 조문정밀 source 우선.

    문서단위 행(child_article=None)은 (child_id, None) 키로 자기들끼리 dedup되고,
    조문단위 행과는 키가 달라 공존한다(문서=존재사실, 조문=정밀 위치 둘 다 보존).
    동일 키 충돌 시 _SOURCE_PRIORITY 최소(정밀) 행을 승자로 채택하되, 승자에 없는
    parent_article/trigger_text/citation_text 는 패자에서 보충한다.
    """
    best: dict[tuple, dict] = {}
    for src in sources:
        for row in _util.as_list(list(src)):
            if not isinstance(row, dict):
                continue
            key = (row.get("child_id"), row.get("child_article") or "")
            prev = best.get(key)
            if prev is None:
                best[key] = dict(row)
                continue
            pr = _SOURCE_PRIORITY.get(row.get("source_path"), 9)
            pp = _SOURCE_PRIORITY.get(prev.get("source_path"), 9)
            if pr < pp:
                winner, loser = dict(row), prev
            else:
                winner, loser = prev, row
            for f in ("parent_article", "trigger_text", "citation_text"):
                if not winner.get(f) and loser.get(f):
                    winner[f] = loser[f]
            best[key] = winner
    # 결정적 정렬(child_id, child_article, source_path)
    return sorted(
        best.values(),
        key=lambda r: (str(r.get("child_id")), str(r.get("child_article") or ""),
                       _SOURCE_PRIORITY.get(r.get("source_path"), 9)),
    )


# --------------------------------------------------------------------------- #
# 4. 저장
# --------------------------------------------------------------------------- #
def save_delegations(conn, rows: Iterable[dict]) -> dict[str, int]:
    """delegations upsert. delegation_id=stable_id(child_id,parent_id,child_article,
    parent_article,source_path). computed_at 채움. NOT NULL 컬럼 기본값 보정."""
    now = _util.now_kst_iso()
    prepared: list[dict] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("child_id") or not r.get("parent_id"):
            continue
        row = dict(r)
        row.setdefault("child_kind", "ordinance")
        row.setdefault("parent_kind", "instrument")
        row.setdefault("relation", "DELEGATED_FROM")
        row.setdefault("source_path", "citation")
        row.setdefault("verification_status", "needs-review")
        row["delegation_id"] = _util.stable_id(
            row.get("child_id"), row.get("parent_id"),
            row.get("child_article"), row.get("parent_article"), row.get("source_path"),
        )
        row["computed_at"] = now
        prepared.append(row)
    if not prepared:
        return {"inserted": 0, "updated": 0, "unchanged": 0}
    with _db.tx(conn):
        return _db.upsert_many(conn, "delegations", prepared, "delegation_id")


def build_citation_rows(src_kind: str, src_id: str, citations: Iterable[dict],
                        *, src_article: Optional[str] = None) -> list[dict]:
    """parse_citations 결과 → instrument_relations(CITES) row[] 변환(편의 헬퍼).

    상위법 instrument_id 를 해석하지 못한 인용은 dst_id='lawname:{정규화명}' 명목키로 보존
    (검증상태는 저수준 unverified — 후속 legal-source-resolution 으로 승격).
    """
    out: list[dict] = []
    for c in _util.as_list(list(citations)):
        if not isinstance(c, dict):
            continue
        law_name = str(c.get("law_name") or "").strip()
        if not law_name:
            continue
        dst_id = c.get("dst_id") or f"lawname:{_util.compact(law_name)}"
        out.append({
            "src_kind": src_kind,
            "src_id": src_id,
            "dst_kind": c.get("dst_kind") or "instrument",
            "dst_id": dst_id,
            "relation": "CITES",
            "citation_text": c.get("raw") or law_name,
            "citation_type": "reference",
            "src_article": src_article or c.get("src_article"),
            "inferred": 1,
        })
    return out


def save_citations(conn, rows: Iterable[dict]) -> dict[str, int]:
    """instrument_relations(relation='CITES') upsert.
    rel_id=stable_id(src_id,dst_id,relation,src_article,citation_text). computed_at 채움.

    dst_id 미해석 행은 build_citation_rows 로 'lawname:' 명목키를 먼저 부여할 것.
    dst_id 가 없고 law_name 만 있는 원시 행도 방어적으로 명목키를 생성한다.
    """
    now = _util.now_kst_iso()
    prepared: list[dict] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("src_id"):
            continue
        row = dict(r)
        if not row.get("dst_id"):
            name = str(row.get("law_name") or "").strip()
            if not name:
                continue
            row["dst_id"] = f"lawname:{_util.compact(name)}"
            row.setdefault("citation_text", row.get("raw") or name)
        row.setdefault("src_kind", "ordinance")
        row.setdefault("dst_kind", "instrument")
        row.setdefault("relation", "CITES")
        row.setdefault("citation_type", "reference")
        # instrument_relations 실존 컬럼만 upsert 가 취하므로 여분키(law_name/raw)는 무시됨.
        row["rel_id"] = _util.stable_id(
            row.get("src_id"), row.get("dst_id"), row.get("relation"),
            row.get("src_article"), row.get("citation_text"),
        )
        row["computed_at"] = now
        prepared.append(row)
    if not prepared:
        return {"inserted": 0, "updated": 0, "unchanged": 0}
    with _db.tx(conn):
        return _db.upsert_many(conn, "instrument_relations", prepared, "rel_id")


__all__ = [
    "parse_citations",
    "extract_delegations_from_lsdelegated",
    "extract_delegations_from_lsstmd",
    "extract_delegations_from_lnkjo",
    "extract_delegations_from_lnkls",
    "merge_delegations",
    "save_delegations",
    "build_citation_rows",
    "save_citations",
]
