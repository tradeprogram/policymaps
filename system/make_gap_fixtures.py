"""실 DB → 시각화용 사전계산 fixture 생성.

격차분석·유사지자체 화면은 정적 JSON 만으로 완결되지 않는다(전국 조합을 브라우저에서
계산할 수 없다). 그래서 대표 지자체 몇 곳의 결과를 미리 계산해 파일로 떨군다.

실제 엔진은 MCP tool 이름(gap_analysis)이 아니라
analytics.peers.recommend_ordinances / find_similar_governments 다.

출력: <out>/api/gap.json, <out>/api/peers.json  (기본 out = system/data)
사용: python make_gap_fixtures.py [--out DIR] [--regions 47190,11110,...]
"""
import argparse, json, sys, time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from policymap import db as D
from policymap.analytics import peers as P

# 기본 대상: 완료판정 시나리오(구미시) + 성격이 다른 지자체 몇 곳
DEFAULT = ["47190", "11110", "41135", "26170", "48170"]


def envelope(data, **extra):
    """MCP 서버와 동일한 응답 봉투(프론트가 두 경로를 같은 코드로 다룬다)."""
    out = {
        "data": data,
        "as_of_date": time.strftime("%Y-%m-%d"),
        "stale": False,
        "execution_allowed": False,
        "disclaimer": "조례↔예산 연결은 확률적 매칭이다. verified=1 이 아닌 항목은 추정이며, "
                      "폐지 조례는 선례로 추천되지 않는다. 인용 전 원문을 확인할 것.",
    }
    out.update(extra)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data"))
    ap.add_argument("--regions", default=",".join(DEFAULT))
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--limit", type=int, default=25)
    a = ap.parse_args()

    out = Path(a.out) / "api"
    out.mkdir(parents=True, exist_ok=True)
    conn = D.connect()
    sigs = [s.strip() for s in a.regions.split(",") if s.strip()]

    gaps, peers = {}, {}
    for sig in sigs:
        t0 = time.time()
        try:
            rec = P.recommend_ordinances(conn, sig, k=a.k, min_peers=3, limit=a.limit)
            sim = P.find_similar_governments(conn, sig, k=a.k, policy_profile_weight=0.35)
        except Exception as e:  # noqa: BLE001
            print(f"  {sig}: 실패 {type(e).__name__}: {str(e)[:80]}", flush=True)
            continue
        gaps[sig] = rec
        peers[sig] = sim
        n_rec = len(rec.get("recommendations", []))
        n_warn = sum(1 for r in rec.get("recommendations", []) if r.get("repealed_peer_count"))
        name = (rec.get("target") or {}).get("name") or sig
        print(f"  {sig} {name}: 추천 {n_rec}건(폐지경고 {n_warn}) / "
              f"peer {len(sim.get('peers', []))}곳 / {time.time()-t0:.1f}s", flush=True)

    (out / "gap.json").write_text(
        json.dumps(envelope(gaps, regions=sigs), ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "peers.json").write_text(
        json.dumps(envelope(peers, regions=sigs), ensure_ascii=False, indent=1), encoding="utf-8")
    for f in ("gap.json", "peers.json"):
        print(f"[out] {out/f}  {(out/f).stat().st_size/1024:.0f} KB", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
