"""자치법규 본문 전수 수집 드라이버 — 장시간 백그라운드 실행용.

collect_bodies() 를 청크 단위로 반복 호출해 남은 전건을 채운다.
- 재개 가능: 워터마크 + article_count NULL 기준이라 중단 후 재실행하면 이어짐
- 자동 백오프: 청크 오류율이 높으면 qps 를 낮추고, 연속 실패 시 대기
- 진행상황: data/bulk_bodies_status.json 에 기록(외부에서 폴링 가능)
"""
import json, sys, time, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from policymap.config import load_config
from policymap.util import HttpClient
from policymap import db as D
from policymap.collectors import ordin_bulk

STATUS = Path("data/bulk_bodies_status.json")
CHUNK = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 6
QPS = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0


def remaining(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM ordinances "
        "WHERE (article_count IS NULL OR article_count = 0) "
        "AND (status IS NULL OR status <> 'repealed')"
    ).fetchone()
    return int(row[0])


def write_status(**kw):
    try:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(kw, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    cfg = load_config()
    conn = D.connect()
    started = time.time()
    total0 = remaining(conn)
    done = 0
    chunk_no = 0
    qps = QPS
    consecutive_fail = 0
    print(f"[start] 남은 본문 {total0:,}건 / chunk={CHUNK} workers={WORKERS} qps={qps}", flush=True)

    while True:
        left = remaining(conn)
        if left <= 0:
            print("[done] 남은 건수 0 — 전수 완료", flush=True)
            break
        chunk_no += 1
        t0 = time.time()
        try:
            res = ordin_bulk.collect_bodies(
                HttpClient(), cfg, conn,
                limit=min(CHUNK, left), priority="recent",
                workers=WORKERS, qps=qps, log_every=1000,
                run_id=f"bulk-bodies-{chunk_no}",
            )
            consecutive_fail = 0
        except Exception as e:
            msg = str(e).lower()
            # DB 락은 일시적 경합(다른 에이전트의 쓰기)일 뿐이므로 실패로 세지 않는다.
            if "database is locked" in msg or "database is busy" in msg:
                print(f"[chunk {chunk_no}] DB 락 경합 — 30초 후 재시도", flush=True)
                time.sleep(30)
                continue
            consecutive_fail += 1
            print(f"[chunk {chunk_no}] 예외 {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            if consecutive_fail >= 8:
                print("[abort] 연속 8회 실패 — 중단(재실행하면 이어짐)", flush=True)
                write_status(state="error", remaining=left, done=done, chunk=chunk_no)
                return 1
            time.sleep(60 * consecutive_fail)
            continue

        got = int(res.get("updated", 0) or 0)
        errs = int(res.get("errors", 0) or 0)
        done += got
        dt = time.time() - t0
        left_after = remaining(conn)
        rate = done / max(1e-9, time.time() - started)
        eta_h = left_after / rate / 3600 if rate > 0 else -1
        print(f"[chunk {chunk_no}] +{got} err={errs} {dt:.0f}s | 남음 {left_after:,} "
              f"| {rate:.2f}건/s | ETA {eta_h:.1f}h", flush=True)
        write_status(state="running", started_at=started, elapsed_sec=round(time.time() - started),
                     total_at_start=total0, collected=done, remaining=left_after,
                     rate_per_sec=round(rate, 3), eta_hours=round(eta_h, 2),
                     chunk=chunk_no, qps=qps, errors_last_chunk=errs)

        # 오류율 높으면 감속, 안정적이면 회복
        if got and errs / max(1, got + errs) > 0.10:
            qps = max(2.0, qps * 0.6)
            print(f"[backoff] 오류율 높음 → qps={qps:.1f}", flush=True)
            time.sleep(30)
        elif errs == 0 and qps < QPS:
            qps = min(QPS, qps * 1.25)
        if got == 0 and errs == 0:
            print("[warn] 진전 없음 — 대상 없음으로 간주하고 종료", flush=True)
            break

    write_status(state="done", elapsed_sec=round(time.time() - started),
                 total_at_start=total0, collected=done, remaining=remaining(conn))
    print(f"[finish] 총 {done:,}건 수집, {(time.time()-started)/3600:.1f}시간", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
