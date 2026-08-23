#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""실 DB → **법령 전량(29,811건)** + **위임관계 전량(421,627건)** 정적 shard 생성.

기존 생성기는 상위 200개 법령만 담았다(make_extend_fixtures.build_statute →
api/statute/{slug}.json, 조문 본문 포함). 이 스크립트는 **표본이 아니라 전량**을
담되, 조문 본문은 빼고 메타·위임관계만 실어 배포 가능한 용량으로 유지한다.

    api/statute/all/{bucket}.json   법령·행정규칙 전량 메타(tier·kind·공포/시행일·
                                    official_url·근거조례수). 기본 48버킷.
    api/statute/names/{bucket}.json 정규화 법령명 → [instrument_id, 버킷키] 매핑
    api/statute/index.json          버킷 색인 + 해시 규격 + 이름색인 카탈로그 + 총계
    api/delegation/{sig_cd}.json    지역별 위임관계 전량(조례→상위법) 243파일 + _unassigned
    api/delegation/index.json       지역 파일 카탈로그

기존 api/statute/{slug}.json(대표 200건, 조문 본문 포함)은 **건드리지 않는다** —
상세 열람용으로 그대로 남는다. 새 파일은 all/ · names/ 하위 디렉터리와
index.json 뿐이라 이름이 겹치지 않는다(slug 에 '/' 가 들어갈 수 없다).

용량 설계(왜 이 형식인가)
------------------------
- **버킷팅**: 29,811개를 파일 하나로 내면 6MB 단일 다운로드가 된다. 반대로 파일당
  1건이면 한 디렉터리에 3만 파일이라 git·배포가 무너진다. fnv1a32(instrument_id)%N
  으로 N개 버킷에 고르게 흩는다. 프론트는 instrument_id 만 있으면 색인 조회 없이
  버킷을 **계산**해서 그 파일만 받는다(해시 규격은 index.json 에 JS 구현까지 싣는다).
- **이름→버킷**: 전체 이름 맵을 index.json 에 인라인하면 index 자체가 2MB 를 넘겨
  (실측 추정: 29,811 × 약 72B) 모든 화면이 그 2MB 를 먼저 받는다. 그래서 이름 맵도
  같은 해시로 버킷에 쪼개고, index.json 은 그 카탈로그와 해시 규격만 싣는다.
  이름 조회는 names 버킷 1회 + all 버킷 1회, 총 2회 요청이다.
  (index.json 에 통째로 넣고 싶으면 --inline-name-map)
- **위임관계 컬럼 형식**: 421,627행에 parent 법령명을 행마다 박으면 법령명만
  약 28MB 다(평균 22자 × 3B). 그래서 parents/children 사전을 파일 앞에 두고 행은
  그 인덱스를 참조하는 **컬럼 배열**로 낸다. 행 하나가 약 34B 로 줄어든다.
  columns/defaults/codes 를 파일이 스스로 들고 있어 해석에 외부 지식이 필요 없다.
- **조례 official_url 생략**: 199,858건 전부가 mst 에서 기계적으로 유도된다
  [실측: 유도 불일치 0건]. 그래서 파일 단위 url_template + mst 만 싣는다
  (행당 약 85B 절약). 법령 official_url 은 efYd 가 제각각이라 유도가 안 되므로
  all/ 버킷에 그대로 싣는다.

표기 규율(유지)
--------------
- as_of_date: 모든 응답 봉투에 들어간다(envelope 재사용).
- 추정 배지: parent_id 가 'lawname:*' 인 위임행은 법령 원문이 아니라 조례 본문의
  인용 문구에서 이름만 뽑은 것이다. 이름해소에 성공해도 resolved_by='name-match'
  로 표시하고 단정하지 않는다. 해소 실패는 resolved=false + note.
- 폐지 경고: 조례는 repealed, 법령은 repealed_on/current_history 를 그대로 싣는다.
- 검증상태: 위임행마다 verification_status(article-verified/article-missing/
  unverifiable)를 코드로 싣고, 파일 stats 에 분포를 남긴다.

중복 구현 금지 — 봉투·살균·원자적쓰기·이름해소·URL보정은 기존 생성기 것을 그대로 쓴다.
  envelope        ← make_gap_fixtures
  _sanitize_keys  ← make_more_fixtures   (RAG 인덱스가 살균 전 URL 을 갖고 있어 필수)
  existing/human  ← make_nationwide
  write_shard     ← make_graph_fixtures  (살균 + 원자적 쓰기, indent 없음)
  Resolver/norm_name ← make_graph_fixtures
  abs_url/ymd/load_regions ← make_extend_fixtures

재개 가능: 이미 만들어진 파일은 건너뛴다(--force 로 강제). 원자적 쓰기라 중간에
끊겨도 반쪽 파일이 남지 않는다. 한 지역이 실패해도 나머지는 계속 만든다.

사용:
  cd system
  python make_full_statute.py --only statute --buckets 4      # 소규모 시험
  python make_full_statute.py --only delegation --limit 5     # 소규모 시험
  python make_full_statute.py                                 # 전량
  python make_full_statute.py --force                         # 재생성
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:  # 콘솔이 cp949 여도 한글 출력에서 죽지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from policymap import db as D                                   # noqa: E402
from policymap.config import get_config                         # noqa: E402

# 중복 구현 금지 — 아래는 전부 기존 생성기 것을 그대로 쓴다.
from make_gap_fixtures import envelope                          # noqa: E402
from make_more_fixtures import _sanitize_keys                   # noqa: E402
from make_nationwide import existing, human                     # noqa: E402
from make_graph_fixtures import Resolver, norm_name, write_shard  # noqa: E402
from make_extend_fixtures import abs_url, ymd, load_regions     # noqa: E402

KINDS = ("statute", "delegation")

# 조례 본문 링크는 mst 에서 기계적으로 유도된다(실측 199,858건 전부 일치).
ORD_URL_TEMPLATE = ("https://www.law.go.kr/DRF/lawService.do"
                    "?target=ordin&MST={mst}&type=HTML&mobileYn=")

# 법령·행정규칙 링크도 instrument_id 꼬리(+시행일)에서 유도된다
# [실측 29,811건 중 29,746건 일치, 예외 65건은 official_url 을 그대로 싣는다].
# 행마다 74~95B 짜리 URL 을 박으면 그것만 약 3MB 다.
STATUTE_URL_TEMPLATES = {
    "statute": ("https://www.law.go.kr/DRF/lawService.do"
                "?target=law&MST={key}&type=HTML&mobileYn=&efYd={effective_on_raw}"),
    "admin-rule": ("https://www.law.go.kr/DRF/lawService.do"
                   "?target=admrul&ID={key}&type=HTML&mobileYn="),
}


def derived_statute_url(iid: str, source_type: str, effective_on_raw: str | None):
    """instrument_id 꼬리에서 official_url 을 되만든다. 규칙 밖이면 None."""
    tpl = STATUTE_URL_TEMPLATES.get(source_type)
    if not tpl or ":" not in iid:
        return None
    return tpl.format(key=iid.split(":", 1)[1],
                      effective_on_raw=(effective_on_raw or ""))

# 프론트가 버킷을 직접 계산할 수 있게 index.json 에 함께 싣는 참조 구현.
FNV_JS = (
    "function fnv1a32(s){let h=0x811c9dc5;const b=new TextEncoder().encode(s);"
    "for(let i=0;i<b.length;i++){h^=b[i];h=Math.imul(h,0x01000193)>>>0;}return h>>>0;}"
    "function bucketOf(id,n){return String(fnv1a32(id)%n).padStart(2,'0');}"
)

DELEG_CAVEAT = (
    "위임 관계는 조례 본문의 인용 문구에서 추출한 것이다(source_path=citation / "
    "citation-backfill). verification_status 가 article-verified 가 아닌 행은 "
    "상위 법령의 해당 조문 존재를 확인하지 못했다. parent_id 가 'lawname:' 으로 "
    "시작하면 법령 원문이 아니라 인용된 이름만 확인된 것이며, resolved_by="
    "'name-match' 는 이름 일치에 의한 추정이다. 인용 전 원문을 확인할 것."
)

STATUTE_CAVEAT = (
    "이 버킷은 법령·행정규칙 '메타 전량'이다. 조문 본문은 담지 않는다"
    "(상세 열람은 api/statute/{slug}.json 대표본 또는 official_url 원문). "
    "national_tier 는 legal_instrument 값이 없으면 instrument_kind 기본값을 쓰며, "
    "tier_disputed=1 인 종류(조약·헌법기관규칙 등)는 서열을 단정하지 않는다."
)


# --------------------------------------------------------------------------- #
# 해시 버킷
# --------------------------------------------------------------------------- #
def fnv1a32(s: str) -> int:
    """FNV-1a 32bit. JS 로도 몇 줄이라 프론트가 같은 값을 재현할 수 있다.

    md5/sha 를 쓰면 브라우저에서 crypto.subtle 비동기 호출이 필요해진다.
    버킷 배정에는 암호학적 성질이 필요 없으므로 재현이 쉬운 쪽을 택했다.
    """
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def bucket_of(key: str, n: int) -> str:
    return f"{fnv1a32(key) % n:02d}"


# --------------------------------------------------------------------------- #
# 공통 유틸
# --------------------------------------------------------------------------- #
def compact(d: dict) -> dict:
    """None·빈문자열 키를 턴다. 3만 건 × 빈 키 5개면 그것만 수백 KB 다."""
    return {k: v for k, v in d.items() if v is not None and v != ""}


def resolve_parent(resolver: Resolver, pid: str) -> tuple[str, str | None]:
    """'lawname:*' 이름해소. 반환 (실제 instrument_id, 해소방법 or None).

    [실측] delegations.parent_id 의 'lawname:' 꼬리는 정규화돼 있지 않다. 공백과
    'ㆍ' 가 그대로 들어 있는 키가 대부분이다('lawname:행정 효율과 협업 촉진에 관한 규정'
    511행). Resolver.resolve 는 꼬리를 by_norm(정규화 키) 에 그대로 넣으므로
    30,626개 lawname 키 중 396개(1.3%)밖에 맞추지 못한다. 꼬리를 norm_name 으로
    한 번 더 정규화하면 1,733개 키 / 19,213행이 붙는다. 나머지는 조례명이거나
    수집 범위 밖 법령이라 정말로 해소되지 않는다(단정 금지 — resolved=false).
    """
    real, changed = resolver.resolve(pid)
    if changed:
        return real, "exact"
    if pid.startswith("lawname:"):
        hit = resolver.by_norm.get(norm_name(pid[len("lawname:"):]))
        if hit:
            return hit, "normalized-name"
    return pid, None


def hoist_defaults(records: list[dict], fields) -> dict:
    """값이 한쪽으로 쏠린 필드를 파일 기본값으로 올리고 레코드에서 지운다.

    [실측] verification_status 는 29,750/29,811 이 'source-linked', status 는 전부
    'active', as_of_date 는 사실상 한 날짜다. 레코드마다 이 넷을 박으면 버킷당
    약 780KB, 전체 약 3MB 를 같은 문자열로 채운다. 프론트는 rec[f] ?? defaults[f]
    로 읽는다(값이 기본값과 다른 레코드는 그대로 남아 있다).
    """
    out: dict = {}
    for f in fields:
        counts: dict = {}
        for r in records:
            v = r.get(f)
            if v is not None:
                counts[v] = counts.get(v, 0) + 1
        # 일부 레코드에만 있는 필드는 올리지 않는다. 올려 버리면 '키가 없다'가
        # '기본값이다'와 '값이 없다' 둘 다를 뜻하게 돼 복원이 불가능해진다.
        if not counts or sum(counts.values()) != len(records):
            continue
        mode = max(counts.items(), key=lambda kv: kv[1])[0]
        out[f] = mode
        for r in records:
            if r.get(f) == mode:
                del r[f]
    return out


def write_catalog(path: Path, obj: dict) -> int:
    """색인은 응답 봉투가 아니라 카탈로그다(make_nationwide index.json 과 같은 규약).

    사람이 열어보는 파일이라 indent 를 유지한다. 살균·원자적 쓰기는 동일.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(_sanitize_keys(obj), ensure_ascii=False, indent=1)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return path.stat().st_size


class Report:
    def __init__(self, args):
        self.args = args
        self.data: dict = {
            "generator": "make_full_statute.py",
            "as_of_date": time.strftime("%Y-%m-%d"),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "statute": {}, "delegation": {}, "errors": [], "warnings": [],
        }

    def fail(self, kind, key, e):
        msg = f"{type(e).__name__}: {str(e)[:160]}"
        self.data["errors"].append({"kind": kind, "key": key, "error": msg})
        print(f"  [실패] {kind}/{key} — {msg}", flush=True)

    def warn(self, kind, key, message):
        self.data["warnings"].append({"kind": kind, "key": key, "message": message})
        print(f"  [경고] {kind}/{key} — {message}", flush=True)


# --------------------------------------------------------------------------- #
# 1) 법령 전량 메타 버킷
# --------------------------------------------------------------------------- #
def delegation_counts(conn) -> tuple[dict, dict]:
    """parent_id → (위임행수, 근거조례수). parent_id 그대로의 집계다.

    'lawname:*' 은 별개 키로 남는다(전량 305,054 statute + 113,848 lawname +
    2,725 admrul [실측]). 이름해소로 실제 법령에 얹는 몫은 호출부에서 따로 더한다 —
    합쳐 버리면 '원문 확인된 근거'와 '이름 추정 근거'가 구분되지 않는다.
    """
    rows = D.fetchall(conn, """
        SELECT parent_id, COUNT(*) n, COUNT(DISTINCT child_id) c
          FROM delegations GROUP BY parent_id""")
    return ({r["parent_id"]: r["n"] for r in rows},
            {r["parent_id"]: r["c"] for r in rows})


def build_statute(conn, out: Path, args, report: Report) -> None:
    t0 = time.time()
    n_bucket = args.buckets
    print(f"[법령전량] legal_instrument 적재 중… (버킷 {n_bucket}개)", flush=True)

    # 버킷 수가 바뀌면 옛 배정으로 만든 파일이 그대로 남아 index 와 어긋난다
    # (48버킷으로 만든 뒤 32버킷으로 다시 돌리면 32~47번 파일이 유령으로 남는다).
    ipath = out / "statute" / "index.json"
    if ipath.exists():
        try:
            prev = json.loads(ipath.read_text(encoding="utf-8")).get("bucket_count")
        except Exception:  # noqa: BLE001
            prev = None
        if prev and prev != n_bucket:
            stale = [p for d in ("all", "names")
                     for p in (out / "statute" / d).glob("*.json")]
            for p in stale:
                p.unlink()
            report.warn("statute", "bucket-count",
                        f"버킷 수가 {prev} → {n_bucket} 로 바뀌어 옛 shard {len(stale)}개를 "
                        f"지우고 다시 만든다(옛 배정 파일이 남으면 index 와 어긋난다)")

    # 이름해소기 재사용 — tier 기본값 COALESCE 까지 이미 들어 있다.
    resolver = Resolver(conn)
    rows_all = D.fetchall(conn, """
        SELECT li.instrument_id, li.kind, li.source_type,
               COALESCE(li.national_tier, ik.national_tier)  AS tier,
               COALESCE(li.tier_disputed, ik.tier_disputed, 0) AS tier_disputed,
               li.mst, li.law_id, li.admrul_serial, li.admrul_knd,
               li.name, li.short_name, li.competent_authority, li.generality,
               li.enacted_on, li.promulgation_no, li.effective_on, li.repealed_on,
               li.rr_cls_cd, li.current_history, li.official_url,
               li.status, li.verification_status, li.as_of_date
          FROM legal_instrument li
          LEFT JOIN instrument_kind ik ON ik.kind = li.kind
         ORDER BY li.instrument_id""")
    print(f"  법령·행정규칙 {len(rows_all):,}건 · 이름해소 사전 {len(resolver.by_norm):,}개"
          f" · {time.time() - t0:.1f}s", flush=True)

    dl_rows, dl_kids = delegation_counts(conn)

    # 이름해소로 실제 법령에 얹히는 몫(추정)을 따로 모은다.
    by_name_rows: dict[str, int] = {}
    by_name_kids: dict[str, int] = {}
    unresolved_names = 0
    lawname_keys = lawname_rows = unresolved_rows = 0
    for pid, n in dl_rows.items():
        if not pid.startswith("lawname:"):
            continue
        lawname_keys += 1
        lawname_rows += n
        real, how = resolve_parent(resolver, pid)
        if not how:
            unresolved_names += 1
            unresolved_rows += n
            continue
        by_name_rows[real] = by_name_rows.get(real, 0) + n
        by_name_kids[real] = by_name_kids.get(real, 0) + dl_kids.get(pid, 0)
    print(f"  위임 parent 키 {len(dl_rows):,}개 · 그중 lawname {lawname_keys:,}개"
          f"({lawname_rows:,}행) · 이름해소 실패 {unresolved_names:,}개"
          f"({unresolved_rows:,}행)", flush=True)

    # ---- 레코드 만들기 + 버킷 배정 ----
    buckets: dict[str, list] = {f"{i:02d}": [] for i in range(n_bucket)}
    names: dict[str, dict] = {f"{i:02d}": {} for i in range(n_bucket)}
    name_multi = 0
    kinds: dict[str, int] = {}
    tiers: dict[str, int] = {}
    src_types: dict[str, int] = {}
    with_deleg = 0
    url_exceptions = 0
    norm_index: dict[str, list[str]] = {}

    for r in rows_all:
        iid = r["instrument_id"]
        bkey = bucket_of(iid, n_bucket)
        rows_n = dl_rows.get(iid, 0)
        kids_n = dl_kids.get(iid, 0)
        nm_rows = by_name_rows.get(iid, 0)
        nm_kids = by_name_kids.get(iid, 0)
        if rows_n or nm_rows:
            with_deleg += 1
        have = abs_url(r["official_url"])
        derived = derived_statute_url(iid, r["source_type"], r["effective_on"])
        if derived != have:
            url_exceptions += 1
        rec = compact({
            "id": iid,
            "name": r["name"],
            "short_name": r["short_name"],
            "kind": r["kind"],
            "source_type": r["source_type"],
            "tier": r["tier"],
            "authority": r["competent_authority"],
            "admrul_knd": r["admrul_knd"],
            "generality": r["generality"],
            "enacted_on": ymd(r["enacted_on"]),
            "promulgation_no": r["promulgation_no"],
            "effective_on": ymd(r["effective_on"]),
            "repealed_on": ymd(r["repealed_on"]),
            "rr_cls_cd": r["rr_cls_cd"],
            "current_history": r["current_history"],
            "status": r["status"],
            "verification_status": r["verification_status"],
            "as_of_date": r["as_of_date"],
            # official_url 은 규칙에서 유도되는 것(29,746건)은 싣지 않는다.
            # 프론트는 url_templates 로 되만들고, 이 키가 있는 예외만 그대로 쓴다.
            "official_url": (None if derived == have else have),
            # 근거 조례: 원문 id 로 직접 걸린 것과 이름 추정으로 걸린 것을 나눈다.
            "deleg_rows": rows_n or None,
            "child_ordinances": kids_n or None,
            "deleg_rows_by_name": nm_rows or None,
            "child_ordinances_by_name": nm_kids or None,
        })
        if r["tier_disputed"]:
            # 서열 학설대립 — 단정 금지 배지.
            rec["tier_disputed"] = 1
        if nm_rows:
            rec["by_name_estimated"] = True
        if r["repealed_on"] or r["current_history"] == "폐지":
            rec["repealed"] = True
        buckets[bkey].append(rec)

        kinds[r["kind"] or "미상"] = kinds.get(r["kind"] or "미상", 0) + 1
        tk = str(r["tier"]) if r["tier"] is not None else "미상"
        tiers[tk] = tiers.get(tk, 0) + 1
        st = r["source_type"] or "미상"
        src_types[st] = src_types.get(st, 0) + 1

        k = norm_name(r["name"])
        if k:
            norm_index.setdefault(k, []).append(iid)

    # ---- 이름 → [instrument_id, 버킷] 매핑 ----
    for k, ids in norm_index.items():
        primary = resolver.by_norm.get(k) or ids[0]
        entry = [primary, bucket_of(primary, n_bucket)]
        if len(ids) > 1:
            name_multi += 1
            entry.append([i for i in ids if i != primary])  # 연혁본 등 동명이본
        names[bucket_of(k, n_bucket)][k] = entry

    # 쏠린 필드는 파일 기본값으로 올린다(버킷마다 같은 값이 나오게 전체에서 한 번 계산).
    hoist_fields = ("status", "verification_status", "as_of_date", "source_type")
    rec_defaults = hoist_defaults(
        [r for b in buckets.values() for r in b], hoist_fields)

    # ---- 쓰기 ----
    bucket_cat, name_cat = [], []
    total_bytes = 0
    for i in range(n_bucket):
        bkey = f"{i:02d}"
        recs = buckets[bkey]
        rel = f"statute/all/{bkey}.json"
        path = out / rel
        size = None if args.force else existing(path)
        reused = size is not None
        if not reused:
            try:
                size = write_shard(path, envelope({
                    "bucket": bkey, "bucket_count": n_bucket,
                    "hash": {"algorithm": "fnv1a32", "modulo": n_bucket,
                             "input": "instrument_id"},
                    "defaults": rec_defaults,
                    "url_templates": STATUTE_URL_TEMPLATES,
                    "url_note": "official_url 키가 없으면 source_type 별 url_templates 로 "
                                "만든다. {key}=instrument_id 의 ':' 뒤, "
                                "{effective_on_raw}=시행일 YYYYMMDD "
                                "(레코드 effective_on 에서 '-' 를 뺀 값; 없으면 빈 문자열). "
                                "규칙 밖 예외만 official_url 을 직접 싣는다.",
                    "instruments": recs,
                    "count": len(recs),
                    "caveat": STATUTE_CAVEAT,
                }, kind="statute-all"))
            except Exception as e:  # noqa: BLE001
                report.fail("statute", rel, e)
                continue
        bucket_cat.append({"key": bkey, "path": rel, "bytes": size,
                           "instruments": len(recs), "reused": reused})
        total_bytes += size

        nrel = f"statute/names/{bkey}.json"
        npath = out / nrel
        nsize = None if args.force else existing(npath)
        nreused = nsize is not None
        if not nreused:
            try:
                nsize = write_shard(npath, envelope({
                    "bucket": bkey, "bucket_count": n_bucket,
                    "hash": {"algorithm": "fnv1a32", "modulo": n_bucket,
                             "input": "정규화 법령명(norm_name)"},
                    "names": names[bkey],
                    "count": len(names[bkey]),
                    "value_format": "[instrument_id, all_bucket_key, (동명이본 id 목록)]",
                }, kind="statute-names"))
            except Exception as e:  # noqa: BLE001
                report.fail("statute", nrel, e)
                continue
        name_cat.append({"key": bkey, "path": nrel, "bytes": nsize,
                         "names": len(names[bkey]), "reused": nreused})
        total_bytes += nsize
        if (i + 1) % 8 == 0 or i + 1 == n_bucket:
            print(f"  [{i + 1}/{n_bucket}] 버킷 {bkey} · 법령 {len(recs)}건 "
                  f"{human(size)} · 이름 {len(names[bkey])}개 {human(nsize)}", flush=True)

    sizes = [b["bytes"] for b in bucket_cat]
    if sizes:
        lo, hi = min(sizes), max(sizes)
        if hi > 300 * 1024:
            report.warn("statute", "bucket-size",
                        f"가장 큰 버킷 {human(hi)} — 목표 100~300KB 초과. --buckets 를 늘려라")
        if lo < 50 * 1024 and n_bucket > 30:
            report.warn("statute", "bucket-size",
                        f"가장 작은 버킷 {human(lo)} — 파일이 잘게 쪼개졌다. --buckets 를 줄여라")

    idx = {
        "generator": "make_full_statute.py",
        "as_of_date": time.strftime("%Y-%m-%d"),
        "bucket_count": n_bucket,
        "hash": {
            "algorithm": "fnv1a32",
            "modulo": n_bucket,
            "input": "instrument_id (UTF-8 바이트)",
            "bucket_key": "(fnv1a32(id) % modulo) 를 2자리 0패딩한 문자열",
            "js": FNV_JS,
        },
        "buckets": bucket_cat,
        "record_defaults": rec_defaults,
        "url_templates": STATUTE_URL_TEMPLATES,
        "url_exceptions": url_exceptions,
        "names": {
            "path": "statute/names/{bucket}.json",
            "hash_input": "정규화 법령명 — 한글·영숫자 외 문자 제거(norm_name)",
            "value_format": "[instrument_id, all_bucket_key, (동명이본 id 목록)]",
            "note": "이름 맵 전체를 index.json 에 인라인하면 index 가 2MB 를 넘겨 "
                    "모든 화면이 그것부터 받는다. 그래서 같은 해시로 버킷에 쪼갰다. "
                    "이름 조회 = names 버킷 1회 + all 버킷 1회.",
            "shards": name_cat,
            "distinct_names": sum(x["names"] for x in name_cat),
            "names_with_duplicates": name_multi,
        },
        "totals": {
            "instruments": len(rows_all),
            "instruments_with_delegation": with_deleg,
            "delegation_rows": D.fetchone(
                conn, "SELECT COUNT(*) n FROM delegations")["n"],
            "delegation_parent_keys": len(dl_rows),
            "delegation_parent_keys_lawname": lawname_keys,
            "delegation_rows_lawname": lawname_rows,
            "delegation_parent_keys_unresolved_lawname": unresolved_names,
            "delegation_rows_unresolved_lawname": unresolved_rows,
            "bytes": total_bytes,
            "files": len(bucket_cat) + len(name_cat),
        },
        "by_kind": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "by_tier": dict(sorted(tiers.items())),
        "by_source_type": dict(sorted(src_types.items(), key=lambda kv: -kv[1])),
        "detail_shards": {
            "path": "statute/{slug}.json",
            "note": "위임 참조 상위 200건의 조문 본문 포함 상세본. "
                    "make_extend_fixtures.py 가 만든다. 여기서는 건드리지 않는다.",
            "catalog": "statute_index.json",
        },
        "articles_note": "조문 본문 2,365,068건(약 490MB)은 깃허브 한계로 배포본에 "
                         "싣지 않는다. 로컬 완전판(DB 직결 API)에서만 제공한다.",
        "caveat": STATUTE_CAVEAT,
    }
    if args.inline_name_map:
        idx["names"]["inline"] = {k: v for b in names.values() for k, v in b.items()}

    isize = write_catalog(out / "statute" / "index.json", idx)
    total_bytes += isize
    report.data["statute"] = {
        "buckets": len(bucket_cat), "name_shards": len(name_cat),
        "instruments": len(rows_all), "bytes": total_bytes,
        "index_bytes": isize, "seconds": round(time.time() - t0, 1),
        "bucket_bytes_min": min(sizes) if sizes else 0,
        "bucket_bytes_max": max(sizes) if sizes else 0,
    }
    print(f"  법령 {len(rows_all):,}건 → {len(bucket_cat)}버킷 + 이름 {len(name_cat)}버킷"
          f" · 합계 {human(total_bytes)} (index {human(isize)})"
          f" · {time.time() - t0:.1f}s", flush=True)


# --------------------------------------------------------------------------- #
# 2) 지역별 위임관계 전량
# --------------------------------------------------------------------------- #
def region_rollup(conn) -> tuple[dict, dict, list]:
    """조례의 region_id → 243개 대상 sig_cd 로 굴린다.

    조례 199,858건 중 10,045건이 243개 밖에 붙어 있다 [실측]:
    교육청(edu:11 등 lvl4) 5,671건 · 통합·폐지 지자체(광주 29, 창원 48110 등) 4,314건 ·
    광역연합(org:충청광역연합) 60건. 그냥 버리면 '전량'이 아니게 되므로
    부모지역 → 승계지역 순으로 굴리고, 그래도 안 붙는 것만 _unassigned 로 모은다.

    반환: (region_id → sig_cd, sig_cd → [region_id...], 대상 지역 행 목록)
    """
    targets = D.fetchall(conn, """
        SELECT sig_cd, region_id, name, full_name, level FROM regions
         WHERE status='active' AND has_legislation=1 AND level IN (1,2)
           AND sig_cd IS NOT NULL
         ORDER BY level, sig_cd""")
    tgt_by_rid = {r["region_id"]: r["sig_cd"] for r in targets}
    tgt_sigs = {r["sig_cd"] for r in targets}
    allr = load_regions(conn)  # region_id → 지역 메타(기존 생성기 것 재사용)
    succ = {r["old_region_id"]: r["new_region_id"] for r in D.fetchall(
        conn, "SELECT old_region_id, new_region_id FROM region_succession")}

    def resolve(rid: str) -> str | None:
        seen = set()
        cur = rid
        for _ in range(6):
            if cur is None or cur in seen:
                break
            seen.add(cur)
            if cur in tgt_by_rid:
                return tgt_by_rid[cur]
            row = allr.get(cur) or {}
            sig = row.get("sig_cd")
            if sig in tgt_sigs:
                return sig
            cur = row.get("parent_region") or succ.get(cur)
        return None

    rid2sig: dict[str, str] = {}
    sig2rids: dict[str, list[str]] = {}
    for r in D.fetchall(conn, "SELECT DISTINCT region_id FROM ordinances"):
        rid = r["region_id"]
        sig = resolve(rid) or "_unassigned"
        rid2sig[rid] = sig
        sig2rids.setdefault(sig, []).append(rid)
    return rid2sig, sig2rids, targets


def encode_fields(records: list[dict], fields) -> dict:
    """반복되는 짧은 문자열 필드를 코드표 인덱스로 바꾼다(rows 의 codes 와 같은 규약).

    hoist_defaults 를 쓸 수 없는 자리에 쓴다. 예: parents 의 verification_status 는
    resolved=true 인 항목에만 있다. 기본값으로 올려 버리면 '법령 원문 미수집(=값 없음)'
    인 항목까지 'source-linked' 로 읽히게 된다 — 단정 금지 규율 위반이다.
    코드표는 키가 없는 항목을 그대로 없는 채로 둔다.
    [실측 11000] parents 의 이 필드들만 72KB(파일의 17%)를 같은 문자열로 채우고 있었다.
    """
    out: dict = {}
    for f in fields:
        table: list = []
        idx: dict = {}
        used = False
        for r in records:
            if f not in r:
                continue
            used = True
            v = r[f]
            if v not in idx:
                idx[v] = len(table)
                table.append(v)
            r[f] = idx[v]
        if used:
            out[f] = table
    return out


def _codes(values: list) -> tuple[list, dict]:
    """등장 순 코드표. 값이 하나뿐이면 호출부가 defaults 로 올린다."""
    table: list = []
    idx: dict = {}
    for v in values:
        if v not in idx:
            idx[v] = len(table)
            table.append(v)
    return table, idx


def delegation_file(conn, resolver: Resolver, sig: str, rids: list[str],
                    regmeta: dict, args) -> dict:
    """한 지역의 위임관계 전량을 컬럼 배열 봉투로 만든다."""
    ph = ",".join("?" * len(rids))
    cols = ("d.child_id, d.child_article, d.parent_id, d.parent_article, "
            "d.relation, d.delegation_type, d.source_path, d.verification_status, "
            "d.inferred, d.tier_gap")
    if args.with_citation:
        cols += ", d.citation_text"
    rows = D.fetchall(conn, f"""
        SELECT {cols},
               o.region_id, o.mst AS o_mst, o.name AS o_name, o.org_name AS o_org,
               o.ord_kind AS o_kind, o.repealed_on AS o_repealed, o.status AS o_status
          FROM ordinances o
          JOIN delegations d ON d.child_id = o.ordinance_id
         WHERE o.region_id IN ({ph})
         ORDER BY d.child_id, d.child_article, d.parent_id""", tuple(rids))

    children: list[dict] = []
    child_idx: dict[str, int] = {}
    parents: list[dict] = []
    parent_idx: dict[str, int] = {}
    unresolved = 0
    name_matched = 0

    def child_ref(r) -> int:
        cid = r["child_id"]
        i = child_idx.get(cid)
        if i is not None:
            return i
        i = len(children)
        child_idx[cid] = i
        # mst 는 싣지 않는다 — id('ordin:{mst}') 의 ':' 뒤가 곧 mst 다(실측 전건 일치).
        children.append(compact({
            "id": cid, "name": r["o_name"],
            "org_name": r["o_org"], "ord_kind": r["o_kind"],
            "region_id": r["region_id"],
            # 폐지 경고 규율 — 폐지 조례를 선례처럼 보이게 두지 않는다.
            "repealed": True if (r["o_repealed"] or "").strip() else None,
            "repealed_on": ymd(r["o_repealed"]),
            "status": r["o_status"] if r["o_status"] != "active" else None,
        }))
        return i

    def parent_ref(pid: str) -> int:
        nonlocal unresolved, name_matched
        i = parent_idx.get(pid)
        if i is not None:
            return i
        i = len(parents)
        parent_idx[pid] = i
        real, how = resolve_parent(resolver, pid)
        meta = resolver.instruments.get(real)
        if meta:
            # bucket 은 싣지 않는다 — statute/index.json 의 fnv1a32 로
            # bucketOf(resolved_id || id) 를 계산하면 된다(행당 9B × 42만행 절약).
            p = compact({
                "id": pid, "name": meta.get("name"), "short_name": meta.get("short_name"),
                "kind": meta.get("kind"), "source_type": meta.get("source_type"),
                "tier": meta.get("tier"),
                "current_history": meta.get("current_history"),
                "repealed_on": ymd(meta.get("repealed_on")),
                "verification_status": meta.get("verification_status"),
            })
            p["resolved"] = True
            if meta.get("tier_disputed"):
                p["tier_disputed"] = 1
            if how:  # 추정 배지 — 이름 일치로 얹은 것이지 원문 확인이 아니다.
                name_matched += 1
                p["resolved_id"] = real
                p["resolved_by"] = "name-match" if how == "exact" else f"name-match/{how}"
            if meta.get("repealed_on") or meta.get("status") == "repealed":
                p["repealed"] = True
        else:
            unresolved += 1
            # note 는 파일 단위 unresolved_note 로 한 번만 싣는다(같은 문장 × 수백 건).
            p = {"id": pid, "resolved": False,
                 "name": pid[len("lawname:"):] if pid.startswith("lawname:") else pid}
        parents.append(p)
        return i

    # child/parent 를 뺀 모든 컬럼은 코드표를 만든다. 값이 하나뿐이면 defaults 로
    # 올리고 컬럼 자체를 지운다. [실측 11000] child_article 은 4,634행에 149종,
    # parent_article 은 244종, tier_gap 은 전건 NULL 이다. 문자열을 행마다 박는 대신
    # 코드표 인덱스를 쓰면 행 하나가 35B → 약 20B 로 준다.
    candidates = ["child_article", "parent_article", "relation", "delegation_type",
                  "source_path", "verification_status", "inferred", "tier_gap"]
    if args.with_citation:
        candidates.append("citation_text")
    defaults: dict = {"child_kind": "ordinance", "parent_kind": "instrument"}
    codes: dict = {}
    coded_fields: list[str] = []
    for f in candidates:
        table, _ = _codes([r[f] for r in rows])
        if len(table) <= 1:
            defaults[f] = table[0] if table else None
        else:
            codes[f] = table
            coded_fields.append(f)
    code_idx = {f: {v: i for i, v in enumerate(codes[f])} for f in coded_fields}

    columns = ["child", "parent"] + coded_fields

    out_rows = []
    for r in rows:
        row = [child_ref(r), parent_ref(r["parent_id"])]
        row += [code_idx[f][r[f]] for f in coded_fields]
        out_rows.append(row)

    # 조례·상위법 사전에서도 쏠린 필드를 파일 기본값으로 올린다.
    child_defaults = hoist_defaults(children, ("region_id", "org_name", "ord_kind"))
    parent_defaults = hoist_defaults(parents, ("resolved",))
    # resolved=false 항목에는 없는 필드라 기본값으로 올릴 수 없다 → 코드표.
    parent_codes = encode_fields(parents, ("kind", "source_type", "verification_status",
                                           "current_history", "resolved_by"))
    child_codes = encode_fields(children, ("ord_kind", "region_id", "org_name", "status"))

    def dist(field: str) -> dict:
        if field in defaults:
            return {str(defaults[field]): len(rows)}
        c: dict = {}
        for r in rows:
            k = str(r[field])
            c[k] = c.get(k, 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    meta = regmeta.get(sig) or {}
    return envelope({
        "sig_cd": sig,
        "region": compact({"sig_cd": sig, "name": meta.get("full_name") or meta.get("name"),
                           "level": meta.get("level"), "region_ids": sorted(rids)}),
        "format": "columnar",
        "columns": columns,
        "defaults": defaults,
        "codes": codes,
        "read_note": "rows[i][j] 는 columns[j] 필드다. child/parent 는 children/parents "
                     "배열의 인덱스, 나머지는 codes[필드] 배열의 인덱스다. columns 에 "
                     "없는 필드는 defaults 에서 읽는다. children/parents 원소에 없는 "
                     "키는 child_defaults/parent_defaults 에서 읽되, "
                     "child_codes/parent_codes 에 있는 필드의 값은 그 배열의 인덱스다"
                     "(키가 아예 없으면 값이 없는 것이다 — 기본값을 씌우지 말 것).",
        "child_defaults": child_defaults,
        "child_codes": child_codes,
        "parent_defaults": parent_defaults,
        "parent_codes": parent_codes,
        "url_template": {
            "ordinance": ORD_URL_TEMPLATE,
            "note": "children[].id 의 ':' 뒤(=mst)를 {mst} 에 넣으면 조례 원문 링크가 "
                    "된다(실측 199,858건 전부 이 패턴).",
        },
        "parent_bucket": {
            "index": "statute/index.json",
            "note": "상위법 상세는 statute/all/{bucket}.json 에 있다. bucket 은 "
                    "statute/index.json 의 fnv1a32 로 "
                    "(parents[].resolved_id || parents[].id) 를 해싱해 구한다. "
                    "resolved=false 인 상위법은 법령 원문이 없어 버킷도 없다.",
        },
        "unresolved_note": "인용문에서 이름만 확인됨 — 법령 원문 미수집"
                           "(tier·폐지여부 미상). parents[].resolved=false 인 항목.",
        "children": children,
        "parents": parents,
        "rows": out_rows,
        "stats": {
            "rows": len(out_rows),
            "child_ordinances": len(children),
            "parents": len(parents),
            "parents_unresolved": unresolved,
            "parents_name_matched": name_matched,
            "inferred_rows": sum(1 for r in rows if r["inferred"]),
            "by_verification_status": dist("verification_status"),
            "by_source_path": dist("source_path"),
            "by_delegation_type": dist("delegation_type"),
            "repealed_children": sum(1 for c in children if c.get("repealed")),
        },
        "caveat": DELEG_CAVEAT,
    }, kind="delegation", sig_cd=sig)


def build_delegation(conn, out: Path, args, report: Report) -> None:
    t0 = time.time()
    print("[위임전량] 지역 매핑 계산 중…", flush=True)
    rid2sig, sig2rids, targets = region_rollup(conn)
    regmeta = {r["sig_cd"]: r for r in targets}
    total_rows_db = D.fetchone(conn, "SELECT COUNT(*) n FROM delegations")["n"]

    sigs = [r["sig_cd"] for r in targets]
    extra = [s for s in sig2rids if s not in regmeta]  # _unassigned 등
    order = [s for s in sigs if s in sig2rids] + sorted(extra)
    if args.regions:
        want = {s.strip() for s in args.regions.split(",") if s.strip()}
        order = [s for s in order if s in want]
    if args.limit:
        order = order[: args.limit]

    unmapped = [rid for rid, s in rid2sig.items() if s == "_unassigned"]
    if unmapped:
        report.warn("delegation", "_unassigned",
                    f"243개 지역으로 굴리지 못한 조례 region_id {len(unmapped)}개 "
                    f"→ delegation/_unassigned.json 으로 모았다: {sorted(unmapped)[:8]}")

    resolver = Resolver(conn)
    print(f"  대상 {len(order)}개 파일 · DB 위임행 {total_rows_db:,}건", flush=True)

    cat = []
    total_bytes = 0
    covered_rows = 0
    for i, sig in enumerate(order, 1):
        rel = f"delegation/{sig}.json"
        path = out / rel
        size = None if args.force else existing(path)
        if size is not None:
            try:
                d = (json.loads(path.read_text(encoding="utf-8")).get("data") or {})
                st = d.get("stats") or {}
            except Exception:  # noqa: BLE001
                st = {}
            cat.append({"sig_cd": sig, "path": rel, "bytes": size, "reused": True,
                        "name": (d.get("region") or {}).get("name") if st else None,
                        "rows": st.get("rows"), "child_ordinances": st.get("child_ordinances"),
                        "parents": st.get("parents")})
            covered_rows += st.get("rows") or 0
            total_bytes += size
            if i <= 3 or i % 40 == 0 or i == len(order):
                print(f"  [{i}/{len(order)}] {sig} · skip {human(size)}", flush=True)
            continue
        try:
            env = delegation_file(conn, resolver, sig, sig2rids[sig], regmeta, args)
            size = write_shard(path, env)
        except Exception as e:  # noqa: BLE001
            report.fail("delegation", sig, e)
            continue
        st = env["data"]["stats"]
        covered_rows += st["rows"]
        total_bytes += size
        cat.append({"sig_cd": sig, "path": rel, "bytes": size, "reused": False,
                    "name": (env["data"]["region"] or {}).get("name"),
                    "rows": st["rows"], "child_ordinances": st["child_ordinances"],
                    "parents": st["parents"]})
        if size > 100 * 1024 * 1024:
            report.warn("delegation", sig, f"{human(size)} — 파일당 100MB 한계 초과")
        done = time.time() - t0
        if i <= 3 or i % 20 == 0 or i == len(order):
            eta = done / i * (len(order) - i)
            print(f"  [{i}/{len(order)}] {sig} {cat[-1]['name'] or ''} · "
                  f"위임 {st['rows']:,}행 / 조례 {st['child_ordinances']:,} / "
                  f"상위법 {st['parents']:,} · {human(size)} · "
                  f"{done:.0f}s 경과 / ETA {eta:.0f}s", flush=True)

    partial = bool(args.limit or args.regions)
    if not partial and covered_rows != total_rows_db:
        report.warn("delegation", "coverage",
                    f"파일 합계 {covered_rows:,}행 ≠ DB {total_rows_db:,}행 "
                    f"(차 {total_rows_db - covered_rows:,}) — 전량이 아니다")

    sizes = [c["bytes"] for c in cat]
    idx = {
        "generator": "make_full_statute.py",
        "as_of_date": time.strftime("%Y-%m-%d"),
        "path": "delegation/{sig_cd}.json",
        "format": "columnar — 파일의 columns/defaults/codes/children/parents 로 해석한다",
        "regions": cat,
        "totals": {
            "files": len(cat), "bytes": total_bytes,
            "rows_in_files": covered_rows, "rows_in_db": total_rows_db,
            "complete": (not partial) and covered_rows == total_rows_db,
            "bytes_max": max(sizes) if sizes else 0,
            "bytes_min": min(sizes) if sizes else 0,
        },
        "unmapped_region_ids": sorted(unmapped),
        "statute_index": "statute/index.json",
        "caveat": DELEG_CAVEAT,
    }
    # 부분 실행(--limit/--regions)은 카탈로그를 덮지 않는다. 덮으면 3곳짜리 시험
    # 한 번에 243곳짜리 색인이 3줄로 줄어든다 [실측: --limit 3 재개 시험에서 발생].
    ipath = out / "delegation" / "index.json"
    kept = partial and ipath.exists()
    if kept:
        print(f"  [보존] 부분 실행이라 {ipath.name} 을 덮지 않았다 "
              f"(기존 {human(ipath.stat().st_size)} 유지). 전량 실행 시 갱신된다.",
              flush=True)
        isize = 0
    else:
        isize = write_catalog(ipath, idx)
        total_bytes += isize
    report.data["delegation"] = {
        "files": len(cat), "bytes": total_bytes, "index_bytes": isize,
        "rows_in_files": covered_rows, "rows_in_db": total_rows_db,
        "complete": idx["totals"]["complete"],
        "bytes_max": idx["totals"]["bytes_max"],
        "seconds": round(time.time() - t0, 1),
    }
    print(f"  위임 {covered_rows:,}/{total_rows_db:,}행 → {len(cat)}파일 "
          f"· 합계 {human(total_bytes)} · 최대 {human(idx['totals']['bytes_max'])}"
          f" · {time.time() - t0:.1f}s", flush=True)


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="법령 전량 + 위임관계 전량 정적 shard 생성")
    ap.add_argument("--out", default=str(ROOT / "data"), help="출력 루트(기본 system/data)")
    ap.add_argument("--only", default=",".join(KINDS),
                    help=f"생성 종류 쉼표 구분 {KINDS}")
    ap.add_argument("--buckets", type=int, default=32,
                    help="법령 버킷 수(기본 32 — 실측 all/ 4.8MB → 파일당 약 150KB, "
                         "names/ 2.5MB → 약 78KB. 목표 30~60개·100~300KB)")
    ap.add_argument("--limit", type=int, default=0, help="지역 파일 수 상한(시험용)")
    ap.add_argument("--regions", default=None, help="sig_cd 직접 지정(쉼표)")
    ap.add_argument("--with-citation", action="store_true",
                    help="위임행에 낫표 인용 원문(citation_text)까지 싣는다(용량 +약 10MB)")
    ap.add_argument("--inline-name-map", action="store_true",
                    help="이름→버킷 맵 전체를 statute/index.json 에 인라인(약 +2MB)")
    ap.add_argument("--force", action="store_true", help="기존 파일 무시하고 재생성")
    ap.add_argument("--report", default=None,
                    help="실행 리포트 JSON 경로(기본 system/scratchpad/full_statute_run.json)")
    a = ap.parse_args()
    a.only = {s.strip() for s in a.only.split(",") if s.strip()}
    bad = a.only - set(KINDS)
    if bad:
        print(f"[오류] 알 수 없는 --only: {sorted(bad)} (가능: {KINDS})", flush=True)
        return 2
    if not (1 <= a.buckets <= 512):
        print("[오류] --buckets 는 1~512", flush=True)
        return 2

    t0 = time.time()
    out = Path(a.out) / "api"
    out.mkdir(parents=True, exist_ok=True)
    cfg = get_config()
    conn = D.connect()
    report = Report(a)
    print(f"DB={cfg.db_path}", flush=True)
    print(f"출력={out}  생성={sorted(a.only)}  버킷={a.buckets}  force={a.force}",
          flush=True)
    print("─" * 70, flush=True)

    if "statute" in a.only:
        build_statute(conn, out, a, report)
    if "delegation" in a.only:
        build_delegation(conn, out, a, report)

    total = (report.data["statute"].get("bytes", 0)
             + report.data["delegation"].get("bytes", 0))
    report.data["totals"] = {
        "bytes": total, "human": human(total),
        "seconds": round(time.time() - t0, 1),
        "errors": len(report.data["errors"]),
        "warnings": len(report.data["warnings"]),
    }
    rp = Path(a.report) if a.report else (ROOT / "scratchpad" / "full_statute_run.json")
    write_catalog(rp, report.data)

    print("─" * 70, flush=True)
    if report.data["statute"]:
        s = report.data["statute"]
        print(f"  statute     {s['buckets'] + s['name_shards'] + 1:>4} 파일  "
              f"{human(s['bytes'])}  (법령 {s['instruments']:,}건, "
              f"버킷 {human(s['bucket_bytes_min'])}~{human(s['bucket_bytes_max'])})",
              flush=True)
    if report.data["delegation"]:
        d = report.data["delegation"]
        print(f"  delegation  {d['files'] + 1:>4} 파일  {human(d['bytes'])}  "
              f"(위임 {d['rows_in_files']:,}/{d['rows_in_db']:,}행, "
              f"complete={d['complete']}, 최대 {human(d['bytes_max'])})", flush=True)
    print(f"  합계 {human(total)} · {report.data['totals']['seconds']}s "
          f"· 경고 {len(report.data['warnings'])} · 실패 {len(report.data['errors'])}",
          flush=True)
    print(f"  리포트 {rp}", flush=True)
    for e in report.data["errors"][:10]:
        print(f"   ! {e['kind']}/{e['key']}: {e['error']}", flush=True)
    return 1 if report.data["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
