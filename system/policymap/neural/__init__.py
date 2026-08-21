"""policymap.neural — 그래프 신경망 계층(numpy 단독 구현).

CONTRACTS.md §3(graph) 위에 얹히는 학습 계층. torch/tensorflow/sklearn/gensim 없이
numpy 만으로 순전파·역전파·SGD 를 직접 구현한다.

모듈:
  embeddings.py : 구조 임베딩 — 2차 랜덤워크(node2vec p/q) + 엣지타입 가중 + 메타패스
                  (metapath2vec 유사) → skip-gram with negative sampling(SGNS).
                  train_node2vec(graph, dim, walks_per_node, walk_len, window, epochs, negatives)
  gnn.py        : GraphSAGE 스타일 메시지패싱 인코더(mean/max aggregator, 2~3층) +
                  비지도 링크예측 목적함수(sigmoid+BCE, 수동미분) + held-out AUC 평가.
                  negative 는 타입정합(type-matched)이 기본 — 이종그래프에서 균등
                  negative 는 노드타입만 구분해도 손실이 줄어 표현이 붕괴한다.
                  train_graphsage(graph, features, dim, layers, epochs, lr)

공통 규약:
  * 입력 그래프는 graph.build.build_graph() 산출물(networkx MultiDiGraph 또는 폴백)이며
    graph.build.graph_nodes/graph_edges 접근자만 사용한다(백엔드 무관).
  * numpy 는 선택적 import. 부재 시 모듈 import 는 성공하고 학습 호출만 명시적으로 실패한다.
  * 학습 결과는 node_embeddings / neural_similarity 테이블에 저장한다
    (기존 embeddings / similarity_edges 는 건드리지 않는다 — 병렬 에이전트 충돌 방지).
  * 대용량 그래프는 워크 시작노드 샘플링·페어 상한으로 조절하되 그래프 구조는 보존한다.
  * 링크예측 평가는 held-out 엣지를 워크/메시지패싱 그래프에서 **제거한 뒤** 학습한다
    (GraphArrays.subset_edges / split_edges(...)['train_mask']) — transductive 누수 차단.
"""

from .embeddings import (  # noqa: F401
    DEFAULT_EDGE_WEIGHTS,
    METAPATHS,
    EmbeddingResult,
    GraphArrays,
    build_neural_similarity,
    decode_vector,
    ensure_neural_tables,
    load_node_embeddings,
    most_similar,
    most_similar_db,
    save_node_embeddings,
    train_node2vec,
)
from .gnn import (  # noqa: F401
    GraphSageResult,
    build_node_features,
    category_cohesion,
    evaluate_link_prediction,
    label_pools,
    roc_auc,
    sample_negative_pairs,
    split_edges,
    train_graphsage,
)

__all__ = [
    # embeddings
    "train_node2vec", "EmbeddingResult", "GraphArrays",
    "DEFAULT_EDGE_WEIGHTS", "METAPATHS",
    "ensure_neural_tables", "save_node_embeddings", "load_node_embeddings",
    "build_neural_similarity", "most_similar", "most_similar_db", "decode_vector",
    # gnn
    "train_graphsage", "GraphSageResult", "build_node_features",
    "split_edges", "evaluate_link_prediction", "roc_auc",
    "sample_negative_pairs", "label_pools", "category_cohesion",
]
