"""공개 전 살균 — 데이터에 스며든 API 키를 제거한다.

국가법령정보 API 가 돌려주는 상세링크에는 요청자의 OC 키가 그대로 들어있다.
그걸 official_url 에 저장했기 때문에 DB·정적번들 전체에 실키가 평문으로 퍼졌다.
(regions/*.json 243개, changes feed 1,087회, ordinances 199,858건)

이 스크립트는 DB의 URL 컬럼에서 OC 파라미터를 제거한다. 링크는 OC 없이도
law.go.kr 화면에서 열리며(공개 열람), 우리 수집기는 config 의 키로 별도 호출한다.
"""
import re, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from policymap.config import load_config
from policymap import db as D

TARGETS = [
    ("ordinances", "official_url"),
    ("legal_instrument", "official_url"),
    ("articles", "official_url"),
    ("ordinance_articles", "official_url"),
    ("verification", "official_url"),
    # change_log 도 official_url 을 보관한다(정적번들 regions/*.json 의 recent_changes,
    # changes/feed-*.json 이 여기서 나온다). 누락하면 재export 후에도 키가 남는다. [실측]
    ("change_log", "official_url"),
]


def build_pattern(keys):
    """OC=<키> 뿐 아니라 serviceKey/KEY 형태도 함께 제거."""
    esc = [re.escape(k) for k in keys if k]
    if not esc:
        return None
    joined = "|".join(esc)
    return re.compile(r"([?&])(OC|serviceKey|ServiceKey|KEY|Key)=(" + joined + r")(&|$)")


def sanitize(url, pat):
    if not url or "=" not in url:
        return url
    out = pat.sub(lambda m: m.group(1) if m.group(4) == "" else m.group(1), url)
    out = re.sub(r"[?&]$", "", out)
    out = out.replace("?&", "?").replace("&&", "&")
    return out


def main() -> int:
    cfg = load_config()
    keys = [getattr(cfg, k, None) for k in
            ("law_oc", "assembly_key", "vworld_key", "lofin_key", "stanregin_key")]
    keys = [k for k in keys if k and len(str(k)) >= 4]
    pat = build_pattern(keys)
    if not pat:
        print("[skip] 설정된 키 없음")
        return 0
    conn = D.connect()
    total = 0
    for table, col in TARGETS:
        try:
            rows = conn.execute(
                f"SELECT rowid, {col} FROM {table} WHERE {col} LIKE '%=%'").fetchall()
        except sqlite3.OperationalError:
            print(f"  {table}.{col}: (없음)")
            continue
        upd = []
        for rid, url in rows:
            new = sanitize(url, pat)
            if new != url:
                upd.append((new, rid))
        for i in range(0, len(upd), 5000):
            grp = upd[i:i + 5000]
            for a in range(8):
                try:
                    with D.tx(conn):
                        conn.executemany(
                            f"UPDATE {table} SET {col}=? WHERE rowid=?", grp)
                    break
                except sqlite3.OperationalError as e:
                    if "lock" not in str(e).lower():
                        raise
                    time.sleep(10)
        total += len(upd)
        print(f"  {table}.{col}: {len(upd):,}건 살균 (전체 {len(rows):,})", flush=True)

    # 잔존 검사
    left = 0
    for table, col in TARGETS:
        try:
            for k in keys:
                left += conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?", (f"%{k}%",)).fetchone()[0]
        except sqlite3.OperationalError:
            pass
    print(f"[finish] 총 {total:,}건 살균 / DB 잔존 키 {left}건")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
