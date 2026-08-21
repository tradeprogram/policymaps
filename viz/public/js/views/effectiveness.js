// 7. 조례 실효성 — api/effectiveness.json
//    ★ 조례-예산 링크는 확률적 자동매칭이다. "추정 연결" 배지 + confidence 등급 표기 필수.
import { el, num, won, pct, extLink } from "../util.js";
import { loadFixture } from "../api.js";
import { section, table, note, loading, asOfLine, fixtureMissingPanel, badge, statCard,
         confidenceBadge, statusBadge, envelopeFooter, cdnFailPanel } from "../components.js";
import { ensureChart } from "../vendor.js";

export async function render(root) {
  root.appendChild(loading("조례-예산 실효성 데이터를 불러오는 중…"));
  let env;
  try { env = await loadFixture("effectiveness"); }
  catch (e) { root.innerHTML = ""; root.appendChild(fixtureMissingPanel("effectiveness", e)); return; }
  root.innerHTML = "";

  const d = env.data || {};
  const t = d.totals || {};
  const v = d.verification || {};

  // ★ 최상단 경고 — 이 화면 전체가 추정치임을 감추지 않는다
  root.appendChild(el("div", { class: "banner banner-est", role: "note" },
    el("span", { class: "banner-tag", text: "추정 연결" }),
    el("span", { class: "banner-body", text:
      "조례↔예산 연결은 도메인명사 교집합·분야게이트·부서가중 3채널 자동매칭 결과다. "
      + "verified=1 인 링크만 '확인됨'이며, 나머지는 모두 추정이다. 아래 집행률은 참고치다." })
  ));

  root.appendChild(section("연결 요약",
    asOfLine(`engine=${d._engine || "?"}`),
    el("div", { class: "stat-grid" },
      statCard("링크 수", num(d.link_count), `예산 세부사업 ${num(d.budget_lines)}건`),
      statCard("확인됨(verified)", num(v.verified_links), "수작업 검증 완료"),
      statCard("자동매칭", num(v.auto_links), "미검증 — 추정"),
      statCard("편성액(alloc)", won(t.alloc_amt), null),
      statCard("예산현액", won(t.budget_now), null),
      statCard("지출액", won(t.exe_amt), null),
      statCard("집행률(현액 대비)", pct(t.exec_rate_vs_now), null),
      statCard("집행률(편성 대비)", pct(t.exec_rate_vs_alloc), null)
    ),
    el("div", { class: "chip-row" },
      badge(`검증 상태: ${v.status || "unknown"}`, v.status === "verified" ? "badge-verified" : "badge-warn"),
      d.min_confidence !== undefined ? badge(`min_confidence=${d.min_confidence}`, "badge-plain") : null,
      d.fyr_filter ? badge(`회계연도 필터 ${d.fyr_filter}`, "badge-plain") : badge("회계연도 전체", "badge-plain")
    ),
    v.note ? note(v.note, "warn") : null,
    d.caveat ? note(d.caveat, "warn") : null,
    note("등급 기준: verified=1 → 확인됨 / confidence≥0.8 → 추정(높음) / 0.6~0.8 → 추정(중간) / 0.6 미만 → 추정(낮음). "
      + "표본 584건 수작업 검증에서 전체 정밀도 64.9%, confidence≥0.8 구간 93.2%였다 "
      + "(검증 시점 링크 93,964건 기준이라 현재 모집단에 그대로 적용되지는 않는다).")
  ));

  // 회계연도별
  const fy = d.by_fiscal_year || [];
  if (fy.length) {
    const sec = section("회계연도별");
    root.appendChild(sec);
    const canvas = el("canvas", { height: "300" });
    sec.appendChild(el("div", { class: "chart-box chart-box-sm" }, canvas));
    const tbl = table(["회계연도", "링크", "편성액", "예산현액", "지출액", "집행률(현액)", "집행률(편성)", "지출기준일"],
      fy.map((r) => [r.fyr, num(r.lines), won(r.alloc_amt), won(r.budget_now), won(r.exe_amt),
        pct(r.exec_rate_vs_now), pct(r.exec_rate_vs_alloc), r.exe_ymd || "—"]));
    try {
      await ensureChart();
      new window.Chart(canvas.getContext("2d"), {
        data: {
          labels: fy.map((r) => r.fyr),
          datasets: [
            { type: "bar", label: "예산현액", data: fy.map((r) => r.budget_now), backgroundColor: "#bcd4f0" },
            { type: "bar", label: "지출액", data: fy.map((r) => r.exe_amt), backgroundColor: "#2c66a8" },
            { type: "line", label: "집행률(현액 대비)", data: fy.map((r) => r.exec_rate_vs_now),
              borderColor: "#c0392b", yAxisID: "y1", tension: 0.2 },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: {
            y: { beginAtZero: true, title: { display: true, text: "원" } },
            y1: { position: "right", beginAtZero: true, max: 1, grid: { drawOnChartArea: false },
                  title: { display: true, text: "집행률" } },
          },
        },
      });
      sec.appendChild(el("details", {}, el("summary", { text: "표로 보기" }), tbl));
    } catch (e) {
      canvas.parentElement.remove();
      sec.appendChild(cdnFailPanel("Chart.js(차트)", e, tbl));
    }
  }

  // 조례별
  const ords = d.by_ordinance || [];
  const sec2 = section(`조례별 연결 (${ords.length}건)`);
  root.appendChild(sec2);
  for (const o of ords) sec2.appendChild(ordinanceCard(o));

  if (d.region_budget_baseline) {
    root.appendChild(section("지역 예산 총량 대비",
      table(["항목", "값"], Object.entries(d.region_budget_baseline).map(([k, val]) =>
        [k, typeof val === "number" && Math.abs(val) > 1e6 ? won(val) : String(val)])),
      note("링크된 세부사업 금액이 지역 전체 예산에서 차지하는 비중을 보기 위한 참고값이다.")));
  }

  root.appendChild(envelopeFooter(env));
}

function ordinanceCard(o) {
  const programs = o.programs || [];
  const execNow = o.budget_now ? o.exe_amt / o.budget_now : (o.exec_rate_vs_now ?? null);
  const anyVerified = (o.verified_links || 0) > 0;

  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-head" },
    el("h3", { class: "card-title", text: o.name || o.ordinance_id }),
    el("div", { class: "chip-row" },
      o.status ? statusBadge(o.status) : null,
      badge(`세부사업 ${num(o.lines)}건`, "badge-info"),
      anyVerified ? badge(`확인됨 ${o.verified_links}건`, "badge-verified") : null,
      (o.auto_links || 0) > 0 ? badge(`추정 연결 ${o.auto_links}건`, "badge-est badge-mid") : null,
      o.verification_status ? badge(o.verification_status, "badge-plain") : null
    )));

  if (o.status === "repealed") {
    card.appendChild(el("div", { class: "caution" },
      el("b", { text: "⚠ 폐지된 조례 — " }),
      document.createTextNode("현행 정책의 근거로 인용하면 안 된다.")));
  }

  card.appendChild(el("div", { class: "kv-row" },
    kv("편성액", won(o.alloc_amt)), kv("예산현액", won(o.budget_now)),
    kv("지출액", won(o.exe_amt)), kv("집행률", pct(execNow)),
    o.region_id ? kv("지역", o.region_id) : null,
    o.official_url ? el("div", { class: "kv" }, el("span", { class: "kv-k", text: "원문" }),
      el("span", { class: "kv-v" }, extLink(o.official_url, "law.go.kr"))) : null
  ));

  if (o.methods) {
    card.appendChild(el("div", { class: "chip-row" },
      el("span", { class: "muted small", text: "매칭 채널: " }),
      ...Object.entries(o.methods).map(([m, c]) => badge(`${m} ${c}건`, "badge-plain"))));
  }

  card.appendChild(el("details", {},
    el("summary", { text: `연결된 세부사업 ${programs.length}건 — 개별 신뢰도 보기` }),
    table(["회계연도", "세부사업", "분야", "예산현액", "지출액", "집행률", "신뢰도", "매칭방법"],
      programs
        .slice()
        .sort((a, b) => (b.confidence || 0) - (a.confidence || 0))
        .map((p) => [
          p.fyr, p.dbiz_nm, p.field, won(p.budget_now), won(p.exe_amt), pct(p.exec_rate),
          confidenceBadge(p.confidence, p.verified), p.match_method || "—",
        ])),
    note("한 조례에 여러 회계연도·여러 분야의 사업이 붙는 것은 자동매칭 특성상 흔하다. "
      + "분야가 조례 주제와 어긋나는 행은 오매칭을 의심해야 한다.", "warn")
  ));

  return card;
}

function kv(k, v) {
  return el("div", { class: "kv" }, el("span", { class: "kv-k", text: k }), el("span", { class: "kv-v", text: v }));
}
