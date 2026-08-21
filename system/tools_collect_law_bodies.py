"""위임관계가 실제로 참조하는 상위법의 조문 본문 수집.

29,811개 법령 전량이 아니라, delegations.parent_id 가 가리키는 법령을 참조 빈도
순으로 수집한다. "이 조례의 근거는 「영유아보육법」 제12조이고 그 조문은 …"까지
보여주려면 상위법 조문이 필요하다.

수집기와 동시 실행되므로 QPS 를 낮게 잡는다(기본 2.0).
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from policymap.config import load_config
from policymap.util import HttpClient
from policymap import db as D
from policymap.collectors import law as L
from policymap.parsers import article as A

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
QPS = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0


def _norm(x: str) -> str:
    import re
    x = re.sub(r"\s+", "", x or "")
    for ch in ("ㆍ", "・", ".", "‧"):
        x = x.replace(ch, "·")
    return x


def targets(conn, limit: int):
    """위임 참조가 많은 순으로, 아직 조문이 없는 법령.

    delegations 의 59% 는 아직 'lawname:{명}' 명목키다(해석 개선은 백필 재실행 때 반영).
    그래서 실 instrument_id 조인만으로는 대상이 65건밖에 안 잡힌다. 명목키도 정규화
    매칭해서 '재실행 후 참조될 법령'을 미리 대상에 넣는다. [실측]
    """
    refs: dict = {}
    for pid, n in conn.execute("SELECT parent_id, COUNT(*) FROM delegations GROUP BY parent_id"):
        refs[pid] = refs.get(pid, 0) + n
    # [버그 수정] "조문이 하나라도 있으면 건너뛰기"는 틀렸다. korea100 시드는 인용된
    # 조문만 캐시한 **부분 데이터**라(예: 주민등록법 1개, 소득세법이 104조부터),
    # 그대로 두면 인용 검증이 거짓 실패한다. 전문 수집 이력이 있는 법령만 제외한다. [실측]
    have_art = {r[0] for r in conn.execute(
        "SELECT instrument_id FROM articles GROUP BY instrument_id HAVING COUNT(*) >= 5 "
        "AND MIN(CAST(article_no AS INTEGER)) <= 3")}
    by_norm: dict = {}
    for iid, mst, nm in conn.execute(
            "SELECT instrument_id, mst, name FROM legal_instrument "
            "WHERE mst IS NOT NULL AND mst <> '' AND source_type='statute'"):
        by_norm.setdefault(_norm(nm), (iid, mst, nm))
    scored: dict = {}
    for pid, n in refs.items():
        if pid.startswith("lawname:"):
            hit = by_norm.get(_norm(pid[len("lawname:"):]))
        else:
            hit = next((v for v in (by_norm.get(_norm(r[0])) for r in
                        conn.execute("SELECT name FROM legal_instrument WHERE instrument_id=?", (pid,)))
                        if v), None)
        if not hit or hit[0] in have_art:
            continue
        cur = scored.get(hit[0])
        scored[hit[0]] = (hit[0], hit[1], hit[2], (cur[3] if cur else 0) + n)
    out = sorted(scored.values(), key=lambda t: -t[3])
    return out[:limit]


def main() -> int:
    cfg = load_config(); http = HttpClient(); conn = D.connect()
    rows = targets(conn, LIMIT)
    print(f"[start] 조문 미보유 상위법 {len(rows):,}건 (참조 많은 순), qps={QPS}", flush=True)
    ok = fail = arts = 0
    t0 = time.time(); interval = 1.0 / QPS
    for i, (iid, mst, name, refs) in enumerate(rows, 1):
        t = time.time()
        try:
            body = L.law_body(http, cfg, mst)
            parsed = A.parse_law_articles(body, iid)
            if parsed:
                for a in range(6):
                    try:
                        A.save_articles(conn, parsed, table="articles")
                        break
                    except Exception as e:  # noqa: BLE001
                        if "lock" not in str(e).lower():
                            raise
                        time.sleep(15)
                arts += len(parsed); ok += 1
            else:
                fail += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            if fail <= 3:
                print(f"  실패 {name[:24]}: {type(e).__name__} {str(e)[:60]}", flush=True)
        if i % 200 == 0:
            print(f"  {i}/{len(rows)} ok={ok} fail={fail} 조문 {arts:,} | {time.time()-t0:.0f}s", flush=True)
        dt = interval - (time.time() - t)
        if dt > 0:
            time.sleep(dt)
    print(f"[finish] 법령 {ok:,}건 / 조문 {arts:,}개 / 실패 {fail} / {(time.time()-t0)/60:.1f}분", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
