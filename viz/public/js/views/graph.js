// 5. 법령 위계 그래프 — api/graph/ shard 를 실제로 그린다.
//
//    구조: 세 모드를 탭으로 전환한다(해시 쿼리로 딥링크된다).
//      #/graph?mode=hierarchy                     위계 개념도 (api/graph/hierarchy.json)
//      #/graph?mode=ordinance&key=ordin-1735111   조례 중심 2홉 (api/graph/ordinance/{key}.json)
//      #/graph?mode=statute&key=statute-276653    법령 중심     (api/graph/statute/{key}.json)
//      #/graph?mode=region&key=11110              지역 전량     (api/graph/by-region/{sig_cd}.json)
//
//    api/graph/index.json 이 없으면(가상데이터 번들 등) 예전 경로 — graph/nodes.json+edges.json 의
//    ego 서브그래프 — 로 떨어지고, shard 생성 명령을 안내한다.
//
//    렌더러는 하나다. shard 든 구 번들이든 {nodes, edges} 를 subgraphPanel() 에 넘긴다.
import { el, num, ymd, debounce } from "../util.js";
import { LIMITS, SATELLITE_TILE } from "../config.js";
import { getJSON, DataMissingError, loadAdmDongGeo, loadGraph, loadGraphStats,
         loadRegionCatalog } from "../api.js";
import { section, table, note, loading, asOfLine, errorPanel, cdnFailPanel,
         badge, statusBadge, statCard, envelopeFooter } from "../components.js";
import { ensureLeaflet, ensureVisNetwork } from "../vendor.js";

const IDX_PATH = "api/graph/index.json";
const GENERATOR = "system/make_graph_fixtures.py";
/** 지역 묶음이 공유하는 상위법 사전(전국 32,727건). 지역 파일마다 복제하지 않으려고 뺐다. */
const INSTRUMENTS_PATH = "api/graph/instruments.json";

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

const LABEL_KO = {
  Region: "지자체", Ordinance: "조례·규칙", LegalInstrument: "법령",
  Bill: "의안", Legislator: "의원", Party: "정당", Category: "분야", BudgetLine: "예산",
};

const TIER_FALLBACK = {
  0: "헌법", 1: "법률·조약", 2: "대통령령·국회규칙", 3: "총리령·부령",
  4: "행정규칙·고시·훈령", L1: "조례·의회규칙", L2: "규칙·교육규칙",
};

const VERIF_KO = {
  "article-verified": "조문확인",
  "article-missing": "조문불일치",
  "unverifiable": "확인불가",
  "source-linked": "원문링크",
  "unverified": "미검증",
  "needs-review": "검토필요",
  "body-missing": "본문미수집",
};

/* ==================================================================== *
 *  진입
 * ==================================================================== */

export async function render(root, params, query = {}) {
  const box = el("div", {});
  root.appendChild(box);
  box.appendChild(loading("법령 위계 shard 색인을 확인하는 중…"));

  let idx = null;
  let idxErr = null;
  try { idx = await getJSON(IDX_PATH); }
  catch (e) { idxErr = e; }
  box.innerHTML = "";

  if (!idx) {
    box.appendChild(noIndexPanel(idxErr));
    await renderLegacy(box);
    return;
  }

  const ords = Array.isArray(idx.ordinances) ? idx.ordinances : [];
  const stats = Array.isArray(idx.statutes) ? idx.statutes : [];
  const regions = Array.isArray(idx.regions) ? idx.regions : [];
  const ordKeys = new Set(ords.map((e) => e.key));
  const statKeys = new Set(stats.map((e) => e.key));

  const sec = section("법령 위계 그래프",
    asOfLine(`shard 색인 ${IDX_PATH} (기준일 ${idx.as_of_date || "?"}) · 조례 서브그래프 ${num(ords.length)}건 · 법령 서브그래프 ${num(stats.length)}건`));
  box.appendChild(sec);

  const nm = idx.name_match || {};
  if (nm.cites_edges_unlocked) {
    sec.appendChild(note(
      `상위법 이름해소(name-match): 법령명만 있고 노드가 없던 인용 ${num(nm.cites_edges_unlocked)}건을 `
      + `법령 ${num(nm.resolved_lawnames)}개에 붙였다. 해소된 관계는 추론이므로 그래프에서 `
      + `resolved_by="name-match" 로 표기된다.`));
  }

  const tabs = [
    { key: "hierarchy", label: "위계 개념도" },
    { key: "ordinance", label: `조례 중심 (${num(ords.length)})` },
    { key: "statute", label: `법령 중심 (${num(stats.length)})` },
  ];
  // 지역 전량 묶음(make_full_graph.py). 없으면 탭 자체를 만들지 않는다 — 구 번들 하위호환.
  if (regions.length) tabs.push({ key: "region", label: `지역 전량 (${num(regions.length)})` });

  let mode = tabs.some((t) => t.key === query.mode) ? query.mode : "hierarchy";
  let key = query.key || null;

  const bar = el("div", { class: "toolbar" });
  const body = el("div", {});
  sec.appendChild(bar);
  sec.appendChild(body);

  const ctx = { idx, ords, stats, regions, ordKeys, statKeys, jump };

  function jump(nextMode, nextKey) {
    mode = nextMode;
    key = nextKey || null;
    show();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  let token = 0;
  async function show() {
    const my = ++token;
    drawTabs();
    syncUrl(mode, key);
    body.innerHTML = "";
    body.appendChild(loading("서브그래프를 불러오는 중…"));
    const host = el("div", {});
    try {
      if (mode === "region") {
        const resolved = await renderRegionMode(host, ctx, key, (k) => { key = k; show(); });
        if (resolved && resolved !== key) { key = resolved; syncUrl(mode, key); }
      } else if (mode === "ordinance" || mode === "statute") {
        // 목록의 첫 항목으로 떨어졌을 때도 주소에 실제 key 를 남긴다(딥링크·새로고침 대비).
        const resolved = await renderShardMode(host, ctx, mode, key, (k) => { key = k; show(); });
        if (resolved && resolved !== key) { key = resolved; syncUrl(mode, key); }
      } else {
        await renderHierarchy(host, ctx);
      }
    } catch (e) {
      host.appendChild(errorPanel(e, "서브그래프 렌더 실패"));
      console.error(e);
    }
    if (my !== token) return;
    body.innerHTML = "";
    body.appendChild(host);
  }

  function drawTabs() {
    bar.innerHTML = "";
    for (const t of tabs) {
      const active = t.key === mode;
      bar.appendChild(el("button", {
        class: "btn",
        "aria-pressed": active ? "true" : "false",
        style: active ? "background:var(--brand);color:#fff;border-color:var(--brand);font-weight:600" : null,
        text: t.label,
        onclick: () => { if (!active) { mode = t.key; key = null; show(); } },
      }));
    }
  }

  await show();
}

/** 탭 전환을 주소에 남긴다. hashchange 를 일으키면 화면이 통째로 다시 그려지므로 replaceState 를 쓴다. */
function syncUrl(mode, key) {
  const q = new URLSearchParams();
  q.set("mode", mode);
  if (key) q.set("key", key);
  const next = `#/graph?${q.toString()}`;
  if (location.hash !== next) history.replaceState(null, "", next);
}

function noIndexPanel(err) {
  return el("div", { class: "panel panel-warn" },
    el("h2", { class: "panel-title", text: "법령 위계 shard 가 아직 없습니다" }),
    el("p", { text:
      `${IDX_PATH} 를 찾지 못했습니다. 이 화면의 위계 개념도·조례 중심·법령 중심 뷰는 `
      + "서브그래프 shard 를 전제로 합니다. 아래는 구 정적 번들(graph/nodes.json)로 그린 대체 화면입니다." }),
    el("p", { class: "hint" },
      el("b", { text: "생성 방법: " }),
      el("code", { text: `python ${GENERATOR}` }),
      document.createTextNode(" → "),
      el("code", { text: "api/graph/{index,hierarchy}.json · api/graph/ordinance|statute/{key}.json" })),
    err ? el("pre", { class: "err", text: `${err.name || "Error"}: ${err.message || err}` }) : null);
}

/* ==================================================================== *
 *  위계 개념도 (api/graph/hierarchy.json)
 * ==================================================================== */

async function renderHierarchy(host, ctx) {
  let env;
  try { env = await getJSON("api/graph/hierarchy.json"); }
  catch (e) {
    host.appendChild(note(`api/graph/hierarchy.json 을 읽지 못했습니다 (${e.message}). `
      + `python ${GENERATOR} --only hierarchy 로 생성합니다.`, "warn"));
    return;
  }
  const d = env.data || env;
  const t = d.totals || {};
  const labels = d.tier_labels || {};
  const tierLabel = (k) => labels[String(k)] || TIER_FALLBACK[k] || String(k);

  host.appendChild(el("div", { class: "stat-grid" },
    statCard("법령(국가)", num(t.legal_instrument), "legal_instrument"),
    statCard("자치법규", num(t.ordinances), "조례·규칙 전체"),
    statCard("그중 폐지", num(t.ordinances_repealed),
      t.ordinances ? `${((t.ordinances_repealed / t.ordinances) * 100).toFixed(1)}%` : null),
    statCard("지자체", num(t.regions), "regions")));

  // ── 계단 ─────────────────────────────────────────────────────────
  const tiers = Array.isArray(d.tiers) ? d.tiers : [];
  const maxNodes = Math.max(1, ...tiers.map((x) => x.nodes || 0));
  const ladder = el("div", { style: "margin:12px 0" });
  for (const axis of ["national", "local"]) {
    const rows = tiers.filter((x) => x.axis === axis);
    if (!rows.length) continue;
    ladder.appendChild(el("h3", { style: "margin:14px 0 6px;font-size:14px",
      text: axis === "national" ? "국가 법령축 (헌법 → 법률 → 시행령 → 시행규칙 → 행정규칙)" : "자치 법규축 (조례 → 규칙)" }));
    for (const r of rows) {
      const w = Math.max(2, Math.round((Math.log10((r.nodes || 0) + 1) / Math.log10(maxNodes + 1)) * 100));
      const kinds = Object.entries(r.kinds || {}).sort((a, b) => b[1] - a[1]).slice(0, 4)
        .map(([k, v]) => `${k} ${num(v)}`).join(" · ");
      ladder.appendChild(el("div", { style: "margin:5px 0" },
        el("div", { style: "display:flex;gap:8px;align-items:baseline;flex-wrap:wrap" },
          el("span", { style: "min-width:150px;font-weight:600;font-size:13px",
            text: `${r.tier === null || r.tier === undefined ? "미분류" : r.tier}. ${r.label || tierLabel(r.tier)}` }),
          el("span", { class: "muted small", text: `${num(r.nodes)}건${r.repealed ? ` · 폐지 ${num(r.repealed)}` : ""}` }),
          el("span", { class: "muted small", text: kinds })),
        el("div", { style: `height:10px;border-radius:5px;width:${w}%;background:${axis === "national" ? "#e74c3c" : "#3498db"};opacity:.65` })));
    }
  }
  host.appendChild(section("법령 위계 — tier 별 규모",
    note("막대 길이는 log 스케일이다(건수 차가 100배 이상이라 선형으로는 아래 tier 가 보이지 않는다)."),
    ladder));

  // ── 위임 엣지 ────────────────────────────────────────────────────
  const de = d.delegation_edges || {};
  const ce = d.citation_edges || {};
  const delSec = section("위임(DELEGATED_FROM) — 조례가 어느 상위법에 근거하는가");
  delSec.appendChild(el("div", { class: "stat-grid" },
    statCard("위임 관계 전수", num(de.total), "delegations 테이블"),
    statCard("상위법 노드 해소", num(de.parent_resolved_after_name_match),
      de.total ? `${((de.parent_resolved_after_name_match / de.total) * 100).toFixed(1)}% (이름해소 +${num(de.gained_by_name_match)})` : null),
    statCard("미해결 상위법", num((de.total || 0) - (de.parent_resolved_after_name_match || 0)), "법령 원문 미수집")));
  if (de.by_verification_status) {
    delSec.appendChild(el("h3", { style: "font-size:14px;margin:10px 0 4px", text: "인용 검증 상태" }));
    delSec.appendChild(shareBars(de.by_verification_status, {
      "article-verified": "#1e7a4b", "article-missing": "#c0392b", "unverifiable": "#8a929c",
    }, VERIF_KO));
    delSec.appendChild(note("‘조문확인’은 인용된 상위법 조문이 실제로 존재하는지만 본 것이다. "
      + "인용의 해석·적용 타당성 검증이 아니다. 검증 공시 화면(#/trust)에 전수 비율이 있다."));
  }
  if (de.by_source_path) {
    delSec.appendChild(table(["추출 경로", "건수"],
      Object.entries(de.by_source_path).sort((a, b) => b[1] - a[1]).map(([k, v]) => [k, num(v)])));
  }
  host.appendChild(delSec);

  // ── 인용 엣지(이름해소 이야기) ───────────────────────────────────
  if (ce.total) {
    const gain = (ce.dst_resolved_after_name_match || 0) - (ce.static_bundle_included || 0);
    host.appendChild(section("인용(CITES) — 이름만 있던 상위법을 붙인 결과",
      el("div", { class: "stat-grid" },
        statCard("인용 관계 전수", num(ce.total), "instrument_relations"),
        statCard("구 정적번들 수록", num(ce.static_bundle_included),
          ce.total ? `${((ce.static_bundle_included / ce.total) * 100).toFixed(1)}%` : null),
        statCard("이름해소 후 수록", num(ce.dst_resolved_after_name_match),
          ce.total ? `${((ce.dst_resolved_after_name_match / ce.total) * 100).toFixed(1)}% (+${num(gain)})` : null)),
      note(ce.note || "", "warn"),
      d.name_match ? note(`${d.name_match.method} — ${d.name_match.caveat}`) : null));
  }

  // ── tier 흐름 ────────────────────────────────────────────────────
  const flow = d.flow_parent_tier_to_child || {};
  const flowRows = [];
  let flowTotal = 0;
  for (const [pt, kids] of Object.entries(flow)) {
    for (const [ct, n] of Object.entries(kids || {})) { flowRows.push([pt, ct, n]); flowTotal += n; }
  }
  flowRows.sort((a, b) => b[2] - a[2]);
  if (flowRows.length) {
    host.appendChild(section("상위법 tier → 자치법규 흐름",
      table(["상위법 tier", "자치 tier", "위임 건수", "비중"],
        flowRows.map(([p, c, n]) => [
          p === "미해결" ? "미해결(법령 미수집)" : `${p}. ${tierLabel(p)}`,
          `${c}. ${tierLabel(c)}`, num(n),
          flowTotal ? `${((n / flowTotal) * 100).toFixed(1)}%` : "—"])),
      note("‘미해결’은 조례가 인용한 상위법이 legal_instrument 에 없는 경우다. 근거가 없다는 뜻이 아니라 "
        + "그 법령 원문을 아직 수집하지 못했다는 뜻이다.")));
  }

  // ── 상위법 순위 ──────────────────────────────────────────────────
  const goStatute = (instrumentId) => {
    const k = String(instrumentId).replace(/:/g, "-");
    return ctx.statKeys.has(k) ? k : null;
  };
  const parentRows = (list) => (list || []).map((p) => {
    const k = goStatute(p.instrument_id);
    return [
      k ? el("button", { class: "btn-link", text: p.name || p.instrument_id,
                         onclick: () => ctx.jump("statute", k) })
        : (p.name || p.instrument_id),
      p.kind || "—",
      p.tier === null || p.tier === undefined ? "—" : `${p.tier}. ${tierLabel(p.tier)}`,
      num(p.delegating_ordinances), num(p.citing_edges),
    ];
  });
  if (d.top_parents?.length) {
    host.appendChild(section("가장 많은 조례가 근거로 삼는 상위법 TOP 30",
      note("이름을 누르면 그 법령 중심 서브그래프로 이동한다(서브그래프가 구워진 법령만 링크된다)."),
      table(["법령", "종류", "tier", "위임 조례 수", "인용 엣지"], parentRows(d.top_parents))));
  }
  if (d.top_cited?.length) {
    host.appendChild(section("가장 많이 인용되는 법령 TOP 30",
      table(["법령", "종류", "tier", "위임 조례 수", "인용 엣지"], parentRows(d.top_cited))));
  }
  if (d.unresolved_top?.length) {
    host.appendChild(section("미수집 상위법 TOP 30 (이름만 확인됨)",
      note("조례 본문이 인용했지만 legal_instrument 에 대응 법령이 없는 이름들이다. "
        + "수집 우선순위 목록이기도 하다.", "warn"),
      table(["법령명(정규화)", "인용 엣지"],
        d.unresolved_top.map((u) => [String(u.lawname).replace(/^lawname:/, ""), num(u.citing_edges)]))));
  }

  // ── 지자체 승계 ──────────────────────────────────────────────────
  if (d.region_succession?.length) {
    host.appendChild(section(`지자체 승계 ${num(d.region_succession.length)}건`,
      table(["구 지자체", "신 지자체", "유형", "시행일", "근거"],
        d.region_succession.map((r) => [
          `${r.old_name || "—"} (${r.old_region_id})`,
          `${r.new_name || "—"} (${r.new_region_id})`,
          r.succession_type || "—", r.effective_date || "—", r.legal_basis || "—"])),
      note("승계 이후 조례의 효력은 특별법 부칙 경과규정에 따른다. 이 표는 코드 대응 관계이지 효력 판단이 아니다. "
        + "잔여 감사(T7·T8)는 정책 생애주기 화면(#/lifecycle)에서 본다.")));
  }

  host.appendChild(envelopeFooter(env));
}

/** 값 맵을 가로 100% 누적 막대로. */
function shareBars(obj, colors, ko = {}) {
  const entries = Object.entries(obj).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((a, [, v]) => a + v, 0) || 1;
  const bar = el("div", { style: "display:flex;height:18px;border-radius:4px;overflow:hidden;margin:6px 0" });
  const leg = el("div", { class: "legend" });
  for (const [k, v] of entries) {
    const c = colors[k] || "#95a5a6";
    bar.appendChild(el("div", { style: `width:${(v / total) * 100}%;background:${c}`,
                                title: `${k} ${num(v)}` }));
    leg.appendChild(el("span", { class: "legend-item" },
      el("i", { class: "swatch", style: `background:${c}` }),
      el("span", { text: `${ko[k] || k} ${num(v)} (${((v / total) * 100).toFixed(2)}%)` })));
  }
  return el("div", {}, bar, leg);
}

/* ==================================================================== *
 *  조례 / 법령 중심 shard
 * ==================================================================== */

async function renderShardMode(host, ctx, kind, key, onKey) {
  const entries = kind === "ordinance" ? ctx.ords : ctx.stats;
  if (!entries.length) {
    host.appendChild(note(`${IDX_PATH} 에 ${kind} 서브그래프 목록이 없습니다. `
      + `python ${GENERATOR} --only ${kind} 로 생성합니다.`, "warn"));
    return;
  }
  const cur = entries.find((e) => e.key === key) || entries[0];

  const catalog = await loadRegionCatalog().catch(() => null);
  const groupOf = kind === "ordinance"
    ? (e) => (catalog && e.sig_cd ? catalog.sidoOf(e.sig_cd) : (e.region_name || "지역 미상"))
    : (e) => (e.tier ? `tier ${e.tier} · ${TIER_FALLBACK[e.tier] || ""}` : "tier 미상");
  const textOf = kind === "ordinance"
    ? (e) => `${e.name} — ${e.region_name || e.sig_cd || ""} (연결 ${e.degree ?? "?"})`
    : (e) => `${e.name} — 위임 조례 ${num(e.delegating_ordinances || 0)}건`;

  host.appendChild(entryPicker({
    entries, current: cur.key, groupOf, textOf, onChange: onKey,
    label: kind === "ordinance" ? "기준 조례" : "기준 법령",
    placeholder: kind === "ordinance" ? "조례명·지자체 검색" : "법령명 검색",
  }));

  const detail = el("div", {});
  host.appendChild(detail);
  detail.appendChild(loading(`${cur.name} 서브그래프 로드 중…`));

  const paths = shardPaths(ctx.idx, kind, cur.key, cur);
  const got = await firstJSON(paths);
  detail.innerHTML = "";
  if (!got.doc) {
    detail.appendChild(shardMissingPanel(kind, cur, got));
    return cur.key;
  }
  const env = got.doc;
  const d = env.data || env;
  const seed = d.seed || {};

  const head = el("div", {});
  head.appendChild(el("h3", { style: "margin:6px 0", text: seed.name || cur.name }));
  const chips = el("div", { class: "chip-row" });
  if (kind === "ordinance") {
    if (seed.org_name) chips.appendChild(badge(seed.org_name, "badge-info"));
    if (seed.status) chips.appendChild(statusBadge(seed.status));
    if (seed.repealed_on) chips.appendChild(badge(`폐지일 ${ymd(seed.repealed_on)}`, "badge-repealed"));
    chips.appendChild(badge(`${d.hops ?? 2}홉 · 노드 상한 ${num(d.max_nodes)}`, "badge-plain"));
  } else {
    if (seed.kind) chips.appendChild(badge(seed.kind, "badge-info"));
    if (seed.tier !== undefined && seed.tier !== null) {
      chips.appendChild(badge(`tier ${seed.tier} · ${TIER_FALLBACK[seed.tier] || ""}`, "badge-plain"));
    }
    if (seed.status) chips.appendChild(statusBadge(seed.status));
    // status 와 같은 말을 두 번 보여주지 않는다(active=현행).
    if (seed.current_history && !(seed.current_history === "현행" && seed.status === "active")) {
      chips.appendChild(badge(seed.current_history, "badge-plain"));
    }
  }
  head.appendChild(chips);
  head.appendChild(el("div", { class: "as-of", text: `데이터 소스: ${got.path}` }));
  detail.appendChild(head);

  const st = d.stats || {};
  const cards = [
    statCard("노드", num(st.nodes ?? (d.nodes || []).length),
      Object.entries(st.by_label || {}).map(([k, v]) => `${LABEL_KO[k] || k} ${v}`).join(" · ")),
    statCard("엣지", num(st.edges ?? (d.edges || []).length),
      Object.entries(st.by_relation || {}).map(([k, v]) => `${(REL_STYLE[k] || {}).label || k} ${v}`).join(" · ")),
  ];
  if (st.unresolved_instruments !== undefined) {
    cards.push(statCard("미수집 상위법 노드", num(st.unresolved_instruments), "이 그림 안, 이름만 확인된 법령"));
  }
  if (st.name_matched_instruments !== undefined) {
    cards.push(statCard("이름해소 법령", num(st.name_matched_instruments), "추론 연결"));
  }
  cards.push(statCard("폐지 노드", num(st.repealed_nodes || 0), "회색 + 붉은 테두리"));
  detail.appendChild(el("div", { class: "stat-grid" }, ...cards));

  // 표본·절단 고지
  const cov = d.coverage;
  if (cov) {
    detail.appendChild(note(
      `표본 안내: 위임 조례 ${num(cov.delegating_ordinances_total)}건 중 ${num(cov.delegating_ordinances_shown)}건, `
      + `인용 엣지 ${num(cov.citing_edges_total)}건 중 ${num(cov.citing_ordinances_shown)}건만 그린다. `
      + (cov.note || ""), "warn"));
  }
  const tr = d.truncated;
  if (tr) {
    const bits = [];
    if (tr.delegation_parents_total > tr.delegation_parents_shown) {
      bits.push(`상위법 ${num(tr.delegation_parents_total)}개 중 ${num(tr.delegation_parents_shown)}개`);
    }
    if (tr.cited_instruments_total > tr.cited_instruments_shown) {
      bits.push(`피인용 법령 ${num(tr.cited_instruments_total)}개 중 ${num(tr.cited_instruments_shown)}개`);
    }
    const hubs = Object.entries(tr.hub_peers || {});
    if (hubs.length) {
      bits.push(`2홉 이웃 조례는 상위법별 표본(예: ${hubs[0][0]} 전국 ${num(hubs[0][1].total_children)}건 중 ${num(hubs[0][1].added)}건)`);
    }
    if (bits.length) detail.appendChild(note(`절단 안내 — ${bits.join(" / ")}. ${tr.note || ""}`, "warn"));
  }
  if (st.seed_unresolved_parents) {
    detail.appendChild(note(
      `이 조례가 인용한 상위법 중 ${num(st.seed_unresolved_parents)}건은 법령 원문이 수집되지 않아 `
      + "점선 테두리의 미해결 노드로 표시된다(tier·폐지 여부 미상).", "warn"));
  }

  await subgraphPanel(detail, {
    nodes: d.nodes || [], edges: d.edges || [], seedId: seed.id || null,
    defaults: d.defaults || null, ctx, kind,
  });

  if (kind === "statute") await articlesPanel(detail, cur);
  detail.appendChild(envelopeFooter(env));
  return cur.key;
}

/* ==================================================================== *
 *  지역 전량 (api/graph/by-region/{sig_cd}.json · 247곳)
 *
 *  조례 개별 shard 1,000건은 표본이고, 이 묶음이 위임 구조의 전량이다.
 *  파일은 용량 때문에 (가) 엣지를 인덱스 배열로 접고 (나) 상위법 노드를 전국 공용
 *  사전(graph/instruments.json)으로 빼고 (다) 지역 안에서 상수인 필드를 defaults 로
 *  올려 뒀다. 여기서 그걸 도로 펴서 기존 subgraphPanel 에 그대로 넘긴다.
 * ==================================================================== */

/** by-region 번들을 {nodes, edges} 로 편다. 규약은 파일의 data.edge_encoding·data.defaults 에 있다. */
function decodeRegionBundle(d, dict) {
  const df = d.defaults || {};
  const labelOf = df["node.label"] || { ordinance: "Ordinance", instrument: "LegalInstrument" };
  const hopOf = df["node.hop"] || { ordinance: 0, instrument: 1 };
  const verCodes = df["edge.verification_status_codes"] || [];
  const region = d.region || {};
  const prefix = region.name_prefix || "";
  const nameMatched = new Set(d.name_matched || []);

  const ordNodes = (d.nodes || []).map((n) => ({
    ...n,
    label: labelOf.ordinance || "Ordinance",
    // 생성기가 조례명 앞의 지자체 이름을 잘라냈다(data.region.name_prefix). 붙여서 되돌린다.
    name: prefix && n.name ? `${prefix} ${n.name}` : n.name,
    ord_kind: n.ord_kind ?? df["node.ord_kind"],
    status: n.status ?? df["node.status"],
    repealed: n.repealed ?? df["node.repealed"],
    hop: hopOf.ordinance ?? 0,
    region_name: region.full_name || region.name,
    org_name: region.org_name,
    src_id: String(n.id || "").replace(/^ordinance:/, ""),
  }));

  const instIds = d.instruments || [];
  const instNodes = instIds.map((id, i) => {
    const meta = (dict && dict[id]) || {};
    const bare = String(id).replace(/^instrument:/, "");
    // lawname: 은 인용문에서 이름만 확인된 미수집 법령이다(단정 금지).
    const resolved = meta.resolved !== undefined ? meta.resolved : !bare.startsWith("lawname:");
    const out = {
      id,
      label: labelOf.instrument || "LegalInstrument",
      name: meta.name || bare.replace(/^lawname:/, ""),
      hop: hopOf.instrument ?? 1,
      resolved,
      src_id: bare,
    };
    if (meta.instrument_kind) out.instrument_kind = meta.instrument_kind;
    if (meta.tier !== undefined) out.tier = meta.tier;
    if (meta.status) out.status = meta.status;
    if (nameMatched.has(i)) out.resolved_by = "name-match";
    return out;
  });

  const nodes = ordNodes.concat(instNodes);
  const n0 = ordNodes.length;
  const idAt = (i) => (i < n0 ? ordNodes[i].id : (instNodes[i - n0] || {}).id);

  const edges = [];
  for (const e of d.edges || []) {
    const src = idAt(e[0]);
    const dst = idAt(e[1]);
    if (!src || !dst) continue;          // 색인이 범위를 벗어나면 그리지 않는다(조용히 버리지 말고 셈은 stats 로 본다)
    const row = {
      source: src,
      target: dst,
      relation: d.relation || df["edge.relation"] || "DELEGATED_FROM",
      count: e[2] ?? df["edge.count"] ?? 1,
      verification_status: e[3] !== undefined
        ? (verCodes[e[3]] || String(e[3]))
        : df["edge.verification_status"],
    };
    const tgt = nodes[e[1]];
    if (tgt && tgt.resolved_by) row.resolved_by = tgt.resolved_by;
    edges.push(row);
  }
  return { nodes, edges, ordinanceCount: n0 };
}

async function renderRegionMode(host, ctx, key, onKey) {
  const entries = ctx.regions;
  if (!entries.length) {
    host.appendChild(note(`${IDX_PATH} 에 지역 묶음 목록이 없습니다. `
      + "python system/make_full_graph.py --only by-region 로 생성합니다.", "warn"));
    return;
  }
  const cur = entries.find((e) => String(e.sig_cd) === String(key)) || entries[0];

  host.appendChild(entryPicker({
    entries: entries.map((e) => ({ ...e, key: String(e.sig_cd) })),
    current: String(cur.sig_cd),
    groupOf: (e) => (e.full_name && e.full_name !== e.name
      ? e.full_name.split(" ")[0]
      : (e.level === 1 ? "시·도" : "기타")),
    textOf: (e) => `${e.full_name || e.name} (${e.sig_cd}) — 조례 ${num(e.ordinances || 0)} · 위임 ${num(e.edges || 0)}`,
    onChange: onKey,
    label: "지역", placeholder: "지자체명·코드 검색",
  }));

  const detail = el("div", {});
  host.appendChild(detail);
  detail.appendChild(loading(`${cur.full_name || cur.name} 위임 구조를 불러오는 중…`));

  const relPath = cur.path ? "api/graph/" + cur.path : `api/graph/by-region/${cur.sig_cd}.json`;
  let env, dict = null;
  try {
    [env, dict] = await Promise.all([
      getJSON(relPath),
      getJSON(INSTRUMENTS_PATH).then((doc) => (doc.data || doc).instruments || {}).catch(() => null),
    ]);
  } catch (e) {
    detail.innerHTML = "";
    detail.appendChild(shardMissingPanel("by-region", cur, { tried: [relPath], error: e }));
    return String(cur.sig_cd);
  }
  detail.innerHTML = "";

  const d = env.data || env;
  const st = d.stats || {};
  const tr = d.truncated || {};
  const dec = decodeRegionBundle(d, dict);

  const head = el("div", {});
  head.appendChild(el("h3", { style: "margin:6px 0", text: (d.region || cur).full_name || cur.name }));
  const chips = el("div", { class: "chip-row" });
  if ((d.region || {}).status) chips.appendChild(statusBadge(d.region.status));
  chips.appendChild(badge(`위임 관계만 (DELEGATED_FROM)`, "badge-plain"));
  if (!dict) chips.appendChild(badge("상위법 사전 미로드 — 법령명 대신 id 표시", "badge-warn"));
  head.appendChild(chips);
  head.appendChild(el("div", { class: "as-of", text: `데이터 소스: ${relPath} + ${INSTRUMENTS_PATH}` }));
  detail.appendChild(head);

  detail.appendChild(el("div", { class: "stat-grid" },
    statCard("조례 노드", num(st.ordinance_nodes ?? dec.ordinanceCount), "이 지역에서 위임 근거가 있는 조례"),
    statCard("상위법 노드", num(st.instrument_nodes ?? 0),
      `미수집(이름만) ${num(st.unresolved_instruments || 0)} · 이름해소 ${num(st.name_matched_instruments || 0)}`),
    statCard("위임 엣지", num(st.edges ?? dec.edges.length), `원본 위임 행 ${num(st.delegation_rows || 0)}건을 접은 것`),
    statCard("폐지 노드", num(st.repealed_nodes || 0), "회색 + 붉은 테두리")));

  // 결손을 숨기지 않는다 — 위임 근거가 없어 그래프에 아예 없는 조례를 명시한다.
  if (tr.region_ordinances_all) {
    detail.appendChild(note(
      `이 지역 자치법규 ${num(tr.region_ordinances_all)}건 중 위임 근거가 수집된 것은 `
      + `${num(tr.ordinances_total || 0)}건이다. 나머지 ${num(tr.ordinances_without_delegation || 0)}건은 `
      + "자치조례이거나 상위법 인용이 수집되지 않아 이 그림에 노드가 없다. "
      + (tr.ordinances_shown < tr.ordinances_total
        ? `또한 노드 상한 때문에 ${num(tr.ordinances_shown)}건만 그린다.` : ""), "warn"));
  }
  if (st.unresolved_instruments) {
    detail.appendChild(note(
      `상위법 ${num(st.unresolved_instruments)}건은 법령 원문이 수집되지 않아 이름만 확인된 상태다`
      + "(점선 테두리 · tier·폐지여부 미상). 단정해서 읽으면 안 된다.", "warn"));
  }
  if (st.name_matched_instruments) {
    detail.appendChild(note(
      `상위법 ${num(st.name_matched_instruments)}건은 법령명 정규화로 붙인 추정(name-match) 연결이다.`, "warn"));
  }
  detail.appendChild(note(
    "지역 묶음은 위임(DELEGATED_FROM)만 담는다. 인용(CITES)과 조문 단위 근거는 "
    + "'조례 중심' 탭의 개별 shard 에 있다.", ""));

  // 지역 전체는 수천 노드라 통째로 그리면 브라우저가 멈춘다. 기존 ego() 로 기준 조례 주변만 그린다.
  const seeds = dec.nodes.slice(0, dec.ordinanceCount)
    .filter((n) => dec.edges.some((e) => e.source === n.id))
    .map((n) => ({ key: n.id, name: n.name, parents: n.parents || 0 }))
    .sort((a, b) => (b.parents || 0) - (a.parents || 0));

  if (!seeds.length) {
    detail.appendChild(note("이 지역에는 위임 근거가 있는 조례가 없어 그래프를 그릴 수 없습니다.", "warn"));
    detail.appendChild(envelopeFooter(env));
    return String(cur.sig_cd);
  }

  const byId = new Map(dec.nodes.map((n) => [n.id, n]));
  const graphBox = el("div", {});
  detail.appendChild(entryPicker({
    entries: seeds, current: seeds[0].key,
    groupOf: () => `위임 근거가 있는 조례 (${seeds.length})`,
    textOf: (e) => `${e.name} — 근거 상위법 ${num(e.parents || 0)}건`,
    onChange: (k) => drawSeed(k),
    label: "기준 조례", placeholder: "조례명 검색",
  }));
  detail.appendChild(graphBox);

  async function drawSeed(seedId) {
    graphBox.innerHTML = "";
    const sub = ego(seedId, 2, dec.edges, byId, LIMITS.graphRenderNodes);
    await subgraphPanel(graphBox, {
      nodes: sub.nodes, edges: sub.edges, seedId,
      defaults: d.defaults || null, ctx, kind: "region",
    });
    if (sub.truncated) {
      graphBox.appendChild(note(`노드 상한 ${LIMITS.graphRenderNodes} 에 걸려 잘렸다.`, "warn"));
    }
  }
  await drawSeed(seeds[0].key);

  detail.appendChild(envelopeFooter(env));
  return String(cur.sig_cd);
}

function shardPaths(idx, kind, key, entry) {
  const out = [];
  const tpl = idx.layout && idx.layout[kind];
  if (typeof tpl === "string") out.push("api/" + tpl.replace("{key}", key));
  if (entry && entry.path) out.push("api/graph/" + entry.path);
  out.push(`api/graph/${kind}/${key}.json`);
  return [...new Set(out)];
}

async function firstJSON(paths) {
  let lastErr = null;
  for (const p of paths) {
    try { return { doc: await getJSON(p), path: p, tried: paths }; }
    catch (e) { lastErr = e; if (!(e instanceof DataMissingError)) break; }
  }
  return { doc: null, path: null, tried: paths, error: lastErr };
}

function shardMissingPanel(kind, entry, got) {
  return el("div", { class: "panel panel-warn" },
    el("h2", { class: "panel-title", text: "이 서브그래프는 아직 구워지지 않았습니다" }),
    el("p", { text: `${entry.name || entry.key} 의 shard 파일이 번들에 없습니다. 색인에는 있는데 파일이 없다면 생성이 중간에 끊긴 것입니다.` }),
    el("p", { class: "hint" },
      el("b", { text: "생성 방법: " }),
      el("code", { text: `python ${GENERATOR} --only ${kind} --force` })),
    el("details", {}, el("summary", { text: "시도한 경로" }),
      el("pre", { class: "err", text: (got.tried || []).join("\n") })),
    got.error ? el("pre", { class: "err", text: `${got.error.name}: ${got.error.message}` }) : null);
}

/** 검색 가능한 항목 선택기(조례 300 / 법령 120). */
function entryPicker({ entries, current, groupOf, textOf, onChange, label, placeholder }) {
  const sel = el("select", { class: "sel sel-wide", "aria-label": `${label} 선택` });
  const filter = el("input", { class: "search-input", type: "search", placeholder,
                               "aria-label": `${label} 검색` });
  const count = el("span", { class: "muted small" });

  function build() {
    const q = filter.value.trim().toLowerCase();
    sel.innerHTML = "";
    const groups = new Map();
    let shown = 0;
    for (const e of entries) {
      const g = groupOf(e) || "기타";
      if (q && !`${textOf(e)} ${g} ${e.key}`.toLowerCase().includes(q) && e.key !== current) continue;
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(e);
      shown++;
    }
    if (!groups.size) sel.appendChild(el("option", { value: "", text: "검색 결과 없음", disabled: "disabled" }));
    for (const [g, list] of [...groups.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1))) {
      const og = el("optgroup", { label: `${g} (${list.length})` });
      for (const e of list) {
        const o = el("option", { value: e.key, text: textOf(e) });
        if (e.key === current) o.selected = true;
        og.appendChild(o);
      }
      sel.appendChild(og);
    }
    count.textContent = `${shown}/${entries.length}건 표시`;
  }

  filter.addEventListener("input", debounce(build, 150));
  sel.addEventListener("change", () => { if (sel.value) onChange(sel.value); });
  build();
  return el("div", { class: "toolbar" }, el("label", { text: `${label} ` }), sel, filter, count);
}

/* ==================================================================== *
 *  서브그래프 렌더러 (shard·구 번들 공용)
 * ==================================================================== */

async function subgraphPanel(host, { nodes, edges, seedId, defaults, ctx, kind }) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const hasHop = nodes.some((n) => typeof n.hop === "number");
  const maxHop = hasHop ? Math.max(...nodes.map((n) => n.hop || 0)) : 0;

  const relCounts = {};
  for (const e of edges) relCounts[e.relation] = (relCounts[e.relation] || 0) + 1;
  const enabled = new Set(Object.keys(relCounts));
  let hopLimit = maxHop;

  const controls = el("div", { class: "toolbar" });
  if (maxHop > 0) {
    const hopSel = el("select", { class: "sel", "aria-label": "확장 홉 수" });
    for (let h = 1; h <= maxHop; h++) {
      const o = el("option", { value: String(h), text: `${h}홉` });
      if (h === maxHop) o.selected = true;
      hopSel.appendChild(o);
    }
    hopSel.addEventListener("change", () => { hopLimit = parseInt(hopSel.value, 10); draw(); });
    controls.appendChild(el("label", { text: "확장 " }));
    controls.appendChild(hopSel);
  }
  host.appendChild(controls);

  const relBox = el("div", { class: "chip-row rel-filter" });
  for (const rel of Object.keys(relCounts).sort()) {
    const cb = el("input", { type: "checkbox", checked: "checked", "aria-label": rel });
    cb.addEventListener("change", () => { cb.checked ? enabled.add(rel) : enabled.delete(rel); draw(); });
    relBox.appendChild(el("label", { class: "relchip" }, cb,
      el("i", { class: "swatch", style: `background:${(REL_STYLE[rel] || {}).color || "#999"}` }),
      el("span", { text: `${(REL_STYLE[rel] || {}).label || rel} ${rel} (${num(relCounts[rel])})` })));
  }
  host.appendChild(relBox);

  const legend = el("div", { class: "legend" }, el("span", { class: "legend-title", text: "노드" }));
  for (const [lab, stl] of Object.entries(NODE_STYLE)) {
    if (!nodes.some((n) => n.label === lab)) continue;
    legend.appendChild(el("span", { class: "legend-item" },
      el("i", { class: "swatch", style: `background:${stl.color}` }),
      el("span", { text: LABEL_KO[lab] || lab })));
  }
  legend.appendChild(el("span", { class: "legend-item" },
    el("i", { class: "swatch", style: "background:#dcdcdc;border:2px solid #c0392b" }),
    el("span", { text: "폐지" })));
  legend.appendChild(el("span", { class: "legend-item" },
    el("i", { class: "swatch", style: "background:#fff;border:2px dashed #8a929c" }),
    el("span", { text: "미수집 법령(이름만 확인)" })));
  host.appendChild(legend);

  const canvas = el("div", { class: "graph-canvas" });
  host.appendChild(canvas);
  const summary = el("div", { class: "as-of" });
  host.appendChild(summary);
  const detail = el("div", { class: "graph-detail" });
  host.appendChild(detail);
  const fallback = el("div", {});
  host.appendChild(fallback);

  if (defaults) {
    host.appendChild(el("details", {}, el("summary", { text: "생략된 필드 기본값 / 판독 안내" }),
      table(["필드", "기본값·설명"], Object.entries(defaults).map(([k, v]) => [k, String(v)]))));
  }

  let visOK = true;
  try { await ensureVisNetwork(); }
  catch (e) { visOK = false; canvas.remove(); host.insertBefore(cdnFailPanel("vis-network(그래프)", e), summary); }

  // 위성·행정동 지도는 11MB GeoJSON 이라 펼칠 때만 로드한다.
  const mapCanvas = el("div", { class: "graph-map-canvas" });
  const mapInfo = el("div", { class: "graph-map-info" });
  const mapWrap = el("details", { class: "graph-spatial" },
    el("summary", { text: "위성영상·행정동 경계에 얹어 보기 (geo/adm_dong.geojson 11MB 로드)" }),
    el("div", { class: "graph-spatial-layout" }, mapCanvas, mapInfo));
  host.appendChild(mapWrap);
  let spatial = null;
  mapWrap.addEventListener("toggle", async () => {
    if (!mapWrap.open || spatial === "loading" || spatial) return;
    spatial = "loading";
    mapInfo.appendChild(loading("행정동 경계를 불러오는 중…"));
    try {
      const geo = await loadAdmDongGeo();
      await ensureLeaflet();
      mapInfo.innerHTML = "";
      spatial = initSpatialMap(mapCanvas, mapInfo, geo, detail);
      spatial.update(current());
    } catch (e) {
      spatial = null;
      mapInfo.innerHTML = "";
      mapInfo.appendChild(cdnFailPanel("Leaflet·행정동 경계", e));
    }
  });

  let network = null;
  let currentSub = { nodes, edges };
  function current() { return currentSub; }

  function draw() {
    const keep = nodes.filter((n) => !hasHop || (n.hop || 0) <= hopLimit);
    const ids = new Set(keep.map((n) => n.id));
    const subEdges = edges.filter((e) => enabled.has(e.relation) && ids.has(e.source) && ids.has(e.target));
    // 홉 축소로 고립된 노드는 seed 를 빼고 지운다
    const linked = new Set([seedId]);
    for (const e of subEdges) { linked.add(e.source); linked.add(e.target); }
    const subNodes = keep.filter((n) => linked.has(n.id));
    currentSub = { nodes: subNodes, edges: subEdges };
    if (spatial && spatial !== "loading") spatial.update(currentSub);

    summary.textContent = `표시 노드 ${num(subNodes.length)} / 전체 ${num(nodes.length)} · `
      + `엣지 ${num(subEdges.length)} / ${num(edges.length)}`;

    if (visOK) {
      const vnodes = subNodes.map((n) => visNode(n, n.id === seedId));
      const vedges = subEdges.map((e, i) => {
        const st = REL_STYLE[e.relation] || { color: "#999", width: 1 };
        return {
          id: `e${i}`, from: e.source, to: e.target,
          color: { color: st.color }, width: (st.width || 1) + (e.count > 1 ? 1 : 0),
          dashes: st.dashes || false,
          arrows: "to", label: st.label || e.relation,
          font: { size: 10, align: "middle", color: "#555" },
          title: edgeTitle(e, byId),
        };
      });
      if (network) network.destroy();
      network = new window.vis.Network(canvas, { nodes: vnodes, edges: vedges }, {
        physics: { stabilization: { iterations: 220 }, barnesHut: { gravitationalConstant: -9000, springLength: 150 } },
        interaction: { hover: true, tooltipDelay: 120 },
        layout: { improvedLayout: subNodes.length < 150 },
      });
      network.on("click", (p) => {
        if (p.nodes.length) showNode(byId.get(p.nodes[0]), detail, ctx, p.nodes[0] === seedId);
        else if (p.edges.length) {
          const idx = parseInt(String(p.edges[0]).slice(1), 10);
          showEdge(subEdges[idx], byId, detail);
        }
      });
    }

    fallback.innerHTML = "";
    fallback.appendChild(el("details", { open: visOK ? null : "open" },
      el("summary", { text: `관계 목록으로 보기 (${num(subEdges.length)}건)` }),
      table(["출발", "관계", "도착", "검증", "근거 조문"],
        subEdges.slice(0, 400).map((e) => [
          truncate(byId.get(e.source)?.name || e.source, 34),
          (REL_STYLE[e.relation] || {}).label || e.relation,
          truncate(byId.get(e.target)?.name || e.target, 34),
          VERIF_KO[e.verification_status] || e.verification_status || "—",
          e.dst_articles || e.citation_text || "—",
        ]))));
  }

  draw();
  if (seedId && byId.has(seedId)) showNode(byId.get(seedId), detail, ctx, true);
}

function visNode(n, isSeed) {
  const st = NODE_STYLE[n.label] || { color: "#95a5a6", shape: "dot" };
  const unresolved = n.resolved === false;
  const repealed = n.repealed === true || n.status === "repealed";
  const v = {
    id: n.id,
    label: truncate(n.name || n.id, 22),
    shape: st.shape,
    color: {
      background: repealed ? "#dcdcdc" : unresolved ? "#ffffff" : st.color,
      border: repealed ? "#c0392b" : unresolved ? "#8a929c" : (isSeed ? "#111" : "#555"),
    },
    borderWidth: isSeed ? 4 : repealed || unresolved ? 3 : 1,
    font: { size: isSeed ? 14 : 12, color: "#111" },
    title: nodeTitle(n),
  };
  // vis-network 는 undefined 옵션을 그대로 읽다가 죽는다. 필요할 때만 넣는다.
  if (unresolved) v.shapeProperties = { borderDashes: [4, 4] };
  return v;
}

function truncate(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function nodeTitle(n) {
  const bits = [`[${LABEL_KO[n.label] || n.label}] ${n.name}`, `id: ${n.id}`];
  if (n.org_name) bits.push(`기관: ${n.org_name}`);
  if (n.region_name) bits.push(`지역: ${n.region_name}`);
  if (n.ord_kind) bits.push(`종류: ${n.ord_kind}`);
  if (n.instrument_kind) bits.push(`법령종류: ${n.instrument_kind}`);
  if (n.tier !== undefined && n.tier !== null) bits.push(`tier: ${n.tier} (${TIER_FALLBACK[n.tier] || "?"})`);
  if (n.competent_authority) bits.push(`소관: ${n.competent_authority}`);
  if (n.enacted_on) bits.push(`제정: ${ymd(n.enacted_on)}`);
  if (n.effective_on) bits.push(`시행: ${ymd(n.effective_on)}`);
  if (typeof n.hop === "number") bits.push(`거리: ${n.hop}홉`);
  if (n.status) bits.push(`상태: ${n.status === "repealed" ? "폐지" : n.status}`);
  if (n.resolved === false) bits.push("※ 법령 원문 미수집 — 이름만 확인됨");
  return bits.join("\n");
}

function edgeTitle(e, byId) {
  const bits = [
    `${byId.get(e.source)?.name || e.source}`,
    `  └ ${(REL_STYLE[e.relation] || {}).label || e.relation} (${e.relation})`,
    `${byId.get(e.target)?.name || e.target}`,
  ];
  if (e.verification_status) bits.push(`검증: ${VERIF_KO[e.verification_status] || e.verification_status}`);
  if (e.citation_text) bits.push(`인용문: ${e.citation_text}`);
  if (e.dst_articles) bits.push(`상위법 조문: ${e.dst_articles}`);
  if (e.src_articles) bits.push(`조례 조문키: ${e.src_articles}`);
  if (e.count) bits.push(`접힌 근거: ${e.count}건`);
  if (e.resolved_by) bits.push(`연결 방식: ${e.resolved_by}(추론)`);
  return bits.join("\n");
}

function lawUrl(n) {
  const id = String(n.src_id || "");
  if (id.startsWith("ordin:")) return `https://www.law.go.kr/ordinInfoP.do?ordinSeq=${id.slice(6)}`;
  if (id.startsWith("statute:") && n.resolved !== false) return `https://www.law.go.kr/lsInfoP.do?lsiSeq=${id.slice(8)}`;
  return null;
}

function showNode(n, detail, ctx, isSeed = false) {
  if (!n) return;
  const skip = new Set(["id", "name", "label", "kind", "src_id"]);
  const rows = Object.entries(n).filter(([k]) => !skip.has(k))
    .map(([k, v]) => [k, v === null ? "—" : String(v)]);

  const actions = el("div", { class: "chip-row" });
  const url = lawUrl(n);
  if (url) {
    actions.appendChild(el("a", { class: "btn", href: url, target: "_blank",
                                  rel: "noopener noreferrer", text: "law.go.kr 원문" }));
  }
  if (ctx) {
    const key = String(n.src_id || "").replace(/:/g, "-");
    if (!isSeed && n.label === "LegalInstrument" && ctx.statKeys.has(key)) {
      actions.appendChild(el("button", { class: "btn", text: "이 법령 중심으로 보기",
                                         onclick: () => ctx.jump("statute", key) }));
    }
    if (!isSeed && n.label === "Ordinance" && ctx.ordKeys.has(key)) {
      actions.appendChild(el("button", { class: "btn", text: "이 조례 중심으로 보기",
                                         onclick: () => ctx.jump("ordinance", key) }));
    }
    if (n.label === "Region" && n.sig_cd) {
      actions.appendChild(el("a", { class: "btn", href: `#/region/${n.sig_cd}`, text: "지역 상세" }));
      actions.appendChild(el("a", { class: "btn", href: `#/lifecycle?sig=${n.sig_cd}`, text: "생애주기" }));
    }
  }

  const box = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("h3", { class: "card-title", text: n.name || n.id }),
      el("div", { class: "chip-row" },
        badge(LABEL_KO[n.label] || n.label, "badge-info"),
        n.status ? statusBadge(n.status) : null,
        n.resolved === false ? badge("미수집 법령", "badge-warn") : null,
        n.resolved_by ? badge(`이름해소(${n.resolved_by})`, "badge-est") : null,
        n.verification_status ? badge(VERIF_KO[n.verification_status] || n.verification_status, "badge-plain") : null)),
    n.note ? note(n.note, "warn") : null,
    actions.childNodes.length ? actions : null,
    table(["필드", "값"], rows));

  const old = detail.querySelector(".card");
  if (old) old.remove();
  detail.appendChild(box);
}

function showEdge(e, byId, detail) {
  if (!e) return;
  const box = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("h3", { class: "card-title", text: `${(REL_STYLE[e.relation] || {}).label || e.relation} 관계` }),
      el("div", { class: "chip-row" },
        badge(e.relation, "badge-info"),
        e.verification_status ? badge(VERIF_KO[e.verification_status] || e.verification_status,
          e.verification_status === "article-verified" ? "badge-active" : "badge-warn") : null,
        e.inferred ? badge("추론(inferred)", "badge-est") : null)),
    table(["필드", "값"], [
      ["출발", byId.get(e.source)?.name || e.source],
      ["도착", byId.get(e.target)?.name || e.target],
      ...Object.entries(e).filter(([k]) => !["source", "target", "relation"].includes(k))
        .map(([k, v]) => [k, v === null ? "—" : String(v)]),
    ]));
  const old = detail.querySelector(".card");
  if (old) old.remove();
  detail.appendChild(box);
}

/** 법령 중심 뷰에서만 — api/statute/{key}.json 의 조문 목록을 붙인다(있을 때만). */
async function articlesPanel(host, entry) {
  let env;
  try { env = await getJSON(`api/statute/${entry.key}.json`); }
  catch (e) { return; }
  const d = env.data || env;
  const arts = d.articles || [];
  const det = el("details", { class: "statute-articles" },
    el("summary", { text: `상위법 조문 열람 (${num(d.article_count || arts.length)}조 중 ${num(d.articles_shown || arts.length)}조)` }));
  if (!arts.length) {
    det.appendChild(note("이 법령은 조문(articles)이 수집되지 않았다.", "warn"));
  } else {
    if (d.articles_truncated_bodies) {
      det.appendChild(note(`본문은 미리보기다(${num(d.articles_truncated_bodies)}조 절단). 전문은 law.go.kr 원문으로 확인할 것.`, "warn"));
    }
    for (const a of arts.slice(0, 60)) {
      det.appendChild(el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h3", { class: "card-title", text: `제${a.article_no}조${a.article_branch ? `의${a.article_branch}` : ""} ${a.title || ""}` }),
          a.effective_on ? badge(`시행 ${ymd(a.effective_on)}`, "badge-plain") : null),
        el("p", { class: "article-text", text: (a.body || "").slice(0, 400) + (a.body_truncated ? " …" : "") })));
    }
  }
  if (d.caveat) det.appendChild(note(d.caveat, "warn"));
  host.appendChild(det);
}

/* ==================================================================== *
 *  구 정적 번들 대체 경로 (api/graph/index.json 이 없을 때)
 * ==================================================================== */

async function renderLegacy(host) {
  const stats = await loadGraphStats().catch(() => null);
  const sec = section("구 정적 번들 서브그래프", asOfLine("graph/nodes.json + graph/edges.json 의 ego 그래프"));
  host.appendChild(sec);

  if (stats) {
    sec.appendChild(el("div", { class: "chip-row" },
      badge(`노드 ${num(stats.total_nodes ?? sum(stats.node_counts))}`, "badge-info"),
      badge(`엣지 ${num(stats.total_edges ?? sum(stats.edge_counts))}`, "badge-info"),
      badge(`backend ${stats.backend || "?"}`, "badge-plain")));
    const skipped = stats.skipped_edges || {};
    const skipTotal = Object.values(skipped).reduce((a, b) => a + (b || 0), 0);
    if (skipTotal > 0) {
      sec.appendChild(note("정적 번들에서 제외된 엣지가 있다: "
        + Object.entries(skipped).filter(([, v]) => v).map(([k, v]) => `${k} ${num(v)}건`).join(", ")
        + ". 해당 관계는 이 화면에서 그릴 수 없다.", "warn"));
    }
  }

  let graph;
  try { graph = await loadGraph(); }
  catch (e) { sec.appendChild(errorPanel(e, "graph/nodes.json 또는 graph/edges.json 로드 실패")); return; }

  if (graph.sample) {
    sec.appendChild(note(
      `실데이터 그래프(노드 ${num(graph.realNodeCount)} · 엣지 ${num(graph.realEdgeCount)})는 브라우저로 통째로 받을 수 없어 `
      + "가상데이터 표본을 대신 그린다. 실데이터 위계는 위 shard 를 구워야 볼 수 있다.", "warn"));
  }

  const { nodes, edges } = graph;
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const seedIds = [...new Set(edges.filter((e) => e.relation === "DELEGATED_FROM").map((e) => e.source))]
    .filter((id) => byId.has(id));
  if (!seedIds.length) {
    sec.appendChild(note("DELEGATED_FROM 엣지가 없어 위임 그래프를 그릴 수 없습니다.", "warn"));
    return;
  }

  const entries = seedIds.slice(0, 500).map((id) => ({ key: id, name: byId.get(id).name || id }));
  const body = el("div", {});
  const picker = entryPicker({
    entries, current: entries[0].key, label: "기준 조례", placeholder: "조례명 검색",
    groupOf: () => "위임 관계가 있는 조례", textOf: (e) => e.name,
    onChange: (k) => draw(k),
  });
  sec.appendChild(picker);
  sec.appendChild(body);

  async function draw(seed) {
    body.innerHTML = "";
    const sub = ego(seed, 2, edges, byId, LIMITS.graphRenderNodes);
    await subgraphPanel(body, { nodes: sub.nodes, edges: sub.edges, seedId: seed, ctx: null, kind: "legacy" });
    if (sub.truncated) body.appendChild(note(`노드 상한 ${LIMITS.graphRenderNodes} 에 걸려 잘렸다.`, "warn"));
  }
  await draw(entries[0].key);
}

function sum(obj) { return Object.values(obj || {}).reduce((a, b) => a + (b || 0), 0); }

/** seed 에서 hops 만큼 확장한 무방향 ego 그래프 (구 번들 전용) */
function ego(seed, hops, edges, byId, maxNodes) {
  const adj = new Map();
  for (const e of edges) {
    if (!adj.has(e.source)) adj.set(e.source, []);
    if (!adj.has(e.target)) adj.set(e.target, []);
    adj.get(e.source).push(e);
    adj.get(e.target).push(e);
  }
  const seen = new Set([seed]);
  const hopOf = new Map([[seed, 0]]);
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
          hopOf.set(other, h + 1);
          next.push(other);
        }
        if (truncated) break;
      }
      if (truncated) break;
    }
    frontier = next;
  }
  const nodes = [...seen].map((id) => byId.get(id)).filter(Boolean)
    .map((n) => ({ ...n, hop: hopOf.get(n.id) ?? 0 }));
  const nodeSet = new Set(nodes.map((n) => n.id));
  return { nodes, edges: edges.filter((e) => nodeSet.has(e.source) && nodeSet.has(e.target)), truncated };
}

/* ==================================================================== *
 *  위성·행정동 지도 (서브그래프를 지리 위에 얹는다)
 * ==================================================================== */

function initSpatialMap(mapCanvas, mapInfo, admGeo, detailHost) {
  const L = window.L;
  const map = L.map(mapCanvas, { preferCanvas: true, attributionControl: false });
  L.tileLayer(SATELLITE_TILE.url, { maxZoom: 19, attribution: SATELLITE_TILE.attribution }).addTo(map);
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
        return `<b>${p.adm_nm || p.adm_cd}</b><br>행정동 ${p.adm_cd || ""}<br>`
          + `시군구 ${p.sig_name || p.sig_cd || "미매칭"}`
          + (matched ? `<br><b>그래프 매칭: ${matched}</b>` : "");
      }, { sticky: true });
    },
  }).addTo(map);

  const allBounds = admLayer.getBounds();
  if (allBounds.isValid()) map.fitBounds(allBounds.pad(0.02));

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
      L.circleMarker(b.getCenter(), { radius: 7, color: "#ffffff", weight: 2,
                                      fillColor: "#ffcf33", fillOpacity: 0.95 })
        .bindTooltip(`${hit.label}<br>${dongsBySig.get(sig)}개 행정동`).addTo(markerLayer);
    }
    drawGraphOnMap(sub, centersBySig, graphEdgeLayer, graphNodeLayer, detailHost);

    mapInfo.innerHTML = "";
    mapInfo.appendChild(el("h3", { text: "위성·행정동 매칭" }));
    if (!matched.size) {
      mapInfo.appendChild(note("현재 서브그래프에서 지역 코드를 찾지 못했습니다.", "warn"));
      return;
    }
    mapInfo.appendChild(el("div", { class: "as-of",
      text: `그래프 지역 ${num(matched.size)}개 · 경계 매칭 ${num(boundsBySig.size)}개 시군구` }));
    mapInfo.appendChild(table(["지역", "sig_cd", "그래프", "경계"],
      [...matched.entries()].slice(0, 12).map(([sig, hit]) => [
        hit.label, sig, `${num(hit.weight)}개 노드`, `${num(dongsBySig.get(sig) || 0)}개 동`])));

    if (activeBounds.length) {
      const groupBounds = activeBounds.reduce((acc, b) => acc.extend(b), activeBounds[0]);
      if (groupBounds.isValid()) map.fitBounds(groupBounds.pad(0.18), { maxZoom: 13 });
    }
  }

  return { update };
}

function baseAdmStyle() {
  return { color: "#ffffff", weight: 0.45, opacity: 0.38, fillColor: "#1f2937", fillOpacity: 0.04 };
}

function matchedAdmStyle(weight) {
  return { color: "#ffcf33", weight: Math.min(3, 1 + Math.log2(1 + weight)), opacity: 1,
           fillColor: "#ffcf33", fillOpacity: 0.28 };
}

function drawGraphOnMap(sub, centersBySig, edgeLayer, nodeLayer, detailHost) {
  const L = window.L;
  const byId = new Map(sub.nodes.map((n) => [n.id, n]));
  const positions = projectGraphNodes(sub, centersBySig);

  for (const e of sub.edges) {
    const a = positions.get(e.source);
    const b = positions.get(e.target);
    if (!a || !b) continue;
    const st = REL_STYLE[e.relation] || { color: "#ffffff", width: 1 };
    L.polyline([a, b], { color: st.color || "#ffffff", weight: Math.max(1.5, st.width || 1),
                         opacity: 0.72, dashArray: st.dashes ? "5,5" : null })
      .bindTooltip(edgeTitle(e, byId).replace(/\n/g, "<br>"), { sticky: true })
      .addTo(edgeLayer);
  }

  for (const n of sub.nodes) {
    const pos = positions.get(n.id);
    if (!pos) continue;
    const st = NODE_STYLE[n.label] || { color: "#95a5a6" };
    const radius = n.label === "Region" ? 7 : n.label === "LegalInstrument" ? 6 : 4.5;
    const repealed = n.repealed === true || n.status === "repealed";
    L.circleMarker(pos, { radius, color: "#ffffff", weight: repealed ? 3 : 1.5,
                          fillColor: repealed ? "#d9d9d9" : n.resolved === false ? "#ffffff" : st.color,
                          fillOpacity: 0.95, opacity: 1 })
      .bindTooltip(nodeTitle(n).replace(/\n/g, "<br>"), { sticky: true })
      .on("click", () => showNode(n, detailHost, null))
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
  return { lat: points.reduce((a, p) => a + p.lat, 0) / n, lng: points.reduce((a, p) => a + p.lng, 0) / n };
}

function jitterLatLng(center, key, radiusDeg) {
  if (!radiusDeg) return center;
  const h = hashString(key);
  const angle = (h % 6283) / 1000;
  const scale = 0.35 + ((h >>> 8) % 100) / 100;
  return { lat: center.lat + Math.sin(angle) * radiusDeg * scale,
           lng: center.lng + Math.cos(angle) * radiusDeg * scale };
}

function hashString(s) {
  let h = 2166136261;
  for (const ch of String(s || "")) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

function collectMatchedRegions(nodes) {
  const out = new Map();
  for (const n of nodes) {
    const sig = graphNodeSig(n);
    if (!sig || sig.endsWith("000")) continue;
    const prev = out.get(sig);
    if (!prev) out.set(sig, { label: regionLabel(sig, nodes) || sig, weight: 1 });
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
  if (region) return region.full_name || region.name || sig;
  const owned = nodes.find((n) => String(n.region_id || "") === sig);
  if (owned?.org_name) return owned.org_name;
  if (owned?.region_name) return owned.region_name;
  return sig;
}
