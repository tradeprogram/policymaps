// 자치법규 정책지도.agent — 화면 컨텍스트와 서버 도구 결과를 결합한다.
import { el, num, pct, won, ymd } from "./util.js";
import { go, currentPath } from "./router.js";
import { state, loadManifest, loadRegionIndex, loadRegion, loadFixture } from "./api.js";

const QUICK_PROMPTS = [
  { label: "구미시 맨발걷기 분석", prompt: "구미시 맨발걷기 조례 도입 가능성을 분석해줘" },
  { label: "없는 정책 후보", prompt: "유사 지자체가 가진 정책 중 우리에게 없는 후보를 요약해줘" },
  { label: "폐지 위험 확인", prompt: "폐지 사례가 있는 위험 후보만 찾아줘" },
  { label: "예산 근거 정리", prompt: "예산 연결 근거가 있는 정책을 발표용으로 설명해줘" },
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
let lastHandoff = null;
let panelEl;

export function initAgent() {
  const launcher = el("button", { class: "ai-launcher", type: "button", title: "자치법규 정책지도.agent", text: "AI" });
  const panel = el("aside", { class: "ai-panel", "aria-label": "자치법규 정책지도.agent" },
    el("div", { class: "ai-head" },
      el("div", {},
        el("strong", { text: "자치법규 정책지도.agent" }),
        el("span", { class: "ai-sub", text: "정책도구를 실행하고 근거를 묶습니다" })),
      el("button", { class: "ai-close", type: "button", title: "닫기", text: "×" })),
    panelBody = el("div", { class: "ai-body" }),
    el("form", { class: "ai-form" },
      input = el("textarea", { class: "ai-input", rows: "2", placeholder: "예: 구미시 맨발걷기 조례 도입 근거를 정리해줘" }),
      el("div", { class: "ai-form-row" },
        statusLine = el("span", { class: "ai-status", text: "Gemini 연결 시 에이전트 답변, 실패 시 로컬 요약" }),
        sendBtn = el("button", { class: "btn ai-send", type: "submit", text: "질문" }))));

  document.body.appendChild(launcher);
  document.body.appendChild(panel);
  panelEl = panel;

  launcher.addEventListener("click", () => panel.classList.add("open"));
  panel.querySelector(".ai-close").addEventListener("click", () => panel.classList.remove("open"));
  window.addEventListener("agent:panel-question", (ev) => {
    const detail = ev.detail || {};
    const title = detail.title || "현재 패널";
    panel.classList.add("open");
    ask("이 패널을 설명해줘", {
      title,
      route: detail.route,
      text: detail.text,
    });
  });
  const form = panel.querySelector("form");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    await ask(q);
  });
  // Enter = 전송, Shift+Enter = 줄바꿈
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      form.requestSubmit();
    }
  });

  addAssistant(
    "무엇을 볼까요? 저는 현재 화면과 정책도구 결과를 읽고, 필요한 화면으로 이동하면서 근거를 정리합니다.",
    getQuickPrompts());
}

async function ask(text, panelContext = null) {
  const visibleText = panelContext ? "이 패널을 설명해줘" : text;
  addUser(visibleText);
  setBusy(true);
  const context = await buildContext(text, panelContext);
  const fallbackActions = suggestActions(text, context);
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
    if (data.answer) {
      if (data.handoff) lastHandoff = data.handoff;
      addAssistant(
        data.answer,
        normalizeActions(data.actions) || fallbackActions,
        { evidence: data.evidence, toolTrace: data.tool_trace, handoff: data.handoff });
      statusLine.textContent = data.error
        ? `Gemini 오류 · 서버 요약 (${shortError(data.detail || data.error)})`
        : (data.model ? `Gemini: ${data.model}` : "AI 응답 완료");
      return;
    }
    if (data.error) throw new Error(data.detail || data.error);
    if (data.handoff) lastHandoff = data.handoff;
    addAssistant(
      data.answer || localAnswer(text, context),
      normalizeActions(data.actions) || fallbackActions,
      { evidence: data.evidence, toolTrace: data.tool_trace, handoff: data.handoff });
    statusLine.textContent = data.model ? `Gemini: ${data.model}` : "AI 답변 완료";
  } catch (e) {
    addAssistant(localAnswer(text, context), fallbackActions);
    statusLine.textContent = `Gemini 오류 · 로컬 요약 (${shortError(e.message)})`;
  } finally {
    setBusy(false);
  }
}

function shortError(message) {
  const s = String(message || "연결 실패").replace(/\s+/g, " ").trim();
  return s.length > 56 ? `${s.slice(0, 56)}...` : s;
}

async function buildContext(question, panelContext = null) {
  await loadManifest().catch(() => null);
  const ctx = {
    route: currentPath(),
    as_of_date: state.asOfDate,
    data_mode: state.isMock ? "mock" : "real",
    agent_memory: lastHandoff,
    panel_context: panelContext,
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

function normalizeActions(actions) {
  if (!Array.isArray(actions) || !actions.length) return null;
  return actions
    .filter((a) => a && (a.path || a.prompt) && a.label)
    .slice(0, 5)
    .map((a) => ({
      label: a.label,
      path: a.path,
      prompt: a.prompt,
      kind: a.kind,
      primary: a.primary === true,
      description: a.description,
    }));
}

function addUser(text) {
  messages.push({ role: "user", text });
  panelBody.appendChild(el("div", { class: "ai-msg ai-user", text }));
  panelBody.scrollTop = panelBody.scrollHeight;
}

function addAssistant(text, actions = [], meta = {}) {
  messages.push({ role: "assistant", text });
  const box = el("div", { class: "ai-msg ai-assistant" });
  panelBody.appendChild(box);
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) {
    renderParagraphs(box, text);
    appendMeta(box, meta);
    appendActions(box, actions);
    panelBody.scrollTop = panelBody.scrollHeight;
    return;
  }
  typeInto(box, String(text), () => {
    appendMeta(box, meta);
    appendActions(box, actions);
  });
}

function renderParagraphs(box, text) {
  for (const p of String(text).split(/\n+/).filter(Boolean)) box.appendChild(el("p", { text: p }));
}

function appendActions(box, actions) {
  if (!actions || !actions.length) return;
  box.appendChild(el("div", { class: "ai-actions" },
    ...actions.map((a) => el("button", {
      class: "ai-action",
      "data-primary": a.primary ? "true" : null,
      title: a.description || a.label,
      type: "button",
      text: a.label,
      onclick: () => (a.prompt ? ask(a.prompt) : go(a.path)),
    }))));
  panelBody.scrollTop = panelBody.scrollHeight;
}

function appendMeta(box, meta = {}) {
  const evidence = Array.isArray(meta.evidence) ? meta.evidence : [];
  const trace = Array.isArray(meta.toolTrace) ? meta.toolTrace : [];
  const handoff = meta.handoff && typeof meta.handoff === "object" ? meta.handoff : null;
  if (!evidence.length && !trace.length && !handoff) return;
  const chips = [];
  if (handoff?.intent) chips.push(el("span", { class: "ai-meta-chip ai-meta-intent", text: `intent · ${handoff.intent}` }));
  if (handoff?.policy_keyword) chips.push(el("span", { class: "ai-meta-chip", text: `정책 · ${handoff.policy_keyword}` }));
  for (const ev of evidence.slice(0, 3)) {
    chips.push(el("span", { class: "ai-meta-chip", text: `${ev.kind || "근거"} · ${ev.title || ""}` }));
  }
  if (trace.length) {
    const ok = trace.filter((t) => t.status === "ok").length;
    chips.push(el("span", { class: "ai-meta-chip", text: `도구 ${ok}/${trace.length}` }));
  }
  box.appendChild(el("div", { class: "ai-meta" }, chips));
  appendEvidenceCards(box, evidence, handoff);
  if (handoff?.steps?.length) {
    box.appendChild(el("div", { class: "ai-plan" },
      ...handoff.steps.slice(0, 5).map((s) => el("span", {
        class: `ai-plan-step ${s.status === "ok" ? "ok" : "miss"}`,
        text: `${s.status === "ok" ? "✓" : "!"} ${s.label}`,
      }))));
  }
  appendSuggestedQuestions(box, handoff?.suggested_questions);
}

function appendSuggestedQuestions(box, questions) {
  const items = Array.isArray(questions) && questions.length
    ? questions
    : getQuickPrompts().map((q) => q.prompt);
  if (!items.length) return;
  box.appendChild(el("div", { class: "ai-suggest" },
    el("span", { class: "ai-suggest-title", text: "다음 질문" }),
    ...items.slice(0, 3).map((q) => el("button", {
      class: "ai-suggest-btn",
      type: "button",
      text: q,
      onclick: () => ask(q),
    }))));
}

function getQuickPrompts() {
  const route = currentPath();
  const policy = lastHandoff?.policy_keyword || "맨발걷기";
  if (route.startsWith("/gap")) {
    return [
      { label: "상위 후보 판정", prompt: "이 후보들 중 발표에서 가장 설득력 있는 정책을 골라줘" },
      { label: "폐지 위험", prompt: `${policy} 폐지 사례와 위험 신호를 확인해줘` },
      { label: "예산 확인", prompt: `${policy} 예산 연결까지 이어서 확인해줘` },
    ];
  }
  if (route.startsWith("/diffusion")) {
    return [
      { label: "도입률 해석", prompt: `${policy} 확산 단계와 도입률을 발표용으로 해석해줘` },
      { label: "격차 근거", prompt: `${policy} 격차분석 근거와 연결해서 설명해줘` },
      { label: "실효성 확인", prompt: `${policy} 예산 근거까지 이어서 확인해줘` },
    ];
  }
  if (route.startsWith("/effectiveness")) {
    return [
      { label: "예산 신뢰도", prompt: `${policy} 예산 연결의 확인됨과 추정 연결을 구분해줘` },
      { label: "발표 문장", prompt: `${policy} 조례 도입 근거를 발표 문장으로 정리해줘` },
      { label: "주의점", prompt: "심사에서 오해받을 수 있는 표현을 걸러줘" },
    ];
  }
  return QUICK_PROMPTS;
}

function appendEvidenceCards(box, evidence, handoff) {
  const cards = [];
  for (const ev of evidence.slice(0, 3)) {
    cards.push(el("div", { class: "ai-evidence-card" },
      el("span", { class: "ai-evidence-kind", text: ev.kind || "근거" }),
      el("strong", { text: ev.title || "근거 데이터" }),
      ev.summary ? el("span", { class: "ai-evidence-summary", text: ev.summary }) : null,
      ev.as_of_date ? el("span", { class: "ai-evidence-date", text: `기준 ${String(ev.as_of_date).slice(0, 10)}` }) : null
    ));
  }
  if (handoff?.next) {
    cards.push(el("button", {
      class: "ai-evidence-card ai-next-card",
      type: "button",
      title: handoff.next.description || handoff.next.label,
      onclick: () => go(handoff.next.path),
    },
      el("span", { class: "ai-evidence-kind", text: "다음 실행" }),
      el("strong", { text: handoff.next.label }),
      handoff.next.description ? el("span", { class: "ai-evidence-date", text: handoff.next.description }) : null
    ));
  }
  if (cards.length) box.appendChild(el("div", { class: "ai-evidence-grid" }, cards));
}

// 답변을 단어 단위로 빠르게 흘려 쓴다(스트리밍 느낌). 문단(\n)은 <p> 로 유지한다.
function typeInto(box, text, done) {
  const paragraphs = String(text).split(/\n+/).filter(Boolean);
  const cursor = el("span", { class: "ai-cursor" });
  let pi = 0;
  const nextPara = () => {
    if (pi >= paragraphs.length) { cursor.remove(); if (done) done(); return; }
    const p = el("p", {});
    box.appendChild(p);
    p.appendChild(cursor);
    const tokens = paragraphs[pi].match(/\S+\s*/g) || [paragraphs[pi]];
    // 긴 답변은 한 틱에 여러 단어를 흘려 전체 시간이 늘어지지 않게 한다(빠른 스트리밍 느낌).
    const burst = tokens.length > 80 ? 3 : tokens.length > 40 ? 2 : 1;
    let ti = 0;
    const step = () => {
      if (ti >= tokens.length) { pi += 1; nextPara(); return; }
      for (let b = 0; b < burst && ti < tokens.length; b += 1) {
        cursor.insertAdjacentText("beforebegin", tokens[ti]);
        ti += 1;
      }
      panelBody.scrollTop = panelBody.scrollHeight;
      setTimeout(step, 12 + Math.random() * 16);
    };
    step();
  };
  nextPara();
}

function setBusy(busy) {
  sendBtn.disabled = busy;
  input.disabled = busy;
  statusLine.textContent = busy ? "분석 중..." : statusLine.textContent;
}
