"""policymap.analytics.eha — 이산시간 사건사분석(Event History Analysis).

정책확산 계량의 표준 설계를 조례 데이터에 적용한다.

  * Berry & Berry (1990) "State Lottery Adoptions as Policy Innovations:
    An Event History Analysis", American Political Science Review 84(2): 395-415.
    → 채택을 사건(event)으로 두고 내부요인(재정·인구)과 외부요인(인접 주 채택)을
      동시에 넣는 통합모형(unified model). 본 모듈의 공변량 구성이 이를 따른다.
  * Shipan & Volden (2008) "The Mechanisms of Policy Diffusion",
    American Journal of Political Science 52(4): 840-857.
    → 학습·경쟁·모방·강제의 네 기제. 인접채택(수평)과 상위 광역채택(수직)을
      분리해 넣는 본 모듈의 설계가 여기 대응한다.
  * Valente (1996) "Social network thresholds in the diffusion of innovations",
    Social Networks 18(1): 69-89.
    → exposure = (채택한 이웃 수)/(이웃 수). neighbor_exposure 변수의 정의.
  * Allison (1982) "Discrete-Time Methods for the Analysis of Event Histories",
    Sociological Methodology 13: 61-98.
    → 위험집합 × 이산기간 패널을 만들어 로짓/보완로그로그로 추정하면
      이산시간 위험모형이 된다는 결과. 본 모듈의 추정 절차.
  * Liang, K.-Y. & Zeger, S.L. (1986) "Longitudinal data analysis using generalized
    linear models", Biometrika 73(1): 13-22.
    → 군집(지자체) 내 상관을 가정하지 않고도 일치추정되는 샌드위치 분산.
      패널 EHA 에서 보고해야 할 표준오차가 이것이다.

구현 범위
---------
  build_risk_set_panel : 위험집합 패널(지자체 × 연도, 채택 후 제거) 생성
  fit_discrete_time_hazard : logit / cloglog 링크 IRLS 추정
                             + 표준오차(관측정보행렬), + 지자체 클러스터 로버스트 SE
  estimate_diffusion_hazard : 위 둘을 잇는 고수준 진입점

**주의**: 계수 해석은 관측된 채택시점 자료의 한계(base.adoption_years 의 warning)를
그대로 물려받는다. mode='enactment' 와 'upper_bound' 양쪽에서 부호·유의성이 유지되는지
반드시 함께 보고할 것.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Optional, Sequence

import numpy as np

from . import base as _base

# --------------------------------------------------------------------------- #
# 정규분포 꼬리확률 (scipy 없이)
# --------------------------------------------------------------------------- #


def _two_sided_p(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


# --------------------------------------------------------------------------- #
# 1) 위험집합 패널
# --------------------------------------------------------------------------- #
def build_risk_set_panel(
    conn: sqlite3.Connection,
    template: str,
    *,
    y0: int,
    y1: int,
    level: int = 2,
    mode: str = "enactment",
    fyr: int = 2025,
    peer_m: int = 20,
    weights_kwargs: Optional[dict] = None,
) -> dict:
    """지자체 × 연도 위험집합 패널.

    규칙
      - 모집단: 조례 제정권이 있는 현행 기초자치단체(has_legislation=1).
      - y0 이전에 이미 채택한 지자체는 **좌측절단**으로 패널에서 제외하되,
        이웃 노출(exposure) 계산에는 '이미 채택함'으로 반영한다(정보 손실 최소화).
      - 각 지자체는 y0부터 위험집합에 있고, 채택연도에 event=1 행을 남기고 이탈한다.
      - 미채택 지자체는 y1까지 event=0 으로 우측절단된다.
      - 시변 공변량은 모두 **t-1 시점** 상태로 만든다(동시성 역인과 차단).

    공변량
      neighbor_exposure  t-1까지 채택한 **지리적 인접** 지자체 비율 (Valente 1996)
      peer_exposure      t-1까지 채택한 **행안부 유사자치단체 Top-m** 비율.
                         neighbor_exposure 와 동일 형식이라 head-to-head 비교가 된다.
                         (지리적 근접 vs 구조적 유사 — 어느 쪽이 채택을 예측하는가)
      neural_exposure    t-1까지 채택한 **그래프 신경망 임베딩 Top-m 유사 지자체** 비율.
                         위 둘과 같은 형식이라 세 확산 경로를 한 모형에서 비교할 수 있다.
                         통계지표(인구·재정)가 아니라 '어떤 조례를 어떤 상위법 아래 두고
                         어떤 이웃과 붙어 있는가' 라는 구조에서 학습된 유사도다.
                         Region 임베딩이 없으면 전부 None 이고 모형에서 자동 제외된다.
      sido_exposure      t-1까지 채택한 동일 광역 내 타 기초 비율
      upper_adopted      t-1까지 상위 광역자치단체가 같은 이름의 조례를 채택했는지(0/1)
      log_pop            log(인구)
      welfare_ratio      사회복지 예산 비중 (FY{fyr}, 시불변)
      fiscal_self_ratio  자체재원 비율 (FY{fyr}, 시불변)
      year_trend         t - y0 (선형 시간추세; 기저위험 근사)

    반환 {"rows": [...], "meta": {...}}
    """
    ad = _base.adoption_years(conn, template, level=level, mode=mode)
    years = ad["years"]
    govs = _base.active_local_governments(conn, level=level)
    gov_by_id = {g["region_id"]: g for g in govs}
    wk = dict(weights_kwargs or {})
    wk.setdefault("level", level)
    Wpack = _base.build_spatial_weights(conn, **wk)
    adjacency: dict[str, set[str]] = Wpack["adjacency"]
    cov = _base.region_covariates(conn, level=level, fyr=fyr)
    from . import peers as _peers
    pm = _peers.peer_matrix(conn, level=level, m=peer_m)
    # 그래프 신경망 임베딩 기준 유사 지자체. Region 임베딩이 없으면 빈 dict 이고,
    # 그 경우 neural_exposure 가 전부 None 이 되어 모형에서 자동으로 빠진다.
    nm = _peers.neural_peer_matrix(conn, m=peer_m)

    # 상위 광역의 동일 템플릿 채택연도(수직 확산)
    upper = _base.adoption_years(conn, template, level=1, mode=mode)["years"]
    sido_of_upper = {}
    for r in _base._rows(conn,
                         "SELECT region_id, sig_cd FROM regions WHERE level=1 AND status='active'"):
        sido_of_upper[r["region_id"][:2]] = r["region_id"]

    # 동일 광역 그룹
    sido_members: dict[str, list[str]] = {}
    for g in govs:
        sido_members.setdefault(g["sido_cd"], []).append(g["region_id"])

    left_truncated = sorted(rid for rid, y in years.items() if y < y0)
    rows: list[dict] = []
    n_events = 0
    for t in range(y0, y1 + 1):
        adopted_before = {rid for rid, y in years.items() if y <= t - 1}
        upper_before = {s for s, ry in sido_of_upper.items()
                        if ry in upper and upper[ry] <= t - 1}
        for g in govs:
            rid = g["region_id"]
            ay = years.get(rid)
            if ay is not None and ay < y0:
                continue                     # 좌측절단
            if ay is not None and ay < t:
                continue                     # 이미 채택 → 위험집합 이탈
            nbs = adjacency.get(rid) or set()
            n_nb = len(nbs)
            expo = (len(nbs & adopted_before) / n_nb) if n_nb else None
            peers = [p for p in sido_members.get(g["sido_cd"], []) if p != rid]
            sexpo = (len([p for p in peers if p in adopted_before]) / len(peers)) if peers else None
            pl = pm.get(rid) or []
            pexpo = (len([p for p in pl if p in adopted_before]) / len(pl)) if pl else None
            nl = nm.get(rid) or []
            nexpo = (len([p for p in nl if p in adopted_before]) / len(nl)) if nl else None
            c = cov.get(rid, {})
            pop = c.get("population")
            rows.append({
                "region_id": rid,
                "name": g.get("full_name"),
                "rtype": g.get("rtype"),
                "sido_cd": g.get("sido_cd"),
                "year": t,
                "event": 1 if ay == t else 0,
                "neighbor_exposure": expo,
                "n_neighbors": n_nb,
                "sido_exposure": sexpo,
                "peer_exposure": pexpo,
                "n_peers": len(pm.get(rid) or []),
                "neural_exposure": nexpo,
                "n_neural_peers": len(nl),
                "upper_adopted": 1.0 if g["sido_cd"] in upper_before else 0.0,
                "log_pop": math.log(pop) if pop and pop > 0 else None,
                "welfare_ratio": c.get("welfare_ratio"),
                "fiscal_self_ratio": c.get("fiscal_self_ratio"),
                "year_trend": float(t - y0),
            })
            if ay == t:
                n_events += 1

    return {
        "rows": rows,
        "meta": {
            "template": template, "mode": mode, "y0": y0, "y1": y1, "level": level,
            "universe": len(govs),
            "n_obs": len(rows), "n_events": n_events,
            "n_regions_in_panel": len({r["region_id"] for r in rows}),
            "left_truncated": len(left_truncated),
            "left_truncated_regions": [gov_by_id[r].get("full_name") for r in left_truncated][:20],
            "right_censored": len([g for g in govs
                                   if years.get(g["region_id"]) is None]),
            "adoption_meta": ad["meta"],
            "weights_meta": {k: v for k, v in Wpack["meta"].items()
                             if k in ("n_with_neighbors", "mean_cardinality",
                                      "standardize", "island_policy")},
        },
    }


# --------------------------------------------------------------------------- #
# 2) IRLS 추정기
# --------------------------------------------------------------------------- #
def _link_funcs(link: str):
    if link == "logit":
        def mu(eta):
            return 1.0 / (1.0 + np.exp(-np.clip(eta, -35, 35)))

        def dmu(eta):
            p = mu(eta)
            return p * (1.0 - p)
        return mu, dmu
    if link == "cloglog":
        def mu(eta):
            e = np.clip(eta, -35, 35)
            return 1.0 - np.exp(-np.exp(e))

        def dmu(eta):
            e = np.clip(eta, -35, 35)
            return np.exp(e - np.exp(e))
        return mu, dmu
    raise ValueError(f"unsupported link: {link}")


def fit_glm_binomial(
    X: np.ndarray,
    y: np.ndarray,
    names: Sequence[str],
    *,
    link: str = "logit",
    clusters: Optional[np.ndarray] = None,
    max_iter: int = 100,
    tol: float = 1e-10,
    ridge: float = 1e-8,
) -> dict:
    """이항 GLM(IRLS) 직접 구현. logit/cloglog 링크.

    표준오차 2종
      se        : 관측정보행렬 (X'WX)^-1 의 대각 제곱근 (독립 가정)
      se_robust : 지자체 클러스터 샌드위치. 동일 지자체의 연도별
                  관측치는 독립이 아니므로 패널 EHA 에서는 이쪽을 보고해야 한다.
                  유한표본 보정 c = G/(G-1) * (N-1)/(N-k).
    """
    mu, dmu = _link_funcs(link)
    n, k = X.shape
    beta = np.zeros(k)
    # 절편 초기값을 사건비율로
    ybar = float(np.clip(y.mean(), 1e-6, 1 - 1e-6))
    if link == "logit":
        beta[0] = math.log(ybar / (1 - ybar))
    else:
        beta[0] = math.log(-math.log(1 - ybar))

    converged = False
    for _ in range(max_iter):
        eta = X @ beta
        p = np.clip(mu(eta), 1e-10, 1 - 1e-10)
        d = np.clip(dmu(eta), 1e-10, None)
        w = d * d / (p * (1 - p))              # IRLS 작업가중치
        z = eta + (y - p) / d                  # 작업반응
        XtW = X.T * w
        H = XtW @ X + ridge * np.eye(k)
        try:
            new = np.linalg.solve(H, XtW @ z)
        except np.linalg.LinAlgError:
            new = np.linalg.pinv(H) @ (XtW @ z)
        if np.max(np.abs(new - beta)) < tol:
            beta = new
            converged = True
            break
        beta = new

    eta = X @ beta
    p = np.clip(mu(eta), 1e-10, 1 - 1e-10)
    d = np.clip(dmu(eta), 1e-10, None)
    w = d * d / (p * (1 - p))
    XtW = X.T * w
    H = XtW @ X + ridge * np.eye(k)
    try:
        cov = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        cov = np.linalg.pinv(H)
    se = np.sqrt(np.clip(np.diag(cov), 0, None))

    # 스코어(사건 기여) — 샌드위치용
    u = (X.T * ((y - p) * d / (p * (1 - p)))).T          # n x k
    se_robust = None
    n_clusters = None
    if clusters is not None:
        uniq = np.unique(clusters)
        n_clusters = int(uniq.size)
        meat = np.zeros((k, k))
        for g in uniq:
            ug = u[clusters == g].sum(axis=0)
            meat += np.outer(ug, ug)
        c = (n_clusters / max(1, n_clusters - 1)) * ((n - 1) / max(1, n - k))
        Vr = cov @ (c * meat) @ cov
        se_robust = np.sqrt(np.clip(np.diag(Vr), 0, None))

    ll = float(np.sum(y * np.log(p) + (1 - y) * np.log(1 - p)))
    p0 = float(np.clip(y.mean(), 1e-10, 1 - 1e-10))
    ll0 = float(np.sum(y * math.log(p0) + (1 - y) * math.log(1 - p0)))

    se_use = se_robust if se_robust is not None else se
    terms = []
    for i, nm in enumerate(names):
        s = float(se_use[i])
        z_ = float(beta[i] / s) if s > 0 else float("nan")
        terms.append({
            "term": nm,
            "coef": float(beta[i]),
            "se": float(se[i]),
            "se_robust": float(se_robust[i]) if se_robust is not None else None,
            "z": z_,
            "p_value": _two_sided_p(z_) if s > 0 and not math.isnan(z_) else None,
            "odds_ratio": float(np.exp(beta[i])) if link == "logit" else None,
            "or_ci95": ([float(np.exp(beta[i] - 1.959964 * s)),
                         float(np.exp(beta[i] + 1.959964 * s))] if link == "logit" and s > 0
                        else None),
        })
    return {
        "link": link, "converged": converged, "n_obs": int(n), "n_params": int(k),
        "n_events": int(y.sum()), "n_clusters": n_clusters,
        "terms": terms,
        "log_likelihood": ll, "ll_null": ll0,
        "mcfadden_r2": float(1 - ll / ll0) if ll0 != 0 else None,
        "lr_chi2": float(2 * (ll - ll0)),
        "aic": float(-2 * ll + 2 * k),
        "se_reported": "cluster_robust" if se_robust is not None else "observed_information",
    }


# --------------------------------------------------------------------------- #
# 3) 고수준 진입점
# --------------------------------------------------------------------------- #
DEFAULT_COVARIATES = (
    "neighbor_exposure", "sido_exposure", "upper_adopted",
    "log_pop", "welfare_ratio", "fiscal_self_ratio", "year_trend",
)


def estimate_diffusion_hazard(
    conn: sqlite3.Connection,
    template: str,
    *,
    y0: int,
    y1: int,
    level: int = 2,
    mode: str = "enactment",
    covariates: Sequence[str] = DEFAULT_COVARIATES,
    link: str = "logit",
    standardize: bool = True,
    fyr: int = 2025,
    peer_m: int = 20,
) -> dict:
    """이산시간 위험모형 추정 원스톱.

    standardize=True 면 연속 공변량을 패널 내 z-표준화한다(계수 = 1표준편차 효과).
    upper_adopted 같은 0/1 변수는 표준화하지 않는다.
    """
    panel = build_risk_set_panel(conn, template, y0=y0, y1=y1, level=level,
                                 mode=mode, fyr=fyr, peer_m=peer_m)
    rows = panel["rows"]
    use = [r for r in rows if all(r.get(c) is not None for c in covariates)]
    dropped = len(rows) - len(use)
    if not use or sum(r["event"] for r in use) < 5:
        return {"template": template, "model": None, "panel_meta": panel["meta"],
                "error": "관측 사건 수 부족(<5) 또는 공변량 결측으로 추정 불가",
                "n_usable": len(use),
                "n_events_usable": sum(r["event"] for r in use) if use else 0}

    binary = {"upper_adopted"}
    Xcols, names, scaling = [], ["(intercept)"], {}
    Xcols.append(np.ones(len(use)))
    dropped_constant = []
    for c in covariates:
        v = np.array([float(r[c]) for r in use])
        if v.std(ddof=0) == 0:
            # 상수열은 절편과 완전공선 → 계수 0·SE 0·z=nan 을 내놓는다. 아예 뺀다.
            dropped_constant.append({"term": c, "constant_value": float(v[0])})
            continue
        if standardize and c not in binary:
            scaling[c] = {"mean": float(v.mean()), "sd": float(v.std(ddof=0))}
            v = (v - v.mean()) / v.std(ddof=0)
        Xcols.append(v)
        names.append(c)
    X = np.column_stack(Xcols)
    y = np.array([float(r["event"]) for r in use])
    reg_ids = np.array([r["region_id"] for r in use])

    fit = fit_glm_binomial(X, y, names, link=link, clusters=reg_ids)
    fit["scaling"] = scaling
    fit["standardized"] = standardize
    fit["rows_dropped_missing_covariate"] = dropped
    fit["dropped_constant_terms"] = dropped_constant
    return {
        "template": template,
        "mode": mode,
        "window": [y0, y1],
        "model": fit,
        "panel_meta": panel["meta"],
    }


def format_table(result: dict) -> str:
    """추정 결과를 콘솔 표로."""
    m = result.get("model")
    if not m:
        return f"[{result.get('template')}] 추정 불가: {result.get('error')}"
    head = (f"[{result['template']}] {result['window'][0]}-{result['window'][1]} "
            f"mode={result['mode']} link={m['link']}  "
            f"N={m['n_obs']} events={m['n_events']} clusters={m['n_clusters']}  "
            f"McFadden R2={m['mcfadden_r2']:.4f}  SE={m['se_reported']}")
    lines = [head,
             f"{'term':<22}{'coef':>10}{'SE(cl)':>10}{'z':>8}{'p':>10}{'OR':>9}"
             f"{'  OR 95% CI':>22}"]
    for t in m["terms"]:
        se = t["se_robust"] if t["se_robust"] is not None else t["se"]
        ci = t.get("or_ci95")
        cis = f"[{ci[0]:.3f}, {ci[1]:.3f}]" if ci else ""
        orv = f"{t['odds_ratio']:.3f}" if t["odds_ratio"] is not None else ""
        star = ("***" if (t["p_value"] or 1) < 0.001 else
                "**" if (t["p_value"] or 1) < 0.01 else
                "*" if (t["p_value"] or 1) < 0.05 else
                "†" if (t["p_value"] or 1) < 0.10 else "")
        lines.append(f"{t['term']:<22}{t['coef']:>10.4f}{se:>10.4f}{t['z']:>8.2f}"
                     f"{(t['p_value'] if t['p_value'] is not None else float('nan')):>10.4f}"
                     f"{orv:>9}{cis:>22} {star}")
    return "\n".join(lines)


__all__ = ["build_risk_set_panel", "fit_glm_binomial", "estimate_diffusion_hazard",
           "format_table", "DEFAULT_COVARIATES"]
