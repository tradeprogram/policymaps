"""국회 의안 ↔ 법령 연결 — 그래프의 고립된 섬을 잇는다.

문제 [실측 2026-08-25]
    bills.enacted_instrument_id 가 19,847건 **전부 NULL** 이라 국회 서브그래프가
    조례 그래프와 엣지를 하나도 공유하지 않는 고립된 섬이었다(전체 엣지의 25.6%).
    "국회 법률이 조례로 내려온다" 는 이 시스템의 핵심 설계 가정이 데이터에서
    검증 불가능했다.

발견
    데이터가 없는 게 아니라 파이프라인이 없었다. 의안명에서 개정 접미를 떼고
    법령명과 정규화 비교하면 **16,423건(82.7%)** 이 붙는다.
    그 결과 고유 법령 1,252개가 연결되고, 그중 994개(79.4%)가 실제로 조례를 낳았으며,
    그 법령들이 낳은 조례 인용은 210,938건 — **전체 위임의 50.0%** 다.

해결
    이름 정규화 매칭으로 bill_instrument_link 테이블을 만든다.
    추론이므로 match_method 를 함께 남긴다(exact-normalized). 원본 테이블은 건드리지 않는다.

사용
    python system/make_bill_instrument_link.py            # 링크 생성
    python system/make_bill_instrument_link.py --dry-run  # 매칭만 세어 본다
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from policymap import db as D
from policymap import util as _util
from policymap.config import load_config

# 의안명 접미. 긴 것부터 지워야 '법률안' 이 '일부개정법률안' 을 먼저 먹지 않는다.
BILL_SUFFIX = re.compile(
    r"\s*(일부개정법률안|전부개정법률안|폐지법률안|제정법률안|법률안|개정안|안)\s*$")

DDL = """
CREATE TABLE IF NOT EXISTS bill_instrument_link (
  bill_id       TEXT NOT NULL,
  instrument_id TEXT NOT NULL,
  match_method  TEXT NOT NULL,
  bill_name     TEXT,
  instrument_name TEXT,
  proc_result   TEXT,
  proc_dt       TEXT,
  computed_at   TEXT,
  PRIMARY KEY (bill_id, instrument_id)
)
"""
IDX = ("CREATE INDEX IF NOT EXISTS ix_bil_inst ON bill_instrument_link(instrument_id)",
       "CREATE INDEX IF NOT EXISTS ix_bil_bill ON bill_instrument_link(bill_id)")


def norm(s: str) -> str:
    """의안명·법령명을 비교 가능한 형태로. 공백 제거 + 개정 접미 제거."""
    return re.sub(r"\s+", "", BILL_SUFFIX.sub("", s or ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    conn = D.connect(cfg.db_path)
    conn.execute("PRAGMA busy_timeout = 300000")
    t0 = time.time()

    idx: dict[str, tuple[str, str]] = {}
    for r in D.fetchall(conn, "SELECT instrument_id, name FROM legal_instrument WHERE name IS NOT NULL"):
        idx.setdefault(norm(r["name"]), (r["instrument_id"], r["name"]))
    print(f"  법령 색인 {len(idx):,}개 ({time.time()-t0:.0f}s)", flush=True)

    bills = D.fetchall(conn, "SELECT bill_id, name, proc_result, proc_dt FROM bills")
    now = _util.now_kst_iso()
    rows, unmatched = [], 0
    for b in bills:
        hit = idx.get(norm(b["name"]))
        if not hit:
            unmatched += 1
            continue
        rows.append((b["bill_id"], hit[0], "exact-normalized", b["name"], hit[1],
                     b["proc_result"], b["proc_dt"], now))

    print(f"  의안 {len(bills):,} · 매칭 {len(rows):,} ({len(rows)/max(len(bills),1):.1%})"
          f" · 미매칭 {unmatched:,}", flush=True)
    inst = {r[1] for r in rows}
    print(f"  연결된 고유 법령 {len(inst):,}개", flush=True)

    if args.dry_run:
        print("  --dry-run: 쓰지 않고 종료")
        return 0

    conn.executescript(DDL)
    for s in IDX:
        conn.execute(s)
    conn.execute("DELETE FROM bill_instrument_link WHERE match_method='exact-normalized'")
    conn.executemany(
        "INSERT OR REPLACE INTO bill_instrument_link "
        "(bill_id,instrument_id,match_method,bill_name,instrument_name,proc_result,proc_dt,computed_at) "
        "VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()

    n = D.fetchone(conn, "SELECT COUNT(*) AS n FROM bill_instrument_link")["n"]
    print(f"  → bill_instrument_link {n:,}행 적재 (총 {time.time()-t0:.0f}s)")
    print("\n  ※ 이 링크는 이름 매칭 추론이다. 동명이법·개정 전후 명칭 변경은 잡지 못한다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
