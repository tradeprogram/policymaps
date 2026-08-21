// 6. 정책 생애주기 / 확산 타임라인 — api/diffusion.json
import { el, num, pct } from "../util.js";
import { loadFixture } from "../api.js";
import { section, table, note, loading, asOfLine, fixtureMissingPanel, badge,
         envelopeFooter, cdnFailPanel, statCard } from "../components.js";
import { ensureChart } from "../vendor.js";

export async function render(root) {
  root.appendChild(loading("확산 타임라인을 불러오는 중…"));
  let env;
  try { env = await loadFixture("diffusion"); }
  catch (e) { root.innerHTML = ""; root.appendChild(fixtureMissingPanel("diffusion", e)); return; }
  root.innerHTML = "";

  const d = env.data || {};
  const curve = d.curve || [];

  root.appendChild(section(`정책 확산 — 「${d.template || "?"}」`,
    asOfLine(`mode=${d.mode || "?"} · level=${d.level ?? "?"} · engine=${d._engine || "?"}`),
    el("div", { class: "stat-grid" },
      statCard("모집단", num(d.universe), `level ${d.level} 지자체`),
      statCard("채택", num(d.adopters), "해당 정책 보유"),
      statCard("최종 채택률", pct(d.final_adoption_rate, 1), null),
      statCard("관측 구간", (d.window || []).join(" ~ "), `${curve.length}개 연도`)
    ),
    d.adoption_meta ? note(`채택 판정 조건: ${d.adoption_meta.filter || "—"} · 매칭 조례 ${num(d.adoption_meta.matched_ordinances)}건`) : null
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
  root.appendChild(envelopeFooter(env));
}
