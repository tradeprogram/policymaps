"""policymap.seed_korea100 — korea100 로컬 데이터로 기반 시드.

`F:/policy_maps/external/korea100/web/data` 의 두 산출물을 읽어 국가법령 백본을
DB 에 적재한다 → 실키 없이도 end-to-end 데모(그래프 빌드·MCP·export)가 가능하다.

입력(실측 구조):
  * institutions/*.json (578개): verification.sources[] 에
      statute   → {kind, sourceType, officialName, lawId, mst, promulgatedOn, effectiveOn, officialUrl}
      admin-rule→ {kind, sourceType, officialName, adminRuleId, adminRuleSerial, promulgatedOn, org, issueNo, officialUrl}
      treaty    → {kind, sourceType, officialName, treatyId, treatyNumber, effectiveOn, officialUrl}
    verification.status ∈ {source-linked, article-verified, needs-review},
    verification.articleVerification = {citationEntries, explicitCitationEntries, articleReferences,
                                        verifiedReferences, missingReferences, uncheckableReferences}
  * article-text-registry.json:
      articles = { "statute:<mst>::제N조[의M]" | "admin-rule:<serial>::제N조[의M]":
                   {article, title, text, effectiveOn, checkedAt} }  (실측 4,427건, msts ⊂ sources)

적재 대상 테이블: instrument_kind → legal_instrument → articles → verification.
(FK 순서 준수: instrument_kind 먼저, 그다음 legal_instrument, 그다음 articles.)

원문 미러링 규율: 조문 텍스트(body)는 저장하되 official_url 로 원문을 링크한다.
파싱·변환은 전부 순수함수(아래 §1)로 분리해 픽스처 테스트가 가능하다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

from . import config as _config
from . import db as _db
from . import util as _util

log = _util.get_logger("policymap.seed")

# korea100 로컬 데이터 기본 경로: F:/policy_maps/external/korea100/web/data
DEFAULT_DATA_DIR = _config.SYSTEM_ROOT.parent / "external" / "korea100" / "web" / "data"

_ARTICLE_KEY_RE = re.compile(r"^제(\d+)조(?:의(\d+))?")

# prefix/sourceType → instrument_id 접두(legal_instrument.instrument_id 규약)
_ID_PREFIX = {"statute": "statute", "admin-rule": "admrul", "treaty": "treaty"}

# 검증 상태 우선순위(best-of 병합)
_STATUS_RANK = {"needs-review": 1, "source-linked": 2, "article-verified": 3}


# =========================================================================== #
# 0. instrument_kind 통제어휘 메타데이터
#    national_tier: 0 헌법 / 1 법률·조약 / 2 대통령령·헌법기관규칙 / 3 총리령·부령 / 4 행정규칙
# =========================================================================== #
# (source_type, national_tier, local_tier, tier_disputed, api_target, api_knd)
_KIND_META: dict[str, tuple] = {
    "헌법":         ("constitution", 0, None, 0, None, None),
    "법률":         ("statute", 1, None, 0, "law", None),
    "조약":         ("treaty", 1, None, 1, None, None),      # 서열 학설대립
    "대통령령":     ("statute", 2, None, 0, "law", None),
    "대법원규칙":   ("statute", 2, None, 1, "law", None),    # 헌법기관규칙
    "감사원규칙":   ("statute", 2, None, 1, "law", None),
    "헌법재판소규칙": ("statute", 2, None, 1, "law", None),
    "국회규칙":     ("statute", 2, None, 1, "law", None),
    "중앙선거관리위원회규칙": ("statute", 2, None, 1, "law", None),
    "총리령":       ("statute", 3, None, 0, "law", None),
    "부령":         ("statute", 3, None, 0, "law", None),
    "행정안전부령": ("statute", 3, None, 0, "law", None),
    "행정규칙":     ("admin-rule", 4, None, 0, "admrul", None),
    "고시·지침":    ("admin-rule", 4, None, 0, "admrul", None),
    "훈령":         ("admin-rule", 4, None, 0, "admrul", "1"),
    "예규":         ("admin-rule", 4, None, 0, "admrul", "2"),
    "고시":         ("admin-rule", 4, None, 0, "admrul", "3"),
    "조례":         ("ordinance", None, "L1", 0, "ordin", "30001"),
    "규칙":         ("ordinance", None, "L2", 0, "ordin", "30002"),
    "교육규칙":     ("ordinance", None, "L2", 0, "ordin", None),
    "의회규칙":     ("ordinance", None, "L1", 0, "ordin", None),
    "표준":         ("standard", 4, None, 0, None, None),
    "기타":         ("기타", None, None, 0, None, None),
}


def kind_metadata(kind: Optional[str]) -> dict:
    """kind → instrument_kind 메타. 미등록 kind 는 '기타' 폴백(source_type 은 호출부 override)."""
    meta = _KIND_META.get(kind or "기타") or _KIND_META["기타"]
    return {
        "source_type": meta[0],
        "national_tier": meta[1],
        "local_tier": meta[2],
        "tier_disputed": meta[3],
        "api_target": meta[4],
        "api_knd": meta[5],
    }


def kind_row(kind: str) -> dict:
    """instrument_kind upsert row."""
    m = kind_metadata(kind)
    return {
        "kind": kind,
        "source_type": m["source_type"],
        "national_tier": m["national_tier"],
        "local_tier": m["local_tier"],
        "tier_disputed": m["tier_disputed"],
        "api_target": m["api_target"],
        "api_knd": m["api_knd"],
        "note": "korea100 seed",
    }


# =========================================================================== #
# 1. 순수 파싱/변환 함수 (DB·IO 없음 — 픽스처 테스트 가능)
# =========================================================================== #
def parse_article_key(key: str) -> tuple[str, str, str, Optional[str]]:
    """'statute:188376::제10조의2' → ('statute','188376','10','2').

    'admin-rule:2100000207982::제3조' → ('admin-rule','2100000207982','3',None).
    반환 (prefix, ident, article_no, article_branch|None).
    """
    left, _, tail = key.partition("::")
    prefix, _, ident = left.partition(":")
    m = _ARTICLE_KEY_RE.match(tail or "")
    if not m:
        return prefix, ident, "", None
    return prefix, ident, m.group(1), m.group(2)


def instrument_id_for(prefix_or_source_type: str, ident: str) -> str:
    """prefix/sourceType + 원천식별자 → 정규 instrument_id.

    statute→'statute:{mst}', admin-rule→'admrul:{serial}', treaty→'treaty:{id}'.
    """
    p = _ID_PREFIX.get(prefix_or_source_type, prefix_or_source_type)
    return f"{p}:{ident}"


def source_to_instrument_row(source: dict, *, as_of_date: Optional[str] = None) -> Optional[dict]:
    """institution verification.sources[] 항목 → legal_instrument row(또는 None)."""
    st = source.get("sourceType")
    name = source.get("officialName") or source.get("law")
    url = source.get("officialUrl")
    kind = source.get("kind")
    if st == "statute":
        mst = str(source.get("mst") or "").strip()
        if not mst:
            return None
        row = {
            "instrument_id": instrument_id_for("statute", mst),
            "kind": kind or "법률",
            "source_type": "statute",
            "mst": mst,
            "law_id": source.get("lawId"),
            "enacted_on": source.get("promulgatedOn"),
            "effective_on": source.get("effectiveOn"),
        }
    elif st == "admin-rule":
        serial = str(source.get("adminRuleSerial") or "").strip()
        if not serial:
            return None
        row = {
            "instrument_id": instrument_id_for("admin-rule", serial),
            "kind": kind or "행정규칙",
            "source_type": "admin-rule",
            "admrul_serial": serial,
            "admrul_id": source.get("adminRuleId"),
            "enacted_on": source.get("promulgatedOn"),
            "competent_authority": source.get("org"),
            "promulgation_no": source.get("issueNo"),
        }
    elif st == "treaty":
        tid = str(source.get("treatyId") or "").strip()
        if not tid:
            return None
        row = {
            "instrument_id": instrument_id_for("treaty", tid),
            "kind": kind or "조약",
            "source_type": "treaty",
            "treaty_id": tid,
            "treaty_number": source.get("treatyNumber"),
            "effective_on": source.get("effectiveOn"),
        }
    else:
        return None

    meta = kind_metadata(row["kind"])
    row["name"] = name or f"(미상 {row['instrument_id']})"
    row["national_tier"] = meta["national_tier"]
    row["tier_disputed"] = meta["tier_disputed"]
    row["official_url"] = url
    row["current_history"] = "현행"
    row["status"] = "active"
    row["as_of_date"] = as_of_date
    row["updated_at"] = as_of_date
    return row


def minimal_instrument_row(prefix: str, ident: str, entry: dict,
                           *, as_of_date: Optional[str] = None) -> dict:
    """sources 에 없던 부모(방어). 레지스트리 조문만으로 최소 instrument 생성."""
    st = {"statute": "statute", "admin-rule": "admin-rule",
          "treaty": "treaty"}.get(prefix, "statute")
    kind = "행정규칙" if prefix == "admin-rule" else ("조약" if prefix == "treaty" else "법률")
    iid = instrument_id_for(prefix, ident)
    meta = kind_metadata(kind)
    row = {
        "instrument_id": iid,
        "kind": kind,
        "source_type": st,
        "name": f"(레지스트리 {iid})",
        "national_tier": meta["national_tier"],
        "tier_disputed": meta["tier_disputed"],
        "status": "active",
        "current_history": "현행",
        "verification_status": "needs-review",
        "as_of_date": as_of_date,
        "updated_at": as_of_date,
    }
    if prefix == "statute":
        row["mst"] = ident
    elif prefix == "admin-rule":
        row["admrul_serial"] = ident
    elif prefix == "treaty":
        row["treaty_id"] = ident
    return row


def article_entry_to_row(key: str, entry: dict, *, as_of_date: Optional[str] = None) -> dict:
    """레지스트리 articles 항목 → articles row. article_id 는 instrument_id 규약으로 재구성."""
    prefix, ident, no, branch = parse_article_key(key)
    iid = instrument_id_for(prefix, ident)
    article_id = f"{iid}::제{no}조" + (f"의{branch}" if branch else "")
    text = entry.get("text") or ""
    return {
        "article_id": article_id,
        "instrument_id": iid,
        "article_no": no or "0",
        "article_branch": branch,
        "title": entry.get("title"),
        "body": text,
        "effective_on": entry.get("effectiveOn"),
        "article_key": key,
        "content_hash": _util.content_hash(text),
        "updated_at": as_of_date,
    }


def better_status(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """검증 상태 best-of(article-verified > source-linked > needs-review)."""
    ra = _STATUS_RANK.get(a or "", 0)
    rb = _STATUS_RANK.get(b or "", 0)
    if ra == 0 and rb == 0:
        return a or b
    return a if ra >= rb else b


def merge_instrument(prev: Optional[dict], new: dict) -> dict:
    """동일 instrument 가 여러 institution 에 등장할 때 필드 병합(비어있는 값만 채움)."""
    if prev is None:
        return dict(new)
    merged = dict(prev)
    for k, v in new.items():
        if v not in (None, "") and merged.get(k) in (None, ""):
            merged[k] = v
    # 이름/URL 은 더 긴(정보량 많은) 쪽 유지
    if new.get("name") and len(str(new["name"])) > len(str(merged.get("name") or "")):
        merged["name"] = new["name"]
    return merged


def institution_verification_row(inst: dict) -> Optional[dict]:
    """institution 단위 verification row(articleVerification 카운터 보존)."""
    slug = inst.get("slug")
    if not slug:
        return None
    v = inst.get("verification") or {}
    av = v.get("articleVerification") or {}
    notes = {"institution": inst.get("name"), "category": inst.get("category")}
    if v.get("unresolved"):
        notes["unresolved"] = v.get("unresolved")
    return {
        "entity_type": "institution",
        "entity_id": slug,
        "status": v.get("status") or "needs-review",
        "verified_at": v.get("verifiedAt"),
        "method": v.get("method"),
        "scope": v.get("scope"),
        "notes": json.dumps(notes, ensure_ascii=False),
        "citation_entries": av.get("citationEntries"),
        "explicit_citation_entries": av.get("explicitCitationEntries"),
        "article_references": av.get("articleReferences"),
        "verified_references": av.get("verifiedReferences"),
        "missing_references": av.get("missingReferences"),
        "uncheckable_references": av.get("uncheckableReferences"),
    }


def instrument_verification_row(instrument_id: str, status: Optional[str],
                                *, as_of_date: Optional[str] = None) -> dict:
    """instrument 단위 verification row."""
    return {
        "entity_type": "instrument",
        "entity_id": instrument_id,
        "status": status or "needs-review",
        "verified_at": as_of_date,
        "method": "korea100 seed(Open API 출처 대조)",
        "scope": "source-linked seed",
    }


# =========================================================================== #
# 2. IO — 로컬 파일 로드
# =========================================================================== #
def load_institutions(inst_dir: Path) -> list[dict]:
    """institutions/*.json 로드(파싱 실패 파일은 스킵)."""
    out: list[dict] = []
    if not inst_dir.exists():
        return out
    for fp in sorted(inst_dir.glob("*.json")):
        try:
            out.append(json.loads(fp.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("institution 파싱 실패 %s: %s", fp.name, exc)
    return out


def load_registry(path: Path) -> dict:
    """article-text-registry.json 로드. 실패 시 빈 구조."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("레지스트리 로드 실패 %s: %s", path, exc)
        return {"articles": {}, "institutions": {}}


# =========================================================================== #
# 3. 시드 오케스트레이션
# =========================================================================== #
def build_seed_rows(institutions: list[dict], registry: dict,
                    *, as_of_date: Optional[str] = None) -> dict:
    """institutions + registry → 적재용 row 묶음(순수 조립; DB 없음).

    반환 {instruments, articles, verification, kinds}.
    """
    instruments: dict[str, dict] = {}
    status_by_iid: dict[str, Optional[str]] = {}
    institution_verif: list[dict] = []

    # (a) institution sources → instrument + institution 검증
    for inst in institutions:
        v = inst.get("verification") or {}
        inst_status = v.get("status")
        for src in (v.get("sources") or []):
            row = source_to_instrument_row(src, as_of_date=as_of_date)
            if not row:
                continue
            iid = row["instrument_id"]
            instruments[iid] = merge_instrument(instruments.get(iid), row)
            status_by_iid[iid] = better_status(status_by_iid.get(iid), inst_status)
        iv = institution_verification_row(inst)
        if iv:
            institution_verif.append(iv)

    # (b) 레지스트리 조문 → articles(부모 없으면 최소 instrument 생성)
    article_rows: list[dict] = []
    for key, entry in (registry.get("articles") or {}).items():
        prefix, ident, no, _branch = parse_article_key(key)
        if not ident or not no:
            continue
        iid = instrument_id_for(prefix, ident)
        if iid not in instruments:
            instruments[iid] = minimal_instrument_row(prefix, ident, entry,
                                                      as_of_date=as_of_date)
            status_by_iid.setdefault(iid, "needs-review")
        article_rows.append(article_entry_to_row(key, entry, as_of_date=as_of_date))

    # (c) instrument 에 best-of 검증상태 반영 + instrument 검증행
    instrument_verif: list[dict] = []
    for iid, row in instruments.items():
        st = status_by_iid.get(iid) or row.get("verification_status") or "needs-review"
        row["verification_status"] = st
        instrument_verif.append(instrument_verification_row(iid, st, as_of_date=as_of_date))

    # (d) 필요한 kind 통제어휘(기본 어휘 + 실제 등장 kind)
    needed = {r["kind"] for r in instruments.values() if r.get("kind")}
    kinds = sorted(set(_KIND_META.keys()) | needed)

    return {
        "instruments": list(instruments.values()),
        "articles": article_rows,
        "verification": instrument_verif + institution_verif,
        "kinds": kinds,
    }


def seed(conn=None, data_dir: Optional[str | Path] = None, *,
         run_id: Optional[str] = None) -> dict:
    """korea100 로컬 데이터를 DB 에 시드. 반환 = 적재 카운트 요약.

    conn 미지정 시 자체 연결 생성·종료. 항상 init_db(멱등)로 테이블을 보장한다.
    """
    as_of = _util.today_kst()
    ddir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    own = conn is None
    if own:
        conn = _db.connect()
    _db.init_db(conn)  # 멱등

    institutions = load_institutions(ddir / "institutions")
    registry = load_registry(ddir / "article-text-registry.json")
    if not institutions and not registry.get("articles"):
        log.warning("시드 데이터 없음: %s", ddir)

    rows = build_seed_rows(institutions, registry, as_of_date=as_of)

    counts: dict[str, Any] = {
        "data_dir": str(ddir),
        "institutions_read": len(institutions),
        "registry_articles": len(registry.get("articles") or {}),
    }
    try:
        with _db.tx(conn):
            # FK 순서: instrument_kind → legal_instrument → articles
            for kind in rows["kinds"]:
                _db.upsert(conn, "instrument_kind", kind_row(kind), "kind")
            counts["kinds"] = len(rows["kinds"])
            counts["instruments"] = _db.upsert_many(
                conn, "legal_instrument", rows["instruments"], "instrument_id")
            counts["articles"] = _db.upsert_many(
                conn, "articles", rows["articles"], "article_id",
                hash_col="content_hash")
            counts["verification"] = _db.upsert_many(
                conn, "verification", rows["verification"],
                ("entity_type", "entity_id"))
    finally:
        if own:
            conn.close()

    log.info("시드 완료: instruments=%s articles=%s verification=%s (from %s)",
             counts.get("instruments"), counts.get("articles"),
             counts.get("verification"), ddir)
    return counts


# =========================================================================== #
# 4. CLI
# =========================================================================== #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="policymap.seed_korea100",
        description="korea100 로컬 데이터(institutions + article-text-registry)로 "
                    "국가법령 백본을 시드한다(키 불필요).")
    ap.add_argument("--data-dir", dest="data_dir",
                    help=f"korea100 web/data 경로(기본: {DEFAULT_DATA_DIR})")
    ap.add_argument("--db", help="SQLite 경로 override")
    args = ap.parse_args(argv)

    cfg = _config.get_config()
    if args.db:
        cfg.db_path = args.db
    result = seed(data_dir=args.data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
