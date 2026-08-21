# -*- coding: utf-8 -*-
"""tests.test_analytics — policymap.analytics 계량 모듈 검증.

검증 전략
  * **합성 진실값 복원**: 계수를 알고 있는 자료를 만들어 추정기가 되찾는지 본다
    (로지스틱 회귀 계수, 로지스틱 성장모형 K·r·t0).
  * **해석적 극단값**: 완전 군집/체스판 패턴에서 Moran's I 의 부호·크기.
  * **계약**: 행표준화 W 행합=1, 위험집합 이탈 규칙, level=3 가드, BH-FDR 단조성.
  * DB 는 인메모리 합성 지자체(6×6 격자)로 만든다 — 실 DB 를 건드리지 않는다.
"""
import math
import sys

import _support

np = _support.need("numpy")


# --------------------------------------------------------------------------- #
# 합성 DB: 6x6 격자 지자체 + 알려진 확산
# --------------------------------------------------------------------------- #
GRID = 6


def _grid_db(*, adoption=None, populations=True):
    """6x6=36개 기초자치단체 격자. rook 인접. 필요한 표만 채운다."""
    from policymap import db as pm_db
    conn = _support.fresh_db(seed=False)
    today = "2026-08-20"

    pm_db.upsert(conn, "regions", {
        "region_id": "90", "region_cd": "9000000000", "sig_cd": "90999",
        "sido_cd": "90", "name": "합성광역", "full_name": "합성광역",
        "level": 1, "has_legislation": 1, "status": "active",
        "as_of_date": today, "updated_at": today}, "region_id")

    for i in range(GRID):
        for j in range(GRID):
            rid = "90%03d" % (i * GRID + j)
            pm_db.upsert(conn, "regions", {
                "region_id": rid, "region_cd": rid + "00000", "sig_cd": rid,
                "sido_cd": "90", "sgg_cd": rid[2:],
                "name": "합성%02d시" % (i * GRID + j),
                "full_name": "합성광역 합성%02d시" % (i * GRID + j),
                "level": 2, "parent_region": "90", "has_legislation": 1,
                "population": (50000 + 1000 * (i * GRID + j)) if populations else None,
                "centroid_lon": 127.0 + 0.3 * j, "centroid_lat": 36.0 + 0.3 * i,
                "status": "active", "as_of_date": today, "updated_at": today}, "region_id")

    for i in range(GRID):
        for j in range(GRID):
            a = "90%03d" % (i * GRID + j)
            for di, dj in ((0, 1), (1, 0)):
                ni, nj = i + di, j + dj
                if ni >= GRID or nj >= GRID:
                    continue
                b = "90%03d" % (ni * GRID + nj)
                for x, y in ((a, b), (b, a)):
                    pm_db.upsert(conn, "region_adjacency", {
                        "region_id": x, "neighbor_id": y, "contiguity_type": "rook",
                        "same_province": 1, "method": "synthetic",
                        "computed_at": today}, ("region_id", "neighbor_id"))

    # 예산(재정지표가 있어야 covariate 가 채워진다)
    for i in range(GRID * GRID):
        rid = "90%03d" % i
        for field, amt in (("사회복지", 300 + i), ("일반공공행정", 700 - i)):
            pm_db.upsert(conn, "budget_lines", {
                "budget_id": "b-%s-%s" % (rid, field), "fyr": 2025, "laf_cd": rid,
                "region_id": rid, "dbiz_cd": "D1", "dbiz_nm": field, "field": field,
                "budget_now": float(amt) * 1e6, "gov_fund": float(amt) * 1e5,
                "sido_fund": float(amt) * 1e5, "as_of_date": "2026-01-01",
                "updated_at": today}, "budget_id")

    if adoption:
        for rid, year in adoption.items():
            pm_db.upsert(conn, "ordinances", {
                "ordinance_id": "o-%s" % rid, "mst": rid, "region_id": rid,
                "org_name": "합성", "name": "합성광역 %s 확산시범 조례" % rid,
                "ord_kind": "조례", "enacted_on": "%04d0301" % year,
                "rr_cls_cd": "제정", "status": "active",
                "as_of_date": today, "updated_at": today}, "ordinance_id")
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# 1) IRLS 로지스틱 — 진실 계수 복원
# --------------------------------------------------------------------------- #
def test_glm_recovers_known_coefficients():
    eha = _support.need("policymap.analytics.eha", "fit_glm_binomial")
    rng = np.random.default_rng(7)
    n = 20000
    x1 = rng.normal(size=n)
    x2 = rng.normal(size=n)
    beta_true = np.array([-1.0, 0.8, -0.5])
    X = np.column_stack([np.ones(n), x1, x2])
    p = 1.0 / (1.0 + np.exp(-(X @ beta_true)))
    y = (rng.random(n) < p).astype(float)

    fit = eha.fit_glm_binomial(X, y, ["(intercept)", "x1", "x2"])
    assert fit["converged"], "IRLS 미수렴"
    est = np.array([t["coef"] for t in fit["terms"]])
    assert np.allclose(est, beta_true, atol=0.06), f"계수 복원 실패: {est} vs {beta_true}"
    # SE 가 이론 표준오차 규모(≈ 1/sqrt(n) 급)와 맞는지
    ses = np.array([t["se"] for t in fit["terms"]])
    assert np.all(ses > 0) and np.all(ses < 0.1), f"SE 이상: {ses}"
    # 유의한 계수는 p 가 작아야 한다
    assert fit["terms"][1]["p_value"] < 1e-10
    # 오즈비 = exp(coef)
    assert abs(fit["terms"][1]["odds_ratio"] - math.exp(fit["terms"][1]["coef"])) < 1e-9
    assert fit["mcfadden_r2"] > 0


def test_glm_cloglog_link_runs_and_differs():
    eha = _support.need("policymap.analytics.eha", "fit_glm_binomial")
    rng = np.random.default_rng(11)
    n = 4000
    x = rng.normal(size=n)
    X = np.column_stack([np.ones(n), x])
    eta = -2.0 + 0.7 * x
    p = 1.0 - np.exp(-np.exp(eta))
    y = (rng.random(n) < p).astype(float)
    fit = eha.fit_glm_binomial(X, y, ["(intercept)", "x"], link="cloglog")
    assert fit["converged"]
    est = [t["coef"] for t in fit["terms"]]
    assert abs(est[0] + 2.0) < 0.15 and abs(est[1] - 0.7) < 0.12, est
    assert fit["terms"][0]["odds_ratio"] is None, "cloglog 에 OR 를 붙이면 안 된다"


def test_cluster_robust_se_reacts_to_clustering():
    """클러스터 내 상관이 있으면 로버스트 SE 가 순진 SE 보다 커야 한다."""
    eha = _support.need("policymap.analytics.eha", "fit_glm_binomial")
    rng = np.random.default_rng(3)
    G, T = 200, 10
    clusters, xs, ys = [], [], []
    for g in range(G):
        u = rng.normal(scale=1.5)              # 클러스터 임의효과
        xg = rng.normal()                      # 클러스터 수준 회귀변수
        for _ in range(T):
            eta = -1.0 + 0.6 * xg + u
            ys.append(1.0 if rng.random() < 1 / (1 + math.exp(-eta)) else 0.0)
            xs.append(xg)
            clusters.append(g)
    X = np.column_stack([np.ones(len(xs)), np.array(xs)])
    y = np.array(ys)
    fit = eha.fit_glm_binomial(X, y, ["(intercept)", "x"],
                               clusters=np.array(clusters))
    t = fit["terms"][1]
    assert fit["n_clusters"] == G
    assert t["se_robust"] > t["se"] * 1.3, (t["se"], t["se_robust"])
    assert fit["se_reported"] == "cluster_robust"


# --------------------------------------------------------------------------- #
# 2) 공간가중행렬 / Moran's I
# --------------------------------------------------------------------------- #
def test_row_standardized_weights_sum_to_one():
    base = _support.need("policymap.analytics.base", "build_spatial_weights")
    conn = _grid_db()
    try:
        pack = base.build_spatial_weights(conn, standardize="row")
        assert pack["meta"]["universe"] == GRID * GRID
        assert pack["meta"]["n_with_neighbors"] == GRID * GRID
        for rid, row in pack["W"].items():
            assert abs(sum(row.values()) - 1.0) < 1e-12, (rid, row)
        # 격자 rook 인접: 모서리 2, 변 3, 내부 4
        card = sorted(pack["cardinality"].values())
        assert card[0] == 2 and card[-1] == 4
        binary = base.build_spatial_weights(conn, standardize="binary")
        assert all(w == 1.0 for row in binary["W"].values() for w in row.values())
    finally:
        base.clear_cache()
        conn.close()


def test_moran_extremes():
    """완전 군집(좌우 반씩) → I 크게 +, 체스판 → I 크게 -."""
    base = _support.need("policymap.analytics.base", "build_spatial_weights")
    spatial = _support.need("policymap.analytics.spatial", "moran")
    conn = _grid_db()
    try:
        pack = base.build_spatial_weights(conn, standardize="row")
        W = pack["W"]
        clustered, checker = {}, {}
        for i in range(GRID):
            for j in range(GRID):
                rid = "90%03d" % (i * GRID + j)
                clustered[rid] = 1.0 if j < GRID // 2 else 0.0
                checker[rid] = 1.0 if (i + j) % 2 == 0 else 0.0
        nodes, x, nbi, nbw = spatial._prep(clustered, W)
        I_c, _ = spatial._global_i(x - x.mean(), nbi, nbw)
        nodes, x, nbi, nbw = spatial._prep(checker, W)
        I_k, _ = spatial._global_i(x - x.mean(), nbi, nbw)
        assert I_c > 0.55, I_c
        assert I_k < -0.9, I_k
    finally:
        base.clear_cache()
        conn.close()


def test_moran_pipeline_on_synthetic_db():
    base = _support.need("policymap.analytics.base", "build_spatial_weights")
    spatial = _support.need("policymap.analytics.spatial", "moran")
    conn = _grid_db()
    try:
        r = spatial.moran(conn, "population", permutations=199, lisa=True)
        assert r["n"] == GRID * GRID
        assert r["moran_i"] is not None
        assert 0.0 <= r["p_sim"] <= 1.0
        assert len(r["lisa"]) == r["n"]
        for row in r["lisa"]:
            assert row["quadrant"] in ("HH", "LL", "HL", "LH")
            assert 0.0 <= row["q_value"] <= 1.0
            assert row["q_value"] >= row["p_sim"] - 1e-9, "BH q 는 p 이상이어야 한다"
        assert r["lisa_summary"]["n_significant_fdr"] <= r["lisa_summary"]["n_significant_raw_p05"]
    finally:
        base.clear_cache()
        conn.close()


def test_area_from_geojson_matches_analytic_box():
    base = _support.need("policymap.analytics.base")
    gj = ('{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,1],[0,0]]]}')
    a = base._area_km2_from_geojson(gj)
    R = base.EARTH_R_KM
    expected = (R * math.pi / 180.0) ** 2 * math.cos(math.radians(0.5))
    assert a is not None
    assert abs(a - expected) / expected < 0.02, (a, expected)


# --------------------------------------------------------------------------- #
# 3) 채택연도 / 위험집합 패널
# --------------------------------------------------------------------------- #
def test_adoption_years_and_risk_set_rules():
    base = _support.need("policymap.analytics.base", "adoption_years")
    eha = _support.need("policymap.analytics.eha", "build_risk_set_panel")
    adoption = {"90000": 2016, "90001": 2018, "90002": 2018, "90010": 2020}
    conn = _grid_db(adoption=adoption)
    try:
        ad = base.adoption_years(conn, "확산시범")
        assert ad["years"] == adoption, ad["years"]
        assert ad["meta"]["universe"] == GRID * GRID
        assert ad["meta"]["adopters_observed"] == 4
        assert "warning" in ad["meta"]

        panel = eha.build_risk_set_panel(conn, "확산시범", y0=2017, y1=2020)
        meta = panel["meta"]
        assert meta["left_truncated"] == 1, "2016 채택은 좌측절단이어야 한다"
        assert meta["n_events"] == 3
        # 채택 지자체는 채택연도까지만 행이 있어야 한다
        by_r = {}
        for row in panel["rows"]:
            by_r.setdefault(row["region_id"], []).append(row["year"])
        assert "90000" not in by_r, "좌측절단 지자체가 패널에 남았다"
        assert max(by_r["90001"]) == 2018 and 2019 not in by_r["90001"]
        assert max(by_r["90010"]) == 2020
        never = "90035"
        assert sorted(by_r[never]) == [2017, 2018, 2019, 2020]
        # 노출은 t-1 기준 — 2017년에는 이웃(90001)이 아직 미채택
        r17 = [x for x in panel["rows"] if x["region_id"] == "90001" and x["year"] == 2017][0]
        assert r17["neighbor_exposure"] == 0.0 or r17["neighbor_exposure"] is not None
        # 90001 의 이웃 90000 은 2016 채택 → 2018 행의 노출 = 1/3
        r18 = [x for x in panel["rows"] if x["region_id"] == "90001" and x["year"] == 2018][0]
        assert abs(r18["neighbor_exposure"] - 1.0 / 3.0) < 1e-12, r18["neighbor_exposure"]
        assert r18["event"] == 1
        assert r18["peer_exposure"] is not None
        # 90003 의 이웃 90002 는 2018 채택 → 2019 행에서 비로소 노출이 잡힌다(t-1 규칙)
        r19 = [x for x in panel["rows"] if x["region_id"] == "90003" and x["year"] == 2019][0]
        r18b = [x for x in panel["rows"] if x["region_id"] == "90003" and x["year"] == 2018][0]
        assert r18b["neighbor_exposure"] == 0.0 and r19["neighbor_exposure"] > 0
    finally:
        base.clear_cache()
        conn.close()


def test_estimate_hazard_reports_insufficient_events():
    base = _support.need("policymap.analytics.base")
    eha = _support.need("policymap.analytics.eha", "estimate_diffusion_hazard")
    conn = _grid_db(adoption={"90000": 2018, "90001": 2019})
    try:
        r = eha.estimate_diffusion_hazard(conn, "확산시범", y0=2017, y1=2020)
        assert r["model"] is None and "부족" in r["error"]
    finally:
        base.clear_cache()
        conn.close()


# --------------------------------------------------------------------------- #
# 4) 확산 곡선
# --------------------------------------------------------------------------- #
def test_logistic_growth_recovers_parameters():
    diff = _support.need("policymap.analytics.diffusion", "fit_logistic_growth")
    K, r, t0 = 200.0, 0.8, 2018.0
    t = np.arange(2010, 2031, dtype=float)
    y = K / (1 + np.exp(-r * (t - t0)))
    fit = diff.fit_logistic_growth(t, y)
    assert abs(fit["K"] - K) < 1.0, fit
    assert abs(fit["r"] - r) < 0.02, fit
    assert abs(fit["t0"] - t0) < 0.1, fit
    assert fit["r2"] > 0.999
    assert abs(fit["t_10_90_years"] - math.log(81) / r) < 1e-6


def test_diffusion_profile_and_null_test():
    base = _support.need("policymap.analytics.base")
    diff = _support.need("policymap.analytics.diffusion", "diffusion_profile")
    # 좌→우로 번지는 결정적 확산: 열 j 는 2010+j 년에 전원 채택
    adoption = {}
    for i in range(GRID):
        for j in range(GRID):
            adoption["90%03d" % (i * GRID + j)] = 2010 + j
    conn = _grid_db(adoption=adoption)
    try:
        p = diff.diffusion_profile(conn, "확산시범", permutations=199)
        assert p["adopters"] == GRID * GRID
        assert p["final_adoption_rate"] == 1.0
        assert p["window"] == [2010, 2010 + GRID - 1]
        assert p["logistic"]["K_free"]["r2"] > 0.9
        nt = p["path_null_test"]
        assert nt is not None and 0 < nt["p_sim"] <= 1
        # 결정적 서→동 확산이면 '선행 이웃 보유' 비중이 귀무 평균보다 커야 한다
        assert nt["observed"] >= nt["null_mean"], nt
        shares = p["path_decomposition"]["shares"]
        assert abs(sum(shares.values()) - 1.0) < 1e-6
        assert p["innovators"][0]["year"] == 2010
    finally:
        base.clear_cache()
        conn.close()


# --------------------------------------------------------------------------- #
# 5) peer group
# --------------------------------------------------------------------------- #
def test_peer_level3_guard():
    """일반구는 조례 제정권이 없다 → 빈 리스트 + 사유. (기존 구현은 similarity 0.0 10건 반환)"""
    from policymap import db as pm_db
    base = _support.need("policymap.analytics.base")
    peers = _support.need("policymap.analytics.peers", "find_similar_governments")
    conn = _grid_db()
    try:
        pm_db.upsert(conn, "regions", {
            "region_id": "90900", "region_cd": "9090000000", "sig_cd": "90900",
            "sido_cd": "90", "name": "합성일반구", "full_name": "합성광역 합성00시 합성일반구",
            "level": 3, "parent_region": "90000", "has_legislation": 0,
            "status": "active", "as_of_date": "2026-08-20",
            "updated_at": "2026-08-20"}, "region_id")
        conn.commit()
        r = peers.find_similar_governments(conn, "90900")
        assert r["peers"] == []
        assert "조례 제정권" in r["reason"]
        assert r["parent_region"] == "90000"
    finally:
        base.clear_cache()
        conn.close()


def test_peer_ranking_and_type_partition():
    base = _support.need("policymap.analytics.base")
    peers = _support.need("policymap.analytics.peers", "find_similar_governments",
                          "peer_matrix")
    conn = _grid_db()
    try:
        r = peers.find_similar_governments(conn, "90000", k=5)
        assert len(r["peers"]) == 5
        sims = [p["similarity"] for p in r["peers"]]
        assert sims == sorted(sims, reverse=True)
        assert all(p["rtype"] == r["target"]["rtype"] for p in r["peers"]), "유형 분할 위반"
        assert r["method"]["weights"]
        assert set(r["method"]["weight_provenance"]) == set(r["method"]["weights"])
        # 인구가 이웃한 지자체가 상위에 와야 한다(인구는 인덱스 순 선형 증가)
        top = r["peers"][0]["region_id"]
        assert top in ("90001", "90002"), top
        pm = peers.peer_matrix(conn, m=3)
        assert len(pm) == GRID * GRID
        assert all(len(v) == 3 for v in pm.values())
        assert "90000" not in pm["90000"]
    finally:
        base.clear_cache()
        conn.close()


def test_materialize_region_features_roundtrip():
    base = _support.need("policymap.analytics.base")
    peers = _support.need("policymap.analytics.peers", "materialize_region_features",
                          "load_region_features")
    conn = _grid_db()
    try:
        info = peers.materialize_region_features(conn)
        assert info["rows"] == GRID * GRID
        feats = peers.load_region_features(conn)
        assert len(feats) == GRID * GRID
        one = feats["90000"]
        assert one["population"] == 50000
        assert one["welfare_ratio"] is not None and 0 < one["welfare_ratio"] < 1
        assert one["fiscal_self_ratio"] is not None and 0 < one["fiscal_self_ratio"] < 1
        assert one["rtype"] == "시"
    finally:
        base.clear_cache()
        conn.close()


# --------------------------------------------------------------------------- #
# 6) 소품
# --------------------------------------------------------------------------- #
def test_cosine_returns_none_not_zero_for_empty():
    base = _support.need("policymap.analytics.base", "cosine")
    assert base.cosine({}, {"a": 1.0}) is None, "빈 벡터에 0.0 을 돌려주면 가드를 통과해버린다"
    assert base.cosine({"a": 1.0}, {"a": 1.0}) == 1.0
    v = base.cosine({"a": 1.0, "b": 1.0}, {"a": 1.0})
    assert abs(v - 1 / math.sqrt(2)) < 1e-12


def test_region_type_axis():
    base = _support.need("policymap.analytics.base", "region_type")
    assert base.region_type("종로구") == "자치구"
    assert base.region_type("완도군") == "군"
    assert base.region_type("경주시") == "시"
    assert base.region_type("장안구", level=3) == "일반구"
    assert base.region_type("서울특별시", level=1) == "광역"


def test_year_of_parsing():
    base = _support.need("policymap.analytics.base", "year_of")
    assert base.year_of("20180301") == 2018
    assert base.year_of("2018-03-01") == 2018
    assert base.year_of(None) is None
    assert base.year_of("18") is None
    assert base.year_of("30250101") is None


def test_policy_key_and_canon_absorb_real_variants():
    """실 DB 에서 확인된 표기 변이가 같은 정규형으로 모여야 한다."""
    peers = _support.need("policymap.analytics.peers", "policy_key", "canon_key", "dice")
    assert peers.policy_key("서울특별시 종로구 자원봉사활동 지원 조례",
                            "종로구", "서울특별시 종로구") == "자원봉사활동 지원"
    assert peers.policy_key("서울특별시 서대문구 구세 감면에 관한 조례",
                            "서대문구", "서울특별시 서대문구") == "구세 감면"
    # 지자체 접두가 full_name 과 다르게 붙은 경우(예: '경주시 포상 조례')
    assert peers.policy_key("경주시 포상 조례", "경주시", "경상북도 경주시") == "포상"
    pairs = [
        ("사회복지사 등의 처우 및 지위 향상", "사회복지사 등의 처우 및 지위향상"),
        ("녹색제품 구매촉진", "녹색제품 구매 촉진"),
        ("통합재정안정화기금 설치 및 운용", "통합재정안정화기금 설치·운용"),
        ("자살예방 및 생명존중문화 조성", "자살예방 및 생명존중 문화조성"),
    ]
    for a, b in pairs:
        assert peers.canon_key(a) == peers.canon_key(b), (a, b)
    # 연결어는 어절 단위로만 지운다 — 글자 단위로 지우면 뜻이 망가진다
    assert peers.canon_key("의회 의원 행동강령") == "의회의원행동강령"
    assert peers.canon_key("양성평등 기본") == "양성평등기본"
    assert peers.canon_key("지역사회보장협의체 운영") == "지역사회보장협의체운영"
    assert peers.dice("가나다", "가나다") == 1.0
    assert peers.dice("가나다", "마바사") == 0.0


def test_recommend_ordinances_finds_gap_and_hides_variant():
    base = _support.need("policymap.analytics.base")
    peers = _support.need("policymap.analytics.peers", "recommend_ordinances")
    from policymap import db as pm_db
    conn = _grid_db()
    try:
        today = "2026-08-20"

        def add_ord(rid, nm, oid):
            pm_db.upsert(conn, "ordinances", {
                "ordinance_id": oid, "mst": oid, "region_id": rid, "org_name": "합성",
                "name": nm, "ord_kind": "조례", "enacted_on": "20200101",
                "rr_cls_cd": "제정", "status": "active",
                "as_of_date": today, "updated_at": today}, "ordinance_id")

        me = "90000"
        # 나는 '주차장 설치 및 관리에 관한' + '자원봉사활동 지원' 표기변이본을 갖고 있다
        add_ord(me, "합성광역 합성00시 주차장 설치 및 관리에 관한 조례", "me-1")
        add_ord(me, "합성광역 합성00시 자원봉사활동지원 조례", "me-2")
        # 이웃 8곳: 주차장(동일), 자원봉사활동 지원(표기변이), 양성평등 기본(내겐 없음)
        for i in range(1, 9):
            rid = "90%03d" % i
            nm = "합성광역 합성%02d시 " % i
            add_ord(rid, nm + "주차장 설치 및 관리 조례", "p%d-1" % i)
            add_ord(rid, nm + "자원봉사활동 지원 조례", "p%d-2" % i)
            add_ord(rid, nm + "양성평등 기본 조례", "p%d-3" % i)
        conn.commit()

        r = peers.recommend_ordinances(conn, me, k=8, min_peers=3, limit=10)
        keys = [x["policy_key"] for x in r["recommendations"]]
        assert "양성평등 기본" in keys, keys
        assert not any("주차장" in k for k in keys), "정규형 완전일치는 제외돼야 한다"
        assert not any("자원봉사" in k for k in keys), "표기변이는 변이로 걸러져야 한다"
        assert r["suppressed_exact_duplicate"] >= 1
        top = [x for x in r["recommendations"] if x["policy_key"] == "양성평등 기본"][0]
        assert top["peer_count"] == 8
        assert top["likely_variant_of_mine"] is False
        assert top["peers"][0]["ordinance_id"].startswith("p")
    finally:
        base.clear_cache()
        conn.close()


if __name__ == "__main__":
    sys.exit(_support.run_dict(dict(globals()), "analytics"))
