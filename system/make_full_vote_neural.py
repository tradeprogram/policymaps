#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""실 DB → **표결·의안·신경망 전량** shard 생성.

기존 생성기가 '대표 표본'만 구웠던 세 자산을 전량으로 끌어올린다. 실측 대조는 이렇다.

    votes 57,178행 / 표결보유 의안 200건 → 기존 shard 149건   (51건 누락)
    bills 19,847건                        → shard 0건          (전량 누락)
    neural_similarity 1,893,530행          → 대표 조례 400건뿐  (조례 154,310 src 중 0.26%)

이 스크립트는 그 셋만 채운다. **기존 생성기의 봉투·살균·원자적 쓰기·재개 규율을 그대로 재사용**한다.

    make_gap_fixtures.envelope          응답 봉투(data/as_of_date/stale/disclaimer)
    make_more_fixtures._sanitize_keys   API 키 살균 (RAG 인덱스가 살균 전 URL 보유 → 필수)
    make_nationwide.write_json          살균 + 원자적 쓰기 (indent=1)
    make_nationwide.existing/human      재개 판정 · 용량 표기
    make_extend_fixtures.safe_slug      파일명 안전 슬러그
    mcp_server.server._tool_bill_vote_breakdown   표결 집계 엔진 그대로 호출

출력(<out>/api/):
    legislators.json              의원 320명 전량(표결·의안 shard 의 참조 대상)
    votes/{bill_no}.json          표결 의안 **200건 전량**. MCP 표결 집계 + 의원별 roll_call
    votes_index.json              의안 카탈로그(기존 스키마 유지: data.bills[])
    bill/{bucket}.json            의안 메타 **19,847건 전량**. bill_no 앞 5자리 버킷 207개
    bill_index.json               버킷 카탈로그 + 처리결과·발의자 집계
    neural/by-region/{sig_cd}.json 지역별 신경망 유사도 번들. **neural_similarity 전량**
    neural_by_region_index.json   지역 번들 카탈로그 + 커버리지
    full_vote_neural_report.json  실행 리포트(생성·재사용·실패·용량)

카탈로그를 shard 디렉터리 밖(api/{kind}_index.json)에 두는 이유: make_nationwide.build_index 가
api/{kind}/*.json 을 통째로 glob 해 shard 로 간주한다. 디렉터리 안에 index.json 을 넣으면
'index' 라는 이름의 가짜 shard 가 카탈로그에 섞인다.

용량 설계(실측 근거):
  * 신경망 Top-10 전량을 조례 1건당 객체로 펴면 133MB 다(사전 실측). 지역 번들 안에서
    **노드 인터닝**(조례 메타를 nodes[] 에 한 번만 두고 edges 는 정수 인덱스 참조)을 쓰면
    최대 지역(경기도 1,671 src)이 K=5 기준 1.56MB → 0.74MB 로 절반이 된다. 여기에
    compact JSON(공백 없음)을 더해 0.41MB. 그래서 K 를 줄이지 않고 **저장 Top-10 전량**을
    담는다. 조례명·지자체명은 반복이 심해 인터닝 효과가 크다.
  * 원문 링크는 노드마다 넣지 않고 mst 만 담는다. canonical_url 은
    'https://www.law.go.kr/ordinInfoP.do?ordinSeq={mst}' 로 전량 일치함을 확인했다
    (불일치 0건 / null 40,406건은 폐지 조례이며 같은 규칙으로 복원된다).
  * 의원 메타(320명)는 파일마다 복제하지 않고 api/legislators.json 한 곳에 둔다.
    표결 shard·의안 버킷은 legislator_id 로 참조한다. 복제하면 표결 200파일 × 294명,
    의안 207버킷 × 320명이 순수 중복이 된다[실측: 표결 파일당 50KB → 29KB,
    의안 파일당 107KB → 84KB].
  * 의안 버킷은 bill_no 앞 5자리(207개·파일당 약 100건 = 84KB)로 쪼갠다. 앞 4자리는
    21개 파일이지만 파일당 840KB 라 의안 1건 보려고 840KB 를 받게 된다[실측].

표기 규율: 모든 봉투에 as_of_date·disclaimer 를 싣고, 조례 노드는 repealed_on/status 를
그대로 실어 폐지본을 화면에서 경고할 수 있게 한다. 신경망은 '선례 추천'이 아니라
'탐색 보조'임을 봉투 disclaimer 에 못박는다(make_neural_fixtures 와 동일 문구 재사용).

재개 가능: 이미 만들어진 shard 는 건너뛴다. 단 **스키마 버전이 다르면 재생성**한다
(기존 votes shard 149건은 roll_call 이 없는 구 스키마라 자동으로 다시 굽힌다).
원자적 쓰기라 중간에 끊겨도 반쪽 파일이 남지 않는다. 한 건이 실패해도 나머지는 계속.

사용:
  cd system
  python make_full_vote_neural.py --only votes  --limit 3     # 표결 소규모 시험
  python make_full_vote_neural.py --only neural --limit 3      # 신경망 소규모 시험(용량 실측)
  python make_full_vote_neural.py --dry-run                    # 쓰지 않고 용량만 추정
  python make_full_vote_neural.py                              # 전량
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:  # 콘솔이 cp949 여도 한글 출력에서 죽지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from policymap import db as D                                  # noqa: E402
from policymap.config import get_config                        # noqa: E402
from policymap.mcp_server.server import Server                 # noqa: E402

# 중복 구현 금지 — 봉투·살균·쓰기·슬러그는 기존 생성기 것을 그대로 쓴다.
from make_gap_fixtures import envelope                         # noqa: E402
from make_more_fixtures import _sanitize_keys                  # noqa: E402
from make_nationwide import existing, human, write_json        # noqa: E402
from make_extend_fixtures import safe_slug                     # noqa: E402

try:  # numpy 미탑재 환경에서도 표결·의안은 굽히도록 상수만 안전하게 가져온다
    from make_neural_fixtures import NEURAL_DISCLAIMER         # noqa: E402
except Exception:  # pragma: no cover  # noqa: BLE001
    NEURAL_DISCLAIMER = (
        "그래프 신경망 임베딩의 코사인 유사도다. 조문 의미가 아니라 그래프 구조를 학습한 "
        "값이므로 '선례 추천'이 아니라 '탐색 보조'로만 쓸 것. 폐지 조례는 선례로 인용하지 "
        "말 것. 인용 전 원문 링크로 확인할 것."
    )

KINDS = ("votes", "bill", "neural")

# 스키마 버전 — 올리면 기존 shard 를 자동으로 다시 굽는다(재개 규율과 양립).
SCHEMA = {
    "votes": "votes/2",      # 1 = make_extend_fixtures(roll_call 없음), 2 = 의원별 표결 포함
    "bill": "bill/1",
    "neural": "neural-by-region/1",
}

ORD_URL_TEMPLATE = "https://www.law.go.kr/ordinInfoP.do?ordinSeq={mst}"
BILL_URL_TEMPLATE = "https://likms.assembly.go.kr/bill/billDetail.do?billId={bill_id}"

VOTE_DISCLAIMER = (
    "국회 의안·표결 원자료(열린국회정보)를 그대로 집계한 값이다. tally_reported 는 의안별 "
    "표결현황 공표치, tally_from_votes 는 의원별 표결기록 재집계치이며 두 값이 어긋날 수 "
    "있다(공표 시점 차이). 정당은 표결 당시 소속(party_at_vote)이다. 인용 전 원문 확인할 것."
)
BILL_DISCLAIMER = (
    "제22대 국회 의안 메타 전량이다. proc_result 가 비어 있는 의안은 계류 중이거나 "
    "처리결과가 아직 수집되지 않은 것이며 '부결'이 아니다. enacted_instrument_id 는 "
    "확률적 매칭 결과이므로 enact_verified=1 이 아닌 연결은 추정이다."
)


# --------------------------------------------------------------------------- #
# 공통 유틸
# --------------------------------------------------------------------------- #
def write_env(path: Path, env: dict, compact: bool = False) -> int:
    """살균 + 원자적 쓰기. compact=False 면 기존 write_json 을 그대로 쓴다.

    compact 는 신경망 번들 전용이다. indent=1 로 굽으면 전량이 100MB 를 넘어
    배포 예산(api/ 250MB)을 잠식한다 — 같은 내용이 공백 없이는 40% 수준이다[실측].
    살균(_sanitize_keys)과 tmp→os.replace 원자성은 write_json 과 동일하게 지킨다.
    """
    if not compact:
        return write_json(path, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(_sanitize_keys(env), ensure_ascii=False, separators=(",", ":"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return path.stat().st_size


def schema_of(path: Path):
    """이미 만들어진 shard 의 스키마 버전. 못 읽으면 None."""
    try:
        with path.open(encoding="utf-8") as fh:
            head = fh.read(4096)
        i = head.find('"schema"')
        if i < 0:
            return None
        j = head.find('"', head.find(":", i) + 1)
        k = head.find('"', j + 1)
        return head[j + 1:k]
    except OSError:
        return None


class Ctx:
    """실행 컨텍스트(연결·서버·출력경로·리포트)를 한 곳에 모은다."""

    def __init__(self, conn, srv, out, args, report):
        self.conn, self.srv, self.out, self.args, self.report = conn, srv, out, args, report
        self._legs = None

    def fail(self, kind, key, e):
        msg = f"{type(e).__name__}: {str(e)[:160]}"
        self.report["errors"].append({"kind": kind, "key": key, "error": msg})
        print(f"  [실패] {kind}/{key} — {msg}", flush=True)

    def warn(self, kind, key, message):
        self.report["warnings"].append({"kind": kind, "key": key, "message": message})
        print(f"  [경고] {kind}/{key} — {message}", flush=True)


def emit(ctx, kind, key, rel, builder, note_fn=None, compact=False):
    """shard 1건 생성. 존재하고 스키마가 같으면 skip. 반환: 카탈로그 항목 dict 또는 None."""
    path = ctx.out / rel
    want = SCHEMA[kind]
    size = None if ctx.args.force else existing(path)
    if size is not None and schema_of(path) == want:
        item = {"key": key, "path": rel, "bytes": size, "reused": True}
        if note_fn:  # 재사용본도 카탈로그 메모는 채운다(빈 칸이 생기면 화면이 '-' 로 보인다)
            try:
                item.update(note_fn(json.loads(path.read_text(encoding="utf-8"))) or {})
            except Exception:  # noqa: BLE001
                pass
        return item
    t0 = time.time()
    try:
        env = builder()
    except Exception as e:  # noqa: BLE001
        ctx.fail(kind, key, e)
        return None
    # schema 는 **맨 앞**에 둔다. 봉투 끝에 붙이면 compact 로 구운 53MB 번들에서
    # schema_of() 가 읽는 앞 4KB 안에 들어오지 않아 재개가 전부 무효가 된다[실측].
    env = {"schema": want, **env}
    if ctx.args.dry_run:
        body = json.dumps(_sanitize_keys(env), ensure_ascii=False,
                          separators=(",", ":") if compact else None,
                          indent=None if compact else 1)
        size = len(body.encode("utf-8"))
    else:
        size = write_env(path, env, compact=compact)
    item = {"key": key, "path": rel, "bytes": size, "reused": False,
            "seconds": round(time.time() - t0, 2)}
    if note_fn:
        try:
            item.update(note_fn(env) or {})
        except Exception:  # noqa: BLE001
            pass
    return item


def write_catalog(ctx, rel, data, **extra):
    """카탈로그(색인)는 dry-run 이어도 크기만 계산하고 쓰지 않는다."""
    env = envelope(data, **extra)
    if ctx.args.dry_run:
        return len(json.dumps(_sanitize_keys(env), ensure_ascii=False, indent=1).encode("utf-8"))
    return write_json(ctx.out / rel, env)


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------------------- #
# 1) votes — 표결 보유 의안 전량 + 의원별 roll_call
# --------------------------------------------------------------------------- #
def ensure_legislators(ctx):
    """의원 320명 공용 자산. 표결 shard·의안 버킷이 legislator_id 로 참조한다.

    파일마다 의원표를 복제하지 않기 위한 단일 출처다. 두 빌더가 모두 호출하므로
    실행당 한 번만 굽는다. 반환: legislator_id → row.
    """
    if getattr(ctx, "_legs", None) is not None:
        return ctx._legs
    rows = D.fetchall(ctx.conn, "SELECT legislator_id, mona_cd, name, name_hanja, "
                                "current_party, district, elect_type, reelection, units, "
                                "sex, committees, age_first FROM legislators ORDER BY name")
    party_counts = defaultdict(int)
    for r in rows:
        party_counts[r["current_party"] or "(미상)"] += 1
    n = write_catalog(ctx, "legislators.json", {
        "legislators": rows, "count": len(rows),
        "party_counts": dict(sorted(party_counts.items(), key=lambda x: -x[1])),
    }, disclaimer=VOTE_DISCLAIMER)
    print(f"[의원] legislators.json — {len(rows)}명 · {human(n)}", flush=True)
    ctx._legs = {r["legislator_id"]: r for r in rows}
    return ctx._legs


def build_votes(ctx):
    a = ctx.args
    conn = ctx.conn
    total_bills = D.fetchone(conn, "SELECT COUNT(DISTINCT bill_id) n FROM votes")["n"]
    total_rows = D.fetchone(conn, "SELECT COUNT(*) n FROM votes")["n"]
    rows = D.fetchall(conn, """
        SELECT v.bill_id, COUNT(*) AS n, b.bill_no, b.name, b.committee,
               b.propose_dt, b.proc_dt, b.proc_result, b.proc_result_cd,
               b.member_tcnt, b.vote_tcnt, b.yes_tcnt, b.no_tcnt, b.blank_tcnt
          FROM votes v LEFT JOIN bills b ON b.bill_id = v.bill_id
         GROUP BY v.bill_id
         ORDER BY n DESC, b.proc_dt DESC, v.bill_id ASC""")
    if a.limit:
        rows = rows[: a.limit]
    print(f"[표결] 표결기록 보유 의안 {total_bills}건 / 표결행 {total_rows:,}행 "
          f"→ shard {len(rows)}건 생성", flush=True)

    legs = ensure_legislators(ctx)

    def roll_call(bill_id):
        """의원별 표결 전량.

        레코드에 legislator_id 와 이름을 함께 실어 shard 하나로 화면이 완결되게 한다.
        지역구·재선 같은 나머지 의원 메타는 파일마다 반복하지 않고 api/legislators.json
        한 곳에 둔다(200개 파일 × 294명 = 58,800회 중복 방지 — 실측 파일당 50KB → 25KB).
        """
        vs = D.fetchall(conn, """
            SELECT legislator_id, result_vote_mod, party_at_vote, vote_date
              FROM votes WHERE bill_id=? ORDER BY party_at_vote, legislator_id""", (bill_id,))
        recs = [[v["legislator_id"], (legs.get(v["legislator_id"]) or {}).get("name"),
                 v["result_vote_mod"], v["party_at_vote"]] for v in vs]
        dates = sorted({v["vote_date"] for v in vs if v["vote_date"]})
        return {
            "record_fields": ["legislator_id", "name", "result_vote_mod", "party_at_vote"],
            "records": recs,
            "vote_dates": dates,
            "legislators_ref": "api/legislators.json",
            "unknown_legislators": sum(1 for v in vs if v["legislator_id"] not in legs),
        }

    def build_one(r):
        env = ctx.srv._tool_bill_vote_breakdown({"bill_id": r["bill_id"]})
        data = env.get("data") or {}
        data["roll_call"] = roll_call(r["bill_id"])
        data["bill"]["link_url"] = BILL_URL_TEMPLATE.format(bill_id=r["bill_id"])
        data["vote_records"] = r["n"]
        env["disclaimer"] = VOTE_DISCLAIMER
        return env

    items = []
    for i, r in enumerate(rows, 1):
        key = str(r.get("bill_no") or r["bill_id"])
        it = emit(ctx, "votes", key, f"votes/{safe_slug(key)}.json",
                  lambda rr=r: build_one(rr),
                  note_fn=lambda env: {
                      "roll_call_records": len(
                          ((env.get("data") or {}).get("roll_call") or {}).get("records") or [])})
        if it is None:
            continue
        it.update({"bill_id": r["bill_id"], "bill_no": r.get("bill_no"),
                   "name": r.get("name"), "committee": r.get("committee"),
                   "propose_dt": r.get("propose_dt"), "proc_dt": r.get("proc_dt"),
                   "proc_result": r.get("proc_result") or r.get("proc_result_cd"),
                   "vote_records": r["n"], "yes_tcnt": r.get("yes_tcnt"),
                   "no_tcnt": r.get("no_tcnt"), "blank_tcnt": r.get("blank_tcnt")})
        items.append(it)
        if i <= 5 or i % 50 == 0 or i == len(rows):
            print(f"  [{i}/{len(rows)}] {key} · 표결 {r['n']}건 · "
                  f"{(r.get('name') or '')[:28]} · "
                  f"{'skip' if it['reused'] else human(it['bytes'])}", flush=True)

    covered = {it["bill_id"] for it in items}
    missing = [r["bill_id"] for r in rows if r["bill_id"] not in covered]
    if missing:
        ctx.warn("votes", "coverage", f"미생성 {len(missing)}건")
    ctx.report["votes"] = {"shards": len(items), "bytes": sum(i["bytes"] for i in items),
                           "bills_with_votes": total_bills, "missing": len(missing)}
    write_catalog(ctx, "votes_index.json", {
        "bills": items,
        "totals": {"bills_with_votes": total_bills, "shards": len(items),
                   "vote_rows": total_rows,
                   "bills_total": D.fetchone(conn, "SELECT COUNT(*) n FROM bills")["n"]},
        "selection": {"mode": "all", "note": "표결기록 보유 의안 전량"},
        "layout": {"shard": "api/votes/{bill_no}.json", "schema": SCHEMA["votes"]},
    }, disclaimer=VOTE_DISCLAIMER)
    print(f"[표결] 완료 — shard {len(items)}건 / "
          f"{human(sum(i['bytes'] for i in items))}", flush=True)


# --------------------------------------------------------------------------- #
# 2) bill — 의안 메타 전량(표결 없는 것 포함), bill_no 앞 4자리 버킷
# --------------------------------------------------------------------------- #
BILL_FIELDS = ["bill_id", "bill_no", "age", "name", "committee", "committee_id",
               "propose_dt", "proc_dt", "proc_result", "proc_result_cd", "bill_kind_cd",
               "member_tcnt", "vote_tcnt", "yes_tcnt", "no_tcnt", "blank_tcnt",
               "enacted_instrument_id", "enact_match_method", "enact_verified"]


def bucket_of(bill_no, width):
    s = str(bill_no or "")
    return s[:width] if len(s) >= width else (s or "unknown").ljust(width, "0")


def build_bill(ctx):
    a = ctx.args
    conn = ctx.conn
    total = D.fetchone(conn, "SELECT COUNT(*) n FROM bills")["n"]
    legs = ensure_legislators(ctx)
    props = defaultdict(list)
    for r in D.fetchall(conn, "SELECT bill_id, legislator_id, role FROM bill_proposers"):
        props[r["bill_id"]].append((r["role"], r["legislator_id"]))
    voted = {r["bill_id"] for r in D.fetchall(conn, "SELECT DISTINCT bill_id FROM votes")}

    rows = D.fetchall(conn, f"SELECT {', '.join(BILL_FIELDS)}, link_url, detail_link "
                            f"FROM bills ORDER BY bill_no")
    buckets = defaultdict(list)
    for r in rows:
        buckets[bucket_of(r["bill_no"], a.bill_bucket_width)].append(r)
    keys = sorted(buckets)
    if a.limit:
        keys = keys[: a.limit]
    print(f"[의안] bills {total:,}건 → 버킷 {len(buckets)}개(앞 {a.bill_bucket_width}자리) · "
          f"발의자 {sum(len(v) for v in props.values()):,}행 · 이번 실행 {len(keys)}개",
          flush=True)

    # 처리결과 집계는 shard 생성 여부와 무관하게 DB 에서 바로 센다.
    # 빌더 안에서 세면 재개 실행(전부 skip)에서 빈 dict 가 되어 색인이 망가진다[실측].
    proc_counter = defaultdict(int)
    for r in rows:
        proc_counter[r["proc_result"] or r["proc_result_cd"] or "(미기재)"] += 1
    items = []
    for i, bk in enumerate(keys, 1):
        brs = buckets[bk]

        def build_one(brs=brs, bk=bk):
            out = []
            for r in brs:
                d = {k: r[k] for k in BILL_FIELDS}
                pr = props.get(r["bill_id"]) or []
                # 대표발의(RST)는 이름·정당까지 실어 목록 화면이 한 번의 fetch 로 끝나게 하고,
                # 공동발의(PUBL, 평균 11.2명)는 legislator_id 만 둔다. 나머지 의원 메타는
                # api/legislators.json(320명) 한 곳에만 둔다 — 버킷마다 표를 복제하면
                # 파일당 30KB(=전체 6MB)가 순수 중복이 된다[실측].
                d["rst"] = [{"legislator_id": lid,
                             "name": (legs.get(lid) or {}).get("name"),
                             "current_party": (legs.get(lid) or {}).get("current_party"),
                             "district": (legs.get(lid) or {}).get("district")}
                            for role, lid in pr if role == "RST"]
                d["publ"] = [lid for role, lid in pr if role != "RST"]
                d["proposer_count"] = len(pr)
                d["has_votes"] = 1 if r["bill_id"] in voted else 0
                if d["has_votes"]:
                    d["votes_shard"] = f"api/votes/{safe_slug(str(r['bill_no']))}.json"
                d["link_url"] = r["link_url"] or r["detail_link"] or \
                    BILL_URL_TEMPLATE.format(bill_id=r["bill_id"])
                out.append(d)
            return envelope({
                "bucket": bk,
                "count": len(out),
                "url_template": BILL_URL_TEMPLATE,
                "legislators_ref": "api/legislators.json",
                "bills": out,
            }, disclaimer=BILL_DISCLAIMER)

        it = emit(ctx, "bill", bk, f"bill/{bk}.json", build_one,
                  note_fn=lambda env: {"count": (env.get("data") or {}).get("count")})
        if it is None:
            continue
        it["count"] = it.get("count") or len(brs)
        items.append(it)
        if i <= 3 or i % 5 == 0 or i == len(keys):
            print(f"  [{i}/{len(keys)}] {bk} · 의안 {len(brs)}건 · "
                  f"{'skip' if it['reused'] else human(it['bytes'])}", flush=True)

    got = sum(it["count"] for it in items)
    ctx.report["bill"] = {"shards": len(items), "bytes": sum(i["bytes"] for i in items),
                          "bills_total": total, "bills_in_shards": got}
    if not a.limit and got != total:
        ctx.warn("bill", "coverage", f"버킷 합계 {got} ≠ bills {total}")
    write_catalog(ctx, "bill_index.json", {
        "buckets": items,
        "totals": {"bills_total": total, "bills_in_shards": got, "shards": len(items),
                   "proposer_rows": sum(len(v) for v in props.values()),
                   "legislators": len(legs), "bills_with_votes": len(voted)},
        "proc_result_counts": dict(sorted(proc_counter.items(), key=lambda x: -x[1])),
        "layout": {"shard": "api/bill/{bucket}.json",
                   "bucket_rule": f"bill_no 앞 {a.bill_bucket_width}자리",
                   "schema": SCHEMA["bill"]},
    }, disclaimer=BILL_DISCLAIMER)
    print(f"[의안] 완료 — 버킷 {len(items)}개 / 의안 {got:,}건 / "
          f"{human(sum(i['bytes'] for i in items))}", flush=True)


# --------------------------------------------------------------------------- #
# 3) neural — neural_similarity 전량을 지역 번들로
# --------------------------------------------------------------------------- #
# 노드는 화면에 필요한 최소만 담는다. mst 는 ordinance_id('ordin:2146281') 의 접미사와
# 항상 같아 중복이고(불일치 0건 실측), 지자체명은 sig_cd 로 기존 regions 색인에서 붙는다.
# repealed_on/status 는 폐지 경고 규율상 반드시 남긴다.
NODE_FIELDS = ["ordinance_id", "name", "sig_cd", "repealed_on", "status"]
EDGE_FIELDS = ["src", "dst", "sim", "rank"]


def build_neural(ctx):
    a = ctx.args
    conn = ctx.conn
    t0 = time.time()

    models = [r["model_name"] for r in D.fetchall(
        conn, "SELECT model_name, COUNT(*) n FROM neural_similarity "
              "WHERE node_kind='Ordinance' GROUP BY model_name ORDER BY n DESC")]
    if a.neural_models:
        want = {s.strip() for s in a.neural_models.split(",") if s.strip()}
        models = [m for m in models if m in want]
    total_rows = D.fetchone(
        conn, "SELECT COUNT(*) n FROM neural_similarity WHERE node_kind='Ordinance'")["n"]

    # 조례 메타 199,858건 (약 60MB RAM). region_id → sig_cd/이름은 regions 에서.
    regs = {r["region_id"]: r for r in D.fetchall(
        conn, "SELECT region_id, sig_cd, name, full_name, level, status, has_legislation "
              "FROM regions")}
    ords, by_region = {}, defaultdict(list)
    for r in D.fetchall(conn, "SELECT ordinance_id, region_id, name, "
                              "repealed_on, status FROM ordinances"):
        rg = regs.get(r["region_id"])
        ords[r["ordinance_id"]] = (
            r["name"], (rg or {}).get("sig_cd") or r["region_id"], (rg or {}).get("name"),
            r["repealed_on"], r["status"])
        by_region[r["region_id"]].append(r["ordinance_id"])

    # 지역-지역 신경망 유사도(537 src)는 해당 지역 번들 안에 함께 싣는다(파일 수 증가 없음).
    region_nb = defaultdict(lambda: defaultdict(list))
    for r in D.fetchall(conn, "SELECT src_id, dst_id, model_name, cosine_sim, rank "
                              "FROM neural_similarity WHERE node_kind='Region' "
                              "ORDER BY src_id, model_name, rank"):
        src = r["src_id"].split(":", 1)[1]
        dst = r["dst_id"].split(":", 1)[1]
        drg = regs.get(dst) or {}
        region_nb[src][r["model_name"]].append(
            {"sig_cd": drg.get("sig_cd") or dst, "name": drg.get("name"),
             "sim": round(r["cosine_sim"], 4), "rank": r["rank"]})

    targets = [rid for rid in sorted(by_region, key=lambda x: (len(by_region[x]), x),
                                     reverse=True)]
    if a.limit:
        targets = targets[: a.limit]
    print(f"[신경망] neural_similarity(Ordinance) {total_rows:,}행 · 모델 {models} · "
          f"조례 {len(ords):,}건 · 지역 {len(by_region)}곳 → 이번 실행 {len(targets)}곳 "
          f"(top_k={a.neural_topk}) · 적재 {time.time()-t0:.1f}s", flush=True)

    ph_models = ",".join("?" for _ in models)
    items, cov_src, cov_edges = [], 0, 0
    for i, rid in enumerate(targets, 1):
        rg = regs.get(rid) or {}
        sig = rg.get("sig_cd") or rid
        oids = by_region[rid]

        def build_one(rid=rid, rg=rg, sig=sig, oids=oids):
            rows = []
            for part in chunks(oids, 800):
                ph = ",".join("?" * len(part))
                rows += D.fetchall(conn, f"""
                    SELECT src_id, dst_id, model_name, cosine_sim, rank
                      FROM neural_similarity
                     WHERE node_kind='Ordinance' AND rank<=? AND model_name IN ({ph_models})
                       AND src_id IN ({ph})""",
                    [a.neural_topk] + models + ["ordinance:" + o for o in part])
            nodes, nidx = [], {}

            def ni(oid):
                if oid in nidx:
                    return nidx[oid]
                m = ords.get(oid)
                if m is None:
                    return None
                nidx[oid] = len(nodes)
                nodes.append([oid, m[0], m[1], m[3], m[4]])
                return nidx[oid]

            edges = {m: [] for m in models}
            srcs = set()
            for r in sorted(rows, key=lambda x: (x["src_id"], x["model_name"], x["rank"])):
                s = ni(r["src_id"].split(":", 1)[1])
                d = ni(r["dst_id"].split(":", 1)[1])
                if s is None or d is None:
                    continue
                edges[r["model_name"]].append([s, d, round(r["cosine_sim"], 4), r["rank"]])
                srcs.add(s)
            n_edges = sum(len(v) for v in edges.values())
            return envelope({
                "sig_cd": sig,
                "region": {"region_id": rid, "sig_cd": sig, "name": rg.get("name"),
                           "full_name": rg.get("full_name"), "level": rg.get("level"),
                           "status": rg.get("status"),
                           "in_picker": bool(rg.get("status") == "active"
                                             and rg.get("has_legislation")
                                             and rg.get("level") in (1, 2))},
                "top_k": a.neural_topk,
                "models": models,
                "url_template": ORD_URL_TEMPLATE,
                "node_fields": NODE_FIELDS,
                "nodes": nodes,
                "edge_fields": EDGE_FIELDS,
                "edges": edges,
                "region_neighbors": {m: v for m, v in (region_nb.get(rid) or {}).items()
                                     if m in models},
                "coverage": {
                    "ordinances_in_region": len(oids),
                    "src_covered": len(srcs),
                    "src_missing": len(oids) - len(srcs),
                    "nodes": len(nodes),
                    "edges": n_edges,
                    "edges_by_model": {m: len(v) for m, v in edges.items()},
                },
            }, disclaimer=NEURAL_DISCLAIMER)

        it = emit(ctx, "neural", sig, f"neural/by-region/{safe_slug(sig)}.json", build_one,
                  note_fn=lambda env: (env.get("data") or {}).get("coverage") or {},
                  compact=not a.indent_neural)
        if it is None:
            continue
        it.update({"sig_cd": sig, "name": rg.get("name"), "level": rg.get("level"),
                   "region_id": rid})
        cov_src += it.get("src_covered") or 0
        cov_edges += it.get("edges") or 0
        items.append(it)
        if i <= 5 or i % 25 == 0 or i == len(targets):
            print(f"  [{i}/{len(targets)}] {sig} {rg.get('name') or rid} · "
                  f"조례 {len(oids)} · src {it.get('src_covered', '-')} · "
                  f"edge {it.get('edges', '-')} · "
                  f"{'skip' if it['reused'] else human(it['bytes'])}", flush=True)

    total_bytes = sum(i["bytes"] for i in items)
    ctx.report["neural"] = {"shards": len(items), "bytes": total_bytes,
                            "rows_total": total_rows, "edges_in_shards": cov_edges,
                            "src_in_shards": cov_src, "models": models,
                            "top_k": a.neural_topk}
    write_catalog(ctx, "neural_by_region_index.json", {
        "regions": items,
        "totals": {"shards": len(items), "bytes": total_bytes,
                   "similarity_rows_total": total_rows,
                   "edges_in_shards": cov_edges, "src_in_shards": cov_src,
                   "ordinances_total": len(ords), "models": models,
                   "top_k": a.neural_topk},
        "layout": {"shard": "api/neural/by-region/{sig_cd}.json",
                   "schema": SCHEMA["neural"],
                   "note": "nodes[] 인터닝 + edges[] 정수 인덱스 참조. compact JSON.",
                   "node_fields": NODE_FIELDS, "edge_fields": EDGE_FIELDS,
                   "url_template": ORD_URL_TEMPLATE},
    }, disclaimer=NEURAL_DISCLAIMER)
    print(f"[신경망] 완료 — 지역 {len(items)}곳 / src {cov_src:,} / edge {cov_edges:,} / "
          f"{human(total_bytes)}", flush=True)


BUILDERS = {"votes": build_votes, "bill": build_bill, "neural": build_neural}


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="표결·의안·신경망 전량 shard 생성")
    ap.add_argument("--out", default=str(ROOT / "data"), help="출력 루트(기본 system/data)")
    ap.add_argument("--only", default=",".join(KINDS),
                    help=f"생성 대상 쉼표구분 {KINDS}")
    ap.add_argument("--force", action="store_true", help="기존 shard 무시하고 재생성")
    ap.add_argument("--limit", type=int, default=0,
                    help="소규모 시험: votes=의안수 · bill=버킷수 · neural=지역수")
    ap.add_argument("--dry-run", action="store_true",
                    help="파일을 쓰지 않고 용량만 실측 추정")
    ap.add_argument("--bill-bucket-width", type=int, default=5,
                    help="의안 버킷 자릿수(기본 5 → 버킷 207개·파일당 약 100건)")
    ap.add_argument("--neural-topk", type=int, default=5,
                    help="신경망 Top-K(저장본 최대 10). 기본 5 = 배포 예산 안에서의 최대치. "
                         "로컬 완전판은 10 으로 굽는다(용량 79MB)")
    ap.add_argument("--neural-models", default="",
                    help="모델 한정(쉼표구분). 비우면 전량")
    ap.add_argument("--indent-neural", action="store_true",
                    help="신경망 번들도 indent=1 로(용량 2.5배). 기본은 compact")
    a = ap.parse_args()
    a.only = [k.strip() for k in a.only.split(",") if k.strip()]
    bad = [k for k in a.only if k not in KINDS]
    if bad:
        print(f"[중단] 알 수 없는 --only: {bad} (가능: {list(KINDS)})", flush=True)
        return 2

    out = Path(a.out) / "api"
    out.mkdir(parents=True, exist_ok=True)
    cfg = get_config()
    conn = D.connect()
    srv = Server(conn, cfg) if "votes" in a.only else None

    report = {
        "generator": "make_full_vote_neural.py",
        "as_of_date": time.strftime("%Y-%m-%d"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "db_path": str(cfg.db_path), "out": str(out), "only": a.only,
        "args": {"limit": a.limit, "force": a.force, "dry_run": a.dry_run,
                 "neural_topk": a.neural_topk, "bill_bucket_width": a.bill_bucket_width},
        "errors": [], "warnings": [],
    }
    ctx = Ctx(conn, srv, out, a, report)
    print(f"DB={cfg.db_path}\n출력={out}  생성={a.only}  force={a.force}  "
          f"dry_run={a.dry_run}  limit={a.limit or '-'}", flush=True)

    t0 = time.time()
    for kind in KINDS:
        if kind not in a.only:
            continue
        try:
            BUILDERS[kind](ctx)
        except Exception as e:  # noqa: BLE001
            ctx.fail(kind, "(전체)", e)
            import traceback
            traceback.print_exc()
    report["seconds"] = round(time.time() - t0, 1)
    # --only 로 나눠 돌려도 리포트가 덮이지 않도록 이전 실행의 다른 kind 결과를 물려받는다.
    prev = {}
    try:
        prev = (json.loads((out / "full_vote_neural_report.json").read_text(encoding="utf-8"))
                .get("data") or {})
    except Exception:  # noqa: BLE001
        pass
    for k in KINDS:
        if k not in a.only and isinstance(prev.get(k), dict):
            report[k] = prev[k]
            report.setdefault("carried_over", []).append(k)
    report["totals"] = {
        k: report[k].get("bytes", 0) for k in KINDS if isinstance(report.get(k), dict)}
    report["total_bytes"] = sum(report["totals"].values())
    if not a.dry_run:
        write_json(out / "full_vote_neural_report.json", envelope(report))
    print(f"\n=== 완료 {report['seconds']}s · 합계 {human(report['total_bytes'])} · "
          f"실패 {len(report['errors'])} · 경고 {len(report['warnings'])} ===", flush=True)
    for k in a.only:
        v = report.get(k)
        if isinstance(v, dict):
            print(f"  {k:8s} shard {v.get('shards')}건 · {human(v.get('bytes', 0))}", flush=True)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
