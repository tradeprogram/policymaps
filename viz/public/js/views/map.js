// 2. 지도 — 시군구 코로플레스. 지표 전환 + 지역 클릭 상세.
import { el, num, won, pct, quantileBreaks, classOf, CHOROPLETH_COLORS, mapLimit } from "../util.js";
import { LIMITS } from "../config.js";
import { loadRegionIndex, loadRegion, loadGeo, categoryName, state } from "../api.js";
import { section, table, note, loading, asOfLine, errorPanel, cdnFailPanel, progressBar, badge } from "../components.js";
import { ensureLeaflet } from "../vendor.js";
import { go } from "../router.js";

const METRICS = {
  ordinance_total: {
    label: "자치법규 수",
    needsShard: false,
    fmt: (v) => num(v),
    get: (row) => row.ordinance_total ?? null,
  },
  exec_rate: {
    label: "예산 집행률 (지출/예산현액)",
    needsShard: true,
    fmt: (v) => pct(v),
    get: (row) => {
      const b = row.shard?.budget;
      if (!b || !b.budget_now) return null;
      return b.exe_amt / b.budget_now;
    },
  },
  budget_now: {
    label: "예산현액",
    needsShard: true,
    fmt: (v) => won(v),
    get: (row) => row.shard?.budget?.budget_now ?? null,
  },
  category: {
    label: "선택 분야 조례 수",
    needsShard: true,
    fmt: (v) => num(v),
    get: (row, opt) => {
      const tc = (row.shard?.top_categories || []).find((x) => x.code === opt);
      return tc ? tc.count : 0;
    },
  },
  category_share: {
    label: "선택 분야 비중",
    needsShard: true,
    fmt: (v) => pct(v),
    get: (row, opt) => {
      const cats = row.shard?.top_categories || [];
      const tot = cats.reduce((a, b) => a + (b.count || 0), 0);
      if (!tot) return null;
      const tc = cats.find((x) => x.code === opt);
      return (tc ? tc.count : 0) / tot;
    },
  },
};

let ctx = null; // {rows, geo, layer, map}

export async function render(root) {
  root.appendChild(loading("지역 목록과 경계 데이터를 불러오는 중…"));

  let idx, geo;
  try {
    idx = await loadRegionIndex();
  } catch (e) {
    root.innerHTML = "";
    root.appendChild(errorPanel(e, "regions/index.json 로드 실패"));
    return;
  }
  try {
    geo = await loadGeo();
  } catch (e) {
    geo = null;
  }
  root.innerHTML = "";

  const items = (idx.items || idx.regions || []);
  const rows = items.map((it) => ({ ...it, shard: null }));
  const bySig = new Map(rows.map((r) => [String(r.sig_cd), r]));

  const controls = el("div", { class: "map-controls" });
  const metricSel = el("select", { class: "sel", id: "metric-sel" },
    ...Object.entries(METRICS).map(([k, v]) => el("option", { value: k, text: v.label })));
  const catSel = el("select", { class: "sel", id: "cat-sel", disabled: "disabled" });
  controls.appendChild(el("label", { text: "지표 " }));
  controls.appendChild(metricSel);
  controls.appendChild(el("label", { text: " 분야 " }));
  controls.appendChild(catSel);

  const legend = el("div", { class: "legend" });
  const statusLine = el("div", { class: "as-of" });
  const mapDiv = el("div", { id: "map", class: "map-canvas" });
  const detail = el("div", { class: "map-detail" }, note("지도에서 지역을 클릭하면 요약이 표시됩니다."));

  const panel = section("시군구 코로플레스",
    asOfLine(),
    controls, statusLine, legend,
    el("div", { class: "map-layout" }, mapDiv, detail)
  );
  root.appendChild(panel);

  if (!geo) {
    mapDiv.remove();
    panel.appendChild(note(
      "경계 GeoJSON(./geo/municipalities.geojson)이 없습니다. "
      + "python viz/tools/build_geo.py 로 생성하세요. 아래 표로 대체합니다.", "warn"));
  }

  ctx = { rows, bySig, geo, map: null, layer: null, mapDiv, legend, statusLine, detail, panel,
          metricSel, catSel, tableHolder: el("div") };
  panel.appendChild(el("details", { class: "table-fallback" },
    el("summary", { text: "표로 보기 / 지역 목록" }), ctx.tableHolder));

  metricSel.addEventListener("change", onMetricChange);
  catSel.addEventListener("change", () => paint());

  if (geo) {
    try {
      await ensureLeaflet();
      initLeaflet();
    } catch (e) {
      mapDiv.remove();
      panel.insertBefore(cdnFailPanel("Leaflet(지도)", e), ctx.tableHolder.parentElement);
    }
  }
  await paint();
}

function initLeaflet() {
  const L = window.L;
  // SVG 렌더러를 쓴다(canvas 아님). 250개 피처 규모에서는 성능 차이가 없고,
  // canvas 렌더러는 requestAnimationFrame 에 의존해 백그라운드 탭에서 아무것도 그리지 않는다.
  // SVG 는 path 요소가 DOM 에 그대로 남아 자동 점검·스타일링·접근성 모두 유리하다.
  const map = L.map(ctx.mapDiv, { preferCanvas: false, renderer: L.svg(), attributionControl: false });
  ctx.map = map;
  const layer = L.geoJSON(ctx.geo, {
    style: () => ({ color: "#7d8794", weight: 0.6, fillColor: "#f0f2f5", fillOpacity: 0.9 }),
    onEachFeature: (feature, lyr) => {
      const sig = String(feature.properties.sig_cd);
      // 값이 상위 단위에서 온 폴리곤은 그 상위 단위의 상세를 연다(paint 에서 지정)
      lyr.on("click", () => showDetail(lyr._sigForDetail || sig));
      lyr.on("mouseover", () => lyr.setStyle({ weight: 2, color: "#111" }));
      lyr.on("mouseout", () => lyr.setStyle({ weight: 0.6, color: "#7d8794" }));
    },
  }).addTo(map);
  ctx.layer = layer;
  map.fitBounds(layer.getBounds());
  L.control.attribution({ prefix: false })
    .addAttribution("경계: southkorea-maps 2018 시군구(단순화본) · 코드 변환: kostat_to_bjd.json")
    .addTo(map);
}

async function onMetricChange() {
  const key = ctx.metricSel.value;
  const m = METRICS[key];
  const needsCat = key === "category" || key === "category_share";
  ctx.catSel.disabled = !needsCat;
  if (m.needsShard) await ensureShards();
  if (needsCat && !ctx.catSel.options.length) fillCategorySelect();
  await paint();
}

async function ensureShards() {
  if (ctx.rows.every((r) => r.shard !== null || r.shardFailed)) return;
  const pb = progressBar();
  ctx.statusLine.innerHTML = "";
  ctx.statusLine.appendChild(el("span", { text: "지역 shard 로드 중 " }));
  ctx.statusLine.appendChild(pb);
  await mapLimit(ctx.rows, LIMITS.fetchConcurrency, async (r) => {
    if (r.shard) return;
    try { r.shard = await loadRegion(r.sig_cd); }
    catch (e) { r.shardFailed = true; }
  }, (d, t) => pb.update(d, t));
  const failed = ctx.rows.filter((r) => r.shardFailed).length;
  ctx.statusLine.textContent = `지역 shard ${ctx.rows.length - failed}/${ctx.rows.length} 로드 완료`
    + (failed ? ` (실패 ${failed}건)` : "");
}

function fillCategorySelect() {
  const codes = new Set();
  for (const r of ctx.rows) for (const tc of r.shard?.top_categories || []) codes.add(tc.code);
  const sorted = [...codes].sort();
  ctx.catSel.innerHTML = "";
  for (const c of sorted) ctx.catSel.appendChild(el("option", { value: c, text: `${categoryName(c)} (${c})` }));
}

/**
 * 폴리곤 sig_cd 에 대응하는 값을 찾는다.
 *
 * 경계 파일(southkorea-maps 2018)은 일반구를 둔 시를 시 단위 feature 로 갖고 있지 않다.
 * 수원시(41110)는 없고 장안구(41111)·권선구(41113)… 만 있다. 제주시(50110)도 행정시라
 * 자치법규 제정권이 없어 shard 의 ordinance_total 이 0 이다.
 * 그대로 두면 수원시 867건이 지도에서 사라지고 0인 일반구가 칠해진다.
 * 그래서 자치법규 제정권이 없는 단위(level 3)와 index 에 없는 폴리곤은 상위 단위 값으로 채우고,
 * 채운 사실을 툴팁·상태줄에 표기한다.
 */
function lookupValue(sig, values, ctxRows) {
  const own = ctxRows.get(sig);

  // 자기 값이 있고 제정권 있는 단위면 그대로 쓴다.
  if (own && own.level !== 3 && values.has(sig)) return { v: values.get(sig), via: null, src: own };

  // 소속 시로 올린다. 대상은 딱 둘뿐이다.
  //   (a) index 에 level 3 으로 등재된 단위(일반구·행정시) — ordinance_total 이 구조적으로 0이다
  //   (b) index 에 아예 없는 폴리곤 중 상위 시 코드(앞 4자리+0)가 실재하는 것 — 일반구 폴리곤
  // 시도(앞 2자리+000)로는 올리지 않는다. 그러면 shard 가 없는 자치구까지 광역 값으로 칠해져
  // "서울 모든 구 = 서울시 값" 같은 왜곡이 생긴다.
  const canRollUp = (own && own.level === 3) || !own;
  if (canRollUp) {
    const parentCity = sig.slice(0, 4) + "0";
    if (parentCity !== sig) {
      const p = ctxRows.get(parentCity);
      if (p && p.level === 2 && values.has(parentCity)) {
        return { v: values.get(parentCity), via: "소속 시", src: p };
      }
    }
    // 행정시(제주시·서귀포시)는 상위가 시도다. index 에 level 3 으로 등재된 경우에만 허용한다.
    if (own && own.level === 3) {
      const parentSido = sig.slice(0, 2) + "000";
      const p = ctxRows.get(parentSido);
      if (p && values.has(parentSido)) return { v: values.get(parentSido), via: "소속 광역", src: p };
    }
  }

  if (values.has(sig)) return { v: values.get(sig), via: null, src: own };
  return { v: null, via: null, src: own || null };
}

async function paint() {
  const key = ctx.metricSel.value;
  const m = METRICS[key];
  const opt = ctx.catSel.value;
  const values = new Map();
  for (const r of ctx.rows) {
    const v = m.get(r, opt);
    if (v !== null && v !== undefined) values.set(String(r.sig_cd), v);
  }
  const breaks = quantileBreaks([...values.values()], 5);

  // 범례
  ctx.legend.innerHTML = "";
  ctx.legend.appendChild(el("span", { class: "legend-title", text: m.label + " · 5분위" }));
  const edges = [null, ...breaks];
  CHOROPLETH_COLORS.forEach((c, i) => {
    const lo = edges[i];
    const label = lo === null ? `최저 ~ ${m.fmt(breaks[0])}` :
      (i === CHOROPLETH_COLORS.length - 1 ? `${m.fmt(lo)} 이상` : `${m.fmt(lo)} ~ ${m.fmt(breaks[i])}`);
    ctx.legend.appendChild(el("span", { class: "legend-item" },
      el("i", { class: "swatch", style: `background:${c}` }), el("span", { text: label })));
  });
  ctx.legend.appendChild(el("span", { class: "legend-item" },
    el("i", { class: "swatch", style: "background:#f0f2f5;border:1px dashed #999" }),
    el("span", { text: "데이터 없음" })));

  // 지도 색칠
  if (ctx.layer) {
    let matched = 0, rolled = 0;
    ctx.layer.eachLayer((lyr) => {
      const sig = String(lyr.feature.properties.sig_cd);
      const { v, via, src } = lookupValue(sig, values, ctx.bySig);
      if (v !== null) { matched++; if (via) rolled++; }
      const ci = classOf(v, breaks);
      lyr.setStyle({
        fillColor: ci < 0 ? "#f0f2f5" : CHOROPLETH_COLORS[ci],
        fillOpacity: ci < 0 ? 0.5 : 0.9,
        color: "#7d8794",
        weight: 0.6,
        dashArray: via ? "3,3" : null,
      });
      lyr.bindTooltip(
        `<b>${lyr.feature.properties.sido || ""} ${lyr.feature.properties.name}</b><br>` +
        `sig_cd ${sig}<br>${m.label}: ${v === null ? "데이터 없음" : m.fmt(v)}` +
        (via ? `<br><i>※ ${via} ${src ? src.name : ""}(${src ? src.sig_cd : ""}) 값으로 채움</i>` : ""),
        { sticky: true }
      );
      lyr._sigForDetail = via && src ? src.sig_cd : sig;
    });
    const total = ctx.geo.features.length;
    const bits = [`경계 ${total}개 중 ${matched}개에 값이 매칭됨`];
    if (rolled) bits.push(`그 중 ${rolled}개는 상위 단위 값으로 채움(점선 테두리) — 일반구·행정시는 자치법규 제정권이 없다`);
    if (matched < total) {
      bits.push(`나머지는 현재 데이터 소스에 shard 가 없어 회색 처리${state.isMock ? " (가상데이터는 표본 지역만 포함)" : ""}`);
    }
    ctx.statusLine.textContent = bits.join(" · ");
  }

  // 표
  const rowsOut = ctx.rows
    .map((r) => ({ r, v: values.has(String(r.sig_cd)) ? values.get(String(r.sig_cd)) : null }))
    .sort((a, b) => (b.v ?? -Infinity) - (a.v ?? -Infinity));
  ctx.tableHolder.innerHTML = "";
  ctx.tableHolder.appendChild(table(
    ["지역", "sig_cd", "level", m.label, ""],
    rowsOut.map(({ r, v }) => [
      r.name, r.sig_cd, r.level, v === null ? "—" : m.fmt(v),
      el("button", { class: "btn-link", text: "상세", onclick: () => go(`/region/${r.sig_cd}`) }),
    ])
  ));
}

async function showDetail(sig) {
  const r = ctx.bySig.get(sig);
  ctx.detail.innerHTML = "";
  const feat = ctx.geo.features.find((f) => String(f.properties.sig_cd) === sig);
  const title = feat ? `${feat.properties.sido || ""} ${feat.properties.name}` : sig;
  ctx.detail.appendChild(el("h3", { text: title }));
  ctx.detail.appendChild(el("div", { class: "as-of", text: `sig_cd ${sig}` }));
  if (!r) {
    ctx.detail.appendChild(note("현재 데이터 소스에 이 지역 shard 가 없습니다.", "warn"));
    return;
  }
  if (!r.shard) {
    ctx.detail.appendChild(loading());
    try { r.shard = await loadRegion(sig); } catch (e) { r.shardFailed = true; }
    ctx.detail.innerHTML = "";
    ctx.detail.appendChild(el("h3", { text: title }));
  }
  const s = r.shard;
  if (!s) { ctx.detail.appendChild(note("shard 로드 실패", "warn")); return; }
  ctx.detail.appendChild(el("div", { class: "chip-row" },
    badge(`level ${s.level}`, "badge-info"),
    badge(s.status === "active" ? "현행" : (s.status || "상태 미상"), s.status === "active" ? "badge-active" : "badge-unknown"),
    s.population ? badge(`인구 ${num(s.population)}`, "badge-info") : null
  ));
  ctx.detail.appendChild(table(["항목", "값"], [
    ["자치법규", num(s.ordinance_total)],
    ...Object.entries(s.ordinance_kinds || {}).map(([k, v]) => [`· ${k}`, num(v)]),
    ["예산 세부사업", num(s.budget?.lines)],
    ["예산현액", won(s.budget?.budget_now)],
    ["지출액", won(s.budget?.exe_amt)],
    ["집행률", s.budget?.budget_now ? pct(s.budget.exe_amt / s.budget.budget_now) : "—"],
  ]));
  ctx.detail.appendChild(el("button", { class: "btn", text: "지역 상세 화면으로", onclick: () => go(`/region/${sig}`) }));
}
