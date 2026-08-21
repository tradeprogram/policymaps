"""smoke — 무키(no-key) end-to-end 파이프라인 점검.

단계: init(스키마) → seed(코어 미니월드 + korea100 법령 인리치) →
      build graph → analysis(위임격차) → export(정적 번들) → mcp 질의.

코어(init+seed)는 표준라이브러리만으로 반드시 성공해야 한다.
병렬 미구현 모듈(graph/mcp)은 자동 SKIP 로 표기하고 계속 진행한다.
네트워크·인증키 일절 사용 안 함.

실행:  python tests/smoke.py       (pytest 불필요)
반환:  0 = 코어 성공(그 외 단계는 SKIP 허용), 1 = 코어 실패
"""
import io
import importlib
import json
import sys
import tempfile
from pathlib import Path

# 경로 부트스트랩 + 코어 헬퍼
from _support import (
    SYSTEM_ROOT, fresh_db, seed_reference, seed_sample,
    pm_db as db, pm_util as util, pm_config as config,
)

KOREA100_DIR = SYSTEM_ROOT.parent / "external" / "korea100" / "web" / "data" / "institutions"

_KIND_SRC = {
    "헌법": "constitution", "조약": "treaty",
    "행정규칙": "admin-rule", "훈령": "admin-rule", "예규": "admin-rule",
    "고시": "admin-rule", "지침": "admin-rule",
}


def _src_type(kind: str) -> str:
    for tok, st in _KIND_SRC.items():
        if tok in (kind or ""):
            return st
    return "statute"


def seed_korea100(conn, limit: int = 30) -> dict:
    """korea100 institution JSON 의 canvas.legalBasis → legal_instrument 인리치.

    파일 부재/파싱실패는 정상적 없음으로 흡수(무키 규율). 추가 건수 반환.
    """
    added, kinds, files = 0, set(), 0
    if not KOREA100_DIR.exists():
        return {"available": False, "instruments_added": 0, "files": 0}
    today = util.today_kst()
    for path in sorted(KOREA100_DIR.glob("*.json"))[:limit]:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        files += 1
        canvas = d.get("canvas") or {}
        lb = canvas.get("legalBasis") or d.get("legalBasis") or []
        if isinstance(lb, dict):
            lb = [lb]
        for item in lb:
            if not isinstance(item, dict):
                continue
            law = (item.get("law") or item.get("name") or "").strip()
            if not law:
                continue
            kind = (item.get("kind") or "법률").strip()
            src = _src_type(kind)
            # instrument_kind FK 선보장
            if kind not in kinds:
                db.upsert(conn, "instrument_kind",
                          {"kind": kind, "source_type": src}, "kind")
                kinds.add(kind)
            iid = "statute:k100-" + util.stable_id(law)[:10]
            action = db.upsert(conn, "legal_instrument", {
                "instrument_id": iid, "kind": kind, "source_type": src,
                "name": law, "current_history": "현행", "status": "active",
                "as_of_date": today, "content_hash": util.content_hash(law),
                "verification_status": "source-linked", "updated_at": today,
            }, "instrument_id", hash_col="content_hash")
            if action == "inserted":
                added += 1
    conn.commit()
    return {"available": True, "instruments_added": added,
            "files": files, "kinds": sorted(kinds)}


# --------------------------------------------------------------------------- #
# MCP 구동(인라인, 무의존)
# --------------------------------------------------------------------------- #
def _mcp_query(server, requests):
    inp = "\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n"
    for meth in ("handle_request", "handle", "dispatch", "_dispatch", "_handle_request"):
        fn = getattr(server, meth, None)
        if callable(fn):
            return [fn(r) for r in requests if fn(r) is not None]
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(inp), io.StringIO()
    try:
        server.serve_stdio()
    except Exception:
        pass
    finally:
        out = sys.stdout.getvalue()
        sys.stdin, sys.stdout = old_in, old_out
    resp = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                resp.append(json.loads(line))
            except Exception:
                pass
    return resp


def _optional(dotted, *attrs):
    try:
        mod = importlib.import_module(dotted)
    except Exception as exc:
        return None, f"미구현/임포트실패: {exc}"
    missing = [a for a in attrs if not hasattr(mod, a)]
    if missing:
        return None, f"미구현 심볼 {missing}"
    return mod, "ok"


def main() -> int:
    print("=" * 68)
    print("policy_maps SMOKE (무키 end-to-end)")
    print("=" * 68)
    summary: list[tuple[str, str, str]] = []  # (단계, 상태, 비고)

    # --- 1. init + seed(코어) ---
    try:
        conn = fresh_db(seed=True)   # 스키마 + 미니월드
        n_tables = db.count(conn, "sqlite_master", "type='table'")
        print(f"[init]  스키마 적용: {n_tables} 테이블")
        summary.append(("init", "OK", f"{n_tables} tables"))
    except Exception as exc:
        print(f"[init]  실패: {exc}")
        summary.append(("init", "FAIL", str(exc)))
        _print_summary(summary)
        return 1

    try:
        k = seed_korea100(conn, limit=30)
        stats = {t: db.count(conn, t) for t in
                 ("regions", "legal_instrument", "ordinances", "delegations",
                  "budget_lines", "bills", "votes")}
        # CDC 경로 시연: 워터마크 + 변경로그 1건
        db.advance_cursor(conn, "ordin", "sig:11110",
                          cursor="efYd:20260401|maxMST:9002", changed=2, rows_seen=2)
        db.log_change(conn, entity_type="ordinance", entity_id="ordin:9001",
                      event="amended", source="ordin", scope="sig:11110",
                      entity_name="종로구 주차장 조례", region_code="11110")
        print(f"[seed]  코어: {stats}")
        note = "korea100 미탐지" if not k["available"] else \
            f"korea100 +{k['instruments_added']} instruments ({k['files']} files)"
        print(f"[seed]  {note}")
        summary.append(("seed", "OK",
                        f"instr={stats['legal_instrument']} ord={stats['ordinances']} "
                        f"deleg={stats['delegations']}; {note}"))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[seed]  실패: {exc}")
        summary.append(("seed", "FAIL", str(exc)))
        _print_summary(summary)
        return 1

    # --- 2. build graph ---
    build, why = _optional("policymap.graph.build", "build_graph")
    g = None
    if build is None:
        print(f"[graph] SKIP ({why})")
        summary.append(("graph.build", "SKIP", why))
    else:
        try:
            from _support import graph_counts
            g = build.build_graph(conn)
            nn, ne = graph_counts(g)
            print(f"[graph] build_graph: nodes={nn} edges={ne}")
            summary.append(("graph.build", "OK", f"nodes={nn} edges={ne}"))
        except Exception as exc:
            print(f"[graph] 실패: {exc}")
            summary.append(("graph.build", "ERR", str(exc)))

    # --- 3. analysis: 위임격차 ---
    analysis, why = _optional("policymap.graph.analysis", "get_delegation_gap")
    if analysis is None:
        print(f"[analy] SKIP ({why})")
        summary.append(("graph.analysis", "SKIP", why))
    else:
        try:
            gaps = analysis.get_delegation_gap(conn, "26170")
            print(f"[analy] get_delegation_gap('26170'): {len(gaps)} 건")
            summary.append(("graph.analysis", "OK", f"gap={len(gaps)}"))
        except Exception as exc:
            print(f"[analy] 실패: {exc}")
            summary.append(("graph.analysis", "ERR", str(exc)))

    # --- 4. export ---
    export, why = _optional("policymap.graph.export", "export_static")
    if export is None:
        print(f"[expo]  SKIP ({why})")
        summary.append(("graph.export", "SKIP", why))
    else:
        try:
            with tempfile.TemporaryDirectory() as out:
                export.export_static(conn, out)
                files = [p.name for p in Path(out).rglob("*") if p.is_file()]
                has_manifest = (Path(out) / "manifest.json").exists()
                print(f"[expo]  export_static: {len(files)} files, manifest={has_manifest}")
                summary.append(("graph.export", "OK",
                                f"{len(files)} files manifest={has_manifest}"))
        except Exception as exc:
            print(f"[expo]  실패: {exc}")
            summary.append(("graph.export", "ERR", str(exc)))

    # --- 5. mcp 질의 ---
    mcp, why = _optional("policymap.mcp_server.server", "Server")
    if mcp is None:
        print(f"[mcp]   SKIP ({why})")
        summary.append(("mcp", "SKIP", why))
    else:
        try:
            cfg = config.get_config()
            try:
                server = mcp.Server(conn, cfg)
            except TypeError:
                server = mcp.Server(conn=conn, cfg=cfg)
            resp = _mcp_query(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "gap_analysis",
                            "arguments": {"region_id": "26170"}}},
            ])
            tl = next((r for r in resp if r.get("id") == 2), None)
            ntools = len(tl["result"]["tools"]) if tl and "result" in tl else 0
            got_call = any(r.get("id") == 3 for r in resp)
            print(f"[mcp]   tools/list={ntools} tools, tools/call 응답={got_call}")
            summary.append(("mcp", "OK", f"tools={ntools} call={got_call}"))
        except Exception as exc:
            print(f"[mcp]   실패: {exc}")
            summary.append(("mcp", "ERR", str(exc)))

    _print_summary(summary)
    # 코어(init/seed) 성공이면 0. 병렬 미구현 SKIP 은 실패로 치지 않음.
    core_ok = all(s != "FAIL" for name, s, _ in summary if name in ("init", "seed"))
    hard_err = any(s == "ERR" for _, s, _ in summary)
    return 0 if core_ok and not hard_err else 1


def _print_summary(summary):
    print("-" * 68)
    print("SMOKE 요약:")
    for name, status, note in summary:
        print(f"  {status:5s} {name:16s} {note}")
    print("-" * 68)
    ok = sum(1 for _, s, _ in summary if s == "OK")
    skip = sum(1 for _, s, _ in summary if s == "SKIP")
    bad = sum(1 for _, s, _ in summary if s in ("FAIL", "ERR"))
    print(f"OK={ok} SKIP={skip} FAIL/ERR={bad}")


if __name__ == "__main__":
    sys.exit(main())
