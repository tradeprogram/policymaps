/**
 * 정적 번들 데이터 로더.
 *
 * 실데이터(system/data)와 가상데이터(viz/public/data)는 같은 스키마를 쓴다.
 * 차이는 두 가지뿐이고 여기서 흡수한다.
 *   1) 가상데이터에만 _mock / _mock_warning / _mock_fixtures 가 붙는다.
 *   2) api/*.json (유사지자체·격차·확산·실효성·표결·검색) 은 가상데이터에만 있다.
 *      실데이터에서는 MCP 서버가 계산해야 하는 값이라 정적 번들에 없다 -> notAvailable 로 떨어진다.
 */
import { DATA_BASE, DATA_SOURCES, GEO_URL, ADM_DONG_GEO_URL, LIMITS, CATEGORY_FALLBACK,
         API_SHARDS, EXTRA_CATALOGS, PICKER_LEVELS, SIDO_FALLBACK, FULL_API } from "./config.js";
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

/**
 * 사전압축(.json.gz) 대상 판정 — system/tools_compress_api.py 의 규칙과 반드시 같아야 한다.
 *   "api/<디렉터리>/…/*.json" 형태의 shard 는 .json.gz 로 굽혀 있다(지역·버킷 shard).
 *   "api/*.json" 최상위 카탈로그는 비압축 그대로다(부팅 때 읽는다).
 * 전량 shard 405MB 를 76MB 로 줄이려고 넣었다. 한 건도 버리지 않기 위한 선택이다.
 */
const GZIP_SHARD_RE = /^api\/[^/]+\/.+\.json$/;

/**
 * 응답 바이트를 JSON 으로 만든다. 서버가 Content-Encoding: gzip 을 붙였으면 브라우저가
 * 이미 풀어 놨고, 안 붙였으면(python -m http.server · Vercel 정적) 여기서 직접 푼다.
 * 매직바이트 0x1f8b 로 두 경우를 구분하므로 서버 설정에 의존하지 않는다.
 */
async function parseMaybeGzip(res, relPath) {
  const buf = new Uint8Array(await res.arrayBuffer());
  if (buf.length > 1 && buf[0] === 0x1f && buf[1] === 0x8b) {
    if (typeof DecompressionStream === "undefined") {
      throw new Error(`이 브라우저는 DecompressionStream 을 지원하지 않아 사전압축 shard(${relPath}.gz)를 ` +
        `읽을 수 없습니다. Chrome 80+ / Firefox 113+ / Safari 16.4+ 를 쓰거나 ` +
        `python system/tools_compress_api.py --decompress 로 되돌리세요.`);
    }
    const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
    return JSON.parse(await new Response(stream).text());
  }
  return JSON.parse(new TextDecoder().decode(buf));
}

async function getJSONFromBase(base, relPath) {
  const url = `${base.replace(/\/+$/, "")}/${relPath.replace(/^\/+/, "")}`;
  if (cache.has(url)) return cache.get(url);
  const p = (async () => {
    // 가상데이터(viz/public/data)에는 api/ 하위 디렉터리가 없다 -> 헛된 .gz 요청을 만들지 않는다.
    const tryGzip = base !== DATA_SOURCES.mock && GZIP_SHARD_RE.test(relPath);
    if (tryGzip) {
      let gzRes = null;
      try {
        gzRes = await fetch(`${url}.gz`, { cache: "no-cache" });
      } catch { /* 네트워크 오류면 비압축본으로 한 번 더 시도한다 */ }
      if (gzRes && gzRes.ok) return parseMaybeGzip(gzRes, relPath);
      // 404 면 비압축본으로 폴백한다(압축 전 번들 · 부분 압축 상태 하위호환).
    }
    let res;
    try {
      res = await fetch(url, { cache: "no-cache" });
    } catch (e) {
      throw new Error(`요청 실패: ${url} — ${e.message}. file:// 로 열면 fetch가 막힙니다. HTTP 서버로 여세요.`);
    }
    if (!res.ok) throw new DataMissingError(relPath, res.status);
    return parseMaybeGzip(res, relPath);
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
    // 예전에는 여기서 가상데이터 표본(viz/public/data/graph)을 대신 그렸다. 실데이터의
    // graph_nodes 는 250,416 이라 이 분기가 **항상** 참이었고, 결과적으로 실데이터 모드에서도
    // 조용히 목업 500노드가 화면에 나왔다. 소스 교차 참조는 금지한다 — 못 그리면 못 그린다고
    // 알리고, 화면이 shard 안내를 띄우게 한다.
    return {
      nodes: [],
      edges: [],
      tooLarge: true,
      realNodeCount: n,
      realEdgeCount: m.counts?.graph_edges ?? 0,
    };
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

/* ==================================================================== *
 *  전국 shard 로더 — api/index.json + api/{kind}/{key}.json
 *
 *  설계 원칙
 *   1) shard 우선, 단일 파일 폴백. api/gap/47190.json 이 없으면 api/gap.json 의
 *      data["47190"] 로 떨어지고, 그것도 없으면 missing 을 돌려준다(에러로 죽지 않는다).
 *   2) 색인은 관대하게 읽는다. make_nationwide.py 산출 형태가 확정되기 전이므로
 *      배열/맵, 문자열/객체, regions/items 등을 모두 받는다.
 *   3) 색인이 아예 없어도 regions/index.json(전국 284곳)만으로 선택기를 채운다.
 * ==================================================================== */

/** 지역 shard 를 갖는 화면 */
export const REGIONAL_KINDS = ["gap", "peers", "effectiveness"];
/** 항목(정책 템플릿·의안·질의) 목록을 갖는 화면 */
export const CATALOG_KINDS = ["diffusion", "votes", "search"];

function asArray(v) { return Array.isArray(v) ? v : []; }

/** sig_cd 로 보이는 키만으로 이뤄진 맵인가 (fixture 의 data 가 {sig: 결과} 인지 판정) */
export function isSigMap(o) {
  if (!o || typeof o !== "object" || Array.isArray(o)) return false;
  const ks = Object.keys(o);
  return ks.length > 0 && ks.every((k) => /^\d{4,5}$/.test(k));
}

function shardDir(kind) {
  return API_SHARDS[kind] || ("api/" + kind);
}

/** api/index.json 이 있는 디렉터리. 색인이 주는 상대경로의 기준이다. */
const API_DIR = API_SHARDS.index.replace(/[^/]*$/, "");

/**
 * 색인이 준 경로를 실제로 받아볼 후보들로 바꾼다.
 * make_nationwide.py 는 "gap/47190.json" 처럼 api/ 기준 상대경로를 준다.
 * 번들 루트 기준("api/gap/47190.json")으로 주는 구현도 받아들인다.
 */
function pathCandidates(p) {
  if (typeof p !== "string" || !p) return [];
  if (/^https?:\/\//.test(p) || p.startsWith("/")) return [p];
  const clean = p.replace(/^\.\//, "");
  return clean.startsWith(API_DIR) ? [clean] : [API_DIR + clean, clean];
}

/** {path, bytes} 객체 / 문자열 어느 쪽이든 경로 문자열로 */
function pathOf(v) {
  if (typeof v === "string") return v;
  if (v && typeof v === "object" && typeof v.path === "string") return v.path;
  if (v && typeof v === "object" && typeof v.file === "string") return v.file;
  return null;
}

/** 색인의 layout({kind: "gap/{key}.json"}) 로 경로를 만든다 */
function layoutPath(api, kind, key) {
  const tpl = api && api.layout && api.layout[kind];
  if (typeof tpl !== "string") return null;
  return tpl.replace("{key}", String(key)).replace("{sig_cd}", String(key)).replace("{sig}", String(key));
}

/** 색인에서 kind 목록을 뽑는다. 배열/맵/문자열 어느 쪽이든 배열로 정규화. */
function pickList(idx, kind) {
  if (!idx || typeof idx !== "object") return [];
  const cands = [
    idx[kind],
    idx.coverage && idx.coverage[kind],
    idx.catalog && idx.catalog[kind],
    idx.shards && idx.shards[kind],
    idx.data && idx.data[kind],
  ];
  for (const c of cands) if (Array.isArray(c)) return c;
  for (const c of cands) {
    if (c && typeof c === "object") {
      return Object.entries(c).map(([k, v]) =>
        (v && typeof v === "object" ? { key: k, sig_cd: k, ...v } : { key: k, sig_cd: k }));
    }
  }
  return [];
}

function normRegionEntry(x) {
  if (typeof x === "string" || typeof x === "number") return { sig_cd: String(x) };
  if (!x || typeof x !== "object") return null;
  const sig = x.sig_cd ?? x.sig ?? x.sigCd ?? x.code ?? x.region_sig ?? x.key ?? x.id ?? x.region_id;
  if (sig === undefined || sig === null || sig === "") return null;
  return {
    sig_cd: String(sig),
    name: x.name || x.region_name || x.full_name || null,
    level: x.level ?? null,
    sido: x.sido || x.sido_name || x.ctp_kor_nm || null,
    file: pathOf(x.file) || pathOf(x.path) || null,
    files: x.files || null,
    has: x.has || x.kinds || x.available || null,
  };
}

let apiIndexPromise = null;

/** api/index.json (전국 커버리지 색인). 없으면 null — 에러가 아니다. */
export async function loadApiIndex() {
  if (!apiIndexPromise) {
    apiIndexPromise = (async () => {
      try { return await getJSON(API_SHARDS.index); }
      catch (e) {
        if (e instanceof DataMissingError) return null;
        throw e;
      }
    })();
  }
  return apiIndexPromise;
}

let regionCatalogPromise = null;

/**
 * 전국 지역 선택기용 목록.
 * items = [{sig_cd, name, level, sido, hasRegionShard}] — level 1·2 만, sig_cd 오름차순.
 * 실데이터에서는 243곳(광역 16 + 기초 227)이 잡힌다. level 3 일반구는 조례 제정권이 없어 뺀다.
 */
export async function loadRegionCatalog() {
  if (regionCatalogPromise) return regionCatalogPromise;
  regionCatalogPromise = (async () => {
    const byCd = new Map();
    const sidoName = {};
    const sources = [];

    const add = (e) => {
      if (!e) return;
      const cur = byCd.get(e.sig_cd) || { sig_cd: e.sig_cd };
      byCd.set(e.sig_cd, {
        sig_cd: e.sig_cd,
        name: cur.name || e.name || null,
        level: cur.level ?? e.level ?? null,
        sido: cur.sido || e.sido || null,
        hasRegionShard: cur.hasRegionShard || !!e.hasRegionShard,
      });
    };

    // 1) regions/index.json — 지역 상세 shard 목록(실데이터 284곳)
    try {
      const idx = await loadRegionIndex();
      const items = asArray(idx.items || idx.regions);
      for (const it of items) {
        const n = normRegionEntry(it);
        if (n) { n.hasRegionShard = true; add(n); }
      }
      for (const it of items) {
        if (it && it.level === 1 && it.sig_cd && it.name) {
          sidoName[String(it.sig_cd).slice(0, 2)] = it.name;
        }
      }
      if (items.length) sources.push(state.manifest?.region_index || "regions/index.json");
    } catch (e) { /* 지역 색인이 없어도 아래 소스로 계속 */ }

    // 2) api/index.json — make_nationwide.py 산출
    const api = await loadApiIndex();
    if (api) {
      let n = 0;
      for (const raw of pickList(api, "regions")) {
        const e = normRegionEntry(raw);
        if (!e) continue;
        add(e); n++;
        const p = e.sig_cd.slice(0, 2);
        if (e.sido && !sidoName[p]) sidoName[p] = e.sido;
      }
      for (const kind of REGIONAL_KINDS) {
        for (const raw of pickList(api, kind)) { const e = normRegionEntry(raw); if (e) { add(e); n++; } }
      }
      if (n) sources.push(API_SHARDS.index);
    }

    const sidoOf = (sig) => {
      const p = String(sig).slice(0, 2);
      return sidoName[p] || SIDO_FALLBACK[p] || "기타";
    };

    const all = [...byCd.values()]
      .map((e) => ({ ...e, sido: e.sido || sidoOf(e.sig_cd) }))
      .sort((a, b) => (a.sig_cd < b.sig_cd ? -1 : a.sig_cd > b.sig_cd ? 1 : 0));
    // items = 선택기에 올릴 level 1·2. all = 일반구(level 3) 포함 전체(사전계산된 일반구를 되살릴 때 쓴다).
    const items = all.filter((e) => e.level === null || e.level === undefined || PICKER_LEVELS.includes(e.level));

    return { items, all, sidoOf, sources, hasApiIndex: !!api };
  })();
  return regionCatalogPromise;
}

/** 색인이 알려주는 kind 별 커버 지역 Set. 색인이 없으면 빈 Set(=커버리지 미상). */
export async function shardCoverage(kind) {
  const api = await loadApiIndex();
  const set = new Set();
  if (!api) return set;
  for (const raw of pickList(api, kind)) { const e = normRegionEntry(raw); if (e) set.add(e.sig_cd); }
  for (const raw of pickList(api, "regions")) {
    const e = normRegionEntry(raw);
    if (!e) continue;
    const has = e.has;
    // files 가 있으면 그것이 가장 정확한 커버리지다(kind 별로 파일이 있는지)
    if (e.files && typeof e.files === "object") {
      if (pathOf(e.files[kind]) || e.files[kind] === true) set.add(e.sig_cd);
      continue;
    }
    const ok = has === null || has === undefined || has === true
      || (Array.isArray(has) && has.includes(kind))
      || (typeof has === "object" && !Array.isArray(has) && (has[kind] === true || has[kind] === 1));
    if (ok) set.add(e.sig_cd);
  }
  return set;
}

/** 색인이 지정한 shard 경로가 있으면 그것부터, 없으면 관례 경로. */
async function regionalShardPaths(kind, sig) {
  const out = [];
  const push = (p) => { for (const c of pathCandidates(p)) out.push(c); };
  const api = await loadApiIndex();
  if (api) {
    push(pathOf((api.files && api.files[kind] && api.files[kind][sig])
      || (api.paths && api.paths[kind] && api.paths[kind][sig])));
    for (const raw of [...pickList(api, kind), ...pickList(api, "regions")]) {
      const e = normRegionEntry(raw);
      if (!e || e.sig_cd !== String(sig)) continue;
      if (e.files) push(pathOf(e.files[kind]));
      if (e.file) push(e.file);
    }
    push(layoutPath(api, kind, sig));
  }
  out.push(shardDir(kind) + "/" + sig + ".json");
  return [...new Set(out.filter(Boolean))];
}

function unwrapSub(env, sig) {
  const dd = (env && typeof env === "object" && "data" in env) ? env.data : env;
  if (isSigMap(dd)) return dd[String(sig)] ?? null;
  return dd;
}

/**
 * 지역 단위 사전계산 결과 로드. 절대 throw 하지 않는다(화면이 안내로 처리한다).
 * 반환 source: "shard" | "fixture-map" | "fixture-single" | "none"
 *   missing=true → "이 지역은 아직 사전계산되지 않았습니다" 안내 대상.
 */
export async function loadRegionalShard(kind, sig) {
  const tried = await regionalShardPaths(kind, sig);
  for (const p of tried) {
    try {
      const env = await getJSON(p);
      const sub = unwrapSub(env, sig);
      if (sub) return { env, data: sub, source: "shard", path: p };
    } catch (e) {
      if (!(e instanceof DataMissingError)) return { env: null, data: null, source: "none", missing: true, tried, error: e };
    }
  }
  // 폴백 — 기존 단일 파일 (api/gap.json 등). 하위호환.
  let env;
  try { env = await loadFixture(kind); }
  catch (e) { return { env: null, data: null, source: "none", missing: true, tried, error: e }; }

  const dd = (env && env.data) || {};
  if (isSigMap(dd)) {
    if (dd[String(sig)]) return { env, data: dd[String(sig)], source: "fixture-map" };
    return { env: null, data: null, source: "none", missing: true, tried, fixtureRegions: Object.keys(dd) };
  }
  // 단일 객체 fixture — 어느 지역 결과인지 확인한다. 다른 지역으로 오인시키면 안 된다.
  const own = (dd.scope && (dd.scope.sig_cd || dd.scope.region_id))
    || (dd.target && (dd.target.sig_cd || dd.target.region_id))
    || dd.region_id || null;
  if (own && String(own) !== String(sig)) {
    return { env: null, data: null, source: "none", missing: true, tried, fixtureRegions: [String(own)] };
  }
  return { env, data: dd, source: "fixture-single", fixtureRegion: own ? String(own) : null };
}

function normCatalogEntry(x, kind, api) {
  const base = shardDir(kind);
  const conv = (k) => base + "/" + encodeURIComponent(String(k)) + ".json";
  if (typeof x === "string" || typeof x === "number") {
    const k = String(x);
    return { key: k, label: k, paths: [conv(k)], file: conv(k), meta: {} };
  }
  if (!x || typeof x !== "object") return null;
  const key = x.key ?? x.id ?? x.slug ?? x.template ?? x.bill_no ?? x.bill_id ?? x.query ?? x.name;
  if (key === undefined || key === null || key === "") return null;
  const label = x.label || x.title || x.name || x.template || x.query || String(key);
  const paths = [];
  for (const p of [pathOf(x.file), pathOf(x.path), layoutPath(api, kind, key)]) {
    for (const c of pathCandidates(p)) paths.push(c);
  }
  paths.push(conv(key));
  const uniq = [...new Set(paths.filter(Boolean))];
  return { key: String(key), label: String(label), paths: uniq, file: uniq[0], meta: x };
}

/**
 * 확산 템플릿 / 의안 / 질의 목록. 색인이 없으면 빈 배열 → 화면은 단일 fixture 로 동작.
 *
 * 소스가 둘이다.
 *   1) api/index.json          — make_nationwide.py (표결 15 · 검색 5)
 *   2) config.EXTRA_CATALOGS   — make_extend_fixtures.py (표결 149 · 검색 40)
 * 둘을 key 로 합친다. 앞선 소스가 우선이고, 확장 색인이 없으면 조용히 1)만 쓴다.
 */
export async function loadCatalog(kind) {
  const api = await loadApiIndex();
  const out = [];
  const seen = new Set();
  const push = (raw, idx) => {
    const e = normCatalogEntry(raw, kind, idx);
    if (e && !seen.has(e.key)) { seen.add(e.key); out.push(e); }
  };

  if (api) for (const raw of pickList(api, kind)) push(raw, api);

  const extra = EXTRA_CATALOGS[kind];
  if (extra) {
    try {
      const env = await getJSON(extra.path);
      const body = (env && env.data) || env || {};
      for (const listKey of extra.lists) for (const raw of asArray(body[listKey])) push(raw, null);
    } catch (e) {
      if (!(e instanceof DataMissingError)) throw e;   // 없으면 확장 색인만 건너뛴다
    }
  }
  return out;
}

/** 단일 fixture 가 실제로 어느 항목의 결과인지 — 폴백이 엉뚱한 항목을 대신 그리는 것을 막는다. */
function catalogKeyOf(kind, d) {
  if (!d || typeof d !== "object") return null;
  if (kind === "diffusion") return d.template != null ? String(d.template) : null;
  if (kind === "votes") {
    const b = d.bill || {};
    const v = b.bill_no ?? b.bill_id;
    return v != null && v !== "" ? String(v) : null;
  }
  if (kind === "search") return d.query != null ? String(d.query) : null;
  return null;
}

/**
 * 목록 항목 1건 로드. entry 가 null 이면(=색인 없음) 단일 fixture 를 그대로 쓴다.
 * shard 가 없을 때 단일 fixture 로 폴백하되, 그 fixture 가 다른 항목의 결과면 폴백하지 않는다.
 * 반환 source: "shard" | "fixture" | "fixture-fallback" | "none"
 */
export async function loadCatalogItem(kind, entry) {
  const tried = entry ? (entry.paths || (entry.file ? [entry.file] : [])) : [];
  for (const p of tried) {
    try {
      const env = await getJSON(p);
      return { env, data: (env && env.data) || env, source: "shard", path: p };
    } catch (e) {
      if (!(e instanceof DataMissingError)) return { env: null, data: null, source: "none", missing: true, tried, error: e };
    }
  }
  let env;
  try { env = await loadFixture(kind); }
  catch (e) { return { env: null, data: null, source: "none", missing: true, tried, error: e }; }

  const data = (env && env.data) || env;
  if (entry) {
    const own = catalogKeyOf(kind, data);
    const meta = entry.meta || {};
    const matches = own === null
      || own === entry.key
      || String(meta.bill_id ?? "") === own
      || String(meta.bill_no ?? "") === own
      || String(meta.template ?? "") === own;
    if (!matches) {
      return { env: null, data: null, source: "none", missing: true, tried, fixtureKey: own };
    }
  }
  return { env, data, source: entry ? "fixture-fallback" : "fixture" };
}

/* ==================================================================== *
 *  완전판(로컬 DB 직결) — viz/serve_full.py 의 /api/db/*
 *
 *  설계 원칙
 *   1) 있으면 쓰고 없으면 조용히 정적 shard 로 돌아간다. 배포본이 깨지면 안 된다.
 *   2) 화면은 full.enabled 만 보고 분기한다. 경로·타임아웃은 여기서 감춘다.
 *   3) 정적 shard 와 봉투 모양이 같다({data, as_of_date, stale, disclaimer}).
 * ==================================================================== */

export const full = {
  requested: null,   // ?full= 값 ("1"|"0"|null)
  enabled: false,    // DB API 를 실제로 쓸 수 있는가
  base: null,        // 확정된 API 기본 경로
  status: null,      // /status 의 data
  error: null,       // 탐지 실패 사유(요청했을 때만 화면에 알린다)
  probed: false,
};

function fullParam() {
  return new URLSearchParams(location.search).get("full");
}

async function fetchJSONTimeout(url, ms) {
  const ctrl = typeof AbortController === "function" ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), ms) : null;
  try {
    const res = await fetch(url, { cache: "no-store", signal: ctrl ? ctrl.signal : undefined });
    if (!res.ok) throw new DataMissingError(url, res.status);
    return await res.json();
  } finally {
    if (timer) clearTimeout(timer);
  }
}

let fullProbePromise = null;

/** 완전판 API 탐지. 절대 throw 하지 않는다. 여러 번 불러도 한 번만 탐지한다. */
export async function probeFullApi() {
  if (fullProbePromise) return fullProbePromise;
  fullProbePromise = (async () => {
    full.requested = fullParam();
    full.probed = true;
    if (full.requested === "0" || full.requested === "off" || full.requested === "false") {
      full.error = null;
      return full;
    }
    // 배포(https) 페이지에서 127.0.0.1 후보를 때리면 혼합콘텐츠/사설망 차단으로
    // 매 로딩마다 타임아웃까지 기다린다. 로컬에서 열었을 때만 절대 URL 후보를 쓴다.
    const isLocal = /^(localhost|127\.0\.0\.1|\[::1\]|)$/.test(location.hostname)
      || location.protocol === "file:";
    const candidates = FULL_API.candidates.filter(
      (b) => isLocal || !/^https?:\/\//.test(b));

    const tried = [];
    for (const base of candidates) {
      const url = `${base.replace(/\/+$/, "")}/status`;
      try {
        const env = await fetchJSONTimeout(url, FULL_API.probeTimeoutMs);
        const d = (env && env.data) || {};
        if (d.full_edition) {
          full.enabled = true;
          full.base = base.replace(/\/+$/, "");
          full.status = d;
          full.error = null;
          return full;
        }
        tried.push(`${url} → full_edition=false (${d.db_path || "DB 없음"})`);
      } catch (e) {
        tried.push(`${url} → ${e.name === "AbortError" ? "시간초과" : e.message}`);
      }
    }
    full.error = tried.join(" / ");
    return full;
  })();
  return fullProbePromise;
}

/** 완전판 API 호출. 봉투 그대로 돌려준다. 실패는 throw(화면이 폴백을 결정한다). */
export async function fullGet(path, params = {}) {
  if (!full.enabled || !full.base) throw new Error("완전판 API 가 연결되어 있지 않습니다.");
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== "") qs.set(k, String(v));
  }
  const url = `${full.base}/${String(path).replace(/^\/+/, "")}` + (qs.toString() ? `?${qs}` : "");
  return fetchJSONTimeout(url, FULL_API.requestTimeoutMs);
}

/* ---------------- 정적 조례 번들(columnar-v1) 디코더 ---------------- *
 *  make_full_ordinance.py 산출. 199,858건을 키 반복 없이 담기 위해 열지향이다.
 *  규약은 각 파일의 data.format / columns / enums / derived 에 들어 있다.
 * ------------------------------------------------------------------- */

/** columnar-v1 (또는 objects-v1) 을 객체 배열로 편다. */
export function decodeOrdinanceBundle(env) {
  const d = (env && env.data) || {};
  if (d.format === "objects-v1" || Array.isArray(d.ordinances)) {
    return { region: d.region || null, counts: d.counts || null,
             items: d.ordinances || d.rows || [] };
  }
  const cols = d.columns || [];
  const enums = d.enums || {};
  const urlTpl = (d.derived && d.derived.official_url) || null;
  const items = (d.rows || []).map((row) => {
    const o = {};
    cols.forEach((c, i) => {
      const raw = row[i];
      const table = enums[c];
      o[c] = (table && typeof raw === "number") ? (table[raw] ?? null) : (raw ?? null);
    });
    if (!o.ordinance_id && o.mst) o.ordinance_id = `ordin:${o.mst}`;
    if (!o.official_url && urlTpl && o.mst) o.official_url = urlTpl.replace("{mst}", o.mst);
    return o;
  });
  return { region: d.region || null, counts: d.counts || null, items };
}

/** api/ordinance/{sig}.json — 지역 자치법규 전량 메타(정적 shard). */
export async function loadOrdinanceBundle(sig) {
  const env = await getJSON(`api/ordinance/${sig}.json`);
  return { env, ...decodeOrdinanceBundle(env) };
}

/** api/ordinance/articles/{sig}.json — 조문 '번호+제목'만(본문은 완전판 전용). */
export async function loadArticleTitles(sig) {
  const env = await getJSON(`api/ordinance/articles/${sig}.json`);
  const d = (env && env.data) || {};
  return { env, byMst: d.articles || {}, note: d.body_excluded || null };
}
