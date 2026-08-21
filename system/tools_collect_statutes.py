"""전국 현행법령·행정규칙 메타 수집 → legal_instrument 채우기.

위임관계의 상위법 해석률이 40.9% 에 그친 원인은 이름 정규화가 아니라
legal_instrument 에 법령이 627건(korea100 시드분)뿐이었다는 것이다. [실측]
전국 법령 5,611 + 행정규칙 24,135 를 채우면 lawname: 명목키가 실 instrument_id 로 승격된다.
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from policymap.config import load_config
from policymap.util import HttpClient, now_kst_iso
from policymap import db as D
from policymap.collectors import law as L

BATCH = 100
TIER = {"헌법": 0, "법률": 1, "조약": 1, "대통령령": 2, "총리령": 3, "부령": 3}


def kind_of(row: dict) -> tuple[str, int]:
    k = (row.get("법종구분명") or "").strip()
    if not k:
        nm = row.get("법령명한글") or ""
        if nm.endswith("시행령"):
            k = "대통령령"
        elif nm.endswith("시행규칙"):
            k = "부령"
        else:
            k = "법률"
    return k, TIER.get(k, 3)


def ensure_kind(conn, kinds: set, *, source_type: str, tier: int) -> None:
    """instrument_kind 는 kind/source_type 이 NOT NULL 이다. 미등록 종류를 시드해
    legal_instrument.kind FK 위반을 막는다(행정규칙 훈령·지침·공고 등)."""
    for k in kinds:
        try:
            with D.tx(conn):
                conn.execute(
                    "INSERT INTO instrument_kind(kind, source_type, national_tier, note) "
                    "VALUES(?,?,?,?) ON CONFLICT(kind) DO NOTHING",
                    (k, source_type, tier, "자동 시드(전국 법령·행정규칙 수집)"))
        except Exception as e:  # noqa: BLE001
            print(f"  kind 시드 실패 {k}: {str(e)[:60]}", flush=True)


def collect(http, cfg, conn, *, target: str) -> int:
    fn = L.law_list if target == "law" else L.admrul_list
    tot, _ = fn(http, cfg, None, display=1, page=1)
    pages = (tot + BATCH - 1) // BATCH
    print(f"[{target}] {tot:,}건 / {pages}페이지", flush=True)
    got = 0
    t0 = time.time()
    for page in range(1, pages + 1):
        try:
            _, rows = fn(http, cfg, None, display=BATCH, page=page)
        except Exception as e:  # noqa: BLE001
            print(f"  page {page} 실패 {type(e).__name__}", flush=True)
            continue
        payload, kinds = [], set()
        for r in rows:
            if target == "law":
                mst = r.get("법령일련번호"); name = r.get("법령명한글")
                iid = f"statute:{mst}" if mst else None
                kind, tier = kind_of(r)
                url = r.get("법령상세링크")
                promu, eff = r.get("공포일자"), r.get("시행일자")
            else:
                mst = r.get("행정규칙일련번호"); name = r.get("행정규칙명")
                iid = f"admrul:{mst}" if mst else None
                kind, tier = (r.get("행정규칙종류") or "행정규칙"), 4
                url = r.get("행정규칙상세링크")
                promu, eff = r.get("발령일자"), r.get("시행일자")
            if not iid or not name:
                continue
            kinds.add(kind)
            payload.append({
                "instrument_id": iid, "mst": str(mst), "name": str(name).strip(),
                "source_type": ("statute" if target == "law" else "admin-rule"),
                "kind": kind, "tier": tier, "promulgated_on": promu, "effective_on": eff,
                "official_url": url, "status": "active",
                "verification_status": "source-linked",
                "as_of_date": now_kst_iso()[:10], "updated_at": now_kst_iso(),
            })
        ensure_kind(conn, kinds, source_type=("statute" if target == "law" else "admin-rule"),
                    tier=(1 if target == "law" else 4))
        for attempt in range(8):
            try:
                with D.tx(conn):
                    if payload:
                        D.upsert_many(conn, "legal_instrument", payload, ("instrument_id",))
                break
            except Exception as e:  # noqa: BLE001
                if "lock" not in str(e).lower():
                    print("  저장 오류:", str(e)[:90], flush=True); break
                time.sleep(15)
        got += len(payload)
        if page % 25 == 0 or page == pages:
            print(f"  {target} {page}/{pages} 누적 {got:,} | {time.time()-t0:.0f}s", flush=True)
    return got


def main():
    cfg = load_config(); http = HttpClient(); conn = D.connect()
    n1 = collect(http, cfg, conn, target="law")
    n2 = collect(http, cfg, conn, target="admrul")
    print(f"[finish] 법령 {n1:,} + 행정규칙 {n2:,} 적재", flush=True)


if __name__ == "__main__":
    main()
