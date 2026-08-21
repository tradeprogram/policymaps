/**
 * 정적 번들 데이터 로더.
 *
 * 실데이터(system/data)와 가상데이터(viz/public/data)는 같은 스키마를 쓴다.
 * 차이는 두 가지뿐이고 여기서 흡수한다.
 *   1) 가상데이터에만 _mock / _mock_warning / _mock_fixtures 가 붙는다.
 *   2) api/*.json (유사지자체·격차·확산·실효성·표결·검색) 은 가상데이터에만 있다.
 *      실데이터에서는 MCP 서버가 계산해야 하는 값이라 정적 번들에 없다 -> notAvailable 로 떨어진다.
 */
import { DATA_BASE, DATA_SOURCES, GEO_URL, ADM_DONG_GEO_URL, LIMITS, CATEGORY_FALLBACK } from "./config.js";
import { mapLimit } from "./util.js";

/** ?src=real|mock 로 임시 전환 가능 */
function resolveBase() {
  const p = new URLSearchParams(location.search).get("src");
  if (p && DATA_SOURCES[p]) return DATA_SOURCES[p];
  if (p && (p.startsWith("./") || p.startsWith("../") || p.startsWith("/"))) return p;
  return DATA_BASE;
}

export const BASE = resolveBase().replace(/\/+$/, "");

const cache = new Map();

export class DataMissingError extends Error {
  constructor(path, status) {
    super(`데이터 없음: ${path} (HTTP ${status})`);
    this.name = "DataMissingError";
    this.path = path;
    this.status = status;
  }
}

/** 번들 내부 상대경로 로드 */
export async function getJSON(relPath) {
  return getJSONFromBase(BASE, relPath);
}

async function getJSONFromBase(base, relPath) {
  const url = `${base.replace(/\/+$/, "")}/${relPath.replace(/^\/+/, "")}`;
  if (cache.has(url)) return cache.get(url);
  const p = (async () => {
    let res;
    try {
      res = await fetch(url, { cache: "no-cache" });
    } catch (e) {
      throw new Error(`요청 실패: ${url} — ${e.message}. file:// 로 열면 fetch가 막힙니다. HTTP 서버로 여세요.`);
    }
    if (!res.ok) throw new DataMissingError(relPath, res.status);
    return res.json();
  })();
  cache.set(url, p);
  try { return await p; } catch (e) { cache.delete(url); throw e; }
}

/** 번들 밖 절대/상대 경로 (지도 GeoJSON 등) */
export async function getRaw(url) {
  if (cache.has(url)) return cache.get(url);
  const p = fetch(url, { cache: "no-cache" }).then((r) => {
    if (!r.ok) throw new DataMissingError(url, r.status);
    return r.json();
  });
  cache.set(url, p);
  try { return await p; } catch (e) { cache.delete(url); throw e; }
}

/* ---------------- 전역 상태 ---------------- */

export const state = {
  manifest: null,
  index: null,
  isMock: false,
  asOfDate: null,
  stale: false,
  staleDays: null,
  categoryNames: { ...CATEGORY_FALLBACK },
};

export async function loadManifest() {
  if (state.manifest) return state.manifest;
  const m = await getJSON("manifest.json");
  state.manifest = m;
  state.isMock = m._mock === true;
  state.asOfDate = m.as_of_date || null;
  state.stale = m.stale === true;
  state.staleDays = m.stale_days ?? null;
  return m;
}

export async function loadRegionIndex() {
  if (state.index) return state.index;
  const m = await loadManifest();
  const rel = m.region_index || "regions/index.json";
  const idx = await getJSON(rel);
  state.index = idx;
  return idx;
}

export async function loadRegion(sigCd) {
  return getJSON(`regions/${sigCd}.json`);
}

/** 여러 지역 shard 를 동시 개수 제한하며 로드. 실패한 것은 null. */
export async function loadRegions(sigCds, onProgress) {
  return mapLimit(sigCds, LIMITS.fetchConcurrency, async (cd) => {
    try { return await loadRegion(cd); } catch (e) { return null; }
  }, onProgress);
}

export async function loadChangesLatest() {
  const m = await loadManifest();
  return getJSON(m.changes_latest || "changes/latest.json");
}

export async function loadGraphStats() {
  try { return await getJSON("meta/graph-stats.json"); }
  catch (e) {
    const m = await loadManifest();
    return m.graph_stats ? { ...m.graph_stats, as_of_date: m.as_of_date } : null;
  }
}

/**
 * 그래프 로드. 실데이터 nodes.json 은 134MiB 이므로 기본적으로 막고,
 * force=true 일 때만 받는다.
 */
export async function loadGraph({ force = false } = {}) {
  const m = await loadManifest();
  const n = m.counts?.graph_nodes ?? 0;
  if (!force && n > LIMITS.graphNodeWarn) {
    return loadGraphSample({
      reason: "real graph is too large for automatic browser loading",
      realNodeCount: n,
      realEdgeCount: m.counts?.graph_edges ?? 0,
    });
  }
  const [nodesDoc, edgesDoc] = await Promise.all([
    getJSON("graph/nodes.json"),
    getJSON("graph/edges.json"),
  ]);
  const nodes = nodesDoc.nodes || nodesDoc;
  const edges = edgesDoc.edges || edgesDoc;
  for (const nd of nodes) {
    if (nd.label === "Category" && nd.src_id) state.categoryNames[nd.src_id] = nd.name;
  }
  return { nodes, edges };
}

async function loadGraphSample(meta = {}) {
  const [nodesDoc, edgesDoc] = await Promise.all([
    getJSONFromBase(DATA_SOURCES.mock, "graph/nodes.json"),
    getJSONFromBase(DATA_SOURCES.mock, "graph/edges.json"),
  ]);
  const nodes = nodesDoc.nodes || nodesDoc;
  const edges = edgesDoc.edges || edgesDoc;
  for (const nd of nodes) {
    if (nd.label === "Category" && nd.src_id) state.categoryNames[nd.src_id] = nd.name;
  }
  return {
    nodes,
    edges,
    sample: true,
    sampleBase: DATA_SOURCES.mock,
    sampleWarning: nodesDoc._mock_warning || edgesDoc._mock_warning || null,
    ...meta,
  };
}

export function categoryName(code) {
  return state.categoryNames[code] || code;
}

/* ---------------- 사전계산 fixture (api/*.json) ---------------- */

const FIXTURES = {
  peers: "api/peers.json",
  gap: "api/gap.json",
  diffusion: "api/diffusion.json",
  effectiveness: "api/effectiveness.json",
  votes: "api/votes.json",
  search: "api/search.json",
};

/** MCP tool 이름 — 실데이터에서 이 화면을 채우려면 이 tool 을 호출해야 한다 */
export const FIXTURE_TOOLS = {
  peers: "similar_regions",
  gap: "recommend_ordinances",
  diffusion: "diffusion_timeline",
  effectiveness: "ordinance_effectiveness",
  votes: "bill_vote_breakdown",
  search: "semantic_search_ordinance",
};

/**
 * fixture 로드. 없으면 DataMissingError 를 그대로 던진다(화면에서 안내로 처리).
 * 반환은 MCP 응답 봉투 {data, as_of_date, stale, execution_allowed, disclaimer} 그대로.
 */
export async function loadFixture(key) {
  const m = await loadManifest();
  const rel = (m._mock_fixtures && m._mock_fixtures[key]) || FIXTURES[key];
  if (!rel) throw new DataMissingError(key, 404);
  return getJSON(rel);
}

export async function loadGeo() {
  return getRaw(GEO_URL);
}

export async function loadAdmDongGeo() {
  return getRaw(ADM_DONG_GEO_URL);
}
