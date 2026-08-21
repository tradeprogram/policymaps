"""위임관계의 상위법 조문 인용을 실제 조문 존재 여부로 검증한다.

korea100 의 검증 규율(조문 참조를 원문과 대조해 verified/missing 으로 표기)을
우리 delegations 에 적용한다. "지어내지 않기 / 부재의 확인도 검증" 원칙.

delegations.verification_status 를 다음으로 갱신:
  article-verified : 상위법이 해석됐고 인용 조문이 실제로 존재
  article-missing  : 상위법은 해석됐으나 그 조문이 없음(오인용 또는 개정으로 삭제)
  unverifiable     : 상위법 미해석(lawname:) 또는 조문번호 없음 → 검증 불가
"""
import re, sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from policymap import db as D

ART_RE = re.compile(r"제?\s*(\d+)\s*조(?:\s*의\s*(\d+))?")


def norm_article(x: str):
    """'제36조' → '36' · '제36조의2' → '36-2' · 실패 시 None."""
    if not x:
        return None
    m = ART_RE.search(str(x))
    if not m:
        return None
    return f"{int(m.group(1))}-{int(m.group(2))}" if m.group(2) else str(int(m.group(1)))


def article_index(conn) -> dict:
    """instrument_id → {정규화 조문번호}"""
    idx: dict = {}
    for iid, no in conn.execute("SELECT instrument_id, article_no FROM articles"):
        n = norm_article(no) or (str(int(no)) if str(no).isdigit() else None)
        if n:
            idx.setdefault(iid, set()).add(n)
    return idx


def main() -> int:
    conn = D.connect()
    t0 = time.time()
    idx = article_index(conn)
    print(f"[index] 조문 보유 법령 {len(idx):,}건", flush=True)

    rows = conn.execute(
        "SELECT delegation_id, parent_id, parent_article FROM delegations").fetchall()
    stats = {"article-verified": 0, "article-missing": 0, "unverifiable": 0}
    updates = []
    for did, pid, part in rows:
        if not pid or pid.startswith("lawname:"):
            st = "unverifiable"
        else:
            n = norm_article(part)
            if not n:
                st = "unverifiable"
            elif pid not in idx:
                st = "unverifiable"          # 상위법 조문 미수집 — 부재가 아니라 미확인
            else:
                st = "article-verified" if n in idx[pid] else "article-missing"
        stats[st] += 1
        updates.append((st, did))

    for i in range(0, len(updates), 3000):
        grp = updates[i:i + 3000]
        for a in range(8):
            try:
                with D.tx(conn):
                    conn.executemany(
                        "UPDATE delegations SET verification_status=? WHERE delegation_id=?", grp)
                break
            except sqlite3.OperationalError as e:
                if "lock" not in str(e).lower():
                    raise
                time.sleep(15)

    tot = sum(stats.values()) or 1
    checkable = stats["article-verified"] + stats["article-missing"]
    print(f"[finish] 위임 {tot:,}건 / {(time.time()-t0):.0f}s", flush=True)
    for k, v in stats.items():
        print(f"  {k:18s} {v:>7,}  ({v/tot*100:5.1f}%)", flush=True)
    if checkable:
        print(f"  → 검증 가능분 {checkable:,}건 중 실재 확인 "
              f"{stats['article-verified']/checkable*100:.1f}%", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
