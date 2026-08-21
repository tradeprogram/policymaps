"""test_parsers — parsers.article/delegation/category/embedding 순수함수 테스트.

병렬 구현 중이므로 미구현 시 need()가 skip. 계약(CONTRACTS.md §2)에 맞춰 선작성.
"""
import json

from _support import (
    need, skip, fresh_db, raw_db, load_json, run_dict,
    pm_db as db, pm_util as util,
)


# --------------------------------------------------------------------------- #
# article.py
# --------------------------------------------------------------------------- #
def _find(rows, no, branch=None):
    for r in rows:
        if str(r.get("article_no")) == str(no) and \
           (branch is None or str(r.get("article_branch") or "") == str(branch)):
            return r
    return None


def test_parse_law_articles():
    art = need("policymap.parsers.article", "parse_law_articles")
    body = load_json("law_body.json")
    rows = art.parse_law_articles(body, "statute:001234")
    assert isinstance(rows, list)
    # 조문여부=='조문'만 → 전문(부칙) 제외 → 3건
    assert len(rows) == 3, f"조문만 3건 기대, got {len(rows)}"
    ids = {r["article_id"] for r in rows}
    assert "statute:001234::제1조" in ids
    # 제2조의2(조문가지번호=2)
    branch_row = _find(rows, "2", branch="2")
    assert branch_row is not None, "제2조의2(가지번호) 파싱 필요"
    assert branch_row["article_id"].endswith("제2조의2")
    # 모든 row instrument_id 일치 + content_hash 존재
    for r in rows:
        assert r["instrument_id"] == "statute:001234"
        assert str(r.get("content_hash", "")).startswith("sha256:")
    # 제2조 본문에 항/호 접힘(자동차/노상주차장 텍스트 포함)
    a2 = _find(rows, "2", branch=None) or _find(rows, "2", branch="")
    assert a2 is not None
    assert "자동차" in (a2.get("body") or ""), "항 내용이 body 에 접혀야 함"


def test_parse_ordinance_articles_and_save():
    art = need("policymap.parsers.article", "parse_ordinance_articles")
    body = load_json("ordinance_body.json")
    jomun = body["LawService"]["조문"]["조"]
    rows = art.parse_ordinance_articles(jomun, "ordin:9001")
    assert isinstance(rows, list) and len(rows) == 2
    first = rows[0]
    assert first["oa_id"] == "ordin:9001::000100", f"oa_id 형식 위반: {first['oa_id']}"
    assert first["ordinance_id"] == "ordin:9001"
    assert "「주차장법」" in (first.get("body") or ""), "상위법 낫표 인용 보존 필요"
    # save_articles 왕복(있으면)
    if hasattr(art, "save_articles"):
        conn = fresh_db(seed=True)  # ordin:9001 존재(FK)
        counts = art.save_articles(conn, rows, table="ordinance_articles")
        assert sum(counts.values()) == 2


# --------------------------------------------------------------------------- #
# delegation.py
# --------------------------------------------------------------------------- #
def test_parse_citations():
    dele = need("policymap.parsers.delegation", "parse_citations")
    text = "이 조례는 「주차장법」 제12조제1항제2호에 따라 필요한 사항을 규정한다."
    cites = dele.parse_citations(text)
    assert isinstance(cites, list) and len(cites) >= 1
    c = cites[0]
    assert c["law_name"] == "주차장법", f"법령명 파싱 오류: {c}"
    # article 은 '제N조[의M]' 라벨 형태(항/호는 clause/item 정수)
    assert c["article"] == "제12조", f"조 라벨 파싱 오류: {c}"
    assert c["clause"] == 1 and c["item"] == 2, f"항/호 파싱 오류: {c}"


def test_merge_delegations_dedup():
    dele = need("policymap.parsers.delegation", "merge_delegations")
    # 동일 (자치법규일련번호=child_id + 조문=child_article) 이 서로 다른 경로에서 중복 수신
    weak = [{"child_id": "ordin:9001", "child_article": "제1조",
             "parent_id": "statute:001234", "parent_article": "제12조",
             "source_path": "lsStmd", "relation": "DELEGATED_FROM"}]
    strong = [{"child_id": "ordin:9001", "child_article": "제1조",
               "parent_id": "statute:001234", "parent_article": "제12조",
               "source_path": "lnkLsOrdJo", "relation": "DELEGATED_FROM"},
              {"child_id": "ordin:9002", "child_article": "제1조",
               "parent_id": "statute:001234", "parent_article": "제12조",
               "source_path": "lnkLsOrdJo", "relation": "DELEGATED_FROM"}]
    merged = dele.merge_delegations(weak, strong)
    assert isinstance(merged, list)
    # 3입력 → 중복 1쌍 병합 → 2건
    keys = {(m["child_id"], m.get("child_article")) for m in merged}
    assert len(keys) == 2, f"dedup 후 유일 키 2개 기대: {keys}"
    assert len(merged) == 2, f"dedup 후 2건 기대, got {len(merged)}"
    # 병합 생존자는 조문정밀 경로(lnkLsOrdJo/lsDelegated) 우선
    surv = next(m for m in merged if m["child_id"] == "ordin:9001")
    assert surv["source_path"] in ("lnkLsOrdJo", "lsDelegated"), \
        f"조문정밀 경로 우선 위반: {surv['source_path']}"


def test_extract_delegations_from_lsdelegated():
    dele = need("policymap.parsers.delegation", "extract_delegations_from_lsdelegated")
    # lsDelegated.법령.위임조문정보[].위임정보[] — 위임구분=='자치법규' 만 조례 위임으로 채택
    resp = {"lsDelegated": {"법령": {"위임조문정보": [
        {"조정보": {"조문번호": "12"},
         "위임정보": [
             {"위임구분": "자치법규", "위임법령일련번호": "9001",
              "위임법령제목": "종로구 주차장 조례",
              "위임법령조문정보": [{"조항호목": "제1조",
                                    "링크텍스트": "종로구 주차장 조례 제1조"}]},
             {"위임구분": "대통령령", "위임법령일련번호": "5555"},
         ]},
    ]}}}
    rows = dele.extract_delegations_from_lsdelegated(resp, "statute:001234")
    assert isinstance(rows, list)
    # 자치법규 위임 1건만
    assert len(rows) == 1, f"자치법규 위임만 채택 기대: {rows}"
    assert rows[0]["source_path"] == "lsDelegated"
    assert rows[0]["child_id"] == "ordin:9001", f"하위 조례 child_id 오류: {rows[0]}"
    assert rows[0]["parent_article"] == "12", f"상위 위임조문 오류: {rows[0]}"


# --------------------------------------------------------------------------- #
# category.py
# --------------------------------------------------------------------------- #
def test_classify_ordinance_rule():
    cat = need("policymap.parsers.category", "classify_ordinance")
    ordinance = {"ordinance_id": "ordin:9001",
                 "name": "서울특별시 종로구 주차장 설치 및 관리 조례"}
    articles = [{"body": "이 조례는 주차장의 설치 및 관리에 관한 사항을 규정한다."}]
    categories = [
        {"code": "C01", "name": "교통ㆍ주차",
         "keywords": json.dumps(["주차", "주차장", "교통"], ensure_ascii=False)},
        {"code": "C02", "name": "문화ㆍ도서관",
         "keywords": json.dumps(["도서관", "문화"], ensure_ascii=False)},
    ]
    rows = cat.classify_ordinance(ordinance, articles, categories)
    assert isinstance(rows, list) and len(rows) >= 1
    codes = {r["category_code"] for r in rows}
    assert "C01" in codes, f"주차 키워드 → C01 기대: {rows}"
    assert all(r.get("method") in ("rule", "llm") for r in rows)


# --------------------------------------------------------------------------- #
# embedding.py
# --------------------------------------------------------------------------- #
def test_embedder_fallback_similarity():
    emb = need("policymap.parsers.embedding", "Embedder")
    e = emb.Embedder()  # 기본 폴백(char-ngram-tf)
    v1 = e.embed("주차장 설치 및 관리")
    v2 = e.embed("주차장 관리 운영")
    v3 = e.embed("도서관 운영 및 독서 진흥")
    # 자기 유사도 ≈ 1
    assert e.similarity(v1, v1) > 0.99
    s_related = e.similarity(v1, v2)
    s_unrelated = e.similarity(v1, v3)
    assert 0.0 <= s_unrelated <= 1.0 and 0.0 <= s_related <= 1.0
    assert s_related > s_unrelated, \
        f"주차장 유사쌍이 더 높아야 함: related={s_related:.3f} unrelated={s_unrelated:.3f}"


def test_embed_ordinances_and_similarity():
    emb = need("policymap.parsers.embedding", "embed_ordinances", "build_similarity")
    conn = fresh_db(seed=True)
    r1 = emb.embed_ordinances(conn)
    assert isinstance(r1, dict)
    assert db.count(conn, "embeddings") > 0, "임베딩 적재 필요"
    r2 = emb.build_similarity(conn, top_k=5)
    assert isinstance(r2, dict)
    assert db.count(conn, "similarity_edges") > 0, "유사도 엣지 적재 필요"
    # 9001(주차장)의 최근접이 9002(주차장)인지 — 있으면 검증
    edge = db.fetchone(
        conn, "SELECT cosine_sim FROM similarity_edges "
              "WHERE src_id=? AND dst_id=?", ("ordin:9001", "ordin:9002"))
    if edge is not None:
        assert edge["cosine_sim"] >= 0.0


if __name__ == "__main__":
    import sys
    sys.exit(run_dict(globals(), "test_parsers"))
