"""policymap.analytics.peers — 유사 지자체(peer group) 재정의.

문제의식(직전 라운드 실측)
  기존 graph.analysis.find_peer_governments 는
    (1) '정책구조' 특성을 ordinance_category(159,452건 중 1,087건=0.68%, 코드 2종)
        위에서 계산해 후보 226곳 구조유사도가 평균 0.9402 — 변별력이 0인데 가중치 1/3,
    (2) level 만 맞추고 자치구/시/군을 섞어 서대문구의 peer 로 달성군·경산시가 나오며,
    (3) level=3(일반구)은 _cosine({}, {}) 가 0.0 을 돌려주는 탓에 전원 similarity 0.0 을
        '정답인 양' 반환하고,
    (4) 콜드 캐시 호출당 18.2초가 걸린다.

본 모듈의 재정의
  * **유형 사전분할**: 행정안전부 유사자치단체 분류의 1차 축(자치구/시/군)을 먼저 나눈다.
    행안부는 특별·광역시 자치구, 시, 군을 구분한 뒤 인구 등으로 13유형으로 세분한다
    (출처: 지방재정365 lofin365.go.kr 의 유사단체 비교 안내. 원 고시문 미확인).
  * **재정·인구·면적·복지 지표**를 가중 결합. 각 지표는 유형 내 z-표준화.
  * **정책 프로파일**은 ordinance_category 대신 조례명 TF-IDF(전수 적용 가능)로 교체.
  * **level=3 가드**: 일반구는 조례 제정권이 없으므로 빈 결과 + 사유를 반환한다.
  * **물화(materialize)**: region_features 테이블로 O(1) 조회 — 웹 데모 응답성.

가중치 출처 표기 규칙
  DEFAULT_WEIGHTS 의 provenance 필드에 'mois_public'(지방재정365 공개 설명에서 확인) /
  'ours'(본 구현이 배분한 값, 행안부 원값 미확인) 를 명시한다. 발표 시 그대로 인용할 것.
"""
from __future__ import annotations

import math
import re
import sqlite3
from typing import Any, Optional

from . import base as _base

# --------------------------------------------------------------------------- #
# 지표 정의와 가중치
# --------------------------------------------------------------------------- #
INDICATORS: dict[str, dict] = {
    "population":        {"log": True,  "label": "인구"},
    "area_km2":          {"log": True,  "label": "면적"},
    "fiscal_self_ratio": {"log": False, "label": "자체재원비율(재정력 대용)"},
    "welfare_ratio":     {"log": False, "label": "사회복지비 비중"},
    "budget_total":      {"log": True,  "label": "예산총액"},
}

DEFAULT_WEIGHTS: dict[str, float] = {
    "population": 0.25,          # provenance: ours (행안부 원 가중치 미확인)
    "area_km2": 0.13,            # provenance: ours
    "fiscal_self_ratio": 0.30,   # provenance: mois_public (지방재정365 공개 설명)
    "welfare_ratio": 0.07,       # provenance: mois_public
    "budget_total": 0.25,        # provenance: ours (재정규모 축)
}
WEIGHT_PROVENANCE = {
    "population": "ours", "area_km2": "ours", "budget_total": "ours",
    "fiscal_self_ratio": "mois_public", "welfare_ratio": "mois_public",
}
MISSING_MOIS_INDICATORS = ["인구증감률", "고령인구비율", "조출생률"]  # KOSIS 미확보

FEATURES_DDL = """
CREATE TABLE IF NOT EXISTS region_features (
  region_id TEXT PRIMARY KEY,
  sig_cd TEXT, name TEXT, full_name TEXT, level INTEGER,
  rtype TEXT, sido_cd TEXT,
  population REAL, area_km2 REAL, pop_density REAL,
  budget_total REAL, fiscal_self_ratio REAL, welfare_ratio REAL,
  ordinance_count INTEGER,
  fyr INTEGER, computed_at TEXT
)
"""


# --------------------------------------------------------------------------- #
# 물화
# --------------------------------------------------------------------------- #
def materialize_region_features(conn: sqlite3.Connection, *, level: int = 2,
                                fyr: int = 2025) -> dict:
    """지자체 특성을 region_features 테이블로 물화(짧은 단일 트랜잭션).

    기존 find_peer_governments 는 호출마다 전 지자체 집계를 재계산해 콜드 18.2초가
    걸렸다. 이 테이블이 있으면 조회는 O(1) 이 된다.
    """
    cov = _base.region_covariates(conn, level=level, fyr=fyr)
    now = _iso_now()
    rows = [(c["region_id"], c.get("sig_cd"), c.get("name"), c.get("full_name"), level,
             c.get("rtype"), c.get("sido_cd"), c.get("population"), c.get("area_km2"),
             c.get("pop_density"), c.get("budget_total"), c.get("fiscal_self_ratio"),
             c.get("welfare_ratio"), c.get("ordinance_count"), fyr, now)
            for c in cov.values()]
    conn.execute(FEATURES_DDL)
    conn.executemany(
        "INSERT OR REPLACE INTO region_features "
        "(region_id,sig_cd,name,full_name,level,rtype,sido_cd,population,area_km2,"
        " pop_density,budget_total,fiscal_self_ratio,welfare_ratio,ordinance_count,"
        " fyr,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return {"table": "region_features", "rows": len(rows), "level": level, "fyr": fyr,
            "computed_at": now}


def _iso_now() -> str:
    from .. import util as _util
    try:
        return _util.now_kst_iso()
    except Exception:  # pragma: no cover
        import datetime
        return datetime.datetime.now().isoformat(timespec="seconds")


def load_region_features(conn: sqlite3.Connection, *, level: int = 2,
                         fyr: int = 2025) -> dict[str, dict]:
    """물화 테이블이 있으면 그것을, 없으면 즉석 계산을 반환."""
    try:
        cur = conn.execute("SELECT * FROM region_features WHERE level=? AND fyr=?",
                           (level, fyr))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if rows:
            return {r["region_id"]: r for r in rows}
    except sqlite3.OperationalError:
        pass
    return _base.region_covariates(conn, level=level, fyr=fyr)


# --------------------------------------------------------------------------- #
# 유사도
# --------------------------------------------------------------------------- #
def _zscores(feats: dict[str, dict], ids: list[str], key: str, use_log: bool
             ) -> dict[str, float]:
    vals = {}
    for rid in ids:
        v = feats.get(rid, {}).get(key)
        if v is None or (use_log and v <= 0):
            continue
        vals[rid] = math.log(v) if use_log else float(v)
    if len(vals) < 2:
        return {}
    mu = sum(vals.values()) / len(vals)
    sd = math.sqrt(sum((x - mu) ** 2 for x in vals.values()) / len(vals))
    if sd == 0:
        return {rid: 0.0 for rid in vals}
    return {rid: (x - mu) / sd for rid, x in vals.items()}


def find_similar_governments(
    conn: sqlite3.Connection,
    sig_cd: str,
    *,
    k: int = 10,
    weights: Optional[dict[str, float]] = None,
    policy_profile_weight: float = 0.0,
    partition_by_type: bool = True,
    min_coverage: float = 0.6,
    tfidf: Optional[dict[str, dict[str, float]]] = None,
    features: Optional[dict[str, dict]] = None,
) -> dict:
    """행안부 유사자치단체 기준에 정렬한 peer Top-k.

    policy_profile_weight > 0 이면 조례명 TF-IDF 코사인을 그 가중치로 혼합한다
    (행안부 기준에는 없는 우리 확장 — mode 를 명시해 보고할 것).

    반환 {"target": {...}, "peers": [...], "method": {...}} 또는
    비교 불가 시 {"peers": [], "reason": ...}.
    """
    tgt = _lookup_region(conn, sig_cd)
    if not tgt:
        return {"peers": [], "reason": f"region not found: {sig_cd}"}
    level = int(tgt["level"])
    if level == 3 or not tgt.get("has_legislation"):
        return {
            "target": {"sig_cd": sig_cd, "name": tgt.get("full_name"), "level": level},
            "peers": [],
            "reason": "일반구(level=3)는 지방자치법상 조례 제정권이 없어 비교 대상이 아니다. "
                      f"모(母) 자치단체 {tgt.get('parent_region')} 로 조회하라.",
            "parent_region": tgt.get("parent_region"),
        }
    if tgt.get("status") != "active":
        return {"peers": [], "reason": f"현행 지자체가 아니다(status={tgt.get('status')})"}

    W = dict(weights or DEFAULT_WEIGHTS)
    feats = features if features is not None else load_region_features(conn, level=level)
    if tgt["region_id"] not in feats:
        return {"peers": [], "reason": "대상 지자체 특성 없음"}
    trec = feats[tgt["region_id"]]

    ids = list(feats)
    if partition_by_type:
        ids = [r for r in ids if feats[r].get("rtype") == trec.get("rtype")]
    if tgt["region_id"] not in ids:
        ids.append(tgt["region_id"])

    z = {key: _zscores(feats, ids, key, INDICATORS[key]["log"]) for key in W}
    if policy_profile_weight > 0 and tfidf is None:
        tfidf = _base.ordinance_name_tfidf(conn, level=level)

    tid = tgt["region_id"]
    out = []
    for rid in ids:
        if rid == tid:
            continue
        num, den, gaps = 0.0, 0.0, {}
        for key, w in W.items():
            zt, zc = z[key].get(tid), z[key].get(rid)
            if zt is None or zc is None:
                continue
            num += w * (zt - zc) ** 2
            den += w
            gaps[key] = round(abs(zt - zc), 4)
        if den < min_coverage * sum(W.values()):
            continue          # 지표 결측이 많은 후보는 비교 불가로 제외(0.0 을 정답인 양 돌려주지 않는다)
        d = math.sqrt(num / den)
        sim = 1.0 / (1.0 + d)
        pol = None
        if policy_profile_weight > 0 and tfidf:
            pol = _base.cosine(tfidf.get(tid, {}), tfidf.get(rid, {}))
        if pol is not None:
            sim = (1 - policy_profile_weight) * sim + policy_profile_weight * pol
        f = feats[rid]
        out.append({
            "region_id": rid, "sig_cd": f.get("sig_cd"),
            "name": f.get("full_name") or f.get("name"),
            "rtype": f.get("rtype"),
            "similarity": round(sim, 6),
            "weighted_distance": round(d, 6),
            "policy_profile_cosine": round(pol, 4) if pol is not None else None,
            "indicator_gap_sd": gaps,
            "indicators": {key: f.get(key) for key in INDICATORS},
        })
    out.sort(key=lambda r: r["similarity"], reverse=True)
    return {
        "target": {"region_id": tid, "sig_cd": tgt.get("sig_cd"),
                   "name": tgt.get("full_name"), "rtype": trec.get("rtype"),
                   "indicators": {key: trec.get(key) for key in INDICATORS}},
        "peers": out[:k],
        "method": {
            "partition_by_type": partition_by_type,
            "candidate_pool": len(ids) - 1,
            "weights": W,
            "weight_provenance": WEIGHT_PROVENANCE,
            "policy_profile_weight": policy_profile_weight,
            "min_indicator_coverage": min_coverage,
            "missing_mois_indicators": MISSING_MOIS_INDICATORS,
            "note": "지표는 동일 유형 내 z-표준화 후 가중 유클리드. sim=1/(1+d).",
        },
    }


def _lookup_region(conn: sqlite3.Connection, sig_cd: str) -> Optional[dict]:
    rows = _base._rows(
        conn,
        "SELECT region_id, sig_cd, name, full_name, level, parent_region, status, "
        "has_legislation FROM regions WHERE sig_cd=? ORDER BY (status='active') DESC, level",
        (sig_cd,))
    return rows[0] if rows else None


# --------------------------------------------------------------------------- #
# 방법 비교표
# --------------------------------------------------------------------------- #
def compare_peer_methods(conn: sqlite3.Connection, sig_cd: str, *, k: int = 10,
                         features: Optional[dict] = None,
                         tfidf: Optional[dict] = None) -> dict:
    """행안부정렬 / 정책프로파일혼합 / 기존(graph.analysis) 세 방식의 Top-k 비교.

    겹침률(Jaccard·overlap@k)과 각 방식이 뽑은 유형 구성(자치구/시/군)을 함께 낸다.
    """
    res: dict[str, Any] = {"sig_cd": sig_cd}
    a = find_similar_governments(conn, sig_cd, k=k, features=features)
    b = find_similar_governments(conn, sig_cd, k=k, policy_profile_weight=0.30,
                                 features=features, tfidf=tfidf)
    res["mois_aligned"] = a
    res["hybrid_policy"] = b

    legacy = None
    try:
        from ..graph import analysis as _ga
        legacy = _ga.find_peer_governments(conn, sig_cd, k=k)
    except Exception as exc:  # pragma: no cover
        res["legacy_error"] = str(exc)
    res["legacy"] = legacy

    def ids(x):
        if not x:
            return []
        return [p["region_id"] for p in (x["peers"] if isinstance(x, dict) else x)]

    sets = {"mois_aligned": ids(a), "hybrid_policy": ids(b), "legacy": ids(legacy)}
    ov = {}
    keys = [k_ for k_, v in sets.items() if v]
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            s1, s2 = set(sets[keys[i]]), set(sets[keys[j]])
            ov[f"{keys[i]}|{keys[j]}"] = {
                "overlap_at_k": len(s1 & s2),
                "jaccard": round(len(s1 & s2) / len(s1 | s2), 4) if (s1 | s2) else None,
            }
    res["overlap"] = ov

    # 유형 구성 진단(기존 방식의 자치구/시/군 혼합 문제 정량화)
    feats = features if features is not None else load_region_features(conn)
    tgt_type = feats.get(a.get("target", {}).get("region_id"), {}).get("rtype")
    comp = {}
    for name, lst in sets.items():
        if not lst:
            continue
        types = [feats.get(r, {}).get("rtype") for r in lst]
        comp[name] = {
            "types": {t: types.count(t) for t in set(types) if t},
            "same_type_rate": round(sum(1 for t in types if t == tgt_type) / len(types), 4),
        }
    res["target_type"] = tgt_type
    res["type_composition"] = comp
    return res


__all__ = ["find_similar_governments", "compare_peer_methods", "peer_matrix",
           "recommend_ordinances", "policy_key",
           "materialize_region_features", "load_region_features",
           "DEFAULT_WEIGHTS", "INDICATORS", "WEIGHT_PROVENANCE"]


# --------------------------------------------------------------------------- #
# peer 행렬 (EHA 공변량용)
# --------------------------------------------------------------------------- #
def neural_peer_matrix(conn: sqlite3.Connection, *, m: int = 20,
                       model: Optional[str] = None) -> dict[str, list[str]]:
    """그래프 신경망 임베딩 기준 Top-m 유사 지자체 (region_id -> [region_id]).

    peer_matrix 와 **같은 형식**을 돌려주므로 EHA 의 노출 변수로 그대로 꽂을 수 있다.
    셋을 한 모형에 같이 넣으면 확산 경로를 head-to-head 로 비교할 수 있다.

        neighbor_exposure  지리적 인접 — "옆에 있으니까 따라 한다"
        peer_exposure      행안부 유사자치단체(인구·재정 통계) — "형편이 비슷하니까"
        neural_exposure    그래프 구조 임베딩 — "조례 구성과 상위법 연결이 닮았으니까"

    셋째는 통계 지표가 아니라 **어떤 조례를 어떤 상위법 아래 두고 어떤 이웃과 붙어
    있는가** 라는 구조에서 학습된 유사도다. 앞의 둘이 못 보는 축이다.

    neural_similarity 에 Region 행이 없으면 빈 dict 를 돌려준다 —
    호출부는 이때 neural_exposure 를 None 으로 두고 모형에서 자동 제외한다.
    """
    sql = ("SELECT src_id, dst_id, cosine_sim FROM neural_similarity "
           "WHERE node_kind='Region'")
    params: list = []
    if model:
        sql += " AND model_name=?"
        params.append(model)
    sql += " ORDER BY src_id, cosine_sim DESC"
    out: dict[str, list[str]] = {}
    for r in _base._rows(conn, sql, params):
        src = str(r["src_id"]).split(":", 1)[-1]
        dst = str(r["dst_id"]).split(":", 1)[-1]
        lst = out.setdefault(src, [])
        if len(lst) < m and dst not in lst:
            lst.append(dst)
    return out


def peer_matrix(conn: sqlite3.Connection, *, level: int = 2, m: int = 20,
                weights: Optional[dict[str, float]] = None,
                partition_by_type: bool = True,
                min_coverage: float = 0.6) -> dict[str, list[str]]:
    """전 지자체에 대해 Top-m 유사 지자체 목록을 한 번에 계산(전체 O(n²), n=227).

    EHA 의 peer_exposure 공변량(=행안부 기준 유사단체 중 채택 비율)을 만드는 데 쓴다.
    지리적 인접(neighbor_exposure)과 **동일한 형식**의 노출 변수라 head-to-head 비교가 된다.
    """
    W = dict(weights or DEFAULT_WEIGHTS)
    feats = load_region_features(conn, level=level)
    ids = list(feats)
    z = {key: _zscores(feats, ids, key, INDICATORS[key]["log"]) for key in W}
    out: dict[str, list[str]] = {}
    for a in ids:
        cand = [b for b in ids if b != a and
                (not partition_by_type or feats[b].get("rtype") == feats[a].get("rtype"))]
        scored = []
        for b in cand:
            num, den = 0.0, 0.0
            for key, w in W.items():
                za, zb = z[key].get(a), z[key].get(b)
                if za is None or zb is None:
                    continue
                num += w * (za - zb) ** 2
                den += w
            if den < min_coverage * sum(W.values()):
                continue
            scored.append((math.sqrt(num / den), b))
        scored.sort()
        out[a] = [b for _, b in scored[:m]]
    return out


# --------------------------------------------------------------------------- #
# 실무 산출: 유사 지자체가 이미 만든 조례 중 우리에게 없는 것
# --------------------------------------------------------------------------- #
_KEY_TAIL = re.compile(
    r"\s*(시행규칙|시행세칙|규칙|조례|에\s*관한|에\s*관하여|에\s*대한|"
    r"을\s*위한|를\s*위한|등에\s*관한|등의|등\s*)\s*$")


def policy_key(name: Optional[str], region_name: Optional[str] = None,
               full_name: Optional[str] = None) -> str:
    """조례명 → 지자체명·법형식 접미를 걷어낸 '정책 키'.

    '서울특별시 종로구 자원봉사활동 지원 조례' → '자원봉사활동 지원'
    '서울특별시 서대문구 구세 감면에 관한 조례' → '구세 감면'
    지자체 간 동일 정책을 맞대응시키기 위한 최소 정규화다(형태소 분석 없음).
    '…에 관한/…을 위한' 같은 연결어미까지 걷어내지 않으면 같은 정책이 다른 키가 되어
    '이웃엔 있는데 우리엔 없다'는 오탐이 생긴다(실측: 서대문구 '구세 감면').
    """
    s = (name or "").strip()
    for pref in (full_name or "", region_name or ""):
        if pref and s.startswith(pref):
            s = s[len(pref):].strip()
            break
    else:
        s = re.sub(r"^[가-힣]+(특별자치시|특별자치도|특별시|광역시|도|시|군|구)\s+", "", s)
        s = re.sub(r"^[가-힣]+(시|군|구)\s+", "", s)
    prev = None
    while prev != s:
        prev = s
        s = _KEY_TAIL.sub("", s).strip()
    return re.sub(r"\s+", " ", s)


_CANON_PHRASE = re.compile(r"(등에\s*관한|에\s*관한|에\s*대한|을\s*위한|를\s*위한)")
_CANON_PUNCT = re.compile(r"[·ㆍ,/()\[\]「」<>\-—~ㆍ]+")
_CANON_TOKEN_DROP = {"및", "등", "등의", "의", "관한", "대한", "위한"}


def canon_key(key: str) -> str:
    """정책 키의 표기 변이를 흡수한 정규형.

    실측된 변이 예: '지위 향상'/'지위향상', '구매 촉진'/'구매촉진',
    '설치 및 운용'/'설치·운용', '생명존중문화 조성'/'생명존중 문화조성'.

    주의: 연결어(및·등·의)는 **어절 단위로만** 지운다. 글자 단위로 지우면
    '의회'→'회', '양성평등'→'양성평', '협의체'→'협체' 처럼 뜻이 망가진다.
    """
    s = _CANON_PHRASE.sub(" ", key or "")
    s = _CANON_PUNCT.sub(" ", s)
    return "".join(t for t in s.split() if t not in _CANON_TOKEN_DROP)


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def dice(a: str, b: str) -> float:
    """문자 bigram Dice 계수. canon_key 로 못 잡는 잔여 변이용."""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 1.0 if a == b else 0.0
    return 2 * len(A & B) / (len(A) + len(B))


def recommend_ordinances(
    conn: sqlite3.Connection,
    sig_cd: str,
    *,
    k: int = 15,
    min_peers: int = 3,
    limit: int = 30,
    same_sido_boost: bool = True,
    dup_threshold: float = 0.55,
    exclude_variants: bool = True,
    features: Optional[dict] = None,
) -> dict:
    """"우리와 비슷한 지역이 이미 만든, 우리에겐 없는 조례" 목록.

    peer 집합 정의는 find_similar_governments(행안부 유사자치단체 기준)를 따르되,
    same_sido_boost=True 면 동일 광역 소속 지자체를 peer 후보에 우선 포함한다.
    (실증 근거: 조례 프로파일 유사도는 전국 무작위 대비 동일 광역 무작위가 크게 높다 —
     광역 소속이 지리적 인접보다 강한 설명력을 가진다.)

    반환 {"target":..., "peers":[...], "recommendations":[{policy_key, peer_count,
          peers:[{name, ordinance_id, ordinance_name, enacted_on, url}], ...}]}
    """
    sim = find_similar_governments(conn, sig_cd, k=k, features=features)
    if not sim.get("peers"):
        return {"target": sim.get("target"), "peers": [], "recommendations": [],
                "reason": sim.get("reason")}
    tid = sim["target"]["region_id"]
    peer_ids = [p["region_id"] for p in sim["peers"]]
    feats = features if features is not None else load_region_features(conn)
    if same_sido_boost:
        my_sido = (feats.get(tid) or {}).get("sido_cd") or tid[:2]
        same = [r for r, f in feats.items()
                if r != tid and (f.get("sido_cd") or r[:2]) == my_sido
                and f.get("rtype") == (feats.get(tid) or {}).get("rtype")]
        for r in same:
            if r not in peer_ids:
                peer_ids.append(r)

    ids = [tid] + peer_ids
    ph = ",".join("?" for _ in ids)
    rows = _base._rows(
        conn,
        f"SELECT ordinance_id, region_id, name, enacted_on, official_url "
        f"FROM ordinances WHERE status='active' AND ord_kind='조례' "
        f"AND region_id IN ({ph})", ids)

    # 폐지 이력도 함께 본다. 같은 정책을 peer 가 제정했다가 **폐지**했다면 그것은
    # 추천을 뒤집을 수 있는 정보다(상위법 개정으로 전국이 일제히 폐지한 사례가 실재한다.
    # 예: 저탄소 녹색성장 기본 조례 186곳 폐지 → 탄소중립 조례로 대체). [실측]
    repealed_rows = _base._rows(
        conn,
        f"SELECT ordinance_id, region_id, name, repealed_on "
        f"FROM ordinances WHERE status='repealed' AND ord_kind='조례' "
        f"AND region_id IN ({ph})", ids)

    reg = {r["region_id"]: r for r in _base._rows(
        conn, f"SELECT region_id, name, full_name FROM regions WHERE region_id IN ({ph})", ids)}

    mine: dict[str, str] = {}                 # canon → 원 키
    theirs: dict[str, dict[str, dict]] = {}   # canon → {region_id: row}
    label: dict[str, str] = {}                # canon → 대표 표기
    for r in rows:
        rid = r["region_id"]
        g = reg.get(rid, {})
        key = policy_key(r["name"], g.get("name"), g.get("full_name"))
        if not key or len(key) < 2:   # 2글자 조례명(예: 경주시 포상 조례)도 유효하다
            continue
        ck = canon_key(key)
        if not ck:
            continue
        if rid == tid:
            mine[ck] = key
        else:
            theirs.setdefault(ck, {})[rid] = r
            label.setdefault(ck, key)

    # canon → {region_id: 폐지행}
    repealed_by: dict[str, dict[str, dict]] = {}
    for r in repealed_rows:
        rid = r["region_id"]
        g = reg.get(rid, {})
        rk = policy_key(r["name"], g.get("name"), g.get("full_name"))
        if not rk or len(rk) < 2:
            continue
        rck = canon_key(rk)
        if rck:
            repealed_by.setdefault(rck, {})[rid] = r

    recs, exact_dup, variant = [], 0, 0
    my_items = [(ck_, mine[ck_]) for ck_ in mine]
    for ck, holders in theirs.items():
        if len(holders) < min_peers:
            continue
        if ck in mine:
            exact_dup += 1
            continue
        # 잔여 표기변이는 **숨기지 않고** 가장 가까운 우리 조례를 함께 붙여 보여준다.
        # (완전 자동 판정은 불가능하다: '산업재해 예방' vs '산업재해 예방 및 노동안전보건 지원'
        #  처럼 변이인지 확장인지 사람이 봐야 하는 사례가 실제로 있다.)
        best_d, best_k, best_name = 0.0, None, None
        for mck, mkey in my_items:
            d = dice(ck, mck)
            if mck in ck or ck in mck:
                d = max(d, min(len(mck), len(ck)) / max(len(mck), len(ck)))
            if d > best_d:
                best_d, best_k, best_name = d, mck, mkey
        is_variant = best_d >= dup_threshold
        if is_variant:
            variant += 1
            if exclude_variants:
                continue
        rep = repealed_by.get(ck, {})
        rep_peers = {rid: rr for rid, rr in rep.items() if rid in set(peer_ids)}
        recs.append({
            "policy_key": label[ck],
            "peer_count": len(holders),
            "peer_share": round(len(holders) / len(peer_ids), 4),
            "repealed_peer_count": len(rep_peers),
            "repealed_peers": [{"region_id": rid,
                                "name": (reg.get(rid) or {}).get("full_name"),
                                "repealed_on": rr.get("repealed_on")}
                               for rid, rr in sorted(rep_peers.items(),
                                                     key=lambda kv: str(kv[1].get("repealed_on") or ""))][:3],
            "caution": ("유사 지자체 중 폐지 사례 있음 — 상위법 개정 등으로 대체되었을 수 있으니 "
                        "제정 전 확인 필요" if rep_peers else None),
            "likely_variant_of_mine": is_variant,
            "closest_own": ({"policy_key": best_name, "similarity": round(best_d, 3)}
                            if best_k else None),
            "peers": [{"region_id": rid,
                       "name": (reg.get(rid) or {}).get("full_name"),
                       "ordinance_id": r["ordinance_id"],
                       "ordinance_name": r["name"],
                       "enacted_on": r.get("enacted_on"),
                       "url": r.get("official_url")}
                      for rid, r in sorted(holders.items(),
                                           key=lambda kv: str(kv[1].get("enacted_on") or ""))][:5],
        })
    recs.sort(key=lambda x: (x["likely_variant_of_mine"], -x["peer_count"], x["policy_key"]))
    return {
        "target": sim["target"],
        "peers": [{"region_id": r, "name": (reg.get(r) or {}).get("full_name")}
                  for r in peer_ids],
        "peer_pool_size": len(peer_ids),
        "my_policy_count": len(mine),
        "suppressed_exact_duplicate": exact_dup,
        "flagged_as_variant": variant,
        "recommendations": recs[:limit],
        "method": {**sim["method"], "min_peers": min_peers,
                   "same_sido_boost": same_sido_boost,
                   "dup_threshold": dup_threshold,
                   "exclude_variants": exclude_variants,
                   "policy_key": ("조례명에서 지자체명·법형식 접미 제거 → canon_key(어절 단위 "
                                  "연결어 제거) → 정규형 완전일치는 '보유'로 제외, 잔여 근사는 "
                                  "closest_own 과 함께 likely_variant 로 표시")},
    }
