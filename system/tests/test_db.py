"""test_db — 코어(config/util/db) 실측 테스트. 병렬 모듈 무관, 항상 실행.

대상: db.init_db/upsert(해시가드)/upsert_many/soft_delete/watermark/log_change/count,
      util.compact/content_hash/stable_id/as_list/retry, config.load_config/require,
      그리고 예산 CSV → budget_lines 매핑 적재(스키마 왕복 검증).
"""
import os

from _support import (
    fresh_db, raw_db, load_csv_rows, run_dict,
    pm_config as config, pm_db as db, pm_util as util,
)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_config_defaults_and_require():
    cfg = config.load_config(env_path="__no_such_env__")
    assert cfg.law_base.startswith("https://")
    assert cfg.assembly_base.startswith("https://")
    # 키 미설정 → require 예외
    raised = False
    try:
        cfg.require("law_oc")
    except RuntimeError:
        raised = True
    assert raised, "미설정 키에 require 가 RuntimeError 를 던져야 함"
    # 채우면 통과
    cfg.law_oc = "someid"
    cfg.require("law_oc")  # 예외 없어야 함


def test_config_env_override():
    old = os.environ.get("LAW_OC")
    os.environ["LAW_OC"] = "envtestid"
    try:
        config.reset_config()
        cfg = config.load_config()
        assert cfg.law_oc == "envtestid"
    finally:
        if old is None:
            os.environ.pop("LAW_OC", None)
        else:
            os.environ["LAW_OC"] = old
        config.reset_config()


# --------------------------------------------------------------------------- #
# util
# --------------------------------------------------------------------------- #
def test_util_compact_and_hash():
    # 공백·낫표·가운뎃점 제거 결정성
    a = util.compact("「주차장법」 제12조ㆍ제2항")
    b = util.compact("주차장법제12조제2항")
    assert a == b, f"compact 정규화 불일치: {a!r} != {b!r}"
    assert util.compact(None) == ""
    # content_hash: 접두 + 정규화 동치
    h1 = util.content_hash("가 나\t다")
    h2 = util.content_hash("가나다")
    assert h1.startswith("sha256:")
    assert h1 == h2, "정규화 후 동일 콘텐츠는 동일 해시여야 함"
    assert util.content_hash("다른내용") != h1


def test_util_stable_id_and_as_list():
    i1 = util.stable_id("a", "b", None, 3)
    i2 = util.stable_id("a", "b", None, 3)
    assert i1 == i2 and len(i1) == 16
    assert util.stable_id("a", "b", None, 4) != i1
    assert util.as_list(None) == []
    assert util.as_list([1, 2]) == [1, 2]
    assert util.as_list({"x": 1}) == [{"x": 1}]  # 단건 dict → 리스트화
    assert util.as_list("s") == ["s"]


def test_util_now_kst_iso():
    s = util.now_kst_iso()
    assert s.endswith("+0900"), f"KST 오프셋(+0900) 필요: {s}"
    assert "T" in s


def test_util_retry_transient_then_success():
    calls = {"n": 0}

    @util.retry(retries=3, base=0.0)  # base=0 → 슬립 없이
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise util.TransientError("일시적")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3, "TransientError 2회 후 성공까지 3콜"


def test_util_retry_notfound_not_retried():
    calls = {"n": 0}

    @util.retry(retries=3, base=0.0)
    def missing():
        calls["n"] += 1
        raise util.NotFound("정상적 없음")

    raised = False
    try:
        missing()
    except util.NotFound:
        raised = True
    assert raised
    assert calls["n"] == 1, "NotFound 는 재시도하지 않아야 함"


# --------------------------------------------------------------------------- #
# db 초기화 / 스키마
# --------------------------------------------------------------------------- #
def test_init_db_creates_schema():
    conn = raw_db()
    tables = {r["name"] for r in db.fetchall(
        conn, "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("regions", "legal_instrument", "ordinances", "delegations",
              "watermarks", "change_log", "budget_lines", "bills", "votes"):
        assert t in tables, f"필수 테이블 누락: {t}"
    ver = db.fetchone(conn, "SELECT value FROM schema_meta WHERE key='schema_version'")
    assert ver and ver["value"] == "1"


# --------------------------------------------------------------------------- #
# upsert 해시가드
# --------------------------------------------------------------------------- #
def _ord_row(cid, name, chash, **extra):
    row = {"ordinance_id": cid, "mst": cid.split(":")[-1], "name": name,
           "ord_kind": "조례", "content_hash": chash}
    row.update(extra)
    return row


def test_upsert_insert_update_unchanged():
    conn = raw_db()
    r = _ord_row("ordin:T1", "테스트 조례", "sha256:aaa")
    assert db.upsert(conn, "ordinances", r, "ordinance_id", hash_col="content_hash") == "inserted"
    # 동일 해시 → unchanged (무변경=무이벤트)
    assert db.upsert(conn, "ordinances", r, "ordinance_id", hash_col="content_hash") == "unchanged"
    # 해시 변경 → updated
    r2 = _ord_row("ordin:T1", "테스트 조례(개정)", "sha256:bbb")
    assert db.upsert(conn, "ordinances", r2, "ordinance_id", hash_col="content_hash") == "updated"
    got = db.fetchone(conn, "SELECT name, content_hash FROM ordinances WHERE ordinance_id=?",
                      ("ordin:T1",))
    assert got["name"] == "테스트 조례(개정)" and got["content_hash"] == "sha256:bbb"


def test_upsert_ignores_extra_keys_and_requires_pk():
    conn = raw_db()
    # 스키마에 없는 여분 키는 무시(collectors 느슨한 dict 전달 대응)
    r = _ord_row("ordin:T2", "여분키 조례", "sha256:c", 존재하지않는컬럼="무시됨", junk=123)
    assert db.upsert(conn, "ordinances", r, "ordinance_id") == "inserted"
    assert db.count(conn, "ordinances", "ordinance_id=?", ("ordin:T2",)) == 1
    # PK 값 없으면 ValueError
    raised = False
    try:
        db.upsert(conn, "ordinances", {"name": "무PK", "ord_kind": "조례"}, "ordinance_id")
    except ValueError:
        raised = True
    assert raised


def test_upsert_many_counts():
    conn = raw_db()
    rows = [_ord_row(f"ordin:M{i}", f"조례{i}", f"sha256:{i}") for i in range(3)]
    counts = db.upsert_many(conn, "ordinances", rows, "ordinance_id", hash_col="content_hash")
    assert counts == {"inserted": 3, "updated": 0, "unchanged": 0}
    # 재적재 → 모두 unchanged
    counts2 = db.upsert_many(conn, "ordinances", rows, "ordinance_id", hash_col="content_hash")
    assert counts2["unchanged"] == 3


def test_soft_delete_tombstone():
    conn = raw_db()
    db.upsert(conn, "ordinances", _ord_row("ordin:D1", "폐지대상", "sha256:z"), "ordinance_id")
    db.soft_delete(conn, "ordinances", {"ordinance_id": "ordin:D1"},
                   status="repealed", valid_to_col="repealed_on", valid_to="20260101")
    got = db.fetchone(conn, "SELECT status, repealed_on FROM ordinances WHERE ordinance_id=?",
                      ("ordin:D1",))
    assert got["status"] == "repealed" and got["repealed_on"] == "20260101"
    # 하드삭제 아님 — 행은 존재
    assert db.count(conn, "ordinances", "ordinance_id=?", ("ordin:D1",)) == 1


# --------------------------------------------------------------------------- #
# watermark
# --------------------------------------------------------------------------- #
def test_watermark_lifecycle():
    conn = raw_db()
    assert db.get_watermark(conn, "ordin", "sig:11110") is None
    db.set_watermark(conn, "ordin", "sig:11110", status="partial", rows_seen=10)
    wm = db.get_watermark(conn, "ordin", "sig:11110")
    assert wm["status"] == "partial" and wm["rows_seen"] == 10
    assert wm["last_run"], "last_run 자동 채움"
    # 파티션 완전 영속화 후 커서 전진
    db.advance_cursor(conn, "ordin", "sig:11110", cursor="efYd:20260101|maxMST:9999",
                      last_hash="sha256:part", status="ok", changed=5, rows_seen=42)
    wm2 = db.get_watermark(conn, "ordin", "sig:11110")
    assert wm2["cursor"].startswith("efYd:")
    assert wm2["status"] == "ok" and wm2["changed"] == 5
    assert wm2["last_success"], "advance_cursor 는 last_success 를 채워야 함"
    # 실패 파티션 격리 + retry 증가
    db.mark_partition_status(conn, "ordin", "sig:11110", "error",
                             note="타임아웃", bump_retry=True)
    wm3 = db.get_watermark(conn, "ordin", "sig:11110")
    assert wm3["status"] == "error" and wm3["retry_count"] == 1
    # 커서는 보존(미전진)
    assert wm3["cursor"] == wm2["cursor"]


def test_watermark_composite_key_isolation():
    conn = raw_db()
    db.set_watermark(conn, "budget", "laf:11110|fyr:2026", cursor="A")
    db.set_watermark(conn, "budget", "laf:26110|fyr:2026", cursor="B")
    assert db.get_watermark(conn, "budget", "laf:11110|fyr:2026")["cursor"] == "A"
    assert db.get_watermark(conn, "budget", "laf:26110|fyr:2026")["cursor"] == "B"
    assert db.count(conn, "watermarks") == 2


# --------------------------------------------------------------------------- #
# change_log
# --------------------------------------------------------------------------- #
def test_log_change_append():
    conn = raw_db()
    cid = db.log_change(conn, entity_type="ordinance", entity_id="ordin:9001",
                        event="amended", source="ordin", scope="sig:11110",
                        entity_name="종로구 주차장 조례", region_code="11110",
                        official_url="https://www.law.go.kr/...")
    assert cid and len(cid) == 32  # uuid4 hex
    assert db.count(conn, "change_log") == 1
    row = db.fetchone(conn, "SELECT * FROM change_log WHERE change_id=?", (cid,))
    assert row["event"] == "amended" and row["region_code"] == "11110"
    assert row["ts"], "ts 자동 채움"


# --------------------------------------------------------------------------- #
# 예산 CSV → budget_lines (스키마 왕복, 병렬 모듈 무관)
# --------------------------------------------------------------------------- #
def test_budget_csv_ingest():
    # region_id 를 매핑하지 않으므로(NULL) FK 무관 → 시드 없는 raw_db 사용(ID 충돌 회피)
    conn = raw_db()
    rows = load_csv_rows("budget_sample.csv")
    assert len(rows) == 3
    mapped = []
    for i, r in enumerate(rows, start=1):
        mapped.append({
            "budget_id": f"ehojo-{r['laf_cd']}-{r['fyr']}-{i:04d}",
            "fyr": int(r["fyr"]), "laf_cd": r["laf_cd"], "dbiz_cd": r["dbiz_cd"],
            "dbiz_nm": r["dbiz_nm"], "dept_cd": r["dept_cd"], "field": r["field"],
            "sector": r["sector"], "budget_now": int(r["budget_now"]),
            "gov_fund": int(r["gov_fund"]), "sido_fund": int(r["sido_fund"]),
            "sigungu_fund": int(r["sigungu_fund"]), "alloc_amt": int(r["alloc_amt"]),
            "exe_amt": int(r["exe_amt"]), "exe_ymd": r["exe_ymd"],
        })
    counts = db.upsert_many(conn, "budget_lines", mapped, "budget_id")
    assert counts["inserted"] == 3
    assert db.count(conn, "budget_lines") == 3
    total_exe = db.fetchone(
        conn, "SELECT SUM(exe_amt) AS s FROM budget_lines WHERE laf_cd='11110'")["s"]
    assert total_exe == 500000 + 220000


if __name__ == "__main__":
    import sys
    sys.exit(run_dict(globals(), "test_db"))
