"""완전판 작업 진행 상황 — 1시간 주기 보고용.

무엇이 끝났고 무엇이 남았는지를 api/ 산출물과 코드 존재 여부로 판정한다.
워크플로 내부 상태를 못 보므로 '결과물'로 역추적하는 방식이다.
"""
import os, subprocess, time
from pathlib import Path

ROOT = Path(__file__).parent.parent
API = ROOT / "system" / "data" / "api"
SYS = ROOT / "system"
VIZ = ROOT / "viz"

# (라벨, 판정 경로, 기대치, 단위)
TARGETS = [
    ("A1 조례 전량",      API / "ordinance",           243, "지역번들"),
    ("A2 법령 전량",      API / "statute" / "all",      20, "버킷"),
    ("A2 위임 전량",      API / "delegation",          243, "지역번들"),
    ("A3 표결 전량",      API / "votes",               200, "의안"),
    ("A3 의안 메타",      API / "bill",                 10, "버킷"),
    ("A3 신경망 지역",    API / "neural" / "by-region", 243, "지역번들"),
    ("A4 그래프 지역",    API / "graph" / "by-region",  243, "지역번들"),
]
SCRIPTS = [
    ("A1 생성기", SYS / "make_full_ordinance.py"),
    ("A2 생성기", SYS / "make_full_statute.py"),
    ("A3 생성기", SYS / "make_full_vote_neural.py"),
    ("A4 생성기", SYS / "make_full_graph.py"),
    ("C  완전판서버", VIZ / "serve_full.py"),
]


def dsize(p: Path):
    if not p.exists():
        return 0, 0
    # api/ 는 gzip 전환됨(.json.gz). 둘 다 세야 진행률이 맞는다. [실측: .json 만 세면 4,953개를 놓침]
    fs = list(p.rglob("*.json")) + list(p.rglob("*.json.gz"))
    return len(fs), sum(f.stat().st_size for f in fs)


def main():
    print(f"[PROGRESS {time.strftime('%m-%d %H:%M')}]", flush=True)

    done = 0
    print("  단계별:", flush=True)
    for label, path in SCRIPTS:
        ok = path.exists()
        done += ok
        print(f"    {'OK ' if ok else '.. '} {label:14s} {path.name}", flush=True)

    print("  산출물:", flush=True)
    for label, path, expect, unit in TARGETS:
        n, sz = dsize(path)
        pct = min(100, n * 100 // expect) if expect else 0
        bar = "#" * (pct // 10) + "." * (10 - pct // 10)
        print(f"    {label:16s} [{bar}] {n:>5}/{expect} {unit} {sz/1024/1024:>6.1f}MB", flush=True)

    tn, tsz = dsize(API)
    print(f"  api/ 합계: {tn:,}파일 {tsz/1024/1024:.1f}MB (상한 250MB)", flush=True)

    # 원격 커밋
    try:
        h = subprocess.run(["git", "log", "--oneline", "-1"], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=20).stdout.strip()
        print(f"  최근 커밋: {h[:70]}", flush=True)
    except Exception:
        pass

    total = len(SCRIPTS)
    print(f"  진행: 생성기 {done}/{total} · " +
          ("완료" if done == total and tn > 3000 else "진행 중"), flush=True)


if __name__ == "__main__":
    main()
