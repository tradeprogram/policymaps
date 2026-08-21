"""policymap.graph.export — 정적 JSON 번들 + GraphML export.

CONTRACTS.md §3.3 계약:
    export_static(conn, out_dir, *, as_of_date=None) -> dict
    export_graphml(conn, path) -> None

산출물(klocal/09 §5 manifest+조각 패턴):
    manifest.json                      # 파일 목록 + gov/file/rows/total/hash + as_of/stale
    graph/nodes.json, graph/edges.json # build_graph 전량 export
    regions/{sig_cd}.json              # 지역별 shard(프로파일 + recentChanges)
    changes/latest.json                # 최근 N 변경
    changes/feed-YYYY-MM.json          # 월별 변경 피드
    meta/graph-stats.json              # 빌드 통계(node/edge/skip)

모든 조각에 as_of_date 동봉, 만료(신선도 창 초과) 시 stale:true.
GraphML 은 순수파이썬 XML 작성(networkx 불요) — build.graph_nodes/graph_edges 접근자 사용.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional
from xml.sax.saxutils import escape as _xml_escape

from .. import db as _db
from .. import util as _util
from . import build as _build

# 신선도 창(일). 이 일수 초과 시 stale:true. 주간 갱신 기준 여유.
_DEFAULT_STALE_DAYS = 8
_LATEST_CHANGES = 200


# --------------------------------------------------------------------------- #
# 유틸
# --------------------------------------------------------------------------- #

_GRAPH_CACHE: dict = {}


def _cached_graph(conn, include_articles: bool):
    """export 안에서 그래프를 한 번만 빌드한다.

    [실측] 전량 빌드는 18.8GB / 약 1.5시간이라, export_static 과 export_graphml 이
    각각 build_graph 를 호출하면 같은 작업을 2회 더 반복하게 된다.
    또한 서빙용 그래프에는 예산 노드(93만)가 불필요해 include_budget=False 로 던다.
    """
    key = bool(include_articles)
    G = _GRAPH_CACHE.get(key)
    if G is None:
        G = _build.build_graph(conn, include_articles=include_articles, include_budget=False)
        _GRAPH_CACHE.clear()
        _GRAPH_CACHE[key] = G
    return G

def _fetch(conn, sql, params=()) -> list[dict]:
    try:
        return _db.fetchall(conn, sql, params)
    except sqlite3.OperationalError:
        return []


def _write_json(path: Path, obj: Any) -> dict:
    """JSON 파일 기록 후 {path, rows(또는 None), bytes, hash} 요약 반환."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    rows = None
    if isinstance(obj, list):
        rows = len(obj)
    elif isinstance(obj, dict):
        for k in ("items", "rows", "nodes", "edges", "changes", "features"):
            if isinstance(obj.get(k), list):
                rows = len(obj[k])
                break
    return {
        "bytes": len(text.encode("utf-8")),
        "rows": rows,
        "hash": _util.content_hash(text),
    }


def _days_between(iso_a: str, iso_b: str) -> Optional[int]:
    """두 날짜 문자열(YYYY-MM-DD 또는 ISO) 사이 일수. 파싱 실패 시 None."""
    from datetime import date

    def to_date(s: str):
        digits = "".join(ch for ch in str(s) if ch.isdigit())
        if len(digits) >= 8:
            try:
                return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
            except ValueError:
                return None
        return None

    da, dbd = to_date(iso_a), to_date(iso_b)
    if da is None or dbd is None:
        return None
    return abs((dbd - da).days)


def _is_stale(as_of_date: Optional[str], today: str, stale_days: int) -> bool:
    if not as_of_date:
        return True
    d = _days_between(as_of_date, today)
    return d is None or d > stale_days


# --------------------------------------------------------------------------- #
# export_static
# --------------------------------------------------------------------------- #
def export_static(
    conn: sqlite3.Connection,
    out_dir: str,
    *,
    as_of_date: Optional[str] = None,
    stale_days: int = _DEFAULT_STALE_DAYS,
    include_articles: bool = False,
) -> dict:
    """정적 번들 생성. manifest + graph + regions shard + changes 피드.

    반환: {out_dir, as_of_date, stale, files_written, counts, manifest_path}.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = _util.today_kst()
    as_of = as_of_date or today
    stale = _is_stale(as_of, today, stale_days)

    files: list[dict] = []

    def record(rel: str, summary: dict):
        entry = {"file": rel, **summary}
        files.append(entry)

    # --- 1) 그래프 전량(nodes/edges) ---
    G = _cached_graph(conn, include_articles)
    meta = _build.graph_meta(G)

    nodes_out = []
    for nid, attr in _build.graph_nodes(G):
        row = {"id": nid}
        row.update({k: v for k, v in attr.items() if not isinstance(v, (dict, list, set))})
        nodes_out.append(row)
    edges_out = []
    for u, v, key, attr in _build.graph_edges(G):
        row = {"source": u, "target": v, "relation": key}
        row.update({k: val for k, val in attr.items()
                    if k != "relation" and not isinstance(val, (dict, list, set))})
        edges_out.append(row)

    record("graph/nodes.json", _write_json(out / "graph" / "nodes.json",
           {"as_of_date": as_of, "stale": stale, "count": len(nodes_out), "nodes": nodes_out}))
    record("graph/edges.json", _write_json(out / "graph" / "edges.json",
           {"as_of_date": as_of, "stale": stale, "count": len(edges_out), "edges": edges_out}))
    record("meta/graph-stats.json", _write_json(out / "meta" / "graph-stats.json",
           {"as_of_date": as_of, **meta}))

    # --- 2) 지역 shard(자치입법권 있는 현행 지자체) ---
    from . import analysis as _analysis  # 지연 import(순환 방지)

    region_rows = _fetch(conn,
                         "SELECT DISTINCT sig_cd FROM regions "
                         "WHERE sig_cd IS NOT NULL AND status='active'")
    region_index = []
    for rr in region_rows:
        sig = rr["sig_cd"]
        if not sig:
            continue
        profile = _analysis.build_region_profile(conn, sig)
        if not profile.get("region_id"):
            continue
        profile["stale"] = _is_stale(profile.get("as_of_date"), today, stale_days)
        summary = _write_json(out / "regions" / f"{sig}.json", profile)
        record(f"regions/{sig}.json", summary)
        region_index.append({
            "sig_cd": sig,
            "name": profile.get("name"),
            "level": profile.get("level"),
            "ordinance_total": profile.get("ordinance_total", 0),
            "file": f"regions/{sig}.json",
        })
    record("regions/index.json", _write_json(out / "regions" / "index.json",
           {"as_of_date": as_of, "stale": stale, "count": len(region_index),
            "items": region_index}))

    # --- 3) 변경 피드(latest + 월별) ---
    latest = _fetch(conn,
                    "SELECT change_id, ts, source, scope, entity_type, entity_id, "
                    "entity_name, event, before, after, region_code, official_url "
                    "FROM change_log ORDER BY ts DESC LIMIT ?", (_LATEST_CHANGES,))
    record("changes/latest.json", _write_json(out / "changes" / "latest.json",
           {"as_of_date": as_of, "stale": stale, "count": len(latest), "changes": latest}))

    # 월별 그룹핑(ts 앞 YYYY-MM)
    by_month: dict[str, list[dict]] = {}
    for c in _fetch(conn,
                    "SELECT change_id, ts, source, entity_type, entity_id, entity_name, "
                    "event, region_code, official_url FROM change_log ORDER BY ts DESC"):
        ts = str(c.get("ts") or "")
        month = ts[:7] if len(ts) >= 7 else "unknown"
        by_month.setdefault(month, []).append(c)
    month_index = []
    for month, rows in sorted(by_month.items(), reverse=True):
        rel = f"changes/feed-{month}.json"
        summary = _write_json(out / "changes" / f"feed-{month}.json",
                              {"as_of_date": as_of, "month": month,
                               "count": len(rows), "changes": rows})
        record(rel, summary)
        month_index.append({"month": month, "count": len(rows), "file": rel})

    # --- 4) manifest ---
    counts = {
        "regions": _db.count(conn, "regions"),
        "legal_instrument": _db.count(conn, "legal_instrument"),
        "ordinances": _db.count(conn, "ordinances"),
        "delegations": _db.count(conn, "delegations"),
        "bills": _db.count(conn, "bills"),
        "budget_lines": _db.count(conn, "budget_lines"),
        "change_log": _db.count(conn, "change_log"),
        "graph_nodes": meta.get("total_nodes", len(nodes_out)),
        "graph_edges": meta.get("total_edges", len(edges_out)),
    }
    watermarks = _fetch(conn,
                        "SELECT source, scope, cursor, status, last_success, changed "
                        "FROM watermarks ORDER BY source, scope")
    any_stale = stale or any(w.get("status") in ("stale", "error", "partial") for w in watermarks)

    manifest = {
        "schema": "policymap.static.v1",
        "generated_at": _util.now_kst_iso(),
        "as_of_date": as_of,
        "stale": any_stale,
        "stale_days": stale_days,
        "counts": counts,
        "graph_stats": {
            "backend": meta.get("backend"),
            "node_counts": meta.get("node_counts"),
            "edge_counts": meta.get("edge_counts"),
            "skipped_edges": meta.get("skipped_edges"),
        },
        "watermarks": watermarks,
        "region_index": "regions/index.json",
        "changes_latest": "changes/latest.json",
        "changes_months": month_index,
        "files": files,
    }
    manifest_path = out / "manifest.json"
    manifest_summary = _write_json(manifest_path, manifest)

    return {
        "out_dir": str(out),
        "as_of_date": as_of,
        "stale": any_stale,
        "files_written": len(files) + 1,  # + manifest
        "counts": counts,
        "manifest_path": str(manifest_path),
        "manifest_hash": manifest_summary["hash"],
    }


# --------------------------------------------------------------------------- #
# export_graphml (순수파이썬 XML)
# --------------------------------------------------------------------------- #
def _graphml_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "long"
    if isinstance(value, float):
        return "double"
    return "string"


def _graphml_val(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return _xml_escape(str(value))


def export_graphml(conn: sqlite3.Connection, path: str, *, include_articles: bool = False) -> None:
    """그래프를 GraphML 로 기록. networkx 유무와 무관하게 순수파이썬 XML 작성.

    노드/엣지 속성 키를 스캔해 <key> 선언 후, 스칼라 속성만 직렬화한다.
    엣지의 relation 은 GraphML edge 속성 'relation' 으로 기록.
    """
    G = _cached_graph(conn, include_articles)

    # 속성 키 수집 + 타입 추정(스칼라만)
    node_keys: dict[str, str] = {}
    for _nid, attr in _build.graph_nodes(G):
        for k, v in attr.items():
            if isinstance(v, (dict, list, set)) or v is None:
                continue
            node_keys.setdefault(k, _graphml_type(v))
    edge_keys: dict[str, str] = {"relation": "string"}
    for _u, _v, _key, attr in _build.graph_edges(G):
        for k, v in attr.items():
            if k == "relation" or isinstance(v, (dict, list, set)) or v is None:
                continue
            edge_keys.setdefault(k, _graphml_type(v))

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<graphml xmlns="http://graphml.graphdrawing.org/xmlns">')
    # key 선언(노드/엣지 네임스페이스 충돌 방지 위해 접두)
    for k, t in node_keys.items():
        kid = f"nd_{k}"
        lines.append(f'  <key id="{kid}" for="node" attr.name="{_xml_escape(k)}" attr.type="{t}"/>')
    for k, t in edge_keys.items():
        kid = f"ed_{k}"
        lines.append(f'  <key id="{kid}" for="edge" attr.name="{_xml_escape(k)}" attr.type="{t}"/>')
    lines.append('  <graph edgedefault="directed">')

    for nid, attr in _build.graph_nodes(G):
        lines.append(f'    <node id="{_xml_escape(str(nid))}">')
        for k in node_keys:
            v = attr.get(k)
            if v is None or isinstance(v, (dict, list, set)):
                continue
            lines.append(f'      <data key="nd_{k}">{_graphml_val(v)}</data>')
        lines.append('    </node>')

    eid = 0
    for u, v, key, attr in _build.graph_edges(G):
        lines.append(f'    <edge id="e{eid}" source="{_xml_escape(str(u))}" '
                     f'target="{_xml_escape(str(v))}">')
        lines.append(f'      <data key="ed_relation">{_graphml_val(key)}</data>')
        for k in edge_keys:
            if k == "relation":
                continue
            val = attr.get(k)
            if val is None or isinstance(val, (dict, list, set)):
                continue
            lines.append(f'      <data key="ed_{k}">{_graphml_val(val)}</data>')
        lines.append('    </edge>')
        eid += 1

    lines.append('  </graph>')
    lines.append('</graphml>')
    out.write_text("\n".join(lines), encoding="utf-8")


__all__ = ["export_static", "export_graphml"]
