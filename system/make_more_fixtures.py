#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""실 DB → 나머지 4개 시각화 fixture 생성 (확산·실효성·표결·검색).

make_gap_fixtures.py 가 격차·유사지자체(gap/peers)를 만들었다면, 이 스크립트는
정책확산·조례실효성·국회표결·조문검색 화면의 사전계산 fixture 를 만든다.

  api/diffusion.json      ← diffusion_timeline       (template 필수)
  api/effectiveness.json  ← ordinance_effectiveness  (sig_cd 로 지역 단위)
  api/votes.json          ← bill_vote_breakdown       (bill 미지정 시 표결 최다 의안 자동선택)
  api/search.json         ← semantic_search_ordinance (query 필수, RAG 인덱스 필요)

이 4개 화면은 데이터가 sig_cd 맵이 아니라 '단일 객체'다(gap 과 다름). 각 tool 응답
봉투를 그대로 저장하면 뷰가 바로 소비한다.

전제:
  - 실 DB(system/data/policymap.db, 또는 POLICYMAP_DB).
  - 확산·실효성은 numpy 필요(pip install -e ".[full]"). 검색은 GraphRAG 인덱스 필요(run index).
    → 준비 안 된 fixture 는 건너뛰고 나머지는 정상 생성한다(개별 try/except).

사용:
  cd system
  python make_more_fixtures.py
  python make_more_fixtures.py --sig 47190 --diffusion-template "출산장려" --query "청년 월세 지원"
  python make_more_fixtures.py --bill-no 2214069            # 특정 의안 표결
"""
import argparse, json, re, sys, time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from policymap import db as D
from policymap.config import get_config
from policymap.mcp_server.server import Server



_URL_KEY_RE = re.compile(r"([?&])(OC|serviceKey|ServiceKey|KEY|Key)=[^&\"'\s]+")


def _sanitize_keys(obj):
    """fixture 에 스며든 API 키를 제거한다.

    RAG 인덱스(docs.jsonl)는 DB 살균 이전에 구축돼 옛 official_url 을 그대로 들고 있다.
    그래서 search fixture 는 DB 가 깨끗해도 키가 딸려 나온다. 인덱스를 다시 굽지 않는 한
    출력 단에서 한 번 더 걸러야 안전하다. [실측: search.json 에 OC 키 10건 유출]
    """
    if isinstance(obj, str):
        out = _URL_KEY_RE.sub(lambda m: m.group(1), obj)
        return re.sub(r"[?&]$", "", out).replace("?&", "?").replace("&&", "&")
    if isinstance(obj, list):
        return [_sanitize_keys(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_keys(v) for k, v in obj.items()}
    return obj

def save(out_dir, name, env, summary):
    env = _sanitize_keys(env)
    (out_dir / f"{name}.json").write_text(
        json.dumps(env, ensure_ascii=False, indent=1), encoding="utf-8")
    kb = (out_dir / f"{name}.json").stat().st_size / 1024
    print(f"  [OK] {name}.json  {kb:.0f} KB  · {summary}", flush=True)


def run(label, name, out_dir, fn):
    """fn() -> (envelope, summary). 실패해도 다른 fixture 는 계속 만든다."""
    t0 = time.time()
    try:
        env, summary = fn()
    except Exception as e:  # noqa: BLE001
        print(f"  [건너뜀] {name}.json — {type(e).__name__}: {str(e)[:100]}", flush=True)
        return
    save(out_dir, name, env, f"{summary} · {time.time()-t0:.1f}s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data"), help="출력 루트(기본 system/data)")
    ap.add_argument("--sig", default="47190", help="실효성 대상 시군구코드(기본 47190 구미시)")
    ap.add_argument("--diffusion-template", default="출산장려", help="확산 분석 조례명 패턴")
    ap.add_argument("--query", default="청년 월세 지원", help="조문 검색 질의")
    ap.add_argument("--bill-id", default=None, help="표결 대상 의안 ID(미지정 시 자동선택)")
    ap.add_argument("--bill-no", default=None, help="표결 대상 의안번호(미지정 시 자동선택)")
    a = ap.parse_args()

    out_dir = Path(a.out) / "api"
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = get_config()
    conn = D.connect()
    srv = Server(conn, cfg)
    print(f"DB={cfg.db_path}  →  {out_dir}", flush=True)

    # 표결: 의안 미지정이면 표결 레코드가 가장 많은 의안을 자동 선택
    bill_id, bill_no = a.bill_id, a.bill_no
    if not bill_id and not bill_no:
        row = D.fetchone(conn,
            "SELECT bill_id, COUNT(*) AS n FROM votes GROUP BY bill_id ORDER BY n DESC LIMIT 1")
        if row and row.get("bill_id"):
            bill_id = row["bill_id"]
            print(f"  표결 의안 자동선택: {bill_id} (표결 {row['n']}건)", flush=True)

    # 1) 정책 확산
    def _diffusion():
        env = srv._tool_diffusion_timeline({"template": a.diffusion_template, "mode": "enactment"})
        d = env.get("data", {})
        return env, f"template={a.diffusion_template} 채택 {d.get('adopters')}/{d.get('universe')}"
    run("정책확산", "diffusion", out_dir, _diffusion)

    # 2) 조례 실효성 (지역 단위)
    def _effectiveness():
        env = srv._tool_ordinance_effectiveness({"sig_cd": a.sig})
        d = env.get("data", {})
        return env, f"sig={a.sig} 링크 {d.get('link_count')}건 (verified {(d.get('verification') or {}).get('verified')})"
    run("조례실효성", "effectiveness", out_dir, _effectiveness)

    # 3) 국회 표결
    def _votes():
        if not bill_id and not bill_no:
            raise RuntimeError("표결 데이터(votes)가 비어 있어 의안을 고를 수 없다")
        args = {}
        if bill_id: args["bill_id"] = bill_id
        if bill_no: args["bill_no"] = bill_no
        env = srv._tool_bill_vote_breakdown(args)
        d = env.get("data", {})
        b = d.get("bill", {})
        return env, f"의안 {b.get('bill_no') or bill_id} · 정당 {len(d.get('party_breakdown', []))}"
    run("국회표결", "votes", out_dir, _votes)

    # 4) 조문 검색 (RAG 인덱스 필요)
    def _search():
        env = srv._tool_semantic_search_ordinance({"query": a.query, "k": 10, "with_text": True})
        d = env.get("data", {})
        return env, f"query='{a.query}' 결과 {d.get('count')}건"
    run("조문검색", "search", out_dir, _search)

    print("완료. viz 를 ?src=real 로 열어 #/diffusion #/effectiveness #/votes #/search 확인.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
