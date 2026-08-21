"""test_rag — policymap.rag(GraphRAG 검색 계층) 블랙박스 테스트.

검증 대상(계약·불변식):
  * build_index        : 코퍼스 전량 색인, 결정적(같은 입력 → 같은 통계), 증분 재빌드 무동작
  * bm25/dense/hybrid  : 시드 조례를 실제로 회수하는가, rank 가 1부터 연속인가
  * graph_expand       : 확장 항목마다 via/path/근거가 붙어 추적 가능한가(GraphRAG 핵심)
  * hybrid_graph_search: 전문검색으로 못 찾는 상위법이 그래프 경로로 랭킹에 진입하는가
  * answer_context     : mcp_server 와 동일한 안전 봉투(execution_allowed/as_of_date/disclaimer)
  * 원문 미러링 금지    : 인덱스는 메타만 저장하고 본문은 질의 시점에 DB 재조회

인덱스는 항상 임시 디렉터리에 만든다(운영 data/index 를 건드리지 않는다).
"""
import shutil
import sys
import tempfile
from pathlib import Path

from _support import fresh_db, need, run_dict, skip  # noqa: F401


def _rag():
    return need("policymap.rag", "build_index", "hybrid_search", "graph_expand",
                "answer_context", "hybrid_graph_search")


# 시드 미니월드(조문 3건)만으로는 BM25 가 동작하지 않는다 — 3문서 코퍼스에서는
# df≥2 인 어휘의 IDF 가 0 이하가 되어 전부 탈락한다(BM25 정의상 정상 동작).
# 어휘 분포를 현실화하기 위해 검색 테스트 전용 조문을 추가한다.
_EXTRA_ORDINANCES = [
    ("ordin:9101", "11110", "서울특별시 종로구 자전거 이용 활성화 조례",
     "자전거도로 및 자전거 주차대의 설치·관리에 필요한 사항을 규정한다."),
    ("ordin:9102", "11110", "서울특별시 종로구 도시공원 조성 및 관리 조례",
     "도시공원과 녹지의 조성 및 관리에 필요한 사항을 규정한다."),
    ("ordin:9103", "11140", "서울특별시 중구 옥외광고물 관리 조례",
     "옥외광고물의 표시방법과 허가에 필요한 사항을 규정한다."),
    ("ordin:9104", "11140", "서울특별시 중구 평생학습 진흥 조례",
     "평생학습관 운영과 학습동아리 지원에 필요한 사항을 규정한다."),
    ("ordin:9105", "26110", "부산광역시 중구 전통시장 육성 조례",
     "전통시장과 상점가의 시설현대화 지원에 필요한 사항을 규정한다."),
    ("ordin:9106", "26110", "부산광역시 중구 청소년 문화의집 운영 조례",
     "청소년 문화의집의 운영과 프로그램 지원에 필요한 사항을 규정한다."),
    ("ordin:9107", "11110", "서울특별시 종로구 노후경유차 저공해화 지원 조례",
     "노후경유차 배출가스 저감장치 부착 지원에 필요한 사항을 규정한다."),
    ("ordin:9108", "11140", "서울특별시 중구 소상공인 지원 조례",
     "소상공인의 경영안정과 창업지원에 필요한 사항을 규정한다."),
    ("ordin:9109", "26110", "부산광역시 중구 재난안전기금 운용 조례",
     "재난안전기금의 조성과 운용에 필요한 사항을 규정한다."),
    ("ordin:9110", "11110", "서울특별시 종로구 정보공개 운영 조례",
     "행정정보 공개 청구의 처리절차에 필요한 사항을 규정한다."),
]


def _enrich(conn) -> int:
    """검색 테스트용 조례·조문 추가(어휘 분포 현실화). 삽입 문서 수 반환."""
    today = "2026-08-19"
    for oid, rid, name, body in _EXTRA_ORDINANCES:
        conn.execute(
            "INSERT OR IGNORE INTO ordinances "
            "(ordinance_id, mst, region_id, name, ord_kind, status, as_of_date, "
            " verification_status, official_url) VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, oid.split(":")[1], rid, name, "조례", "active", today,
             "source-linked", f"https://law.go.kr/자치법규/{name}"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO ordinance_articles "
            "(oa_id, ordinance_id, article_no, title, body) VALUES (?,?,?,?,?)",
            (f"{oid}::000100", oid, "000100", "목적", body),
        )
    conn.commit()
    return len(_EXTRA_ORDINANCES)


class _Sandbox:
    """(conn, 임시 인덱스 루트) 묶음. with 블록 종료 시 정리."""

    def __init__(self, seed: bool = True, enrich: bool = False):
        self.conn = fresh_db(seed=seed)
        if enrich:
            _enrich(self.conn)
        self.root = Path(tempfile.mkdtemp(prefix="policymap-rag-test-"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.root, ignore_errors=True)
        return False


def _names(hits):
    return [h.get("parent_name") or h.get("name") or h.get("doc_key") for h in hits]


# --------------------------------------------------------------------------- #
# 1) 인덱스 구축 / 증분
# --------------------------------------------------------------------------- #
def test_build_index_covers_corpus():
    rag = _rag()
    with _Sandbox() as sb:
        r = rag.build_index(sb.conn, scope="all", index_dir=sb.root, force=True)
        assert r.get("status") == "ok", r
        n_docs = sb.conn.execute(
            "SELECT (SELECT COUNT(*) FROM ordinance_articles) "
            "     + (SELECT COUNT(*) FROM articles)").fetchone()[0]
        assert r["docs_indexed"] == n_docs, (
            f"색인 문서 {r['docs_indexed']} != 코퍼스 {n_docs}")
        assert r.get("bytes", 0) > 0, "인덱스 파일이 비어 있음"
        stats = rag.index_stats("all", index_dir=sb.root)
        assert stats.get("exists") is True, stats


def test_build_index_refuses_empty_corpus_without_touching_existing_index():
    """빈 DB로 실 인덱스를 덮어쓰는 사고를 막는다."""
    rag = _rag()
    with _Sandbox() as sb:
        rag.build_index(sb.conn, scope="all", index_dir=sb.root, force=True)
        marker = sb.root / "all" / "sentinel.txt"
        marker.write_text("keep", encoding="utf-8")

        empty_conn = fresh_db(seed=False)
        try:
            try:
                rag.build_index(empty_conn, scope="all", index_dir=sb.root, force=True)
            except RuntimeError as exc:
                assert "empty" in str(exc).lower(), exc
            else:
                assert False, "empty corpus build should fail"
        finally:
            empty_conn.close()

        assert marker.exists(), "기존 인덱스 디렉터리를 건드리면 안 된다"


def test_index_incremental_is_noop_without_changes():
    """변경이 없으면 재빌드하지 않고 reused=True 로 반환해야 한다(증분 규율)."""
    rag = _rag()
    with _Sandbox() as sb:
        rag.build_index(sb.conn, scope="all", index_dir=sb.root, force=True)
        again = rag.build_index(sb.conn, scope="all", index_dir=sb.root)
        assert again.get("reused") is True, f"무변경인데 재빌드 발생: {again}"
        assert again.get("added", 0) == 0 and again.get("changed", 0) == 0, again


def test_index_incremental_picks_up_new_article():
    """새 조문을 넣으면 증분 색인이 그것만 추가하고 즉시 검색된다."""
    rag = _rag()
    with _Sandbox() as sb:
        rag.build_index(sb.conn, scope="all", index_dir=sb.root, force=True)
        sb.conn.execute(
            "INSERT INTO ordinance_articles (oa_id, ordinance_id, article_no, title, body) "
            "VALUES (?,?,?,?,?)",
            ("ordin:9003::000900", "ordin:9003", "000900", "특이어휘시험",
             "이 조는 즐겁도다콰트로 라는 시험용 어휘를 포함한다."),
        )
        sb.conn.commit()
        inc = rag.build_index(sb.conn, scope="all", index_dir=sb.root)
        assert inc.get("added", 0) >= 1, f"신규 문서를 잡지 못함: {inc}"
        hits = rag.bm25_search(sb.conn, "즐겁도다콰트로", k=3, index_dir=sb.root)
        assert hits, "신규 조문이 검색되지 않음"
        assert hits[0]["doc_key"] == "ordin:9003::000900", hits[0]


# --------------------------------------------------------------------------- #
# 2) 검색 채널
# --------------------------------------------------------------------------- #
def test_search_channels_recall_seeded_ordinance():
    rag = _rag()
    with _Sandbox(enrich=True) as sb:
        rag.build_index(sb.conn, scope="all", index_dir=sb.root, force=True)
        for label, fn in (("bm25", rag.bm25_search), ("dense", rag.dense_search),
                          ("hybrid", rag.hybrid_search)):
            hits = fn(sb.conn, "주차장 설치 및 관리", k=5, index_dir=sb.root,
                      group_by="parent")
            assert hits, f"{label}: 결과 없음"
            names = " ".join(str(n) for n in _names(hits))
            assert "주차장" in names, f"{label}: 주차장 조례 미회수 — {names}"
            ranks = [h.get("rank") for h in hits if h.get("rank") is not None]
            if ranks:
                assert ranks == list(range(1, len(ranks) + 1)), f"{label} rank 비연속: {ranks}"


def test_hybrid_search_group_by_parent_dedupes():
    """group_by='parent' 는 같은 조례의 조문 히트를 한 줄로 접어야 한다."""
    rag = _rag()
    with _Sandbox(enrich=True) as sb:
        rag.build_index(sb.conn, scope="all", index_dir=sb.root, force=True)
        hits = rag.hybrid_search(sb.conn, "설치 및 관리", k=10, index_dir=sb.root,
                                 group_by="parent")
        pids = [h.get("parent_id") for h in hits]
        assert len(pids) == len(set(pids)), f"부모 중복: {pids}"


def test_body_is_not_mirrored_in_index():
    """인덱스는 메타만 저장하고 본문은 DB 재조회로 채운다(원문 미러링 금지 규율)."""
    rag = _rag()
    with _Sandbox() as sb:
        rag.build_index(sb.conn, scope="all", index_dir=sb.root, force=True)
        plain = rag.hybrid_search(sb.conn, "주차장", k=3, index_dir=sb.root,
                                  group_by="parent", with_text=False)
        withtext = rag.hybrid_search(sb.conn, "주차장", k=3, index_dir=sb.root,
                                     group_by="parent", with_text=True)
        assert plain and withtext
        assert not plain[0].get("text"), "with_text=False 인데 본문이 실려 있음"
        assert withtext[0].get("text"), "with_text=True 인데 본문이 비어 있음"
        # 인덱스 파일 어디에도 조문 본문 원문이 통째로 들어 있지 않아야 한다
        needle = "주차장의 설치 및 관리에 필요한 사항을 규정"
        for path in sb.root.rglob("*"):
            if not path.is_file():
                continue
            blob = path.read_bytes()
            assert needle.encode("utf-8") not in blob, f"본문 미러링 발견: {path.name}"


# --------------------------------------------------------------------------- #
# 3) 그래프 확장(GraphRAG 핵심)
# --------------------------------------------------------------------------- #
def test_graph_expand_returns_traceable_evidence():
    rag = _rag()
    with _Sandbox() as sb:
        rag.build_index(sb.conn, scope="all", index_dir=sb.root, force=True)
        hits = rag.hybrid_search(sb.conn, "주차장 설치 및 관리", k=5, index_dir=sb.root,
                                 group_by="parent")
        nodes = rag.graph_expand(sb.conn, hits, hops=1, query="주차장 설치 및 관리")
        assert nodes, "그래프 확장 결과 없음"
        for n in nodes:
            assert n.get("via"), f"관계(via) 누락: {n}"
            assert n.get("path"), f"근거 경로(path) 누락: {n}"
            assert n.get("seed") is not None, f"시드 추적 정보 누락: {n}"
        # 시드 조례의 위임 상위법(주차장법)이 확장에 잡혀야 한다
        vias = {n["via"] for n in nodes}
        assert "DELEGATED_FROM" in vias, f"위임 관계 미확장: {vias}"
        names = " ".join(str(n.get("name")) for n in nodes)
        assert "주차장법" in names, f"상위법 미도달: {names}"


def test_hybrid_graph_search_surfaces_unreachable_statute():
    """전문검색만으로는 상위 랭크에 못 오는 상위법이 그래프 경로로 진입해야 한다."""
    rag = _rag()
    with _Sandbox() as sb:
        rag.build_index(sb.conn, scope="all", index_dir=sb.root, force=True)
        merged = rag.hybrid_graph_search(sb.conn, "주차장 설치 및 관리", k=10,
                                         index_dir=sb.root, hops=1, graph_weight=1.0)
        assert merged, "융합 결과 없음"
        origins = {m.get("origin") for m in merged}
        assert origins & {"graph", "both"}, f"그래프 기여 없음: {origins}"
        instruments = [m for m in merged if m.get("node_type") == "instrument"]
        assert instruments, f"법령 노드 미진입: {[m.get('name') for m in merged]}"
        # 랭크는 1부터 연속
        ranks = [m["rank"] for m in merged]
        assert ranks == list(range(1, len(ranks) + 1)), ranks


# --------------------------------------------------------------------------- #
# 4) 컨텍스트 조립 — mcp_server 와 동일한 안전 규율
# --------------------------------------------------------------------------- #
def test_answer_context_safety_envelope():
    rag = _rag()
    with _Sandbox() as sb:
        rag.build_index(sb.conn, scope="all", index_dir=sb.root, force=True)
        ctx = rag.answer_context(sb.conn, "주차장 설치 및 관리", k=3, hops=1,
                                 index_dir=sb.root)
        assert ctx.get("execution_allowed") is False, "execution_allowed 규율 위반"
        assert "as_of_date" in ctx, "as_of_date 누락"
        assert ctx.get("disclaimer"), "면책 고지 누락"
        assert ctx.get("seeds"), "근거 조문(seeds) 없음"
        for s in ctx["seeds"]:
            assert s.get("text"), "seed 조문 원문 누락"
        assert "coverage" in ctx, "coverage 요약 누락"
        assert isinstance(ctx.get("citations"), list), "citations 목록 누락"
        # LLM 을 호출하지 않는다(생성 결과 필드가 있으면 계약 위반)
        assert "answer" not in ctx and "generation" not in ctx, \
            "rag 계층이 생성 결과를 반환하고 있음(생성은 MCP 클라이언트 책임)"


def test_community_report_builds_or_degrades():
    """유사도 엣지가 없는 시드 DB 에서도 커뮤니티 리포트가 예외 없이 강등돼야 한다."""
    mod = need("policymap.rag", "build_community_report", "global_search")
    with _Sandbox() as sb:
        r = mod.build_community_report(sb.conn, scope="ordinance_similarity",
                                       index_dir=sb.root)
        assert isinstance(r, dict), r
        assert "communities" in r, r
        out = mod.global_search(sb.conn, "주차장", k=3, index_dir=sb.root)
        assert isinstance(out, list), out  # 관련 커뮤니티 없음 = 빈 목록(정상적 없음)


if __name__ == "__main__":
    sys.exit(run_dict(globals(), "test_rag"))
