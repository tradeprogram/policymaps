"""test_neural — policymap.neural(그래프 신경망 계층) 블랙박스 테스트.

검증 대상(구현 세부가 아니라 계약·불변식):
  * GraphArrays.from_graph  : 그래프 → CSR 변환 정합(노드/엣지 수, 고립노드 제거)
  * split_edges             : held-out 분할이 겹치지 않고 메시지패싱에서 제거되는가(누수 차단)
  * train_node2vec          : 결정적(동일 seed → 동일 결과), 커뮤니티 분리가 실제로 되는가
  * train_graphsage         : 학습이 held-out AUC 를 랜덤(0.5) 위로 올리는가
  * roc_auc                 : 자명한 경계값
  * DB 왕복                 : ensure_neural_tables → save → load 무손실(f32 인코딩)
  * most_similar            : 자기 자신 제외 + 코사인 내림차순

numpy 부재 환경에서는 학습 호출이 RuntimeError 로 명시 실패해야 하며(조용한 오답 금지),
그 경우 이 파일의 학습 테스트는 skip 된다.
"""
import sys

from _support import fresh_db, need, run_dict, skip  # noqa: F401


def _np():
    try:
        import numpy
        return numpy
    except ImportError:
        skip("numpy 부재 — 신경망 학습 테스트 불가")
        return None  # pragma: no cover


def _neural():
    return need("policymap.neural", "train_node2vec", "train_graphsage",
                "GraphArrays", "split_edges", "roc_auc")


# --------------------------------------------------------------------------- #
# 합성 이종그래프(테스트 전용) — 2개 커뮤니티 + 지역/조례/법령 3종 라벨
# --------------------------------------------------------------------------- #
def _toy_graph(n_per_comm: int = 12):
    """커뮤니티 2개가 명확히 분리된 소형 이종그래프. build._FallbackGraph 재사용."""
    build = need("policymap.graph.build", "new_graph", "node_id")
    G = build.new_graph()
    edges = []
    for c in range(2):
        region = build.node_id("region", f"R{c}")
        G.add_node(region, label="Region", name=f"지역{c}")
        statute = build.node_id("instrument", f"statute:{c}")
        G.add_node(statute, label="LegalInstrument", name=f"법률{c}")
        ords_ = []
        for i in range(n_per_comm):
            oid = build.node_id("ordinance", f"ordin:{c}{i:02d}")
            G.add_node(oid, label="Ordinance", name=f"커뮤니티{c} 조례{i}")
            ords_.append(oid)
            edges.append((region, oid, "HAS_ORDINANCE"))
            edges.append((oid, statute, "DELEGATED_FROM"))
        # 커뮤니티 내부 조례끼리 촘촘히 연결(구조 신호)
        for i in range(n_per_comm):
            for j in range(i + 1, min(i + 4, n_per_comm)):
                edges.append((ords_[i], ords_[j], "SIMILAR_TO"))
    for u, v, rel in edges:
        G.add_edge(u, v, relation=rel)
    return G, len(edges)


def _comm_of(node_id: str) -> str:
    """'ordinance:ordin:007' → 커뮤니티 인덱스('0'/'1')."""
    return node_id.rsplit(":", 1)[1][0]


# --------------------------------------------------------------------------- #
# 1) GraphArrays / split_edges
# --------------------------------------------------------------------------- #
def test_grapharrays_from_graph_shapes():
    neural = _neural()
    np = _np()
    G, n_edges = _toy_graph()
    ga = neural.GraphArrays.from_graph(G)
    assert ga.num_nodes > 0, "노드 0개"
    assert ga.num_edges == n_edges, f"엣지 수 불일치: {ga.num_edges} != {n_edges}"
    # 라벨은 그래프의 label 속성을 그대로 승계
    assert set(ga.label_names) >= {"Ordinance", "Region", "LegalInstrument"}, ga.label_names
    # CSR 정합: indptr 단조증가 + 마지막이 이웃 총수
    assert bool(np.all(np.diff(ga.indptr) >= 0)), "CSR indptr 비단조"
    assert int(ga.indptr[-1]) == len(ga.indices), "CSR indptr 말단 불일치"


def test_split_edges_no_leak():
    """held-out 엣지는 train 과 겹치지 않고, subset_edges 로 메시지패싱에서 빠져야 한다."""
    neural = _neural()
    np = _np()
    G, _ = _toy_graph()
    ga = neural.GraphArrays.from_graph(G)
    sp = neural.split_edges(ga, test_frac=0.2, seed=7)
    assert sp["num_train"] + sp["num_test"] == ga.num_edges
    assert sp["num_test"] > 0, "held-out 0건 — 평가 불가"
    both = set(map(int, sp["train_idx"])) & set(map(int, sp["test_idx"]))
    assert not both, f"train/test 엣지 중복 {len(both)}건 — 누수"
    train_ga = ga.subset_edges(sp["train_mask"])
    assert train_ga.num_edges == sp["num_train"], "subset_edges 가 held-out 을 제거하지 않음"
    assert int(np.count_nonzero(sp["train_mask"])) == sp["num_train"]


# --------------------------------------------------------------------------- #
# 2) node2vec
# --------------------------------------------------------------------------- #
def test_node2vec_deterministic_and_separates_communities():
    neural = _neural()
    np = _np()
    G, _ = _toy_graph()
    kw = dict(dim=32, walks_per_node=20, walk_len=20, window=5, epochs=20,
              negatives=5, seed=1234)
    r1 = neural.train_node2vec(G, **kw)
    r2 = neural.train_node2vec(G, **kw)
    assert r1.nodes == r2.nodes, "동일 seed 인데 노드 순서가 다름"
    assert float(np.max(np.abs(r1.matrix - r2.matrix))) == 0.0, "동일 seed 재현 실패"
    assert r1.dim == 32 and r1.matrix.shape[0] == len(r1.nodes)

    # 커뮤니티 분리: 같은 커뮤니티 조례쌍 평균 코사인 > 다른 커뮤니티
    U = r1.unit_matrix()
    idx = {n: i for i, n in enumerate(r1.nodes)}
    ords = [n for n in r1.nodes if n.startswith("ordinance:")]
    same, diff = [], []
    for i, a in enumerate(ords):
        for b in ords[i + 1:]:
            cos = float(U[idx[a]] @ U[idx[b]])
            (same if _comm_of(a) == _comm_of(b) else diff).append(cos)
    assert same and diff, "비교쌍 부족"
    m_same, m_diff = sum(same) / len(same), sum(diff) / len(diff)
    assert m_same > m_diff, f"커뮤니티 분리 실패: 같은 {m_same:.3f} <= 다른 {m_diff:.3f}"


def _walk_stats(neural, np, ga, *, p: float, q: float, seed: int = 99):
    """워크를 직접 생성해 (회귀율, 비인접 전이 수) 계산."""
    mod = need("policymap.neural.embeddings", "generate_walks")
    rng = np.random.default_rng(seed)
    returns = steps = invalid = 0
    for chunk in mod.generate_walks(ga, walks_per_node=8, walk_len=16, p=p, q=q, rng=rng):
        arr = np.asarray(chunk)
        for w in arr:
            w = [int(x) for x in w if int(x) >= 0]
            for i in range(1, len(w)):
                # 인접성: w[i] 가 w[i-1] 의 CSR 이웃에 있어야 한다
                lo, hi = int(ga.indptr[w[i - 1]]), int(ga.indptr[w[i - 1] + 1])
                if w[i] not in set(int(x) for x in ga.indices[lo:hi]):
                    invalid += 1
                if i >= 2:
                    steps += 1
                    if w[i] == w[i - 2]:
                        returns += 1
    return (returns / steps if steps else 0.0), invalid


def test_node2vec_pq_bias_is_effective():
    """p 를 키우면 직전 노드로 되돌아오는 비율이 줄어야 한다(2차 워크가 실제로 작동).

    동시에 모든 전이가 실제 그래프 간선을 따라가는지(비인접 전이 0건)도 검사한다.
    """
    neural = _neural()
    np = _np()
    G, _ = _toy_graph()
    ga = neural.GraphArrays.from_graph(G)
    r_low, inv_low = _walk_stats(neural, np, ga, p=0.25, q=1.0)
    r_high, inv_high = _walk_stats(neural, np, ga, p=4.0, q=1.0)
    assert inv_low == 0 and inv_high == 0, (
        f"비인접 전이 발생: p=0.25 {inv_low}건 / p=4.0 {inv_high}건")
    assert r_low > r_high, f"p 편향 무효: p=0.25 회귀율 {r_low:.4f} <= p=4.0 {r_high:.4f}"


# --------------------------------------------------------------------------- #
# 3) GraphSAGE
# --------------------------------------------------------------------------- #
def test_graphsage_learns_above_random():
    """학습 후 held-out AUC 가 학습 전보다 오르고 랜덤(0.5) 위여야 한다."""
    neural = _neural()
    _np()
    G, _ = _toy_graph(n_per_comm=16)
    res = neural.train_graphsage(G, dim=32, layers=2, epochs=60, lr=0.05,
                                 test_frac=0.2, seed=4242)
    assert res.matrix.shape[1] == 32
    assert res.loss_history, "손실 이력 없음"
    assert res.loss_history[-1] < res.loss_history[0], (
        f"손실이 줄지 않음: {res.loss_history[0]} → {res.loss_history[-1]}")
    assert res.auc is not None, "held-out AUC 미보고"
    assert res.auc > 0.5, f"학습 후 AUC 가 랜덤 이하: {res.auc}"


def test_roc_auc_boundaries():
    neural = _neural()
    _np()
    assert neural.roc_auc([1.0, 2.0, 3.0], [0.0, 0.0, 0.0]) == 1.0
    assert neural.roc_auc([0.0, 0.0, 0.0], [0.0, 0.0, 0.0]) == 0.5
    assert neural.roc_auc([0.0, 0.0], [1.0, 2.0]) == 0.0


# --------------------------------------------------------------------------- #
# 4) DB 왕복 / 조회 API
# --------------------------------------------------------------------------- #
def test_save_load_roundtrip_and_most_similar():
    neural = _neural()
    np = _np()
    mod = need("policymap.neural", "ensure_neural_tables", "save_node_embeddings",
               "load_node_embeddings", "most_similar", "decode_vector")
    conn = fresh_db(seed=True)
    try:
        G, _ = _toy_graph(n_per_comm=8)
        res = neural.train_node2vec(G, dim=16, walks_per_node=10, walk_len=16,
                                    window=4, epochs=8, negatives=4, seed=5,
                                    model_name="test-node2vec")
        mod.ensure_neural_tables(conn)
        saved = mod.save_node_embeddings(conn, res, run_id="test-run")
        assert saved["saved"] == len(res.nodes), saved
        assert saved["dim"] == 16

        loaded = mod.load_node_embeddings(conn, "test-node2vec")
        assert len(loaded) == len(res.nodes), "적재/조회 행수 불일치"
        # f32 왕복 무손실(원본이 float32 이므로 정확히 일치해야 한다)
        probe = res.nodes[0]
        back = np.asarray(loaded[probe], dtype="float32")
        assert float(np.max(np.abs(back - res.matrix[0].astype("float32")))) == 0.0, \
            "f32b64 인코딩 왕복 손실"

        top = mod.most_similar(res, probe, k=3)
        assert len(top) == 3
        assert all(t["node_id"] != probe for t in top), "자기 자신이 결과에 포함"
        sims = [t["cosine"] for t in top]
        assert sims == sorted(sims, reverse=True), f"코사인 내림차순 아님: {sims}"
    finally:
        conn.close()


def test_build_neural_similarity_persists_topk():
    neural = _neural()
    _np()
    mod = need("policymap.neural", "build_neural_similarity")
    conn = fresh_db(seed=True)
    try:
        G, _ = _toy_graph(n_per_comm=8)
        res = neural.train_node2vec(G, dim=16, walks_per_node=10, walk_len=16,
                                    window=4, epochs=8, negatives=4, seed=6,
                                    model_name="test-sim")
        out = mod.build_neural_similarity(conn, res, top_k=3, kinds=("Ordinance",),
                                          max_items=None)
        assert out["status"] == "ok", out
        assert out["pairs"] > 0, "저장된 쌍 0건"
        n = conn.execute(
            "SELECT COUNT(*) FROM neural_similarity WHERE model_name='test-sim'"
        ).fetchone()[0]
        assert n == out["pairs"], f"DB 행수 {n} != 보고 {out['pairs']}"
        # rank 는 1부터 연속
        ranks = [r[0] for r in conn.execute(
            "SELECT rank FROM neural_similarity WHERE model_name='test-sim' "
            "AND src_id=(SELECT src_id FROM neural_similarity WHERE model_name='test-sim' "
            "LIMIT 1) ORDER BY rank")]
        assert ranks == list(range(1, len(ranks) + 1)), f"rank 비연속: {ranks}"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 5) run.py 서브커맨드 배선 — 모델 디스패치
# --------------------------------------------------------------------------- #
def test_run_neural_model_dispatch():
    """`run neural --model X` 이 세 모델 모두를 실제로 학습시키는가.

    metapath2vec 은 METAPATHS 가 {이름: [라벨,...]} dict 이므로 스키마만 뽑아
    그래프에 실존하는 라벨의 메타패스로 걸러야 한다(부분 그래프 대응).
    """
    neural = _neural()
    _np()
    run = need("policymap.run", "_train_one_model")
    G, _ = _toy_graph(n_per_comm=12)
    ga = neural.GraphArrays.from_graph(G)
    sp = neural.split_edges(ga, test_frac=0.1, seed=1)
    train_ga = ga.subset_edges(sp["train_mask"])

    expected = {"node2vec": "node2vec-numpy", "metapath2vec": "metapath2vec-numpy",
                "graphsage": "graphsage-numpy"}
    for name, model_name in expected.items():
        res = run._train_one_model(neural, name, None, ga, train_ga, sp,
                                   dim=32, epochs=2, seed=7)
        assert res.model_name == model_name, f"{name} → {res.model_name}"
        assert len(res.nodes) == ga.num_nodes, f"{name}: 노드 수 불일치"
        assert res.loss_history, f"{name}: 손실 이력 없음"

    try:
        run._train_one_model(neural, "bogus", None, ga, train_ga, sp,
                             dim=8, epochs=1, seed=1)
    except ValueError:
        pass
    else:
        raise AssertionError("알 수 없는 모델명이 조용히 통과함")


if __name__ == "__main__":
    sys.exit(run_dict(globals(), "test_neural"))
