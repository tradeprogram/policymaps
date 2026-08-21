"""본문 전수 수집 완료를 감지해 후속 4단계를 자동 완주하는 파이프라인.

1) 위임관계 백필 전건 재실행 (커서 리셋 — 새로 들어온 본문 포함)
2) 카테고리 재분류 전건 (목적 조문 활용으로 커버리지 상승)
3) RAG 인덱스 전면 재구축 (--force)
4) 그래프 재빌드 + 정적 번들 export

세션과 무관하게 완주하도록 분리 프로세스로 띄운다.
각 단계는 실패해도 다음 단계로 넘어가되 결과를 status 파일에 남긴다.
"""
import json, subprocess, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
STATUS = ROOT / "data" / "finalize_status.json"
PY_EXE = sys.executable

# 수집 정체 판정: 이 시간 동안 진전이 없으면 수집이 끝난 것으로 본다
STALL_MIN = 25
POLL_SEC = 180


def remaining() -> int:
    c = sqlite3.connect(f"file:{ROOT/'data'/'policymap.db'}?mode=ro", uri=True)
    c.execute("PRAGMA busy_timeout=60000")
    n = c.execute(
        "SELECT COUNT(*) FROM ordinances WHERE (article_count IS NULL OR article_count=0) "
        "AND (status IS NULL OR status<>'repealed')").fetchone()[0]
    c.close()
    return int(n)


def write(**kw):
    try:
        cur = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    except Exception:
        cur = {}
    cur.update(kw)
    STATUS.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def run(name: str, args: list[str], timeout: int = 14400) -> dict:
    print(f"\n{'='*60}\n[{name}] 시작: {' '.join(args)}\n{'='*60}", flush=True)
    t0 = time.time()
    try:
        p = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        tail = "\n".join((p.stdout or "").strip().splitlines()[-12:])
        err = "\n".join((p.stderr or "").strip().splitlines()[-6:])
        ok = p.returncode == 0
        print(tail, flush=True)
        if err:
            print("[stderr]", err, flush=True)
        res = {"ok": ok, "rc": p.returncode, "sec": round(time.time() - t0), "tail": tail}
    except subprocess.TimeoutExpired:
        res = {"ok": False, "rc": -1, "sec": round(time.time() - t0), "tail": "timeout"}
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "rc": -2, "sec": round(time.time() - t0), "tail": f"{type(e).__name__}: {e}"}
    print(f"[{name}] 완료 ok={res['ok']} {res['sec']}s", flush=True)
    write(**{name: res})
    return res


def reset_cursor(source: str, scope: str) -> None:
    """전건 재처리를 위해 워터마크 커서를 비운다(신규 본문이 커서 아래에도 생기므로)."""
    try:
        from policymap import db as D
        conn = D.connect()
        with D.tx(conn):
            D.set_watermark(conn, source, scope, cursor="", status="reset")
        conn.close()
        print(f"[reset] {source}/{scope} 커서 초기화", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[reset] 실패({source}/{scope}): {e}", flush=True)


def wait_for_collection() -> int:
    last, stall = None, 0
    while True:
        left = remaining()
        if left <= 0:
            print(f"[wait] 수집 완료(남음 0)", flush=True)
            return left
        if last is not None and left >= last:
            stall += POLL_SEC
            if stall >= STALL_MIN * 60:
                print(f"[wait] {STALL_MIN}분간 진전 없음 → 수집 종료로 간주(남음 {left:,})", flush=True)
                return left
        else:
            stall = 0
        last = left
        write(state="waiting", remaining=left, stalled_sec=stall)
        print(f"[wait] 남음 {left:,} (정체 {stall//60}분)", flush=True)
        time.sleep(POLL_SEC)


def main() -> int:
    t0 = time.time()
    write(state="start", started_at=t0)
    left = wait_for_collection()
    write(state="running", remaining_at_start=left)

    # 1) 위임관계 백필 — 전건
    reset_cursor("deleg_backfill", "ordinance-bodies")
    run("delegations", [PY_EXE, "tools_backfill_delegations.py", "500"])

    # 2) 카테고리 재분류 — 전건(본문 확보분은 목적 조문까지 반영)
    reset_cursor("category_clf", "all")
    run("categories", [PY_EXE, "tools_seed_categories.py"])

    # 3) RAG 인덱스 전면 재구축
    run("rag_index", [PY_EXE, "-m", "policymap.run", "index", "--force"])

    # 4) 그래프 재빌드 + export
    run("graph_build", [PY_EXE, "-m", "policymap.run", "build"])
    run("export", [PY_EXE, "-m", "policymap.run", "export"])

    write(state="done", total_sec=round(time.time() - t0))
    print(f"\n[finish] 전체 {(time.time()-t0)/60:.1f}분", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
