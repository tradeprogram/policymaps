"""시스템 헬스체크 — 진행률 + 데이터 품질 + 무결성 + 이상징후를 한 번에 점검.

1시간마다 호출되어 한 블록으로 출력한다(모니터가 단일 알림으로 묶도록 빠르게 출력).
DB 경합을 피해 읽기전용·짧은 타임아웃을 쓰고, 실패한 항목은 '?'로 표시하되 죽지 않는다.
"""
import json, os, re, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).parent
DB = ROOT / "data" / "policymap.db"
ALERTS: list[str] = []


def conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=25)
    c.execute("PRAGMA busy_timeout=25000")
    return c


def q1(c, sql, default=None):
    try:
        return c.execute(sql).fetchone()[0]
    except Exception:
        return default


def fmt(n):
    return f"{n:,}" if isinstance(n, int) else "?"


def jread(p):
    try:
        return json.loads((ROOT / "data" / p).read_text(encoding="utf-8"))
    except Exception:
        return {}


def proc_alive(needle: str) -> bool:
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             f"Where-Object {{ $_.CommandLine -like '*{needle}*' }} | Measure-Object).Count"],
            capture_output=True, text=True, timeout=25)
        return (out.stdout or "0").strip().split()[-1] != "0"
    except Exception:
        return False


def main() -> int:
    t = time.strftime("%m-%d %H:%M")
    c = conn()

    # ── 1. 수집 진행
    body = q1(c, "SELECT COUNT(*) FROM ordinances WHERE article_count>0", 0)
    left = q1(c, "SELECT COUNT(*) FROM ordinances WHERE (article_count IS NULL OR article_count=0) "
                 "AND (status IS NULL OR status<>'repealed')", 0)
    tot = (body or 0) + (left or 0)
    pct = (body / tot * 100) if tot else 0
    st = jread("bulk_bodies_status.json")
    rate = st.get("rate_per_sec")
    eta = st.get("eta_hours")
    col_alive = proc_alive("tools_bulk_bodies")

    # ── 2. 파이프라인
    fs = jread("finalize_status.json")
    pstate = fs.get("state", "?")
    steps = [k for k in ("delegations", "categories", "rag_index", "graph_build", "export") if k in fs]
    step_txt = " ".join(f"{k}={'OK' if fs[k].get('ok') else 'FAIL'}" for k in steps) or "대기"
    pipe_alive = proc_alive("finalize_pipeline")

    # ── 3. 데이터 규모
    counts = {
        "자치법규": q1(c, "SELECT COUNT(*) FROM ordinances"),
        "조문": q1(c, "SELECT MAX(rowid) FROM ordinance_articles"),
        "법령": q1(c, "SELECT COUNT(*) FROM legal_instrument"),
        "위임": q1(c, "SELECT COUNT(*) FROM delegations"),
        "카테고리": q1(c, "SELECT COUNT(DISTINCT ordinance_id) FROM ordinance_category"),
        "예산": q1(c, "SELECT COUNT(*) FROM budget_lines"),
    }

    # ── 4. 품질·무결성 점검
    # 수집기가 계속 쓰는 중이라 전체 스캔은 타임아웃된다. 전부 유계(LIMIT) 표본 쿼리로 본다.
    checks = []
    SAMPLE = 20000
    empty_art = q1(c, f"SELECT COUNT(*) FROM (SELECT body FROM ordinance_articles LIMIT {SAMPLE}) "
                      "WHERE body IS NULL OR TRIM(body)=''")
    checks.append((f"빈조문/{SAMPLE//1000}k", empty_art, (empty_art or 0) < 50))
    # [주의] LIMIT 은 삽입순서 표본이라 편향된다(임베딩 진단에서 같은 함정을 겪었다).
    # ordinances(20만)·delegations(5만)는 작아서 정확히 센다. 1.5M 조문만 표본.
    # [개선] 단순 '조문1개' 카운트는 오탐이다 — 폐지·개정 조례는 조문 1개가 정상 형식이다
    # (예: "제1조 OO조례를 폐지한다"). 실측 523건 중 파싱 실패는 0건이었다.
    # 진짜 실패 모드는 '조례 전문이 조문 1개에 통째로 들어간 것' 이므로 본문 길이로 잡는다.
    lump = q1(c, "SELECT COUNT(*) FROM ordinances o JOIN ordinance_articles a "
                 "ON a.ordinance_id=o.ordinance_id "
                 "WHERE o.article_count=1 AND LENGTH(a.body)>8000")
    checks.append(("통짜파싱의심", lump, (lump or 0) < 30))
    no_region = q1(c, "SELECT COUNT(*) FROM ordinances WHERE region_id IS NULL "
                      "AND (status IS NULL OR status<>'repealed')")
    checks.append(("지역미매칭(현행)", no_region, (no_region or 0) < 6000))
    resolved = q1(c, "SELECT COUNT(*) FROM delegations WHERE parent_id NOT LIKE 'lawname:%'")
    dtot = q1(c, "SELECT COUNT(*) FROM delegations") or 1
    rr = (resolved or 0) / dtot * 100
    checks.append(("상위법해석률%", round(rr, 1), rr >= 35))

    # ── 5. 수집 오류율
    err_recent = 0
    try:
        log = (ROOT / "data" / "bulk_bodies.log").read_text(encoding="utf-8", errors="replace")
        chunks = re.findall(r"err=(\d+)", log)[-10:]
        err_recent = sum(int(x) for x in chunks)
    except Exception:
        pass
    checks.append(("최근10청크 오류", err_recent, err_recent == 0))

    for name, val, ok in checks:
        if not ok:
            ALERTS.append(f"{name}={val}")
    if not col_alive and (left or 0) > 0 and pstate == "waiting":
        ALERTS.append("수집기 중단(재시작 필요)")
    if not pipe_alive and pstate not in ("done",):
        ALERTS.append("파이프라인 중단")

    dbsz = os.path.getsize(DB) / 1024**3 if DB.exists() else 0
    head = "[HEALTH %s] 본문 %s/%s (%.1f%%)" % (t, fmt(body), fmt(tot), pct)
    if rate:
        head += f" | {rate}건/s ETA {eta}h"
    head += f" | 수집기 {'●' if col_alive else '×'} 파이프 {'●' if pipe_alive else '×'}({pstate})"
    print(head, flush=True)
    print("  규모: " + " · ".join(f"{k} {fmt(v)}" for k, v in counts.items()) + f" · DB {dbsz:.2f}GB", flush=True)
    print("  품질: " + " · ".join(f"{n}={v}{'' if ok else ' ⚠'}" for n, v, ok in checks), flush=True)
    print("  단계: " + step_txt, flush=True)
    if ALERTS:
        print("  ⚠ ALERT: " + " / ".join(ALERTS), flush=True)
    else:
        print("  ✔ 이상 없음", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
