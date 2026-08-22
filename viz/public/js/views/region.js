// 3. 지역 상세 — regions/{sig_cd}.json 소비.
//    지역 선택기는 전국(시도별 optgroup + 검색). 목록 출처는 regions/index.json + api/index.json.
import { el, num, won, pct, dtime, extLink } from "../util.js";
import { loadRegion, loadRegionCatalog, categoryName, state } from "../api.js";
import { regionSelector } from "../nationwide.js";
import { section, statCard, table, note, loading, asOfLine, errorPanel, badge, statusBadge, cdnFailPanel } from "../components.js";
import { ensureChart } from "../vendor.js";
import { go } from "../router.js";

export async function render(root, params) {
  const sig = params.sig;
  root.appendChild(loading(`${sig} 지역 데이터를 불러오는 중…`));

  let cat = null;
  try { cat = await loadRegionCatalog(); } catch (e) { /* 선택기만 못 그림 */ }

  let doc;
  try {
    doc = await loadRegion(sig);
  } catch (e) {
    root.innerHTML = "";
    root.appendChild(regionPicker(cat, sig));
    root.appendChild(errorPanel(e, `regions/${sig}.json 을 읽지 못했습니다. 현재 데이터 소스에 없는 지역일 수 있습니다.`));
    return;
  }
  root.innerHTML = "";
  root.appendChild(regionPicker(cat, sig));

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
    note("유사 지자체·격차분석·실효성 화면은 사전계산 결과를 소비한다. "
      + "전국 shard(api/gap/{sig}.json 등)가 있으면 그것을, 없으면 기존 단일 파일(api/gap.json)을 쓰고, "
      + "둘 다 없는 지역은 '아직 사전계산되지 않았습니다' 안내가 뜬다.")
  ));
}

function regionPicker(cat, current) {
  if (!cat || !(cat.all || []).length) return el("div", { class: "toolbar" },
    el("span", { class: "muted small", text: "지역 목록(regions/index.json)을 읽지 못해 선택기를 그리지 못했습니다." }));

  // 지역 상세는 일반구(level 3)도 shard 가 있으므로 전체 목록을 쓴다.
  const items = cat.all.slice();
  if (!items.some((x) => x.sig_cd === String(current))) {
    items.push({ sig_cd: String(current), name: null, level: null, sido: cat.sidoOf(current) });
    items.sort((a, b) => (a.sig_cd < b.sig_cd ? -1 : a.sig_cd > b.sig_cd ? 1 : 0));
  }
  const covered = new Set(items.filter((x) => x.hasRegionShard).map((x) => x.sig_cd));

  const picker = regionSelector({
    items, sidoOf: cat.sidoOf, current: String(current), covered,
    label: "지역", coveredLabel: "shard 있는 곳만", coveredWord: "shard 보유",
    onChange: (sig) => go(`/region/${sig}`),
  });
  picker.appendChild(el("span", { class: "muted small",
    text: `전국 ${items.length}곳 · shard 보유 ${covered.size}곳(✓) · 목록 출처 ${(cat.sources || []).join(", ") || "없음"}` }));
  return picker;
}
