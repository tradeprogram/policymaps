"""policymap.parsers.article — 법령/조례/행정규칙 본문 → 조문 정규화(순수함수).

CONTRACTS.md §2.1 계약:
    parse_law_articles(body, instrument_id)      -> list[dict]   # articles row[]
    parse_ordinance_articles(jomun, ordinance_id)-> list[dict]   # ordinance_articles row[]
    parse_admrul_articles(body, instrument_id)   -> list[dict]   # articles row[]
    save_articles(conn, rows, *, table)          -> dict[str,int]

설계(명세[law_api] §2·§4·§5 + korea100 law-service.mjs/admin-rule-service.mjs 규율):
  * 법령 본문: 법령.조문.조문단위[] — 조문여부=='조문'만. 항 있으면 항/호/목 접어 body,
    없으면 조문내용(제목 제거) + 호/목. article_id='statute:{mst}::제{no}조[의{branch}]'.
  * 자치법규 본문: LawService.조문.조[] — 조문번호가 배열/6자리 zero-pad(중복 방어).
    oa_id='ordin:{mst}::{조문번호6자리}'.
  * 행정규칙 본문: AdmRulService.조문내용(통짜/배열 텍스트) → 정규식 '제N조(제목)' 블록 분할.
  * content_hash=util.content_hash(body) (CDC 2단; hash_col 가드로 무변경 스킵).

파싱 함수는 DB 비의존 순수함수 → 픽스처로 단위테스트 가능. save_* 만 db 접점.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from .. import db as _db
from .. import util as _util

# --------------------------------------------------------------------------- #
# 공통 헬퍼
# --------------------------------------------------------------------------- #
_HEADING_RE = re.compile(r"^\s*제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*\([^)]*\))?\s*")
_DIGITS_RE = re.compile(r"\D")


def _strip_heading(value: Any) -> str:
    """'제N조(제목)' 선두 헤딩 제거(korea100 stripArticleHeading 규율)."""
    return _HEADING_RE.sub("", str(value or "")).strip()


def _digits8(value: Any) -> Optional[str]:
    """YYYYMMDD 8자리 숫자만 추출(원천 그대로 보존). 아니면 None."""
    d = _DIGITS_RE.sub("", str(value or ""))
    return d if len(d) == 8 else None


def _push_line(lines: list[str], value: Any) -> None:
    if isinstance(value, str):
        v = value.strip()
        if v:
            lines.append(v)


def _article_label(no: Any, branch: Any = None) -> Optional[str]:
    """'제{no}조[의{branch}]' 라벨. no가 정수형이 아니면 None(장·절 표제 등 배제)."""
    s = str(no or "").strip()
    if not s.isdigit():
        return None
    n = int(s)
    b = str(branch or "").strip()
    if b.isdigit() and int(b) > 0:
        return f"제{n}조의{int(b)}"
    return f"제{n}조"


# --------------------------------------------------------------------------- #
# 1. 법령 본문 (target=law) → articles
# --------------------------------------------------------------------------- #
def _law_units(body: dict) -> list:
    """법령.조문.조문단위[] 추출. body 는 전체 응답({'법령':..}) 또는 법령 dict 둘 다 수용."""
    if not isinstance(body, dict):
        return []
    root = body.get("법령") if "법령" in body else body
    jomun = root.get("조문") if isinstance(root, dict) else None
    units = jomun.get("조문단위") if isinstance(jomun, dict) else None
    return _util.as_list(units)


def _law_article_text(unit: dict) -> str:
    """항 있으면 항/호/목 접어 텍스트, 없으면 조문내용(제목제거)+호/목.

    korea100 lawArticleText 규율 승계.
    """
    lines: list[str] = []
    paras = _util.as_list(unit.get("항"))
    if paras:
        for p in paras:
            if not isinstance(p, dict):
                continue
            _push_line(lines, p.get("항내용"))
            for ho in _util.as_list(p.get("호")):
                if not isinstance(ho, dict):
                    continue
                _push_line(lines, ho.get("호내용"))
                for mok in _util.as_list(ho.get("목")):
                    if isinstance(mok, dict):
                        _push_line(lines, mok.get("목내용"))
    else:
        _push_line(lines, _strip_heading(unit.get("조문내용")))
        for ho in _util.as_list(unit.get("호")):
            if not isinstance(ho, dict):
                continue
            _push_line(lines, ho.get("호내용"))
            for mok in _util.as_list(ho.get("목")):
                if isinstance(mok, dict):
                    _push_line(lines, mok.get("목내용"))
    return "\n".join(lines).strip()


def parse_law_articles(body: dict, instrument_id: str) -> list[dict]:
    """법령 본문 응답 → articles row[]. 조문여부=='조문'만, 조문번호가 숫자인 것만.

    반환 row 컬럼(articles 스키마): article_id, instrument_id, article_no,
    article_branch, title, body, effective_on, article_key, content_hash, updated_at.
    """
    now = _util.now_kst_iso()
    out: dict[str, dict] = {}  # article_id 중복 방어
    for unit in _law_units(body):
        if not isinstance(unit, dict):
            continue
        if unit.get("조문여부") != "조문":
            continue
        no = unit.get("조문번호")
        branch = unit.get("조문가지번호")
        label = _article_label(no, branch)
        if label is None:
            continue
        title = unit.get("조문제목")
        title = title.strip() if isinstance(title, str) and title.strip() else None
        header = f"{label}({title})" if title else label
        folded = _law_article_text(unit)
        text = header + ("\n" + folded if folded else "")
        article_id = f"{instrument_id}::{label}"
        out[article_id] = {
            "article_id": article_id,
            "instrument_id": instrument_id,
            "article_no": str(no).strip(),
            "article_branch": (str(branch).strip()
                               if str(branch or "").strip().isdigit() else None),
            "title": title,
            "body": text,
            "effective_on": _digits8(unit.get("조문시행일자")),
            "article_key": (str(unit.get("조문키")).strip()
                            if unit.get("조문키") is not None else None),
            "content_hash": _util.content_hash(text),
            "updated_at": now,
        }
    return list(out.values())


# --------------------------------------------------------------------------- #
# 2. 자치법규 본문 (target=ordin) → ordinance_articles
# --------------------------------------------------------------------------- #
def parse_ordinance_articles(jomun: list[dict], ordinance_id: str) -> list[dict]:
    """LawService.조문.조[] → ordinance_articles row[].

    입력 jomun 은 조[] 리스트(collectors.law.ordin_body 두번째 반환값).
    조문번호는 ['000100','000100'] 처럼 배열/중복 → 첫 원소·oa_id 중복 방어.
    반환 컬럼: oa_id, ordinance_id, article_no, title, body, content_hash, updated_at.
    """
    now = _util.now_kst_iso()
    out: dict[str, dict] = {}
    for jo in _util.as_list(jomun):
        if not isinstance(jo, dict):
            continue
        # 조문여부: 자치법규는 'Y'가 조문. 명시적 'N'/기타(장·절 표제)는 제외.
        flag = jo.get("조문여부")
        if flag not in (None, "Y", "조문"):
            continue
        nums = _util.as_list(jo.get("조문번호"))
        no6 = str(nums[0]).strip() if nums else ""
        if not no6:
            continue
        oa_id = f"{ordinance_id}::{no6}"
        if oa_id in out:  # 배열 중복 방어(첫 등장 유지)
            continue
        title = jo.get("조제목")
        title = title.strip() if isinstance(title, str) and title.strip() else None
        text = jo.get("조내용")
        text = text.strip() if isinstance(text, str) else ""
        out[oa_id] = {
            "oa_id": oa_id,
            "ordinance_id": ordinance_id,
            "article_no": no6,
            "title": title,
            "body": text,
            "content_hash": _util.content_hash(text),
            "updated_at": now,
        }
    return list(out.values())


# --------------------------------------------------------------------------- #
# 3. 행정규칙 본문 (target=admrul) → articles
# --------------------------------------------------------------------------- #
_ADMRUL_BLOCK_RE = re.compile(r"(?:^|\n)\s*(제\s*\d+\s*조(?:\s*의\s*\d+)?(?:\s*\([^\n)]*\))?)")
_ADMRUL_HEAD_RE = re.compile(r"^제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*(?:\(([^\n)]*)\))?\s*")
_ADMRUL_MARK_RE = re.compile(
    r"([^\n])([①-⑳㉑-㉟㊱-㊿])")  # ①②… 앞 개행
_ADMRUL_NUM_RE = re.compile(r"([^\n\d])(\d+\.\s+)")           # "1. " 앞 개행
_ADMRUL_MULTINL_RE = re.compile(r"\n{2,}")


def _admrul_service(body: dict) -> dict:
    """AdmRulService 루트 추출(전체 응답 또는 서비스 dict 수용)."""
    if not isinstance(body, dict):
        return {}
    return body.get("AdmRulService", body) if isinstance(body.get("AdmRulService", body), dict) else {}


def _admrul_content_items(service: dict) -> list[str]:
    content = service.get("조문내용")
    if content is None and isinstance(service.get("조문"), dict):
        content = service["조문"].get("조문내용")
    if isinstance(content, list):
        return [c for c in content if isinstance(c, str)]
    return [content] if isinstance(content, str) else []


def _admrul_format_body(text: str) -> str:
    """줄바꿈 정규화(korea100 formatBody 규율): 항 기호·번호목록 앞 개행 삽입."""
    t = text.strip()
    t = _ADMRUL_MARK_RE.sub(r"\1\n\2", t)
    t = _ADMRUL_NUM_RE.sub(r"\1\n\2", t)
    t = _ADMRUL_MULTINL_RE.sub("\n", t)
    return t


def _admrul_split_blocks(text: str) -> list[str]:
    starts: list[int] = []
    for m in _ADMRUL_BLOCK_RE.finditer(text):
        starts.append(m.start(1))
    blocks: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        blocks.append(text[start:end].strip())
    return blocks


def parse_admrul_articles(body: dict, instrument_id: str) -> list[dict]:
    """AdmRulService.조문내용(통짜/배열 텍스트) → 정규식 '제N조(제목)' 블록 분할 → articles row[].

    조문단위 구조가 아니라 통짜 텍스트이므로 블록 분할로 조문을 복원한다(고시·훈령 편차 대응).
    """
    now = _util.now_kst_iso()
    service = _admrul_service(body)
    eff = _digits8((service.get("행정규칙기본정보") or {}).get("시행일자")) \
        if isinstance(service.get("행정규칙기본정보"), dict) else None
    out: dict[str, dict] = {}
    for item in _admrul_content_items(service):
        for block in _admrul_split_blocks(item):
            m = _ADMRUL_HEAD_RE.match(block)
            if not m:
                continue
            label = _article_label(m.group(1), m.group(2))
            if label is None:
                continue
            title = m.group(3).strip() if m.group(3) and m.group(3).strip() else None
            rest = _admrul_format_body(block[m.end():])
            header = f"{label}({title})" if title else label
            text = header + ("\n" + rest if rest else "")
            article_id = f"{instrument_id}::{label}"
            out[article_id] = {
                "article_id": article_id,
                "instrument_id": instrument_id,
                "article_no": str(int(m.group(1))),
                "article_branch": str(int(m.group(2))) if m.group(2) else None,
                "title": title,
                "body": text,
                "effective_on": eff,
                "article_key": None,
                "content_hash": _util.content_hash(text),
                "updated_at": now,
            }
    return list(out.values())


# --------------------------------------------------------------------------- #
# 4. 저장
# --------------------------------------------------------------------------- #
def save_articles(conn, rows: Iterable[dict], *, table: str) -> dict[str, int]:
    """articles/ordinance_articles upsert(해시가드). PK는 table로 결정.

    table=='ordinance_articles' → pk='oa_id', 그 외 → pk='article_id'.
    무변경(content_hash 동일)은 'unchanged' 로 스킵(무변경=무이벤트).
    """
    pk = "oa_id" if table == "ordinance_articles" else "article_id"
    rows = list(rows)
    if not rows:
        return {"inserted": 0, "updated": 0, "unchanged": 0}
    with _db.tx(conn):
        return _db.upsert_many(conn, table, rows, pk, hash_col="content_hash")


__all__ = [
    "parse_law_articles",
    "parse_ordinance_articles",
    "parse_admrul_articles",
    "save_articles",
]
