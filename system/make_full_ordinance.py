#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""실 DB → **자치법규 199,858건 전량** 배포용 shard 생성.

기존 생성기들은 화면별 사전계산 결과(격차·확산·생애주기 집계 등)만 구웠다. 조례
'목록' 자체는 어디에도 없어서 배포본에서 전수 열람이 불가능했다. 이 스크립트가
그 구멍을 메운다 — **한 건도 빠뜨리지 않고** 지역별 번들로 굽는다.

  api/ordinance/index.json              지역별 건수·파일 경로·커버리지 색인
  api/ordinance/{sig_cd}.json           지역별 조례 전량 메타 (247파일)
  api/ordinance/unassigned.json         sig_cd 미매칭 조례 (시·도 교육청 18곳 + 충청광역연합)
  api/ordinance/articles/{sig_cd}.json  조례별 **조문번호+제목** 목록 (본문 제외)
  api/ordinance/articles/unassigned.json

전량 보장(실측 2026-08-23):
  ordinances 199,858건 = level1 27,409 + level2 166,718 + level4(교육청 등) 5,731.
  sig_cd 를 가진 지역 247곳(선택기 243곳 + 이력지역 4곳: 29000 광주광역시(merged),
  46000 전라남도(merged), 48110 창원시(abolished), 43710 청원군(abolished))과
  sig_cd 가 없는 level4 19곳으로 나뉜다. 어느 쪽도 버리지 않는다.
  index.json 의 totals.ordinances 가 199,858 과 일치하는지 스스로 검증한다.

■ 왜 조문 '본문'이 없나
  ordinance_articles 2,365,068행의 body 합계는 약 490MB 다. GitHub·Vercel 정적
  배포로 감당할 수 없어 배포본에는 **조문번호와 제목까지만** 담는다. 본문은
  로컬 완전판(DB 직결 API, viz/serve_ai.py)에서만 제공한다. 각 articles 번들의
  data.body_excluded 에 같은 사유를 박아 둬서 파일만 봐도 알 수 있게 했다.

■ 저장 포맷 (--format columnar, 기본값)
  객체 배열로 쓰면 키 이름이 199,858번 반복돼 86MB 가 된다(실측). 그래서 기본은
  **열지향(columnar)** 이다. 손실은 없고 디코딩은 5줄이면 된다.

    data.format  = "columnar-v1"
    data.columns = ["ordinance_id","mst","name","ord_kind", ...]   # 열 이름 순서
    data.rows    = [[...], [...]]                                   # 값 배열
    data.enums   = {"ord_kind": ["조례","규칙"], ...}                # 저카디널리티 열은 정수 코드
    data.derived = {"ordinance_id": "'ordin:' + mst",
                    "official_url": "…MST={mst}…"}                   # null 이면 이 규칙으로 복원

  디코딩(JS):
    const {columns, rows, enums, derived} = payload.data;
    const items = rows.map(r => Object.fromEntries(columns.map((c,i) =>
        [c, enums[c] ? (r[i]==null?null:enums[c][r[i]]) : r[i]])));
    items.forEach(o => { o.ordinance_id ??= "ordin:"+o.mst;
                         o.official_url ??= derived.official_url.replace("{mst}", o.mst); });

  `--format objects` 를 주면 사람이 읽기 쉬운 객체 배열로 굽는다(대신 3배 커진다).

■ official_url 정규화
  DB 의 40,406건(폐지본)은 official_url 이 "/DRF/lawService.do?…" 상대경로다.
  같은 호스트(law.go.kr)를 가리키므로 절대 URL 로 정규화해 담는다. 정규화 건수는
  index.json 의 totals.url_normalized 에 기록한다.

■ 규율
  - 키 살균: make_more_fixtures._sanitize_keys 를 그대로 쓴다(중복 구현 금지).
    RAG 인덱스가 살균 전 URL 을 갖고 있어 출력단 살균이 필수다.
  - 봉투: make_gap_fixtures.envelope (as_of_date·disclaimer 동일).
  - 표기: 폐지 조례도 담되 status/repealed_on 으로 구분하고, verification_status 를
    함께 실어 검증상태를 화면에서 배지로 띄울 수 있게 한다.
  - 재개 가능: 이미 만들어진 번들은 건너뛴다(--force 로 재생성). 원자적 쓰기.

사용:
  cd system
  python make_full_ordinance.py --limit 3          # 소규모 시험 + 용량 실측
  python make_full_ordinance.py                    # 전량 247지역 + 미매칭
  python make_full_ordinance.py --skip-articles    # 메타만 (조문 제목 번들 생략)
  python make_full_ordinance.py --format objects --force
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:  # 콘솔이 cp949 여도 한글 지자체명 출력에서 죽지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from policymap import db as D                       # noqa: E402
from policymap.config import get_config             # noqa: E402

# 중복 구현 금지 — 봉투와 키 살균은 기존 생성기 것을 그대로 쓴다.
from make_gap_fixtures import envelope              # noqa: E402
from make_more_fixtures import _sanitize_keys       # noqa: E402

LAW_HOST = "https://www.law.go.kr"
URL_TEMPLATE = LAW_HOST + "/DRF/lawService.do?target=ordin&MST={mst}&type=HTML&mobileYn="

# 열 순서(columnar 포맷의 계약). 바꾸면 소비자도 같이 바뀌어야 하므로 뒤에만 추가할 것.
COLUMNS = [
    "mst",                    # 법령ID(MST). ordinance_id = "ordin:" + mst
    "name",
    "ord_kind",               # enum: 조례 / 규칙
    "enacted_on",             # YYYYMMDD (DB 원문 형식 그대로)
    "effective_on",           # YYYYMMDD
    "repealed_on",            # YYYYMMDD, null 이면 미폐지
    "rr_cls_cd",              # enum: 제정/일부개정/전부개정/폐지/타법개정
    "status",                 # enum: active / repealed
    "lifecycle",              # enum: in_force / pending / superseded / undetermined / null
    "article_count",
    "category",               # enum: C01~C14 등 (ordinance_category 최고 confidence 1건)
    "verification_status",    # enum: source-linked / body-missing
    "department",
    "official_url",           # null 이면 derived.official_url 템플릿으로 복원
    "ordinance_id",           # null 이면 "ordin:" + mst 로 복원
]
ENUM_COLUMNS = ("ord_kind", "rr_cls_cd", "status", "lifecycle",
                "category", "verification_status", "department")

UNASSIGNED_KEY = "unassigned"

BODY_EXCLUDED_NOTE = (
    "조문 '본문'은 배포본에 없다. ordinance_articles 2,365,068행의 본문 합계가 약 490MB 라 "
    "GitHub·Vercel 정적 배포 용량을 넘는다. 배포본은 조문번호와 제목까지만 담고, "
    "본문은 로컬 완전판(DB 직결 API, viz/serve_ai.py)에서 제공한다."
)


# --------------------------------------------------------------------------- #
# 유틸
# --------------------------------------------------------------------------- #
def write_json(path: Path, env: dict, indent: int | None = None) -> int:
    """키 살균 후 원자적 쓰기. 반환: 바이트 수.

    make_nationwide.write_json 과 같은 규율(살균 → tmp → os.replace)이되, 이쪽 번들은
    수십 MB 라 indent 를 끌 수 있어야 해서 indent 인자를 받는다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if indent is None:
        body = json.dumps(_sanitize_keys(env), ensure_ascii=False, separators=(",", ":"))
    else:
        body = json.dumps(_sanitize_keys(env), ensure_ascii=False, indent=indent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return path.stat().st_size


def existing(path: Path) -> int | None:
    """이미 만들어진 번들이면 크기, 아니면 None."""
    try:
        n = path.stat().st_size
    except OSError:
        return None
    return n if n > 0 else None


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def norm_url(url: str | None, mst: str | None) -> tuple[str | None, bool]:
    """official_url 정규화. 반환 (url, 정규화했는가)."""
    if not url:
        return (URL_TEMPLATE.format(mst=mst) if mst else None), False
    if url.startswith("/"):
        return LAW_HOST + url, True
    return url, False


def shrink_article_no(no: str | None) -> str | None:
    """'000100' -> '1', '000102' -> '1-2'. DB 는 전부 6자리 고정(실측)."""
    if not no:
        return no
    if len(no) == 6 and no.isdigit():
        main, branch = int(no[:4]), int(no[4:])
        return f"{main}-{branch}" if branch else str(main)
    return no


class Enums:
    """저카디널리티 열을 정수 코드로 바꾼다. 열별 사전을 따로 들고 간다."""

    def __init__(self, columns):
        self.maps = {c: {} for c in columns}

    def code(self, col: str, value):
        if value is None or col not in self.maps:
            return value
        d = self.maps[col]
        if value not in d:
            d[value] = len(d)
        return d[value]

    def dump(self):
        return {c: list(d) for c, d in self.maps.items() if d}


# --------------------------------------------------------------------------- #
# 데이터 적재
# --------------------------------------------------------------------------- #
def load_category_map(conn) -> dict:
    """조례 -> 대표 카테고리 코드(최고 confidence 1건). 실측 187,983건."""
    out: dict[str, str] = {}
    sql = ("SELECT ordinance_id, category_code FROM ordinance_category "
           "ORDER BY ordinance_id, confidence DESC, category_code")
    for oid, code in conn.execute(sql):
        out.setdefault(oid, code)
    return out


def load_targets(conn, levels=None, limit=0, sigs=None):
    """번들 대상 목록. sig_cd 를 가진 지역 + sig_cd 없는 지역(미매칭 묶음)."""
    rows = D.fetchall(conn, """
        SELECT r.sig_cd, r.region_id, r.name, r.full_name, r.level, r.status,
               r.has_legislation, COUNT(*) AS n
          FROM ordinances o JOIN regions r ON r.region_id = o.region_id
         GROUP BY r.region_id
         ORDER BY r.level, r.sig_cd, r.region_id""")
    assigned, unassigned = {}, []
    for r in rows:
        if r["sig_cd"]:
            assigned.setdefault(r["sig_cd"], []).append(r)
        else:
            unassigned.append(r)
    targets = []
    for sig, group in assigned.items():
        head = group[0]
        if levels and int(head["level"]) not in levels:
            continue
        if sigs and sig not in sigs:
            continue
        targets.append({
            "sig_cd": sig, "name": head["name"], "full_name": head["full_name"],
            "level": int(head["level"]), "region_status": head["status"],
            "region_ids": [g["region_id"] for g in group],
            "expected": sum(int(g["n"]) for g in group),
        })
    targets.sort(key=lambda t: (t["level"], t["sig_cd"]))
    if limit:
        targets = targets[:limit]
    return targets, unassigned


ORD_SQL = """
    SELECT o.ordinance_id, o.mst, o.name, o.ord_kind, o.enacted_on, o.effective_on,
           o.repealed_on, o.rr_cls_cd, o.status, o.lifecycle, o.article_count,
           o.official_url, o.verification_status, o.department, o.org_name, o.region_id
      FROM ordinances o
     WHERE o.region_id IN ({ph})
     ORDER BY o.name, o.mst"""

ART_SQL = """
    SELECT a.ordinance_id, a.article_no, a.title
      FROM ordinance_articles a
     WHERE a.ordinance_id IN (SELECT ordinance_id FROM ordinances WHERE region_id IN ({ph}))
     ORDER BY a.ordinance_id, a.article_no"""


def build_meta(conn, region_ids, catmap, fmt, extra_cols=()):
    """조례 메타 번들의 data 블록을 만든다. 반환 (data, stats)."""
    ph = ",".join("?" for _ in region_ids)
    rows = D.fetchall(conn, ORD_SQL.format(ph=ph), region_ids)
    columns = list(COLUMNS) + list(extra_cols)
    enums = Enums(ENUM_COLUMNS)
    counts = {"total": 0, "active": 0, "repealed": 0}
    by_kind, by_rr, by_cat = {}, {}, {}
    url_normalized = 0
    items = []
    for o in rows:
        mst = o["mst"]
        url, normed = norm_url(o["official_url"], mst)
        url_normalized += 1 if normed else 0
        cat = catmap.get(o["ordinance_id"])
        counts["total"] += 1
        st = o["status"] or "unknown"
        counts[st] = counts.get(st, 0) + 1
        by_kind[o["ord_kind"] or "미상"] = by_kind.get(o["ord_kind"] or "미상", 0) + 1
        by_rr[o["rr_cls_cd"] or "미상"] = by_rr.get(o["rr_cls_cd"] or "미상", 0) + 1
        by_cat[cat or "미분류"] = by_cat.get(cat or "미분류", 0) + 1

        vals = {
            "mst": mst,
            "name": o["name"],
            "ord_kind": o["ord_kind"],
            "enacted_on": o["enacted_on"],
            "effective_on": o["effective_on"],
            "repealed_on": o["repealed_on"],
            "rr_cls_cd": o["rr_cls_cd"],
            "status": o["status"],
            "lifecycle": o["lifecycle"],
            "article_count": o["article_count"],
            "category": cat,
            "verification_status": o["verification_status"],
            "department": o["department"],
            # 템플릿과 같으면 null 로 두고 derived 규칙으로 복원한다(용량 절감).
            "official_url": None if url == URL_TEMPLATE.format(mst=mst) else url,
            "ordinance_id": None if o["ordinance_id"] == f"ordin:{mst}" else o["ordinance_id"],
        }
        for c in extra_cols:
            vals[c] = o[c]
        if fmt == "columnar":
            items.append([enums.code(c, vals[c]) for c in columns])
        else:
            obj = {c: vals[c] for c in columns}
            obj["ordinance_id"] = obj["ordinance_id"] or f"ordin:{mst}"
            obj["official_url"] = obj["official_url"] or URL_TEMPLATE.format(mst=mst)
            items.append(obj)

    data = {
        "counts": {**counts, "by_ord_kind": by_kind, "by_rr_cls_cd": by_rr,
                   "by_category": by_cat},
        "format": "columnar-v1" if fmt == "columnar" else "objects-v1",
        "derived": {
            "ordinance_id": "'ordin:' + mst",
            "official_url": URL_TEMPLATE,
            "note": "columnar-v1 에서 두 열이 null 이면 위 규칙으로 복원한다. "
                    "objects-v1 은 이미 복원된 값이 들어 있다.",
        },
        "date_format": "YYYYMMDD (DB 원문 형식)",
        "includes_repealed": True,
        "repealed_note": "폐지 조례도 전량 포함한다. status='repealed' / repealed_on 으로 구분하고, "
                         "선례 추천에는 쓰지 말 것.",
    }
    if fmt == "columnar":
        data["columns"] = columns
        data["enums"] = enums.dump()
        data["rows"] = items
    else:
        data["ordinances"] = items
    stats = {"count": counts["total"], "active": counts.get("active", 0),
             "repealed": counts.get("repealed", 0), "url_normalized": url_normalized}
    return data, stats


def build_articles(conn, region_ids):
    """조문 제목 번들의 data 블록. 본문은 담지 않는다. 반환 (data, stats)."""
    ph = ",".join("?" for _ in region_ids)
    by_ord: dict[str, list] = {}
    n = 0
    untitled = 0
    for oid, no, title in conn.execute(ART_SQL.format(ph=ph), region_ids):
        n += 1
        if not title:
            untitled += 1
        key = oid[6:] if oid.startswith("ordin:") else oid
        by_ord.setdefault(key, []).append([shrink_article_no(no), title or ""])
    data = {
        "format": "articles-titles-v1",
        "key": "mst (ordinance_id 에서 'ordin:' 접두어를 뗀 값)",
        "item": ["article_no", "title"],
        "article_no_format": "'1' = 제1조, '1-2' = 제1조의2 (DB 의 6자리 '000102' 를 줄인 값)",
        "body_excluded": BODY_EXCLUDED_NOTE,
        "articles": by_ord,
    }
    stats = {"articles": n, "ordinances_with_articles": len(by_ord), "untitled": untitled}
    return data, stats


# --------------------------------------------------------------------------- #
# 생성 루프
# --------------------------------------------------------------------------- #
def count_url_relative(conn, region_ids) -> int:
    """재개 시 URL 정규화 건수를 색인에 채우기 위한 실측 카운트(DB 직접)."""
    ph = ",".join("?" for _ in region_ids)
    return D.fetchall(conn, f"SELECT COUNT(*) AS n FROM ordinances "
                            f"WHERE region_id IN ({ph}) AND official_url LIKE '/%'",
                      region_ids)[0]["n"]


def count_articles(conn, region_ids) -> tuple[int, int]:
    """재개 시 조문 수를 색인에 채우기 위한 실측 카운트. 반환 (조문수, 조문보유 조례수)."""
    ph = ",".join("?" for _ in region_ids)
    r = D.fetchall(conn, f"""
        SELECT COUNT(*) AS n, COUNT(DISTINCT a.ordinance_id) AS m
          FROM ordinance_articles a
         WHERE a.ordinance_id IN (SELECT ordinance_id FROM ordinances
                                   WHERE region_id IN ({ph}))""", region_ids)[0]
    return int(r["n"]), int(r["m"])


def emit(conn, out, catmap, target, args, report):
    """지역 1곳(또는 미매칭 묶음) 의 메타·조문제목 번들을 굽는다."""
    key = target["sig_cd"]
    entry = {k: target[k] for k in
             ("sig_cd", "name", "full_name", "level", "region_status", "expected")
             if k in target}
    entry["files"] = {}
    if target.get("members"):          # 미매칭 묶음은 색인만 봐도 구성을 알 수 있게
        entry["members"] = target["members"]
        entry["note"] = target.get("note")
    marks = []

    meta_path = out / f"{key}.json"
    size = None if args.force else existing(meta_path)
    if size is not None:
        try:
            prev = json.loads(meta_path.read_text(encoding="utf-8"))
            c = ((prev.get("data") or {}).get("counts") or {})
            entry["counts"] = {"total": c.get("total"), "active": c.get("active"),
                               "repealed": c.get("repealed")}
            entry["files"]["meta"] = {"path": f"{key}.json", "bytes": size, "reused": True}
            # 재개해도 색인 합계가 맞아야 한다 — DB 에서 다시 센다.
            entry["url_normalized"] = count_url_relative(conn, target["region_ids"])
            marks.append("meta=skip")
        except Exception:  # noqa: BLE001  깨진 파일이면 다시 굽는다
            size = None
    if size is None:
        t0 = time.time()
        try:
            data, stats = build_meta(conn, target["region_ids"], catmap, args.format,
                                     extra_cols=target.get("extra_cols", ()))
            data["region"] = {k: target.get(k) for k in
                              ("sig_cd", "name", "full_name", "level", "region_status")}
            if target.get("members"):
                data["region"]["members"] = target["members"]
                data["region"]["note"] = target.get("note")
            env = envelope(data, sig_cd=key if key != UNASSIGNED_KEY else None,
                           generator="make_full_ordinance.py")
            size = write_json(meta_path, env, indent=args.indent)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {str(e)[:150]}"
            entry.setdefault("errors", {})["meta"] = msg
            report["errors"].append({"kind": "meta", "key": key, "error": msg})
            marks.append("meta=FAIL")
            stats = None
        if stats is not None:
            entry["counts"] = {"total": stats["count"], "active": stats["active"],
                               "repealed": stats["repealed"]}
            entry["files"]["meta"] = {"path": f"{key}.json", "bytes": size,
                                      "seconds": round(time.time() - t0, 2)}
            entry["url_normalized"] = stats["url_normalized"]
            marks.append(f"meta={human(size)}/{stats['count']}건")

    if not args.skip_articles:
        art_path = out / "articles" / f"{key}.json"
        size = None if args.force else existing(art_path)
        if size is not None:
            n_art, n_ord = count_articles(conn, target["region_ids"])
            entry["files"]["articles"] = {"path": f"articles/{key}.json", "bytes": size,
                                          "articles": n_art,
                                          "ordinances_with_articles": n_ord,
                                          "reused": True}
            marks.append("art=skip")
        else:
            t0 = time.time()
            try:
                data, stats = build_articles(conn, target["region_ids"])
                data["region"] = {k: target.get(k) for k in ("sig_cd", "name", "full_name")}
                env = envelope(data, sig_cd=key if key != UNASSIGNED_KEY else None,
                               generator="make_full_ordinance.py")
                size = write_json(art_path, env, indent=args.indent)
                entry["files"]["articles"] = {
                    "path": f"articles/{key}.json", "bytes": size,
                    "articles": stats["articles"],
                    "ordinances_with_articles": stats["ordinances_with_articles"],
                    "seconds": round(time.time() - t0, 2)}
                marks.append(f"art={human(size)}/{stats['articles']}조")
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {str(e)[:150]}"
                entry.setdefault("errors", {})["articles"] = msg
                report["errors"].append({"kind": "articles", "key": key, "error": msg})
                marks.append("art=FAIL")
    return entry, marks


def build_index(conn, out, args, report, entries, unassigned_entry):
    """지역별 건수·파일 경로 색인 + 전량 자기검증."""
    db_total = D.fetchall(conn, "SELECT COUNT(*) AS n FROM ordinances")[0]["n"]
    # 합계는 항상 entry 에서 다시 모은다 — 재개 실행(파일 재사용)에서도 색인이 맞아야 한다.
    all_entries = entries + ([unassigned_entry] if unassigned_entry else [])
    covered = sum((e.get("counts") or {}).get("total") or 0 for e in all_entries)
    bytes_meta = sum(((e.get("files") or {}).get("meta") or {}).get("bytes") or 0 for e in all_entries)
    bytes_art = sum(((e.get("files") or {}).get("articles") or {}).get("bytes") or 0 for e in all_entries)
    n_articles = sum(((e.get("files") or {}).get("articles") or {}).get("articles") or 0
                     for e in all_entries)
    n_urlnorm = sum(e.get("url_normalized") or 0 for e in all_entries)
    data_as_of = D.fetchall(conn, "SELECT MAX(as_of_date) AS d FROM ordinances")[0]["d"]

    complete = (covered == db_total) and not report["errors"] and not args.limit and not args.sigs
    totals = {
        "db_ordinances": db_total,
        "covered_ordinances": covered,
        "complete": complete,
        "regions": len(entries),
        "unassigned_ordinances": (unassigned_entry or {}).get("counts", {}).get("total", 0),
        "articles": n_articles,
        "url_normalized": n_urlnorm,
        "bytes_meta": bytes_meta,
        "bytes_articles": bytes_art,
        "bytes_total": bytes_meta + bytes_art,
    }
    data = {
        "generator": "make_full_ordinance.py",
        "base": "api/ordinance",
        "data_as_of": data_as_of,
        "format": "columnar-v1" if args.format == "columnar" else "objects-v1",
        "columns": list(COLUMNS),
        "enum_columns": list(ENUM_COLUMNS),
        "derived": {"ordinance_id": "'ordin:' + mst", "official_url": URL_TEMPLATE},
        "totals": totals,
        "regions": entries,
        "unassigned": unassigned_entry,
        "articles_note": BODY_EXCLUDED_NOTE,
        "repealed_note": "폐지 조례 포함. status='repealed' 로 구분한다.",
        "verification_note": "verification_status 는 원문 링크 검증 상태다"
                             "(source-linked=원문 링크 확인, body-missing=본문 미수집).",
        "errors": report["errors"],
    }
    if not complete:
        data["warning"] = (
            f"전량 아님 — DB {db_total}건 중 {covered}건만 담겼다. "
            "(--limit/--sigs 로 부분 실행했거나 실패한 지역이 있다.)")
    env = envelope(data, generator="make_full_ordinance.py")
    size = write_json(out / "index.json", env, indent=1)
    return totals, size


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="자치법규 199,858건 전량 지역별 번들 생성")
    ap.add_argument("--out", default=str(ROOT / "data"), help="출력 루트(기본 system/data)")
    ap.add_argument("--limit", type=int, default=0, help="지역 수 상한(0=전체). 시험용")
    ap.add_argument("--sigs", default=None, help="sig_cd 직접 지정(쉼표)")
    ap.add_argument("--level", default=None, help="대상 레벨 필터(쉼표). 기본=전체")
    ap.add_argument("--format", choices=("columnar", "objects"), default="columnar",
                    help="columnar=열지향(기본, 3배 작다) / objects=객체 배열")
    ap.add_argument("--indent", type=int, default=None,
                    help="JSON 들여쓰기(기본 없음=최소 용량). 디버깅용으로 1 을 준다")
    ap.add_argument("--skip-articles", action="store_true", help="조문 제목 번들 생략")
    ap.add_argument("--skip-unassigned", action="store_true", help="미매칭 묶음 생략(권장하지 않음)")
    ap.add_argument("--force", action="store_true", help="이미 있는 번들도 재생성")
    a = ap.parse_args()

    out = Path(a.out) / "api" / "ordinance"
    out.mkdir(parents=True, exist_ok=True)

    cfg = get_config()
    conn = D.connect()
    levels = [int(s) for s in a.level.split(",") if s.strip()] if a.level else None
    sigs = {s.strip() for s in a.sigs.split(",") if s.strip()} if a.sigs else None

    t_all = time.time()
    print(f"DB={cfg.db_path}", flush=True)
    print("[적재] 카테고리 매핑…", flush=True)
    catmap = load_category_map(conn)
    targets, unassigned = load_targets(conn, levels, a.limit, sigs)
    total_expected = sum(t["expected"] for t in targets)
    print(f"출력={out}  대상 지역 {len(targets)}곳 / 조례 {total_expected:,}건"
          f"  미매칭 지역 {len(unassigned)}곳 / {sum(int(u['n']) for u in unassigned):,}건"
          f"  format={a.format} force={a.force} articles={not a.skip_articles}", flush=True)

    report = {"errors": []}
    entries = []
    for i, t in enumerate(targets, 1):
        entry, marks = emit(conn, out, catmap, t, a, report)
        entries.append(entry)
        done = time.time() - t_all
        eta = done / i * (len(targets) - i)
        print(f"  [{i}/{len(targets)}] {t['sig_cd']} {t['full_name'] or t['name']} · "
              + " ".join(marks) + f" · {done:.0f}s / ETA {eta:.0f}s", flush=True)

    unassigned_entry = None
    if unassigned and not a.skip_unassigned and not sigs:
        tgt = {
            "sig_cd": UNASSIGNED_KEY, "name": "지역 미매칭",
            "full_name": "sig_cd 미매칭 (시·도 교육청 등)",
            "level": None, "region_status": "unassigned",
            "region_ids": [u["region_id"] for u in unassigned],
            "expected": sum(int(u["n"]) for u in unassigned),
            "extra_cols": ("region_id", "org_name"),
            "members": [{"region_id": u["region_id"], "name": u["full_name"] or u["name"],
                         "level": u["level"], "count": int(u["n"])} for u in unassigned],
            "note": "시·도 교육청 등 sig_cd(행정구역코드)가 없는 발의주체의 자치법규다. "
                    "지도에는 찍히지 않지만 한 건도 빠뜨리지 않기 위해 별도 번들로 담는다. "
                    "행은 region_id/org_name 열로 주체를 구분한다.",
        }
        entry, marks = emit(conn, out, catmap, tgt, a, report)
        unassigned_entry = entry
        print(f"  [미매칭] {tgt['full_name']} · " + " ".join(marks), flush=True)

    totals, isize = build_index(conn, out, a, report, entries, unassigned_entry)

    print("-" * 72, flush=True)
    print(f"index.json {human(isize)}", flush=True)
    print(f"조례 DB {totals['db_ordinances']:,}건 중 담은 것 {totals['covered_ordinances']:,}건 "
          f"({'전량 일치' if totals['complete'] else '부분'})", flush=True)
    print(f"지역 번들 {totals['regions']}개 + 미매칭 {totals['unassigned_ordinances']:,}건", flush=True)
    print(f"조문 제목 {totals['articles']:,}건 (본문 제외)", flush=True)
    print(f"URL 정규화 {totals['url_normalized']:,}건", flush=True)
    print(f"용량 — 메타 {human(totals['bytes_meta'])} + 조문 {human(totals['bytes_articles'])}"
          f" = {human(totals['bytes_total'])}", flush=True)
    if report["errors"]:
        print(f"[실패] {len(report['errors'])}건 — index.json 의 data.errors 참조", flush=True)
    print(f"총 {time.time() - t_all:.0f}s", flush=True)
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
