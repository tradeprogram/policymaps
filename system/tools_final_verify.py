"""후속 파이프라인 완료를 감지해 최종 검증을 수행하고 리포트를 남긴다.

1) 인용 검증 재실행 (위임 백필 재실행으로 상위법 해석률이 올랐으므로)
2) 전체 테스트
3) 최종 수치 집계 → data/final_report.json + 콘솔 출력
"""
import json, os, sqlite3, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
PYE = sys.executable
POLL = 120


def state() -> str:
    try:
        return json.loads((ROOT / "data" / "finalize_status.json").read_text(encoding="utf-8")).get("state", "?")
    except Exception:
        return "?"


def run(name, args, timeout=7200):
    print(f"\n[{name}] {' '.join(args)}", flush=True)
    try:
        p = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = "\n".join((p.stdout or "").strip().splitlines()[-14:])
        print(out, flush=True)
        return {"ok": p.returncode == 0, "tail": out}
    except Exception as e:  # noqa: BLE001
        print(f"[{name}] 실패 {type(e).__name__}", flush=True)
        return {"ok": False, "tail": str(e)[:200]}


def main() -> int:
    print(f"[wait] 파이프라인 완료 대기 (현재 {state()})", flush=True)
    while state() != "done":
        time.sleep(POLL)
    print("[wait] 파이프라인 완료 감지", flush=True)
    time.sleep(30)

    rep = {"finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    rep["citations"] = run("citations", [PYE, "tools_verify_citations.py"])
    rep["tests"] = run("pytest", [PYE, "-m", "pytest", "tests/", "-q"], timeout=1800)

    c = sqlite3.connect(f"file:{ROOT/'data'/'policymap.db'}?mode=ro", uri=True, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")

    def q(sql, d=None):
        try:
            return c.execute(sql).fetchone()[0]
        except Exception:
            return d

    rep["counts"] = {
        "자치법규": q("SELECT COUNT(*) FROM ordinances"),
        "현행": q("SELECT COUNT(*) FROM ordinances WHERE status IS NULL OR status<>'repealed'"),
        "폐지": q("SELECT COUNT(*) FROM ordinances WHERE status='repealed'"),
        "본문확보": q("SELECT COUNT(*) FROM ordinances WHERE article_count>0"),
        "자치법규조문": q("SELECT COUNT(*) FROM ordinance_articles"),
        "법령": q("SELECT COUNT(*) FROM legal_instrument"),
        "법령조문": q("SELECT COUNT(*) FROM articles"),
        "위임관계": q("SELECT COUNT(*) FROM delegations"),
        "위임_해석됨": q("SELECT COUNT(*) FROM delegations WHERE parent_id NOT LIKE 'lawname:%'"),
        "인용관계": q("SELECT COUNT(*) FROM instrument_relations"),
        "카테고리조례": q("SELECT COUNT(DISTINCT ordinance_id) FROM ordinance_category"),
        "예산": q("SELECT COUNT(*) FROM budget_lines"),
        "지역": q("SELECT COUNT(*) FROM regions"),
        "국회표결": q("SELECT COUNT(*) FROM votes"),
        "노드임베딩": q("SELECT COUNT(*) FROM node_embeddings"),
    }
    rep["db_gb"] = round(os.path.getsize(ROOT / "data" / "policymap.db") / 1024**3, 2)
    (ROOT / "data" / "final_report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 60, flush=True)
    print("[FINAL] 최종 집계", flush=True)
    print("=" * 60, flush=True)
    for k, v in rep["counts"].items():
        print(f"  {k:14s} {v:>12,}" if isinstance(v, int) else f"  {k:14s} {v}", flush=True)
    print(f"  {'DB':14s} {rep['db_gb']:>12} GB", flush=True)
    print(f"  검증: citations={rep['citations']['ok']} tests={rep['tests']['ok']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
