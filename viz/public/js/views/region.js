// 3. 지역 상세 — regions/{sig_cd}.json 소비
import { el, num, won, pct, dtime, extLink } from "../util.js";
import { loadRegion, loadRegionIndex, categoryName, state } from "../api.js";
import { section, statCard, table, note, loading, asOfLine, errorPanel, badge, statusBadge, cdnFailPanel } from "../components.js";
import { ensureChart } from "../vendor.js";
import { go } from "../router.js";

export async function render(root, params) {
  const sig = params.sig;
  root.appendChild(loading(`${sig} 지역 데이터를 불러오는 중…`));

  let idx = null;
  try { idx = await loadRegionIndex(); } catch (e) { /* 선택자만 못 그림 */ }

  let doc;
  try {
    doc = await loadRegion(sig);
  } catch (e) {
    root.innerHTML = "";
    root.appendChild(regionPicker(idx, sig));
    root.appendChild(errorPanel(e, `regions/${sig}.json 을 읽지 못했습니다. 현재 데이터 소스에 없는 지역일 수 있습니다.`));
    return;
  }
  root.innerHTML = "";
  root.appendChild(regionPicker(idx, sig));

  const b = doc.budget || {};
  const execRate = b.budget_now ? b.exe_amt / b.budget_now : null;

  root.appendChild(section(doc.full_name || doc.name || sig,
    el("div", { class: "chip-row" },
      badge(`sig_cd ${doc.sig_cd}`, "badge-info"),
      badge(`region_id ${doc.region_id}`, "badge-info"),
      badge(`level ${doc.level}`, "badge-info"),
      statusBadge(doc.status)
    ),
    asOfLine(doc.as_of_date && doc.as_of_date !== state.asOfDate ? `shard 기준일 ${doc.as_of_date}` : null),
    doc.stale ? note("이 지역 shard 는 stale=true 입니다. 최신 상태가 아닐 수 있습니다.", "warn") : null,
    el("div", { class: "stat-grid" },
      statCard("자치법규", num(doc.ordinance_total),
        Object.entries(doc.ordinance_kinds || {}).map(([k, v]) => `${k} ${num(v)}`).join(" · ") || null),
      statCard("인구", doc.population ? num(doc.population) : "—", "주민등록"),
      statCard("예산 세부사업", num(b.lines), null),
      statCard("예산현액", won(b.budget_now), null),
      statCard("지출액", won(b.exe_amt), null),
      statCard("집행률", pct(execRate), "지출액 / 예산현액")
    ),
    note("집행률은 예산 원장 기준 수치이며 개별 조례의 정책 효과를 뜻하지 않는다. "
      + "회계연도 진행 중인 당해년도 스냅샷은 낮게 나오는 것이 정상이다.")
  ));

  // 카테고리
  const cats = doc.top_categories || [];
  const catPanel = section("정책분야 구성");
  root.appendChild(catPanel);
  if (!cats.length) {
    catPanel.appendChild(note("top_categories 가 비어 있습니다.", "warn"));
  } else {
    const total = cats.reduce((a, x) => a + (x.count || 0), 0);
    const sorted = [...cats].sort((a, x) => x.count - a.count);
    const tbl = table(["분야", "코드", "조례 수", "비중"],
      sorted.map((c) => [categoryName(c.code), c.code, num(c.count), pct(c.count / total, 1)]));
    const canvas = el("canvas", { height: "300" });
    catPanel.appendChild(el("div", { class: "chart-box chart-box-sm" }, canvas));
    try {
      await ensureChart();
      new window.Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
          labels: sorted.map((c) => categoryName(c.code)),
          datasets: [{ label: "조례 수", data: sorted.map((c) => c.count), backgroundColor: "#2c66a8" }],
        },
        options: {
          responsive: true, maintainAspectRatio: false, indexAxis: "y",
          plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true } },
        },
      });
      catPanel.appendChild(el("details", {}, el("summary", { text: "표로 보기" }), tbl));
    } catch (e) {
      canvas.parentElement.remove();
      catPanel.appendChild(cdnFailPanel("Chart.js(차트)", e, tbl));
    }
    catPanel.appendChild(note(
      `top_categories 합계 ${num(total)}건은 자치법규 총수 ${num(doc.ordinance_total)}건과 다를 수 있다 — `
      + "한 조례가 여러 분야로 분류되거나 미분류인 경우가 있기 때문이다."));
  }

  // 최근 변경
  const ch = doc.recent_changes || [];
  root.appendChild(section("최근 변경",
    ch.length
      ? table(["시각", "entity_type", "대상", "event", "원문"],
        ch.map((c) => [
          dtime(c.ts),
          c.entity_type || "—",
          c.entity_name || c.entity_id || "—",
          c.event || "—",
          extLink(c.official_url, c.official_url ? "law.go.kr" : "—"),
        ]))
      : note("이 지역의 최근 변경 이력이 없습니다.")
  ));

  root.appendChild(section("다음 화면",
    el("div", { class: "chip-row" },
      el("button", { class: "btn", text: "유사 지자체 · 격차분석", onclick: () => go("/gap") }),
      el("button", { class: "btn", text: "조례 실효성", onclick: () => go("/effectiveness") }),
      el("button", { class: "btn", text: "지도로 돌아가기", onclick: () => go("/map") })
    ),
    note("유사 지자체·격차분석·실효성 화면은 사전계산 fixture(api/*.json)를 소비한다. "
      + "가상데이터에서는 특정 기준 지역 1곳에 대한 결과만 들어 있다.")
  ));
}

function regionPicker(idx, current) {
  const items = idx ? (idx.items || idx.regions || []) : [];
  const sel = el("select", { class: "sel" },
    ...items.map((it) => el("option", { value: it.sig_cd, selected: String(it.sig_cd) === String(current) ? "selected" : null,
      text: `${it.name} (${it.sig_cd})` })));
  sel.addEventListener("change", () => go(`/region/${sel.value}`));
  return el("div", { class: "toolbar" },
    el("label", { text: "지역 선택 " }), sel,
    el("span", { class: "muted", text: ` · shard ${items.length}개` })
  );
}
