#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""실 DB → **법령 위계·인용망 서브그래프** shard 생성.

정적 번들(graph/nodes.json 134MB)은 브라우저가 통째로 못 받아 그래프 화면이
"너무 큼" 가드만 띄운다. 게다가 그 번들은 엣지 양끝 노드가 다 있어야 살아남는
build_graph 규칙 때문에 CITES 812,339건 중 2,189건(0.3%)만 담고 있다.

원인은 인용 대상이 대부분 **미해결 이름노드**라는 데 있다(실측).

    delegations          421,627건 중 parent_id 가 'lawname:*' 인 것 113,848건
                         → 나머지 307,779건만 정적 번들에 들어감
    instrument_relations 812,339건 중 dst_id 가 'lawname:*' 인 것 810,150건
                         → statute:* 로 직결된 2,169건만 번들에 들어감

이 스크립트는 두 가지를 한다.

  1) DB 에서 **씨앗 중심 서브그래프만** 뽑는다(전량 빌드 없음). 파일당 200KB 이하.
  2) 'lawname:{정규화명}' 을 legal_instrument.name 정규화 키로 **이름해소**한다.
     [실측] CITES 엣지의 74.5%(603,253/810,150)가 실제 법령 노드로 붙는다.
     해소된 엣지는 resolved_by='name-match', inferred=1 로 표시하고,
     해소 실패분은 placeholder 노드(resolved=false)로 남겨 "수집 안 된 상위법"임을 드러낸다.

출력(<out>/api/graph/):

    ordinance/{key}.json   조례 중심 2홉 서브그래프 (key = ordinance_id 의 ':'→'-')
                           조례 → DELEGATED_FROM 상위법 → 그 법의 다른 위임조례 표본
                           + CITES 인용. 노드 상한 --max-nodes.
    statute/{key}.json     법령 중심 서브그래프(이 법에 근거·인용한 조례 상위 N)
    hierarchy.json         법령 위계 전체 요약(tier별 노드 수, 위임/인용 엣지 통계,
                           tier→tier 흐름 행렬) — 위계 개념도용
    index.json             커버 목록(조례/법령 shard + 파일 크기)

노드/엣지 스키마는 graph/nodes.json·edges.json 과 동일하다(뷰 재사용).
    node : {id, label, kind, name, tier, status, ...}      id = 'ordinance:ordin:123'
    edge : {source, target, relation, verification_status, ...}

엔진 재사용(중복 구현 금지):
    graph.build.node_id / _LABEL / _clean_attrs   노드키·라벨 규약
    make_gap_fixtures.envelope                    응답 봉투
    make_nationwide.write_json / existing / human 원자적 쓰기·재개·단위표기
    make_more_fixtures._sanitize_keys             API 키 살균(RAG 인덱스가 살균 전 URL 보유)

  shard 본문만 write_shard() 로 들여쓰기 없이 쓴다(420개 × 80노드에서 약 20% 절감).
  hierarchy.json / index.json 은 사람이 열어보므로 indent 를 유지한다.

사용:
  cd system
  python make_graph_fixtures.py --ord-limit 5 --statute-limit 3   # 소규모 검증
  python make_graph_fixtures.py                                    # 기본 300 + 120
  python make_graph_fixtures.py --only hierarchy --force
  python make_graph_fixtures.py --max-nodes 60 --force             # 용량 더 줄이기
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:  # 콘솔이 cp949 여도 한글 법령명 출력에서 죽지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from policymap import db as D                                  # noqa: E402
from policymap.config import get_config                        # noqa: E402
from policymap.graph.build import _LABEL, _clean_attrs, node_id  # noqa: E402

# 중복 구현 금지 — 봉투/쓰기/재개는 기존 생성기 것을 그대로 쓴다.
from make_gap_fixtures import envelope                         # noqa: E402
from make_more_fixtures import _sanitize_keys                  # noqa: E402
from make_nationwide import existing, human, write_json        # noqa: E402


def write_shard(path: Path, env: dict) -> int:
    """make_nationwide.write_json 과 같은 원자적 쓰기 + 키 살균. 들여쓰기만 뺀다.

    shard 420개 × 노드 80개 규모에서 indent=1 은 전체의 약 20%(5MB)를 공백으로
    쓴다. 지역 shard 처럼 사람이 열어보는 파일이 아니라 뷰가 통째로 파싱하는
    데이터라 가독성보다 용량을 택했다. hierarchy/index 는 indent 를 유지한다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(_sanitize_keys(env), ensure_ascii=False, separators=(",", ":"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return path.stat().st_size


KINDS = ("ordinance", "statute", "hierarchy")

# 인용문 원문은 길다. 노드 150개 × 원문이면 200KB 를 넘긴다.
_CITE_MAX = 60

# --------------------------------------------------------------------------- #
# 이름해소
# --------------------------------------------------------------------------- #
_NORM_RE = re.compile(r"[\s·ㆍ・,\.]")


def norm_name(s: str | None) -> str:
    """법령명 정규화 키. 'lawname:' 접두는 이미 이 규칙으로 만들어져 있다."""
    return _NORM_RE.sub("", s or "")


def truncate(s: str | None, n: int = _CITE_MAX) -> str | None:
    if not s:
        return None
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


class Resolver:
    """'lawname:{정규화명}' → legal_instrument.instrument_id 이름해소기.

    같은 정규화명에 연혁본이 여러 개면 current_history='현행' 을 우선한다.
    해소 결과는 추론이므로 노드에 resolved_by='name-match' 를 남긴다(단정 금지).
    """

    def __init__(self, conn):
        self.instruments: dict[str, dict] = {}
        self.by_norm: dict[str, str] = {}
        rows = D.fetchall(
            conn,
            """SELECT li.instrument_id, li.kind, li.source_type, li.name, li.short_name,
                      li.competent_authority, li.status, li.current_history,
                      li.effective_on, li.repealed_on, li.verification_status,
                      COALESCE(li.national_tier, ik.national_tier) AS tier,
                      COALESCE(li.tier_disputed, ik.tier_disputed, 0) AS tier_disputed
               FROM legal_instrument li
               LEFT JOIN instrument_kind ik ON ik.kind = li.kind""",
        )
        for r in rows:
            self.instruments[r["instrument_id"]] = r
            k = norm_name(r.get("name"))
            if not k:
                continue
            prev = self.by_norm.get(k)
            if prev is None or (r.get("current_history") == "현행"
                                and self.instruments[prev].get("current_history") != "현행"):
                self.by_norm[k] = r["instrument_id"]

    def resolve(self, ident: str) -> tuple[str, bool]:
        """(실제 instrument_id, 이름해소로 바뀌었는지). 실패 시 원래 id 그대로."""
        if ident in self.instruments:
            return ident, False
        if ident.startswith("lawname:"):
            hit = self.by_norm.get(ident[len("lawname:"):])
            if hit:
                return hit, True
        return ident, False


# --------------------------------------------------------------------------- #
# 노드 만들기 (graph.build 규약과 동일한 키·라벨)
# --------------------------------------------------------------------------- #
def instrument_node(resolver: Resolver, ident: str, *, hop: int,
                    resolved_from: str | None = None, fallback_name: str | None = None) -> dict:
    row = resolver.instruments.get(ident)
    nid = node_id("instrument", ident)
    if row:
        n = {"id": nid}
        n.update(_clean_attrs(
            row, ("name", "short_name", "source_type", "competent_authority",
                  "status", "current_history", "effective_on", "repealed_on",
                  "verification_status"),
            extra={"label": _LABEL["instrument"], "kind": "instrument",
                   "instrument_kind": row.get("kind"), "src_id": ident,
                   "tier": row.get("tier"), "tier_disputed": row.get("tier_disputed"),
                   "resolved": True, "hop": hop}))
        if resolved_from:
            n["resolved_from"] = resolved_from
            n["resolved_by"] = "name-match"
        if bool(row.get("repealed_on")) or row.get("status") == "repealed":
            n["repealed"] = True
        return n
    # 미해결 이름노드: 수집 안 된 상위법. tier 를 지어내지 않는다.
    name = fallback_name or (ident[len("lawname:"):] if ident.startswith("lawname:") else ident)
    return {
        "id": nid, "label": _LABEL["instrument"], "kind": "instrument",
        "src_id": ident, "name": name, "instrument_kind": None, "tier": None,
        "status": "unknown", "resolved": False, "hop": hop,
        "note": "인용문에서 이름만 확인됨 — 법령 원문 미수집(tier·폐지여부 미상)",
    }


# 씨앗(hop0)은 전체 속성을, 주변 노드(hop≥1)는 화면·지도에 필요한 것만 싣는다.
# [실측] 전체 속성으로 140노드를 담으면 shard 가 155KB 라 510개 × 155KB = 79MB 로
# api/ 예산(50MB)을 넘긴다. 슬림화하면 같은 노드 수로 60KB 대다.
_ORD_FULL = ("name", "org_name", "ord_kind", "local_tier", "delegation_type",
             "department", "enacted_on", "effective_on", "repealed_on",
             "article_count", "status", "verification_status", "lifecycle")
_ORD_SLIM = ("name", "org_name", "ord_kind", "enacted_on", "repealed_on", "status")


def ordinance_node(row: dict, *, hop: int, region_name: str | None = None,
                   slim: bool = False) -> dict:
    n = {"id": node_id("ordinance", row["ordinance_id"])}
    extra = {"label": _LABEL["ordinance"], "kind": "ordinance",
             "region_id": row.get("region_id"), "tier": row.get("local_tier"), "hop": hop}
    if not slim:
        extra["src_id"] = row["ordinance_id"]
    n.update(_clean_attrs(row, _ORD_SLIM if slim else _ORD_FULL, extra=extra))
    # 폐지 여부는 status 로 이미 드러난다. true 일 때만 실어 용량을 아낀다.
    if bool(row.get("repealed_on")) or row.get("status") == "repealed":
        n["repealed"] = True
    if region_name and not slim:
        n["region_name"] = region_name
    return n


def region_node(row: dict, *, hop: int) -> dict:
    n = {"id": node_id("region", row["region_id"])}
    n.update(_clean_attrs(
        row, ("name", "full_name", "level", "sig_cd", "has_legislation",
              "population", "status"),
        extra={"label": _LABEL["region"], "kind": "region",
               "src_id": row["region_id"], "tier": None, "hop": hop}))
    if row.get("status") not in (None, "active"):
        n["repealed"] = True
    return n


# 검증상태 우선순위(높을수록 강함). 같은 쌍의 여러 조문 근거를 하나로 접을 때
# 가장 강한 근거를 대표로 올린다.
_VER_RANK = {"article-verified": 3, "source-linked": 2, "needs-review": 1,
             "article-missing": 1, "unverifiable": 0, "unverified": 0}

# 엣지에서 생략되는 기본값(봉투의 defaults 로 한 번만 알린다)
_DEFAULT_DELEG_TYPE = "law-delegated"
_DEFAULTS = {
    "edge.count": 1,
    "edge.delegation_type": _DEFAULT_DELEG_TYPE,
    "node.repealed": False,
    "note": "생략된 필드는 이 기본값이다. edge.count 는 접힌 조문단위 근거 건수, "
            "edge.src_articles/dst_articles 는 근거 조문 최대 3개(쉼표 구분). "
            "edge.verification_status 는 접힌 근거 중 가장 강한 것. "
            "node.resolved=false 는 인용문에서 이름만 확인된 미수집 법령이다.",
}


class EdgeBag:
    """(source, target, relation) 단위로 엣지를 접는다.

    조문 단위 위임/인용은 같은 법에 대해 수십 건씩 중복된다(실측: 씨앗 1건이
    DELEGATED_FROM 180 + CITES 167). 접지 않으면 파일이 200KB 를 넘고 vis.js 화면도
    같은 두 노드 사이 평행선 수십 개로 읽을 수 없게 된다. 접되 근거 조문과 건수를
    남겨 provenance 를 잃지 않는다.
    """

    def __init__(self) -> None:
        self._e: dict[tuple[str, str, str], dict] = {}

    def add(self, source: str, target: str, relation: str, *,
            verification_status: str | None = None, src_article: str | None = None,
            dst_article: str | None = None, citation_text: str | None = None,
            resolved_from: str | None = None, **attrs) -> None:
        key = (source, target, relation)
        e = self._e.get(key)
        if e is None:
            e = {"source": source, "target": target, "relation": relation, "_n": 0}
            if resolved_from:
                e["resolved_from"] = resolved_from
                e["resolved_by"] = "name-match"
            for k, v in attrs.items():
                if v not in (None, ""):
                    e[k] = v
            self._e[key] = e
        e["_n"] += 1
        if verification_status and _VER_RANK.get(verification_status, 0) > _VER_RANK.get(
                e.get("verification_status") or "", -1):
            e["verification_status"] = verification_status
        for field, val in (("_src_arts", src_article), ("_dst_arts", dst_article)):
            if val:
                lst = e.setdefault(field, [])
                if val not in lst and len(lst) < 3:
                    lst.append(val)
        if citation_text and "citation_text" not in e:
            e["citation_text"] = truncate(citation_text)

    def list(self) -> list[dict]:
        """직렬화 형태로 마감. 조문 목록은 배열 대신 쉼표문자열(들여쓰기 JSON 에서
        배열 한 개가 3줄을 먹는다), count 는 2 이상일 때만 싣는다."""
        out = []
        for e in self._e.values():
            n = e.pop("_n", 1)
            if n > 1:
                e["count"] = n
            for src, dst in (("_src_arts", "src_articles"), ("_dst_arts", "dst_articles")):
                arts = e.pop(src, None)
                if arts:
                    e[dst] = ",".join(arts)
            out.append(e)
        return out


def add_deleg_edge(bag: EdgeBag, r: dict, src: str, dst: str, *,
                   resolved_from: str | None = None) -> None:
    bag.add(node_id("ordinance", src), node_id("instrument", dst),
            r.get("relation") or "DELEGATED_FROM",
            verification_status=r.get("verification_status"),
            src_article=r.get("child_article"), dst_article=r.get("parent_article"),
            citation_text=r.get("citation_text"), resolved_from=resolved_from,
            # delegation_type 은 DB 전량이 'law-delegated' 다(실측 421,627/421,627).
            # 기본값은 봉투의 defaults 에 한 번만 적고, 다른 값일 때만 엣지에 싣는다.
            delegation_type=(r.get("delegation_type")
                             if r.get("delegation_type") != _DEFAULT_DELEG_TYPE else None),
            source_path=r.get("source_path"), inferred=int(r.get("inferred") or 0))


def add_cites_edge(bag: EdgeBag, r: dict, dst: str, *,
                   resolved_from: str | None = None) -> None:
    # instrument_relations 에는 검증컬럼이 없다. inferred 로만 신뢰도를 표기한다.
    bag.add(node_id("ordinance", r["src_id"]), node_id("instrument", dst),
            r.get("relation") or "CITES",
            verification_status="unverified" if int(r.get("inferred") or 0) else "source-linked",
            src_article=r.get("src_article"), citation_text=r.get("citation_text"),
            resolved_from=resolved_from, citation_type=r.get("citation_type"),
            inferred=int(r.get("inferred") or 0))


# --------------------------------------------------------------------------- #
# 사전계산 (전량 스캔 1회)
# --------------------------------------------------------------------------- #
class Degrees:
    """조례/법령별 위임·인용 차수. 대표 선정과 2홉 표본 순위에 쓴다."""

    def __init__(self, conn, resolver: Resolver):
        t0 = time.time()
        # 지역명은 556행뿐이다. shard 마다 regions 를 조인하면 8,104행 위임 질의가
        # 0.07s → 2.31s 로 33배 느려진다(실측). 한 번 올려두고 파이썬에서 붙인다.
        self.regions = {r["region_id"]: r for r in D.fetchall(conn, "SELECT * FROM regions")}
        self.deleg_child = {r["k"]: r["n"] for r in D.fetchall(
            conn, "SELECT child_id AS k, COUNT(*) AS n FROM delegations GROUP BY child_id")}
        self.deleg_parent = {r["k"]: r["n"] for r in D.fetchall(
            conn, """SELECT parent_id AS k, COUNT(DISTINCT child_id) AS n
                     FROM delegations GROUP BY parent_id""")}
        self.cites_src = {r["k"]: r["n"] for r in D.fetchall(
            conn, """SELECT src_id AS k, COUNT(*) AS n FROM instrument_relations
                     WHERE relation='CITES' GROUP BY src_id""")}
        # 인용 대상은 이름해소 후 집계해야 실제 피인용 상위법이 보인다.
        self.cites_dst: dict[str, int] = {}
        self.alias: dict[str, list[str]] = {}   # 실제 instrument_id → 붙은 lawname 키들
        for r in D.fetchall(
                conn, """SELECT dst_id AS k, COUNT(*) AS n FROM instrument_relations
                         WHERE relation='CITES' GROUP BY dst_id"""):
            real, changed = resolver.resolve(r["k"])
            self.cites_dst[real] = self.cites_dst.get(real, 0) + r["n"]
            if changed:
                self.alias.setdefault(real, []).append(r["k"])
        self.seconds = round(time.time() - t0, 1)
        self._peer_cache: dict[str, list[dict]] = {}

    def peers(self, conn, hub: str, pool: int) -> list[dict]:
        """상위법 hub 에 위임된 조례 후보 풀. shard 간 재사용(허브는 자주 겹친다)."""
        got = self._peer_cache.get(hub)
        if got is None:
            if len(self._peer_cache) > 400:   # 600행 × 수백 허브면 메모리가 샌다
                self._peer_cache.clear()
            got = D.fetchall(
                conn,
                """SELECT o.ordinance_id, o.name, o.region_id, o.org_name, o.ord_kind,
                          o.local_tier, o.delegation_type, o.enacted_on, o.effective_on,
                          o.repealed_on, o.article_count, o.status, o.verification_status,
                          o.lifecycle,
                          d.relation, d.delegation_type AS d_type, d.source_path,
                          d.verification_status AS d_ver, d.inferred, d.child_article,
                          d.parent_article, d.citation_text
                   FROM delegations d
                   JOIN ordinances o ON o.ordinance_id = d.child_id
                   WHERE d.parent_kind='instrument' AND d.parent_id = ? AND o.ord_kind='조례'
                   GROUP BY d.child_id
                   ORDER BY (o.status='active') DESC, o.region_id, o.ordinance_id
                   LIMIT ?""",
                (hub, pool),
            )
            self._peer_cache[hub] = got
        return got


# --------------------------------------------------------------------------- #
# 조례 중심 shard
# --------------------------------------------------------------------------- #
def pick_seed_ordinances(conn, deg: Degrees, args) -> list[dict]:
    """대표 조례 선정: 지역당 상위 --per-region 건(위임+인용 차수 기준).

    전국 커버리지를 우선한다. 차수 상위만 뽑으면 대도시 조례로 쏠려
    '위계 그래프' 화면이 특정 지자체 얘기가 돼 버린다.
    """
    rows = D.fetchall(
        conn,
        """SELECT o.ordinance_id, o.name, o.region_id, o.org_name, o.ord_kind, o.status,
                  r.full_name AS region_name, r.level AS region_level, r.sig_cd
           FROM ordinances o
           LEFT JOIN regions r ON r.region_id = o.region_id
           WHERE o.ord_kind = '조례' AND o.status = 'active' AND o.region_id IS NOT NULL""",
    )
    for r in rows:
        r["degree"] = deg.deleg_child.get(r["ordinance_id"], 0) + deg.cites_src.get(r["ordinance_id"], 0)
    rows = [r for r in rows if r["degree"] >= args.min_degree]
    rows.sort(key=lambda r: (-r["degree"], r["ordinance_id"]))
    # 라운드 단위로 지역당 1건씩 채운다. 한 번에 지역당 --per-region 을 채우면
    # 차수 높은 대도시가 앞자리를 먼저 다 먹어 커버 지역 수가 줄어든다
    # (실측: 한 방에 채우면 300 shard 가 208곳, 라운드로빈이면 더 넓다).
    picked: list[dict] = []
    taken: set[str] = set()
    for rnd in range(max(1, args.per_region)):
        seen_round: set[str] = set()
        for r in rows:
            if len(picked) >= args.ord_limit:
                return picked
            if r["ordinance_id"] in taken or r["region_id"] in seen_round:
                continue
            seen_round.add(r["region_id"])
            taken.add(r["ordinance_id"])
            picked.append(r)
    return picked


def _rank_instruments(deg: Degrees, resolver: Resolver, ids) -> list[str]:
    """상위법 표시 우선순위: 해소된 것 먼저, 그 다음 전국 위임/피인용 규모 순."""
    return sorted(
        ids,
        key=lambda i: (0 if i in resolver.instruments else 1,
                       -(deg.deleg_parent.get(i, 0) + deg.cites_dst.get(i, 0)), i))


def ordinance_subgraph(conn, resolver: Resolver, deg: Degrees, seed: dict, args) -> dict:
    """조례 1건의 2홉 서브그래프."""
    oid = seed["ordinance_id"]
    nodes: dict[str, dict] = {}
    bag = EdgeBag()

    def put(n: dict) -> str:
        cur = nodes.get(n["id"])
        if cur is None or n.get("hop", 9) < cur.get("hop", 9):
            nodes[n["id"]] = n
        return n["id"]

    # --- hop0: 씨앗 조례 + 소속 지자체 ---
    orow = D.fetchone(conn, "SELECT * FROM ordinances WHERE ordinance_id = ?", (oid,))
    if not orow:
        raise RuntimeError(f"조례 없음: {oid}")
    put(ordinance_node(orow, hop=0, region_name=seed.get("region_name")))
    rrow = deg.regions.get(orow.get("region_id"))
    if rrow:
        put(region_node(rrow, hop=0))
        bag.add(node_id("region", rrow["region_id"]), node_id("ordinance", oid),
                "HAS_ORDINANCE", verification_status="source-linked")

    # --- hop1: DELEGATED_FROM 상위법 / CITES 인용 ---
    drows = D.fetchall(
        conn,
        "SELECT * FROM delegations WHERE child_kind='ordinance' AND child_id = ?", (oid,))
    crows = D.fetchall(
        conn,
        """SELECT * FROM instrument_relations
           WHERE src_kind='ordinance' AND src_id = ?
             AND relation IN ('CITES','INCORPORATES_STANDARD')""",
        (oid,))

    # 씨앗이 인용하는 법이 150개를 넘는 경우가 흔하다(실측: 공유재산 관리 조례 167건).
    # 전부 실으면 노드 상한을 hop1 이 다 먹어 2홉이 사라지고 파일도 200KB 를 넘는다.
    # 중요한 상위법부터 자르고, 잘린 수는 truncated 에 남긴다.
    del_par: dict[str, list[dict]] = {}
    for r in drows:
        real, changed = resolver.resolve(r["parent_id"])
        del_par.setdefault(real, []).append(r | {"_changed": changed})
    cit_par: dict[str, list[dict]] = {}
    for r in crows:
        real, changed = resolver.resolve(r["dst_id"])
        cit_par.setdefault(real, []).append(r | {"_changed": changed})

    keep_del = _rank_instruments(deg, resolver, del_par)[: args.max_parents]
    keep_cit = _rank_instruments(deg, resolver, cit_par)[: args.max_cited]
    hop1_ids = list(dict.fromkeys(keep_del + keep_cit))[: args.max_parents + args.max_cited]

    for real in hop1_ids:
        rows = del_par.get(real) or cit_par.get(real) or []
        sample = rows[0]
        put(instrument_node(resolver, real, hop=1,
                            resolved_from=(sample["parent_id"] if "parent_id" in sample
                                           else sample["dst_id"]) if sample["_changed"] else None,
                            fallback_name=_cite_name(sample.get("citation_text"))))
        for r in del_par.get(real, []):
            add_deleg_edge(bag, r, oid, real,
                           resolved_from=r["parent_id"] if r["_changed"] else None)
        for r in cit_par.get(real, []):
            add_cites_edge(bag, r, real, resolved_from=r["dst_id"] if r["_changed"] else None)

    truncated = {
        "delegation_parents_total": len(del_par),
        "delegation_parents_shown": len(keep_del),
        "cited_instruments_total": len(cit_par),
        "cited_instruments_shown": len(keep_cit),
        "note": "상위법이 상한을 넘으면 전국 위임·피인용 규모가 큰 순으로 남긴다.",
    }

    # --- hop2: 상위법을 공유하는 다른 지자체 조례 표본 ---
    hubs = [i for i in _rank_instruments(deg, resolver, hop1_ids)
            if i in resolver.instruments and deg.deleg_parent.get(i, 0) > 1][: args.hub_parents]
    budget = args.max_nodes - len(nodes)
    truncated["hop2_budget"] = max(budget, 0)
    per_hub = max(1, budget // max(1, len(hubs))) if hubs and budget > 0 else 0
    used_regions = {orow.get("region_id")}
    for hub in hubs:
        if len(nodes) >= args.max_nodes:
            truncated["node_cap_hit"] = True
            break
        peers = deg.peers(conn, hub, args.peer_pool)
        # 지역 라운드로빈: 같은 지자체 조례로 채우지 않고 전국 확산을 보여준다.
        added = 0
        for rnd in (0, 1):
            for p in peers:
                if added >= per_hub or len(nodes) >= args.max_nodes:
                    break
                if p["ordinance_id"] == oid:
                    continue
                if rnd == 0 and p.get("region_id") in used_regions:
                    continue
                nid = node_id("ordinance", p["ordinance_id"])
                if nid in nodes:
                    continue
                put(ordinance_node(p, hop=2, slim=True))
                used_regions.add(p.get("region_id"))
                # 2홉 엣지는 '같은 상위법에 근거한다'는 사실만 보이면 된다.
                # 인용 원문은 허브 노드 이름과 중복이라 싣지 않는다(용량).
                add_deleg_edge(bag, {
                    "relation": p.get("relation"), "delegation_type": p.get("d_type"),
                    "verification_status": p.get("d_ver"), "inferred": p.get("inferred"),
                    "child_article": p.get("child_article")}, p["ordinance_id"], hub)
                added += 1
            if added >= per_hub:
                break
        truncated.setdefault("hub_peers", {})[hub] = {
            "added": added, "pool": len(peers),
            "total_children": deg.deleg_parent.get(hub, 0)}

    # 엣지 정리: 서브그래프 안에 없는 노드로 가는 엣지는 버린다(dangling 방지 — build 규칙과 동일)
    all_edges = bag.list()
    keep = [e for e in all_edges if e["source"] in nodes and e["target"] in nodes]
    dropped = len(all_edges) - len(keep)

    node_list = sorted(nodes.values(), key=lambda n: (n.get("hop", 9), n["id"]))
    stats = {
        "nodes": len(node_list),
        "edges": len(keep),
        "dropped_edges": dropped,
        "by_label": _count(node_list, "label"),
        "by_relation": _count(keep, "relation"),
        "unresolved_instruments": sum(1 for n in node_list
                                      if n["kind"] == "instrument" and not n.get("resolved")),
        "name_matched_instruments": sum(1 for n in node_list if n.get("resolved_by") == "name-match"),
        "repealed_nodes": sum(1 for n in node_list if n.get("repealed")),
        "seed_delegation_rows": len(drows),
        "seed_citation_rows": len(crows),
        "collapsed_rows": sum(e.get("count", 1) for e in keep),
        # 상한에 잘려 화면에 안 나온 것까지 포함한 '수집 안 된 상위법' 규모.
        # 표시 우선순위가 해소된 법령을 앞세우므로 이 수치 없이는 결손이 숨는다.
        "seed_unresolved_parents": sum(1 for i in del_par if i not in resolver.instruments),
        "seed_unresolved_cited": sum(1 for i in cit_par if i not in resolver.instruments),
    }
    return {
        "seed": {"id": node_id("ordinance", oid), "ordinance_id": oid,
                 "name": orow.get("name"), "org_name": orow.get("org_name"),
                 "region_id": orow.get("region_id"), "sig_cd": seed.get("sig_cd"),
                 "region_name": seed.get("region_name"), "status": orow.get("status"),
                 "repealed_on": orow.get("repealed_on"),
                 "canonical_url": orow.get("canonical_url")},
        "hops": 2, "max_nodes": args.max_nodes,
        "defaults": _DEFAULTS,
        "nodes": node_list, "edges": keep, "stats": stats, "truncated": truncated,
    }


_CITE_NAME_RE = re.compile(r"「([^」]+)」")


def _cite_name(citation_text: str | None) -> str | None:
    """인용 원문 「법령명」 에서 표시용 이름을 뽑는다(미해결 노드 라벨용)."""
    if not citation_text:
        return None
    m = _CITE_NAME_RE.search(citation_text)
    return m.group(1) if m else None


def _count(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key))
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------- #
# 법령 중심 shard
# --------------------------------------------------------------------------- #
def pick_seed_statutes(deg: Degrees, resolver: Resolver, args) -> list[str]:
    """참조 많은 상위법: 위임 자식수 상위 ∪ (이름해소 후) 피인용 상위."""
    by_deleg = sorted((i for i in deg.deleg_parent if i in resolver.instruments),
                      key=lambda i: (-deg.deleg_parent[i], i))
    by_cites = sorted((i for i in deg.cites_dst if i in resolver.instruments),
                      key=lambda i: (-deg.cites_dst[i], i))
    out: list[str] = []
    seen: set[str] = set()
    for lst in (by_deleg[: args.statute_limit], by_cites[: args.statute_limit]):
        for i in lst:
            if i not in seen:
                seen.add(i)
                out.append(i)
    return out[: args.statute_limit]


def statute_subgraph(conn, resolver: Resolver, deg: Degrees, iid: str, args) -> dict:
    nodes: dict[str, dict] = {}
    bag = EdgeBag()
    nodes_root = instrument_node(resolver, iid, hop=0)
    nodes[nodes_root["id"]] = nodes_root

    children = deg.peers(conn, iid, args.peer_pool)
    used_regions: set[str] = set()
    picked: list[dict] = []
    seen_ord: set[str] = set()
    for rnd in (0, 1):
        for p in children:
            if len(picked) >= args.statute_children:
                break
            if rnd == 0 and p.get("region_id") in used_regions:
                continue
            if p["ordinance_id"] in seen_ord:
                continue
            seen_ord.add(p["ordinance_id"])
            picked.append(p)
            used_regions.add(p.get("region_id"))
        if len(picked) >= args.statute_children:
            break
    for p in picked:
        n = ordinance_node(p, hop=1, slim=True)
        nodes[n["id"]] = n
        add_deleg_edge(bag, {
            "relation": p.get("relation"), "delegation_type": p.get("d_type"),
            "source_path": p.get("source_path"), "verification_status": p.get("d_ver"),
            "inferred": p.get("inferred"), "child_article": p.get("child_article"),
            "parent_article": p.get("parent_article")},
            p["ordinance_id"], iid)

    # 인용망: 같은 법을 인용한 조례. 이름해소 별칭(lawname:*)도 함께 조회한다.
    ids = [iid] + deg.alias.get(iid, [])
    q = ",".join("?" * len(ids))
    crows = D.fetchall(
        conn,
        f"""SELECT src_id, dst_id, relation, citation_text, citation_type, src_article, inferred
            FROM instrument_relations
            WHERE dst_kind='instrument' AND dst_id IN ({q}) AND relation='CITES'
            GROUP BY src_id
            ORDER BY src_id
            LIMIT ?""",
        (*ids, args.peer_pool),
    )
    cite_total = sum(deg.cites_dst.get(x, 0) for x in [iid])
    # 인용 조례 중 아직 노드가 없는 것만 한 번에 메타조회(행별 fetchone 금지).
    need = [r["src_id"] for r in crows if node_id("ordinance", r["src_id"]) not in nodes]
    need = need[: max(0, args.max_nodes - len(nodes))]
    ometa: dict[str, dict] = {}
    for i in range(0, len(need), 400):
        chunk = need[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for orow in D.fetchall(
                conn, f"SELECT * FROM ordinances WHERE ordinance_id IN ({ph})", tuple(chunk)):
            ometa[orow["ordinance_id"]] = orow
    added_cite = 0
    for r in crows:
        nid = node_id("ordinance", r["src_id"])
        if nid not in nodes:
            orow = ometa.get(r["src_id"])
            if not orow or len(nodes) >= args.max_nodes:
                continue
            nodes[nid] = ordinance_node(orow, hop=1, slim=True)
            added_cite += 1
        real, changed = resolver.resolve(r["dst_id"])
        add_cites_edge(bag, r, real, resolved_from=r["dst_id"] if changed else None)

    all_edges = bag.list()
    keep = [e for e in all_edges if e["source"] in nodes and e["target"] in nodes]
    node_list = sorted(nodes.values(), key=lambda n: (n.get("hop", 9), n["id"]))
    root = resolver.instruments[iid]
    return {
        "seed": {"id": nodes_root["id"], "instrument_id": iid, "name": root.get("name"),
                 "kind": root.get("kind"), "tier": root.get("tier"),
                 "tier_disputed": root.get("tier_disputed"),
                 "competent_authority": root.get("competent_authority"),
                 "status": root.get("status"), "current_history": root.get("current_history"),
                 "repealed_on": root.get("repealed_on")},
        "coverage": {
            "delegating_ordinances_total": deg.deleg_parent.get(iid, 0),
            "delegating_ordinances_shown": len(picked),
            "citing_edges_total": cite_total,
            "citing_ordinances_shown": added_cite,
            "name_match_aliases": deg.alias.get(iid, [])[:20],
            "note": "shown 은 전국 지역 라운드로빈 표본이다(전수 아님).",
        },
        "max_nodes": args.max_nodes,
        "defaults": _DEFAULTS,
        "nodes": node_list, "edges": keep,
        "stats": {"nodes": len(node_list), "edges": len(keep),
                  "by_label": _count(node_list, "label"),
                  "by_relation": _count(keep, "relation"),
                  "repealed_nodes": sum(1 for n in node_list if n.get("repealed"))},
    }


# --------------------------------------------------------------------------- #
# 위계 요약
# --------------------------------------------------------------------------- #
_TIER_LABEL = {0: "헌법", 1: "법률·조약", 2: "대통령령·헌법기관규칙",
               3: "총리령·부령", 4: "행정규칙·고시·표준",
               "L1": "조례·의회규칙", "L2": "규칙·교육규칙"}


def hierarchy_summary(conn, resolver: Resolver, deg: Degrees) -> dict:
    def rows(sql, p=()):
        return D.fetchall(conn, sql, p)

    tier_nodes = rows(
        """SELECT COALESCE(li.national_tier, ik.national_tier) AS tier,
                  li.kind, li.source_type, COUNT(*) AS n,
                  SUM(CASE WHEN li.repealed_on IS NOT NULL OR li.status='repealed' THEN 1 ELSE 0 END) AS repealed
           FROM legal_instrument li LEFT JOIN instrument_kind ik ON ik.kind = li.kind
           GROUP BY 1,2,3 ORDER BY n DESC""")
    ord_tier = rows(
        """SELECT COALESCE(local_tier, '미분류') AS tier, ord_kind, COUNT(*) AS n,
                  SUM(CASE WHEN status='repealed' OR repealed_on IS NOT NULL THEN 1 ELSE 0 END) AS repealed
           FROM ordinances GROUP BY 1,2 ORDER BY n DESC""")

    # 국가법령 축(national_tier)과 자치법규 축(local_tier)은 서로 다른 축이다.
    # 키를 축으로 네임스페이스하지 않으면 tier 미상 법령 11건과 local_tier 미상 조례
    # 40,587건이 한 칸에 섞인다.
    tiers: dict[str, dict] = {}
    for r in tier_nodes:
        key = f"nat:{r['tier']}" if r["tier"] is not None else "nat:미분류"
        t = tiers.setdefault(key, {"key": key, "tier": r["tier"],
                                   "label": _TIER_LABEL.get(r["tier"], "국가법령 tier 미상"),
                                   "axis": "national", "nodes": 0, "repealed": 0, "kinds": {}})
        t["nodes"] += r["n"]
        t["repealed"] += r["repealed"] or 0
        t["kinds"][r["kind"]] = t["kinds"].get(r["kind"], 0) + r["n"]
    for r in ord_tier:
        key = f"loc:{r['tier']}"
        t = tiers.setdefault(key, {"key": key, "tier": r["tier"],
                                   "label": _TIER_LABEL.get(r["tier"], "자치법규 tier 미상"),
                                   "axis": "local", "nodes": 0, "repealed": 0, "kinds": {}})
        t["nodes"] += r["n"]
        t["repealed"] += r["repealed"] or 0
        t["kinds"][r["ord_kind"]] = t["kinds"].get(r["ord_kind"], 0) + r["n"]

    deleg_total = D.fetchone(conn, "SELECT COUNT(*) AS n FROM delegations")["n"]
    ir_total = D.fetchone(conn, "SELECT COUNT(*) AS n FROM instrument_relations")["n"]

    # 위임 엣지: 원본 상태(해소 전) / 이름해소 후
    unresolved_deleg = D.fetchone(
        conn, "SELECT COUNT(*) AS n FROM delegations WHERE parent_id LIKE 'lawname:%'")["n"]
    unresolved_cites = D.fetchone(
        conn, "SELECT COUNT(*) AS n FROM instrument_relations "
              "WHERE relation='CITES' AND dst_id LIKE 'lawname:%'")["n"]

    # tier→tier 흐름 행렬(위임). 부모 tier 는 이름해소 후 legal_instrument 기준.
    flow: dict[str, dict[str, int]] = {}
    resolved_deleg = 0
    name_matched_deleg = 0
    for r in D.fetchall(conn, """SELECT parent_id, child_kind, COUNT(*) AS n
                                 FROM delegations GROUP BY parent_id, child_kind"""):
        real, changed = resolver.resolve(r["parent_id"])
        row = resolver.instruments.get(real)
        if row is None:
            ptier = "미해결"
        else:
            resolved_deleg += r["n"]
            if changed:
                name_matched_deleg += r["n"]
            ptier = str(row.get("tier")) if row.get("tier") is not None else "미분류"
        ctier = "L1" if r["child_kind"] == "ordinance" else "미분류"
        flow.setdefault(ptier, {})
        flow[ptier][ctier] = flow[ptier].get(ctier, 0) + r["n"]

    cites_resolved = sum(n for i, n in deg.cites_dst.items() if i in resolver.instruments)
    cites_all = sum(deg.cites_dst.values())

    return {
        "tiers": sorted(tiers.values(), key=lambda t: (t["axis"], str(t["tier"]))),
        "totals": {
            "legal_instrument": sum(t["nodes"] for t in tiers.values() if t["axis"] == "national"),
            "ordinances": sum(t["nodes"] for t in tiers.values() if t["axis"] == "local"),
            "ordinances_repealed": sum(t["repealed"] for t in tiers.values() if t["axis"] == "local"),
            "regions": len(deg.regions),
        },
        "tier_labels": {str(k): v for k, v in _TIER_LABEL.items()},
        "delegation_edges": {
            "total": deleg_total,
            "parent_unresolved_raw": unresolved_deleg,
            "parent_resolved_after_name_match": resolved_deleg,
            "gained_by_name_match": name_matched_deleg,
            "by_source_path": {f"{r['source_path']}": r["n"] for r in D.fetchall(
                conn, "SELECT source_path, COUNT(*) AS n FROM delegations GROUP BY 1 ORDER BY n DESC")},
            "by_verification_status": {f"{r['verification_status']}": r["n"] for r in D.fetchall(
                conn, "SELECT verification_status, COUNT(*) AS n FROM delegations GROUP BY 1 ORDER BY n DESC")},
            "by_delegation_type": {f"{r['delegation_type']}": r["n"] for r in D.fetchall(
                conn, "SELECT delegation_type, COUNT(*) AS n FROM delegations GROUP BY 1 ORDER BY n DESC")},
            "inferred": D.fetchone(conn, "SELECT SUM(inferred) AS n FROM delegations")["n"],
        },
        "citation_edges": {
            "total": ir_total,
            "by_relation": {f"{r['relation']}": r["n"] for r in D.fetchall(
                conn, "SELECT relation, COUNT(*) AS n FROM instrument_relations GROUP BY 1 ORDER BY n DESC")},
            "dst_unresolved_raw": unresolved_cites,
            "dst_resolved_after_name_match": cites_resolved,
            "dst_total_cites": cites_all,
            "static_bundle_included": 2189,
            "note": "정적 nodes/edges 번들은 양끝 노드가 다 있어야 엣지를 싣는다. "
                    "'lawname:*' 인용은 노드가 없어 통째로 빠져 있었다.",
        },
        "flow_parent_tier_to_child": flow,
        "top_parents": [
            {"instrument_id": i, "name": (resolver.instruments.get(i) or {}).get("name"),
             "kind": (resolver.instruments.get(i) or {}).get("kind"),
             "tier": (resolver.instruments.get(i) or {}).get("tier"),
             "delegating_ordinances": deg.deleg_parent[i],
             "citing_edges": deg.cites_dst.get(i, 0)}
            for i in sorted((x for x in deg.deleg_parent if x in resolver.instruments),
                            key=lambda x: -deg.deleg_parent[x])[:30]],
        "top_cited": [
            {"instrument_id": i, "name": (resolver.instruments.get(i) or {}).get("name"),
             "kind": (resolver.instruments.get(i) or {}).get("kind"),
             "tier": (resolver.instruments.get(i) or {}).get("tier"),
             "citing_edges": deg.cites_dst[i],
             "delegating_ordinances": deg.deleg_parent.get(i, 0)}
            for i in sorted((x for x in deg.cites_dst if x in resolver.instruments),
                            key=lambda x: -deg.cites_dst[x])[:30]],
        "unresolved_top": [
            {"lawname": i, "citing_edges": n}
            for i, n in sorted(((x, n) for x, n in deg.cites_dst.items()
                                if x not in resolver.instruments),
                               key=lambda kv: -kv[1])[:30]],
        "name_match": {
            "method": "legal_instrument.name 을 공백·중점 제거해 정규화한 키로 대조",
            "instrument_keys": len(resolver.by_norm),
            "resolved_lawnames": len(deg.alias),
            "caveat": "동명이본(연혁본)은 current_history='현행' 을 우선했다. "
                      "이름 일치는 추론이므로 엣지에 resolved_by='name-match', inferred 로 표기한다.",
        },
        "region_succession": D.fetchall(
            conn,
            """SELECT s.old_region_id, s.new_region_id, s.succession_type, s.effective_date,
                      s.legal_basis, s.status_note,
                      ro.full_name AS old_name, rn.full_name AS new_name
               FROM region_succession s
               LEFT JOIN regions ro ON ro.region_id = s.old_region_id
               LEFT JOIN regions rn ON rn.region_id = s.new_region_id
               ORDER BY s.effective_date"""),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def file_key(ident: str) -> str:
    """'ordin:2146281' → 'ordin-2146281'. Windows 파일명에 ':' 를 못 쓴다."""
    return re.sub(r"[^0-9A-Za-z._-]", "-", ident)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data"), help="출력 루트(기본 system/data)")
    ap.add_argument("--only", default=",".join(KINDS), help=f"생성 대상 {KINDS}")
    # 기본값은 api/ 용량 예산(총 90MB 상한, 현재 39.8MB)에 맞춘 것이다.
    # shard 크기는 노드 수에 거의 선형이다(실측 약 0.75KB/노드).
    ap.add_argument("--ord-limit", type=int, default=300, help="대표 조례 shard 수")
    ap.add_argument("--per-region", type=int, default=2, help="지역당 대표 조례 수")
    ap.add_argument("--min-degree", type=int, default=3, help="대표 조례 최소 차수(위임+인용)")
    ap.add_argument("--statute-limit", type=int, default=120, help="법령 shard 수")
    ap.add_argument("--statute-children", type=int, default=60, help="법령 shard 의 조례 상한")
    ap.add_argument("--max-nodes", type=int, default=80, help="shard 노드 상한(50~150)")
    ap.add_argument("--max-parents", type=int, default=20, help="조례 shard 의 위임 상위법 상한")
    ap.add_argument("--max-cited", type=int, default=20, help="조례 shard 의 인용 법령 상한")
    ap.add_argument("--hub-parents", type=int, default=4, help="2홉을 펼칠 상위법 수")
    ap.add_argument("--peer-pool", type=int, default=600, help="지역 라운드로빈 후보 풀")
    ap.add_argument("--force", action="store_true", help="기존 shard 재생성")
    a = ap.parse_args()
    a.only = {s.strip() for s in a.only.split(",") if s.strip()}

    out = Path(a.out) / "api" / "graph"
    out.mkdir(parents=True, exist_ok=True)
    cfg = get_config()
    conn = D.connect()
    print(f"DB={cfg.db_path}  →  {out}", flush=True)

    t0 = time.time()
    resolver = Resolver(conn)
    deg = Degrees(conn, resolver)
    print(f"[사전계산] instrument {len(resolver.instruments)}건 · 정규화키 {len(resolver.by_norm)}개 · "
          f"위임부모 {len(deg.deleg_parent)} · 인용대상 {len(deg.cites_dst)} "
          f"(이름해소 {len(deg.alias)}건) · {deg.seconds}s", flush=True)

    report = {
        "generator": "make_graph_fixtures.py",
        "as_of_date": time.strftime("%Y-%m-%d"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+0900"),
        "params": {k: v for k, v in vars(a).items() if k != "only"} | {"only": sorted(a.only)},
        "layout": {"ordinance": "graph/ordinance/{key}.json",
                   "statute": "graph/statute/{key}.json",
                   "hierarchy": "graph/hierarchy.json"},
        "name_match": {"resolved_lawnames": len(deg.alias),
                       "cites_edges_unlocked": sum(
                           n for i, n in deg.cites_dst.items()
                           if i in resolver.instruments and deg.alias.get(i))},
        "ordinances": [], "statutes": [], "errors": [], "warnings": [],
    }

    # --- 1) 조례 shard ---
    if "ordinance" in a.only:
        seeds = pick_seed_ordinances(conn, deg, a)
        print(f"[조례] 대표 {len(seeds)}건 (지역 {len({s['region_id'] for s in seeds})}곳)", flush=True)
        for i, s in enumerate(seeds, 1):
            key = file_key(s["ordinance_id"])
            path = out / "ordinance" / f"{key}.json"
            size = None if a.force else existing(path)
            item = {"key": key, "ordinance_id": s["ordinance_id"], "name": s["name"],
                    "region_id": s["region_id"], "sig_cd": s.get("sig_cd"),
                    "region_name": s.get("region_name"),
                    "degree": s["degree"], "path": f"ordinance/{key}.json"}
            if size is not None:
                item |= {"bytes": size, "reused": True}
            else:
                try:
                    sub = ordinance_subgraph(conn, resolver, deg, s, a)
                except Exception as e:  # noqa: BLE001
                    msg = f"{type(e).__name__}: {str(e)[:120]}"
                    report["errors"].append({"kind": "ordinance", "id": s["ordinance_id"], "error": msg})
                    print(f"  [FAIL] {key} — {msg}", flush=True)
                    continue
                size = write_shard(path, envelope(sub, seed=s["ordinance_id"], subgraph="ordinance"))
                item |= {"bytes": size, "nodes": sub["stats"]["nodes"],
                         "edges": sub["stats"]["edges"],
                         "unresolved": sub["stats"]["unresolved_instruments"]}
            report["ordinances"].append(item)
            if size > 200 * 1024:
                report["warnings"].append({"kind": "ordinance", "key": key,
                                           "message": f"{human(size)} > 200KB 상한"})
            if i % 25 == 0 or i == len(seeds):
                print(f"  [{i}/{len(seeds)}] {key} {human(size)}", flush=True)

    # --- 2) 법령 shard ---
    if "statute" in a.only:
        sids = pick_seed_statutes(deg, resolver, a)
        print(f"[법령] 상위 {len(sids)}건", flush=True)
        for i, iid in enumerate(sids, 1):
            key = file_key(iid)
            path = out / "statute" / f"{key}.json"
            size = None if a.force else existing(path)
            row = resolver.instruments[iid]
            item = {"key": key, "instrument_id": iid, "name": row.get("name"),
                    "kind": row.get("kind"), "tier": row.get("tier"),
                    "delegating_ordinances": deg.deleg_parent.get(iid, 0),
                    "citing_edges": deg.cites_dst.get(iid, 0),
                    "path": f"statute/{key}.json"}
            if size is not None:
                item |= {"bytes": size, "reused": True}
            else:
                try:
                    sub = statute_subgraph(conn, resolver, deg, iid, a)
                except Exception as e:  # noqa: BLE001
                    msg = f"{type(e).__name__}: {str(e)[:120]}"
                    report["errors"].append({"kind": "statute", "id": iid, "error": msg})
                    print(f"  [FAIL] {key} — {msg}", flush=True)
                    continue
                size = write_shard(path, envelope(sub, seed=iid, subgraph="statute"))
                item |= {"bytes": size, "nodes": sub["stats"]["nodes"], "edges": sub["stats"]["edges"]}
            report["statutes"].append(item)
            if size > 200 * 1024:
                report["warnings"].append({"kind": "statute", "key": key,
                                           "message": f"{human(size)} > 200KB 상한"})
            if i % 25 == 0 or i == len(sids):
                print(f"  [{i}/{len(sids)}] {key} {human(size)}", flush=True)

    # --- 3) 위계 요약 ---
    if "hierarchy" in a.only:
        path = out / "hierarchy.json"
        size = None if a.force else existing(path)
        if size is None:
            h = hierarchy_summary(conn, resolver, deg)
            size = write_json(path, envelope(h, subgraph="hierarchy"))
        report["hierarchy"] = {"path": "hierarchy.json", "bytes": size}
        print(f"[위계] hierarchy.json {human(size)}", flush=True)

    # --- 4) index ---
    report["totals"] = {
        "ordinance_shards": len(report["ordinances"]),
        "statute_shards": len(report["statutes"]),
        "bytes": sum(x.get("bytes") or 0 for x in report["ordinances"] + report["statutes"])
                 + (report.get("hierarchy") or {}).get("bytes", 0),
        "seconds": round(time.time() - t0, 1),
    }
    idx = out / "index.json"
    idx.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[index] {idx} {human(idx.stat().st_size)}", flush=True)
    print(f"완료 · shard {report['totals']['ordinance_shards']}+{report['totals']['statute_shards']}"
          f" · 합계 {human(report['totals']['bytes'])} · {report['totals']['seconds']}s"
          f" · 오류 {len(report['errors'])} 경고 {len(report['warnings'])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
