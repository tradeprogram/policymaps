// 12. 확산 위험모형 · 커뮤니티 · 유사방법비교
//     api/analytics.json 카탈로그 + api/eha/{slug}.json · api/community/summary.json
//     · api/peer_methods/{sig_cd}.json (make_analytics_fixtures.py 산출)
//
//  이 화면이 있는 이유 — 분석 엔진 세 개(analytics.eha / rag.community / analytics.peers)가
//  계산은 하는데 어느 화면에도 붙어 있지 않았다. 수치를 그대로 공시한다.
//  ★ 표기 규율: 인과 아님 · 이웃효과 미지지 결론을 숨기지 않음 · 원문 링크 · as_of_date.
import { el, num, won, pct, ymd, extLink } from "../util.js";
import { getJSON, DataMissingError, loadRegionCatalog, categoryName } from "../api.js";
import { section, table, note, loading, asOfLine, errorPanel, badge,
         statCard, envelopeFooter } from "../components.js";
import { regionSelector } from "../nationwide.js";
import { go } from "../router.js";

const CATALOG = "api/analytics.json";
const GUIDE = "api/peer_methods/_guide.json";

const TABS = [
  ["eha", "확산 위험모형(EHA)"],
  ["community", "커뮤니티 탐지"],
  ["peers", "유사 지자체 방법비교"],
];

const GEN_CMD = "python system/make_analytics_fixtures.py";

/* ------------------------------------------------------------------ */
/*  진입                                                               */
/* ------------------------------------------------------------------ */
export async function render(root, params, query) {
  root.appendChild(loading("분석 카탈로그를 불러오는 중…"));

  let cat = null, env = null;
  try {
    env = await getJSON(CATALOG);
    cat = (env && env.data) || env;
  } catch (e) {
    root.innerHTML = "";
    if (e instanceof DataMissingError) { root.appendChild(missingPanel(CATALOG)); return; }
    root.appendChild(errorPanel(e, "analytics.json 을 읽지 못했습니다.")); return;
  }
  root.innerHTML = "";

  root.appendChild(headerPanel(cat, env));

  const tabBar = el("div", { class: "chip-row" });
  const body = el("div", {});
  root.appendChild(tabBar);
  root.appendChild(body);

  let current = TABS.some(([k]) => k === query.tab) ? query.tab : "eha";
  const btns = new Map();
  for (const [key, label] of TABS) {
    const b = el("button", { class: "btn", text: label, onclick: () => select(key) });
    btns.set(key, b);
    tabBar.appendChild(b);
  }

  let token = 0;
  async function select(key) {
    current = key;
    for (const [k, b] of btns) b.classList.toggle("active", k === key);
    setHash({ tab: key });
    const my = ++token;
    body.innerHTML = "";
    body.appendChild(loading());
    let node;
    try {
      if (key === "eha") node = await ehaTab(cat, query);
      else if (key === "community") node = await communityTab(cat);
      else node = await peersTab(cat, query);
    } catch (e) {
      node = e instanceof DataMissingError ? missingPanel(e.message || "shard") : errorPanel(e, `${key} 탭 렌더 실패`);
    }
    if (my !== token) return;
    body.innerHTML = "";
    body.appendChild(node);
  }

  await select(current);
  root.appendChild(envelopeFooter(env));
}

/** 화면 전체를 다시 그리지 않고 주소만 갱신한다(공유·새로고침 대비). */
function setHash(patch) {
  const [path, qs] = String(location.hash.replace(/^#/, "")).split("?");
  const q = new URLSearchParams(qs || "");
  for (const [k, v] of Object.entries(patch)) { if (v == null) q.delete(k); else q.set(k, v); }
  const next = "#" + (path || "/analytics") + (q.toString() ? "?" + q.toString() : "");
  if (next !== location.hash) history.replaceState(null, "", next);
}

function missingPanel(what) {
  return el("div", { class: "panel panel-warn" },
    el("h2", { class: "panel-title", text: "이 산출물이 현재 데이터 소스에 없습니다" }),
    el("p", { text: `${what} 을(를) 찾지 못했습니다. 실데이터 모드(?src=real)인지 확인하고, 아직 굽지 않았다면 아래로 생성하세요.` }),
    el("p", { class: "hint" }, el("b", { text: "생성 방법: " }), el("code", { text: GEN_CMD })));
}

function headerPanel(cat, env) {
  const p = cat.params || {};
  const eha = cat.eha || [], sp = cat.spatial || [], pm = cat.peer_methods_summary || {};
  return section("확산 위험모형 · 커뮤니티 · 유사방법비교",
    asOfLine(`생성기 ${cat.generator || "?"} · 생성시각 ${cat.generated_at || "?"} · 카탈로그 ${CATALOG}`),
    el("div", { class: "banner banner-est", role: "note" },
      el("span", { class: "banner-tag", text: "인과 아님" }),
      el("span", { class: "banner-body", text:
        "아래 계수·군집은 관측 데이터의 연관 구조다. 무작위 배정 실험이 아니므로 "
        + "'이웃이 만들면 우리도 만든다' 같은 인과 주장으로 읽으면 안 된다. "
        + "각 모형의 해석 주의문구를 함께 읽을 것." })),
    el("div", { class: "stat-grid" },
      statCard("EHA 템플릿", num(eha.length), `모형 ${eha.reduce((a, x) => a + (x.n_models || 0), 0)}종`),
      statCard("커뮤니티 scope", num((cat.community || []).length ? Object.keys((cat.community[0] || {}).scopes || {}).length : 0),
        "조례 유사도 · 지자체 인접"),
      statCard("유사방법비교", num(pm.shards || 0) + "곳",
        pm.mean_jaccard_mois_vs_legacy != null
          ? `구 방식과 겹침 평균 Jaccard ${pm.mean_jaccard_mois_vs_legacy}` : "—"),
      statCard("공간지표(별도 화면)", num(sp.length), "#/spatial 에서 본다")),
    note(`순열 ${p.permutations ?? "?"}회 · level=${p.level ?? "?"} · 회계연도 FY${p.fyr ?? "?"} · seed=${p.seed ?? "?"}`),
    (cat.errors || []).length
      ? note(`생성 중 오류 ${cat.errors.length}건: ` + cat.errors.map((e) => `${e.kind}/${e.slug || e.scope || e.sig_cd}`).join(", "), "warn")
      : null,
    env ? null : null);
}

/* ------------------------------------------------------------------ */
/*  1) EHA — 이산시간 위험모형                                          */
/* ------------------------------------------------------------------ */
const STAR = (p) => (p == null ? "" : p < 0.001 ? "***" : p < 0.01 ? "**" : p < 0.05 ? "*" : p < 0.10 ? "†" : "");

async function ehaTab(cat, query) {
  const entries = (cat.eha || []).filter((e) => e.slug);
  if (!entries.length) return missingPanel("api/eha/*.json");

  const wrap = el("div", {});
  const initial = (query.slug && entries.some((e) => e.slug === query.slug)) ? query.slug : entries[0].slug;

  const sel = el("select", { class: "sel sel-wide", "aria-label": "정책 템플릿 선택" });
  for (const e of entries) {
    const o = el("option", { value: e.slug,
      text: `${e.template} (${(e.window || []).join("~")} · N=${num(e.n_obs)} · 사건 ${num(e.n_events)})` });
    if (e.slug === initial) o.selected = true;
    sel.appendChild(o);
  }
  const body = el("div", {});
  sel.addEventListener("change", () => { setHash({ slug: sel.value }); draw(sel.value); });

  wrap.appendChild(el("div", { class: "toolbar" },
    el("label", { text: "정책 템플릿 " }), sel,
    el("span", { class: "muted small", text: `${entries.length}종 사전계산` })));
  wrap.appendChild(body);

  async function draw(slug) {
    body.innerHTML = "";
    body.appendChild(loading("위험모형 결과를 불러오는 중…"));
    let d;
    try {
      const e = entries.find((x) => x.slug === slug);
      const env = await getJSON(`api/${(e.path || `eha/${slug}.json`).replace(/^\.?\//, "")}`);
      d = (env && env.data) || env;
    } catch (err) {
      body.innerHTML = "";
      body.appendChild(err instanceof DataMissingError ? missingPanel(`api/eha/${slug}.json`) : errorPanel(err, "EHA shard 로드 실패"));
      return;
    }
    body.innerHTML = "";
    body.appendChild(ehaPanels(d, slug));
  }
  setHash({ slug: initial });
  await draw(initial);
  return wrap;
}

function ehaPanels(d, slug) {
  const wrap = el("div", {});
  // 미수렴(완전분리) 모형은 계수가 1e9 대로 발산한 채 유의성 별표까지 달고 온다.
  // 생성기가 diverged 로 표시해 두므로 표에서 빼고 사유만 따로 알린다.
  const allModels = d.models || [];
  const models = allModels.filter((m) => !m.diverged);
  const diverged = allModels.filter((m) => m.diverged);
  const primary = models.find((m) => m.role === (d.primary_role || "primary")) || models[0];
  const rs = d.risk_set || {};

  // 결론 배너 — 지지/미지지를 먼저 말한다
  wrap.appendChild(section(`${d.template} — 이산시간 위험모형`,
    asOfLine(`engine=${d._engine || "?"} · 관측창 ${(d.window || []).join("~")} · level=${d.level ?? "?"}`),
    el("div", { class: `banner ${d.neighbor_effect_supported ? "banner-ok" : "banner-warn"}`, role: "note" },
      el("span", { class: "banner-tag", text: d.neighbor_effect_supported ? "이웃효과 지지" : "이웃효과 미지지" }),
      el("span", { class: "banner-body", text: d.neighbor_effect_note || "—" })),
    el("div", { class: "stat-grid" },
      statCard("관측(위험집합)", num(rs.n_obs), "지자체 × 연도 행"),
      statCard("채택 사건", num(rs.n_events), `우측절단 ${num(rs.right_censored)} · 좌측절단 ${num(rs.left_truncated)}`),
      statCard("클러스터", num(rs.n_clusters), "지자체 단위 로버스트 SE"),
      statCard("McFadden R²", (primary?.model?.mcfadden_r2 ?? 0).toFixed(4), `링크 ${primary?.link || "?"}`),
      statCard("제정본 커버리지", pct(d.enactment_date_coverage), "채택시점 관측 비율")),
    note(rs.note || ""),
    note(d.coverage_note || "", "warn")));

  // 주모형 계수표
  if (primary) {
    const terms = (primary.model?.terms) || [];
    const gl = d.covariate_glossary || {};
    wrap.appendChild(section(`주모형 계수 — ${primary.spec_note || primary.role}`,
      table(["항목", "계수", "SE(로버스트)", "z", "p", "유의", "OR", "OR 95% CI", "뜻"],
        terms.map((t) => [
          t.term,
          fx(t.coef, 4),
          fx(t.se_robust ?? t.se, 4),
          fx(t.z, 3),
          t.p_value == null ? "—" : (t.p_value < 1e-4 ? "<0.0001" : t.p_value.toFixed(4)),
          STAR(t.p_value),
          fx(t.odds_ratio, 3),
          Array.isArray(t.or_ci95) ? `${fx(t.or_ci95[0], 3)} ~ ${fx(t.or_ci95[1], 3)}` : "—",
          gl[t.term] || "",
        ])),
      note("유의표기 " + Object.entries(d.significance_legend || {}).map(([k, v]) => `${k} ${v}`).join(" · ")),
      note(`SE = ${primary.model?.se_reported || "?"} · 연속변수 z-표준화 ${primary.model?.standardized ? "적용" : "미적용"} `
        + `· 결측으로 제외된 행 ${num(primary.model?.rows_dropped_missing_covariate ?? 0)}`)));
  }

  // 민감도 비교 — neighbor_exposure 만 뽑아 나란히
  const rows = models.map((m) => {
    const t = (m.model?.terms || []).find((x) => x.term === "neighbor_exposure") || {};
    const pe = (m.model?.terms || []).find((x) => x.term === "peer_exposure");
    return [
      m.role, m.spec_note || "", `${m.mode || "?"} / ${m.link || "?"}`,
      fx(t.coef, 4), t.p_value == null ? "—" : t.p_value.toFixed(4), STAR(t.p_value),
      pe ? `${fx(pe.coef, 4)} (p=${pe.p_value?.toFixed(4)})` : "—",
      fx(m.model?.mcfadden_r2, 4),
    ];
  });
  wrap.appendChild(section("민감도 — 정의·링크를 바꿔도 결론이 뒤집히는가",
    table(["역할", "설명", "mode/link", "neighbor 계수", "p", "유의", "peer_exposure", "R²"], rows),
    note("주모형과 민감도 모형에서 neighbor_exposure 의 부호·유의성이 같으면 결론이 정의 선택에 좌우되지 않는다는 뜻이다.")));

  /* 확산 경로 3종 head-to-head.
   *
   * 세 노출 변수는 형식이 같다(t-1 까지 채택한 비율). 다른 것은 '누구를 이웃으로
   * 세는가' 뿐이다 — 지리적 인접 / 인구·재정이 비슷한 곳 / 그래프 임베딩이 가까운 곳.
   * 한 모형에 같이 넣으면 서로를 통제한 뒤 어느 경로가 남는지 볼 수 있다.
   * three_channel 모형이 없으면(생성기 미실행·Region 임베딩 부재) 이 절을 건너뛴다. */
  const CHANNELS = [
    ["neighbor_exposure", "지리적 인접", "옆에 있으니까 따라 한다"],
    ["peer_exposure", "통계적 유사", "인구·재정 형편이 비슷하니까 (행안부 기준)"],
    ["neural_exposure", "구조적 유사", "조례 구성·상위법 연결이 닮았으니까 (그래프 신경망)"],
  ];
  const tri = models.filter((m) => (m.model?.terms || []).some((t) => t.term === "neural_exposure"));
  if (tri.length) {
    const box = section("확산 경로 3종 비교 — 무엇이 채택을 예측하는가");
    for (const m of tri) {
      const terms = m.model?.terms || [];
      box.appendChild(el("h3", { class: "card-title",
        text: `${m.role} — ${m.mode || "?"} / ${m.link || "?"}` }));
      box.appendChild(table(
        ["확산 경로", "뜻", "계수", "SE", "p", "유의", "OR", "OR 95% CI"],
        CHANNELS.map(([key, label, meaning]) => {
          const t = terms.find((x) => x.term === key);
          if (!t) return [label, meaning, "—", "—", "—", "", "—", "이 모형에 미포함"];
          return [
            label, meaning,
            fx(t.coef, 4), fx(t.se_robust ?? t.se, 4),
            t.p_value == null ? "—" : (t.p_value < 1e-4 ? "<0.0001" : t.p_value.toFixed(4)),
            STAR(t.p_value), fx(t.odds_ratio, 3),
            Array.isArray(t.or_ci95) ? `${fx(t.or_ci95[0], 3)} ~ ${fx(t.or_ci95[1], 3)}` : "—",
          ];
        })));
    }
    box.appendChild(note(
      "세 변수는 모두 't-1 까지 채택한 이웃 비율' 로 형식이 같다. 다른 것은 이웃을 세는 기준뿐이라 "
      + "계수를 직접 비교할 수 있다. 셋을 한 모형에 같이 넣었으므로 각 계수는 나머지 둘을 통제한 뒤의 값이다."));
    wrap.appendChild(box);
  }

  if (diverged.length) {
    wrap.appendChild(section("추정에 실패한 모형",
      note("아래 사양은 최대우도 추정이 수렴하지 않았다(완전분리 의심). 계수가 발산해 "
        + "해석할 수 없으므로 위 표에서 제외했다. 어떤 사양이 왜 실패했는지도 결과이므로 감추지 않는다.", "warn"),
      table(["역할", "mode/link", "사유"],
        diverged.map((m) => [m.role, `${m.mode || "?"} / ${m.link || "?"}`, m.divergence_reason || "미수렴"]))));
  }

  // 해석 주의 + 참고문헌 + 콘솔표
  wrap.appendChild(section("해석 주의와 근거",
    note(d.interpretation_caveat || "", "warn"),
    el("details", {},
      el("summary", { text: "공변량 용어집" }),
      table(["항목", "정의"], Object.entries(d.covariate_glossary || {}))),
    el("details", {},
      el("summary", { text: "채택시점 정의(모드)별 커버리지" }),
      table(["모드", "관측 채택", "보유(판본무관)", "커버리지", "경고"],
        Object.entries(d.adoption_meta || {}).map(([k, v]) => [
          k, num(v.adopters_observed), num(v.holders_any_version),
          pct(v.enactment_date_coverage), v.warning || ""]))),
    ...models.map((m) => el("details", {},
      el("summary", { text: `콘솔 표 — ${m.role}` }),
      el("pre", { class: "code-block", text: m.console_table || "(없음)" }))),
    el("details", {},
      el("summary", { text: `참고문헌 ${(d.references || []).length}건` }),
      el("ul", {}, ...(d.references || []).map((r) => el("li", { text: r })))),
    note(`검증상태: ${d.verification_status || "?"} · shard api/eha/${slug}.json`),
    (d.model_errors || []).length ? note(`모형 실패 ${d.model_errors.length}건: ${d.model_errors.join(" / ")}`, "warn") : null));

  return wrap;
}

function fx(v, n) { return v == null || Number.isNaN(v) ? "—" : Number(v).toFixed(n); }

/* ------------------------------------------------------------------ */
/*  2) 커뮤니티 탐지                                                    */
/* ------------------------------------------------------------------ */
async function communityTab(cat) {
  const meta = (cat.community || [])[0];
  const rel = "api/" + String((meta && meta.path) || "community/summary.json").replace(/^\.?\//, "");
  let d;
  try {
    const env = await getJSON(rel);
    d = (env && env.data) || env;
  } catch (e) {
    return e instanceof DataMissingError ? missingPanel(rel) : errorPanel(e, "커뮤니티 요약 로드 실패");
  }

  const scopes = d.scopes || {};
  const keys = Object.keys(scopes);
  if (!keys.length) return missingPanel(rel);

  const wrap = el("div", {});
  const sel = el("select", { class: "sel sel-wide", "aria-label": "커뮤니티 scope 선택" });
  for (const k of keys) sel.appendChild(el("option", { value: k, text: `${k} — ${(d.scope_guide || {})[k] || ""}` }));
  const body = el("div", {});
  sel.addEventListener("change", () => draw(sel.value));

  wrap.appendChild(el("div", { class: "toolbar" }, el("label", { text: "그래프 " }), sel,
    el("span", { class: "muted small", text: `${keys.length}종` })));
  wrap.appendChild(note(d.coverage_caveat || "", "warn"));
  wrap.appendChild(body);

  function draw(k) {
    const s = scopes[k];
    body.innerHTML = "";
    body.appendChild(section(`커뮤니티 — ${k}`,
      asOfLine(`engine=${d._engine || "?"} · backend=${s.backend || "?"} · as_of=${s.as_of_date || "?"}`),
      el("div", { class: "stat-grid" },
        statCard("모듈러리티", (s.modularity ?? 0).toFixed(6), "1에 가까울수록 군집이 뚜렷"),
        statCard("탐지 커뮤니티", num(s.num_communities_detected), `요약 ${num(s.num_communities_summarized)}개`),
        statCard("최소 크기", num(s.min_size), "이보다 작은 군집은 요약 생략"),
        statCard("계산시간", `${s.elapsed_sec ?? "?"}s`, s.built_at || "")),
      note((d.scope_guide || {})[k] || "")));

    for (const c of (s.communities || [])) body.appendChild(communityCard(c, k));
    body.appendChild(note(`검증상태: ${d.verification_status || "?"} · shard ${rel}`));
  }
  draw(keys[0]);
  return wrap;
}

function communityCard(c, scope) {
  const kids = [];
  kids.push(el("div", { class: "chip-row" },
    badge(`#${c.id}`, "badge-plain"),
    badge(`크기 ${num(c.size)}`, "badge-info"),
    c.region_count != null ? badge(`지자체 ${num(c.region_count)}곳`, "badge-plain") : null,
    c.adjacency_ratio != null ? badge(`인접비율 ${c.adjacency_ratio}`, "badge-plain") : null,
    c.ordinance_total != null ? badge(`조례 ${num(c.ordinance_total)}건`, "badge-plain") : null));

  if (c.narrative) kids.push(el("p", { class: "article-text", text: c.narrative }));

  if ((c.name_pattern || []).length) {
    kids.push(el("div", { class: "chip-row" },
      ...c.name_pattern.map((t) => badge(`${t.term} ${t.count} (${pct(t.share)})`, "badge-plain"))));
  }
  if ((c.categories || []).length) {
    kids.push(el("div", { class: "chip-row" },
      ...c.categories.slice(0, 8).map((x) => badge(`${x.name || categoryName(x.code)} ${num(x.n)}`, "badge-info"))));
  }
  if ((c.top_provinces || []).length) {
    kids.push(table(["시도", "소속 수"], c.top_provinces.map((p) => [p.name || p.region_id, num(p.n)])));
  }
  if (c.year_span) {
    kids.push(note(`제정 ${c.year_span.first}~${c.year_span.last} · 최다 ${c.year_span.peak}년(${num(c.year_span.peak_count)}건)`));
  }
  if (c.budget) {
    kids.push(el("div", { class: "chip-row" },
      badge("추정 연결", "badge-est"),
      badge(`연계 예산사업 ${num(c.budget.linked_budget_lines)}건`, "badge-plain"),
      badge(`편성 ${won(c.budget.alloc_amt)}`, "badge-plain"),
      badge(`지출 ${won(c.budget.exe_amt)}`, "badge-plain")));
    kids.push(note("조례↔예산 연결은 확률적 자동매칭이다(표본 정밀도 64.9%). 확정 사실이 아니다.", "warn"));
  }
  if (c.nationwide) {
    const nw = c.nationwide;
    kids.push(note(`전국 투영 — 앵커 '${nw.anchor}' 로 역조회한 현행 조례 ${num(nw.ordinances_nationwide)}건 / `
      + `${num(nw.regions_nationwide)}개 지자체. 커뮤니티 size(본문 확보분)와 다른 수다.`));
  }
  if ((c.representatives || []).length) {
    kids.push(el("details", {},
      el("summary", { text: `대표 조례 ${c.representatives.length}건` }),
      table(["조례", "지자체", "제정일", "중심성", "원문"],
        c.representatives.map((r) => [
          r.name, r.region_name || r.org_name || "—", ymd(r.enacted_on),
          r.centrality != null ? r.centrality.toFixed(4) : "—",
          r.official_url ? extLink(r.official_url, "law.go.kr") : "—",
        ]))));
  }
  if ((c.regions || []).length) {
    kids.push(el("details", {},
      el("summary", { text: `소속 지자체 ${c.regions.length}곳` }),
      table(["지자체", "코드", "상세"],
        c.regions.map((r) => [
          r.full_name || r.name, r.region_id,
          el("button", { class: "btn-link", text: "지역 상세", onclick: () => go(`/region/${r.region_id}`) }),
        ]))));
  }
  return section(`[${scope}] ${c.label || "(무명)"} `, ...kids);
}

/* ------------------------------------------------------------------ */
/*  3) 유사 지자체 방법비교                                              */
/* ------------------------------------------------------------------ */
async function peersTab(cat, query) {
  const rows = (cat.peer_methods || []).filter((r) => r.sig_cd);
  const sum = cat.peer_methods_summary || {};
  if (!rows.length) return missingPanel("api/peer_methods/*.json");

  let guide = null;
  try { const g = await getJSON(GUIDE); guide = (g && g.data) || g; } catch (e) { guide = null; }

  const cat2 = await loadRegionCatalog();
  const covered = new Set(rows.map((r) => String(r.sig_cd)));
  const byCd = new Map(rows.map((r) => [String(r.sig_cd), r]));

  const wrap = el("div", {});
  wrap.appendChild(section("유사 지자체 — 방법을 바꾸면 결론이 바뀌는가",
    asOfLine("engine=analytics.peers.compare_peer_methods"),
    el("div", { class: "stat-grid" },
      statCard("사전계산 지자체", num(sum.shards || rows.length), "level=2 기초"),
      statCard("겹침 평균 Jaccard", sum.mean_jaccard_mois_vs_legacy ?? "—", "행안부정렬 vs 구 방식"),
      statCard("구 방식 산출 성공", num(sum.n_with_legacy || 0), `${num((sum.shards || rows.length) - (sum.n_with_legacy || 0))}곳 실패`)),
    note(guide?.reading_guide || sum.note || ""),
    note(guide?.same_type_note || "")));

  const initial = (query.sig && covered.has(String(query.sig)) ? String(query.sig) : null)
    || (covered.has("47190") ? "47190" : rows[0].sig_cd);

  const body = el("div", {});
  wrap.appendChild(regionSelector({
    items: cat2.items, sidoOf: cat2.sidoOf, current: initial, covered,
    label: "기준 지자체", coveredLabel: "사전계산된 곳만", coveredWord: "사전계산",
    onChange: (sig) => { setHash({ sig }); draw(sig); },
  }));
  wrap.appendChild(body);

  async function draw(sig) {
    body.innerHTML = "";
    body.appendChild(loading("방법 비교를 불러오는 중…"));
    const meta = byCd.get(String(sig));
    const rel = "api/" + String((meta && meta.path) || `peer_methods/${sig}.json`).replace(/^\.?\//, "");
    let d;
    try { const env = await getJSON(rel); d = (env && env.data) || env; }
    catch (e) {
      body.innerHTML = "";
      body.appendChild(e instanceof DataMissingError ? missingPanel(rel) : errorPanel(e, "방법비교 shard 로드 실패"));
      return;
    }
    body.innerHTML = "";
    body.appendChild(peerCompare(d, rel, guide));
  }
  setHash({ sig: initial });
  await draw(initial);
  return wrap;
}

function peerCompare(d, rel, guide) {
  const wrap = el("div", {});
  const methods = d.methods || {};
  const keys = Object.keys(methods);
  const tgt = d.target || {};

  wrap.appendChild(section(`${tgt.name || d.sig_cd} — 방법별 Top-${d.k ?? "?"}`,
    asOfLine(`대상 유형 ${tgt.rtype || "?"} · shard ${rel}`),
    el("div", { class: "chip-row" },
      ...keys.map((k) => badge(`${k} (${methods[k].k}곳)`, k === "mois_aligned" ? "badge-verified" : "badge-plain"))),
    d.legacy_error ? note(`구 방식 산출 실패: ${d.legacy_error}`, "warn") : null,
    keys.some((k) => !methods[k].peers.length)
      ? note("일부 방식이 0곳을 돌려줬다. 지표(재정자립도·복지예산 비율·예산총액)가 DB에 없는 "
             + "지자체는 지표 기반 방식(mois_aligned·hybrid_policy)이 계산 자체를 못한다 — "
             + "2026년 통합·개편으로 신설된 곳이 여기에 해당한다. '유사한 곳이 없다'가 아니라 "
             + "'비교 불가'라는 뜻이다.", "warn")
      : null,
    table(["순위", ...keys.map((k) => k)],
      Array.from({ length: Math.max(...keys.map((k) => methods[k].peers.length)) }, (_, i) => [
        `#${i + 1}`,
        ...keys.map((k) => {
          const p = methods[k].peers[i];
          if (!p) return "—";
          return `${p.name || p.region_id} (${p.rtype || "?"}) · sim ${p.similarity != null ? p.similarity.toFixed(4) : "—"}`;
        }),
      ])),
    el("details", {},
      el("summary", { text: "방법 설명" }),
      table(["방법", "정의"], keys.map((k) => [k, methods[k].label || (guide?.method_labels || {})[k] || ""])))));

  const ov = d.overlap || {};
  wrap.appendChild(section("겹침 — 같은 지자체를 얼마나 같이 뽑나",
    table(["방법 쌍", `공통 Top-${d.k ?? "?"}`, "Jaccard"],
      Object.entries(ov).map(([pair, v]) => [pair, num(v.overlap_at_k), v.jaccard ?? "—"])),
    note("Jaccard 1.0 = 완전히 같은 목록, 0 = 하나도 겹치지 않음. "
      + "격차분석(#/gap)은 mois_aligned 를 쓴다. 겹침이 낮다면 '무엇이 격차인가'가 방법 선택에 달려 있다는 뜻이다.")));

  const tc = d.type_composition || {};
  wrap.appendChild(section("유형 구성 — 자치구·시·군을 섞어 비교하지는 않나",
    table(["방법", "유형 분포", "대상과 같은 유형 비율"],
      Object.entries(tc).map(([k, v]) => [
        k, Object.entries(v.types || {}).map(([t, n]) => `${t} ${n}`).join(" · "), pct(v.same_type_rate)])),
    note(guide?.same_type_note || "")));

  if (guide?.weight_provenance) {
    wrap.appendChild(section("가중치 출처",
      table(["지표", "출처"], Object.entries(guide.weight_provenance)),
      note(guide.policy_profile_note || "")));
  }
  wrap.appendChild(note(`검증상태: ${d.verification_status || "?"}`));
  return wrap;
}
