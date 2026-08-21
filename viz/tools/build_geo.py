# -*- coding: utf-8 -*-
"""
지도 화면용 경량 시군구 경계 GeoJSON 생성기.

입력 (실측 확인 파일):
  - F:/policy_maps/system/data/reference/skorea-municipalities-2018-geo.json
      FeatureCollection, 250 feature, MultiPolygon, properties = {name, base_year, name_eng, code}
      properties.code 는 통계청(SGIS) 시군구 코드다(종로구 11010). 우리 sig_cd(11110)와 다르다.
  - F:/policy_maps/system/data/reference/kostat_to_bjd.json
      {_meta, mapping:{ "11010": {bjd_sig_cd:"11110", bjd_sido_nm, bjd_name, verified, ...}, ... }}
      250건 전수 verified.

출력:
  - F:/policy_maps/viz/public/geo/municipalities.geojson
      properties = {sig_cd, name, sido, kostat_code, verified}
      좌표는 Douglas-Peucker 단순화 + 소수점 절삭으로 축소.

사용:
  python F:/policy_maps/viz/tools/build_geo.py [--eps 0.002] [--precision 4]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VIZ = os.path.dirname(HERE)
REPO = os.path.dirname(VIZ)
REF = os.path.join(REPO, "system", "data", "reference")

SRC_GEO = os.path.join(REF, "skorea-municipalities-2018-geo.json")
SRC_MAP = os.path.join(REF, "kostat_to_bjd.json")
OUT = os.path.join(VIZ, "public", "geo", "municipalities.geojson")


def perpendicular_distance(pt, a, b):
    (x, y), (x1, y1), (x2, y2) = pt, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def douglas_peucker(points, eps):
    """반복(스택) 방식 DP. 재귀 깊이 제한 회피."""
    n = len(points)
    if n < 3:
        return list(points)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        dmax, idx = 0.0, i
        a, b = points[i], points[j]
        for k in range(i + 1, j):
            d = perpendicular_distance(points[k], a, b)
            if d > dmax:
                dmax, idx = d, k
        if dmax > eps:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, f in zip(points, keep) if f]


def simplify_ring(ring, eps, precision):
    closed = len(ring) > 2 and ring[0] == ring[-1]
    pts = ring[:-1] if closed else ring[:]
    out = douglas_peucker(pts, eps)
    if len(out) < 3:
        out = pts[:]  # 너무 줄면 원본 유지(면이 사라지는 것 방지)
    out = [[round(x, precision), round(y, precision)] for x, y in out]
    # 절삭으로 생긴 연속 중복점 제거
    dedup = [out[0]]
    for p in out[1:]:
        if p != dedup[-1]:
            dedup.append(p)
    if len(dedup) < 3:
        return None
    dedup.append(dedup[0])
    return dedup


def ring_bbox_diag(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eps", type=float, default=0.002, help="DP 허용오차(도). 0.002도 ~= 200m")
    ap.add_argument("--precision", type=int, default=4, help="좌표 소수점 자리수. 4 ~= 11m")
    ap.add_argument("--min-ring-diag", type=float, default=0.004,
                    help="이 대각선(도)보다 작은 링(미세 섬)은 제거. 피처의 유일 링이면 유지")
    args = ap.parse_args()

    for p in (SRC_GEO, SRC_MAP):
        if not os.path.exists(p):
            print(f"[ERROR] 입력 없음: {p}", file=sys.stderr)
            return 2

    with open(SRC_GEO, encoding="utf-8") as f:
        geo = json.load(f)
    with open(SRC_MAP, encoding="utf-8") as f:
        cw = json.load(f)["mapping"]

    feats_in = geo["features"]
    out_feats = []
    missing = []
    dropped_rings = 0
    pts_in = pts_out = 0

    for ft in feats_in:
        props = ft.get("properties", {})
        kcode = str(props.get("code", ""))
        m = cw.get(kcode)
        if not m:
            missing.append(kcode)
            continue
        geom = ft["geometry"]
        gtype = geom["type"]
        polys = geom["coordinates"] if gtype == "MultiPolygon" else [geom["coordinates"]]

        new_polys = []
        cand = []
        for poly in polys:
            new_rings = []
            for ri, ring in enumerate(poly):
                pts_in += len(ring)
                if ri == 0 and ring_bbox_diag(ring) < args.min_ring_diag:
                    cand.append((ring_bbox_diag(ring), poly))
                    new_rings = []
                    break
                s = simplify_ring(ring, args.eps, args.precision)
                if s is None:
                    dropped_rings += 1
                    continue
                pts_out += len(s)
                new_rings.append(s)
            if new_rings:
                new_polys.append(new_rings)
        if not new_polys and cand:
            # 전부 미세 링이면 가장 큰 것 하나는 살린다
            cand.sort(key=lambda t: -t[0])
            ring = cand[0][1][0]
            s = simplify_ring(ring, args.eps / 4, args.precision)
            if s:
                new_polys.append([s])
        if not new_polys:
            continue

        out_feats.append({
            "type": "Feature",
            "properties": {
                "sig_cd": m["bjd_sig_cd"],
                "name": m.get("bjd_name") or props.get("name"),
                "sido": m.get("bjd_sido_nm"),
                "kostat_code": kcode,
                "verified": bool(m.get("verified")),
            },
            "geometry": {"type": "MultiPolygon", "coordinates": new_polys},
        })

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    doc = {
        "type": "FeatureCollection",
        "_meta": {
            "source": "system/data/reference/skorea-municipalities-2018-geo.json (250 feature, 2018 기준)",
            "crosswalk": "system/data/reference/kostat_to_bjd.json (properties.code=통계청코드 -> sig_cd)",
            "simplify": {"algorithm": "douglas-peucker", "eps_deg": args.eps,
                         "coord_precision": args.precision, "min_ring_diag_deg": args.min_ring_diag},
            "note": "표시 전용 경량본. 면적·경계 계산에 쓰지 말 것.",
            "features": len(out_feats),
        },
        "features": out_feats,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(OUT)
    print(f"입력 feature {len(feats_in)} -> 출력 {len(out_feats)}")
    print(f"좌표점 {pts_in:,} -> {pts_out:,} ({pts_out / max(pts_in,1):.1%})")
    print(f"크로스워크 미매칭 {len(missing)}건: {missing[:10]}")
    print(f"제거된 링 {dropped_rings}")
    print(f"출력 {OUT}  {size:,} B ({size/1048576:.2f} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
