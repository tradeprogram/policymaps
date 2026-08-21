"""수집된 조례 본문에서 낫표 인용 → 위임관계(delegations) / 인용관계(CITES) 백필.

본문 전수 수집이 진행되는 동안/이후 반복 실행하면 새로 들어온 조례만 처리한다.
- 재개 가능: watermark(source='deleg_backfill') 의 ordinance_id 커서
- 멱등: delegation_id/rel_id 가 stable_id 라 재실행해도 중복 없음
- 락 내성: 배치 단위 짧은 트랜잭션 + OperationalError 재시도
"""
import re, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from policymap import db as D
from policymap.parsers import delegation as DG

BATCH = int(sys.argv[1]) if len(sys.argv) > 1 else 300
MAX_BATCHES = int(sys.argv[2]) if len(sys.argv) > 2 else 10**9
SOURCE, SCOPE = "deleg_backfill", "ordinance-bodies"

# 인용 직후에 오면 '위임'으로 승격하는 유발문구(그 외는 단순 인용으로만 보존)
TRIGGER = re.compile(r"^\s{0,3}[^가-힣]{0,4}(에\s*따라|에\s*따른|에\s*의하여|에\s*의한|의\s*위임|위임|에\s*근거)")


_NAME_INDEX: dict = {}


def _norm_law_name(x: str) -> str:
    """법령명 정규화 — 조례 본문의 인용 표기가 공식 명칭과 흔들리는 것을 흡수한다.
    공백 제거 + 중점 계열 문자(ㆍ・.‧) 통일. [실측] 이 두 규칙만으로 미해석의 63.2% 가 해소된다."""
    x = re.sub(r"\s+", "", x or "")
    for ch in ("ㆍ", "・", ".", "‧"):
        x = x.replace(ch, "·")
    return x


def _build_index(conn) -> dict:
    idx: dict = {}
    for nm, iid in conn.execute("SELECT name, instrument_id FROM legal_instrument"):
        idx.setdefault(_norm_law_name(nm), iid)
    return idx


def resolve_parent(conn, law_name: str, cache: dict) -> str:
    """법령명 → legal_instrument.instrument_id. 미해석은 명목키 lawname:{name}."""
    if law_name in cache:
        return cache[law_name]
    global _NAME_INDEX
    if not _NAME_INDEX:
        _NAME_INDEX = _build_index(conn)
        print(f"  [index] legal_instrument {len(_NAME_INDEX):,}건 색인", flush=True)
    pid = _NAME_INDEX.get(_norm_law_name(law_name)) or f"lawname:{law_name}"
    cache[law_name] = pid
    return pid


def with_retry(fn, *, tries=8, wait=20):
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            print(f"  [lock] {wait}s 대기 후 재시도 ({i+1}/{tries})", flush=True)
            time.sleep(wait)
    raise RuntimeError("락 재시도 초과")


def main() -> int:
    conn = D.connect()
    cache: dict = {}
    wm = D.get_watermark(conn, SOURCE, SCOPE) or {}
    cursor = (wm.get("cursor") or "") if isinstance(wm, dict) else ""
    started = time.time()
    tot_d = tot_c = tot_o = 0
    print(f"[start] 커서='{cursor}' batch={BATCH}", flush=True)

    for b in range(MAX_BATCHES):
        rows = conn.execute(
            "SELECT ordinance_id FROM ordinances WHERE article_count>0 AND ordinance_id>? "
            "ORDER BY ordinance_id LIMIT ?", (cursor, BATCH)
        ).fetchall()
        if not rows:
            print("[done] 처리할 조례 없음", flush=True)
            break
        oids = [r[0] for r in rows]
        d_rows, c_rows = [], []
        for oid in oids:
            for a in conn.execute(
                "SELECT article_no, body FROM ordinance_articles WHERE ordinance_id=?", (oid,)
            ).fetchall():
                art_no, body = a[0], a[1] or ""
                if "「" not in body:
                    continue
                cites = DG.parse_citations(body)
                if not cites:
                    continue
                c_rows.extend(DG.build_citation_rows("ordinance", oid, cites, src_article=art_no))
                for ct in cites:
                    raw = ct.get("raw") or ""
                    idx = body.find(raw)
                    tail = body[idx + len(raw): idx + len(raw) + 14] if idx >= 0 else ""
                    if not TRIGGER.search(tail):
                        continue
                    d_rows.append(DG._deleg_row(
                        child_id=oid,
                        parent_id=resolve_parent(conn, ct.get("law_name") or "", cache),
                        source_path="citation-backfill",
                        child_article=art_no,
                        parent_article=ct.get("article"),
                        trigger_text=tail.strip()[:40],
                        citation_text=raw[:200],
                        inferred=1,
                    ))
        cursor = oids[-1]

        def commit():
            with D.tx(conn):
                if d_rows:
                    DG.save_delegations(conn, d_rows)
                if c_rows:
                    DG.save_citations(conn, c_rows)
                D.set_watermark(conn, SOURCE, SCOPE, cursor=cursor, status="ok")
        with_retry(commit)

        tot_d += len(d_rows); tot_c += len(c_rows); tot_o += len(oids)
        print(f"[batch {b+1}] 조례 {len(oids)} → 위임 {len(d_rows)} 인용 {len(c_rows)} "
              f"| 누적 조례 {tot_o:,} 위임 {tot_d:,} 인용 {tot_c:,} | {time.time()-started:.0f}s", flush=True)

    print(f"[finish] 조례 {tot_o:,} 처리 / 위임 {tot_d:,} / 인용 {tot_c:,} / {(time.time()-started)/60:.1f}분", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
