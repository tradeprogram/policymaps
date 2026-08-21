"""test_link_audit — 조례↔예산 링크 재계산이 수작업 판정을 보존하는지 검증.

회귀 배경(실측 사고, 2026-08-20): `policymap.run build` 1회로 표본검증 584건의
판정이 증발했다(verified=1 421건→2건, evidence.audit 584건→3건). 원인은
link_ordinance_budget 이 재계산 행을 항상 verified=0 + 새 evidence 로 upsert 한 것.
수작업 판정은 재생성 불가능한 자산이므로 아래 3개 성질을 계약으로 고정한다.

  1. 재계산이 점수·근거를 갱신해도 verified(±1)는 이월된다.
  2. evidence.audit 블록은 이월된다(새 근거와 병합).
  3. prune 은 verified≠0 링크를 삭제하지 않는다(오답 -1 포함 — 지우면 다음
     빌드가 같은 오매칭을 무라벨로 재생성해 재판정이 무한 반복된다).
"""
import json

from _support import need, fresh_db, run_dict


def _links(conn):
    return {(r[0], r[1]): (r[2], r[3], r[4])
            for r in conn.execute(
                "SELECT ordinance_id, budget_id, verified, evidence, confidence "
                "FROM ordinance_budget_link")}


def _seeded_links(conn, analysis):
    analysis.link_ordinance_budget(conn, min_confidence=0.3)
    rows = _links(conn)
    assert rows, "테스트 전제 실패: 시드에서 링크가 생성되지 않았다"
    return rows


def _mark(conn, key, label, reason="테스트 판정"):
    oid, bid = key
    ev = conn.execute("SELECT evidence FROM ordinance_budget_link "
                      "WHERE ordinance_id=? AND budget_id=?", (oid, bid)).fetchone()[0]
    e = json.loads(ev) if ev else {}
    e["audit"] = {"label": label, "reason": reason, "auditor": "test"}
    conn.execute("UPDATE ordinance_budget_link SET verified=?, evidence=? "
                 "WHERE ordinance_id=? AND budget_id=?",
                 (label, json.dumps(e, ensure_ascii=False), oid, bid))
    conn.commit()


def test_rebuild_preserves_verdict_and_audit():
    analysis = need("policymap.graph.analysis", "link_ordinance_budget")
    conn = fresh_db(seed=True)
    rows = _seeded_links(conn, analysis)
    key = sorted(rows)[0]
    _mark(conn, key, 1, "사람이 정답으로 판정")

    # 점수를 흔들어 재계산이 반드시 UPDATE 경로를 타게 만든다(unchanged 로 새지 않게).
    conn.execute("UPDATE ordinance_budget_link SET confidence=0.42 "
                 "WHERE ordinance_id=? AND budget_id=?", key)
    conn.commit()

    out = analysis.link_ordinance_budget(conn, min_confidence=0.3)
    assert out["kept_verdicts"] >= 1, f"판정 이월 카운터가 0: {out}"

    after = _links(conn)
    assert key in after, "판정된 링크가 재계산으로 사라졌다"
    verified, evidence, conf = after[key]
    assert verified == 1, f"verified 가 재계산으로 초기화됐다: {verified}"
    audit = json.loads(evidence or "{}").get("audit")
    assert isinstance(audit, dict), f"evidence.audit 이 소실됐다: {evidence}"
    assert audit.get("label") == 1 and audit.get("auditor") == "test"
    # 판정은 이월되되 점수·근거는 새 계산값으로 갱신되어야 한다.
    assert abs(conf - 0.42) > 1e-9, "재계산이 confidence 를 갱신하지 않았다"


def test_prune_keeps_negative_verdict():
    analysis = need("policymap.graph.analysis", "link_ordinance_budget")
    conn = fresh_db(seed=True)
    rows = _seeded_links(conn, analysis)
    key = sorted(rows)[0]
    _mark(conn, key, -1, "사람이 오답으로 판정")

    # 이 조례를 매칭 불가로 만든다(제목 도메인명사 제거) → 재계산 결과에서 탈락 →
    # prune 대상이 되지만 verified=-1 이므로 남아야 한다.
    conn.execute("UPDATE ordinances SET name='제1호' WHERE ordinance_id=?", (key[0],))
    conn.commit()

    analysis.link_ordinance_budget(conn, min_confidence=0.3)
    after = _links(conn)
    assert key in after, "오답 판정(-1) 링크가 prune 으로 삭제됐다"
    assert after[key][0] == -1, f"오답 판정이 초기화됐다: {after[key][0]}"


def test_unverified_stale_link_is_pruned():
    """대조군: 판정 없는 자동링크는 정상적으로 정리되어야 한다(prune 무력화 방지)."""
    analysis = need("policymap.graph.analysis", "link_ordinance_budget")
    conn = fresh_db(seed=True)
    rows = _seeded_links(conn, analysis)
    key = sorted(rows)[0]

    conn.execute("UPDATE ordinances SET name='제1호' WHERE ordinance_id=?", (key[0],))
    conn.commit()

    out = analysis.link_ordinance_budget(conn, min_confidence=0.3)
    assert key not in _links(conn), "판정 없는 낡은 자동링크가 정리되지 않았다"
    assert out["removed"] >= 1, f"removed 카운터 미반영: {out}"


if __name__ == "__main__":
    raise SystemExit(run_dict(globals(), "test_link_audit"))
