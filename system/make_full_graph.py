#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""실 DB → **그래프 shard 전량 확대**. make_graph_fixtures.py 의 엔진을 그대로 쓴다.

make_graph_fixtures.py 는 대표 조례 300건 + 상위법 120건만 구웠다. 화면에서 어느
지역을 골라도 "그 지역엔 shard 가 없다"가 되는 게 문제였다. 이 스크립트가 두 가지를
더 굽는다.

  1) **지역 묶음 서브그래프** api/graph/by-region/{sig_cd}.json  (247곳)
     그 지역 조례 전체(폐지 포함) + 그들이 근거한 상위법 + DELEGATED_FROM 엣지.
     조례 19.9만 건을 건별 shard 로 만들면 파일이 20만 개다. 지역으로 묶으면 247개로
     끝나고, 어느 지역을 골라도 그 지역의 위임 구조 전체가 한 번에 온다.
     [실측] 지역당 조례 중앙값 429건 · 위임쌍 중앙값 1,283건 · 최대 4,852건.

  2) **조례 개별 shard 확대** api/graph/ordinance/{key}.json  (300 → --ord-limit)
     기존 파일은 그대로 두고(재개) 모자란 만큼만 추가한다.

왜 지역 묶음에서 노드를 더 깎았나
---------------------------------
조례 개별 shard 는 노드 80개짜리라 전체 속성을 실어도 38KB 다. 지역 묶음은 노드가
최대 2,800개(조례 1,300 + 상위법 1,500)라 같은 밀도면 1.3MB 가 된다. 그래서
지역 묶음에서는

  - 조례 노드: 지역 안에서 전부 같은 값인 필드(region_id·local_tier·org_name)를 빼고
    봉투의 region/defaults 에 한 번만 적는다.
  - 상위법 노드: 소관부처·시행일·연혁 같은 상세를 뺀다(개별 shard 에 다 있다).
  - 엣지: relation 과 최빈 verification_status 를 defaults 로 접는다.
  - 인용(CITES)은 싣지 않는다. 지역 묶음의 주제는 '위임 구조'다.

  - 상위법 노드 자체를 지역 파일에서 빼 **공용 사전** graph/instruments.json 으로 옮긴다.
    같은 법이 지역마다 반복되기 때문이다 — [실측] 지역 파일에 실릴 상위법 노드는 전국
    147,634개인데 서로 다른 법은 31,877개(4.6배 중복). 사전으로 빼면 지역 파일 합계가
    44.9MB → 26.8MB, 사전이 5.3MB 다. --inline-instruments 로 옛 방식(파일 하나로 완결,
    합계 44.9MB)으로 되돌릴 수 있다.
  - 엣지를 노드 배열 인덱스 쌍으로 쓴다(109B → 12B). data.edge_encoding 에 규약을 적었다.

전량이 아니라 상한을 두는 경우
------------------------------
--region-ord-cap / --region-parent-cap / --region-edge-cap 을 넘는 지역은 위임 차수
상위부터 남기고, 잘린 규모를 data.truncated 에 실어 결손을 숨기지 않는다.
[실측] 기본 상한(1300/1400/4600)에서 절삭되는 곳은 247곳 중 경기도(-169 조례)와
제주(-6 조례, -59 위임쌍) 둘뿐이다 — 조례 117,201건 중 175건(0.15%). 상한을 풀면
절삭은 0 이지만 경기도 파일이 325KB 로 300KB 규율을 넘는다.

출력
----
    by-region/{sig_cd}.json   지역 묶음 서브그래프 (247곳, 26.8MB, 최대 298KB)
    instruments.json          지역 묶음이 참조하는 상위법 공용 사전 (32,727건, 5.3MB)
    ordinance/{key}.json      조례 개별 shard (make_graph_fixtures 와 동일 스키마)
    index.json                기존 index 를 읽어 regions/ordinances 를 갱신하고
                              statutes/hierarchy 항목은 보존한다(덮어쓰지 않는다).

엔진 재사용(중복 구현 금지)
    make_graph_fixtures.Resolver/Degrees/ordinance_subgraph/pick_seed_ordinances
                                              이름해소·차수·조례 서브그래프
    make_graph_fixtures.EdgeBag/write_shard   엣지 접기·원자적 쓰기+키 살균
    make_gap_fixtures.envelope                응답 봉투
    make_nationwide.existing/human            재개·단위표기
    graph.build.node_id/_LABEL/_clean_attrs   노드키·라벨 규약

사용:
  cd system
  python make_full_graph.py --only by-region --regions 47190,11110,41000   # 소규모 시험
  python make_full_graph.py --only by-region                               # 247곳
  python make_full_graph.py --only ordinance --ord-limit 1000              # 조례 확대
  python make_full_graph.py                                                # 둘 다
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:  # 콘솔이 cp949 여도 한글 법령명 출력에서 죽지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from policymap import db as D                                     # noqa: E402
from policymap.config import get_config                           # noqa: E402
from policymap.graph.build import _LABEL, _clean_attrs, node_id   # noqa: E402

from make_gap_fixtures import envelope                            # noqa: E402
from make_more_fixtures import _sanitize_keys                     # noqa: E402
from make_nationwide import existing, human                       # noqa: E402
from make_graph_fixtures import (                                 # noqa: E402
    Degrees, EdgeBag, Resolver, file_key, instrument_node,
    ordinance_subgraph, pick_seed_ordinances, write_shard,
)

KINDS = ("by-region", "ordinance")

# --------------------------------------------------------------------------- #
# 지역 묶음 서브그래프
# --------------------------------------------------------------------------- #

# 지역 안에서 값이 같거나(region_id·local_tier·org_name) 개별 shard 에 이미 있는 필드는
# 노드에서 뺀다. 노드 2,800개 × 30바이트만 아껴도 파일당 80KB 다.
_REG_ORD_FIELDS = ("name", "ord_kind", "enacted_on", "repealed_on", "status")

# 상위법 노드에서 뺄 필드. instrument_node() 가 만든 것을 깎아 쓴다(중복 구현 금지).
# resolved_by 는 '이름해소 추정'이라는 표기라서 남긴다. resolved_from(원문 lawname)은
# 길고 개별 shard 에 있으므로 뺀다.
_REG_INST_DROP = ("short_name", "source_type", "competent_authority", "current_history",
                  "effective_on", "repealed_on", "verification_status", "src_id",
                  "resolved_from", "tier_disputed", "note", "hop", "label")

# 위임 엣지의 최빈 검증상태. [실측] unverifiable 210,534 / article-verified 182,784 /
# article-missing 28,309. 최빈값을 defaults 로 접으면 엣지 절반에서 40바이트가 빠진다.
# inferred 도 마찬가지다 — 420,338/421,627 이 1 이라 0 일 때만 싣는다.
_REG_DEFAULT_VER = "unverifiable"
_REG_RELATION = "DELEGATED_FROM"
_REG_DEFAULT_STATUS = "active"
_REG_DEFAULT_ORD_KIND = "조례"

# 엣지 검증상태 코드표. 배열 엣지의 4번째 자리에 이 인덱스가 들어간다.
_REG_VER_CODES = ["unverifiable", "article-verified", "article-missing", "source-linked",
                  "needs-review", "unverified"]
_REG_VER_IDX = {v: i for i, v in enumerate(_REG_VER_CODES)}

# 엣지 인코딩 규약. 지역 묶음은 엣지가 최대 4,852개라 {"source":…,"target":…} 객체로
# 쓰면 엣지만 500KB 다(실측 109B/엣지). 노드 배열 인덱스 쌍으로 쓰면 11B/엣지가 된다.
# [실측] 전국 36만 엣지에서 40MB → 4MB.
_REG_EDGE_ENCODING = {
    "format": "[source_index, target_index, count, verification_status_index]",
    "index_into": "data.nodes 와 data.instruments 를 이어붙인 배열의 위치(0-based). "
                  "즉 i < data.stats.ordinance_nodes 면 data.nodes[i], 아니면 "
                  "data.instruments[i - data.stats.ordinance_nodes] 다. "
                  "data.instruments 가 없으면(--inline-instruments) 전부 data.nodes 위치다.",
    "relation": _REG_RELATION,
    "count": "접힌 조문단위 위임 근거 건수. 생략되면 1.",
    "verification_status": _REG_VER_CODES,
    "trailing_defaults": "뒤쪽 원소는 기본값이면 생략된다 — count=1, verification_status_index=0"
                         f"(={_REG_DEFAULT_VER}). 그래서 [3,517] 은 "
                         f"[3,517,1,0] 과 같다.",
    "decode_js": "edges.map(([s,t,c=1,v=0])=>({source:nodes[s].id,target:nodes[t].id,"
                 "relation:'DELEGATED_FROM',count:c,"
                 "verification_status:defaults['edge.verification_status_codes'][v]}))",
}

_REG_DEFAULTS = {
    "edge.relation": _REG_RELATION,
    "edge.verification_status": _REG_DEFAULT_VER,
    "edge.verification_status_codes": _REG_VER_CODES,
    "edge.count": 1,
    "edge.inferred": 1,
    "node.label": {"ordinance": _LABEL["ordinance"], "instrument": _LABEL["instrument"]},
    "node.hop": {"ordinance": 0, "instrument": 1},
    "node.status": _REG_DEFAULT_STATUS,
    "node.ord_kind": _REG_DEFAULT_ORD_KIND,
    "node.resolved": True,
    "node.repealed": False,
    "note": "생략된 필드는 이 기본값이다(kind 로 갈리는 것은 kind별 값). 조례 노드의 "
            "region_id·local_tier·org_name 은 data.region 에 한 번만 적었고, 조례명 앞의 "
            "지자체 이름(data.region.name_prefix)도 잘라냈다 — 붙여서 읽으면 원래 이름이다. "
            "node.parents 는 이 지역 안에서 그 조례가 근거한 상위법 수, node.children 은 "
            "이 지역에서 그 법에 근거한 조례 수다. node.resolved=false 는 인용문에서 이름만 "
            "확인된 미수집 법령이다. data.name_matched 는 법령명 정규화로 붙인 추정(name-match) "
            "상위법의 data.instruments 위치다 — 단정이 아니라 추정으로 표기할 것. 엣지는 data.edge_encoding 규약의 인덱스 배열이다. 인용(CITES)과 "
            "조문 단위 근거는 이 파일에 없다 — 지역 묶음은 위임 구조만 담는다. 조문 단위 "
            "근거는 graph/ordinance/{key}.json 에 있다.",
}


_REG_DICT_REF = "graph/instruments.json"


def instrument_dictionary(resolver: Resolver, deg: Degrees) -> dict:
    """지역 묶음이 참조하는 상위법 사전.

    같은 상위법이 지역마다 반복된다 — [실측] 지역 파일에 실리는 상위법 노드는 전국
    147,634개인데 서로 다른 법은 31,877개뿐(4.6배 중복)이고, 그 중복이 지역 파일 합계
    44.9MB 중 23.8MB 를 먹었다. 사전으로 한 번만 싣고 지역 파일은 id 만 들고 있게 하면
    합계가 25.6MB 로 떨어진다. 브라우저도 지역을 바꿔가며 봐도 사전은 한 번만 받는다.

    수록 범위는 delegations 의 parent_id 전량이다(이름해소 후). 지역 shard 를 재개로
    건너뛰어도 사전은 항상 완전하다.
    """
    entries: dict[str, dict] = {}
    unresolved = 0
    for pid in deg.deleg_parent:
        real, changed = resolver.resolve(pid)
        nid = node_id("instrument", real)
        if nid in entries:
            continue
        # resolved_by 는 사전에 싣지 않는다. 이름해소는 '이 법 자체의 성질'이 아니라
        # '그 지역 위임행이 이 법에 어떻게 붙었나'의 성질이라, 같은 법이 어떤 지역에서는
        # 직접 id 로, 다른 지역에서는 법령명 매칭으로 붙는다. 지역 파일의
        # data.name_matched 가 지역별로 알린다.
        n = region_instrument_node(resolver, real, children=0, resolved=False)
        n.pop("id", None)
        n.pop("kind", None)             # 사전 항목은 전부 instrument 다
        if not resolver.instruments.get(real):
            unresolved += 1
        entries[nid] = n
    return {
        "instruments": entries,
        "stats": {"instruments": len(entries), "unresolved": unresolved,
                  "delegation_parents": len(deg.deleg_parent),
                  "name_matched": len(deg.alias)},
        "defaults": {"node.kind": "instrument", "node.label": _LABEL["instrument"],
                     "node.status": _REG_DEFAULT_STATUS, "node.resolved": True,
                     "note": "graph/by-region/{sig_cd}.json 의 data.instruments 에 들어 있는 "
                             "id 를 이 표에서 찾아 노드 속성을 채운다. resolved=false 는 "
                             "인용문에서 이름만 확인된 미수집 법령이다(tier·폐지여부 미상). "
                             "법령명 정규화로 붙인 추정(name-match)인지는 법마다가 아니라 "
                             "지역마다 다르므로 지역 파일의 data.name_matched 가 알린다."},
    }


class RegionData:
    """지역별 조례·위임 행을 **전량 1회 스캔**으로 올려둔다.

    지역별로 질의하면 delegations 를 매번 훑는다 — 1곳당 3.5초다(실측: 구미시 3.58s,
    종로구 3.52s. child_id 인덱스로 쪼개도 2.7s). 247곳이면 15분이 인덱스 탐색으로 간다.
    전량 순차 스캔은 조례 0.9s + 위임 1.6s 로 끝난다(실측). 메모리는 약 120MB.

    --no-preload 면 지역마다 질의하는 옛 경로로 떨어진다(메모리가 부족한 기계용).
    """

    ORD_SQL = ("SELECT region_id, ordinance_id, name, ord_kind, local_tier, org_name, "
               "enacted_on, repealed_on, status FROM ordinances")
    DEL_SQL = ("SELECT child_id, parent_id, verification_status FROM delegations "
               "WHERE child_kind='ordinance' AND parent_kind='instrument'")

    def __init__(self, conn, region_ids: set[str], *, preload: bool = True):
        self.enabled = preload
        self.ords: dict[str, dict[str, dict]] = {}
        self.dels: dict[str, list[dict]] = {}
        if not preload:
            self.seconds = 0.0
            return
        t0 = time.time()
        of = ("ordinance_id", "name", "ord_kind", "local_tier", "org_name",
              "enacted_on", "repealed_on", "status")
        owner: dict[str, str] = {}
        for row in conn.execute(self.ORD_SQL):
            rid = row[0]
            if rid not in region_ids:
                continue
            oid = row[1]
            owner[oid] = rid
            self.ords.setdefault(rid, {})[oid] = dict(zip(of, row[1:]))
        for child, parent, ver in conn.execute(self.DEL_SQL):
            rid = owner.get(child)
            if rid is None:
                continue
            self.dels.setdefault(rid, []).append(
                {"child_id": child, "parent_id": parent, "verification_status": ver})
        self.seconds = round(time.time() - t0, 1)

    def get(self, conn, rid: str) -> tuple[dict[str, dict], list[dict]]:
        if self.enabled:
            return self.ords.get(rid, {}), self.dels.get(rid, [])
        orows = {r["ordinance_id"]: r for r in D.fetchall(
            conn,
            """SELECT ordinance_id, name, ord_kind, local_tier, org_name, enacted_on,
                      repealed_on, status FROM ordinances WHERE region_id = ?""", (rid,))}
        drows = D.fetchall(
            conn,
            """SELECT d.child_id, d.parent_id, d.verification_status
               FROM delegations d JOIN ordinances o ON o.ordinance_id = d.child_id
               WHERE d.child_kind = 'ordinance' AND d.parent_kind = 'instrument'
                 AND o.region_id = ?""", (rid,))
        return orows, drows


def best_prefix(rows, reg: dict) -> str | None:
    """조례명에서 잘라낼 지자체 접두를 고른다.

    후보는 세 가지고 지역마다 다르다 — ordinances.org_name 은 '경상북도 구미시'인데
    조례명은 '구미시 …'로 시작한다(실측). 가장 많이 맞는 후보를 쓴다. 하나도 안 맞으면
    None 이라 아무것도 자르지 않는다.
    """
    cands = [c for c in (_mode(rows, "org_name"), reg.get("full_name"), reg.get("name")) if c]
    best, best_hit = None, 0
    for c in dict.fromkeys(cands):
        hit = sum(1 for r in rows if (r.get("name") or "").startswith(c + " "))
        if hit > best_hit:
            best, best_hit = c, hit
    return best


def strip_prefix(name: str | None, prefix: str | None) -> str | None:
    """조례명 앞의 지자체 이름을 잘라낸다.

    '구미시 관급공사의 …' 처럼 지역 조례는 거의 전부 지자체명으로 시작한다. 지역 묶음
    파일에서는 전부 같은 지역이라 이 접두가 노드 11.7만 개에 걸쳐 반복될 뿐이다.
    [실측] 조례 노드 평균 160B → 128B. 잘라낸 접두는 region.name_prefix 로 알린다.
    """
    if not name or not prefix:
        return name
    if name.startswith(prefix + " "):
        return name[len(prefix) + 1:]
    return name


def region_ordinance_node(row: dict, *, parents: int, prefix: str | None = None) -> dict:
    """지역 묶음용 슬림 조례 노드.

    label·hop·status·ord_kind 는 대부분 같은 값이라 defaults 로 접는다. 노드 11.7만 개에서
    필드 하나가 20바이트면 2.3MB 다.
    """
    n = {"id": node_id("ordinance", row["ordinance_id"])}
    n.update(_clean_attrs(row, _REG_ORD_FIELDS, extra={"kind": "ordinance"}))
    if prefix:
        n["name"] = strip_prefix(n.get("name"), prefix)
    if n.get("status") == _REG_DEFAULT_STATUS:
        n.pop("status")
    if n.get("ord_kind") == _REG_DEFAULT_ORD_KIND:
        n.pop("ord_kind")
    if bool(row.get("repealed_on")) or row.get("status") == "repealed":
        n["repealed"] = True
    if parents > 1:
        n["parents"] = parents          # 이 지역 안에서 이 조례가 근거한 상위법 수
    return n


def region_instrument_node(resolver: Resolver, ident: str, *, children: int,
                           resolved: bool) -> dict:
    """지역 묶음용 슬림 상위법 노드. instrument_node() 산출을 깎아 쓴다."""
    n = instrument_node(resolver, ident, hop=1,
                        resolved_from=ident if resolved else None)
    for k in _REG_INST_DROP:
        n.pop(k, None)
    # 미해결 노드는 instrument_kind/tier 가 명시적 None 이다(지어내지 않는다는 뜻).
    # JSON 으로 굳이 null 을 쓸 필요는 없다 — 없으면 미상이다. [실측] 미해결 3만 건 × 37B.
    for k in [k for k, v in n.items() if v is None]:
        n.pop(k)
    if n.get("status") == _REG_DEFAULT_STATUS:
        n.pop("status")
    if n.get("resolved") is True:
        n.pop("resolved")               # 기본값. false 일 때만 남겨 결손을 드러낸다.
    if children > 1:
        n["children"] = children        # 이 지역에서 이 법에 근거한 조례 수
    return n


def region_subgraph(conn, resolver: Resolver, deg: Degrees, reg: dict, args,
                    data: RegionData) -> dict:
    """지역 1곳의 위임 서브그래프(조례 전체 + 상위법 + DELEGATED_FROM)."""
    rid = reg["region_id"]
    orows, drows = data.get(conn, rid)

    # --- 이름해소 후 (조례, 상위법) 쌍으로 접는다 ---
    pairs: dict[tuple[str, str], list[dict]] = {}
    changed_of: dict[str, str] = {}     # 해소된 실제 id → 원래 lawname 키
    for r in drows:
        if r["child_id"] not in orows:
            continue                     # 지역이 다른 조례를 가리키는 행(있으면 버린다)
        real, changed = resolver.resolve(r["parent_id"])
        if changed:
            changed_of[real] = r["parent_id"]
        pairs.setdefault((r["child_id"], real), []).append(r)

    ord_parents: dict[str, set[str]] = {}
    par_children: dict[str, set[str]] = {}
    for oid, pid in pairs:
        ord_parents.setdefault(oid, set()).add(pid)
        par_children.setdefault(pid, set()).add(oid)

    total_ords, total_pars, total_pairs = len(ord_parents), len(par_children), len(pairs)

    # --- 상한 적용: 위임 차수 상위부터 남긴다 ---
    keep_ord = sorted(ord_parents, key=lambda o: (-len(ord_parents[o]), o))[: args.region_ord_cap]
    keep_ord_set = set(keep_ord)
    # 상위법은 '해소된 것 먼저 → 이 지역에서의 자식 수 → 전국 위임 규모' 순.
    cand_par = {p for o in keep_ord for p in ord_parents[o]}
    keep_par = sorted(
        cand_par,
        key=lambda p: (0 if p in resolver.instruments else 1,
                       -len(par_children[p] & keep_ord_set),
                       -deg.deleg_parent.get(p, 0), p))[: args.region_parent_cap]
    keep_par_set = set(keep_par)

    # --- 엣지 접기 ---
    bag = EdgeBag()
    kept_pairs = 0
    # 엣지 상한에 걸릴 때 무엇이 남는지가 결정적이어야 재실행 결과가 같다.
    # 차수 높은 조례 → 자식 많은 상위법 순으로 남긴다.
    for oid, pid in sorted(pairs, key=lambda k: (-len(ord_parents[k[0]]), k[0],
                                                 -len(par_children[k[1]]), k[1])):
        if oid not in keep_ord_set or pid not in keep_par_set:
            continue
        if kept_pairs >= args.region_edge_cap:
            break
        kept_pairs += 1
        for r in pairs[(oid, pid)]:
            bag.add(node_id("ordinance", oid), node_id("instrument", pid), _REG_RELATION,
                    verification_status=r.get("verification_status"),
                    resolved_from=changed_of.get(pid))

    folded = bag.list()
    used_o = {e["source"] for e in folded}
    used_p = {e["target"] for e in folded}

    # --- 노드 (조례 먼저, 상위법 나중 — 엣지 인덱스가 이 순서를 가리킨다) ---
    prefix = best_prefix(list(orows.values()), reg) if args.region_strip_prefix else None
    nodes: list[dict] = []
    index: dict[str, int] = {}
    for oid in sorted(keep_ord_set):
        nid = node_id("ordinance", oid)
        if nid not in used_o:
            continue
        index[nid] = len(nodes)
        nodes.append(region_ordinance_node(orows[oid], parents=len(ord_parents[oid]),
                                           prefix=prefix))
    n_ord = len(nodes)
    # 상위법은 사전(graph/instruments.json)으로 뺄지, 파일 안에 그대로 실을지 고른다.
    # [실측] 상위법 노드는 전국 147,634개인데 실제로 서로 다른 법은 31,877개뿐이다(4.6배 중복).
    # 사전으로 빼면 지역 파일 합계가 44.9MB → 25.6MB, 사전이 별도 4~5MB 다.
    instruments: list[str] = []
    for pid in sorted(keep_par_set):
        nid = node_id("instrument", pid)
        if nid not in used_p:
            continue
        index[nid] = n_ord + len(instruments) if args.region_dict else len(nodes)
        if args.region_dict:
            instruments.append(nid)
        else:
            nodes.append(region_instrument_node(
                resolver, pid, children=len(par_children[pid] & keep_ord_set),
                resolved=pid in changed_of))
    n_par = len(instruments) if args.region_dict else len(nodes) - n_ord

    # --- 엣지를 인덱스 배열로 마감 ---
    edges: list[list] = []
    collapsed = 0
    for e in folded:
        row = [index[e["source"]], index[e["target"]],
               int(e.get("count", 1)),
               _REG_VER_IDX.get(e.get("verification_status") or _REG_DEFAULT_VER, 0)]
        collapsed += row[2]
        while len(row) > 2 and row[-1] == (1 if len(row) == 3 else 0):
            row.pop()
        edges.append(row)

    shown_par = [p for p in sorted(keep_par_set) if node_id("instrument", p) in used_p]
    stats = {
        "nodes": n_ord + n_par, "edges": len(edges),
        "ordinance_nodes": n_ord, "instrument_nodes": n_par,
        "unresolved_instruments": sum(1 for p in shown_par if p not in resolver.instruments),
        "name_matched_instruments": sum(1 for p in shown_par if p in changed_of),
        "repealed_nodes": sum(1 for n in nodes if n.get("repealed")),
        "delegation_rows": len(drows),
        "collapsed_rows": collapsed,
    }
    truncated = {
        "ordinances_total": total_ords, "ordinances_shown": n_ord,
        "parents_total": total_pars, "parents_shown": n_par,
        "pairs_total": total_pairs, "pairs_shown": kept_pairs,
        "region_ordinances_all": len(orows),
        "ordinances_without_delegation": len(orows) - total_ords,
        "unresolved_parents_total": sum(1 for p in par_children if p not in resolver.instruments),
        "note": "상한을 넘으면 위임 차수 상위부터 남긴다. ordinances_without_delegation 은 "
                "위임 근거가 수집되지 않은 조례(자치조례 포함)라 이 그래프에 노드가 없다.",
    }
    out = {
        "region": {"sig_cd": reg.get("sig_cd"), "region_id": rid, "name": reg.get("name"),
                   "full_name": reg.get("full_name"), "level": reg.get("level"),
                   "status": reg.get("status"), "population": reg.get("population"),
                   "local_tier": _mode(orows.values(), "local_tier"),
                   "org_name": _mode(orows.values(), "org_name"),
                   "name_prefix": prefix},
        "relation": _REG_RELATION,
        "edge_encoding": _REG_EDGE_ENCODING,
        "defaults": _REG_DEFAULTS,
        "nodes": nodes, "edges": edges, "stats": stats, "truncated": truncated,
    }
    if args.region_dict:
        out["instruments"] = instruments
        out["instruments_ref"] = _REG_DICT_REF
        # 이름해소로 붙인 상위법의 data.instruments 위치. '추정' 배지를 띄우는 근거다.
        out["name_matched"] = [i for i, nid in enumerate(instruments)
                               if nid.split(":", 1)[1] in changed_of]
    return out


def _mode(rows, key: str):
    """가장 흔한 값(지역 안에서 상수인 필드를 봉투로 올릴 때 쓴다)."""
    c: dict = {}
    for r in rows:
        v = r.get(key)
        if v is not None:
            c[v] = c.get(v, 0) + 1
    return max(c, key=c.get) if c else None


def pick_regions(conn, args) -> list[dict]:
    """대상 지역. 기본 = level 1·2 중 위임 근거가 1건 이상 있는 곳(실측 247곳).

    폐지·통합된 지자체(광주광역시·전라남도·창원시·청원군)도 조례를 갖고 있어 포함한다.
    status 를 봉투에 실어 화면이 폐지 경고를 띄울 수 있게 한다.
    """
    levels = [int(s) for s in args.levels.split(",") if s.strip()]
    ph = ",".join("?" for _ in levels)
    rows = D.fetchall(
        conn,
        f"""SELECT region_id, sig_cd, name, full_name, level, status, population
            FROM regions WHERE sig_cd IS NOT NULL AND level IN ({ph})
            ORDER BY level, sig_cd""", levels)
    if args.regions:
        want = {s.strip() for s in args.regions.split(",") if s.strip()}
        rows = [r for r in rows if r["sig_cd"] in want or r["region_id"] in want]
        return rows
    have = {r["rid"] for r in D.fetchall(
        conn,
        """SELECT o.region_id AS rid FROM delegations d
           JOIN ordinances o ON o.ordinance_id = d.child_id
           WHERE d.child_kind = 'ordinance' GROUP BY o.region_id""")}
    return [r for r in rows if r["region_id"] in have]


# --------------------------------------------------------------------------- #
# index.json 병합
# --------------------------------------------------------------------------- #
def merge_index(path: Path, report: dict) -> int:
    """기존 index.json 의 statutes/hierarchy/name_match 를 보존하고 갱신분만 덮는다.

    make_graph_fixtures.py 가 만든 index 를 통째로 날리면 법령 shard 120개가 색인에서
    사라진다. 이 스크립트가 굽지 않은 종류는 그대로 물려받는다.
    """
    prev = {}
    if path.exists():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            prev = {}
    # 재개로 건너뛴 항목은 bytes 만 안다(서브그래프를 안 만들었으니 nodes/edges 를 모른다).
    # 이전 index 의 같은 항목을 밑에 깔아 노드·엣지 수를 잃지 않는다.
    report = dict(report)
    for key, idk in (("regions", "sig_cd"), ("ordinances", "key")):
        old = {x.get(idk): x for x in (prev.get(key) or []) if x.get(idk)}
        report[key] = [(old.get(x.get(idk), {}) | x) if x.get("reused") else x
                       for x in report.get(key) or []]
    merged = dict(prev)
    merged.update({k: v for k, v in report.items() if v not in (None, [], {})})
    for key in ("statutes", "hierarchy"):
        if not report.get(key) and prev.get(key):
            merged[key] = prev[key]
    merged["layout"] = (prev.get("layout") or {}) | report["layout"]
    merged["generators"] = sorted({*(prev.get("generators") or []),
                                   prev.get("generator", "make_graph_fixtures.py"),
                                   "make_full_graph.py"} - {None})
    # 이번 실행이 굽지 않은 종류(--only)의 합계는 병합된 목록에서 다시 센다.
    # 그렇게 해야 --only ordinance 로 돌려도 index 의 총량이 지역 shard 를 잃지 않는다.
    t = dict(report["totals"])
    for key, kind in (("regions", "region"), ("ordinances", "ordinance"),
                      ("statutes", "statute")):
        items = merged.get(key) or []
        t[f"{kind}_shards"] = len(items)
        t[f"{kind}_bytes"] = sum(x.get("bytes") or 0 for x in items)
    t["instruments_bytes"] = (merged.get("instruments") or {}).get("bytes", 0)
    t["hierarchy_bytes"] = (merged.get("hierarchy") or {}).get("bytes", 0)
    t["bytes"] = (t["region_bytes"] + t["ordinance_bytes"] + t["statute_bytes"]
                  + t["instruments_bytes"] + t["hierarchy_bytes"])
    merged["totals"] = t
    body = json.dumps(_sanitize_keys(merged), ensure_ascii=False, indent=1)
    path.write_text(body, encoding="utf-8")
    return path.stat().st_size


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data"), help="출력 루트(기본 system/data)")
    ap.add_argument("--only", default=",".join(KINDS), help=f"생성 대상 {KINDS}")
    # 지역 묶음
    ap.add_argument("--levels", default="1,2", help="대상 레벨(기본 1,2)")
    ap.add_argument("--regions", default=None, help="sig_cd/region_id 직접 지정(쉼표) — 시험용")
    # [실측] 이 값이면 247곳 중 절삭되는 곳은 경기도(-169 조례)·제주(-6 조례, -59 쌍) 둘뿐이고
    # 가장 큰 파일이 298KB 다. 상한을 풀면 절삭은 0 이지만 경기도가 325KB 로 300KB 를 넘는다.
    ap.add_argument("--region-ord-cap", type=int, default=1300, help="지역당 조례 노드 상한")
    ap.add_argument("--region-parent-cap", type=int, default=1400, help="지역당 상위법 노드 상한")
    ap.add_argument("--region-edge-cap", type=int, default=4600, help="지역당 엣지 상한")
    ap.add_argument("--no-strip-prefix", dest="region_strip_prefix", action="store_false",
                    default=True, help="조례명 앞 지자체 이름을 자르지 않는다(용량 +4MB)")
    ap.add_argument("--no-preload", dest="preload", action="store_false", default=True,
                    help="전량 스캔 대신 지역마다 질의(느리지만 메모리 120MB 를 안 쓴다)")
    ap.add_argument("--inline-instruments", dest="region_dict", action="store_false",
                    default=True,
                    help="상위법을 지역 파일에 그대로 싣는다(파일 하나로 완결되지만 +19MB)")
    ap.add_argument("--region-max-bytes", type=int, default=300 * 1024, help="지역 shard 경고 상한")
    # 조례 개별 shard 확대 (make_graph_fixtures 와 같은 이름 = 같은 엔진에 그대로 넘긴다)
    # [실측] 1,000개 × 이 상한 = 21.4MB. 상한을 make_graph_fixtures 기본값(80/20/20)으로
    # 두면 같은 1,000개가 40MB 라 graph/ 전체가 60MB 예산을 넘는다. 잘린 상위법·인용 수는
    # 각 shard 의 truncated 에 남는다.
    ap.add_argument("--ord-limit", type=int, default=1000, help="조례 개별 shard 수(누적 목표)")
    ap.add_argument("--per-region", type=int, default=8, help="지역당 대표 조례 수")
    ap.add_argument("--min-degree", type=int, default=3, help="대표 조례 최소 차수")
    ap.add_argument("--max-nodes", type=int, default=35, help="조례 shard 노드 상한")
    ap.add_argument("--max-parents", type=int, default=8, help="조례 shard 의 위임 상위법 상한")
    ap.add_argument("--max-cited", type=int, default=8, help="조례 shard 의 인용 법령 상한")
    ap.add_argument("--hub-parents", type=int, default=4)
    ap.add_argument("--peer-pool", type=int, default=600)
    ap.add_argument("--force", action="store_true", help="기존 shard 재생성")
    ap.add_argument("--dry-run", action="store_true", help="쓰지 않고 크기만 계산")
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
    print(f"[사전계산] instrument {len(resolver.instruments)}건 · 정규화키 {len(resolver.by_norm)}개 "
          f"· 위임부모 {len(deg.deleg_parent)} · 이름해소 {len(deg.alias)}건 · {deg.seconds}s",
          flush=True)

    report = {
        "generator": "make_full_graph.py",
        "as_of_date": time.strftime("%Y-%m-%d"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+0900"),
        "params": {k: v for k, v in vars(a).items() if k != "only"} | {"only": sorted(a.only)},
        "layout": {"by_region": "graph/by-region/{sig_cd}.json",
                   "instruments": _REG_DICT_REF,
                   "ordinance": "graph/ordinance/{key}.json",
                   "statute": "graph/statute/{key}.json",
                   "hierarchy": "graph/hierarchy.json"},
        "regions": [], "ordinances": [], "errors": [], "warnings": [],
    }

    # --- 1) 지역 묶음 ---
    if "by-region" in a.only:
        regs = pick_regions(conn, a)
        todo = [r for r in regs
                if a.force or a.dry_run or existing(out / "by-region" / f"{r['sig_cd']}.json") is None]
        rdata = RegionData(conn, {r["region_id"] for r in todo}, preload=a.preload and bool(todo))
        print(f"[지역] {len(regs)}곳 (새로 구울 곳 {len(todo)}) · "
              f"전량 스캔 {rdata.seconds}s", flush=True)
        if a.region_dict:
            dpath = out / "instruments.json"
            dsize = None if (a.force or a.dry_run) else existing(dpath)
            if dsize is None:
                dic = instrument_dictionary(resolver, deg)
                env = envelope(dic, subgraph="instruments")
                dsize = (len(json.dumps(_sanitize_keys(env), ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8"))
                         if a.dry_run else write_shard(dpath, env))
                report["instruments"] = {"path": "instruments.json", "bytes": dsize,
                                         "count": dic["stats"]["instruments"],
                                         "unresolved": dic["stats"]["unresolved"]}
            else:
                report["instruments"] = {"path": "instruments.json", "bytes": dsize,
                                         "reused": True}
            print(f"[사전] instruments.json {human(dsize)}", flush=True)
        for i, reg in enumerate(regs, 1):
            sig = reg["sig_cd"]
            path = out / "by-region" / f"{sig}.json"
            size = None if (a.force or a.dry_run) else existing(path)
            item = {"sig_cd": sig, "region_id": reg["region_id"], "name": reg.get("name"),
                    "full_name": reg.get("full_name"), "level": reg.get("level"),
                    "status": reg.get("status"), "path": f"by-region/{sig}.json"}
            if size is not None:
                item |= {"bytes": size, "reused": True}
            else:
                try:
                    sub = region_subgraph(conn, resolver, deg, reg, a, rdata)
                except Exception as e:  # noqa: BLE001
                    msg = f"{type(e).__name__}: {str(e)[:160]}"
                    report["errors"].append({"kind": "by-region", "id": sig, "error": msg})
                    print(f"  [FAIL] {sig} — {msg}", flush=True)
                    continue
                env = envelope(sub, region=sig, subgraph="by-region")
                if a.dry_run:
                    size = len(json.dumps(_sanitize_keys(env), ensure_ascii=False,
                                          separators=(",", ":")).encode("utf-8"))
                else:
                    size = write_shard(path, env)
                item |= {"bytes": size, "nodes": sub["stats"]["nodes"],
                         "edges": sub["stats"]["edges"],
                         "ordinances": sub["stats"]["ordinance_nodes"],
                         "instruments": sub["stats"]["instrument_nodes"],
                         "unresolved": sub["stats"]["unresolved_instruments"]}
                if sub["truncated"]["pairs_shown"] < sub["truncated"]["pairs_total"]:
                    item["truncated"] = True
            report["regions"].append(item)
            if size > a.region_max_bytes:
                report["warnings"].append({"kind": "by-region", "sig_cd": sig,
                                           "message": f"{human(size)} > {human(a.region_max_bytes)} 상한"})
            if i % 20 == 0 or i == len(regs):
                print(f"  [{i}/{len(regs)}] {sig} {reg.get('full_name')} {human(size)}", flush=True)

    # --- 2) 조례 개별 shard 확대 ---
    if "ordinance" in a.only:
        seeds = pick_seed_ordinances(conn, deg, a)
        print(f"[조례] 대표 {len(seeds)}건 (지역 {len({s['region_id'] for s in seeds})}곳)", flush=True)
        for i, s in enumerate(seeds, 1):
            key = file_key(s["ordinance_id"])
            path = out / "ordinance" / f"{key}.json"
            size = None if (a.force or a.dry_run) else existing(path)
            item = {"key": key, "ordinance_id": s["ordinance_id"], "name": s["name"],
                    "region_id": s["region_id"], "sig_cd": s.get("sig_cd"),
                    "region_name": s.get("region_name"), "degree": s["degree"],
                    "path": f"ordinance/{key}.json"}
            if size is not None:
                item |= {"bytes": size, "reused": True}
            else:
                try:
                    sub = ordinance_subgraph(conn, resolver, deg, s, a)
                except Exception as e:  # noqa: BLE001
                    msg = f"{type(e).__name__}: {str(e)[:160]}"
                    report["errors"].append({"kind": "ordinance", "id": s["ordinance_id"],
                                             "error": msg})
                    print(f"  [FAIL] {key} — {msg}", flush=True)
                    continue
                env = envelope(sub, seed=s["ordinance_id"], subgraph="ordinance")
                if a.dry_run:
                    size = len(json.dumps(_sanitize_keys(env), ensure_ascii=False,
                                          separators=(",", ":")).encode("utf-8"))
                else:
                    size = write_shard(path, env)
                item |= {"bytes": size, "nodes": sub["stats"]["nodes"],
                         "edges": sub["stats"]["edges"],
                         "unresolved": sub["stats"]["unresolved_instruments"]}
            report["ordinances"].append(item)
            if size > 200 * 1024:
                report["warnings"].append({"kind": "ordinance", "key": key,
                                           "message": f"{human(size)} > 200KB 상한"})
            if i % 100 == 0 or i == len(seeds):
                print(f"  [{i}/{len(seeds)}] {key} {human(size)}", flush=True)

    # --- 3) index 병합 ---
    rb = sum(x.get("bytes") or 0 for x in report["regions"])
    ob = sum(x.get("bytes") or 0 for x in report["ordinances"])
    report["totals"] = {
        "region_shards": len(report["regions"]), "region_bytes": rb,
        "ordinance_shards": len(report["ordinances"]), "ordinance_bytes": ob,
        "seconds": round(time.time() - t0, 1),
    }
    if not a.dry_run:
        n = merge_index(out / "index.json", report)
        print(f"[index] {out / 'index.json'} {human(n)}", flush=True)
    big = max((x.get("bytes") or 0 for x in report["regions"]), default=0)
    print(f"완료 · 지역 {report['totals']['region_shards']}({human(rb)}, 최대 {human(big)})"
          f" · 조례 {report['totals']['ordinance_shards']}({human(ob)})"
          f" · 합계 {human(rb + ob)} · {report['totals']['seconds']}s"
          f" · 오류 {len(report['errors'])} 경고 {len(report['warnings'])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
