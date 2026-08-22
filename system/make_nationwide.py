#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""실 DB → **전국 전체** 시각화 shard 생성 (243개 지자체 + 정책·의안·질의).

기존 두 생성기는 대표 5곳만 담은 단일 파일을 만들었다.

    make_gap_fixtures.py   → api/gap.json, api/peers.json   (data = {sig_cd: 결과} 맵, 5곳)
    make_more_fixtures.py  → api/diffusion|effectiveness|votes|search.json (단일 객체 1건)

이 스크립트는 같은 엔진·같은 응답 봉투를 쓰되 결과를 **지역별 shard** 로 쪼갠다.
regions/{sig_cd}.json 과 같은 패턴이라 뷰가 필요한 지역만 골라 받는다.

    api/gap/{sig_cd}.json            격차분석      (약 47KB)
    api/peers/{sig_cd}.json          유사지자체    (약 7KB)
    api/effectiveness/{sig_cd}.json  조례-예산 실효성
    api/diffusion/{slug}.json        정책 확산 (템플릿별)
    api/votes/{bill_no}.json         의안 표결 (표결수 상위)
    api/search/{slug}.json           대표 질의 사전계산
    api/index.json                   커버 지역 목록 + 파일 경로·크기·경고

스키마 동일성(중요):
  - gap/peers shard 의 data 는 기존 gap.json 과 똑같이 {sig_cd: 결과} 맵이다(원소 1개).
    뷰가 집계본이든 shard 든 같은 코드로 다룬다.
  - effectiveness/diffusion/votes/search 는 기존과 같은 '단일 객체' 봉투다.

엔진 재사용:
  - 격차·유사: analytics.peers.recommend_ordinances / find_similar_governments
  - 나머지  : mcp_server.server.Server 의 _tool_* (make_more_fixtures 와 동일 경로)
  - 봉투     : make_gap_fixtures.envelope
  - 키 살균  : make_more_fixtures._sanitize_keys  (RAG 인덱스가 살균 전 URL 을 갖고 있어 필수)

재개 가능: 이미 만들어진 shard 는 건너뛴다(--force 로 강제 재생성). 원자적 쓰기라
중간에 끊겨도 반쪽 파일이 남지 않는다. 한 지역이 실패해도 나머지는 계속 만든다.

사용:
  cd system
  python make_nationwide.py --limit 5            # 소규모 검증
  python make_nationwide.py                      # 전국 243곳
  python make_nationwide.py --only gap,peers --force
  python make_nationwide.py --level 2 --votes-top 20
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

from policymap import db as D                              # noqa: E402
from policymap.analytics import base as B                  # noqa: E402
from policymap.analytics import peers as P                 # noqa: E402
from policymap.config import get_config                    # noqa: E402
from policymap.mcp_server.server import Server             # noqa: E402

# 중복 구현 금지 — 봉투와 키 살균은 기존 생성기 것을 그대로 쓴다.
from make_gap_fixtures import envelope                     # noqa: E402
from make_more_fixtures import _sanitize_keys              # noqa: E402

# --------------------------------------------------------------------------- #
# 기본 목록
# --------------------------------------------------------------------------- #
# 확산 템플릿: 제정본(rr_cls_cd='제정') 커버리지가 높은 것만 고른다.
# 커버리지가 낮으면 '제정일 있는 본' 만 채택으로 잡혀 곡선이 무너진다
# (실측: '출산장려' 보유 99곳 중 제정본 7곳 → 채택률 2.2%, S자 붕괴).
# 형식은 "조례명패턴=파일slug". slug 는 ASCII 로 둔다(정적 호스팅 URL 안전).
DEFAULT_DIFFUSION = [
    ("맨발걷기", "barefoot-walking"),
    ("안전보안관", "safety-sheriff"),
    ("청년", "youth"),
    ("반려동물", "companion-animal"),
    ("생리용품", "sanitary-products"),
    ("자살예방", "suicide-prevention"),
]

DEFAULT_SEARCH = [
    ("청년 월세 지원", "youth-housing"),
    ("출산 장려금", "birth-incentive"),
    ("반려동물 등록", "pet-registration"),
    ("재난 안전 관리", "disaster-safety"),
    ("탄소중립 녹색성장", "carbon-neutral"),
]

KINDS = ("gap", "peers", "effectiveness", "diffusion", "votes", "search")


# --------------------------------------------------------------------------- #
# 유틸
# --------------------------------------------------------------------------- #
def parse_pairs(spec: str, default: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """'질의=slug,질의2=slug2' 파싱. slug 생략 시 s1,s2... 로 자동 부여."""
    if spec is None:
        return list(default)
    out: list[tuple[str, str]] = []
    for i, part in enumerate(p.strip() for p in spec.split(",")):
        if not part:
            continue
        if "=" in part:
            key, slug = part.split("=", 1)
            out.append((key.strip(), slug.strip()))
        else:
            out.append((part, f"s{i + 1}"))
    return out


def write_json(path: Path, env: dict) -> int:
    """키 살균 후 원자적 쓰기. 반환: 바이트 수.

    tmp 에 쓰고 os.replace 로 바꾼다. 중간에 끊겨도 반쪽 파일이 남지 않으므로
    재개(--skip existing)가 안전하다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(_sanitize_keys(env), ensure_ascii=False, indent=1)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return path.stat().st_size


def existing(path: Path) -> int | None:
    """이미 만들어진 shard 면 크기, 아니면 None."""
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


class Cache:
    """지역특성·조례명 TF-IDF 를 레벨별로 1회만 계산해 재사용한다.

    재사용하지 않으면 지역마다 TF-IDF 를 다시 굽는다(실측 0.7s × 243 = 3분 낭비).
    엔진에 features/tfidf 를 주입해도 결과는 동일하다(실측 검증).
    """

    def __init__(self, conn):
        self.conn = conn
        self._feats: dict[int, dict] = {}
        self._tfidf: dict[int, dict] = {}

    def feats(self, level: int) -> dict:
        if level not in self._feats:
            self._feats[level] = P.load_region_features(self.conn, level=level)
        return self._feats[level]

    def tfidf(self, level: int) -> dict:
        if level not in self._tfidf:
            self._tfidf[level] = B.ordinance_name_tfidf(self.conn, level=level)
        return self._tfidf[level]


# --------------------------------------------------------------------------- #
# shard 생성기
# --------------------------------------------------------------------------- #
REGION_KINDS = ("gap", "peers", "effectiveness")


def region_shards(conn, srv, cache, regions, out, args, report, run_regions):
    """gap / peers / effectiveness 를 지역별 shard 로 만든다."""
    want = [k for k in REGION_KINDS if k in args.only]
    if not want:
        return
    total = len(regions)
    t_start = time.time()
    for i, reg in enumerate(regions, 1):
        sig = reg["sig_cd"]
        level = int(reg["level"])
        name = reg.get("full_name") or reg.get("name") or sig
        entry = {"sig_cd": sig, "name": name, "level": level, "files": {}}
        marks = []
        for kind in want:
            path = out / kind / f"{sig}.json"
            rel = f"{kind}/{sig}.json"
            size = None if args.force else existing(path)
            if size is not None:
                entry["files"][kind] = {"path": rel, "bytes": size, "reused": True}
                marks.append(f"{kind}=skip")
                continue
            t0 = time.time()
            try:
                if kind == "gap":
                    res = P.recommend_ordinances(
                        conn, sig, k=args.k, min_peers=args.min_peers,
                        limit=args.rec_limit, features=cache.feats(level))
                    env = envelope({sig: res}, regions=[sig])
                    note = f"추천 {len(res.get('recommendations') or [])}건"
                    if res.get("reason"):
                        note += f" ({res['reason'][:40]})"
                elif kind == "peers":
                    res = P.find_similar_governments(
                        conn, sig, k=args.k, policy_profile_weight=args.policy_weight,
                        features=cache.feats(level), tfidf=cache.tfidf(level))
                    env = envelope({sig: res}, regions=[sig])
                    note = f"peer {len(res.get('peers') or [])}곳"
                else:
                    env = srv._tool_ordinance_effectiveness({"sig_cd": sig})
                    note = f"링크 {(env.get('data') or {}).get('link_count')}건"
                size = write_json(path, env)
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {str(e)[:120]}"
                entry.setdefault("errors", {})[kind] = msg
                report["errors"].append({"kind": kind, "sig_cd": sig, "error": msg})
                marks.append(f"{kind}=FAIL")
                continue
            entry["files"][kind] = {"path": rel, "bytes": size,
                                    "note": note, "seconds": round(time.time() - t0, 2)}
            marks.append(f"{kind}={human(size)}")
        run_regions[sig] = entry
        done = time.time() - t_start
        eta = done / i * (total - i)
        print(f"  [{i}/{total}] {sig} {name} · " + " ".join(marks)
              + f" · {done:.0f}s 경과 / ETA {eta:.0f}s", flush=True)


def diffusion_shards(srv, out, args, report, run_diffusion):
    """정책 확산. 제정본 커버리지가 낮은 템플릿에는 경고를 남긴다."""
    if "diffusion" not in args.only:
        return
    pairs = parse_pairs(args.diffusion, DEFAULT_DIFFUSION)
    print(f"[확산] 템플릿 {len(pairs)}종", flush=True)
    for template, slug in pairs:
        path = out / "diffusion" / f"{slug}.json"
        rel = f"diffusion/{slug}.json"
        env = None
        size = None if args.force else existing(path)
        if size is not None:
            try:
                env = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                size = None
        if size is None:
            t0 = time.time()
            try:
                env = srv._tool_diffusion_timeline({"template": template, "mode": args.mode})
                size = write_json(path, env)
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {str(e)[:120]}"
                report["errors"].append({"kind": "diffusion", "template": template,
                                         "slug": slug, "error": msg})
                print(f"  [FAIL] {template} — {msg}", flush=True)
                continue
            secs = round(time.time() - t0, 2)
        else:
            secs = None

        d = (env or {}).get("data") or {}
        meta = d.get("adoption_meta") or {}
        cov = meta.get("enactment_date_coverage")
        item = {
            "slug": slug, "template": template, "path": rel, "bytes": size,
            "mode": d.get("mode"), "level": d.get("level"),
            "universe": d.get("universe"), "adopters": d.get("adopters"),
            "holders_any_version": meta.get("holders_any_version"),
            "final_adoption_rate": d.get("final_adoption_rate"),
            "enactment_date_coverage": cov,
            "reused": secs is None,
        }
        if secs is not None:
            item["seconds"] = secs
        if cov is not None and float(cov) < args.min_coverage:
            item["warning"] = (
                f"제정본 커버리지 {float(cov):.1%} < {args.min_coverage:.0%} — "
                "제정일이 있는 판본만 채택으로 잡혀 확산곡선이 과소추정된다. "
                "이 템플릿은 대표 화면에 쓰지 말 것.")
            report["warnings"].append({"kind": "diffusion", "slug": slug,
                                       "template": template, "message": item["warning"]})
        run_diffusion[slug] = item
        tag = "skip" if secs is None else human(size)
        covs = "n/a" if cov is None else f"{float(cov):.1%}"
        print(f"  {slug} ({template}) · 채택 {d.get('adopters')}/{d.get('universe')}"
              f" · 제정본커버리지 {covs} · {tag}"
              + ("  ⚠ 경고" if item.get("warning") else ""), flush=True)


def vote_shards(conn, srv, out, args, report, run_votes):
    """표결수 상위 의안 N건. 파일명은 bill_no(없으면 bill_id)."""
    if "votes" not in args.only:
        return
    rows = D.fetchall(
        conn,
        """SELECT v.bill_id, COUNT(*) AS n, b.bill_no, b.name, b.proc_dt, b.proc_result
           FROM votes v LEFT JOIN bills b ON b.bill_id = v.bill_id
           GROUP BY v.bill_id
           ORDER BY n DESC, b.proc_dt DESC, v.bill_id ASC
           LIMIT ?""",
        (args.votes_top,),
    )
    print(f"[표결] 상위 의안 {len(rows)}건", flush=True)
    for r in rows:
        key = str(r.get("bill_no") or r["bill_id"])
        path = out / "votes" / f"{key}.json"
        rel = f"votes/{key}.json"
        size = None if args.force else existing(path)
        reused = size is not None
        if not reused:
            try:
                env = srv._tool_bill_vote_breakdown({"bill_id": r["bill_id"]})
                size = write_json(path, env)
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {str(e)[:120]}"
                report["errors"].append({"kind": "votes", "bill_id": r["bill_id"],
                                         "error": msg})
                print(f"  [FAIL] {key} — {msg}", flush=True)
                continue
        run_votes[key] = {
            "key": key, "bill_id": r["bill_id"], "bill_no": r.get("bill_no"),
            "name": r.get("name"), "proc_dt": r.get("proc_dt"),
            "proc_result": r.get("proc_result"), "vote_records": r["n"],
            "path": rel, "bytes": size, "reused": reused,
        }
        print(f"  {key} · 표결 {r['n']}건 · {(r.get('name') or '')[:34]}"
              f" · {'skip' if reused else human(size)}", flush=True)


def search_shards(srv, out, args, report, run_search):
    """대표 질의 사전계산(선택). RAG 인덱스가 없으면 통째로 건너뛴다."""
    if "search" not in args.only:
        return
    pairs = parse_pairs(args.search, DEFAULT_SEARCH)
    print(f"[검색] 질의 {len(pairs)}종", flush=True)
    for query, slug in pairs:
        path = out / "search" / f"{slug}.json"
        rel = f"search/{slug}.json"
        size = None if args.force else existing(path)
        reused = size is not None
        count = None
        if not reused:
            try:
                env = srv._tool_semantic_search_ordinance(
                    {"query": query, "k": args.search_k, "with_text": True})
                count = (env.get("data") or {}).get("count")
                size = write_json(path, env)
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {str(e)[:120]}"
                report["errors"].append({"kind": "search", "query": query,
                                         "slug": slug, "error": msg})
                print(f"  [건너뜀] {slug} — {msg}", flush=True)
                continue
        run_search[slug] = {"slug": slug, "query": query, "path": rel,
                            "bytes": size, "count": count, "reused": reused}
        print(f"  {slug} ('{query}') · 결과 {count if count is not None else '-'}건"
              f" · {'skip' if reused else human(size)}", flush=True)


def build_index(conn, out, report, run_regions, run_diffusion, run_votes, run_search):
    """디스크를 훑어 index.json 을 채운다.

    이번 실행이 만든 것만 적으면 부분 실행(--only gap)이 이전 카탈로그를 지운다.
    그래서 '지금 디스크에 실제로 있는 것'을 진실로 삼고, 이번 실행의 메모(추천 건수·
    소요시간 등)는 있으면 덧붙인다.
    """
    # 지역 shard: gap/peers/effectiveness 디렉터리의 합집합
    names = {r["sig_cd"]: r for r in D.fetchall(
        conn, "SELECT sig_cd, name, full_name, level FROM regions WHERE sig_cd IS NOT NULL")}
    found: dict[str, dict] = {}
    for kind in REGION_KINDS:
        for f in sorted((out / kind).glob("*.json")):
            sig = f.stem
            reg = names.get(sig) or {}
            e = found.setdefault(sig, {
                "sig_cd": sig,
                "name": reg.get("full_name") or reg.get("name") or sig,
                "level": reg.get("level"), "files": {}})
            info = {"path": f"{kind}/{f.name}", "bytes": f.stat().st_size}
            prev = ((run_regions.get(sig) or {}).get("files") or {}).get(kind)
            if prev and not prev.get("reused"):
                info.update({k: v for k, v in prev.items() if k in ("note", "seconds")})
            e["files"][kind] = info
    for sig, e in run_regions.items():           # 실패해 파일이 없는 지역도 남긴다
        if e.get("errors"):
            found.setdefault(sig, {"sig_cd": sig, "name": e.get("name"),
                                   "level": e.get("level"), "files": {}})
            found[sig]["errors"] = e["errors"]
    report["regions"] = [found[s] for s in sorted(found)]

    # 단일 객체 shard: 이번 실행 메타가 있으면 그것을, 없으면 파일에서 최소 정보를 읽는다
    for kind, run, key in (("diffusion", run_diffusion, "slug"),
                           ("votes", run_votes, "key"),
                           ("search", run_search, "slug")):
        items = []
        for f in sorted((out / kind).glob("*.json")):
            it = run.get(f.stem)
            if it is None:
                it = {key: f.stem, "path": f"{kind}/{f.name}", "orphan": True}
                try:
                    d = (json.loads(f.read_text(encoding="utf-8")).get("data") or {})
                except Exception:  # noqa: BLE001
                    d = {}
                if kind == "diffusion":
                    meta = d.get("adoption_meta") or {}
                    it.update({"template": d.get("template"), "universe": d.get("universe"),
                               "adopters": d.get("adopters"),
                               "enactment_date_coverage": meta.get("enactment_date_coverage")})
                elif kind == "votes":
                    b = d.get("bill") or {}
                    it.update({"bill_id": b.get("bill_id"), "bill_no": b.get("bill_no"),
                               "name": b.get("name")})
                else:
                    it.update({"query": d.get("query"), "count": d.get("count")})
            it = dict(it)
            it["bytes"] = f.stat().st_size
            items.append(it)
        report[kind] = items

    # 이번 실행 목록에 없던(=예전에 만들어 둔) 확산 파일도 커버리지 경고를 붙인다.
    thr = float(report["params"]["min_coverage"])
    for it in report["diffusion"]:
        cov = it.get("enactment_date_coverage")
        if it.get("warning") or cov is None or float(cov) >= thr:
            continue
        it["warning"] = (
            f"제정본 커버리지 {float(cov):.1%} < {thr:.0%} — 제정일이 있는 판본만 채택으로 "
            "잡혀 확산곡선이 과소추정된다. 이 템플릿은 대표 화면에 쓰지 말 것.")
        report["warnings"].append({"kind": "diffusion", "slug": it.get("slug"),
                                   "template": it.get("template"),
                                   "message": it["warning"]})


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="전국 243개 지자체 shard 생성")
    ap.add_argument("--out", default=str(ROOT / "data"), help="출력 루트(기본 system/data)")
    ap.add_argument("--level", default="1,2", help="대상 레벨(기본 1,2 = 광역+기초 243곳)")
    ap.add_argument("--limit", type=int, default=0, help="지역 수 상한(0=전체)")
    ap.add_argument("--regions", default=None, help="sig_cd 직접 지정(쉼표) — level/limit 무시")
    ap.add_argument("--only", default=",".join(KINDS),
                    help=f"생성 대상(쉼표). 선택지: {','.join(KINDS)}")
    ap.add_argument("--force", action="store_true", help="이미 있는 shard 도 재생성")
    ap.add_argument("--k", type=int, default=12, help="peer Top-k")
    ap.add_argument("--min-peers", type=int, default=3, help="추천 최소 보유 peer 수")
    ap.add_argument("--rec-limit", type=int, default=25, help="추천 건수 상한")
    ap.add_argument("--policy-weight", type=float, default=0.35,
                    help="유사지자체의 조례 프로파일 가중치")
    ap.add_argument("--diffusion", default=None,
                    help="확산 템플릿 '패턴=slug,패턴2=slug2' (기본: 커버리지 높은 6종)")
    ap.add_argument("--mode", default="enactment", help="확산 모드(기본 enactment)")
    ap.add_argument("--min-coverage", type=float, default=0.30,
                    help="제정본 커버리지 경고 임계(기본 0.30)")
    ap.add_argument("--votes-top", type=int, default=15, help="표결 상위 의안 수")
    ap.add_argument("--search", default=None, help="검색 질의 '질의=slug,...'")
    ap.add_argument("--search-k", type=int, default=10, help="검색 결과 수")
    a = ap.parse_args()

    a.only = {s.strip() for s in a.only.split(",") if s.strip()}
    bad = a.only - set(KINDS)
    if bad:
        print(f"[오류] 알 수 없는 --only 값: {sorted(bad)} (선택지 {list(KINDS)})")
        return 2

    out = Path(a.out) / "api"
    out.mkdir(parents=True, exist_ok=True)

    cfg = get_config()
    conn = D.connect()
    srv = Server(conn, cfg)
    cache = Cache(conn)

    if a.regions:
        sigs = [s.strip() for s in a.regions.split(",") if s.strip()]
        ph = ",".join("?" for _ in sigs)
        regions = D.fetchall(
            conn,
            f"SELECT sig_cd, name, full_name, level FROM regions WHERE sig_cd IN ({ph}) "
            f"ORDER BY level, sig_cd", sigs)
        missing = set(sigs) - {r["sig_cd"] for r in regions}
        if missing:
            print(f"[경고] 없는 sig_cd: {sorted(missing)}", flush=True)
    else:
        levels = [int(s) for s in a.level.split(",") if s.strip()]
        ph = ",".join("?" for _ in levels)
        regions = D.fetchall(
            conn,
            f"SELECT sig_cd, name, full_name, level FROM regions "
            f"WHERE status='active' AND has_legislation=1 AND level IN ({ph}) "
            f"ORDER BY level, sig_cd", levels)
    if a.limit:
        regions = regions[: a.limit]

    print(f"DB={cfg.db_path}", flush=True)
    print(f"출력={out}  대상 지역 {len(regions)}곳  생성={sorted(a.only)}"
          f"  force={a.force}", flush=True)

    report = {
        "generator": "make_nationwide.py",
        "as_of_date": time.strftime("%Y-%m-%d"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "params": {"level": a.level, "limit": a.limit, "only": sorted(a.only),
                   "k": a.k, "min_peers": a.min_peers, "rec_limit": a.rec_limit,
                   "policy_weight": a.policy_weight, "mode": a.mode,
                   "votes_top": a.votes_top, "min_coverage": a.min_coverage,
                   "force": bool(a.force)},
        "layout": {k: f"{k}/{{key}}.json" for k in KINDS},
        "regions": [], "diffusion": [], "votes": [], "search": [],
        "warnings": [], "errors": [],
    }

    for kind in KINDS:                 # glob 이 실패하지 않게 디렉터리를 미리 만든다
        (out / kind).mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    run_regions: dict[str, dict] = {}
    run_diffusion: dict[str, dict] = {}
    run_votes: dict[str, dict] = {}
    run_search: dict[str, dict] = {}
    region_shards(conn, srv, cache, regions, out, a, report, run_regions)
    diffusion_shards(srv, out, a, report, run_diffusion)
    vote_shards(conn, srv, out, a, report, run_votes)
    search_shards(srv, out, a, report, run_search)
    build_index(conn, out, report, run_regions, run_diffusion, run_votes, run_search)

    # 요약
    per_kind: dict[str, dict] = {}
    for r in report["regions"]:
        for kind, f in r["files"].items():
            s = per_kind.setdefault(kind, {"files": 0, "bytes": 0})
            s["files"] += 1
            s["bytes"] += int(f.get("bytes") or 0)
    for kind in ("diffusion", "votes", "search"):
        for it in report[kind]:
            s = per_kind.setdefault(kind, {"files": 0, "bytes": 0})
            s["files"] += 1
            s["bytes"] += int(it.get("bytes") or 0)
    report["totals"] = {
        "regions_covered": len(report["regions"]),
        "regions_requested": len(regions),
        "files": sum(v["files"] for v in per_kind.values()),
        "bytes": sum(v["bytes"] for v in per_kind.values()),
        "by_kind": per_kind,
        "errors": len(report["errors"]),
        "warnings": len(report["warnings"]),
        "seconds": round(time.time() - t0, 1),
    }

    # index 는 응답 봉투가 아니라 카탈로그다(data/as_of_date 봉투를 씌우지 않는다).
    idx = out / "index.json"
    body = json.dumps(_sanitize_keys(report), ensure_ascii=False, indent=1)
    tmp = idx.with_suffix(".json.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, idx)
    idx_bytes = idx.stat().st_size

    print("─" * 70, flush=True)
    for kind in KINDS:
        s = per_kind.get(kind)
        if s:
            print(f"  {kind:<14} {s['files']:>4} 파일  {human(s['bytes'])}", flush=True)
    print(f"  {'index.json':<14} {1:>4} 파일  {human(idx_bytes)}", flush=True)
    print(f"합계 {report['totals']['files']} 파일 · {human(report['totals']['bytes'])}"
          f" · 커버 지역 {report['totals']['regions_covered']}곳"
          f" · {report['totals']['seconds']}s"
          f" · 경고 {len(report['warnings'])} · 실패 {len(report['errors'])}", flush=True)
    for w in report["warnings"]:
        print(f"  [경고] {w.get('slug') or w.get('kind')}: {w['message']}", flush=True)
    if report["errors"]:
        for e in report["errors"][:10]:
            print(f"  [실패] {e}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
