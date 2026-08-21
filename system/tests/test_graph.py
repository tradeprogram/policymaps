"""test_graph — graph.build/analysis/export 테스트.

병렬 구현 중이므로 need()로 미구현 시 skip. 계약(CONTRACTS.md §3)에 맞춰 선작성.
seed_sample 미니월드 사용(부산진구 26170 은 주차장 조례 부재 → 위임격차 후보).
"""
import os
import tempfile

from _support import (
    need, skip, fresh_db, graph_counts, run_dict,
    pm_db as db, pm_config as config,
)


# --------------------------------------------------------------------------- #
# build.py
# --------------------------------------------------------------------------- #
def test_node_id_prefix():
    build = need("policymap.graph.build", "node_id")
    nid = build.node_id("region", "26110")
    assert nid == "region:26110", f"접두 규약 위반: {nid}"
    assert build.node_id("ordinance", "ordin:9001").startswith("ordinance:")


def test_build_graph_structure():
    build = need("policymap.graph.build", "build_graph")
    conn = fresh_db(seed=True)
    g = build.build_graph(conn)
    n_nodes, n_edges = graph_counts(g)
    # 지역6 + 법령2 + 조례3 + (의안/의원/정당/예산/카테고리) → 충분히 큰 그래프
    assert n_nodes >= 11, f"노드 부족: {n_nodes}"
    assert n_edges >= 3, f"엣지 부족(HAS_ORDINANCE/DELEGATED_FROM 등): {n_edges}"


# --------------------------------------------------------------------------- #
# analysis.py
# --------------------------------------------------------------------------- #
def test_get_delegation_gap():
    analysis = need("policymap.graph.analysis", "get_delegation_gap")
    conn = fresh_db(seed=True)
    # 26170(부산진구): 주차장법 위임 계열이나 조례 없음 → 격차 후보
    gaps = analysis.get_delegation_gap(conn, "26170")
    assert isinstance(gaps, list)
    # 반환 요소가 있으면 dict 형태
    for g in gaps:
        assert isinstance(g, dict)


def test_compare_ordinance_coverage():
    analysis = need("policymap.graph.analysis", "compare_ordinance_coverage")
    conn = fresh_db(seed=True)
    res = analysis.compare_ordinance_coverage(conn, "statute:001234", region_level=2)
    assert isinstance(res, dict), f"매트릭스 dict 기대: {type(res)}"


def test_compute_spatial_autocorrelation():
    analysis = need("policymap.graph.analysis", "compute_spatial_autocorrelation")
    conn = fresh_db(seed=True)
    try:
        res = analysis.compute_spatial_autocorrelation(conn, "budget_now", method="moran")
    except (NotImplementedError, KeyError, ValueError) as exc:
        skip(f"메트릭 정의 상이(구현 재량): {exc}")
        return
    assert isinstance(res, dict)
    # Moran's I 존재 시 [-1,1] 범위
    if "moran_i" in res and res["moran_i"] is not None:
        assert -1.0001 <= float(res["moran_i"]) <= 1.0001


# --------------------------------------------------------------------------- #
# export.py
# --------------------------------------------------------------------------- #
def test_export_static_bundle():
    export = need("policymap.graph.export", "export_static")
    conn = fresh_db(seed=True)
    with tempfile.TemporaryDirectory() as out:
        res = export.export_static(conn, out)
        assert isinstance(res, dict)
        # manifest.json 필수(klocal 패턴)
        manifest = os.path.join(out, "manifest.json")
        assert os.path.exists(manifest), "manifest.json 산출 필요"
        import json
        m = json.loads(open(manifest, encoding="utf-8").read())
        assert "as_of_date" in m, "manifest 에 as_of_date 동봉 필요"


if __name__ == "__main__":
    import sys
    sys.exit(run_dict(globals(), "test_graph"))
