"""Build a lightweight WGS84 administrative-dong boundary GeoJSON.

Input is the Statistical GIS administrative-dong Shapefile:
  C:/sb2/mask/BND_ADM_DONG_PG (2)/BND_ADM_DONG_PG.shp

The source ADM_CD prefix uses KOSTAT five-digit municipality codes. The
visualization graph uses sig_cd, so this script joins through
viz/public/geo/municipalities.geojson properties.kostat_code -> sig_cd.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform


VIZ_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = Path(r"C:\sb2\mask\BND_ADM_DONG_PG (2)\BND_ADM_DONG_PG.shp")
DEFAULT_OUT = VIZ_ROOT / "public" / "geo" / "adm_dong.geojson"
MUNI_GEO = VIZ_ROOT / "public" / "geo" / "municipalities.geojson"


def _round_coords(obj: Any, digits: int = 5) -> Any:
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(float(x), digits) for x in obj]
        return [_round_coords(x, digits) for x in obj]
    return obj


def _load_crosswalk() -> tuple[dict[str, dict], list[tuple[Any, dict]]]:
    geo = json.loads(MUNI_GEO.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    geoms: list[tuple[Any, dict]] = []
    for feat in geo.get("features", []):
        p = feat.get("properties") or {}
        props = {
            "sig_cd": str(p.get("sig_cd") or ""),
            "sig_name": p.get("name"),
            "sido": p.get("sido"),
        }
        kostat = str(p.get("kostat_code") or "")
        if kostat and props["sig_cd"]:
            out[kostat] = props
        if props["sig_cd"]:
            geoms.append((shape(feat.get("geometry")), props))
    return out, geoms


def _spatial_match(geom: Any, muni_geoms: list[tuple[Any, dict]]) -> dict | None:
    pt = geom.representative_point()
    for muni_geom, props in muni_geoms:
        if muni_geom.contains(pt) or muni_geom.touches(pt):
            return {**props, "join_method": "spatial"}
    nearest = None
    nearest_dist = float("inf")
    for muni_geom, props in muni_geoms:
        dist = muni_geom.distance(pt)
        if dist < nearest_dist:
            nearest_dist = dist
            nearest = props
    if nearest is not None and nearest_dist < 0.02:
        return {**nearest, "join_method": "nearest"}
    return None


def build(src: Path = DEFAULT_SRC, out: Path = DEFAULT_OUT, *, tolerance: float = 0.00025) -> dict:
    prj = src.with_suffix(".prj").read_text(encoding="utf-8")
    source_crs = CRS.from_wkt(prj)
    transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
    crosswalk, muni_geoms = _load_crosswalk()

    reader = shapefile.Reader(str(src), encoding="cp949")
    features = []
    unmatched = 0
    spatial_joined = 0
    nearest_joined = 0

    for sr in reader.iterShapeRecords():
        rec = sr.record.as_dict()
        adm_cd = str(rec.get("ADM_CD") or "")
        kostat_code = adm_cd[:5]
        match = crosswalk.get(kostat_code)

        geom = transform(transformer.transform, shape(sr.shape.__geo_interface__))
        if not match:
            match = _spatial_match(geom, muni_geoms)
            if match and match.get("join_method") == "spatial":
                spatial_joined += 1
            elif match and match.get("join_method") == "nearest":
                nearest_joined += 1
        if not match:
            unmatched += 1
        if tolerance > 0:
            geom = geom.simplify(tolerance, preserve_topology=True)

        gj = mapping(geom)
        gj["coordinates"] = _round_coords(gj["coordinates"], 5)
        features.append({
            "type": "Feature",
            "properties": {
                "adm_cd": adm_cd,
                "adm_nm": rec.get("ADM_NM"),
                "base_date": rec.get("BASE_DATE"),
                "kostat_code": kostat_code,
                "sig_cd": match["sig_cd"] if match else None,
                "sig_name": match["sig_name"] if match else None,
                "sido": match["sido"] if match else None,
                "join_method": match.get("join_method", "kostat_code") if match else None,
            },
            "geometry": gj,
        })

    doc = {
        "type": "FeatureCollection",
        "_meta": {
            "source": str(src),
            "source_crs": source_crs.to_string(),
            "features": len(features),
            "unmatched": unmatched,
            "spatial_joined": spatial_joined,
            "nearest_joined": nearest_joined,
            "simplify_tolerance_deg": tolerance,
            "coord_precision": 5,
            "join": "ADM_CD[:5] KOSTAT code -> sig_cd, then representative-point spatial fallback",
        },
        "features": features,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {
        "out": str(out),
        "features": len(features),
        "unmatched": unmatched,
        "spatial_joined": spatial_joined,
        "nearest_joined": nearest_joined,
        "bytes": out.stat().st_size,
    }


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
