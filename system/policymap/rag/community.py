"""policymap.rag.community — 커뮤니티 요약 계층(Microsoft GraphRAG 방식, LLM 불필요).

지역 전문검색(local search)은 "이 질의에 맞는 조문"을 찾지만, "전국에서 반려동물 조례는
어떤 패턴으로 퍼졌나" 같은 **전역 질의(global search)** 에는 답하지 못한다. GraphRAG 는
이를 그래프 커뮤니티 → 커뮤니티 요약 → 요약 위 검색으로 푼다. 이 모듈이 그 계층이다.

파이프라인:
  1) graph.analysis.detect_communities 재사용 (scope='ordinance_similarity' | 'region_adjacency')
  2) 커뮤니티별 통계 집계 — 대표 조례, 지자체 분포, 카테고리, 제정 연도 히스토그램,
     인접 지자체 비율(공간 확산 vs 전국 동시 확산 판별), 연계 예산 규모
  3) **전국 투영(nationwide projection)** — 커뮤니티의 지배 앵커(예 '반려동물')로
     ordinances 전체 159K 건을 역조회. 유사도 그래프는 본문 확보분(1,087건)만 덮지만,
     앵커 투영은 본문 없는 조례까지 포함한 진짜 전국 확산 곡선을 만든다.
  4) 템플릿 서술문 생성(LLM 없이) + 요약 위 BM25 검색(global_search)

산출물은 data/index/communities/{scope}.json 에 저장하고 재사용한다.
DB 는 읽기 전용으로만 접근한다.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .. import db as _db
from .. import util as _util
from ..graph.analysis import detect_communities
from .index import Tokenizer, default_index_root
from .retrieve import anchor_terms

_LOG = _util.get_logger("policymap.rag.community")

REPORT_VERSION = 1
DEFAULT_SCOPE = "ordinance_similarity"
MIN_COMMUNITY_SIZE = 3
MAX_COMMUNITIES = 60


# --------------------------------------------------------------------------- #
# 0) 헬퍼
# --------------------------------------------------------------------------- #
def _fetch(conn, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    try:
        return _db.fetchall(conn, sql, params)
    except Exception:  # noqa: BLE001 — 스키마 부분 부재 방어
        return []


def _in(n: int) -> str:
    return ",".join("?" * n)


def _year(yyyymmdd: Optional[str]) -> Optional[int]:
    s = str(yyyymmdd or "")
    if len(s) >= 4 and s[:4].isdigit():
        y = int(s[:4])
        if 1948 <= y <= 2100:
            return y
    return None


def _report_path(scope: str, index_dir=None) -> Path:
    base = Path(index_dir) if index_dir else default_index_root()
    return base / "communities" / f"{scope}.json"


# --------------------------------------------------------------------------- #
# 1) 커뮤니티별 집계
# --------------------------------------------------------------------------- #
def _ordinance_rows(conn, ids: list[str]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        out += _fetch(conn,
                      "SELECT o.ordinance_id, o.name, o.region_id, o.org_name, o.ord_kind, "
                      "       o.enacted_on, o.official_url, o.verification_status, "
                      "       r.name AS region_name, r.level AS region_level, "
                      "       r.parent_region AS parent_region, r.sig_cd "
                      "FROM ordinances o LEFT JOIN regions r ON r.region_id = o.region_id "
                      f"WHERE o.ordinance_id IN ({_in(len(chunk))})", tuple(chunk))
    return out


def _degree_map(conn, ids: list[str]) -> dict[str, float]:
    """커뮤니티 내 유사도 가중 연결중심성(대표 조례 선정용)."""
    deg: dict[str, float] = {}
    idset = set(ids)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for r in _fetch(conn,
                        "SELECT src_id, dst_id, cosine_sim FROM similarity_edges "
                        f"WHERE src_id IN ({_in(len(chunk))})", tuple(chunk)):
            if r["dst_id"] in idset:
                deg[r["src_id"]] = deg.get(r["src_id"], 0.0) + float(r["cosine_sim"] or 0.0)
    return deg


def _adjacency_ratio(conn, region_ids: list[str]) -> Optional[float]:
    """커뮤니티가 차지한 지자체들 사이의 인접 쌍 비율.

    높으면 '이웃 따라 번진' 공간적 확산, 낮으면 '전국 동시(상위법·중앙 지침 주도)' 확산.
    """
    regs = sorted({r for r in region_ids if r})
    if len(regs) < 2:
        return None
    edges = 0
    for i in range(0, len(regs), 400):
        chunk = regs[i:i + 400]
        rows = _fetch(conn,
                      f"SELECT region_id, neighbor_id FROM region_adjacency "
                      f"WHERE region_id IN ({_in(len(chunk))})", tuple(chunk))
        rs = set(regs)
        edges += sum(1 for r in rows if r["neighbor_id"] in rs)
    pairs = len(regs) * (len(regs) - 1)          # 방향 2행 저장이므로 순서쌍으로 계산
    return round(edges / pairs, 6) if pairs else None


def _nationwide_projection(conn, anchor: str, *, limit_years: int = 40) -> dict:
    """앵커 문자열로 ordinances 전체를 역조회 → 진짜 전국 확산 곡선.

    유사도 그래프(본문 확보분)로는 볼 수 없는, 본문 미수집 조례까지 포함한다.
    """
    like = f"%{anchor}%"
    row = _fetch(conn,
                 "SELECT COUNT(*) AS n, COUNT(DISTINCT region_id) AS regions "
                 "FROM ordinances WHERE status='active' AND name LIKE ?", (like,))
    total = int(row[0]["n"]) if row else 0
    regions = int(row[0]["regions"]) if row else 0
    years = _fetch(conn,
                   "SELECT substr(enacted_on,1,4) AS y, COUNT(*) AS n FROM ordinances "
                   "WHERE status='active' AND name LIKE ? AND enacted_on IS NOT NULL "
                   "GROUP BY y ORDER BY y", (like,))
    hist = {}
    for r in years:
        y = _year(r["y"])
        if y:
            hist[y] = hist.get(y, 0) + int(r["n"])
    first = _fetch(conn,
                   "SELECT o.ordinance_id, o.name, o.enacted_on, o.org_name, r.name AS region_name "
                   "FROM ordinances o LEFT JOIN regions r ON r.region_id=o.region_id "
                   "WHERE o.status='active' AND o.name LIKE ? AND o.enacted_on IS NOT NULL "
                   "AND length(o.enacted_on)>=4 ORDER BY o.enacted_on ASC LIMIT 3", (like,))
    peak_year = max(hist, key=lambda y: hist[y]) if hist else None
    ordered = sorted(hist.items())[-limit_years:]
    return {
        "anchor": anchor,
        "ordinances_nationwide": total,
        "regions_nationwide": regions,
        "year_histogram": [{"year": y, "n": n} for y, n in ordered],
        "first_enacted": [{"ordinance_id": f["ordinance_id"], "name": f["name"],
                           "enacted_on": f["enacted_on"],
                           "region": f.get("region_name") or f.get("org_name")}
                          for f in first],
        "peak_year": peak_year,
        "peak_count": hist.get(peak_year) if peak_year else None,
    }


def _budget_rollup(conn, ids: list[str]) -> dict:
    tot_alloc = tot_exe = 0
    n = 0
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        rows = _fetch(conn,
                      "SELECT COUNT(*) AS n, COALESCE(SUM(b.alloc_amt),0) AS a, "
                      "COALESCE(SUM(b.exe_amt),0) AS e FROM ordinance_budget_link l "
                      "JOIN budget_lines b ON b.budget_id=l.budget_id "
                      f"WHERE l.ordinance_id IN ({_in(len(chunk))})", tuple(chunk))
        if rows:
            n += int(rows[0]["n"] or 0)
            tot_alloc += int(rows[0]["a"] or 0)
            tot_exe += int(rows[0]["e"] or 0)
    return {"linked_budget_lines": n, "alloc_amt": tot_alloc, "exe_amt": tot_exe}


def _name_pattern(names: list[str], *, top: int = 6) -> list[dict]:
    """커뮤니티 조례명에서 지배 어휘(앵커) 추출 — 커뮤니티 '주제 라벨'."""
    tally: dict[str, int] = {}
    for nm in names:
        for a in anchor_terms(nm, top=6):
            tally[a] = tally.get(a, 0) + 1
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    n = max(1, len(names))
    return [{"term": t, "count": c, "share": round(c / n, 4)} for t, c in ranked[:top]]


def _josa(word: str, with_batchim: str, without_batchim: str) -> str:
    """받침 유무에 따른 조사 선택('반려동물를' → '반려동물을')."""
    if not word:
        return without_batchim
    ch = word[-1]
    if "가" <= ch <= "힣":
        return with_batchim if (ord(ch) - 0xAC00) % 28 else without_batchim
    return without_batchim


def _narrative(summary: dict) -> str:
    """LLM 없이 템플릿으로 커뮤니티 서술문 생성(전역 질의 검색 대상 텍스트이기도 함)."""
    label = summary["label"]
    size = summary["size"]
    regs = summary["region_count"]
    span = summary.get("year_span") or {}
    nat = summary.get("nationwide") or {}
    parts = [f"[커뮤니티 {summary['id']}] 주제 '{label}' — 유사도 그래프 상 조례 {size}건, "
             f"지자체 {regs}곳."]
    if span.get("first") and span.get("last"):
        parts.append(f"제정 시기 {span['first']}~{span['last']}년"
                     + (f", 최다 제정 {span['peak']}년({span.get('peak_count')}건)."
                        if span.get("peak") else "."))
    ar = summary.get("adjacency_ratio")
    if ar is not None:
        if ar >= 0.08:
            parts.append(f"소속 지자체 간 인접 비율 {ar:.3f} — 이웃 지자체를 따라 번진 "
                         "공간적 확산 성격이 강하다.")
        else:
            parts.append(f"소속 지자체 간 인접 비율 {ar:.3f} — 지리적으로 흩어져 있어 "
                         "상위법 위임·중앙 지침에 따른 전국 동시 확산에 가깝다.")
    if nat.get("ordinances_nationwide"):
        parts.append(f"전국 투영: 명칭에 '{nat['anchor']}'{_josa(nat['anchor'], '을', '를')} 포함한 현행 조례가 "
                     f"{nat['ordinances_nationwide']}건, {nat['regions_nationwide']}개 지자체에 "
                     f"존재한다.")
        if nat.get("first_enacted"):
            f0 = nat["first_enacted"][0]
            parts.append(f"최초 제정은 {f0.get('region') or '?'} '{f0['name']}'"
                         f"({f0['enacted_on']}).")
        if nat.get("peak_year"):
            parts.append(f"제정 피크는 {nat['peak_year']}년({nat['peak_count']}건).")
    if summary.get("categories"):
        cats = ", ".join(f"{c['name'] or c['code']}({c['n']})" for c in summary["categories"][:3])
        parts.append(f"분류: {cats}.")
    bud = summary.get("budget") or {}
    if bud.get("linked_budget_lines"):
        parts.append(f"연계 예산사업 {bud['linked_budget_lines']}건, "
                     f"편성 {bud['alloc_amt']:,}원 / 지출 {bud['exe_amt']:,}원.")
    if summary.get("representatives"):
        reps = "; ".join(f"{r['name']}({r.get('region_name') or r.get('org_name') or '?'})"
                         for r in summary["representatives"][:3])
        parts.append(f"대표 조례: {reps}.")
    if summary.get("top_provinces"):
        prov = ", ".join(f"{p['name']}({p['n']})" for p in summary["top_provinces"][:3])
        parts.append(f"광역 분포: {prov}.")
    return " ".join(parts)


def _province_names(conn) -> dict[str, str]:
    return {r["region_id"]: r["name"]
            for r in _fetch(conn, "SELECT region_id, name FROM regions WHERE level=1")}


def _summarize_ordinance_community(conn, cid: int, members: list[str], *,
                                   provinces: dict[str, str], top_reps: int = 5) -> dict:
    rows = _ordinance_rows(conn, members)
    if not rows:
        return {}
    names = [r["name"] or "" for r in rows]
    pattern = _name_pattern(names)
    label = pattern[0]["term"] if pattern else "기타"

    deg = _degree_map(conn, members)
    rows.sort(key=lambda r: (-deg.get(r["ordinance_id"], 0.0), r["name"] or ""))
    reps = [{"ordinance_id": r["ordinance_id"], "name": r["name"],
             "region_id": r["region_id"], "region_name": r.get("region_name"),
             "org_name": r.get("org_name"), "enacted_on": r.get("enacted_on"),
             "official_url": r.get("official_url"),
             "centrality": round(deg.get(r["ordinance_id"], 0.0), 4)}
            for r in rows[:top_reps]]

    region_ids = [r["region_id"] for r in rows if r.get("region_id")]
    prov_tally: dict[str, int] = {}
    for r in rows:
        p = r.get("parent_region") or (r.get("region_id") or "")[:2]
        if p:
            prov_tally[p] = prov_tally.get(p, 0) + 1
    top_prov = [{"region_id": p, "name": provinces.get(p, p), "n": n}
                for p, n in sorted(prov_tally.items(), key=lambda kv: -kv[1])[:5]]

    years = [y for y in (_year(r.get("enacted_on")) for r in rows) if y]
    hist: dict[int, int] = {}
    for y in years:
        hist[y] = hist.get(y, 0) + 1
    peak = max(hist, key=lambda y: hist[y]) if hist else None
    span = {"first": min(years) if years else None, "last": max(years) if years else None,
            "peak": peak, "peak_count": hist.get(peak) if peak else None,
            "histogram": [{"year": y, "n": hist[y]} for y in sorted(hist)]}

    cats = _fetch(conn,
                  "SELECT oc.category_code AS code, c.name AS name, COUNT(*) AS n "
                  "FROM ordinance_category oc LEFT JOIN categories c ON c.code=oc.category_code "
                  f"WHERE oc.ordinance_id IN ({_in(len(members[:400]))}) "
                  "GROUP BY oc.category_code ORDER BY n DESC", tuple(members[:400]))

    summary = {
        "id": cid,
        "scope": "ordinance_similarity",
        "label": label,
        "size": len(members),
        "name_pattern": pattern,
        "representatives": reps,
        "region_count": len(set(region_ids)),
        "top_provinces": top_prov,
        "year_span": span,
        "adjacency_ratio": _adjacency_ratio(conn, region_ids),
        "categories": [{"code": c["code"], "name": c.get("name"), "n": int(c["n"])} for c in cats],
        "budget": _budget_rollup(conn, members),
        "nationwide": _nationwide_projection(conn, label) if label != "기타" else {},
        "members_sample": members[:20],
    }
    summary["narrative"] = _narrative(summary)
    return summary


def _summarize_region_community(conn, cid: int, members: list[str], *,
                                provinces: dict[str, str]) -> dict:
    rows = _fetch(conn,
                  "SELECT region_id, name, full_name, level, parent_region, population "
                  f"FROM regions WHERE region_id IN ({_in(len(members[:400]))})",
                  tuple(members[:400]))
    prov_tally: dict[str, int] = {}
    for r in rows:
        p = r.get("parent_region") or (r["region_id"] or "")[:2]
        prov_tally[p] = prov_tally.get(p, 0) + 1
    top_prov = [{"region_id": p, "name": provinces.get(p, p), "n": n}
                for p, n in sorted(prov_tally.items(), key=lambda kv: -kv[1])[:5]]
    counts = _fetch(conn,
                    "SELECT region_id, COUNT(*) AS n FROM ordinances "
                    f"WHERE status='active' AND region_id IN ({_in(len(members[:400]))}) "
                    "GROUP BY region_id ORDER BY n DESC", tuple(members[:400]))
    label = top_prov[0]["name"] if top_prov else f"권역{cid}"
    summary = {
        "id": cid,
        "scope": "region_adjacency",
        "label": label,
        "size": len(members),
        "top_provinces": top_prov,
        "regions": [{"region_id": r["region_id"], "name": r.get("name"),
                     "full_name": r.get("full_name"), "level": r.get("level")}
                    for r in rows[:12]],
        "ordinance_counts": [{"region_id": c["region_id"], "n": int(c["n"])} for c in counts[:10]],
        "ordinance_total": sum(int(c["n"]) for c in counts),
    }
    prov_txt = ", ".join(f"{p['name']}({p['n']})" for p in top_prov[:3])
    names = ", ".join(r.get("name") or "" for r in rows[:6])
    summary["narrative"] = (
        f"[권역 커뮤니티 {cid}] 인접성으로 묶인 지자체 {len(members)}곳 — 광역 분포 {prov_txt}. "
        f"구성 예: {names}. 현행 자치법규 합계 {summary['ordinance_total']:,}건.")
    return summary


# --------------------------------------------------------------------------- #
# 2) 리포트 빌드/로드
# --------------------------------------------------------------------------- #
def build_community_report(conn, *, scope: str = DEFAULT_SCOPE,
                           min_size: int = MIN_COMMUNITY_SIZE,
                           max_communities: int = MAX_COMMUNITIES,
                           seed: int = 2026, index_dir=None,
                           save: bool = True) -> dict:
    """커뮤니티 탐지 → 커뮤니티별 템플릿 요약 → JSON 리포트 저장.

    scope='ordinance_similarity' : 조례 주제 군집(전역 '어떤 패턴으로 퍼졌나' 질의용)
    scope='region_adjacency'     : 지리 권역 군집(벤치마킹 그룹용)
    반환 {'scope','backend','modularity','num_communities','communities':[...], ...}
    """
    t0 = time.time()
    det = detect_communities(conn, scope=scope, seed=seed)
    provinces = _province_names(conn)
    summaries: list[dict] = []
    for c in det.get("communities", []):
        if c["size"] < min_size:
            continue
        if scope == "ordinance_similarity":
            s = _summarize_ordinance_community(conn, c["id"], c["members"], provinces=provinces)
        else:
            s = _summarize_region_community(conn, c["id"], c["members"], provinces=provinces)
        if s:
            summaries.append(s)
        if len(summaries) >= max_communities:
            break

    report = {
        "version": REPORT_VERSION,
        "scope": scope,
        "backend": det.get("backend"),
        "modularity": det.get("modularity"),
        "num_communities_detected": det.get("num_communities"),
        "num_communities_summarized": len(summaries),
        "min_size": min_size,
        "communities": summaries,
        "as_of_date": _util.today_kst(),
        "built_at": _util.now_kst_iso(),
        "elapsed_sec": round(time.time() - t0, 2),
    }
    if save:
        path = _report_path(scope, index_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["path"] = str(path)
        report["bytes"] = path.stat().st_size
    return report


_REPORT_CACHE: dict[str, tuple[float, dict]] = {}


def load_community_report(scope: str = DEFAULT_SCOPE, *, index_dir=None,
                          cache: bool = True) -> Optional[dict]:
    """저장된 커뮤니티 리포트 로드. 없으면 None.

    answer_context 가 질의마다 호출하므로 mtime 기반 프로세스 캐시를 둔다
    (파일이 갱신되면 자동으로 다시 읽는다).
    """
    path = _report_path(scope, index_dir)
    if not path.exists():
        _REPORT_CACHE.pop(str(path), None)
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    if cache:
        hit = _REPORT_CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if cache:
        _REPORT_CACHE[key] = (mtime, rep)
    return rep


def community_summaries(conn=None, *, scope: str = DEFAULT_SCOPE, rebuild: bool = False,
                        index_dir=None, **kwargs) -> list[dict]:
    """커뮤니티 요약 목록. 리포트가 없고 conn 이 있으면 즉석 빌드."""
    if not rebuild:
        rep = load_community_report(scope, index_dir=index_dir)
        if rep:
            return rep.get("communities", [])
    if conn is None:
        return []
    return build_community_report(conn, scope=scope, index_dir=index_dir,
                                  **kwargs).get("communities", [])


# --------------------------------------------------------------------------- #
# 3) 전역 검색 — 커뮤니티 요약 위의 BM25
# --------------------------------------------------------------------------- #
def _summary_text(s: dict) -> str:
    bits = [s.get("label") or "", s.get("narrative") or ""]
    bits += [p["term"] for p in s.get("name_pattern", [])]
    bits += [r.get("name") or "" for r in s.get("representatives", [])]
    bits += [p.get("name") or "" for p in s.get("top_provinces", [])]
    bits += [c.get("name") or c.get("code") or "" for c in s.get("categories", [])]
    return " ".join(b for b in bits if b)


def global_search(conn=None, query: str = "", k: int = 3, *,
                  scopes: Iterable[str] = ("ordinance_similarity",),
                  index_dir=None, rebuild: bool = False, k1: float = 1.2,
                  b: float = 0.6, min_score_ratio: float = 0.35,
                  max_df_ratio: float = 0.9) -> list[dict]:
    """전역 질의 → 관련 커뮤니티 요약 상위 k건.

    "전국에서 반려동물 조례는 어떤 패턴으로 퍼졌나" 처럼 개별 조문이 아니라 **패턴**을
    묻는 질의에 답하기 위한 계층. 커뮤니티 요약문 코퍼스(수십~수백 건)에 소형 BM25 를
    적용한다(코퍼스가 작아 별도 인덱스 파일 없이 즉석 계산).
    """
    docs: list[dict] = []
    for sc in scopes:
        for s in community_summaries(conn, scope=sc, index_dir=index_dir, rebuild=rebuild):
            docs.append(s)
    if not docs or not query:
        return []

    tok = Tokenizer()
    corpus = [tok.tf(_summary_text(d)) for d in docs]
    lens = [sum(t.values()) or 1 for t in corpus]
    avgdl = sum(lens) / len(lens)
    df: dict[str, int] = {}
    for t in corpus:
        for term in t:
            df[term] = df.get(term, 0) + 1
    n = len(corpus)
    qtf = tok.tf(query)

    # 주제 앵커 게이트 — 점수 문턱만으로는 "전부 약한 매칭"과 "전부 강한 매칭"을 못 가른다.
    # (실측: '산후조리…'와 '청년 주거…'가 '지원' 하나로 똑같이 0.7469를 받았다.)
    # 질의의 주제 앵커('산후조리','반려동물','출산장려금')가 요약문에 실제로 등장하는
    # 커뮤니티만 남긴다. 앵커는 접미를 한 글자씩 줄여가며(출산장려금→출산장려→출산장)
    # 부분일치도 허용해 표기 차이를 흡수한다.
    q_anchors = anchor_terms(query, top=4)

    def _anchor_hit(text: str) -> bool:
        if not q_anchors:
            return True
        for a in q_anchors:
            for cut in range(len(a), 1, -1):
                if a[:cut] in text:
                    return True
        return False

    gate = [_anchor_hit(_summary_text(d)) for d in docs]

    # 요약 코퍼스가 10~수십 건이라 '지원'처럼 전 문서에 있는 어휘도 idf 가 0 이 아니다.
    # 그런 항이 수십 개 누적되면 주제가 전혀 다른 커뮤니티도 점수를 얻는다(실측 0.73).
    # 거의 모든 문서에 나오는 항은 아예 배제해 '변별어가 하나도 안 맞으면 0점'이 되게 한다.
    df_cap = max(1, int(n * max_df_ratio))

    scored: list[tuple[float, int]] = []
    for i, t in enumerate(corpus):
        if not gate[i]:
            continue
        sc = 0.0
        for term, qn in qtf.items():
            f = t.get(term)
            if not f or df[term] > df_cap:
                continue
            idf = math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
            sc += idf * (f * (k1 + 1.0)) / (f + k1 * (1.0 - b + b * lens[i] / avgdl)) \
                * ((k1 + 1.0) * qn / (k1 + qn))
        if sc > 0:
            scored.append((sc, i))
    scored.sort(key=lambda x: (-x[0], x[1]))
    # 코퍼스가 수십 건뿐이라 '지원' 같은 흔한 어휘도 IDF 가 살아있다 → 약한 매칭이 k개를
    # 채우려고 딸려온다. 최고점 대비 비율 하한으로 무관한 커뮤니티를 잘라낸다.
    if scored and min_score_ratio > 0:
        floor = scored[0][0] * min_score_ratio
        scored = [x for x in scored if x[0] >= floor]

    out = []
    for rank, (sc, i) in enumerate(scored[:k], start=1):
        d = docs[i]
        out.append({
            "rank": rank,
            "score": round(sc, 4),
            "community_id": d.get("id"),
            "scope": d.get("scope"),
            "label": d.get("label"),
            "size": d.get("size"),
            "region_count": d.get("region_count"),
            "narrative": d.get("narrative"),
            "name_pattern": d.get("name_pattern", [])[:5],
            "year_span": d.get("year_span"),
            "adjacency_ratio": d.get("adjacency_ratio"),
            "nationwide": d.get("nationwide"),
            "representatives": d.get("representatives", [])[:3],
            "budget": d.get("budget"),
            "_engine": "policymap.rag.community.global_search",
        })
    return out


__all__ = [
    "build_community_report", "load_community_report", "community_summaries",
    "global_search",
]
