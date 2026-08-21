// 1. 대시보드 — 전국 요약 / 카테고리 분포 / 최근 변경 피드
import { el, num, won, dtime, mapLimit } from "../util.js";
import { LIMITS } from "../config.js";
import { state, loadManifest, loadRegionIndex, loadRegion, loadChangesLatest, loadGraphStats, categoryName } from "../api.js";
import { section, statCard, table, note, loading, asOfLine, errorPanel, cdnFailPanel, progressBar, badge } from "../components.js";
import { ensureChart } from "../vendor.js";
import { go } from "../router.js";

export async function render(root) {
  root.appendChild(loading("대시보드 데이터를 불러오는 중…"));
  let m, idx, changes, gstats;
  try {
    m = await loadManifest();
    [idx, changes, gstats] = await Promise.all([
      loadRegionIndex(),
      loadChangesLatest().catch(() => null),
      loadGraphStats().catch(() => null),
    ]);
  } catch (e) {
    root.innerHTML = "";
    root.appendChild(errorPanel(e, "manifest.json 또는 regions/index.json 을 읽지 못했습니다."));
    return;
  }
  root.innerHTML = "";

  const c = m.counts || {};
  root.appendChild(section("전국 요약",
    asOfLine(`번들 생성 ${dtime(m.generated_at)} · schema=${m.schema || "?"}`),
    el("div", { class: "stat-grid" },
      statCard("자치법규", num(c.ordinances), "조례·규칙 등(현행+폐지)"),
      statCard("법령·행정규칙", num(c.legal_instrument), "상위법 인스트루먼트"),
      statCard("위임관계", num(c.delegations), "조례 → 상위법 DELEGATED_FROM"),
      statCard("예산 세부사업", num(c.budget_lines), "budget_lines 행"),
      statCard("의안", num(c.bills), "국회 의안"),
      statCard("지역", num(c.regions), "시도·시군구·일반구(+교육청)"),
      statCard("변경 이력", num(c.change_log), "change_log 행"),
      statCard("그래프", `${num(c.graph_nodes)} / ${num(c.graph_edges)}`, "노드 / 엣지")
    ),
    note("counts 는 manifest.json 의 값을 그대로 표시한 것이다. "
      + "지역 shard 파일 수(regions/*.json)는 counts.regions 와 다를 수 있다 — export 대상에서 빠지는 유형이 있기 때문이다.")
  ));

  // 지역 index 요약
  const items = idx.items || idx.regions || [];
  const byLevel = {};
  for (const it of items) byLevel[it.level] = (byLevel[it.level] || 0) + 1;
  const levelLabel = { 1: "시도", 2: "시군구", 3: "일반구", 4: "교육청" };
  root.appendChild(section("지역 shard",
    el("div", { class: "chip-row" },
      ...Object.keys(byLevel).sort().map((lv) =>
        badge(`level ${lv} (${levelLabel[lv] || "기타"}) ${byLevel[lv]}개`, "badge-info"))
    ),
    note(`regions/index.json 에 ${items.length}개 shard 가 등재되어 있다.`)
  ));

  // 그래프 통계
  if (gstats) {
    const nodeRows = Object.entries(gstats.node_counts || {}).map(([k, v]) => [k, num(v)]);
    const edgeRows = Object.entries(gstats.edge_counts || {}).map(([k, v]) => [k, num(v)]);
    const skipped = gstats.skipped_edges || {};
    const skipTotal = Object.values(skipped).reduce((a, b) => a + (b || 0), 0);
    root.appendChild(section("그래프 구성",
      el("div", { class: "two-col" },
        el("div", {}, el("h3", { text: "노드" }), table(["label", "수"], nodeRows)),
        el("div", {}, el("h3", { text: "엣지" }), table(["relation", "수"], edgeRows))
      ),
      skipTotal > 0
        ? note(`정적 번들에서 제외된 엣지: ${Object.entries(skipped).map(([k, v]) => `${k} ${num(v)}건`).join(", ")}. `
          + "해당 관계는 그래프 화면에서 그릴 수 없다.", "warn")
        : note("제외된 엣지 없음(skipped_edges 전부 0).")
    ));
  }

  // 카테고리 분포
  const catPanel = section("정책분야 분포");
  root.appendChild(catPanel);
  renderCategories(catPanel, items).catch((e) => catPanel.appendChild(errorPanel(e, "카테고리 집계 실패")));

  // 최근 변경
  if (changes) {
    const rows = (changes.changes || []).slice(0, 30).map((ch) => [
      dtime(ch.ts),
      ch.source || "—",
      ch.entity_type || "—",
      ch.entity_name || ch.entity_id || "—",
      ch.event || "—",
    ]);
    root.appendChild(section("최근 변경",
      el("div", { class: "as-of", text: `changes/latest.json · 총 ${num(changes.count ?? (changes.changes || []).length)}건 중 최근 30건` }),
      table(["시각", "source", "entity_type", "대상", "event"], rows),
      (m.changes_months && m.changes_months.length)
        ? note(`월별 피드: ${m.changes_months.map((x) => `${x.month}(${num(x.count)}건)`).join(", ")}`)
        : null
    ));
  }
}

async function renderCategories(panel, items) {
  const holder = el("div");
  const pb = progressBar();
  panel.appendChild(pb);
  panel.appendChild(holder);

  // 조례 수 상위 지역 표본만 받는다 (전수 fetch 는 실데이터에서 284회가 되므로)
  const sample = [...items]
    .sort((a, b) => (b.ordinance_total || 0) - (a.ordinance_total || 0))
    .slice(0, LIMITS.categorySample);
  const docs = await mapLimit(sample, LIMITS.fetchConcurrency,
    (it) => loadRegion(it.sig_cd).catch(() => null),
    (d, t) => pb.update(d, t));
  pb.remove();

  const agg = new Map();
  let used = 0;
  for (const d of docs) {
    if (!d) continue;
    used++;
    for (const tc of d.top_categories || []) {
      agg.set(tc.code, (agg.get(tc.code) || 0) + (tc.count || 0));
    }
  }
  const rows = [...agg.entries()].sort((a, b) => b[1] - a[1]);
  if (!rows.length) {
    holder.appendChild(note("top_categories 데이터가 없습니다.", "warn"));
    return;
  }

  holder.appendChild(el("div", { class: "as-of", text:
    `조례 수 상위 ${used}개 지역 shard 의 top_categories 합산 (전국 전수 아님)` }));

  const canvas = el("canvas", { id: "cat-chart", height: "320" });
  holder.appendChild(el("div", { class: "chart-box" }, canvas));

  const labels = rows.map(([code]) => `${categoryName(code)} (${code})`);
  const values = rows.map(([, v]) => v);
  const total = values.reduce((a, b) => a + b, 0);
  const tbl = table(["분야", "코드", "조례 수", "비중"],
    rows.map(([code, v]) => [categoryName(code), code, num(v), ((v / total) * 100).toFixed(1) + "%"]));

  try {
    await ensureChart();
    new window.Chart(canvas.getContext("2d"), {
      type: "bar",
      data: { labels, datasets: [{ label: "조례 수(표본 합산)", data: values, backgroundColor: "#2c66a8" }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        indexAxis: "y",
        plugins: { legend: { display: false } },
        scales: { x: { beginAtZero: true } },
      },
    });
    holder.appendChild(el("details", {}, el("summary", { text: "표로 보기" }), tbl));
  } catch (e) {
    canvas.parentElement.remove();
    holder.appendChild(cdnFailPanel("Chart.js(차트)", e, tbl));
  }
}
