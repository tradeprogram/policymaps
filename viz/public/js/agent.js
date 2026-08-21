// AI 정책분석관 — 화면 컨텍스트를 읽고 Gemini 프록시(/ai/chat)에 질의한다.
import { el, num, pct, won, ymd } from "./util.js";
import { go, currentPath } from "./router.js";
import { state, loadManifest, loadRegionIndex, loadRegion, loadFixture } from "./api.js";

const QUICK_PROMPTS = [
  "구미시 맨발걷기 조례 도입 가능성을 분석해줘",
  "유사 지자체가 가진 정책 중 우리에게 없는 후보를 요약해줘",
  "폐지 사례가 있는 위험 후보만 찾아줘",
  "예산 연결 근거가 있는 정책을 발표용으로 설명해줘",
];

const ACTIONS = [
  { id: "gap", label: "격차분석", path: "/gap", keywords: ["격차", "없는", "후보", "추천", "유사", "맨발"] },
  { id: "effectiveness", label: "실효성", path: "/effectiveness", keywords: ["예산", "실효", "집행", "confidence", "추정"] },
  { id: "diffusion", label: "정책 확산", path: "/diffusion", keywords: ["확산", "채택", "추세", "s곡선", "전파"] },
  { id: "graph", label: "법령 위계", path: "/graph", keywords: ["법령", "위계", "위임", "그래프", "관계"] },
  { id: "search", label: "검색", path: "/search", keywords: ["검색", "조문", "원문", "근거"] },
  { id: "region", label: "지역 상세", path: "/region/47190", keywords: ["지역", "구미", "지자체", "현황"] },
];

let messages = [];
let panelBody;
let input;
let sendBtn;
let statusLine;

export function initAgent() {
  const launcher = el("button", { class: "ai-launcher", type: "button", title: "AI 정책분석관", text: "AI" });
  const panel = el("aside", { class: "ai-panel", "aria-label": "AI 정책분석관" },
    el("div", { class: "ai-head" },
      el("div", {},
        el("strong", { text: "AI 정책분석관" }),
        el("span", { class: "ai-sub", text: "화면을 읽고 다음 분석으로 이동합니다" })),
      el("button", { class: "ai-close", type: "button", title: "닫기", text: "×" })),
    panelBody = el("div", { class: "ai-body" }),
    el("form", { class: "ai-form" },
      input = el("textarea", { class: "ai-input", rows: "2", placeholder: "예: 구미시 맨발걷기 조례 도입 근거를 정리해줘" }),
      el("div", { class: "ai-form-row" },
        statusLine = el("span", { class: "ai-status", text: "Gemini 프록시 연결 시 AI 답변, 미연결 시 로컬 요약" }),
        sendBtn = el("button", { class: "btn ai-send", type: "submit", text: "질문" }))));

  document.body.appendChild(launcher);
  document.body.appendChild(panel);

  launcher.addEventListener("click", () => panel.classList.add("open"));
  panel.querySelector(".ai-close").addEventListener("click", () => panel.classList.remove("open"));
  panel.querySelector("form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    await ask(q);
  });

  addAssistant(
    "무엇을 볼까요? 저는 현재 화면과 사전계산 데이터를 읽고, 필요한 화면으로 이동하면서 근거를 정리합니다.",
    QUICK_PROMPTS.map((q) => ({ label: q, prompt: q })));
}

async function ask(text) {
  addUser(text);
  setBusy(true);
  const context = await buildContext(text);
  const actions = suggestActions(text, context);
  try {
    const res = await fetch("/ai/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        route: currentPath(),
        context,
        history: messages.slice(-8).map((m) => ({ role: m.role, text: m.text })),
      }),
    });
    if (!res.ok) throw new Error(`AI 서버 HTTP ${res.status}`);
    const data = await res.json();
    if (data.error) throw new Error(data.detail || data.error);
    addAssistant(data.answer || localAnswer(text, context), actions);
    statusLine.textContent = data.model ? `Gemini: ${data.model}` : "AI 답변 완료";
  } catch (e) {
    addAssistant(localAnswer(text, context), actions);
    statusLine.textContent = `Gemini 오류 · 로컬 요약 (${shortError(e.message)})`;
  } finally {
    setBusy(false);
  }
}

function shortError(message) {
  const s = String(message || "연결 실패").replace(/\s+/g, " ").trim();
  return s.length > 56 ? `${s.slice(0, 56)}...` : s;
}

async function buildContext(question) {
  await loadManifest().catch(() => null);
  const ctx = {
    route: currentPath(),
    as_of_date: state.asOfDate,
    data_mode: state.isMock ? "mock" : "real",
    rules: [
      "조례-예산은 verified=1만 확인됨, 나머지는 추정 연결로 표현",
      "폐지 조례는 선례로 추천 금지",
      "수치에는 as_of_date 기준을 함께 언급",
    ],
  };
  const sig = preferredRegion(question, ctx.route);
  const [idx, region, gap, peers, diffusion, effectiveness, search] = await Promise.all([
    loadRegionIndex().catch(() => null),
    sig ? loadRegion(sig).catch(() => null) : null,
    loadFixture("gap").catch(() => null),
    loadFixture("peers").catch(() => null),
    loadFixture("diffusion").catch(() => null),
    loadFixture("effectiveness").catch(() => null),
    loadFixture("search").catch(() => null),
  ]);
  ctx.region_index_count = (idx?.items || idx?.regions || []).length || null;
  ctx.region = summarizeRegion(region);
  ctx.gap = summarizeGap(gap, sig);
  ctx.peers = summarizePeers(peers, sig);
  ctx.diffusion = summarizeDiffusion(diffusion);
  ctx.effectiveness = summarizeEffectiveness(effectiveness);
  ctx.search = summarizeSearch(search);
  return ctx;
}

function preferredRegion(question, path) {
  if (/구미|47190/.test(question)) return "47190";
  const m = /^\/region\/(\d+)/.exec(path);
  return m ? m[1] : "47190";
}

function pickFixtureData(env, sig) {
  const data = env?.data || null;
  if (!data) return null;
  if (data.target || data.recommendations || data.peers) return data;
  return data[sig] || data[Object.keys(data)[0]];
}

function summarizeRegion(doc) {
  const d = doc?.data || doc;
  if (!d) return null;
  const counts = d.counts || {};
  const budget = d.budget || d.budget_summary || {};
  return {
    name: d.name || d.region_name,
    sig_cd: d.sig_cd || d.region_id,
    level: d.level,
    ordinance_count: counts.ordinances ?? counts.ordinance ?? d.ordinance_count,
    population: d.population ?? d.indicators?.population,
    budget_now: budget.budget_now ?? budget.total_budget_now,
    as_of_date: doc.as_of_date || d.as_of_date,
  };
}

function summarizePeers(env, sig) {
  const d = pickFixtureData(env, sig);
  if (!d) return null;
  return {
    target: d.target?.name,
    k: d.k ?? d.peers?.length,
    peers: (d.peers || []).slice(0, 5).map((p) => p.name || p.region_id),
    as_of_date: env.as_of_date,
  };
}

function summarizeGap(env, sig) {
  const d = pickFixtureData(env, sig);
  if (!d) return null;
  const recs = (d.recommendations || []).slice(0, 5).map((r) => ({
    policy: r.policy_key,
    peer_count: r.peer_count,
    repealed_peer_count: r.repealed_peer_count || 0,
    peer_share: r.peer_share,
    active_examples: (r.peers || []).slice(0, 3).map((p) => `${p.name} ${p.ordinance_name || ""}`.trim()),
  }));
  return {
    target: d.target?.name,
    peer_pool_size: d.peer_pool_size,
    my_policy_count: d.my_policy_count,
    recommendations: recs,
    as_of_date: env.as_of_date,
  };
}

function summarizeDiffusion(env) {
  const d = env?.data;
  if (!d) return null;
  const last = (d.curve || [])[d.curve.length - 1] || {};
  return {
    template: d.template,
    universe: d.universe,
    adopters: d.adopters,
    final_adoption_rate: d.final_adoption_rate,
    latest_year: last.year,
    latest_cumulative: last.cumulative,
    rogers_stage: d.rogers?.stage || d.stage,
    as_of_date: env.as_of_date,
  };
}

function summarizeEffectiveness(env) {
  const d = env?.data;
  if (!d) return null;
  const t = d.totals || {};
  const v = d.verification || {};
  return {
    link_count: d.link_count,
    budget_lines: d.budget_lines,
    verified_links: v.verified_links,
    auto_links: v.auto_links,
    precision_sample: "표본 584건 전체 64.9%, confidence>=0.8 구간 93.2%",
    budget_now: t.budget_now,
    exe_amt: t.exe_amt,
    exec_rate_vs_now: t.exec_rate_vs_now,
    top_ordinances: (d.by_ordinance || []).slice(0, 4).map((o) => ({
      name: o.name,
      status: o.status,
      lines: o.lines,
      confidence_hint: o.verification_status,
      budget_now: o.budget_now,
      exec_rate: o.budget_now ? o.exe_amt / o.budget_now : o.exec_rate_vs_now,
    })),
    as_of_date: env.as_of_date,
  };
}

function summarizeSearch(env) {
  const d = env?.data;
  if (!d) return null;
  return {
    query: d.query,
    count: d.count,
    results: (d.results || []).slice(0, 5).map((r) => ({
      name: r.parent_name,
      org: r.org_name,
      status: r.status,
      verified: r.verified,
      article: `${r.article_no || ""} ${r.article_title || ""}`.trim(),
    })),
    as_of_date: env.as_of_date,
  };
}

function localAnswer(question, ctx) {
  const parts = [];
  const target = ctx.gap?.target || ctx.region?.name || "선택 지자체";
  parts.push(`${target} 기준으로 볼 때, 우선 격차분석에서 후보를 좁히고 폐지 사례를 걸러낸 뒤 실효성 화면에서 예산 연결을 확인하는 흐름이 좋습니다.`);
  if (ctx.gap?.recommendations?.length) {
    const top = ctx.gap.recommendations[0];
    parts.push(`상위 후보는 「${top.policy}」이며 유사 지자체 ${num(top.peer_count)}곳이 보유, 폐지 사례는 ${num(top.repealed_peer_count)}곳입니다.`);
  }
  if (ctx.diffusion?.template) {
    parts.push(`확산 화면의 「${ctx.diffusion.template}」 기준 최종 채택률은 ${pct(ctx.diffusion.final_adoption_rate)}입니다.`);
  }
  if (ctx.effectiveness) {
    parts.push(`예산 연결은 ${num(ctx.effectiveness.link_count)}건이지만 verified=1만 확인됨이고 나머지는 추정 연결로 말해야 합니다.`);
  }
  parts.push(`기준일은 ${ctx.as_of_date || "화면 표시 기준일"}입니다.`);
  return parts.join("\n\n");
}

function suggestActions(question, ctx) {
  const q = question.toLowerCase();
  const found = ACTIONS.filter((a) => a.keywords.some((k) => q.includes(k.toLowerCase()))).slice(0, 4);
  const base = found.length ? found : [ACTIONS[0], ACTIONS[1], ACTIONS[2]];
  if (ctx.region?.sig_cd && base.some((a) => a.id === "region")) {
    return base.map((a) => a.id === "region" ? { ...a, path: `/region/${ctx.region.sig_cd}` } : a);
  }
  return base;
}

function addUser(text) {
  messages.push({ role: "user", text });
  panelBody.appendChild(el("div", { class: "ai-msg ai-user", text }));
  panelBody.scrollTop = panelBody.scrollHeight;
}

function addAssistant(text, actions = []) {
  messages.push({ role: "assistant", text });
  const box = el("div", { class: "ai-msg ai-assistant" },
    ...String(text).split("\n").filter(Boolean).map((p) => el("p", { text: p })));
  if (actions.length) {
    box.appendChild(el("div", { class: "ai-actions" },
      ...actions.map((a) => el("button", {
        class: "ai-action",
        type: "button",
        text: a.label,
        onclick: () => a.prompt ? ask(a.prompt) : go(a.path),
      }))));
  }
  panelBody.appendChild(box);
  panelBody.scrollTop = panelBody.scrollHeight;
}

function setBusy(busy) {
  sendBtn.disabled = busy;
  input.disabled = busy;
  statusLine.textContent = busy ? "분석 중..." : statusLine.textContent;
}
