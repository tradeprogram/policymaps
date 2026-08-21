"""policymap.analytics.spatial — 공간자기상관 정식 구현(전역 Moran's I + LISA).

근거 문헌
  * Moran, P.A.P. (1950) "Notes on Continuous Stochastic Phenomena", Biometrika 37(1/2): 17-23.
  * Anselin, L. (1995) "Local Indicators of Spatial Association — LISA",
    Geographical Analysis 27(2): 93-115.
    → 국지 통계량 I_i = z_i * Σ_j w_ij z_j (z 는 편차, m2=Σz²/n 로 표준화),
      유의성은 **조건부 순열검정**(i 를 고정하고 나머지 n-1 값을 무작위 재배치)으로 얻는다.
      Anselin 자신이 다중검정 문제를 지적하며 보수적 판정을 권고한다.
  * Benjamini, Y. & Hochberg, Y. (1995) "Controlling the False Discovery Rate:
    A Practical and Powerful Approach to Multiple Testing",
    Journal of the Royal Statistical Society Series B 57(1): 289-300.
    → n개 국지검정의 FDR 보정.
  * Anselin, L. (1996) "The Moran Scatterplot as an ESDA tool" — HH/LL/HL/LH 사분면.

기존 구현 대비 개선점
  1. 행표준화 W = D^-1 A 를 기본값으로(이진 인접은 다접경 내륙 지자체에 과대 영향력).
     이에 따라 I_i 와 공간지연(spatial lag)의 정규화 기준이 일치한다.
  2. 국지 조건부 순열검정으로 p_i 를 산출하고 BH-FDR 로 보정 → 지도에 칠할 사분면 확정.
  3. 인접 0으로 표본에서 빠지는 지자체를 excluded 로 명시 반환.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any, Optional

import numpy as np

from . import base as _base


# --------------------------------------------------------------------------- #
# 지표
# --------------------------------------------------------------------------- #
def metric_values(conn: sqlite3.Connection, metric: str, *,
                  level: int = 2, fyr: int = 2025) -> dict[str, float]:
    """지원 지표 → {region_id: 값}.

    'ordinance_count' | 'budget_total' | 'budget_per_capita' | 'population'
    | 'welfare_ratio' | 'fiscal_self_ratio' | 'pop_density'
    | 'template:<키워드>'      (해당 조례 보유=1/미보유=0)
    | 'adoption_year:<키워드>' (채택연도 자체 — 시점의 공간군집 검정용. 미채택은 제외)

    'adoption_year:' 는 수평확산(이웃 학습·모방) 가설의 **가장 직접적인 검정**이다.
    이웃을 따라 채택한다면 채택 '시점'이 공간적으로 군집해야 한다.
    """
    if metric.startswith("adoption_year:") or metric.startswith("adoption_year_resid:"):
        resid = metric.startswith("adoption_year_resid:")
        tpl = metric.split(":", 1)[1]
        mode = "upper_bound"
        if "|" in tpl:
            tpl, mode = tpl.split("|", 1)
        ad = _base.adoption_years(conn, tpl, level=level, mode=mode)
        vals = {rid: float(y) for rid, y in ad["years"].items()}
        if not resid:
            return vals
        # 시도 고정효과 제거: 광역 평균을 뺀 잔차.
        # 인접 지자체는 대개 같은 광역에 속하므로, 관측된 시점 군집이
        # '이웃 학습' 인지 '광역 공통충격' 인지 이 잔차 Moran's I 로 가른다.
        gov = {g["region_id"]: g for g in _base.active_local_governments(conn, level=level)}
        grp: dict[str, list[float]] = {}
        for rid, v in vals.items():
            grp.setdefault(gov[rid]["sido_cd"], []).append(v)
        mu = {k: sum(v) / len(v) for k, v in grp.items()}
        return {rid: v - mu[gov[rid]["sido_cd"]] for rid, v in vals.items()}
    cov = _base.region_covariates(conn, level=level, fyr=fyr)
    if metric.startswith("template:"):
        tpl = metric.split(":", 1)[1]
        ad = _base.adoption_years(conn, tpl, level=level, mode="upper_bound")
        holders = set(ad["years"])
        return {rid: (1.0 if rid in holders else 0.0) for rid in cov}
    if metric == "budget_per_capita":
        return {rid: c["budget_total"] / c["population"]
                for rid, c in cov.items()
                if c.get("budget_total") and c.get("population")}
    key = {"ordinance_count": "ordinance_count", "budget_total": "budget_total",
           "population": "population", "welfare_ratio": "welfare_ratio",
           "fiscal_self_ratio": "fiscal_self_ratio", "pop_density": "pop_density"}.get(metric)
    if key is None:
        raise ValueError(f"unsupported metric: {metric}")
    return {rid: float(c[key]) for rid, c in cov.items() if c.get(key) is not None}


# --------------------------------------------------------------------------- #
# 전역 Moran's I
# --------------------------------------------------------------------------- #
def _prep(values: dict[str, float], W: dict[str, dict[str, float]]):
    nodes = sorted(rid for rid in values if W.get(rid))
    nodes = [n for n in nodes if any(j in values for j in W[n])]
    idx = {n: i for i, n in enumerate(nodes)}
    x = np.array([values[nd] for nd in nodes], dtype=float)
    # 이웃 인덱스/가중치 (표본 내로 제한 후 재행표준화)
    nb_idx, nb_w = [], []
    for nd in nodes:
        pairs = [(idx[j], w) for j, w in W[nd].items() if j in idx]
        s = sum(w for _, w in pairs)
        if s <= 0:
            nb_idx.append(np.array([], dtype=int))
            nb_w.append(np.array([], dtype=float))
        else:
            nb_idx.append(np.array([i for i, _ in pairs], dtype=int))
            nb_w.append(np.array([w / s for _, w in pairs], dtype=float))
    return nodes, x, nb_idx, nb_w


def _global_i(z: np.ndarray, nb_idx, nb_w) -> tuple[float, float]:
    n = z.size
    s0 = float(sum(w.sum() for w in nb_w))
    num = 0.0
    for i in range(n):
        if nb_idx[i].size:
            num += z[i] * float(np.dot(nb_w[i], z[nb_idx[i]]))
    den = float(np.dot(z, z))
    if den == 0 or s0 == 0:
        return float("nan"), s0
    return (n / s0) * (num / den), s0


def moran(
    conn: sqlite3.Connection,
    metric: str,
    *,
    level: int = 2,
    fyr: int = 2025,
    weights: str = "row",
    permutations: int = 999,
    seed: int = 2026,
    lisa: bool = True,
    fdr_alpha: float = 0.05,
    weights_kwargs: Optional[dict] = None,
) -> dict:
    """전역 Moran's I + (옵션) LISA + 조건부 순열검정 + BH-FDR.

    반환 dict 의 lisa 항목은 각 지자체의 local_i, spatial_lag(표준화 z 기준),
    quadrant, p_sim(조건부 순열), q_value(BH), significant(FDR 통과) 를 담는다.
    지도에 색을 칠할 때는 significant=True 인 사분면만 칠하라.
    """
    wk = dict(weights_kwargs or {})
    wk.setdefault("level", level)
    wk["standardize"] = weights
    pack = _base.build_spatial_weights(conn, **wk)
    W = pack["W"]
    values = metric_values(conn, metric, level=level, fyr=fyr)

    universe = {g["region_id"]: g for g in _base.active_local_governments(conn, level=level)}
    excluded = list(pack["meta"]["excluded"])
    for rid, g in universe.items():
        if rid not in values:
            excluded.append({"region_id": rid, "name": g.get("full_name"),
                             "reason": f"no_value:{metric}"})

    nodes, x, nb_idx, nb_w = _prep(values, W)
    n = len(nodes)
    out: dict[str, Any] = {
        "metric": metric, "level": level, "weights": weights,
        "n": n, "universe": len(universe),
        "excluded": excluded, "n_excluded": len(excluded),
        "weights_meta": pack["meta"],
    }
    if n < 8:
        out.update({"moran_i": None, "note": "표본 부족(n<8)"})
        return out

    mean = float(x.mean())
    z = x - mean
    I, s0 = _global_i(z, nb_idx, nb_w)  # noqa: E741  (Moran's I 표준 표기)
    if math.isnan(I):
        out.update({"moran_i": None, "note": "분산 0"})
        return out

    rng = np.random.default_rng(seed)
    perm = np.empty(permutations)
    for b in range(permutations):
        zb = rng.permutation(z)
        perm[b], _ = _global_i(zb, nb_idx, nb_w)
    E = -1.0 / (n - 1)
    if I >= E:
        extreme = int(np.sum(perm >= I))
    else:
        extreme = int(np.sum(perm <= I))
    p_sim = (extreme + 1) / (permutations + 1)
    z_sim = float((I - perm.mean()) / perm.std(ddof=0)) if perm.std(ddof=0) > 0 else None

    out.update({
        "moran_i": round(float(I), 6),
        "expected_i": round(E, 6),
        "mean": round(mean, 6),
        "sd": round(float(x.std(ddof=0)), 6),
        "s0": round(s0, 4),
        "p_sim": round(p_sim, 5),
        "z_sim": round(z_sim, 4) if z_sim is not None else None,
        "permutations": permutations,
        "interpretation": ("정적 공간자기상관(유사한 값끼리 인접)" if I > E and p_sim <= 0.05
                           else "부적 공간자기상관(체스판 패턴)" if I < E and p_sim <= 0.05
                           else "무작위 배치와 구별되지 않음"),
    })
    if not lisa:
        return out

    # ---- LISA: 조건부 순열검정 ----
    m2 = float(np.dot(z, z) / n)
    lag = np.array([float(np.dot(nb_w[i], z[nb_idx[i]])) if nb_idx[i].size else 0.0
                    for i in range(n)])
    local_i = z * lag / m2
    zsd = float(z.std(ddof=0)) or 1.0

    rng2 = np.random.default_rng(seed + 1)
    p_i = np.ones(n)
    for i in range(n):
        ki = nb_idx[i].size
        if ki == 0:
            continue
        others = np.delete(z, i)
        wi = nb_w[i]
        sims = np.empty(permutations)
        for b in range(permutations):
            pick = rng2.choice(others, size=ki, replace=False)
            sims[b] = z[i] * float(np.dot(wi, pick)) / m2
        obs = local_i[i]
        extreme_i = int(np.sum(sims >= obs)) if obs >= 0 else int(np.sum(sims <= obs))
        p_i[i] = (extreme_i + 1) / (permutations + 1)

    # BH-FDR
    order = np.argsort(p_i)
    q = np.ones(n)
    prev = 1.0
    for rank in range(n - 1, -1, -1):
        i = order[rank]
        val = p_i[i] * n / (rank + 1)
        prev = min(prev, val)
        q[i] = min(1.0, prev)

    rows = []
    for i, nd in enumerate(nodes):
        g = universe.get(nd, {})
        zi, li = float(z[i] / zsd), float(lag[i] / zsd)
        quad = ("HH" if zi >= 0 and li >= 0 else
                "LL" if zi < 0 and li < 0 else
                "HL" if zi >= 0 and li < 0 else "LH")
        rows.append({
            "region_id": nd, "sig_cd": g.get("sig_cd"), "name": g.get("full_name"),
            "value": round(float(x[i]), 4),
            "z_score": round(zi, 4),
            "spatial_lag_z": round(li, 4),
            "local_i": round(float(local_i[i]), 6),
            "quadrant": quad,
            "p_sim": round(float(p_i[i]), 5),
            "q_value": round(float(q[i]), 5),
            "significant": bool(q[i] <= fdr_alpha),
            "n_neighbors": int(nb_idx[i].size),
        })
    rows.sort(key=lambda r: (not r["significant"], r["q_value"], -abs(r["local_i"])))
    sig = [r for r in rows if r["significant"]]
    counts: dict[str, int] = {}
    for r in sig:
        counts[r["quadrant"]] = counts.get(r["quadrant"], 0) + 1
    out["lisa"] = rows
    out["lisa_summary"] = {
        "fdr_alpha": fdr_alpha,
        "n_significant_raw_p05": int(np.sum(p_i <= 0.05)),
        "n_significant_fdr": len(sig),
        "expected_false_positives_without_fdr": round(0.05 * n, 1),
        "by_quadrant": counts,
        "clusters": {
            "HH": [r["name"] for r in sig if r["quadrant"] == "HH"],
            "LL": [r["name"] for r in sig if r["quadrant"] == "LL"],
            "HL": [r["name"] for r in sig if r["quadrant"] == "HL"],
            "LH": [r["name"] for r in sig if r["quadrant"] == "LH"],
        },
    }
    return out


def format_moran(res: dict) -> str:
    if res.get("moran_i") is None:
        return f"[{res['metric']}] 계산 불가: {res.get('note')}"
    s = (f"[{res['metric']}] n={res['n']}/{res['universe']} (제외 {res['n_excluded']}) "
         f"W={res['weights']}표준화\n"
         f"  Moran's I = {res['moran_i']:.4f}  E[I] = {res['expected_i']:.4f}  "
         f"z = {res['z_sim']}  p(perm,{res['permutations']}) = {res['p_sim']}\n"
         f"  → {res['interpretation']}")
    ls = res.get("lisa_summary")
    if ls:
        s += (f"\n  LISA: raw p<=.05 {ls['n_significant_raw_p05']}곳 → "
              f"BH-FDR(α={ls['fdr_alpha']}) 통과 {ls['n_significant_fdr']}곳 "
              f"{ls['by_quadrant']}  (보정 없으면 기대 위양성 {ls['expected_false_positives_without_fdr']}곳)")
    return s


__all__ = ["moran", "metric_values", "format_moran"]
