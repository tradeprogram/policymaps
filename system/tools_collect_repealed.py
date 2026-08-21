"""폐지 자치법규(rr_cls_cd=300204) 메타 수집.

목적 두 가지:
1) '폐지된 조례를 선례로 추천'하는 위험 제거 — 현행 목록(nw=1)에는 없지만,
   같은 정책이 다른 지자체에서 폐지된 사실을 알 수 있어야 한다.
2) 정책 생애주기 분석 — "이 정책은 N곳이 제정했고 M곳이 폐지했다"가 가능해진다.

status='repealed' 로 넣어 기존 현행 통계(159,452)를 오염시키지 않는다.
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from policymap.config import load_config
from policymap.util import HttpClient, now_kst_iso
from policymap import db as D
from policymap.collectors import law as L

KNDS = (("30001", "조례"), ("30002", "규칙"))
BATCH = 100


def region_index(conn):
    idx = {}
    for rid, nm, fn in conn.execute("SELECT region_id,name,full_name FROM regions"):
        for key in (fn, nm):
            if key:
                idx.setdefault(str(key).replace(" ", ""), rid)
    return idx


def main():
    cfg = load_config(); http = HttpClient(); conn = D.connect()
    ridx = region_index(conn)
    t0 = time.time(); total_ins = 0; calls = 0; unmatched = 0
    for knd, label in KNDS:
        tot, _ = L.ordin_list(http, cfg, query=None, org=None, sborg=None,
                              knd=knd, rr_cls_cd="300204", display=1, page=1)
        calls += 1
        pages = (tot + BATCH - 1) // BATCH
        print(f"[{label}] 폐지 {tot:,}건 / {pages}페이지", flush=True)
        for page in range(1, pages + 1):
            try:
                _, rows = L.ordin_list(http, cfg, query=None, org=None, sborg=None,
                                       knd=knd, rr_cls_cd="300204", display=BATCH, page=page)
                calls += 1
            except Exception as e:  # noqa: BLE001
                print(f"  page {page} 실패: {type(e).__name__}", flush=True); continue
            payload = []
            for r in rows:
                mst = r.get("자치법규일련번호")
                if not mst:
                    continue
                org = str(r.get("지자체기관명") or "")
                rid = ridx.get(org.replace(" ", ""))
                if rid is None:
                    for pre in ("(구)", "（구）"):
                        if org.startswith(pre):
                            rid = ridx.get(org[len(pre):].replace(" ", "")); break
                if rid is None:
                    unmatched += 1
                payload.append({
                    "ordinance_id": f"ordin:{mst}",
                    "mst": str(mst),
                    "ord_id": str(r.get("자치법규ID") or ""),
                    "region_id": rid,
                    "org_name": org,
                    "name": r.get("자치법규명"),
                    "ord_kind": label,
                    "promulgation_no": r.get("공포번호"),
                    "enacted_on": r.get("공포일자"),
                    "effective_on": r.get("시행일자"),
                    "repealed_on": r.get("시행일자") or r.get("공포일자"),
                    "rr_cls_cd": r.get("제개정구분명"),
                    "official_url": r.get("자치법규상세링크"),
                    "status": "repealed",
                    "verification_status": "needs-review",
                    "as_of_date": now_kst_iso()[:10],
                    "updated_at": now_kst_iso(),
                })
            for attempt in range(8):
                try:
                    with D.tx(conn):
                        if payload:
                            D.upsert_many(conn, "ordinances", payload, ("ordinance_id",))
                    break
                except Exception as e:  # noqa: BLE001
                    if "lock" not in str(e).lower():
                        raise
                    time.sleep(20)
            total_ins += len(payload)
            if page % 25 == 0 or page == pages:
                print(f"  {label} {page}/{pages} 누적 {total_ins:,} | {time.time()-t0:.0f}s", flush=True)
    print(f"[finish] 폐지 {total_ins:,}건 적재 / 지역 미매칭 {unmatched:,} / 호출 {calls} / {(time.time()-t0)/60:.1f}분", flush=True)


if __name__ == "__main__":
    main()
