// 5. 법령 위계 그래프 — graph/nodes.json + graph/edges.json 의 경량 서브그래프
import { el, num, ymd } from "../util.js";
import { LIMITS, SATELLITE_TILE } from "../config.js";
import { loadAdmDongGeo, loadGraph, loadGraphStats, state } from "../api.js";
import { section, table, note, loading, asOfLine, errorPanel, cdnFailPanel, badge, statusBadge } from "../components.js";
import { ensureLeaflet, ensureVisNetwork } from "../vendor.js";

const REL_STYLE = {
  DELEGATED_FROM: { color: "#c0392b", label: "위임", dashes: false, width: 2 },
  CITES: { color: "#8e44ad", label: "인용", dashes: [4, 4], width: 1 },
  HAS_ORDINANCE: { color: "#7f8c8d", label: "보유", dashes: [2, 3], width: 1 },
  IN_CATEGORY: { color: "#16a085", label: "분야", dashes: [2, 3], width: 1 },
  SIMILAR_TO: { color: "#2980b9", label: "유사", dashes: [1, 4], width: 1 },
  AMENDED_BY: { color: "#d35400", label: "개정", dashes: false, width: 1 },
  ADJACENT_TO: { color: "#95a5a6", label: "인접", dashes: [2, 4], width: 1 },
  CONTAINS: { color: "#bdc3c7", label: "포함", dashes: [2, 4], width: 1 },
  SUCCEEDED_BY: { color: "#e67e22", label: "승계", dashes: false, width: 2 },
  PROPOSED_BY: { color: "#34495e", label: "발의", dashes: [3, 3], width: 1 },
  VOTED: { color: "#7f8c8d", label: "표결", dashes: [1, 5], width: 1 },
  MEMBER_OF: { color: "#95a5a6", label: "소속", dashes: [2, 4], width: 1 },
};

const NODE_STYLE = {
  Region: { color: "#f39c12", shape: "hexagon" },
  Ordinance: { color: "#3498db", shape: "box" },
  LegalInstrument: { color: "#e74c3c", shape: "ellipse" },
  Bill: { color: "#9b59b6", shape: "diamond" },
  Legislator: { color: "#1abc9c", shape: "dot" },
  Party: { color: "#16a085", shape: "square" },
  Category: { color: "#27ae60", shape: "triangle" },
  BudgetLine: { color: "#95a5a6", shape: "dot" },
};

export async function render(root) {
  root.appendChild(loading("그래프 통계 확인 중…"));
  const stats = await loadGraphStats().catch(() => null);
  root.innerHTML = "";

  const sec = section("법령 위계 그래프", asOfLine("조례 → 상위법 위임(DELEGATED_FROM) 중심 서브그래프"));
  root.appendChild(sec);

  if (stats) {
    const skipped = stats.skipped_edges || {};
    const skipTotal = Object.values(skipped).reduce((a, b) => a + (b || 0), 0);
    sec.appendChild(el("div", { class: "chip-row" },
      badge(`노드 ${num(stats.total_nodes ?? sum(stats.node_counts))}`, "badge-info"),
      badge(`엣지 ${num(stats.total_edges ?? sum(stats.edge_counts))}`, "badge-info"),
      badge(`backend ${stats.backend || "?"}`, "badge-plain")
    ));
    if (skipTotal > 0) {
      sec.appendChild(note(
        "정적 번들에서 제외된 엣지가 있다: "
        + Object.entries(skipped).filter(([, v]) => v).map(([k, v]) => `${k} ${num(v)}건`).join(", ")
        + ". 해당 관계는 이 화면에서 그릴 수 없다.", "warn"));
    }
  }

  let graph;
  try {
    graph = await loadGraph();
  } catch (e) {
    if (e.name === "GraphTooLargeError") {
      const guardBox = el("div", { class: "panel panel-warn" });
      guardBox.appendChild(el("h3", { text: "그래프가 너무 큽니다" }));
      guardBox.appendChild(el("p", {
        text: `노드 ${num(e.nodeCount)}개 / 엣지 ${num(e.edgeCount)}개. `
          + "graph/nodes.json 은 실데이터에서 약 134MiB 라 브라우저에서 통째로 받으면 탭이 멈춥니다. "
          + "확인 후 진행하세요.",
      }));
      guardBox.appendChild(el("button", {
        class: "btn btn-danger",
        text: "그래도 전체 로드",
        onclick: async (ev) => {
          ev.target.disabled = true;
          const spin = loading("전체 그래프 로드 중… 수십 초 걸릴 수 있습니다");
          guardBox.appendChild(spin);
          try {
            const g = await loadGraph({ force: true });
            guardBox.remove();
            await drawUI(sec, g);
          } catch (err) {
            spin.remove();
            guardBox.appendChild(errorPanel(err, "전체 그래프 로드 실패"));
          }
        },
      }));
      sec.appendChild(guardBox);
      return;
    }
    sec.appendChild(errorPanel(e, "graph/nodes.json 또는 graph/edges.json 로드 실패"));
    return;
  }
  if (graph.sample) {
    sec.appendChild(note(
      `발표 데모: 실데이터 전체 그래프(${num(graph.realNodeCount)}노드 / ${num(graph.realEdgeCount)}엣지)는 용량상 자동 로드하지 않고, 이 화면만 샘플 그래프를 사용합니다. 구조와 인터랙션은 동일합니다.`,
      "warn"));
  }
  const admGeo = await loadAdmDongGeo().catch(() => null);
  await drawUI(sec, graph, admGeo);
}

function sum(obj) { return Object.values(obj || {}).reduce((a, b) => a + (b || 0), 0); }

async function drawUI(sec, graph, admGeo) {
  const { nodes, edges } = graph;
  const byId = new Map(nodes.map((n) => [n.id, n]));

  // 위임 관계가 있는 조례만 시작점 후보로
  const delegations = edges.filter((e) => e.relation === "DELEGATED_FROM");
  const seedIds = [...new Set(delegations.map((e) => e.source))].filter((id) => byId.has(id));

  const toolbar = el("div", { class: "toolbar" });
  const seedSel = el("select", { class: "sel sel-wide" },
    ...seedIds.slice(0, 500).map((id) => el("option", { value: id, text: byId.get(id).name || id })));
  const hopSel = el("select", { class: "sel" },
    el("option", { value: "1", text: "1홉" }),
    el("option", { value: "2", text: "2홉", selected: "selected" }),
    el("option", { value: "3", text: "3홉" }));
  toolbar.appendChild(el("label", { text: "기준 조례 " }));
  toolbar.appendChild(seedSel);
  toolbar.appendChild(el("label", { text: " 확장 " }));
  toolbar.appendChild(hopSel);

  const relBox = el("div", { class: "chip-row rel-filter" });
  const relCounts = {};
  for (const e of edges) relCounts[e.relation] = (relCounts[e.relation] || 0) + 1;
  const enabled = new Set(["DELEGATED_FROM", "CITES", "AMENDED_BY", "IN_CATEGORY", "HAS_ORDINANCE"]);
  for (const rel of Object.keys(relCounts).sort()) {
    const cb = el("input", { type: "checkbox", id: `rel-${rel}`, checked: enabled.has(rel) ? "checked" : null });
    cb.addEventListener("change", () => { cb.checked ? enabled.add(rel) : enabled.delete(rel); draw(); });
    relBox.appendChild(el("label", { class: "relchip" },
      cb,
      el("i", { class: "swatch", style: `background:${(REL_STYLE[rel] || {}).color || "#999"}` }),
      el("span", { text: `${rel} (${num(relCounts[rel])})` })));
  }

  sec.appendChild(toolbar);
  sec.appendChild(relBox);

  const mapCanvas = el("div", { class: "graph-map-canvas" });
  const mapInfo = el("div", { class: "graph-map-info" },
    note("위성영상 위 행정동 경계를 현재 선택한 그래프 지역과 매칭합니다."));
  sec.appendChild(el("div", { class: "graph-spatial-layout" }, mapCanvas, mapInfo));

  const canvas = el("div", { class: "graph-canvas", id: "graph-canvas" });
  const abstractGraph = el("details", { class: "graph-abstract" },
    el("summary", { text: "추상 네트워크 보기" }),
    canvas);
  sec.appendChild(abstractGraph);
  const detail = el("div", { class: "graph-detail" });
  sec.appendChild(detail);
  const fallback = el("div");
  sec.appendChild(fallback);

  let visOK = true;
  try { await ensureVisNetwork(); }
  catch (e) { visOK = false; canvas.remove(); sec.insertBefore(cdnFailPanel("vis-network(그래프)", e), detail); }

  let spatial = null;
  if (admGeo) {
    try {
      await ensureLeaflet();
      spatial = initSpatialMap(mapCanvas, mapInfo, admGeo, byId);
    } catch (e) {
      mapCanvas.remove();
      mapInfo.innerHTML = "";
      mapInfo.appendChild(cdnFailPanel("Leaflet(위성지도)", e));
    }
  } else {
    mapCanvas.remove();
    mapInfo.innerHTML = "";
    mapInfo.appendChild(note("행정동 경계 파일(geo/adm_dong.geojson)을 찾지 못했습니다. viz/tools/build_adm_dong_geo.py를 실행하세요.", "warn"));
  }

  seedSel.addEventListener("change", draw);
  hopSel.addEventListener("change", draw);

  let network = null;

  function draw() {
    const seed = seedSel.value;
    const hops = parseInt(hopSel.value, 10);
    const sub = ego(seed, hops, edges, byId, enabled, LIMITS.graphRenderNodes);
    if (spatial) spatial.update(sub);

    detail.innerHTML = "";
    detail.appendChild(el("div", { class: "as-of", text:
      `서브그래프 노드 ${sub.nodes.length} · 엣지 ${sub.edges.length}`
      + (sub.truncated ? ` (노드 상한 ${LIMITS.graphRenderNodes} 도달 — 잘림)` : "") }));

    if (visOK) {
      const vnodes = sub.nodes.map((n) => {
        const st = NODE_STYLE[n.label] || { color: "#95a5a6", shape: "dot" };
        return {
          id: n.id,
          label: truncate(n.name || n.id, 22),
          shape: st.shape,
          color: { background: n.status === "repealed" ? "#dcdcdc" : st.color, border: n.status === "repealed" ? "#c0392b" : "#555" },
          borderWidth: n.status === "repealed" ? 3 : 1,
          font: { size: 12, color: "#111" },
          title: nodeTitle(n),
        };
      });
      const vedges = sub.edges.map((e, i) => {
        const st = REL_STYLE[e.relation] || { color: "#999", width: 1 };
        return {
          id: `e${i}`, from: e.source, to: e.target,
          color: { color: st.color }, width: st.width, dashes: st.dashes,
          arrows: "to", label: st.label || e.relation, font: { size: 10, align: "middle", color: "#555" },
          title: e.relation,
        };
      });
      if (network) network.destroy();
      network = new window.vis.Network(canvas, { nodes: vnodes, edges: vedges }, {
        physics: { stabilization: { iterations: 200 }, barnesHut: { gravitationalConstant: -8000, springLength: 140 } },
        interaction: { hover: true, tooltipDelay: 120 },
        layout: { improvedLayout: sub.nodes.length < 150 },
      });
      network.on("click", (p) => {
        if (p.nodes.length) showNode(byId.get(p.nodes[0]), detail);
      });
      abstractGraph.addEventListener("toggle", () => {
        if (abstractGraph.open && network) setTimeout(() => network.fit(), 0);
      }, { once: true });
    }

    // 항상 표 형태도 제공 (CDN 실패 시 유일한 화면)
    fallback.innerHTML = "";
    fallback.appendChild(el("details", { open: visOK ? null : "open" },
      el("summary", { text: "관계 목록으로 보기" }),
      table(["source", "relation", "target"],
        sub.edges.slice(0, 400).map((e) => [
          truncate(byId.get(e.source)?.name || e.source, 40),
          e.relation,
          truncate(byId.get(e.target)?.name || e.target, 40),
        ]))
    ));
  }

  if (!seedIds.length) {
    sec.appendChild(note("DELEGATED_FROM 엣지가 없어 위임 그래프를 그릴 수 없습니다.", "warn"));
    return;
  }
  draw();
}

function truncate(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function nodeTitle(n) {
  const bits = [`[${n.label}] ${n.name}`, `id: ${n.id}`];
  if (n.org_name) bits.push(`기관: ${n.org_name}`);
  if (n.ord_kind) bits.push(`종류: ${n.ord_kind}`);
  if (n.instrument_kind) bits.push(`법령종류: ${n.instrument_kind}`);
  if (n.competent_authority) bits.push(`소관: ${n.competent_authority}`);
  if (n.enacted_on) bits.push(`제정: ${ymd(n.enacted_on)}`);
  if (n.effective_on) bits.push(`시행: ${ymd(n.effective_on)}`);
  if (n.status) bits.push(`상태: ${n.status === "repealed" ? "폐지" : n.status}`);
  return bits.join("\n");
}

/** seed 에서 hops 만큼 확장한 무방향 ego 그래프 */
function ego(seed, hops, edges, byId, enabledRels, maxNodes) {
  const adj = new Map();
  for (const e of edges) {
    if (!enabledRels.has(e.relation)) continue;
    if (!adj.has(e.source)) adj.set(e.source, []);
    if (!adj.has(e.target)) adj.set(e.target, []);
    adj.get(e.source).push(e);
    adj.get(e.target).push(e);
  }
  const seen = new Set([seed]);
  let frontier = [seed];
  let truncated = false;
  for (let h = 0; h < hops && !truncated; h++) {
    const next = [];
    for (const id of frontier) {
      for (const e of adj.get(id) || []) {
        for (const other of [e.source, e.target]) {
          if (seen.has(other)) continue;
          if (seen.size >= maxNodes) { truncated = true; break; }
          seen.add(other);
          next.push(other);
        }
        if (truncated) break;
      }
      if (truncated) break;
    }
    frontier = next;
  }
  const nodes = [...seen].map((id) => byId.get(id)).filter(Boolean);
  const nodeSet = new Set(nodes.map((n) => n.id));
  const subEdges = edges.filter((e) => enabledRels.has(e.relation) && nodeSet.has(e.source) && nodeSet.has(e.target));
  return { nodes, edges: subEdges, truncated };
}

function initSpatialMap(mapCanvas, mapInfo, admGeo, byId) {
  const L = window.L;
  const map = L.map(mapCanvas, { preferCanvas: true, attributionControl: false });
  L.tileLayer(SATELLITE_TILE.url, {
    maxZoom: 19,
    attribution: SATELLITE_TILE.attribution,
  }).addTo(map);
  L.control.attribution({ prefix: false })
    .addAttribution(SATELLITE_TILE.attribution)
    .addAttribution("행정동 경계: BND_ADM_DONG_PG 2025-06-30")
    .addTo(map);

  const markerLayer = L.layerGroup().addTo(map);
  const graphEdgeLayer = L.layerGroup().addTo(map);
  const graphNodeLayer = L.layerGroup().addTo(map);
  const admLayer = L.geoJSON(admGeo, {
    style: baseAdmStyle,
    onEachFeature: (feature, lyr) => {
      const p = feature.properties || {};
      lyr.bindTooltip(() => {
        const matched = lyr._graphMatched;
        return `<b>${p.adm_nm || p.adm_cd}</b><br>` +
          `행정동 ${p.adm_cd || ""}<br>` +
          `시군구 ${p.sig_name || p.sig_cd || "미매칭"}` +
          (matched ? `<br><b>그래프 매칭: ${matched}</b>` : "");
      }, { sticky: true });
    },
  }).addTo(map);

  const allBounds = admLayer.getBounds();
  if (allBounds.isValid()) map.fitBounds(allBounds.pad(0.02));
  mapInfo.appendChild(el("div", { class: "as-of", text:
    `행정동 ${num(admGeo._meta?.features || admGeo.features?.length || 0)}개 · 미매칭 ${num(admGeo._meta?.unmatched || 0)}개` }));

  function update(sub) {
    const matched = collectMatchedRegions(sub.nodes);
    const boundsBySig = new Map();
    const dongsBySig = new Map();

    admLayer.eachLayer((lyr) => {
      const p = lyr.feature.properties || {};
      const sig = String(p.sig_cd || "");
      const hit = matched.get(sig);
      lyr._graphMatched = hit ? hit.label : "";
      lyr.setStyle(hit ? matchedAdmStyle(hit.weight) : baseAdmStyle());
      if (hit) {
        dongsBySig.set(sig, (dongsBySig.get(sig) || 0) + 1);
        const b = lyr.getBounds();
        if (b.isValid()) {
          if (!boundsBySig.has(sig)) boundsBySig.set(sig, b);
          else boundsBySig.get(sig).extend(b);
        }
      }
    });

    markerLayer.clearLayers();
    graphEdgeLayer.clearLayers();
    graphNodeLayer.clearLayers();
    const activeBounds = [];
    const centersBySig = new Map();
    for (const [sig, b] of boundsBySig) {
      activeBounds.push(b);
      centersBySig.set(sig, b.getCenter());
      const hit = matched.get(sig);
      const center = b.getCenter();
      L.circleMarker(center, {
        radius: 7,
        color: "#ffffff",
        weight: 2,
        fillColor: "#ffcf33",
        fillOpacity: 0.95,
      }).bindTooltip(`${hit.label}<br>${dongsBySig.get(sig)}개 행정동`, { permanent: false })
        .addTo(markerLayer);
    }
    drawGraphOnMap(sub, byId, centersBySig, graphEdgeLayer, graphNodeLayer);

    mapInfo.innerHTML = "";
    mapInfo.appendChild(el("h3", { text: "위성·행정동 매칭" }));
    if (!matched.size) {
      mapInfo.appendChild(note("현재 서브그래프에서 지역 코드를 찾지 못했습니다.", "warn"));
      return;
    }
    mapInfo.appendChild(el("div", { class: "as-of", text:
      `그래프 지역 ${num(matched.size)}개 · 경계 매칭 ${num(boundsBySig.size)}개 시군구` }));
    const topRows = [...matched.entries()].slice(0, 10).map(([sig, hit]) => [
      hit.label,
      sig,
      `${num(hit.weight)}개 노드`,
      `${num(dongsBySig.get(sig) || 0)}개 동`,
    ]);
    mapInfo.appendChild(table(["지역", "sig_cd", "그래프", "경계"], topRows));

    if (activeBounds.length) {
      const groupBounds = activeBounds.reduce((acc, b) => acc.extend(b), activeBounds[0]);
      if (groupBounds.isValid()) map.fitBounds(groupBounds.pad(0.18), { maxZoom: 13 });
    }
  }

  return { update };
}

function baseAdmStyle() {
  return {
    color: "#ffffff",
    weight: 0.45,
    opacity: 0.38,
    fillColor: "#1f2937",
    fillOpacity: 0.04,
  };
}

function matchedAdmStyle(weight) {
  return {
    color: "#ffcf33",
    weight: Math.min(3, 1 + Math.log2(1 + weight)),
    opacity: 1,
    fillColor: "#ffcf33",
    fillOpacity: 0.28,
  };
}

function drawGraphOnMap(sub, byId, centersBySig, edgeLayer, nodeLayer) {
  const L = window.L;
  const positions = projectGraphNodes(sub, centersBySig);

  for (const e of sub.edges) {
    const a = positions.get(e.source);
    const b = positions.get(e.target);
    if (!a || !b) continue;
    const st = REL_STYLE[e.relation] || { color: "#ffffff", width: 1 };
    L.polyline([a, b], {
      color: st.color || "#ffffff",
      weight: Math.max(1.5, st.width || 1),
      opacity: 0.72,
      dashArray: st.dashes ? "5,5" : null,
    }).bindTooltip(`${nodeLabel(byId.get(e.source))}<br>${e.relation}<br>${nodeLabel(byId.get(e.target))}`, {
      sticky: true,
    }).addTo(edgeLayer);
  }

  for (const n of sub.nodes) {
    const pos = positions.get(n.id);
    if (!pos) continue;
    const st = NODE_STYLE[n.label] || { color: "#95a5a6" };
    const radius = n.label === "Region" ? 7 : n.label === "LegalInstrument" ? 6 : 4.5;
    L.circleMarker(pos, {
      radius,
      color: "#ffffff",
      weight: n.status === "repealed" ? 3 : 1.5,
      fillColor: n.status === "repealed" ? "#d9d9d9" : st.color,
      fillOpacity: 0.95,
      opacity: 1,
    }).bindTooltip(nodeTitle(n), { sticky: true })
      .on("click", () => showNode(n, document.querySelector(".graph-detail")))
      .addTo(nodeLayer);
  }
}

function projectGraphNodes(sub, centersBySig) {
  const positions = new Map();
  for (const n of sub.nodes) {
    const sig = graphNodeSig(n);
    if (sig && centersBySig.has(sig)) {
      positions.set(n.id, jitterLatLng(centersBySig.get(sig), n.id, n.label === "Region" ? 0 : 0.032));
    }
  }

  for (let pass = 0; pass < 4; pass++) {
    let changed = false;
    for (const n of sub.nodes) {
      if (positions.has(n.id)) continue;
      const linked = [];
      for (const e of sub.edges) {
        const other = e.source === n.id ? e.target : e.target === n.id ? e.source : null;
        if (other && positions.has(other)) linked.push(positions.get(other));
      }
      if (!linked.length) continue;
      positions.set(n.id, jitterLatLng(avgLatLng(linked), n.id, 0.055 + pass * 0.015));
      changed = true;
    }
    if (!changed) break;
  }
  return positions;
}

function avgLatLng(points) {
  const n = points.length || 1;
  const lat = points.reduce((a, p) => a + p.lat, 0) / n;
  const lng = points.reduce((a, p) => a + p.lng, 0) / n;
  return { lat, lng };
}

function jitterLatLng(center, key, radiusDeg) {
  if (!radiusDeg) return center;
  const h = hashString(key);
  const angle = (h % 6283) / 1000;
  const scale = 0.35 + ((h >>> 8) % 100) / 100;
  return {
    lat: center.lat + Math.sin(angle) * radiusDeg * scale,
    lng: center.lng + Math.cos(angle) * radiusDeg * scale,
  };
}

function hashString(s) {
  let h = 2166136261;
  for (const ch of String(s || "")) {
    h ^= ch.charCodeAt(0);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function nodeLabel(n) {
  return n?.name || n?.id || "";
}

function collectMatchedRegions(nodes) {
  const out = new Map();
  for (const n of nodes) {
    const sig = graphNodeSig(n);
    if (!sig || sig.endsWith("000")) continue;
    const prev = out.get(sig);
    const name = regionLabel(sig, nodes);
    if (!prev) out.set(sig, { label: name || sig, weight: 1 });
    else prev.weight += 1;
  }
  return out;
}

function graphNodeSig(n) {
  if (!n) return "";
  if (n.label === "Region") return String(n.sig_cd || n.src_id || "");
  if (n.region_id) return String(n.region_id);
  if (n.sig_cd) return String(n.sig_cd);
  return "";
}

function regionLabel(sig, nodes) {
  const region = nodes.find((n) => n.label === "Region" && String(n.sig_cd || n.src_id || "") === sig);
  if (region) return region.name || region.full_name || sig;
  const owned = nodes.find((n) => String(n.region_id || "") === sig);
  if (owned?.org_name) return owned.org_name;
  return sig;
}

function showNode(n, detail) {
  if (!n) return;
  const box = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("h3", { class: "card-title", text: n.name || n.id }),
      el("div", { class: "chip-row" },
        badge(n.label, "badge-info"),
        n.status ? statusBadge(n.status) : null,
        n.verification_status ? badge(n.verification_status, "badge-plain") : null)),
    table(["필드", "값"], Object.entries(n)
      .filter(([k]) => !["id", "name", "label", "kind", "src_id"].includes(k))
      .map(([k, v]) => [k, v === null ? "—" : String(v)]))
  );
  const old = detail.querySelector(".card");
  if (old) old.remove();
  detail.appendChild(box);
}
