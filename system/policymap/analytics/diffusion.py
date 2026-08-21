"""policymap.analytics.diffusion — 확산 곡선·속도·혁신자·경로 분해.

근거 문헌
  * Rogers, E.M. (2003) "Diffusion of Innovations", 5th ed., Free Press.
    → 누적 채택이 S자 곡선을 그린다는 고전 명제, 채택자 범주(혁신자 2.5%,
      초기수용자 13.5%, 초기다수 34%, 후기다수 34%, 지각수용자 16%).
  * Gray, V. (1973) "Innovation in the States: A Diffusion Study",
    American Political Science Review 67(4): 1174-1185.
    → 주(州) 정책 채택 누적곡선에 로지스틱을 적합해 확산 속도를 비교한 최초 계열.
  * Mahajan, V. & Peterson, R.A. (1985) "Models for Innovation Diffusion",
    Sage, Quantitative Applications in the Social Sciences 48.
    → 확산 모형의 표준 정리. 본 모듈은 그중 로지스틱 성장모형
      N(t)=K/(1+exp(-r(t-t0))) 을 쓴다(r=내적성장률, t0=변곡점=50% 도달시점).
      10%→90% 소요기간 = ln(81)/r 은 이 함수형에서 직접 유도된다.
  * Shipan & Volden (2008) AJPS 52(4): 840-857 — 수평(인접 학습·모방) vs
    수직(상위정부 강제·유인) 경로 구분. path_decomposition 이 이를 조작화한다.

주의: 곡선의 좌측은 base.adoption_years 의 한계(제정본 선택편의 / 상한 대입)를
그대로 물려받는다. 두 mode 를 함께 적합해 모수 안정성을 확인하라.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Optional

import numpy as np

from . import base as _base


# --------------------------------------------------------------------------- #
# 로지스틱 성장모형 적합 (Levenberg-Marquardt 축약형)
# --------------------------------------------------------------------------- #
def fit_logistic_growth(t: np.ndarray, y: np.ndarray, *, K: Optional[float] = None,
                        max_iter: int = 300) -> dict:
    """누적 채택수 y(t) 에 N(t)=K/(1+exp(-r(t-t0))) 적합.

    K 를 주면 2모수(r, t0), 안 주면 3모수(K, r, t0) 추정.
    최소자승 + Levenberg-Marquardt 감쇠. R² 와 잔차 RMSE 를 함께 반환.
    """
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    fixK = K is not None
    K0 = float(K) if fixK else max(float(y.max()) * 1.15, float(y.max()) + 1.0)
    t0_0 = float(t[np.argmin(np.abs(y - y.max() / 2.0))])
    span = max(1.0, float(t.max() - t.min()))
    r0 = 4.0 / span
    theta = np.array([r0, t0_0]) if fixK else np.array([K0, r0, t0_0])

    def unpack(th):
        return (K0, th[0], th[1]) if fixK else (th[0], th[1], th[2])

    def model(th):
        k, r, t0 = unpack(th)
        return k / (1.0 + np.exp(-np.clip(r * (t - t0), -60, 60)))

    def jac(th):
        k, r, t0 = unpack(th)
        e = np.exp(-np.clip(r * (t - t0), -60, 60))
        d = (1.0 + e)
        dK = 1.0 / d
        common = k * e / (d * d)
        dr = common * (t - t0)
        dt0 = -common * r
        return np.column_stack([dr, dt0] if fixK else [dK, dr, dt0])

    lam = 1e-3
    resid = y - model(theta)
    sse = float(resid @ resid)
    for _ in range(max_iter):
        J = jac(theta)
        A = J.T @ J
        g = J.T @ resid
        step = None
        for _try in range(30):
            try:
                step = np.linalg.solve(A + lam * np.diag(np.clip(np.diag(A), 1e-12, None)), g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            cand = theta + step
            kk, rr, _ = unpack(cand)
            if (not fixK and kk <= 0) or rr <= 0:
                lam *= 10
                continue
            rcand = y - model(cand)
            s = float(rcand @ rcand)
            if s < sse:
                theta, resid, sse = cand, rcand, s
                lam = max(lam / 3, 1e-9)
                break
            lam *= 10
        else:
            break
        if step is not None and np.max(np.abs(step)) < 1e-10:
            break

    k, r, t0 = unpack(theta)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return {
        "K": float(k), "r": float(r), "t0": float(t0),
        "K_fixed": fixK,
        "r2": float(1 - sse / ss_tot) if ss_tot > 0 else None,
        "rmse": float(math.sqrt(sse / len(y))),
        "t_10_90_years": float(math.log(81.0) / r) if r > 0 else None,
        "peak_rate_per_year": float(k * r / 4.0),
        "sse": sse,
    }


# --------------------------------------------------------------------------- #
# 확산 프로파일
# --------------------------------------------------------------------------- #
def diffusion_profile(
    conn: sqlite3.Connection,
    template: str,
    *,
    level: int = 2,
    mode: str = "enactment",
    y0: Optional[int] = None,
    y1: Optional[int] = None,
    innovator_frac: float = 0.10,
    weights_kwargs: Optional[dict] = None,
    permutations: int = 999,
    seed: int = 2026,
) -> dict:
    """템플릿 조례의 확산 곡선·속도·혁신자·경로 분해를 한 번에.

    반환
      curve             연도별 신규/누적 채택과 채택률
      logistic          로지스틱 성장모형 적합 결과(K=위험집합 크기 고정 + 자유추정 2종)
      innovators        상위 innovator_frac 비율의 최초 채택 지자체
      rogers_categories Rogers(2003) 채택자 범주별 지자체 수/시점
      path_decomposition 채택 시점의 선행 신호 분해(인접 선행 / 상위 광역 선행 / 둘다 / 없음)
    """
    ad = _base.adoption_years(conn, template, level=level, mode=mode)
    years = ad["years"]
    govs = _base.active_local_governments(conn, level=level)
    gov = {g["region_id"]: g for g in govs}
    N = len(govs)
    if not years:
        return {"template": template, "mode": mode, "error": "채택 관측 없음",
                "adoption_meta": ad["meta"]}

    ymin = y0 if y0 is not None else min(years.values())
    ymax = y1 if y1 is not None else max(years.values())
    span = list(range(ymin, ymax + 1))
    new = {y: 0 for y in span}
    for rid, y in years.items():
        if ymin <= y <= ymax:
            new[y] += 1
    cum, run = [], 0
    for y in span:
        run += new[y]
        cum.append({"year": y, "new": new[y], "cumulative": run,
                    "adoption_rate": round(run / N, 4)})

    t = np.array(span, dtype=float)
    yc = np.array([c["cumulative"] for c in cum], dtype=float)
    fits = {}
    if len(span) >= 4 and yc.max() > 2:
        fits["K_fixed_universe"] = fit_logistic_growth(t, yc, K=float(N))
        fits["K_free"] = fit_logistic_growth(t, yc)

    # 혁신자
    ordered = sorted(years.items(), key=lambda kv: (kv[1], kv[0]))
    n_inno = max(1, int(round(innovator_frac * len(ordered))))
    innovators = [{"region_id": rid, "name": gov[rid].get("full_name"),
                   "rtype": gov[rid].get("rtype"), "year": y}
                  for rid, y in ordered[:n_inno]]

    # Rogers 범주(관측 채택자 기준 백분위)
    cuts = [(0.025, "innovators"), (0.16, "early_adopters"), (0.50, "early_majority"),
            (0.84, "late_majority"), (1.00, "laggards")]
    rogers, prev = {}, 0
    total = len(ordered)
    for frac, label in cuts:
        upto = int(round(frac * total))
        seg = ordered[prev:upto]
        rogers[label] = {"n": len(seg),
                         "year_range": [seg[0][1], seg[-1][1]] if seg else None}
        prev = upto
    laggard_never = N - total
    rogers["never_adopted"] = {"n": laggard_never,
                               "share_of_universe": round(laggard_never / N, 4)}

    # 경로 분해
    wk = dict(weights_kwargs or {})
    wk.setdefault("level", level)
    adjacency = _base.build_spatial_weights(conn, **wk)["adjacency"]
    upper = _base.adoption_years(conn, template, level=1, mode=mode)["years"]
    sido_year: dict[str, int] = {}
    for r in _base._rows(conn,
                         "SELECT region_id FROM regions WHERE level=1 AND status='active'"):
        if r["region_id"] in upper:
            sido_year[r["region_id"][:2]] = upper[r["region_id"]]

    paths = {"neighbor_first": 0, "upper_first": 0, "both": 0, "neither": 0}
    detail = []
    for rid, y in ordered:
        nbs = adjacency.get(rid) or set()
        nb_prior = [n for n in nbs if years.get(n) is not None and years[n] < y]
        sy = sido_year.get(gov[rid]["sido_cd"])
        up_prior = sy is not None and sy < y
        if nb_prior and up_prior:
            key = "both"
        elif nb_prior:
            key = "neighbor_first"
        elif up_prior:
            key = "upper_first"
        else:
            key = "neither"
        paths[key] += 1
        detail.append({"region_id": rid, "name": gov[rid].get("full_name"), "year": y,
                       "n_prior_neighbors": len(nb_prior), "n_neighbors": len(nbs),
                       "upper_adopted_first": bool(up_prior), "path": key})

    # 경로분해의 귀무분포: 채택연도 벡터를 지자체에 무작위 재배정(연도분포 보존).
    # 채택률이 높으면 '선행 이웃이 있었다'는 사실 자체는 자동으로 흔해진다 →
    # 관측 비중을 이 귀무와 비교해야 공간확산 신호라 말할 수 있다.
    null_test = None
    if permutations and total >= 10:
        rng = np.random.default_rng(seed)
        rid_all = [g["region_id"] for g in govs]
        yr_vec = np.array([y for _, y in ordered], dtype=int)
        obs_share = (paths["neighbor_first"] + paths["both"]) / total
        sims = np.empty(permutations)
        for b in range(permutations):
            picks = rng.choice(len(rid_all), size=total, replace=False)
            assign = {rid_all[i]: int(yr_vec[j]) for j, i in enumerate(picks)}
            hit = 0
            for rid, y in assign.items():
                nbs = adjacency.get(rid) or set()
                if any(assign.get(n) is not None and assign[n] < y for n in nbs):
                    hit += 1
            sims[b] = hit / total
        p = (int(np.sum(sims >= obs_share)) + 1) / (permutations + 1)
        null_test = {
            "statistic": "prior_adopting_neighbor_share",
            "observed": round(float(obs_share), 4),
            "null_mean": round(float(sims.mean()), 4),
            "null_sd": round(float(sims.std(ddof=0)), 4),
            "z": round(float((obs_share - sims.mean()) / sims.std(ddof=0)), 3)
                 if sims.std(ddof=0) > 0 else None,
            "p_sim": round(p, 5),
            "permutations": permutations,
            "note": ("귀무: 채택연도를 지자체에 무작위 재배정(연도분포 보존). "
                     "observed <= null_mean 이면 '선행 이웃' 은 확산 증거가 아니라 "
                     "높은 채택률의 부산물이다."),
        }

    return {
        "template": template, "mode": mode, "level": level,
        "universe": N,
        "adopters": total,
        "final_adoption_rate": round(total / N, 4),
        "window": [ymin, ymax],
        "curve": cum,
        "logistic": fits,
        "innovators": innovators,
        "rogers_categories": rogers,
        "path_decomposition": {
            "counts": paths,
            "shares": {k: round(v / total, 4) for k, v in paths.items()},
            "definition": ("neighbor_first=채택 이전에 인접 지자체 중 채택자 존재, "
                           "upper_first=소속 광역이 먼저 채택, both=둘 다, neither=선행 신호 없음"),
        },
        "path_null_test": null_test,
        "path_detail": detail,
        "adoption_meta": ad["meta"],
    }


def format_profile(p: dict) -> str:
    if p.get("error"):
        return f"[{p['template']}] {p['error']}"
    lines = [f"[{p['template']}] mode={p['mode']} 채택 {p['adopters']}/{p['universe']} "
             f"({p['final_adoption_rate']:.1%})  창 {p['window'][0]}-{p['window'][1]}"]
    for key, f in (p.get("logistic") or {}).items():
        lines.append(f"  로지스틱({key}): K={f['K']:.1f} r={f['r']:.3f}/yr "
                     f"t0={f['t0']:.1f} R2={f['r2']:.4f} RMSE={f['rmse']:.2f} "
                     f"10→90% {f['t_10_90_years']:.1f}년")
    pd = p["path_decomposition"]
    lines.append(f"  경로분해: {pd['counts']} → 비중 {pd['shares']}")
    nt = p.get("path_null_test")
    if nt:
        lines.append(f"  선행이웃 비중 검정: 관측 {nt['observed']:.3f} vs 귀무 "
                     f"{nt['null_mean']:.3f}±{nt['null_sd']:.3f}  z={nt['z']}  p={nt['p_sim']}")
    inn = ", ".join(f"{i['name']}({i['year']})" for i in p["innovators"][:6])
    lines.append(f"  혁신자: {inn}")
    return "\n".join(lines)


__all__ = ["fit_logistic_growth", "diffusion_profile", "format_profile"]
