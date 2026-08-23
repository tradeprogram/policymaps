// 11. 공간 분석 — api/spatial/{slug}.json (analytics.spatial.moran, make_analytics_fixtures.py 산출)
//    ★ 국지 LISA 는 다중비교 보정(BH-FDR)을 통과한 곳만 색칠한다. 미보정 시 n×0.05 곳이 위양성이다.
//    ★ 공간자기상관은 인과가 아니다. "이웃이 따라 한다"는 결론을 여기서 내릴 수 없다.
import { el, esc, num, quantileBreaks, classOf, CHOROPLETH_COLORS } from "../util.js";
import { getJSON, DataMissingError, loadGeo, loadRegionIndex, state } from "../api.js";
import { section, table, note, loading, asOfLine, errorPanel, badge, statCard,
         envelopeFooter, cdnFailPanel } from "../components.js";
import { ensureLeaflet } from "../vendor.js";
import { go } from "../router.js";

const CATALOG = "api/analytics.json";
const DIR = "api/spatial";
const GENERATOR = "system/make_analytics_fixtures.py";

/** analytics.json 이 없을 때 직접 찔러볼 slug (생성기 DEFAULT_METRICS 와 같은 목록) */
const FALLBACK_SLUGS = [
  "ordinance-count", "budget-per-capita", "welfare-ratio",
  "category-share-c03", "category-share-c04",
  "adoption-year-barefoot", "adoption-year-resid-barefoot",
];

/** LISA 사분면 색 — HH/LL 은 군집, HL/LH 은 공간 이상치 */
const QUAD = {
  HH: { color: "#d7301f", label: "HH 고-고 군집" },
  LL: { color: "#2b83ba", label: "LL 저-저 군집" },
  HL: { color: "#fdae61", label: "HL 공간 이상치(높은 값이 낮은 이웃 속에)" },
  LH: { color: "#abd9e9", label: "LH 공간 이상치(낮은 값이 높은 이웃 속에)" },
};
const NEUTRAL = "#eceff3";

async function maybe(rel) {
  try { return await getJSON(rel); }
  catch (e) { if (e instanceof DataMissingError) return null; throw e; }
}

function fx(v, d = 4) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toFixed(d);
}

/** Leaflet 은 컨테이너가 문서에 붙어 있어야 크기를 계산한다. 붙을 때까지 기다린다. */
function whenMounted(node, fn) {
  let tries = 0;
  const tick = () => {
    // 붙기만 한 게 아니라 실제 폭이 잡혀야 한다. 백그라운드 탭에서는 붙은 직후에도
    // 레이아웃 폭이 0 이라 그 시점에 초기화하면 캔버스가 300×150 기본값으로 굳는다(실측).
    if (node.isConnected && node.getBoundingClientRect().width > 0) { fn(); return; }
    if (++tries > 200) { if (node.isConnected) fn(); return; }
    // requestAnimationFrame 은 백그라운드 탭에서 호출되지 않으므로 타이머로 감시한다.
    setTimeout(tick, 25);
  };
  tick();
}

function missingPanel(err) {
  return el("div", { class: "panel panel-warn" },
    el("h2", { class: "panel-title", text: "공간통계 산출물이 현재 데이터 소스에 없습니다" }),
    el("p", { text: `이 화면은 사전계산 파일 ${CATALOG} · ${DIR}/{slug}.json 을 씁니다. `
      + "가상데이터 번들에는 들어 있지 않고, 실데이터에서도 생성기를 돌려야 채워집니다." }),
    el("p", { class: "hint" },
      el("b", { text: "생성 방법: " }),
      el("code", { text: `python ${GENERATOR} --only spatial --permutations 999` }),
      document.createTextNode(" → "),
      el("code", { text: `${DIR}/{slug}.json` })),
    err ? el("pre", { class: "err", text: `${err.name || "Error"}: ${err.message || err}` }) : null
  );
}

/* ------------------------------------------------------------------ *
 * 카탈로그
 * ------------------------------------------------------------------ */

/** analytics.json → [{slug, path, metric, description, moran_i, p_sim, n_significant_fdr}] */
async function loadCatalogEntries() {
  const cat = await maybe(CATALOG);
  const list = cat && cat.data && Array.isArray(cat.data.spatial) ? cat.data.spatial : [];
  const out = [];
  for (const e of list) {
    if (!e || !e.slug) continue;
    out.push({ ...e, rel: `api/${String(e.path || `spatial/${e.slug}.json`).replace(/^\.?\//, "")}` });
  }
  if (out.length) return { entries: out, source: CATALOG, catalog: cat };

  // 카탈로그가 없으면 알려진 slug 를 직접 찔러본다(에러로 죽지 않는다).
  const probed = [];
  for (const slug of FALLBACK_SLUGS) {
    const rel = `${DIR}/${slug}.json`;
    const env = await maybe(rel);
    if (env && env.data) {
      const d = env.data;
      probed.push({ slug, rel, metric: d.metric, description: d.metric_label,
                    moran_i: d.moran_i, p_sim: d.p_sim,
                    n_significant_fdr: (d.lisa_summary || {}).n_significant_fdr,
                    _env: env });
    }
  }
  return { entries: probed, source: probed.length ? "직접 탐색(analytics.json 없음)" : null, catalog: null };
}

/* ------------------------------------------------------------------ *
 * 진입점
 * ------------------------------------------------------------------ */

export async function render(root, params, query = {}) {
  root.appendChild(loading("공간통계 카탈로그를 불러오는 중…"));

  let cat;
  try { cat = await loadCatalogEntries(); }
  catch (e) { root.innerHTML = ""; root.appendChild(errorPanel(e, `${CATALOG} 로드 실패`)); return; }

  root.innerHTML = "";
  if (!cat.entries.length) { root.appendChild(missingPanel()); return; }

  // 경계 GeoJSON·지역 색인은 선택 사항이다. 없으면 표로 떨어진다.
  let geo = null;
  try { geo = await loadGeo(); } catch (e) { geo = null; }
  let regionIndex = null;
  try { regionIndex = await loadRegionIndex(); } catch (e) { regionIndex = null; }

  root.appendChild(headerPanel(cat));

  // 기본 지표는 대표 지표(조례 수)부터. 카탈로그 순서상 첫 항목이 국지 군집 0곳인 지표일 수 있다.
  const PREFERRED = ["ordinance-count", "budget-per-capita", "welfare-ratio"];
  const initial = (cat.entries.some((e) => e.slug === query.metric) && query.metric)
    || PREFERRED.find((s) => cat.entries.some((e) => e.slug === s))
    || cat.entries[0].slug;
  const sel = el("select", { class: "sel sel-wide", "aria-label": "지표 선택" });
  for (const e of cat.entries) {
    const flag = e.p_sim != null && e.p_sim <= 0.05 ? "✓ " : "· ";
    const o = el("option", { value: e.slug,
      text: `${flag}${e.description || e.metric || e.slug} — Moran I ${fx(e.moran_i, 3)} (p=${fx(e.p_sim, 3)})` });
    if (e.slug === initial) o.selected = true;
    sel.appendChild(o);
  }
  const body = el("div", {});
  root.appendChild(el("div", { class: "toolbar" },
    el("label", { text: "지표 " }), sel,
    el("span", { class: "muted small", text: `${cat.entries.length}종 · ✓ = 전역 p≤0.05` })));
  root.appendChild(body);

  let token = 0;
  async function draw(slug) {
    const my = ++token;
    const entry = cat.entries.find((e) => e.slug === slug);
    body.innerHTML = "";
    body.appendChild(loading(`${entry ? (entry.description || entry.metric) : slug} 를 불러오는 중…`));
    let env = entry && entry._env ? entry._env : null;
    if (!env) {
      try { env = await maybe(entry ? entry.rel : `${DIR}/${slug}.json`); }
      catch (e) {
        if (my !== token) return;
        body.innerHTML = ""; body.appendChild(errorPanel(e, `${entry ? entry.rel : slug} 로드 실패`)); return;
      }
    }
    if (my !== token) return;
    body.innerHTML = "";
    if (!env || !env.data) {
      body.appendChild(missingPanel(new DataMissingError(entry ? entry.rel : slug, 404)));
      return;
    }
    renderMetric(body, env, entry, geo, regionIndex);
  }

  sel.addEventListener("change", () => draw(sel.value));
  await draw(initial);
}

function headerPanel(cat) {
  const withCluster = cat.entries.filter((e) => (e.n_significant_fdr || 0) > 0).length;
  const panel = section("공간자기상관 (Moran's I · LISA)",
    asOfLine(`카탈로그 ${cat.source || "?"}`),
    el("div", { class: "banner banner-est", role: "note" },
      el("span", { class: "banner-tag", text: "해석 주의" }),
      el("span", { class: "banner-body", text:
        "공간자기상관은 '값이 공간적으로 뭉쳐 있다'는 사실만 말한다. 왜 뭉쳤는지(이웃 학습인지, 인구·재정 같은 공통 요인인지)는 "
        + "여기서 구분되지 않는다. 인과 주장으로 쓰지 말 것." })),
    el("div", { class: "stat-grid" },
      statCard("지표", num(cat.entries.length), "사전계산된 공간통계"),
      statCard("전역 유의(p≤0.05)", num(cat.entries.filter((e) => e.p_sim != null && e.p_sim <= 0.05).length), "순열검정 기준"),
      statCard("국지 군집 있는 지표", num(withCluster), "BH-FDR 통과 지역 1곳 이상"),
      statCard("엔진", "analytics.spatial.moran", "재구현 아님 — 엔진 원본 호출")
    ));
  return panel;
}

/* ------------------------------------------------------------------ *
 * 지표 1종 렌더
 * ------------------------------------------------------------------ */

function renderMetric(root, env, entry, geo, regionIndex) {
  const d = env.data || {};
  const lisa = Array.isArray(d.lisa) ? d.lisa : [];
  const sum = d.lisa_summary || {};
  const nSig = sum.n_significant_fdr ?? 0;

  // 1) 전역 Moran
  const gsec = section(`${d.metric || entry?.slug} — 전역 Moran's I`,
    d.metric_label ? note(d.metric_label) : null,
    el("div", { class: "chip-row" },
      badge(`metric ${d.metric || "?"}`, "badge-info"),
      badge(`level ${d.level ?? "?"}`, "badge-plain"),
      badge(`가중치 ${d.weights === "row" ? "행표준화 인접" : (d.weights || "?")}`, "badge-plain"),
      d.metric_source === "generator_extension"
        ? badge("생성기 확장 지표(엔진 본체 아님)", "badge-warn")
        : badge(`지표 산출 ${d.metric_source || "engine"}`, "badge-plain")),
    el("div", { class: "stat-grid" },
      statCard("Moran's I", fx(d.moran_i, 4), `기댓값 E[I] = ${fx(d.expected_i, 4)}`),
      statCard("z (순열)", fx(d.z_sim, 4), `순열 ${num(d.permutations)}회`),
      statCard("p_sim", fx(d.p_sim, 3), d.p_sim != null && d.p_sim <= 0.05 ? "전역 유의" : "전역 유의하지 않음"),
      statCard("대상 지역", `${num(d.n)} / ${num(d.universe)}`, `제외 ${num(d.n_excluded)}곳`)),
    note(`판정: ${d.interpretation || "—"}`),
    d.reading_guide ? note(d.reading_guide, "small") : null
  );
  root.appendChild(gsec);

  // 2) 국지 LISA — FDR 통과 여부를 먼저 못박는다
  const lsec = section("국지 LISA (지역별 군집)",
    el("div", { class: "stat-grid" },
      statCard("BH-FDR 통과", num(nSig), `α=${sum.fdr_alpha ?? 0.05}`),
      statCard("미보정 p<0.05", num(sum.n_significant_raw_p05), "그대로 쓰면 위양성 포함"),
      statCard("보정 없을 때 기대 위양성", sum.expected_false_positives_without_fdr == null
        ? "—" : String(sum.expected_false_positives_without_fdr), "n × 0.05"),
      statCard("사분면 분포", Object.entries(sum.by_quadrant || {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "없음",
        "FDR 통과분만 계상")));
  if (!nSig) {
    lsec.appendChild(note(
      "이 지표는 다중비교 보정(BH-FDR) 후 국지 군집이 0곳이다. 지도에 사분면 색이 하나도 칠해지지 않는 것이 정상이며, "
      + "'군집이 안 보인다'가 아니라 '통계적으로 군집이라고 말할 수 없다'가 맞는 해석이다.", "warn"));
  }
  const clusters = sum.clusters || {};
  const clusterRows = Object.entries(clusters).filter(([, v]) => Array.isArray(v) && v.length);
  if (clusterRows.length) {
    lsec.appendChild(table(["사분면", "지역 수", "지역"],
      clusterRows.map(([q, v]) => [
        el("span", {}, el("i", { class: "swatch", style: `background:${(QUAD[q] || {}).color || NEUTRAL}` }),
          document.createTextNode(" " + ((QUAD[q] || {}).label || q))),
        num(v.length), v.join(", "),
      ])));
  }
  root.appendChild(lsec);

  // 3) 지도 + 표
  root.appendChild(mapSection(d, lisa, geo, regionIndex));

  // 4) 가중치·제외 지역 등 방법 공시
  root.appendChild(methodSection(d, env, entry));
}

/* ------------------------------------------------------------------ *
 * 지도
 * ------------------------------------------------------------------ */

function mapSection(d, lisa, geo, regionIndex) {
  const bySig = new Map();
  for (const r of lisa) {
    const k = String(r.sig_cd ?? r.region_id ?? "");
    if (k) bySig.set(k, r);
  }
  const levelOf = new Map();
  for (const it of (regionIndex && (regionIndex.items || regionIndex.regions)) || []) {
    if (it && it.sig_cd != null) levelOf.set(String(it.sig_cd), it.level);
  }

  const values = new Map();
  for (const [k, r] of bySig) if (typeof r.value === "number") values.set(k, r.value);
  const breaks = quantileBreaks([...values.values()], 5);

  const modeSel = el("select", { class: "sel", "aria-label": "지도 표시 방식" },
    el("option", { value: "lisa", text: "LISA 사분면 (FDR 통과분만 색칠)" }),
    el("option", { value: "value", text: "값 5분위 코로플레스" }));

  /* 시도 선택 — 전국을 한 번에 보면 수도권처럼 조밀한 곳의 군집이 안 보인다.
   * 값·분위는 **전국 기준 그대로** 두고 화면만 해당 시도로 좁힌다(부분집합에서
   * 다시 분위를 끊으면 같은 지역의 색이 시도를 고를 때마다 달라져 오해를 준다). */
  const sidoOf = (sig) => String(sig).slice(0, 2);
  const sidoNames = new Map();
  for (const r of lisa) {
    const cd = sidoOf(r.sig_cd);
    if (!sidoNames.has(cd)) {
      const nm = String(r.region_name || r.name || "").trim().split(/\s+/)[0];
      if (nm) sidoNames.set(cd, nm);
    }
  }
  const sidoSel = el("select", { class: "sel", "aria-label": "시도 선택" },
    el("option", { value: "", text: "전국 (전체)" }));
  for (const [cd, nm] of [...sidoNames].sort((a, b) => a[0].localeCompare(b[0]))) {
    sidoSel.appendChild(el("option", { value: cd, text: nm }));
  }
  const legend = el("div", { class: "legend" });
  const statusLine = el("div", { class: "as-of" });
  const mapDiv = el("div", { class: "map-canvas" });
  const detail = el("div", { class: "map-detail" }, note("지도에서 지역을 클릭하면 LISA 상세가 표시됩니다."));
  const tableHolder = el("div", {});

  const sec = section("코로플레스",
    el("div", { class: "map-controls" },
      el("label", { text: "표시 " }), modeSel,
      el("label", { text: " 지역 " }), sidoSel),
    legend, statusLine,
    el("div", { class: "map-layout" }, mapDiv, detail),
    el("details", { class: "table-fallback" },
      el("summary", { text: `표로 보기 — LISA 전체 ${lisa.length}행` }), tableHolder));

  // 표는 지도 유무와 무관하게 항상 그린다.
  renderLisaTable(tableHolder, lisa, d);

  let layer = null;
  let mapRef = null;   // 시도 선택 시 fitBounds 하려면 map 인스턴스가 필요하다
  function paint() {
    const mode = modeSel.value;
    renderLegend(legend, mode, breaks, d);
    if (!layer) return;
    let matched = 0, rolled = 0, painted = 0, inSido = 0;
    layer.eachLayer((lyr) => {
      const sig = String(lyr.feature.properties.sig_cd);
      const hit = lookup(sig, bySig, levelOf);
      const r = hit.row;
      if (r) { matched++; if (hit.via) rolled++; }
      let fill = NEUTRAL, opacity = 0.45;
      if (r) {
        if (mode === "lisa") {
          if (r.significant && QUAD[r.quadrant]) { fill = QUAD[r.quadrant].color; opacity = 0.85; painted++; }
          else { fill = "#f7f9fb"; opacity = 0.7; }
        } else {
          const ci = classOf(r.value, breaks);
          fill = ci < 0 ? NEUTRAL : CHOROPLETH_COLORS[ci];
          opacity = ci < 0 ? 0.45 : 0.9;
          if (ci >= 0) painted++;
        }
      }
      // 시도를 골랐으면 밖은 흐리게 눌러 초점을 만든다. 값·분위는 전국 기준 그대로다.
      const sel = sidoSel.value;
      const outside = sel && sidoOf(sig) !== sel;
      lyr.setStyle({
        fillColor: outside ? "#e9edf1" : fill,
        fillOpacity: outside ? 0.25 : opacity,
        color: outside ? "#c8cfd6" : "#7d8794",
        weight: outside ? 0.4 : 0.6,
        dashArray: hit.via ? "3,3" : null,
      });
      if (!outside && sel) inSido++;
      const p = lyr.feature.properties;
      lyr.bindTooltip(tooltipHtml(p, r, hit, d), { sticky: true });
      lyr._row = r || null;
      lyr._via = hit.via || null;
    });
    const total = (geo && geo.features ? geo.features.length : 0);
    const bits = [`경계 ${total}개 중 ${matched}개에 LISA 행이 매칭됨`];
    if (rolled) bits.push(`그 중 ${rolled}개는 소속 시 값으로 채움(점선) — 일반구는 조례 제정권이 없어 분석 단위가 아니다`);
    bits.push(mode === "lisa" ? `사분면 색칠 ${painted}개(FDR 통과분만)` : `5분위 색칠 ${painted}개`);
    if (sidoSel.value) {
      bits.push(`${sidoSel.options[sidoSel.selectedIndex].text} ${inSido}개만 강조 `
        + `— 색 기준(분위·사분면)은 전국 전체로 계산한 값 그대로다`);
    }
    statusLine.textContent = bits.join(" · ");
  }

  function showDetail(row, props, via) {
    detail.innerHTML = "";
    detail.appendChild(el("h3", { text: `${props.sido || ""} ${props.name || props.sig_cd}` }));
    if (!row) {
      detail.appendChild(note("이 폴리곤에 대응하는 LISA 행이 없습니다(분석 대상에서 제외되었거나 값이 결측).", "warn"));
      return;
    }
    detail.appendChild(el("div", { class: "chip-row" },
      badge(`sig_cd ${row.sig_cd}`, "badge-info"),
      row.significant
        ? badge(`${row.quadrant} · FDR 통과`, "badge-warn")
        : badge("FDR 미통과 — 군집이라 말할 수 없음", "badge-unknown"),
      via ? badge(`소속 시 값으로 표시`, "badge-plain") : null));
    detail.appendChild(table(["항목", "값"], [
      ["지역", row.name || "—"],
      [`값 (${d.metric || "metric"})`, typeof row.value === "number" ? String(row.value) : "—"],
      ["표준화 값 z", fx(row.z_score, 4)],
      ["이웃 평균 z (spatial lag)", fx(row.spatial_lag_z, 4)],
      ["local I", fx(row.local_i, 4)],
      ["사분면", row.quadrant || "—"],
      ["p_sim", fx(row.p_sim, 3)],
      ["q_value (BH)", fx(row.q_value, 5)],
      ["이웃 수", num(row.n_neighbors)],
    ]));
    detail.appendChild(el("button", { class: "btn", text: "지역 상세 화면으로",
      onclick: () => go(`/region/${row.sig_cd}`) }));
  }

  if (!geo) {
    mapDiv.remove();
    detail.remove();
    sec.insertBefore(note("경계 GeoJSON(./geo/municipalities.geojson)이 없어 지도를 생략하고 표로 대체합니다. "
      + "python viz/tools/build_geo.py 로 생성하세요.", "warn"), legend);
    renderLegend(legend, modeSel.value, breaks, d);
    statusLine.textContent = `LISA ${lisa.length}행 · 지도 없음`;
  } else {
    ensureLeaflet().then(() => new Promise((resolve) => whenMounted(mapDiv, resolve))).then(() => {
      const L = window.L;
      const map = L.map(mapDiv, { preferCanvas: false, renderer: L.svg(), attributionControl: false });
      mapRef = map;
      layer = L.geoJSON(geo, {
        style: () => ({ color: "#7d8794", weight: 0.6, fillColor: NEUTRAL, fillOpacity: 0.5 }),
        onEachFeature: (feature, lyr) => {
          lyr.on("click", () => showDetail(lyr._row, feature.properties, lyr._via));
          lyr.on("mouseover", () => lyr.setStyle({ weight: 2, color: "#111" }));
          lyr.on("mouseout", () => lyr.setStyle({ weight: 0.6, color: "#7d8794" }));
        },
      }).addTo(map);
      map.fitBounds(layer.getBounds());
      L.control.attribution({ prefix: false })
        .addAttribution("경계: southkorea-maps 2018 시군구(단순화본)").addTo(map);
      paint();
    }).catch((e) => {
      mapDiv.remove();
      detail.remove();
      sec.insertBefore(cdnFailPanel("Leaflet(지도)", e), legend);
      renderLegend(legend, modeSel.value, breaks, d);
    });
  }

  modeSel.addEventListener("change", paint);
  sidoSel.addEventListener("change", () => {
    paint();
    // 선택 시도가 화면에 꽉 차도록 이동. 전국을 다시 고르면 전체로 되돌린다.
    if (!layer || typeof L === "undefined") return;
    const sel = sidoSel.value;
    let target = null;
    layer.eachLayer((lyr) => {
      if (sel && sidoOf(lyr.feature.properties.sig_cd) !== sel) return;
      const b = lyr.getBounds();
      target = target ? target.extend(b) : L.latLngBounds(b.getSouthWest(), b.getNorthEast());
    });
    if (target && target.isValid() && mapRef) mapRef.fitBounds(target, { padding: [12, 12] });
  });
  renderLegend(legend, modeSel.value, breaks, d);
  return sec;
}

/**
 * 폴리곤 sig_cd → LISA 행.
 * 경계 파일에는 수원시(41110)가 없고 장안구(41111) 등 일반구만 있다. 분석 단위는 level 2 이므로
 * 일반구 폴리곤은 소속 시 값으로 칠하고 그 사실을 점선·툴팁에 표기한다(지도 화면과 같은 규칙).
 */
function lookup(sig, bySig, levelOf) {
  if (bySig.has(sig)) return { row: bySig.get(sig), via: null };
  const own = levelOf.get(sig);
  const canRollUp = own === 3 || own === undefined;
  if (canRollUp) {
    const parent = sig.slice(0, 4) + "0";
    if (parent !== sig && bySig.has(parent)) return { row: bySig.get(parent), via: "소속 시" };
  }
  return { row: null, via: null };
}

function tooltipHtml(props, row, hit, d) {
  const head = `<b>${esc(props.sido || "")} ${esc(props.name || "")}</b><br>sig_cd ${esc(props.sig_cd)}`;
  if (!row) return `${head}<br>분석 대상 아님(값 결측 또는 제외)`;
  const lines = [
    `${esc(d.metric_label || d.metric || "값")}: ${esc(row.value)}`,
    `z ${fx(row.z_score, 3)} · 이웃평균 z ${fx(row.spatial_lag_z, 3)}`,
    `사분면 ${esc(row.quadrant || "—")} · p_sim ${fx(row.p_sim, 3)} · q ${fx(row.q_value, 4)}`,
    row.significant ? "<b>BH-FDR 통과</b>" : "FDR 미통과 — 군집으로 해석 불가",
  ];
  if (hit.via) lines.push(`<i>※ ${esc(hit.via)} ${esc(row.name || "")} 값으로 채움</i>`);
  return `${head}<br>${lines.join("<br>")}`;
}

function renderLegend(legend, mode, breaks, d) {
  legend.innerHTML = "";
  if (mode === "lisa") {
    legend.appendChild(el("span", { class: "legend-title", text: "LISA 사분면 (BH-FDR 통과분만 색칠)" }));
    for (const [q, v] of Object.entries(QUAD)) {
      legend.appendChild(el("span", { class: "legend-item" },
        el("i", { class: "swatch", style: `background:${v.color}` }), el("span", { text: v.label })));
    }
    legend.appendChild(el("span", { class: "legend-item" },
      el("i", { class: "swatch", style: "background:#f7f9fb;border:1px solid #ccc" }),
      el("span", { text: "FDR 미통과(군집 아님)" })));
  } else {
    legend.appendChild(el("span", { class: "legend-title",
      text: `${d.metric_label || d.metric || "값"} · 5분위` }));
    const edges = [null, ...breaks];
    CHOROPLETH_COLORS.forEach((c, i) => {
      const lo = edges[i];
      const label = lo === null ? `최저 ~ ${fx(breaks[0], 2)}`
        : (i === CHOROPLETH_COLORS.length - 1 ? `${fx(lo, 2)} 이상` : `${fx(lo, 2)} ~ ${fx(breaks[i], 2)}`);
      legend.appendChild(el("span", { class: "legend-item" },
        el("i", { class: "swatch", style: `background:${c}` }), el("span", { text: label })));
    });
  }
  legend.appendChild(el("span", { class: "legend-item" },
    el("i", { class: "swatch", style: `background:${NEUTRAL};border:1px dashed #999` }),
    el("span", { text: "분석 대상 아님/데이터 없음" })));
}

function renderLisaTable(holder, lisa, d) {
  holder.innerHTML = "";
  if (!lisa.length) { holder.appendChild(note("LISA 행이 없습니다.", "warn")); return; }
  const rows = lisa.slice()
    .sort((a, b) => (b.significant === a.significant ? (b.local_i ?? 0) - (a.local_i ?? 0) : (b.significant ? 1 : -1)))
    .map((r) => [
      r.name || r.sig_cd,
      r.sig_cd,
      typeof r.value === "number" ? String(r.value) : "—",
      fx(r.z_score, 3),
      fx(r.spatial_lag_z, 3),
      fx(r.local_i, 4),
      r.quadrant || "—",
      fx(r.p_sim, 3),
      fx(r.q_value, 5),
      r.significant ? badge("FDR 통과", "badge-warn") : badge("미통과", "badge-unknown"),
      num(r.n_neighbors),
    ]);
  holder.appendChild(table(
    ["지역", "sig_cd", `값 (${d.metric || "metric"})`, "z", "이웃평균 z", "local I",
     "사분면", "p_sim", "q_value", "판정", "이웃 수"], rows));
}

/* ------------------------------------------------------------------ *
 * 방법 공시
 * ------------------------------------------------------------------ */

function methodSection(d, env, entry) {
  const wm = d.weights_meta || {};
  const sec = section("방법 · 공간가중치 공시",
    table(["항목", "값"], [
      ["엔진", d._engine || "—"],
      ["지표 산출", d.metric_source || "—"],
      ["분석 단위(level)", d.level ?? "—"],
      ["가중치", `${d.weights === "row" ? "행표준화(row-standardized)" : (d.weights || "—")} · S0 ${num(d.s0)}`],
      ["순열 횟수", num(d.permutations)],
      ["표본 평균 / 표준편차", `${fx(d.mean, 3)} / ${fx(d.sd, 3)}`],
      ["섬 처리", wm.island_policy ? `${wm.island_policy} (인접 없는 지역을 최근접 k개로 연결)` : "—"],
      ["일반구 승격", wm.lift_sub_districts ? `적용 — ${(wm.lifted || []).length}곳` : "미적용"],
      ["이웃 있는 지역", `${num(wm.n_with_neighbors)} / ${num(wm.universe)}`],
    ]));

  if (Array.isArray(d.excluded) && d.excluded.length) {
    sec.appendChild(el("details", {},
      el("summary", { text: `분석에서 제외된 지역 ${d.excluded.length}곳` }),
      table(["지역", "region_id", "사유"],
        d.excluded.map((x) => [x.name || "—", x.region_id || "—", x.reason || "—"]))));
  }
  if (Array.isArray(wm.islands_linked) && wm.islands_linked.length) {
    sec.appendChild(el("details", {},
      el("summary", { text: `인접이 없어 최근접 연결한 섬 ${wm.islands_linked.length}곳` }),
      table(["지역", "연결 대상", "최근접 거리(km)"],
        wm.islands_linked.map((x) => [x.name || x.region_id,
          Array.isArray(x.linked_to) ? x.linked_to.join(", ") : "—",
          x.nearest_km == null ? "—" : String(x.nearest_km)]))));
  }
  if (Array.isArray(wm.lifted) && wm.lifted.length) {
    sec.appendChild(el("details", {},
      el("summary", { text: `일반구 인접을 시 단위로 승격한 곳 ${wm.lifted.length}곳` }),
      table(["지역", "회복된 인접 엣지"], wm.lifted.map((x) => [x.name || x.region_id, num(x.edges_recovered)]))));
  }
  if (d.metric_source === "generator_extension") {
    sec.appendChild(note("이 지표(category_share)는 analytics.spatial.metric_values 본체에 없고 생성기가 런타임으로 감싼 것이다. "
      + "Moran·LISA·순열검정·FDR 계산 자체는 엔진 원본이다.", "warn"));
  }
  sec.appendChild(el("div", { class: "as-of",
    text: `데이터 소스: ${entry ? entry.rel : DIR} · 생성기 ${GENERATOR}` + (state.isMock ? " · 가상데이터 모드" : "") }));
  const foot = envelopeFooter(env);
  if (foot) sec.appendChild(foot);
  return sec;
}
