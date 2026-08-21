"""policymap.standards — 법령 지식그래프 국제표준 정합 계층.

대상 표준(원문 확인):
  * ELI (European Legislation Identifier) — 온톨로지 OWL 실다운로드로 클래스·속성명 확인.
    http://data.europa.eu/eli/ontology#  (op.europa.eu 배포 eli.owl, 163KB)
    클래스: LegalResource(Work) / LegalExpression(Expression) / Format(Manifestation) /
            LegalResourceSubdivision / Version / InForce / AdministrativeArea ...
    속성  : is_realized_by / realizes / is_embodied_by / embodies / is_part_of / has_part /
            based_on / basis_for / amends / amended_by / repeals / repealed_by /
            consolidates / consolidated_by / changes / changed_by / cites / cited_by /
            in_force / jurisdiction / language / passed_by / version /
            first_date_entry_in_force / date_no_longer_in_force / date_document /
            date_publication / id_local / title / number / version_date  (전부 실파일 확인)
    Pillar 1 식별(URI) / 2 메타데이터(FRBR) / 3 직렬화(RDFa·JSON-LD) / 4 동기화.
    ELI 는 부분 구현을 명시 허용 → 본 모듈은 Pillar 1+2+3 부분 준수를 목표로 한다.

  * Akoma Ntoso Naming Convention v1.0 (OASIS LegalDocML) — IRI 문법 확인.
    Work        : /akn/{country}/{doctype}/{author}/{date}/{number}
    Expression  : {Work}/{language}@{version-date}   (버전 생략 = 현행)
    Manifestation: {Expression}.{format}
    Portion     : {IRI}/~{eId}      Component: {IRI}/!{component}

  * FRBR Work / Expression / Manifestation 2+1 축.

[실측 정정 — 중요]
  선행 조사 권고 P1 은 "work_id 로 ord_id 를 그대로 쓰면 된다"였으나 실DB 조회 결과
  ordinances 159,452행에서 ord_id distinct = 159,452 (완전 1:1)이며, 동일 자치법규의
  서로 다른 판본이 서로 다른 ord_id 를 갖는 사례가 존재한다
  (예: 강진군 상징물 조례 → ord_id 2021627(1993-11-04) / 2250395(2025-02-07)).
  따라서 자치법규 목록 API 의 자치법규ID 는 FRBR Work 식별자로 쓸 수 없다.
  본 모듈은 Work 축을 (author_token, 정규화 자치법규명, 자치법규종류) 결정적 해시로 만든다.

공개 API
--------
  migrate(conn)                         -> dict   # ALTER TABLE + CREATE VIEW (멱등)
  norm_date(s)                          -> str|None
  norm_name(s)                          -> str
  author_token(conn, region_id, org)    -> str
  akn_work_uri / akn_expression_uri / akn_portion_uri / akn_annex_uri
  canonical_law_url(kind, **ids)        -> str|None
  build_work_chains(conn, apply=True)   -> dict   # FRBR Work 축 구성 + version_no
  assign_identifiers(conn, apply=True)  -> dict   # ELI/AKN URI + canonical_url 부여
  validate_temporal(conn, write=True)   -> dict   # 시간 유효성 규칙 T1~T8
  repair_temporal(conn, apply=False)    -> dict   # 안전한 보정만 수행
  in_force_at(conn, on_date, ...)       -> list   # "시점 t 에 유효한 조례 집합"
  provenance(conn, entity_type, id)     -> dict
  jsonld_ordinance(conn, ordinance_id)  -> dict   # ELI JSON-LD
  standards_report(conn)                -> dict
  run_all(conn, apply=True)             -> dict

DB 쓰기는 전부 짧은 배치 트랜잭션(기본 5,000행)으로 나눈다 — 본문 대량수집기와
동시 실행 중이므로 장기 write lock 을 잡지 않기 위함.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import date
from typing import Any, Optional

from .util import now_kst_iso, sha256_hex, today_kst

__all__ = [
    "ELI_NS", "AKN_PREFIX", "ELI_CONTEXT", "VIEW_DDL",
    "migrate", "norm_date", "norm_name", "author_token",
    "akn_work_uri", "akn_expression_uri", "akn_portion_uri", "akn_annex_uri",
    "canonical_law_url", "build_work_chains", "assign_identifiers",
    "validate_temporal", "repair_temporal", "in_force_at", "provenance",
    "jsonld_ordinance", "standards_report", "run_all",
]

# --------------------------------------------------------------------------- #
# 상수
# --------------------------------------------------------------------------- #
ELI_NS = "http://data.europa.eu/eli/ontology#"
AKN_PREFIX = "/akn/kr"
LANG3 = "kor"                       # ISO 639-2/T
DATE_SENTINEL = {"99991231", "9999-12-31"}

BATCH = 5000

# ELI JSON-LD @context (속성명은 eli.owl 실파일에서 확인한 것만 사용)
ELI_CONTEXT: dict[str, Any] = {
    "eli": ELI_NS,
    "owl": "http://www.w3.org/2002/07/owl#",
    "dct": "http://purl.org/dc/terms/",
    "is_realized_by": {"@id": "eli:is_realized_by", "@type": "@id"},
    "realizes": {"@id": "eli:realizes", "@type": "@id"},
    "is_part_of": {"@id": "eli:is_part_of", "@type": "@id"},
    "has_part": {"@id": "eli:has_part", "@type": "@id"},
    "based_on": {"@id": "eli:based_on", "@type": "@id"},
    "basis_for": {"@id": "eli:basis_for", "@type": "@id"},
    "amends": {"@id": "eli:amends", "@type": "@id"},
    "amended_by": {"@id": "eli:amended_by", "@type": "@id"},
    "repeals": {"@id": "eli:repeals", "@type": "@id"},
    "repealed_by": {"@id": "eli:repealed_by", "@type": "@id"},
    "consolidates": {"@id": "eli:consolidates", "@type": "@id"},
    "cites": {"@id": "eli:cites", "@type": "@id"},
    "jurisdiction": {"@id": "eli:jurisdiction", "@type": "@id"},
    "in_force": {"@id": "eli:in_force", "@type": "@id"},
    "language": {"@id": "eli:language", "@type": "@id"},
    "version": {"@id": "eli:version", "@type": "@id"},
    "first_date_entry_in_force": {"@id": "eli:first_date_entry_in_force", "@type": "xsd:date"},
    "date_no_longer_in_force": {"@id": "eli:date_no_longer_in_force", "@type": "xsd:date"},
    "date_document": {"@id": "eli:date_document", "@type": "xsd:date"},
    "date_publication": {"@id": "eli:date_publication", "@type": "xsd:date"},
    "id_local": "eli:id_local",
    "title": "eli:title",
    "number": "eli:number",
    "version_date": {"@id": "eli:version_date", "@type": "xsd:date"},
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}

# ELI in_force 개념(eli.owl 의 InForce 개별자 URI 체계)
_IN_FORCE_URI = {
    "in_force": "http://publications.europa.eu/resource/authority/legal-status/IN_FORCE",
    "repealed": "http://publications.europa.eu/resource/authority/legal-status/REPEALED",
    "superseded": "http://publications.europa.eu/resource/authority/legal-status/REPEALED",
}

# 검증 완료(2026-08-20 실HTTP 200 + <title> 대조)한 공개 영구 URL 패턴
_URL_ORDIN = "https://www.law.go.kr/ordinInfoP.do?ordinSeq={mst}"
_URL_STATUTE = "https://www.law.go.kr/lsInfoP.do?lsiSeq={mst}"
_URL_ADMRUL = "https://www.law.go.kr/admRulLsInfoP.do?admRulSeq={serial}"
# 주의: lod.law.go.kr/resource/{법령ID} 는 실호출 404 → owl:sameAs 미발급(추측 금지).

_AKN_DOCTYPE = {
    "법률": "act", "조약": "act", "헌법": "act",
    "대통령령": "decree", "총리령": "decree", "부령": "decree", "행정안전부령": "decree",
    "대법원규칙": "rule", "감사원규칙": "rule",
    "행정규칙": "doc", "고시·지침": "doc", "표준": "doc",
}


# --------------------------------------------------------------------------- #
# 정규화 유틸
# --------------------------------------------------------------------------- #
def norm_date(s: Optional[str]) -> Optional[str]:
    """'YYYYMMDD' | 'YYYY-MM-DD' | 'YYYY.MM.DD' → ISO 'YYYY-MM-DD'.

    파싱 불가/센티넬('99991231')은 None. 달력상 실재하지 않는 날짜도 None.
    """
    if not s:
        return None
    t = re.sub(r"[^0-9]", "", str(s))
    if not t or t in DATE_SENTINEL:
        return None
    if len(t) != 8:
        return None
    try:
        d = date(int(t[0:4]), int(t[4:6]), int(t[6:8]))
    except ValueError:
        return None
    if d.year < 1800 or d.year > 2200:
        return None
    return d.isoformat()


def norm_name(s: Optional[str]) -> str:
    """자치법규명 정규화(Work 그룹 키). NFKC + 중점 통일 + '(구)' 제거 + 공백 제거."""
    n = unicodedata.normalize("NFKC", s or "")
    # NFKC 는 'ㆍ'(U+318D)를 'ᆞ'(U+119E)로 바꾸므로 정규화 이후 문자까지 함께 통일한다.
    for ch in ("ㆍ", "ᆞ", "・", "･", "‧", "•"):
        n = n.replace(ch, "·")
    n = re.sub(r"[（(]\s*구\s*[)）]", "", n)
    n = re.sub(r"[\s​　]+", "", n)
    return n


def _slug(s: str, n: int = 12) -> str:
    return sha256_hex(s)[:n]


# --------------------------------------------------------------------------- #
# author_token (AKN NC 의 author 세그먼트)
# --------------------------------------------------------------------------- #
_SIDO_CACHE: dict[int, list[tuple[str, str]]] = {}


def _sido_map(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    key = id(conn)
    if key not in _SIDO_CACHE:
        rows = conn.execute(
            "SELECT region_id, name FROM regions WHERE level=1 AND name IS NOT NULL"
        ).fetchall()
        pairs = [(norm_name(r[1]), str(r[0])) for r in rows]
        pairs.sort(key=lambda p: -len(p[0]))     # 최장 접두 우선
        _SIDO_CACHE[key] = pairs
    return _SIDO_CACHE[key]


def author_token(conn: sqlite3.Connection, region_id: Optional[str], org_name: Optional[str]) -> str:
    """AKN author 세그먼트. 지자체는 sig_cd(region_id), 교육청은 'edu-{시도코드}'."""
    if region_id:
        return str(region_id)
    org = norm_name(org_name)
    if not org:
        return "unknown"
    if "교육청" in org:
        for pref, rid in _sido_map(conn):
            if org.startswith(pref):
                return f"edu-{rid}"
        return f"edu-org-{_slug(org, 8)}"
    for pref, rid in _sido_map(conn):
        if org.startswith(pref):
            return f"org-{rid}"
    return f"org-{_slug(org, 8)}"


# --------------------------------------------------------------------------- #
# 식별자 생성 (Akoma Ntoso NC / ELI Pillar 1)
# --------------------------------------------------------------------------- #
def akn_work_uri(author: str, doctype: str, work_date: Optional[str], number: str) -> str:
    """Work IRI = /akn/kr/{doctype}/{author}/{date}/{number}."""
    d = work_date or "0000-00-00"
    return f"{AKN_PREFIX}/{doctype}/{author}/{d}/{number}"


def akn_expression_uri(work_uri: str, version_date: Optional[str], *, current: bool = False) -> str:
    """Expression IRI = {Work}/kor@{version-date}. current=True 이면 버전 생략(현행 별칭)."""
    if current or not version_date:
        return f"{work_uri}/{LANG3}"
    return f"{work_uri}/{LANG3}@{version_date}"


def akn_portion_uri(expression_uri: str, e_id: str) -> str:
    """Portion IRI = {Expression}/~{eId}  (예: art_5)."""
    return f"{expression_uri}/~{e_id}"


def akn_annex_uri(expression_uri: str, component: str) -> str:
    """Component IRI = {Expression}/!{component}  (예: annex_1)."""
    return f"{expression_uri}/!{component}"


def canonical_law_url(kind: str, **ids: Any) -> Optional[str]:
    """국가법령정보센터 공개 영구 URL. OC(API 키)가 포함되지 않은 형태만 반환.

    kind='ordinance' -> mst / 'statute' -> mst / 'admrul' -> serial
    """
    if kind == "ordinance" and ids.get("mst"):
        return _URL_ORDIN.format(mst=ids["mst"])
    if kind == "statute" and ids.get("mst"):
        return _URL_STATUTE.format(mst=ids["mst"])
    if kind == "admrul" and ids.get("serial"):
        return _URL_ADMRUL.format(serial=ids["serial"])
    return None


# --------------------------------------------------------------------------- #
# 마이그레이션
# --------------------------------------------------------------------------- #
_ORD_COLS = [
    ("work_id", "TEXT"), ("work_uri", "TEXT"), ("expression_uri", "TEXT"),
    ("canonical_url", "TEXT"), ("valid_from", "TEXT"), ("valid_to", "TEXT"),
    ("lifecycle", "TEXT"), ("version_no", "INTEGER"),
]
_LI_COLS = list(_ORD_COLS)

_NEW_TABLES = """
CREATE TABLE IF NOT EXISTS ordinance_work (
  work_id TEXT PRIMARY KEY, work_uri TEXT, author_token TEXT, region_id TEXT,
  org_name TEXT, name TEXT, name_norm TEXT, ord_kind TEXT,
  first_enacted_on TEXT, latest_effective_on TEXT, expression_count INTEGER DEFAULT 0,
  lifecycle TEXT, succession_note TEXT, computed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ow_author ON ordinance_work(author_token, name_norm);
CREATE INDEX IF NOT EXISTS ix_ow_count  ON ordinance_work(expression_count);
CREATE INDEX IF NOT EXISTS ix_ow_life   ON ordinance_work(lifecycle);
CREATE TABLE IF NOT EXISTS temporal_audit (
  audit_id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  rule TEXT NOT NULL, severity TEXT NOT NULL, observed TEXT,
  repaired INTEGER DEFAULT 0, repair_action TEXT, checked_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ta_rule   ON temporal_audit(rule, severity);
CREATE INDEX IF NOT EXISTS ix_ta_entity ON temporal_audit(entity_type, entity_id);
"""

_NEW_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_ord_work  ON ordinances(work_id, version_no);
CREATE INDEX IF NOT EXISTS ix_ord_life  ON ordinances(lifecycle);
CREATE INDEX IF NOT EXISTS ix_ord_valid ON ordinances(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS ix_ord_exuri ON ordinances(expression_uri);
CREATE INDEX IF NOT EXISTS ix_li_work   ON legal_instrument(work_id, version_no);
CREATE INDEX IF NOT EXISTS ix_li_valid  ON legal_instrument(valid_from, valid_to);
"""

# 뷰는 schema.sql 이 아니라 여기서 만든다: SQLite 는 CREATE VIEW 시점에 SELECT 를
# 준비(prepare)하므로, ALTER 가 아직 적용되지 않은 기존 DB 에 schema.sql 을
# executescript 하면 'no such column' 으로 전체가 깨진다.
VIEW_DDL = """
DROP VIEW IF EXISTS v_ordinance_expression;
CREATE VIEW v_ordinance_expression AS
SELECT o.ordinance_id, o.work_id, w.work_uri, o.expression_uri, o.version_no,
       o.mst, o.ord_id, o.name, o.ord_kind, o.region_id, o.org_name,
       o.enacted_on, o.effective_on, o.repealed_on, o.rr_cls_cd,
       o.valid_from, o.valid_to, o.lifecycle, o.canonical_url,
       w.expression_count, w.author_token
FROM ordinances o LEFT JOIN ordinance_work w ON w.work_id = o.work_id;

DROP VIEW IF EXISTS v_ordinance_in_force;
CREATE VIEW v_ordinance_in_force AS
SELECT * FROM v_ordinance_expression WHERE lifecycle = 'in_force';

DROP VIEW IF EXISTS v_work_chain;
CREATE VIEW v_work_chain AS
SELECT w.work_id, w.work_uri, w.author_token, w.region_id, w.name, w.ord_kind,
       w.expression_count, w.first_enacted_on, w.latest_effective_on, w.lifecycle,
       w.succession_note
FROM ordinance_work w WHERE w.expression_count > 1;

DROP VIEW IF EXISTS v_provenance;
CREATE VIEW v_provenance AS
SELECT 'ordinance' AS entity_type, o.ordinance_id AS entity_id, o.name AS entity_name,
       'law.go.kr:ordin' AS source, o.canonical_url AS source_url,
       o.as_of_date AS collected_at, o.updated_at AS updated_at,
       COALESCE(v.status, o.verification_status) AS verification_status,
       v.verified_at AS verified_at, v.method AS verify_method,
       o.content_hash AS content_hash
FROM ordinances o LEFT JOIN verification v
  ON v.entity_type='ordinance' AND v.entity_id=o.ordinance_id
UNION ALL
SELECT 'instrument', li.instrument_id, li.name,
       'law.go.kr:' || li.source_type, li.canonical_url,
       li.as_of_date, li.updated_at,
       COALESCE(v.status, li.verification_status), v.verified_at, v.method,
       li.content_hash
FROM legal_instrument li LEFT JOIN verification v
  ON v.entity_type='instrument' AND v.entity_id=li.instrument_id;

DROP VIEW IF EXISTS v_temporal_health;
CREATE VIEW v_temporal_health AS
SELECT rule, severity, COUNT(*) AS n, SUM(repaired) AS repaired
FROM temporal_audit GROUP BY rule, severity;
"""


def _existing_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate(conn: sqlite3.Connection) -> dict[str, Any]:
    """표준정합 스키마 적용(멱등). ALTER TABLE ADD COLUMN + 신규 테이블 + 뷰."""
    added: list[str] = []
    for table, cols in (("ordinances", _ORD_COLS), ("legal_instrument", _LI_COLS)):
        have = _existing_cols(conn, table)
        for name, typ in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                added.append(f"{table}.{name}")
    conn.commit()
    conn.executescript(_NEW_TABLES)
    conn.commit()
    conn.executescript(_NEW_INDEXES)
    conn.commit()
    conn.executescript(VIEW_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key,value) VALUES ('standards_migrated_at',?)",
        (now_kst_iso(),),
    )
    conn.commit()
    return {"columns_added": added, "views": 5, "tables": ["ordinance_work", "temporal_audit"]}


# --------------------------------------------------------------------------- #
# 배치 실행 헬퍼(짧은 트랜잭션)
# --------------------------------------------------------------------------- #
def _batched_write(conn: sqlite3.Connection, sql: str, rows: list[tuple], batch: int = BATCH) -> int:
    n = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(sql, chunk)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        n += len(chunk)
    return n


# --------------------------------------------------------------------------- #
# FRBR Work 축 구성
# --------------------------------------------------------------------------- #
def build_work_chains(conn: sqlite3.Connection, *, apply: bool = True) -> dict[str, Any]:
    """(author_token, 정규화명, 종류) 로 Work 를 만들고 Expression 순번·유효구간을 계산.

    반환: works / multi_expression_works / expressions / lifecycle 분포 등.
    """
    today = today_kst()
    rows = conn.execute(
        "SELECT ordinance_id, mst, ord_id, region_id, org_name, name, ord_kind, "
        "       enacted_on, effective_on, repealed_on, rr_cls_cd "
        "FROM ordinances"
    ).fetchall()

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        at = author_token(conn, r["region_id"], r["org_name"])
        nn = norm_name(r["name"])
        key = (at, nn, r["ord_kind"] or "")
        groups.setdefault(key, []).append({
            "ordinance_id": r["ordinance_id"], "mst": r["mst"], "ord_id": r["ord_id"],
            "region_id": r["region_id"], "org_name": r["org_name"], "name": r["name"],
            "ord_kind": r["ord_kind"], "enacted": norm_date(r["enacted_on"]),
            "effective": norm_date(r["effective_on"]),
            "effective_raw": r["effective_on"],
            "sentinel": re.sub(r"[^0-9]", "", str(r["effective_on"] or "")) in DATE_SENTINEL,
            "repealed": norm_date(r["repealed_on"]),
            "author": at, "name_norm": nn,
        })

    ord_updates: list[tuple] = []
    work_rows: list[tuple] = []
    lifecycles: dict[str, int] = {}
    multi = 0
    now = now_kst_iso()

    for (at, nn, kind), members in groups.items():
        work_id = "ow:" + _slug(f"{at}|{nn}|{kind}", 16)
        # Expression 정렬: 유효시작(없으면 공포일) → 공포일 → mst
        def sort_key(m: dict) -> tuple:
            vf = (None if m["sentinel"] else m["effective"]) or m["enacted"] or "9999-12-31"
            return (vf, m["enacted"] or "9999-12-31", str(m["mst"]))
        members.sort(key=sort_key)

        first_enacted = next((m["enacted"] for m in members if m["enacted"]), None)
        doctype = "ordinance"
        work_number = "w" + _slug(f"{nn}|{kind}", 12)
        wuri = akn_work_uri(at, doctype, first_enacted, work_number)

        for idx, m in enumerate(members, start=1):
            # 시행일 센티넬(99991231)은 '시행일 미정'이므로 공포일로 대체하지 않는다.
            # (대체하면 미시행 조례가 현행으로 잘못 계산된다 — 실측 174건)
            vf = None if m["sentinel"] else (m["effective"] or m["enacted"])
            # valid_to = 다음 Expression 의 valid_from(없으면 폐지일, 없으면 열림)
            nxt = members[idx] if idx < len(members) else None
            vt = m["repealed"]
            if nxt is not None:
                nvf = None if nxt["sentinel"] else (nxt["effective"] or nxt["enacted"])
                if nvf and (vt is None or nvf < vt):
                    vt = nvf
            # lifecycle
            if m["repealed"] and m["repealed"] <= today:
                lc = "repealed"
            elif vt and vt <= today:
                lc = "superseded"
            elif vf is None:
                lc = "undetermined"           # 시행일 미정(99991231) 또는 파싱불가
            elif vf > today:
                lc = "pending"                # 시행예정
            else:
                lc = "in_force"
            lifecycles[lc] = lifecycles.get(lc, 0) + 1
            euri = akn_expression_uri(wuri, vf)
            curl = canonical_law_url("ordinance", mst=m["mst"])
            ord_updates.append((work_id, wuri, euri, curl, vf, vt, lc, idx, m["ordinance_id"]))
            m["_lc"] = lc

        if len(members) > 1:
            multi += 1
        last = members[-1]
        work_rows.append((
            work_id, wuri, at, last["region_id"], last["org_name"], last["name"], nn,
            kind, first_enacted,
            (None if last["sentinel"] else last["effective"]) or last["enacted"], len(members),
            last["_lc"], None, now,
        ))

    stats = {
        "works": len(groups),
        "multi_expression_works": multi,
        "expressions": len(ord_updates),
        "lifecycle": lifecycles,
        "applied": False,
    }
    if not apply:
        return stats

    _batched_write(
        conn,
        "UPDATE ordinances SET work_id=?, work_uri=?, expression_uri=?, canonical_url=?, "
        "valid_from=?, valid_to=?, lifecycle=?, version_no=? WHERE ordinance_id=?",
        ord_updates,
    )
    _batched_write(
        conn,
        "INSERT OR REPLACE INTO ordinance_work(work_id, work_uri, author_token, region_id, "
        "org_name, name, name_norm, ord_kind, first_enacted_on, latest_effective_on, "
        "expression_count, lifecycle, succession_note, computed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        work_rows,
    )
    stats["applied"] = True
    return stats


def build_amendment_edges(conn: sqlite3.Connection, *, apply: bool = True) -> dict[str, Any]:
    """동일 Work 내 연속 Expression 사이에 AMENDED_BY 엣지 생성(ELI eli:amended_by 대응).

    명칭 정규화 기반 그룹이므로 inferred=1 로 표시한다(공식 API 위임 링크가 아님).
    """
    rows = conn.execute(
        "SELECT ordinance_id, work_id, version_no, valid_from, rr_cls_cd "
        "FROM ordinances WHERE work_id IN "
        "(SELECT work_id FROM ordinance_work WHERE expression_count>1) "
        "ORDER BY work_id, version_no"
    ).fetchall()
    edges: list[tuple] = []
    prev = None
    now = now_kst_iso()
    for r in rows:
        if prev is not None and prev["work_id"] == r["work_id"]:
            rel_id = "ir:" + _slug(f"AMENDED_BY|{prev['ordinance_id']}|{r['ordinance_id']}", 20)
            edges.append((
                rel_id, "ordinance", prev["ordinance_id"], "ordinance", r["ordinance_id"],
                "AMENDED_BY", None, None, None, None, None, None,
                r["rr_cls_cd"], r["valid_from"], 1, now,
            ))
        prev = r
    stats = {"amendment_edges": len(edges), "applied": False}
    if not apply or not edges:
        return stats
    _batched_write(
        conn,
        "INSERT OR REPLACE INTO instrument_relations(rel_id, src_kind, src_id, dst_kind, dst_id, "
        "relation, citation_text, citation_type, src_article, priority_rule, priority_basis, scope, "
        "amend_type, effective_on, inferred, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        edges,
    )
    stats["applied"] = True
    return stats


# --------------------------------------------------------------------------- #
# 국가법령 식별자
# --------------------------------------------------------------------------- #
def assign_identifiers(conn: sqlite3.Connection, *, apply: bool = True) -> dict[str, Any]:
    """legal_instrument 에 ELI/AKN 식별자·유효구간 부여(자치법규는 build_work_chains 담당)."""
    today = today_kst()
    rows = conn.execute(
        "SELECT instrument_id, kind, source_type, mst, law_id, admrul_serial, admrul_id, "
        "       treaty_id, enacted_on, effective_on, repealed_on, current_history "
        "FROM legal_instrument"
    ).fetchall()
    ups: list[tuple] = []
    lifecycles: dict[str, int] = {}
    for r in rows:
        doctype = _AKN_DOCTYPE.get(r["kind"], "doc")
        number = r["law_id"] or r["admrul_id"] or r["admrul_serial"] or r["treaty_id"] or r["mst"]
        if not number:
            continue
        enacted = norm_date(r["enacted_on"])
        vf = norm_date(r["effective_on"]) or enacted
        vt = norm_date(r["repealed_on"])
        wuri = akn_work_uri("kr", doctype, enacted, str(number))
        euri = akn_expression_uri(wuri, vf)
        work_id = "lw:" + _slug(f"{doctype}|{number}", 16)
        if vt and vt <= today:
            lc = "repealed"
        elif vf is None:
            lc = "undetermined"
        elif vf > today:
            lc = "pending"
        else:
            lc = "in_force"
        lifecycles[lc] = lifecycles.get(lc, 0) + 1
        if r["source_type"] == "admin-rule":
            curl = canonical_law_url("admrul", serial=r["admrul_serial"])
        else:
            curl = canonical_law_url("statute", mst=r["mst"])
        ups.append((work_id, wuri, euri, curl, vf, vt, lc, 1, r["instrument_id"]))

    stats = {"instruments": len(ups), "lifecycle": lifecycles, "applied": False}
    if not apply:
        return stats
    _batched_write(
        conn,
        "UPDATE legal_instrument SET work_id=?, work_uri=?, expression_uri=?, canonical_url=?, "
        "valid_from=?, valid_to=?, lifecycle=?, version_no=? WHERE instrument_id=?",
        ups,
    )
    stats["applied"] = True
    return stats


# --------------------------------------------------------------------------- #
# 시간 유효성 검사 (T1~T8)
# --------------------------------------------------------------------------- #
TEMPORAL_RULES = {
    "T1_effective_before_promulgation": ("warn", "시행일 < 공포일(소급적용이면 정상, 오타면 오류)"),
    "T2_unparseable_date": ("error", "날짜 파싱 불가(자릿수/달력 위반)"),
    "T3_pending_effective": ("info", "시행일이 오늘 이후 — 현행 아님(시행예정)"),
    "T4_undetermined_effective": ("warn", "시행일 센티넬 99991231 — 시행일 미정"),
    "T5_repealed_before_effective": ("error", "폐지일 < 시행일 — 유효구간 역전"),
    "T6_interval_inversion": ("error", "valid_to <= valid_from — Work 내 판본 순서 붕괴"),
    "T7_region_no_longer_exists": ("warn", "귀속 지자체가 오늘 기준 소멸(폐치·분합) — 승계 미반영"),
    "T8_orphan_region": ("warn", "region_id 결측(교육청 등) — 공간축 미귀속"),
}


def validate_temporal(conn: sqlite3.Connection, *, write: bool = True) -> dict[str, Any]:
    """시간 유효성 규칙 T1~T8 을 실행하고 temporal_audit 에 기록. 보정은 하지 않음."""
    today = today_kst()
    now = now_kst_iso()
    findings: list[tuple] = []
    counts: dict[str, int] = {k: 0 for k in TEMPORAL_RULES}

    def add(etype: str, eid: str, rule: str, observed: dict) -> None:
        sev = TEMPORAL_RULES[rule][0]
        findings.append((
            "ta:" + _slug(f"{rule}|{etype}|{eid}", 20), etype, eid, rule, sev,
            json.dumps(observed, ensure_ascii=False), 0, None, now,
        ))
        counts[rule] += 1

    ords = conn.execute(
        "SELECT ordinance_id, name, region_id, enacted_on, effective_on, repealed_on, "
        "       valid_from, valid_to FROM ordinances"
    ).fetchall()
    # 오늘 기준 소멸 지자체
    dead = {
        r[0]: r[1] for r in conn.execute(
            "SELECT region_id, status FROM regions WHERE status IN ('abolished','merged')"
        ).fetchall()
    }
    for r in ords:
        eid = r["ordinance_id"]
        e_raw, f_raw, p_raw = r["enacted_on"], r["effective_on"], r["repealed_on"]
        e, f, p = norm_date(e_raw), norm_date(f_raw), norm_date(p_raw)
        if f_raw and f is None and re.sub(r"[^0-9]", "", str(f_raw)) not in DATE_SENTINEL:
            add("ordinance", eid, "T2_unparseable_date", {"effective_on": f_raw})
        if e_raw and e is None:
            add("ordinance", eid, "T2_unparseable_date", {"enacted_on": e_raw})
        if f_raw and re.sub(r"[^0-9]", "", str(f_raw)) in DATE_SENTINEL:
            add("ordinance", eid, "T4_undetermined_effective", {"effective_on": f_raw})
        if e and f and f < e:
            add("ordinance", eid, "T1_effective_before_promulgation",
                {"enacted_on": e, "effective_on": f})
        if f and f > today:
            add("ordinance", eid, "T3_pending_effective", {"effective_on": f, "today": today})
        if p and f and p < f:
            add("ordinance", eid, "T5_repealed_before_effective",
                {"effective_on": f, "repealed_on": p})
        vf, vt = r["valid_from"], r["valid_to"]
        if vf and vt and vt <= vf:
            add("ordinance", eid, "T6_interval_inversion", {"valid_from": vf, "valid_to": vt})
        if r["region_id"] and r["region_id"] in dead:
            add("ordinance", eid, "T7_region_no_longer_exists",
                {"region_id": r["region_id"], "region_status": dead[r["region_id"]]})
        if not r["region_id"]:
            add("ordinance", eid, "T8_orphan_region", {"region_id": None})

    for r in conn.execute(
        "SELECT instrument_id, enacted_on, effective_on, repealed_on, valid_from, valid_to "
        "FROM legal_instrument"
    ).fetchall():
        eid = r["instrument_id"]
        e, f, p = norm_date(r["enacted_on"]), norm_date(r["effective_on"]), norm_date(r["repealed_on"])
        if r["effective_on"] and f is None:
            add("instrument", eid, "T2_unparseable_date", {"effective_on": r["effective_on"]})
        if e and f and f < e:
            add("instrument", eid, "T1_effective_before_promulgation",
                {"enacted_on": e, "effective_on": f})
        if f and f > today:
            add("instrument", eid, "T3_pending_effective", {"effective_on": f})
        if p and f and p < f:
            add("instrument", eid, "T5_repealed_before_effective", {"effective_on": f, "repealed_on": p})

    if write and findings:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM temporal_audit WHERE repaired=0")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        _batched_write(
            conn,
            "INSERT OR REPLACE INTO temporal_audit(audit_id, entity_type, entity_id, rule, "
            "severity, observed, repaired, repair_action, checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
            findings,
        )
    return {"total": len(findings), "by_rule": counts, "written": bool(write and findings)}


def repair_temporal(conn: sqlite3.Connection, *, apply: bool = False) -> dict[str, Any]:
    """안전한 보정만 수행.

    R1 (T2 파싱불가): effective_on 이 깨진 행의 valid_from 을 공포일로 대체.
                      ※ 센티넬(99991231) 행은 대상에서 제외한다 — 공포일로 메우면
                        '시행일 미정' 조례가 현행으로 잘못 계산된다(실측 174건).
    R2 (T4 센티넬)  : 99991231 은 valid_from NULL + lifecycle='undetermined' 로 유지(보정 안 함).
    R3 (T7 소멸지자체): ordinances.succession_status 를 '이관'(승계대상)으로 표시하고
                        ordinance_work.succession_note 에 승계 사유 기록.
    T1(소급적용)·T5 는 원천값이 사실일 수 있으므로 자동 보정하지 않는다(감사 기록만).
    """
    plan = {"R1_fallback_valid_from": 0, "R2_sentinel_marked": 0, "R3_succession_marked": 0}

    r1 = conn.execute(
        "SELECT ordinance_id, enacted_on FROM ordinances "
        "WHERE (valid_from IS NULL OR valid_from='') AND enacted_on IS NOT NULL "
        "AND COALESCE(lifecycle,'') <> 'undetermined'"
    ).fetchall()
    r1_rows = []
    for r in r1:
        e = norm_date(r["enacted_on"])
        if e:
            r1_rows.append((e, r["ordinance_id"]))
    plan["R1_fallback_valid_from"] = len(r1_rows)

    r2 = conn.execute(
        "SELECT COUNT(*) FROM ordinances WHERE lifecycle='undetermined'"
    ).fetchone()[0]
    plan["R2_sentinel_marked"] = int(r2)

    succ = {
        r["old_region_id"]: (r["new_region_id"], r["succession_type"], r["effective_date"])
        for r in conn.execute("SELECT * FROM region_succession").fetchall()
    }
    dead_rows = conn.execute(
        "SELECT o.ordinance_id, o.region_id FROM ordinances o JOIN regions r "
        "ON r.region_id=o.region_id WHERE r.status IN ('abolished','merged')"
    ).fetchall()
    r3_rows = [("이관", r["ordinance_id"]) for r in dead_rows]
    plan["R3_succession_marked"] = len(r3_rows)
    plan["applied"] = False
    if not apply:
        return plan

    if r1_rows:
        _batched_write(conn, "UPDATE ordinances SET valid_from=? WHERE ordinance_id=?", r1_rows)
    if r3_rows:
        _batched_write(conn, "UPDATE ordinances SET succession_status=? WHERE ordinance_id=?", r3_rows)
        wrows = []
        for old, (new, typ, eff) in succ.items():
            wrows.append((f"{old}→{new} {typ} ({eff})", old))
        if wrows:
            _batched_write(
                conn,
                "UPDATE ordinance_work SET succession_note=?, computed_at=datetime('now','localtime') "
                "WHERE region_id=?",
                wrows,
            )
    conn.execute("UPDATE temporal_audit SET repaired=1, repair_action='auto' "
                 "WHERE rule IN ('T2_unparseable_date','T7_region_no_longer_exists')")
    conn.commit()
    plan["applied"] = True
    return plan


# --------------------------------------------------------------------------- #
# 시점 질의
# --------------------------------------------------------------------------- #
def in_force_at(
    conn: sqlite3.Connection,
    on_date: str,
    *,
    region_id: Optional[str] = None,
    ord_kind: Optional[str] = None,
    name_like: Optional[str] = None,
    limit: int = 100,
    count_only: bool = False,
) -> Any:
    """시점 t 에 유효한 자치법규 집합.

    유효 조건: valid_from <= t AND (valid_to IS NULL OR t < valid_to)
    (Koniaris et al. 2015 식 '유효구간 반개구간' 규약: 시작 포함, 종료 배타)
    """
    t = norm_date(on_date)
    if t is None:
        raise ValueError(f"invalid date: {on_date!r}")
    where = [
        "valid_from IS NOT NULL",
        "COALESCE(lifecycle,'') <> 'undetermined'",   # 시행일 미정(99991231)은 유효집합에서 배제
        "valid_from <= ?",
        "(valid_to IS NULL OR ? < valid_to)",
    ]
    params: list[Any] = [t, t]
    if region_id:
        where.append("region_id = ?")
        params.append(region_id)
    if ord_kind:
        where.append("ord_kind = ?")
        params.append(ord_kind)
    if name_like:
        where.append("name LIKE ?")
        params.append(f"%{name_like}%")
    w = " AND ".join(where)
    if count_only:
        return conn.execute(f"SELECT COUNT(*) FROM ordinances WHERE {w}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT ordinance_id, name, ord_kind, region_id, org_name, valid_from, valid_to, "
        f"lifecycle, expression_uri, canonical_url FROM ordinances WHERE {w} "
        f"ORDER BY valid_from DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #
def provenance(conn: sqlite3.Connection, entity_type: str, entity_id: str) -> Optional[dict]:
    """표준 provenance 조회(출처·수집시각·검증상태·워터마크)."""
    row = conn.execute(
        "SELECT * FROM v_provenance WHERE entity_type=? AND entity_id=?",
        (entity_type, entity_id),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    src = (out.get("source") or "").split(":")[-1]
    wm = conn.execute(
        "SELECT source, scope, status, last_success, rows_seen FROM watermarks "
        "WHERE source LIKE ? ORDER BY last_success DESC LIMIT 1",
        (f"{src}%",),
    ).fetchone()
    out["watermark"] = dict(wm) if wm else None
    return out


# --------------------------------------------------------------------------- #
# JSON-LD (ELI Pillar 3)
# --------------------------------------------------------------------------- #
def jsonld_ordinance(conn: sqlite3.Connection, ordinance_id: str) -> Optional[dict]:
    """자치법규 1건을 ELI JSON-LD(Work + Expression)로 직렬화."""
    r = conn.execute(
        "SELECT * FROM v_ordinance_expression WHERE ordinance_id=?", (ordinance_id,)
    ).fetchone()
    if r is None:
        return None
    r = dict(r)
    work_uri = r.get("work_uri") or ""
    expr_uri = r.get("expression_uri") or ""
    expressions = [
        dict(x) for x in conn.execute(
            "SELECT expression_uri, valid_from, valid_to, lifecycle, version_no "
            "FROM ordinances WHERE work_id=? ORDER BY version_no", (r.get("work_id"),)
        ).fetchall()
    ]
    based_on = [
        d["parent_id"] for d in conn.execute(
            "SELECT parent_id FROM delegations WHERE child_kind='ordinance' AND child_id=? LIMIT 20",
            (ordinance_id,),
        ).fetchall()
    ]
    doc: dict[str, Any] = {
        "@context": ELI_CONTEXT,
        "@id": work_uri,
        "@type": "eli:LegalResource",
        "title": r.get("name"),
        "id_local": r.get("ord_id"),
        "number": r.get("mst"),
        # jurisdiction 은 외부 게이트웨이를 추측하지 않고 우리 네임스페이스의
        # 행정구역 IRI(법정동코드 기반)를 쓴다. 실재하지 않는 URI 발급 금지.
        "jurisdiction": (f"{AKN_PREFIX}/region/{r['region_id']}" if r.get("region_id") else None),
        "date_document": norm_date(r.get("enacted_on")),      # 공포일(ISO 정규화)
        "date_publication": norm_date(r.get("enacted_on")),
        "is_realized_by": [
            {
                "@id": e["expression_uri"],
                "@type": "eli:LegalExpression",
                "language": "http://publications.europa.eu/resource/authority/language/KOR",
                "version_date": e["valid_from"],
                "first_date_entry_in_force": e["valid_from"],
                "date_no_longer_in_force": e["valid_to"],
                "in_force": _IN_FORCE_URI.get(e["lifecycle"]),
                "realizes": work_uri,
                # 같은 Work 의 다음 판본이 이 판본을 개정한다(ELI eli:amended_by).
                "amended_by": next(
                    (x["expression_uri"] for x in expressions
                     if x["version_no"] == e["version_no"] + 1), None),
            }
            for e in expressions
        ],
        "based_on": based_on or None,
        "policymap:canonical_url": r.get("canonical_url"),
        "policymap:lifecycle": r.get("lifecycle"),
        "policymap:current_expression": expr_uri,
    }
    doc["is_realized_by"] = [
        {k: v for k, v in e.items() if v is not None} for e in doc["is_realized_by"]
    ]
    return {k: v for k, v in doc.items() if v is not None}


# --------------------------------------------------------------------------- #
# 리포트 / 오케스트레이션
# --------------------------------------------------------------------------- #
def standards_report(conn: sqlite3.Connection) -> dict[str, Any]:
    q = lambda s, p=(): conn.execute(s, p).fetchone()[0]   # noqa: E731
    rep: dict[str, Any] = {
        "ordinances": q("SELECT COUNT(*) FROM ordinances"),
        "ordinances_with_work": q("SELECT COUNT(*) FROM ordinances WHERE work_id IS NOT NULL"),
        "ordinances_with_expression_uri":
            q("SELECT COUNT(*) FROM ordinances WHERE expression_uri IS NOT NULL"),
        "ordinances_with_canonical_url":
            q("SELECT COUNT(*) FROM ordinances WHERE canonical_url IS NOT NULL"),
        "ordinances_with_valid_from":
            q("SELECT COUNT(*) FROM ordinances WHERE valid_from IS NOT NULL"),
        "works": q("SELECT COUNT(*) FROM ordinance_work"),
        "works_multi_expression":
            q("SELECT COUNT(*) FROM ordinance_work WHERE expression_count>1"),
        "instruments_with_expression_uri":
            q("SELECT COUNT(*) FROM legal_instrument WHERE expression_uri IS NOT NULL"),
        "amendment_edges":
            q("SELECT COUNT(*) FROM instrument_relations WHERE relation='AMENDED_BY'"),
        "temporal_findings": q("SELECT COUNT(*) FROM temporal_audit"),
    }
    rep["lifecycle"] = {
        r[0]: r[1] for r in conn.execute(
            "SELECT COALESCE(lifecycle,'(null)'), COUNT(*) FROM ordinances GROUP BY 1"
        ).fetchall()
    }
    rep["temporal_by_rule"] = {
        r[0]: r[1] for r in conn.execute(
            "SELECT rule, COUNT(*) FROM temporal_audit GROUP BY rule ORDER BY 2 DESC"
        ).fetchall()
    }
    return rep


def run_all(conn: sqlite3.Connection, *, apply: bool = True, repair: bool = True) -> dict[str, Any]:
    """마이그레이션 → Work 축 → 국가법령 식별자 → 개정엣지 → 시간감사 → 보정 → 리포트."""
    out: dict[str, Any] = {}
    out["migrate"] = migrate(conn)
    out["work_chains"] = build_work_chains(conn, apply=apply)
    out["instruments"] = assign_identifiers(conn, apply=apply)
    out["amendment_edges"] = build_amendment_edges(conn, apply=apply)
    out["temporal"] = validate_temporal(conn, write=apply)
    out["repair"] = repair_temporal(conn, apply=apply and repair)
    out["report"] = standards_report(conn)
    return out


if __name__ == "__main__":                                   # pragma: no cover
    import argparse

    from . import db as _db

    ap = argparse.ArgumentParser(description="policymap 표준정합(ELI/AKN/FRBR) 실행")
    ap.add_argument("--db", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-repair", action="store_true")
    a = ap.parse_args()
    conn = _db.connect(a.db)
    res = run_all(conn, apply=not a.dry_run, repair=not a.no_repair)
    print(json.dumps(res, ensure_ascii=False, indent=2))
