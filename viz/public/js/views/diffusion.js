// 6. 정책 생애주기 / 확산 타임라인
//    전국 shard: api/diffusion/{템플릿}.json (목록은 api/index.json 의 diffusion)
//    폴백:       api/diffusion.json 단일 1건
import { el, num, pct } from "../util.js";
import { loadCatalog, loadCatalogItem } from "../api.js";
import { section, table, note, loading, asOfLine, badge,
         envelopeFooter, cdnFailPanel, statCard } from "../components.js";
import { catalogSelector, notPrecomputedPanel, sourceLine } from "../nationwide.js";
import { ensureChart } from "../vendor.js";

export async function render(root) {
  root.appendChild(loading("정책 목록을 불러오는 중…"));
  const entries = await loadCatalog("diffusion");
  root.innerHTML = "";

  const picker = catalogSelector({
    entries, current: entries.length ? entries[0].key : null,
    label: "정책 템플릿", onChange: (k) => draw(k),
  });
  if (picker) root.appendChild(picker);
  else if (!entries.length) {
    root.appendChild(note(
      "정책 템플릿 목록(api/index.json 의 diffusion)이 없어 번들에 있는 1건만 표시한다. "
      + "make_nationwide.py 로 템플릿별 shard 를 구우면 여기에 선택기가 붙는다."));
  } else {
    root.appendChild(note(
      `사전계산된 정책이 「${entries[0].label}」 1건뿐이라 선택기를 띄우지 않는다. `
      + "make_nationwide.py 로 템플릿을 더 구우면 선택기가 붙는다."));
  }
  root.appendChild(note(
    "확산 곡선은 rr_cls_cd='제정' 본이 있는 조례만 채택 시점을 관측한다. 개정 이력만 남은 조례는 채택 시점이 "
    + "잡히지 않아 채택률이 실제보다 낮게 나온다 — 템플릿마다 아래 '제정본 커버리지' 배지를 함께 읽어야 한다.", "warn"));

  const body = el("div", {});
  root.appendChild(body);

  let token = 0;
  async function draw(key) {
    const my = ++token;
    body.innerHTML = "";
    body.appendChild(loading("확산 타임라인을 불러오는 중…"));
    const entry = entries.find((e) => e.key === String(key)) || null;
    const res = await loadCatalogItem("diffusion", entry);
    if (my !== token) return;
    body.innerHTML = "";
    if (!res.data) {
      body.appendChild(notPrecomputedPanel({
        kind: "diffusion", sig: key, name: entry ? entry.label : null,
        subject: "정책",
        tried: res.tried || (res.path ? [res.path] : []),
        fixtureRegions: entries.map((e) => e.key).filter((k) => k !== String(key)),
        onPick: (k) => draw(k),
      }));
      return;
    }
    await renderBody(body, res.data, res.env, res);
  }

  await draw(entries.length ? entries[0].key : null);
}


/**
 * 채택 판정 근거 표기. 제정본 커버리지(enactment_date_coverage)가 낮으면 곡선이 붕괴하므로
 * 수치를 숨기지 않고 배지로 띄운다. 구(舊) 필드(filter/matched_ordinances)도 받는다.
 */
function adoptionMeta(am) {
  if (!am) return null;
  const cov = typeof am.enactment_date_coverage === "number" ? am.enactment_date_coverage : null;
  const box = el("div", {},
    el("div", { class: "chip-row" },
      badge(`채택 판정 ${am.filter || `${am.mode || "?"} · ${am.ord_kind || "조례"}`}`, "badge-plain"),
      am.holders_any_version !== undefined
        ? badge(`보유 ${num(am.holders_any_version)}곳 중 제정본 ${num(am.adopters_observed)}곳`, "badge-info")
        : (am.matched_ordinances !== undefined ? badge(`매칭 조례 ${num(am.matched_ordinances)}건`, "badge-info") : null),
      cov !== null ? badge(`제정본 커버리지 ${pct(cov, 1)}`, cov < 0.7 ? "badge-warn" : "badge-active") : null
    )
  );
  if (cov !== null && cov < 0.7) {
    box.appendChild(note(
      `제정본 커버리지가 ${pct(cov, 1)} 에 그친다. 채택 시점을 관측하지 못한 지자체가 많아 `
      + "채택률·로지스틱 적합이 실제보다 낮게/늦게 나온다. 이 템플릿의 곡선은 그대로 인용하면 안 된다.", "warn"));
  }
  if (am.warning) box.appendChild(note(am.warning, "warn"));
  return box;
}

/** 템플릿 1건의 확산 결과 렌더 */
async function renderBody(root, d, env, res) {
  const curve = d.curve || [];

  root.appendChild(section(`정책 확산 — 「${d.template || "?"}」`,
    asOfLine(`mode=${d.mode || "?"} · level=${d.level ?? "?"} · engine=${d._engine || "?"}`),
    el("div", { class: "stat-grid" },
      statCard("모집단", num(d.universe), `level ${d.level} 지자체`),
      statCard("채택", num(d.adopters), "해당 정책 보유"),
      statCard("최종 채택률", pct(d.final_adoption_rate, 1), null),
      statCard("관측 구간", (d.window || []).join(" ~ "), `${curve.length}개 연도`)
    ),
    adoptionMeta(d.adoption_meta)
  ));

  // 확산 곡선
  const curveSec = section("연도별 채택 추이");
  root.appendChild(curveSec);
  const canvas = el("canvas", { height: "340" });
  curveSec.appendChild(el("div", { class: "chart-box" }, canvas));
  const curveTbl = table(["연도", "신규 채택", "누적", "채택률"],
    curve.map((c) => [c.year, num(c.new), num(c.cumulative), pct(c.adoption_rate, 2)]));

  try {
    await ensureChart();
    const datasets = [
      { type: "bar", label: "신규 채택(건)", data: curve.map((c) => c.new), backgroundColor: "#8ab4e2", yAxisID: "y" },
      { type: "line", label: "누적 채택(건)", data: curve.map((c) => c.cumulative), borderColor: "#2c66a8",
        backgroundColor: "#2c66a8", tension: 0.25, yAxisID: "y" },
    ];
    // 로지스틱 적합 곡선 겹치기
    const fits = d.logistic || {};
    const fitColors = { K_fixed_universe: "#c0392b", K_free: "#e67e22" };
    for (const [key, f] of Object.entries(fits)) {
      if (!f || typeof f.K !== "number") continue;
      datasets.push({
        type: "line",
        label: `로지스틱 적합 ${key} (R²=${f.r2})`,
        data: curve.map((c) => f.K / (1 + Math.exp(-f.r * (c.year - f.t0)))),
        borderColor: fitColors[key] || "#999",
        borderDash: [6, 4], pointRadius: 0, tension: 0.3, yAxisID: "y",
      });
    }
    new window.Chart(canvas.getContext("2d"), {
      data: { labels: curve.map((c) => c.year), datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: { y: { beginAtZero: true, title: { display: true, text: "지자체 수" } } },
      },
    });
    curveSec.appendChild(el("details", {}, el("summary", { text: "표로 보기" }), curveTbl));
  } catch (e) {
    canvas.parentElement.remove();
    curveSec.appendChild(cdnFailPanel("Chart.js(차트)", e, curveTbl));
  }

  // 로지스틱 파라미터
  const fits = d.logistic || {};
  if (Object.keys(fits).length) {
    curveSec.appendChild(el("details", { class: "method" },
      el("summary", { text: "로지스틱 적합 파라미터" }),
      table(["설정", "K(포화)", "r(성장률)", "t0(변곡)", "R²", "RMSE", "10→90% 소요"],
        Object.entries(fits).map(([k, f]) => [k, num(f.K), f.r, f.t0, f.r2, f.rmse, `${f.t_10_90_years}년`])),
      note("K_fixed_universe 는 포화점을 모집단 전체로 고정한 적합, K_free 는 포화점도 추정한 적합이다. "
        + "두 값이 크게 다르면 '모두가 채택하지는 않는 정책'일 가능성이 있다.")
    ));
  }

  // Rogers 분류
  const rg = d.rogers_categories || {};
  if (Object.keys(rg).length) {
    const labels = { innovators: "혁신가", early_adopters: "초기 채택자", early_majority: "전기 다수",
                     late_majority: "후기 다수", laggards: "지각 수용자", never_adopted: "미채택" };
    root.appendChild(section("채택 시기 분포 (Rogers)",
      table(["구분", "지자체 수", "연도 범위", "모집단 대비"],
        Object.entries(rg).map(([k, v]) => [
          labels[k] || k, num(v.n),
          v.year_range ? v.year_range.join(" ~ ") : "—",
          v.share_of_universe !== undefined ? pct(v.share_of_universe, 1) : "—",
        ]))
    ));
  }

  // 혁신가
  if ((d.innovators || []).length) {
    root.appendChild(section("최초 채택 지자체",
      table(["지자체", "유형", "채택 연도"],
        d.innovators.map((i) => [i.name, i.rtype || "—", i.year]))));
  }

  // 확산 경로
  const pd = d.path_decomposition;
  const nt = d.path_null_test;
  if (pd || nt) {
    const sec = section("확산 경로 분해");
    root.appendChild(sec);
    if (pd) {
      const lab = { neighbor_first: "이웃 먼저", upper_first: "상위 지자체 먼저", both: "둘 다", neither: "선행 신호 없음" };
      sec.appendChild(table(["경로", "지자체 수", "비중"],
        Object.entries(pd.counts || {}).map(([k, v]) => [lab[k] || k, num(v), pct((pd.shares || {})[k], 1)])));
      if (pd.definition) sec.appendChild(note(pd.definition));
    }
    if (nt) {
      const significant = typeof nt.p_sim === "number" && nt.p_sim < 0.05;
      sec.appendChild(el("div", { class: "chip-row" },
        badge(`관측 ${nt.observed}`, "badge-plain"),
        badge(`귀무 평균 ${nt.null_mean} (sd ${nt.null_sd})`, "badge-plain"),
        badge(`z=${nt.z}`, "badge-plain"),
        badge(`p=${nt.p_sim}`, significant ? "badge-active" : "badge-warn"),
        badge(`순열 ${num(nt.permutations)}회`, "badge-plain")));
      sec.appendChild(note(nt.note || "", significant ? "" : "warn"));
      if (!significant) {
        sec.appendChild(note(
          "p 값이 유의수준에 못 미친다. 즉 '이웃을 따라 퍼졌다'는 인과 주장을 이 데이터로는 할 수 없다. "
          + "관측된 이웃 선행 비율은 전반적인 채택률의 부산물일 수 있다.", "warn"));
      }
    }
  }

  if (d.interpretation_caveat) root.appendChild(note(d.interpretation_caveat, "warn"));
  const src = sourceLine(res);
  if (src) root.appendChild(src);
  root.appendChild(envelopeFooter(env));
}
