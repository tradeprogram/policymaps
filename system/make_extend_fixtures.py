#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""실 DB → **미반영 자산** 노출용 확장 fixture 생성.

make_nationwide.py 가 전국 243곳 shard(격차·유사·실효성·확산·표결·검색)를 만들었지만,
DB 에는 아직 화면 어디에도 붙지 않은 자산이 남아 있다. 실측 대조 결과 아래가 비어 있다.

    articles 86,745            → 상위법 조문 열람 화면 없음
    ordinances(폐지) 40,406    → gap 경고에만 쓰이고 생애주기 화면 없음
    verification 1,205         → 검증 공시 페이지 없음
    temporal_audit 7,060       → 미노출
    delegations 421,627        → 검증상태(article-verified/missing/unverifiable) 미노출
    region_succession 17       → 지자체 승계 미표시
    ordinance_articles 2.36M   → 사전계산 질의 5건뿐(카테고리 C01~C14 미커버)
    votes 57,178 / bills 19,847→ 표결 shard 15건뿐

이 스크립트는 그 구멍만 채운다. **기존 생성기의 봉투·살균·원자적 쓰기를 그대로 재사용**한다.

    make_gap_fixtures.envelope        응답 봉투(data/as_of_date/stale/disclaimer)
    make_more_fixtures._sanitize_keys API 키 살균 (RAG 인덱스가 살균 전 URL 보유 → 필수)
    make_nationwide.write_json        살균+원자적 쓰기, existing/human 유틸
    mcp_server.server.Server._tool_*  검색·표결은 MCP 엔진 그대로 호출

출력(<out>/api/):
    search/{slug}.json          C01~C14 대표 질의 사전계산 (기존 5건 + 33건)
    search_index.json           질의 카탈로그(슬러그·카테고리·결과수)
    votes/{bill_no}.json        표결 의안 (표결수 상위 + 최근)
    votes_index.json            의안 카탈로그
    lifecycle/{sig_cd}.json     지자체별 제정→개정→폐지 생애주기
    lifecycle_index.json        전국 곡선 + 최다 폐지 정책 TOP + 일괄폐지 코호트
    statute/{slug}.json         위임 참조 상위 법령의 조문 목록 + 근거 조례 수
    statute_index.json          법령 카탈로그
    verification/summary.json   검증 공시(verification·temporal_audit·인용검증·조례예산 표본)
    succession.json             region_succession + 폐지 지자체 승계
    extend_index.json           이 스크립트 산출물 전체 카탈로그

카탈로그를 shard 디렉터리 밖(api/{kind}_index.json)에 두는 이유: make_nationwide.build_index 가
api/{kind}/*.json 을 통째로 glob 해 shard 로 간주한다. 디렉터리 안에 index.json 을 넣으면
'index' 라는 이름의 가짜 shard 가 카탈로그에 섞인다.

재개 가능: 이미 만들어진 shard 는 건너뛴다(--force 로 재생성). 한 건이 실패해도 나머지는 계속.

사용:
  cd system
  python make_extend_fixtures.py --only lifecycle,succession        # 빠른 시험(검색 제외)
  python make_extend_fixtures.py --only search --search-limit 3     # 검색 소규모 시험
  python make_extend_fixtures.py                                    # 전체
"""
import argparse
import collections
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:  # 콘솔이 cp949 여도 한글 출력에서 죽지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from policymap import db as D                              # noqa: E402
from policymap.config import get_config                    # noqa: E402
from policymap.mcp_server.server import Server             # noqa: E402

# 중복 구현 금지 — 봉투·살균·쓰기는 기존 생성기 것을 그대로 쓴다.
from make_gap_fixtures import envelope                     # noqa: E402
from make_nationwide import write_json, existing, human    # noqa: E402

KINDS = ("search", "votes", "lifecycle", "statute", "verification", "succession")

# --------------------------------------------------------------------------- #
# 1) 검색 질의 — 카테고리 C01~C14 를 고르게 덮는 대표 질의
#    (query, slug, category_code). slug 는 ASCII(정적 호스팅 URL 안전).
#    기존 5건(youth-housing/birth-incentive/pet-registration/disaster-safety/carbon-neutral)은
#    make_nationwide 가 이미 만들었다. 여기서도 목록에 넣어 두면 존재 시 skip 되고
#    카탈로그에는 함께 실린다.
# --------------------------------------------------------------------------- #
SEARCH_QUERIES = [
    ("주민참여예산 운영",            "participatory-budget", "C01"),
    ("행정정보 공개 및 제공",        "info-disclosure",      "C01"),
    ("주민자치회 설치 운영",         "resident-council",     "C01"),
    ("지방세 감면 및 세제 지원",     "local-tax-relief",     "C02"),
    ("지방보조금 관리 및 정산",      "subsidy-management",   "C02"),
    ("치매 어르신 돌봄 지원",        "dementia-care",        "C03"),
    ("장애인 활동지원 서비스",       "disability-support",   "C03"),
    ("노인 일자리 및 사회활동 지원", "senior-jobs",          "C03"),
    ("출산 장려금",                  "birth-incentive",      "C04"),
    ("아동수당 및 보육료 지원",      "childcare-subsidy",    "C04"),
    ("다함께돌봄 아동 돌봄센터",     "child-care-center",    "C04"),
    ("청년 월세 지원",               "youth-housing",        "C05"),
    ("학교 급식 지원",               "school-meal",          "C05"),
    ("평생교육 진흥",                "lifelong-education",   "C05"),
    ("대학생 학자금 이자 지원",      "student-loan",         "C05"),
    ("감염병 예방 및 관리",          "infectious-disease",   "C06"),
    ("정신건강 증진 및 자살예방",    "mental-health",        "C06"),
    ("공공보건의료 및 응급의료 지원", "public-health-care",  "C06"),
    ("미세먼지 저감 및 대기질 개선", "fine-dust",            "C07"),
    ("탄소중립 녹색성장",            "carbon-neutral",       "C07"),
    ("폐기물 감량 및 재활용 촉진",   "waste-recycling",      "C07"),
    ("하천 및 수질 관리",            "water-quality",        "C07"),
    ("재난 안전 관리",               "disaster-safety",      "C08"),
    ("침수 및 풍수해 예방",          "flood-prevention",     "C08"),
    ("어린이 보호구역 교통안전",     "school-zone-safety",   "C08"),
    ("도시재생 활성화",              "urban-regeneration",   "C09"),
    ("빈집 정비 및 활용",            "vacant-house",         "C09"),
    ("공공임대주택 공급",            "public-housing",       "C09"),
    ("대중교통 요금 지원",           "public-transit",       "C10"),
    ("자전거 이용 활성화",           "bicycle",              "C10"),
    ("전통시장 및 상점가 활성화",    "traditional-market",   "C11"),
    ("소상공인 경영 지원",           "small-business",       "C11"),
    ("사회적경제 기업 육성",         "social-economy",       "C11"),
    ("농업인 경영안정 지원",         "farm-support",         "C12"),
    ("산림 및 도시숲 조성",          "urban-forest",         "C12"),
    ("생활체육 진흥",                "sports-for-all",       "C13"),
    ("문화예술 지원 및 공공도서관",  "culture-library",       "C13"),
    ("관광 활성화 및 축제 지원",     "tourism",              "C13"),
    ("반려동물 등록",                "pet-registration",     "C14"),
    ("유기동물 보호 및 입양",        "stray-animal",         "C14"),
]

# 조례-예산 링크 표본검증 수치. DB 가 아니라 표본검증 보고서에서 온 값이므로
# 출처를 함께 싣는다(검증 안 된 수치는 검증상태 병기 규율).
BUDGET_SAMPLE_REPORT = {
    "source": "17_링크_표본검증_보고서.md",
    "source_kind": "표본검증 보고서(사람 판정)",
    "judged": 584,
    "precision_strict": 0.649,
    "precision_strict_ci95": [0.604, 0.695],
    "precision_high_conf": 0.932,
    "precision_high_conf_ci95": [0.898, 0.966],
    "high_conf_threshold": 0.8,
    "note": "DB 집계가 아니라 584건 표본을 사람이 판정한 결과다. "
            "'엄격' 은 애매 판정을 실패로 계산한 값이다.",
}

# --------------------------------------------------------------------------- #
# 공통 유틸
# --------------------------------------------------------------------------- #
_SIDO_PREFIX = re.compile(
    r"^(서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|"
    r"세종특별자치시|경기도|강원특별자치도|강원도|충청북도|충청남도|전북특별자치도|"
    r"전라북도|전라남도|경상북도|경상남도|제주특별자치도|전남광주통합특별시|"
    r"부산직할시|대구직할시|인천직할시)")
_NON_WORD = re.compile(r"[^0-9A-Za-z가-힣]+")
_SAFE_SLUG = re.compile(r"[^0-9A-Za-z._-]+")


def policy_key(name, region):
    """조례명에서 지자체 접두어를 떼어 '정책 키' 로 정규화한다.

    '고성군 저탄소 녹색성장 기본 조례' → '저탄소녹색성장기본조례'.
    같은 정책을 여러 지자체가 폐지한 것을 세려면 이 정규화가 필요하다
    (원문 그대로 GROUP BY 하면 최다가 3곳에 그친다 — 실측).
    """
    s = (name or "").strip()
    if not s:
        return "", ""
    for pref in ((region or {}).get("full_name") or "", (region or {}).get("name") or ""):
        if pref and s.startswith(pref):
            s = s[len(pref):]
    s = _SIDO_PREFIX.sub("", s)
    pref = (region or {}).get("name") or ""
    if pref and s.startswith(pref):
        s = s[len(pref):]
    display = s.strip(" ㆍ·,")
    return _NON_WORD.sub("", s), display


def safe_slug(s):
    """instrument_id('statute:276653')처럼 ':' 를 담은 키를 파일명으로 바꾼다.

    한글이 섞인 키('lawname:보험업법')를 그냥 치환하면 전부 'lawname--' 가 돼 서로 덮어쓴다
    [실측: top200 중 lawname 3건이 파일 2개로 붕괴 → 200 요청에 198 파일].
    ASCII 밖 문자가 있으면 원본 해시를 붙여 충돌을 막는다.
    """
    s = str(s)
    base = s.replace(":", "-")
    if _SAFE_SLUG.search(base):
        import hashlib
        h = hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]
        return (_SAFE_SLUG.sub("-", base).strip("-") or "id") + "-" + h
    return base


_LAW_HOST = "https://www.law.go.kr"


def abs_url(u):
    """호스트가 빠진 official_url 을 절대 URL 로 되돌린다.

    폐지 조례 40,406건 전부가 '/DRF/lawService.do?...' 로 저장돼 있다(수집기가 상대경로를
    그대로 넣었다). 현행 조례 159,452건은 'https://www.law.go.kr/DRF/...' 다 [실측].
    그대로 화면에 걸면 폐지 조례 링크가 전부 깨진다. 경로 패턴이 동일하므로 같은 호스트를
    붙인다. 원본은 official_url_raw 로 함께 남긴다.
    """
    u = (u or "").strip()
    if u.startswith("/"):
        return _LAW_HOST + u
    return u or None


def ymd(s):
    """'20260701' → '2026-07-01'. 형식이 아니면 None."""
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


def year_of(s):
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        y = int(s[:4])
        return y if 1948 <= y <= 2100 else None
    return None


def load_regions(conn):
    """region_id → 지역 메타. sig_cd 로도 찾을 수 있게 둘 다 키로 담는다."""
    rows = D.fetchall(conn, "SELECT region_id, sig_cd, name, full_name, level, status, "
                            "has_legislation FROM regions")
    by_rid = {r["region_id"]: r for r in rows}
    return by_rid


class Ctx:
    """실행 컨텍스트(연결·서버·출력경로·리포트)를 한 곳에 모은다."""

    def __init__(self, conn, srv, out, args, report):
        self.conn, self.srv, self.out, self.args, self.report = conn, srv, out, args, report

    def fail(self, kind, key, e):
        msg = f"{type(e).__name__}: {str(e)[:160]}"
        self.report["errors"].append({"kind": kind, "key": key, "error": msg})
        print(f"  [실패] {kind}/{key} — {msg}", flush=True)

    def warn(self, kind, key, message):
        self.report["warnings"].append({"kind": kind, "key": key, "message": message})


def emit(ctx, kind, key, rel, builder, note_fn=None):
    """shard 1건 생성(존재하면 skip). 반환: 카탈로그 항목 dict 또는 None."""
    path = ctx.out / rel
    size = None if ctx.args.force else existing(path)
    if size is not None:
        item = {"key": key, "path": rel, "bytes": size, "reused": True}
        if note_fn:  # 재사용본도 카탈로그 메모는 채운다(빈 칸이 생기면 화면이 '-' 로 보인다)
            try:
                item.update(note_fn(json.loads(path.read_text(encoding="utf-8"))) or {})
            except Exception:  # noqa: BLE001
                pass
        return item
    t0 = time.time()
    try:
        env = builder()
    except Exception as e:  # noqa: BLE001
        ctx.fail(kind, key, e)
        return None
    size = write_json(path, env)
    item = {"key": key, "path": rel, "bytes": size, "reused": False,
            "seconds": round(time.time() - t0, 2)}
    if note_fn:
        try:
            item.update(note_fn(env) or {})
        except Exception:  # noqa: BLE001
            pass
    return item


# --------------------------------------------------------------------------- #
# 1) search — 카테고리 대표 질의 사전계산
# --------------------------------------------------------------------------- #
def build_search(ctx):
    a = ctx.args
    cat_names = {r["code"]: r["name"] for r in
                 D.fetchall(ctx.conn, "SELECT code, name FROM categories")}
    todo = SEARCH_QUERIES[: a.search_limit] if a.search_limit else SEARCH_QUERIES
    print(f"[검색] 질의 {len(todo)}종 (카테고리 {len(set(c for *_, c in todo))}종)", flush=True)
    items = []
    for query, slug, cat in todo:
        def _b(q=query):
            return ctx.srv._tool_semantic_search_ordinance(
                {"query": q, "k": a.search_k, "with_text": True})

        it = emit(ctx, "search", slug, f"search/{slug}.json", _b,
                  note_fn=lambda env: {"count": (env.get("data") or {}).get("count")})
        if it is None:
            continue
        it.update({"slug": slug, "query": query, "category_code": cat,
                   "category_name": cat_names.get(cat)})
        items.append(it)
        print(f"  {slug:<22} [{cat}] '{query}' · 결과 {it.get('count', '-')}건 · "
              f"{'skip' if it['reused'] else human(it['bytes'])}", flush=True)
    # 커버리지 점검: C01~C14 중 빠진 카테고리가 있으면 경고
    covered = {it["category_code"] for it in items}
    missing = [f"C{i:02d}" for i in range(1, 15) if f"C{i:02d}" not in covered]
    if missing:
        ctx.warn("search", "coverage", f"미커버 카테고리 {missing}")
    ctx.report["search"] = items
    ctx.report["search_coverage"] = {"categories": sorted(covered), "missing": missing}
    write_json(ctx.out / "search_index.json", envelope(
        {"queries": items, "coverage": ctx.report["search_coverage"],
         "category_names": cat_names}))


# --------------------------------------------------------------------------- #
# 2) votes — 표결 shard 확대(표결수 상위 + 최근)
# --------------------------------------------------------------------------- #
def build_votes(ctx):
    a = ctx.args
    total_bills = D.fetchone(ctx.conn, "SELECT COUNT(DISTINCT bill_id) n FROM votes")["n"]
    rows = D.fetchall(ctx.conn, """
        SELECT v.bill_id, COUNT(*) AS n, b.bill_no, b.name, b.committee,
               b.propose_dt, b.proc_dt, b.proc_result, b.member_tcnt, b.vote_tcnt,
               b.yes_tcnt, b.no_tcnt, b.blank_tcnt
          FROM votes v LEFT JOIN bills b ON b.bill_id = v.bill_id
         GROUP BY v.bill_id
         ORDER BY n DESC, b.proc_dt DESC, v.bill_id ASC""")
    top = rows[: a.votes_top]
    recent = sorted(rows, key=lambda r: (r.get("proc_dt") or ""), reverse=True)[: a.votes_recent]
    seen, picked = set(), []
    for r in top + recent:
        if r["bill_id"] in seen:
            continue
        seen.add(r["bill_id"])
        picked.append(r)
    print(f"[표결] 표결기록 보유 의안 {total_bills}건 중 {len(picked)}건 생성"
          f" (상위 {len(top)} + 최근 {len(recent)})", flush=True)
    items = []
    for i, r in enumerate(picked, 1):
        key = str(r.get("bill_no") or r["bill_id"])
        it = emit(ctx, "votes", key, f"votes/{safe_slug(key)}.json",
                  lambda bid=r["bill_id"]: ctx.srv._tool_bill_vote_breakdown({"bill_id": bid}))
        if it is None:
            continue
        it.update({"bill_id": r["bill_id"], "bill_no": r.get("bill_no"),
                   "name": r.get("name"), "committee": r.get("committee"),
                   "propose_dt": r.get("propose_dt"), "proc_dt": r.get("proc_dt"),
                   "proc_result": r.get("proc_result"), "vote_records": r["n"],
                   "yes_tcnt": r.get("yes_tcnt"), "no_tcnt": r.get("no_tcnt"),
                   "blank_tcnt": r.get("blank_tcnt")})
        items.append(it)
        if i <= 5 or i % 25 == 0:
            print(f"  [{i}/{len(picked)}] {key} · 표결 {r['n']}건 · "
                  f"{(r.get('name') or '')[:30]} · "
                  f"{'skip' if it['reused'] else human(it['bytes'])}", flush=True)
    ctx.report["votes"] = items
    write_json(ctx.out / "votes_index.json", envelope(
        {"bills": items,
         "totals": {"bills_with_votes": total_bills, "shards": len(items),
                    "vote_rows": D.fetchone(ctx.conn, "SELECT COUNT(*) n FROM votes")["n"],
                    "bills_total": D.fetchone(ctx.conn, "SELECT COUNT(*) n FROM bills")["n"]},
         "selection": {"top_by_vote_records": len(top), "recent_by_proc_dt": len(recent)}}))


# --------------------------------------------------------------------------- #
# 3) lifecycle — 폐지 조례 40,406 을 쓰는 생애주기
# --------------------------------------------------------------------------- #
def _repeal_rows(conn):
    return D.fetchall(conn, """
        SELECT ordinance_id, region_id, name, ord_kind, enacted_on, effective_on,
               repealed_on, official_url
          FROM ordinances
         WHERE repealed_on IS NOT NULL AND repealed_on <> ''""")


def build_lifecycle(ctx):
    conn = ctx.conn
    a = ctx.args
    regs = load_regions(conn)
    print("[생애주기] 폐지 조례 집계 중…", flush=True)

    # --- 전국 집계: 연도 곡선 / 정책 키 / 일괄폐지 코호트 -------------------- #
    enact_year = collections.Counter()
    for r in D.fetchall(conn, "SELECT enacted_on, rr_cls_cd FROM ordinances "
                              "WHERE enacted_on IS NOT NULL AND enacted_on <> ''"):
        y = year_of(r["enacted_on"])
        if y:
            enact_year[y] += 1
    kind_year = collections.defaultdict(collections.Counter)
    for r in D.fetchall(conn, "SELECT rr_cls_cd, enacted_on FROM ordinances "
                              "WHERE enacted_on IS NOT NULL AND enacted_on <> ''"):
        y = year_of(r["enacted_on"])
        if y:
            kind_year[r["rr_cls_cd"] or "미상"][y] += 1

    rows = _repeal_rows(conn)
    bad_date, sentinel = 0, 0
    repeal_year = collections.Counter()
    by_key_regions = collections.defaultdict(set)      # 정책키 → 지자체 집합
    by_key_rows = collections.Counter()
    key_display = {}
    cohort = collections.defaultdict(set)              # (정책키, 폐지일) → 지자체
    cohort_ids = collections.defaultdict(list)
    per_region = collections.defaultdict(list)
    for r in rows:
        raw = (r["repealed_on"] or "").strip()
        if raw == "99991231":
            sentinel += 1
        y = year_of(raw)
        if y is None:
            bad_date += 1
        else:
            repeal_year[y] += 1
        reg = regs.get(r["region_id"]) or {}
        k, disp = policy_key(r["name"], reg)
        if k:
            by_key_regions[k].add(r["region_id"])
            by_key_rows[k] += 1
            key_display.setdefault(k, disp)
            if y is not None:
                cohort[(k, raw)].add(r["region_id"])
                if len(cohort_ids[(k, raw)]) < 300:
                    cohort_ids[(k, raw)].append(r["ordinance_id"])
        per_region[r["region_id"]].append((r, k, disp, y))

    top_repealed = sorted(by_key_regions.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    top_repealed = [{
        "policy_key": k, "display_name": key_display.get(k),
        "regions": len(v), "rows": by_key_rows[k],
    } for k, v in top_repealed[: a.lifecycle_top]]

    cohorts = sorted(((kd, regions) for kd, regions in cohort.items()
                      if len(regions) >= a.cohort_min),
                     key=lambda kv: -len(kv[1]))[: a.cohort_top]
    # 폐지 조례에 걸린 위임행 수 — linked_parents 가 비는 이유를 수치로 밝힌다.
    deleg_on_repealed = D.fetchone(conn, """
        SELECT COUNT(*) n FROM delegations d JOIN ordinances o ON o.ordinance_id = d.child_id
         WHERE o.repealed_on IS NOT NULL AND o.repealed_on <> ''""")["n"]
    cohort_items = []
    for (k, date), regions in cohorts:
        ids = cohort_ids[(k, date)]
        parents = []
        if ids:
            ph = ",".join("?" for _ in ids)
            parents = D.fetchall(conn, f"""
                SELECT d.parent_id, COUNT(*) n, li.name
                  FROM delegations d LEFT JOIN legal_instrument li
                    ON li.instrument_id = d.parent_id
                 WHERE d.child_id IN ({ph})
                 GROUP BY d.parent_id ORDER BY n DESC LIMIT 3""", ids)
        cohort_items.append({
            "policy_key": k, "display_name": key_display.get(k),
            "repealed_on": ymd(date), "regions": len(regions),
            "sample_ordinance_ids": ids[:6],
            "delegation_lookup_ids": len(ids),
            "linked_parents": [{"parent_id": p["parent_id"], "name": p.get("name"),
                                "delegation_rows": p["n"]} for p in parents],
            "linked_parents_note": (
                f"위임관계(delegations)는 본문을 수집한 조례에서 추출했다. 폐지 조례 "
                f"{len(rows):,}건에 걸린 위임행은 {deleg_on_repealed:,}행뿐이라 "
                "linked_parents 가 비는 것이 정상이다. 비어 있다고 상위법 근거가 "
                "없다는 뜻이 아니다."),
        })

    relative_url = D.fetchone(conn, "SELECT COUNT(*) n FROM ordinances "
                                    "WHERE official_url LIKE '/%'")["n"]
    quality = {
        "repealed_rows": len(rows),
        "unparseable_repealed_on": bad_date,
        "sentinel_99991231": sentinel,
        "relative_official_url_rows": relative_url,
        "note": "repealed_on 은 YYYYMMDD 문자열이다. '99991231' 은 종기 미정 sentinel 이라 "
                "연도 곡선에서 제외하지 않고 그대로 세면 9999년에 봉우리가 생긴다. "
                "여기서는 파싱 가능한 값만 곡선에 넣고 sentinel 은 별도로 센다.",
        "url_note": f"official_url 이 호스트 없이 '/DRF/...' 로 저장된 행이 "
                    f"{relative_url:,}건이다(폐지 조례 전량). 그대로 링크하면 전부 깨지므로 "
                    f"출력에서는 {_LAW_HOST} 를 붙여 절대 URL 로 내보낸다. "
                    "DB 자체는 손대지 않았다(수집기 tools_collect_repealed.py 의 잔재).",
    }

    national = {
        "totals": {
            "ordinances": D.fetchone(conn, "SELECT COUNT(*) n FROM ordinances")["n"],
            "repealed": len(rows),
            "by_rr_cls_cd": {r["rr_cls_cd"] or "미상": r["n"] for r in D.fetchall(
                conn, "SELECT rr_cls_cd, COUNT(*) n FROM ordinances GROUP BY rr_cls_cd")},
            "by_lifecycle": {r["lifecycle"] or "미기재(폐지본)": r["n"] for r in D.fetchall(
                conn, "SELECT lifecycle, COUNT(*) n FROM ordinances GROUP BY lifecycle")},
            "work_rows": D.fetchone(conn, "SELECT COUNT(*) n FROM ordinance_work")["n"],
        },
        "enacted_by_year": [{"year": y, "n": enact_year[y]} for y in sorted(enact_year)],
        "repealed_by_year": [{"year": y, "n": repeal_year[y]} for y in sorted(repeal_year)],
        "by_rr_cls_year": {k: [{"year": y, "n": c[y]} for y in sorted(c)]
                           for k, c in kind_year.items()},
        "top_repealed_policies": top_repealed,
        "mass_repeal_cohorts": cohort_items,
        "data_quality": quality,
        "method": {
            "policy_key": "조례명에서 지자체 접두어(full_name/name·시도명)를 떼고 "
                          "공백·기호를 제거한 정규화 키. 원문 GROUP BY 로는 같은 정책의 "
                          "전국 폐지를 셀 수 없다(최다 3곳).",
            "cohort": f"같은 정책키를 같은 날짜에 {a.cohort_min}곳 이상이 폐지한 묶음. "
                      "상위법 개정에 따른 일괄 폐지의 관측 신호이며, 인과는 확인되지 않았다.",
        },
        "caveat": "폐지 조례는 선례로 추천하지 않는다. 이 화면은 '왜 사라졌는가' 를 보여주는 "
                  "이력 자료이며 현행 근거가 아니다.",
    }
    write_json(ctx.out / "lifecycle_index.json", envelope(national))
    print(f"  전국 요약: 폐지 {len(rows)}건 · 정책키 {len(by_key_regions)}종 · "
          f"일괄폐지 코호트 {len(cohort_items)}건 · sentinel {sentinel}건", flush=True)

    # --- 지역 shard --------------------------------------------------------- #
    targets = D.fetchall(conn, """
        SELECT region_id, sig_cd, name, full_name, level FROM regions
         WHERE status='active' AND has_legislation=1 AND level IN (1,2)
         ORDER BY level, sig_cd""")
    if a.limit:
        targets = targets[: a.limit]
    items = []
    for i, reg in enumerate(targets, 1):
        rid, sig = reg["region_id"], reg["sig_cd"]

        def _b(rid=rid, reg=reg):
            mine = per_region.get(rid) or []
            ry = collections.Counter()
            keys = collections.Counter()
            disp = {}
            recent = []
            for r, k, d, y in mine:
                if y:
                    ry[y] += 1
                if k:
                    keys[k] += 1
                    disp.setdefault(k, d)
                recent.append(r)
            recent = sorted(recent, key=lambda r: (r["repealed_on"] or ""), reverse=True)
            counts = {r["rr_cls_cd"] or "미상": r["n"] for r in D.fetchall(
                conn, "SELECT rr_cls_cd, COUNT(*) n FROM ordinances WHERE region_id=? "
                      "GROUP BY rr_cls_cd", (rid,))}
            ey = collections.Counter()
            for r in D.fetchall(conn, "SELECT enacted_on FROM ordinances WHERE region_id=?",
                                (rid,)):
                y = year_of(r["enacted_on"])
                if y:
                    ey[y] += 1
            active = D.fetchone(
                conn, "SELECT COUNT(*) n FROM ordinances WHERE region_id=? AND status='active'",
                (rid,))["n"]
            return envelope({
                "region": {"region_id": rid, "sig_cd": sig, "name": reg.get("name"),
                           "full_name": reg.get("full_name"), "level": reg.get("level")},
                "counts": {"total": sum(counts.values()), "active": active,
                           "repealed": len(mine), "by_rr_cls_cd": counts},
                "enacted_by_year": [{"year": y, "n": ey[y]} for y in sorted(ey)],
                "repealed_by_year": [{"year": y, "n": ry[y]} for y in sorted(ry)],
                # 한 지자체가 같은 정책을 두 번 폐지하는 일은 드물어 rows 는 대부분 1이다.
                # 그래서 '전국에서 몇 곳이 같이 폐지했나' 를 2차 정렬키로 쓴다 —
                # 그래야 '우리도 폐지한, 전국적으로 사라진 정책' 이 위로 온다.
                "top_repealed_policies": [
                    {"policy_key": k, "display_name": disp.get(k), "rows": n,
                     "nationwide_regions": len(by_key_regions.get(k) or ())}
                    for k, n in sorted(
                        keys.items(),
                        key=lambda kv: (-kv[1], -len(by_key_regions.get(kv[0]) or ()),
                                        kv[0]))[: a.region_top]],
                "recent_repeals": [
                    {"ordinance_id": r["ordinance_id"], "name": r["name"],
                     "ord_kind": r["ord_kind"], "enacted_on": ymd(r["enacted_on"]),
                     "repealed_on": ymd(r["repealed_on"]),
                     "repealed_on_raw": r["repealed_on"],
                     # 원본(호스트 없는 상대경로)은 싣지 않는다. 243곳 × 40건이면 1MB 가
                     # 통째로 늘고, data_quality.url_note 가 변환 사실을 이미 밝힌다.
                     "official_url": abs_url(r["official_url"])}
                    for r in recent[: a.region_recent]],
                "data_quality": {
                    "unparseable_repealed_on": sum(
                        1 for r, _k, _d, y in mine if y is None),
                    "sentinel_99991231": sum(
                        1 for r, _k, _d, _y in mine
                        if (r["repealed_on"] or "").strip() == "99991231")},
                "caveat": national["caveat"],
            }, regions=[sig])

        it = emit(ctx, "lifecycle", sig, f"lifecycle/{sig}.json", _b)
        if it is None:
            continue
        it.update({"sig_cd": sig, "region_id": rid, "name": reg.get("full_name"),
                   "level": reg.get("level"), "repealed": len(per_region.get(rid) or [])})
        items.append(it)
        if i <= 3 or i % 60 == 0 or i == len(targets):
            print(f"  [{i}/{len(targets)}] {sig} {reg.get('full_name')} · 폐지 "
                  f"{it['repealed']}건 · {'skip' if it['reused'] else human(it['bytes'])}",
                  flush=True)
    ctx.report["lifecycle"] = items


# --------------------------------------------------------------------------- #
# 4) statute — 위임 참조 상위 법령의 조문 열람
# --------------------------------------------------------------------------- #
def build_statute(ctx):
    conn = ctx.conn
    a = ctx.args
    print(f"[법령조문] 위임 참조 상위 {a.statute_top}개 법령 집계 중…", flush=True)
    parents = D.fetchall(conn, """
        SELECT parent_id, COUNT(*) n, COUNT(DISTINCT child_id) c
          FROM delegations GROUP BY parent_id ORDER BY n DESC LIMIT ?""",
                         (a.statute_top,))
    art_total = D.fetchone(conn, "SELECT COUNT(*) n FROM articles")["n"]
    items = []
    for i, p in enumerate(parents, 1):
        pid = p["parent_id"]
        slug = safe_slug(pid)

        def _b(pid=pid, p=p):
            li = D.fetchone(conn, """
                SELECT instrument_id, kind, national_tier, name, short_name,
                       competent_authority, enacted_on, effective_on, repealed_on,
                       rr_cls_cd, official_url, status, verification_status, as_of_date
                  FROM legal_instrument WHERE instrument_id=?""", (pid,))
            arts = D.fetchall(conn, """
                SELECT article_no, article_branch, title, body, effective_on, article_key
                  FROM articles WHERE instrument_id=?""", (pid,))
            # article_no 는 텍스트라 SQL ORDER BY 로는 '10' 이 '2' 앞에 온다. 조문은
            # 번호 순으로 읽는 물건이라 파이썬에서 숫자 우선으로 정렬한다.
            arts.sort(key=lambda x: (
                int(x["article_no"]) if (x["article_no"] or "").isdigit() else 10**9,
                x["article_no"] or "", x["article_branch"] or ""))
            trunc = 0
            out_arts = []
            for x in arts[: a.article_limit]:
                body = x["body"] or ""
                if len(body) > a.body_chars:
                    body = body[: a.body_chars]
                    trunc += 1
                    cut = True
                else:
                    cut = False
                out_arts.append({
                    "article_no": x["article_no"], "article_branch": x["article_branch"],
                    "title": x["title"], "body": body, "body_truncated": cut,
                    "effective_on": x["effective_on"], "article_key": x["article_key"]})
            vstat = {r["verification_status"] or "미상": r["n"] for r in D.fetchall(
                conn, "SELECT verification_status, COUNT(*) n FROM delegations "
                      "WHERE parent_id=? GROUP BY verification_status", (pid,))}
            dtype = {r["delegation_type"] or "미상": r["n"] for r in D.fetchall(
                conn, "SELECT delegation_type, COUNT(*) n FROM delegations "
                      "WHERE parent_id=? GROUP BY delegation_type", (pid,))}
            regions = D.fetchall(conn, """
                SELECT o.region_id, r.full_name, COUNT(DISTINCT d.child_id) n
                  FROM delegations d
                  JOIN ordinances o ON o.ordinance_id = d.child_id
                  LEFT JOIN regions r ON r.region_id = o.region_id
                 WHERE d.parent_id=?
                 GROUP BY o.region_id ORDER BY n DESC LIMIT ?""", (pid, a.statute_regions))
            children = D.fetchall(conn, """
                SELECT DISTINCT o.ordinance_id, o.name, o.region_id, o.org_name,
                       o.repealed_on, o.official_url
                  FROM delegations d JOIN ordinances o ON o.ordinance_id = d.child_id
                 WHERE d.parent_id=? LIMIT ?""", (pid, a.statute_children))
            return envelope({
                "instrument": dict(li) if li else {"instrument_id": pid,
                                                   "name": None,
                                                   "missing": True},
                "delegation": {
                    "rows": p["n"], "child_ordinances": p["c"],
                    "by_verification_status": vstat, "by_delegation_type": dtype,
                    "regions_top": [{"region_id": r["region_id"], "name": r.get("full_name"),
                                     "ordinances": r["n"]} for r in regions],
                },
                "articles": out_arts,
                "article_count": len(arts),
                "articles_shown": len(out_arts),
                "articles_truncated_bodies": trunc,
                "articles_available": bool(arts),
                "child_ordinances_sample": [
                    {"ordinance_id": c["ordinance_id"], "name": c["name"],
                     "region_id": c["region_id"], "org_name": c["org_name"],
                     "repealed": bool((c["repealed_on"] or "").strip()),
                     "official_url": abs_url(c["official_url"])} for c in children],
                "caveat": "위임 관계는 조례 본문의 인용 문구에서 추출한 것이다. "
                          "verification_status 가 article-verified 가 아닌 항목은 "
                          "조문 존재를 확인하지 못했다.",
            })

        it = emit(ctx, "statute", pid, f"statute/{slug}.json", _b, note_fn=lambda env: {
            "article_count": (env.get("data") or {}).get("article_count"),
            "articles_available": (env.get("data") or {}).get("articles_available"),
            "name": ((env.get("data") or {}).get("instrument") or {}).get("name"),
            "kind": ((env.get("data") or {}).get("instrument") or {}).get("kind"),
        })
        if it is None:
            continue
        it.update({"instrument_id": pid, "slug": slug,
                   "delegation_rows": p["n"], "child_ordinances": p["c"]})
        items.append(it)
        if i <= 5 or i % 50 == 0 or i == len(parents):
            print(f"  [{i}/{len(parents)}] {pid} · 위임 {p['n']}행/{p['c']}조례 · "
                  f"조문 {it.get('article_count', '-')} · "
                  f"{'skip' if it['reused'] else human(it['bytes'])}", flush=True)
    with_arts = sum(1 for x in items if x.get("articles_available"))
    ctx.report["statute"] = items
    write_json(ctx.out / "statute_index.json", envelope({
        "instruments": items,
        "totals": {
            "articles_rows": art_total,
            "instruments_with_articles": D.fetchone(
                conn, "SELECT COUNT(DISTINCT instrument_id) n FROM articles")["n"],
            "delegation_rows": D.fetchone(conn, "SELECT COUNT(*) n FROM delegations")["n"],
            "distinct_parents": D.fetchone(
                conn, "SELECT COUNT(DISTINCT parent_id) n FROM delegations")["n"],
            "shards": len(items), "shards_with_articles": with_arts,
        },
        "params": {"top": a.statute_top, "article_limit": a.article_limit,
                   "body_chars": a.body_chars},
        "caveat": "조문 본문은 화면 용량을 위해 앞부분만 싣는다(body_truncated=true). "
                  "인용 전 official_url 의 원문을 확인할 것.",
    }))
    if with_arts < len(items):
        ctx.warn("statute", "articles",
                 f"{len(items) - with_arts}개 법령은 articles 미수집이라 조문 목록이 비어 있다")
    print(f"  조문 보유 {with_arts}/{len(items)}", flush=True)


# --------------------------------------------------------------------------- #
# 5) verification — 검증 공시
# --------------------------------------------------------------------------- #
def build_verification(ctx):
    conn = ctx.conn
    print("[검증공시] 집계 중…", flush=True)
    ver_status = D.fetchall(conn, "SELECT entity_type, status, COUNT(*) n FROM verification "
                                  "GROUP BY entity_type, status ORDER BY n DESC")
    ver_methods = D.fetchall(conn, "SELECT method, COUNT(*) n FROM verification "
                                   "GROUP BY method ORDER BY n DESC LIMIT 10")
    cite = D.fetchone(conn, """
        SELECT SUM(citation_entries) citation_entries,
               SUM(explicit_citation_entries) explicit_citation_entries,
               SUM(article_references) article_references,
               SUM(verified_references) verified_references,
               SUM(missing_references) missing_references,
               SUM(uncheckable_references) uncheckable_references
          FROM verification""")
    ver_dates = D.fetchone(conn, "SELECT MIN(verified_at) a, MAX(verified_at) b "
                                 "FROM verification WHERE verified_at IS NOT NULL")
    audit = D.fetchall(conn, "SELECT entity_type, rule, severity, repaired, COUNT(*) n "
                             "FROM temporal_audit GROUP BY entity_type, rule, severity, "
                             "repaired ORDER BY n DESC")
    audit_samples = D.fetchall(conn, "SELECT rule, entity_type, entity_id, severity, "
                                     "observed, repair_action FROM temporal_audit "
                                     "WHERE severity IN ('error','warn') LIMIT 20")
    deleg = {r["verification_status"] or "미상": r["n"] for r in D.fetchall(
        conn, "SELECT verification_status, COUNT(*) n FROM delegations "
              "GROUP BY verification_status")}
    deleg_total = sum(deleg.values()) or 1
    ord_status = {r["verification_status"] or "미상": r["n"] for r in D.fetchall(
        conn, "SELECT verification_status, COUNT(*) n FROM ordinances "
              "GROUP BY verification_status")}
    li_status = {r["verification_status"] or "미상": r["n"] for r in D.fetchall(
        conn, "SELECT verification_status, COUNT(*) n FROM legal_instrument "
              "GROUP BY verification_status")}
    bl_total = D.fetchone(conn, "SELECT COUNT(*) n FROM ordinance_budget_link")["n"]
    bl_verified = {str(r["verified"]): r["n"] for r in D.fetchall(
        conn, "SELECT verified, COUNT(*) n FROM ordinance_budget_link GROUP BY verified")}
    bl_conf = D.fetchall(conn, """
        SELECT CASE WHEN confidence>=0.8 THEN 'conf>=0.8'
                    WHEN confidence>=0.5 THEN '0.5<=conf<0.8'
                    ELSE 'conf<0.5' END bucket,
               COUNT(*) n, AVG(confidence) avg_conf
          FROM ordinance_budget_link GROUP BY bucket ORDER BY bucket DESC""")
    bl_method = D.fetchall(conn, "SELECT match_method, COUNT(*) n, AVG(confidence) avg_conf "
                                 "FROM ordinance_budget_link GROUP BY match_method "
                                 "ORDER BY n DESC LIMIT 15")

    data = {
        "verification_table": {
            "rows": D.fetchone(conn, "SELECT COUNT(*) n FROM verification")["n"],
            "by_entity_status": [dict(r) for r in ver_status],
            "top_methods": [dict(r) for r in ver_methods],
            "citation_totals": dict(cite) if cite else {},
            "verified_at_range": [ver_dates.get("a"), ver_dates.get("b")] if ver_dates else None,
            "note": "verification 테이블은 korea100 시드·기관 캔버스 등 '사람이 출처를 대조한' "
                    "항목만 담는다. 전체 조례 20만건의 검증 상태가 아니다.",
        },
        "temporal_audit": {
            "rows": D.fetchone(conn, "SELECT COUNT(*) n FROM temporal_audit")["n"],
            "by_rule": [dict(r) for r in audit],
            "samples": [dict(r) for r in audit_samples],
            "note": "시간 무결성 규칙 위반 기록. repaired=1 은 자동 보정됐음을 뜻한다.",
        },
        "citation_verification": {
            "scope": "delegations (조례 → 상위법 위임 인용) 전수",
            "total": deleg_total,
            "by_status": deleg,
            "rates": {k: round(v / deleg_total, 4) for k, v in deleg.items()},
            "legend": {
                "article-verified": "인용된 상위법 조문의 존재를 확인함",
                "article-missing": "인용 조문을 상위법에서 찾지 못함(오인용 또는 개정 이력)",
                "unverifiable": "조문번호가 없거나 상위법 본문 미수집이라 자동 확인 불가",
            },
            "note": "조문 '존재' 확인이지 인용의 해석·적용 타당성 검증이 아니다.",
        },
        "instrument_status": {"ordinances": ord_status, "legal_instrument": li_status},
        "ordinance_budget_link": {
            "rows": bl_total,
            "by_verified": bl_verified,
            "by_confidence": [dict(r) for r in bl_conf],
            "by_method": [dict(r) for r in bl_method],
            "sample_validation": BUDGET_SAMPLE_REPORT,
            "note": "조례↔예산 연결은 확률적 매칭이다. verified=1(사람 확인) 외에는 모두 추정이며 "
                    "화면에는 추정 배지와 confidence 를 함께 표시해야 한다.",
        },
        "coverage_caveat": [
            "검증된 항목과 검증되지 않은 항목을 같은 화면에서 같은 무게로 보여주지 말 것.",
            "폐지 조례는 선례로 추천하지 않는다.",
            "as_of_date 를 항상 함께 노출한다.",
        ],
    }
    write_json(ctx.out / "verification" / "summary.json", envelope(data))
    ctx.report["verification"] = {
        "path": "verification/summary.json",
        "bytes": (ctx.out / "verification" / "summary.json").stat().st_size,
        "verification_rows": data["verification_table"]["rows"],
        "temporal_audit_rows": data["temporal_audit"]["rows"],
        "delegation_rows": deleg_total,
    }
    print(f"  verification {data['verification_table']['rows']}행 · "
          f"temporal_audit {data['temporal_audit']['rows']}행 · "
          f"인용검증 {deleg_total}건", flush=True)


# --------------------------------------------------------------------------- #
# 6) succession — 지자체 승계
# --------------------------------------------------------------------------- #
def build_succession(ctx):
    conn = ctx.conn
    regs = load_regions(conn)
    rows = D.fetchall(conn, "SELECT * FROM region_succession "
                            "ORDER BY effective_date, old_region_id, new_region_id")

    def meta(rid):
        r = regs.get(rid) or {}
        n = D.fetchone(conn, "SELECT COUNT(*) n FROM ordinances WHERE region_id=?", (rid,))["n"]
        return {"region_id": rid, "sig_cd": r.get("sig_cd"), "name": r.get("name"),
                "full_name": r.get("full_name"), "level": r.get("level"),
                "status": r.get("status"), "ordinances": n}

    items = []
    for r in rows:
        items.append({
            "old": meta(r["old_region_id"]), "new": meta(r["new_region_id"]),
            "succession_type": r["succession_type"],
            "effective_date": r["effective_date"],
            "legal_basis": r["legal_basis"],
            "status_note": r["status_note"],
        })
    groups = collections.defaultdict(list)
    for it in items:
        groups[(it["effective_date"], it["succession_type"])].append(it)
    events = [{"effective_date": d, "succession_type": t, "count": len(v),
               "old_regions": sorted({x["old"]["region_id"] for x in v}),
               "new_regions": sorted({x["new"]["region_id"] for x in v}),
               "legal_basis": v[0]["legal_basis"]}
              for (d, t), v in sorted(groups.items())]
    status_counts = {r["status"]: r["n"] for r in D.fetchall(
        conn, "SELECT status, COUNT(*) n FROM regions GROUP BY status")}
    orphan = D.fetchone(conn, "SELECT COUNT(*) n FROM temporal_audit "
                              "WHERE rule='T7_region_no_longer_exists'")["n"]
    orphan_reg = D.fetchone(conn, "SELECT COUNT(*) n FROM temporal_audit "
                                  "WHERE rule='T8_orphan_region'")["n"]
    data = {
        "successions": items,
        "events": events,
        "totals": {"rows": len(items), "events": len(events),
                   "regions_by_status": status_counts,
                   "ordinances_in_superseded_regions": sum(
                       it["old"]["ordinances"] for it in items)},
        "audit": {
            "T7_region_no_longer_exists": orphan,
            "T8_orphan_region": orphan_reg,
            "note": "승계로 사라진 지자체의 조례는 신 코드로 자동 이관되지 않는다. "
                    "temporal_audit 의 T7/T8 이 그 잔여를 센다.",
        },
        "caveat": "승계 이후 조례의 효력은 특별법 부칙의 경과규정에 따른다. "
                  "이 표는 코드 대응 관계이지 조례 효력 판단이 아니다.",
    }
    write_json(ctx.out / "succession.json", envelope(data))
    ctx.report["succession"] = {"path": "succession.json", "rows": len(items),
                                "events": len(events),
                                "bytes": (ctx.out / "succession.json").stat().st_size}
    print(f"[승계] {len(items)}건 / 사건 {len(events)}건 · "
          f"승계 대상 조례 {data['totals']['ordinances_in_superseded_regions']}건", flush=True)


# --------------------------------------------------------------------------- #
BUILDERS = {
    "search": build_search,
    "votes": build_votes,
    "lifecycle": build_lifecycle,
    "statute": build_statute,
    "verification": build_verification,
    "succession": build_succession,
}


def dir_bytes(path):
    if not path.exists():
        return 0, 0
    n = b = 0
    for f in path.rglob("*.json"):
        n += 1
        b += f.stat().st_size
    return n, b


def main() -> int:
    ap = argparse.ArgumentParser(description="미반영 자산 노출용 확장 fixture 생성")
    ap.add_argument("--out", default=str(ROOT / "data"), help="출력 루트(기본 system/data)")
    ap.add_argument("--only", default=",".join(KINDS),
                    help=f"생성 대상(쉼표). 선택지: {','.join(KINDS)}")
    ap.add_argument("--force", action="store_true", help="이미 있는 shard 도 재생성")
    ap.add_argument("--limit", type=int, default=0, help="lifecycle 지역 수 상한(0=전체)")
    # search
    ap.add_argument("--search-k", type=int, default=10, help="검색 결과 수")
    ap.add_argument("--search-limit", type=int, default=0, help="질의 수 상한(0=전체)")
    # votes
    ap.add_argument("--votes-top", type=int, default=120, help="표결수 상위 의안 수")
    ap.add_argument("--votes-recent", type=int, default=60, help="최근 처리 의안 수")
    # lifecycle
    ap.add_argument("--lifecycle-top", type=int, default=100, help="전국 최다 폐지 정책 TOP N")
    ap.add_argument("--cohort-min", type=int, default=10, help="일괄폐지 코호트 최소 지자체 수")
    ap.add_argument("--cohort-top", type=int, default=80, help="일괄폐지 코호트 상한")
    ap.add_argument("--region-top", type=int, default=20, help="지역별 폐지 정책 TOP N")
    ap.add_argument("--region-recent", type=int, default=40, help="지역별 최근 폐지 목록 수")
    # statute
    ap.add_argument("--statute-top", type=int, default=200, help="위임 참조 상위 법령 수")
    ap.add_argument("--article-limit", type=int, default=400, help="법령당 조문 수 상한")
    # 조문 본문은 한글이라 UTF-8 3바이트/자다. 상위 200법령 19,156조문에 본문을 통째로 실으면
    # 25MB 를 넘어 이 스크립트 예산(20MB)을 혼자 다 쓴다. 미리보기 길이로 자르고
    # 전문은 official_url 로 보낸다. [실측: cap 1000 → 약 22MB, cap 220 → 약 10MB]
    ap.add_argument("--body-chars", type=int, default=180,
                    help="조문 본문 미리보기 길이 상한(초과분은 body_truncated=true)")
    ap.add_argument("--statute-regions", type=int, default=20, help="법령당 상위 지역 수")
    ap.add_argument("--statute-children", type=int, default=20, help="법령당 조례 표본 수")
    a = ap.parse_args()

    asked = {s.strip() for s in a.only.split(",") if s.strip()}
    unknown = sorted(asked - set(KINDS))
    if unknown:
        print(f"[오류] 알 수 없는 --only 값: {unknown} (선택지 {list(KINDS)})")
        return 2
    a.only = [k for k in KINDS if k in asked]
    if not a.only:
        print(f"[오류] --only 값이 비었다. 선택지 {list(KINDS)}")
        return 2

    out = Path(a.out) / "api"
    out.mkdir(parents=True, exist_ok=True)
    cfg = get_config()
    conn = D.connect()
    srv = Server(conn, cfg)
    print(f"DB={cfg.db_path}", flush=True)
    print(f"출력={out}  생성={a.only}  force={a.force}", flush=True)

    report = {
        "generator": "make_extend_fixtures.py",
        "as_of_date": time.strftime("%Y-%m-%d"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "params": vars(a) | {"only": a.only},
        "layout": {"search": "search/{slug}.json", "votes": "votes/{bill_no}.json",
                   "lifecycle": "lifecycle/{sig_cd}.json",
                   "statute": "statute/{instrument_id with ':'→'-'}.json",
                   "verification": "verification/summary.json",
                   "succession": "succession.json",
                   "catalogs": ["search_index.json", "votes_index.json",
                                "lifecycle_index.json", "statute_index.json",
                                "extend_index.json"]},
        "warnings": [], "errors": [],
    }
    ctx = Ctx(conn, srv, out, a, report)

    t0 = time.time()
    for kind in a.only:
        (out / kind).mkdir(parents=True, exist_ok=True)
        try:
            BUILDERS[kind](ctx)
        except Exception as e:  # noqa: BLE001
            ctx.fail(kind, "*", e)

    # 산출 요약
    totals = {}
    for kind in KINDS:
        n, b = dir_bytes(out / kind)
        if n:
            totals[kind] = {"files": n, "bytes": b}
    for name in ("search_index.json", "votes_index.json", "lifecycle_index.json",
                 "statute_index.json", "succession.json"):
        f = out / name
        if f.exists():
            totals.setdefault("catalogs", {"files": 0, "bytes": 0})
            totals["catalogs"]["files"] += 1
            totals["catalogs"]["bytes"] += f.stat().st_size
    report["totals"] = {"by_kind": totals,
                        "files": sum(v["files"] for v in totals.values()),
                        "bytes": sum(v["bytes"] for v in totals.values()),
                        "seconds": round(time.time() - t0, 1),
                        "errors": len(report["errors"]),
                        "warnings": len(report["warnings"])}
    write_json(out / "extend_index.json", report)

    print("─" * 70, flush=True)
    for kind, v in totals.items():
        print(f"  {kind:<14} {v['files']:>4} 파일  {human(v['bytes'])}", flush=True)
    api_files, api_bytes = dir_bytes(out)
    print(f"이번 산출 {report['totals']['files']} 파일 · "
          f"{human(report['totals']['bytes'])} · {report['totals']['seconds']}s"
          f" · 경고 {len(report['warnings'])} · 실패 {len(report['errors'])}", flush=True)
    print(f"api/ 전체 {api_files} 파일 · {human(api_bytes)}", flush=True)
    for w in report["warnings"]:
        print(f"  [경고] {w['kind']}/{w['key']}: {w['message']}", flush=True)
    for e in report["errors"][:10]:
        print(f"  [실패] {e}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
