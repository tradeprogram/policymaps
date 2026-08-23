// 10. 신경망 유사도 — api/neural/ (make_neural_fixtures.py 산출)
//    ★ 그래프 구조 임베딩의 코사인이다. 조문 의미 유사도가 아니며 '선례 추천'이 아니다.
//    ★ 품질 지표의 AUC 는 in-sample 재현 지표다. "일반화 AUC" 로 부르면 안 된다.
import { el, num, pct, ymd, extLink, debounce } from "../util.js";
import { getJSON, DataMissingError, loadRegionCatalog, loadRegionalShard, categoryName } from "../api.js";
import { section, table, note, loading, asOfLine, errorPanel, badge, statCard,
         statusBadge, envelopeFooter, cdnFailPanel } from "../components.js";
import { regionSelector, sourceLine } from "../nationwide.js";
import { ensureChart } from "../vendor.js";

/** api/neural/index.json 의 path 는 이 디렉터리 기준 상대경로다(layout 참고). */
const DIR = "api/neural";
const GENERATOR = "system/make_neural_fixtures.py";

const TABS = [
  ["ordinance", "조례 유사도"],
  ["region", "지자체 유사도"],
  ["fullregion", "지역 전량"],
  ["quality", "모델 품질"],
];

/**
 * 지역 전량 묶음(make_full_vote_neural.py --only neural).
 * 위 "조례 유사도" 탭은 사전선정 400건 표본이고, 이쪽이 neural_similarity 저장본 전량이다
 * (조례 노드 154,310건 · Top-10 엣지 1,877,420건 = DB 전량).
 */
const FULL_REGION_INDEX = "api/neural_by_region_index.json";

/** api/neural_eval.json 의 요약. loadEval() 이 채운다. 없으면 빈 값으로 남는다. */
const EVAL_PATH = "api/neural_eval.json";
const NEURAL_EVAL = { best: null, byModel: {}, method: null, ranking: [], loaded: false };

/**
 * 모델 평가 결과를 한 번만 읽는다.
 *
 * '모델 간 일치도 0' 만 보여주면 모델이 서로 다른 타당한 이웃을 고른 것인지,
 * 일부가 사실상 무작위인지 구분되지 않는다. 무작위 기준선 대비 분야 일치율(lift)을
 * 같이 보여야 판단할 수 있다. 생성기는 system/make_neural_eval.py.
 */
async function loadEval() {
  if (NEURAL_EVAL.loaded) return NEURAL_EVAL;
  NEURAL_EVAL.loaded = true;
  try {
    const env = await getJSON(EVAL_PATH);
    const d = env.data || env;
    NEURAL_EVAL.best = d.best_model || null;
    NEURAL_EVAL.method = d.method || null;
    NEURAL_EVAL.ranking = d.ranking || [];
    for (const m of d.models || []) NEURAL_EVAL.byModel[m.model] = m;
  } catch (e) { /* 평가 파일이 없으면 조용히 넘어간다 — 화면은 그대로 동작한다 */ }
  return NEURAL_EVAL;
}

/* ------------------------------------------------------------------ *
 * 로더 — 없으면 null. 화면이 안내로 처리하고 죽지 않는다.
 * ------------------------------------------------------------------ */

async function maybe(rel) {
  try { return await getJSON(rel); }
  catch (e) { if (e instanceof DataMissingError) return null; throw e; }
}

const loadIndex = () => maybe(`${DIR}/index.json`);
const loadQuality = () => maybe(`${DIR}/quality.json`);
const loadShard = (relInsideNeural) => maybe(`${DIR}/${String(relInsideNeural).replace(/^\/+/, "")}`);

/**
 * 노드가 문서에 붙은 뒤 콜백을 실행한다.
 * 탭 본문은 detached 상태로 조립해 반환하므로, Chart.js 처럼 레이아웃 크기가 필요한
 * 라이브러리를 그 시점에 초기화하면 캔버스 폭이 0 이 된다(실측).
 */
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

/** 숫자 포맷 — 코사인·AUC 처럼 0~1 소수 */
function fx(v, d = 4) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toFixed(d);
}

function missingPanel(err) {
  return el("div", { class: "panel panel-warn" },
    el("h2", { class: "panel-title", text: "신경망 산출물이 현재 데이터 소스에 없습니다" }),
    el("p", { text:
      `이 화면은 사전계산 파일 ${DIR}/index.json · ${DIR}/quality.json · ${DIR}/ordinance|region/*.json 을 씁니다. `
      + "가상데이터 번들에는 들어 있지 않고, 실데이터에서도 생성기를 돌려야 채워집니다." }),
    el("p", { class: "hint" },
      el("b", { text: "생성 방법: " }),
      el("code", { text: `python ${GENERATOR}` }),
      document.createTextNode(" → "),
      el("code", { text: `${DIR}/{index,quality}.json · ${DIR}/ordinance/ordin-{mst}.json · ${DIR}/region/{sig_cd}.json` })),
    err ? el("pre", { class: "err", text: `${err.name || "Error"}: ${err.message || err}` }) : null
  );
}

/* ------------------------------------------------------------------ *
 * 진입점
 * ------------------------------------------------------------------ */

export async function render(root, params, query = {}) {
  root.appendChild(loading("신경망 임베딩 색인을 불러오는 중…"));

  let idx;
  try {
    // 평가는 실패해도 화면이 도므로 색인과 함께 병렬로 받고 결과만 무시한다.
    [idx] = await Promise.all([loadIndex(), loadEval()]);
  } catch (e) { root.innerHTML = ""; root.appendChild(errorPanel(e, `${DIR}/index.json 로드 실패`)); return; }

  root.innerHTML = "";
  if (!idx) { root.appendChild(missingPanel()); return; }

  const models = Array.isArray(idx.models) ? idx.models : [];
  const ords = Array.isArray(idx.ordinances) ? idx.ordinances : [];
  const regs = Array.isArray(idx.regions) ? idx.regions : [];

  root.appendChild(headerPanel(idx, models, ords, regs));

  const tabBar = el("div", { class: "chip-row" });
  const body = el("div", {});
  root.appendChild(tabBar);
  root.appendChild(body);

  let current = TABS.some(([k]) => k === query.tab) ? query.tab : (query.ord ? "ordinance" : "ordinance");
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
    const my = ++token;
    body.innerHTML = "";
    body.appendChild(loading());
    let node;
    try {
      if (key === "ordinance") node = await ordinanceTab(idx, ords, query);
      else if (key === "region") node = await regionTab(idx, regs, query);
      else if (key === "fullregion") node = await fullRegionTab(query);
      else node = await qualityTab();
    } catch (e) {
      node = errorPanel(e, `${key} 탭 렌더 실패`);
    }
    if (my !== token) return;
    body.innerHTML = "";
    body.appendChild(node);
  }

  await select(current);
}

function headerPanel(idx, models, ords, regs) {
  const p = idx.params || {};
  const sel = idx.target_selection || {};
  const panel = section("그래프 신경망 임베딩 유사도",
    asOfLine(`생성기 ${idx.generator || "?"} · 생성시각 ${idx.generated_at || "?"}`),
    el("div", { class: "banner banner-est", role: "note" },
      el("span", { class: "banner-tag", text: "탐색 보조" }),
      el("span", { class: "banner-body", text:
        "여기 유사도는 조문 의미가 아니라 그래프 구조(같은 지자체·같은 분류·같은 상위법·같은 재정사업으로 이어지는지)를 "
        + "학습한 임베딩의 코사인이다. 선례 추천이 아니라 탐색 보조로만 쓸 것. 폐지 조례는 선례로 인용하지 말 것." })),
    el("div", { class: "stat-grid" },
      statCard("모델", num(models.length), models.map((m) => m.model_name).join(" · ") || "—"),
      statCard("조례 shard", num(ords.length), `top_k=${p.k ?? "?"} · gap 추천 등장 ${num(sel.from_gap_shards)}건`),
      statCard("지자체 shard", num(regs.length), `커버 sig_cd ${num(sel.distinct_sig_cd)}곳`),
      statCard("임베딩 노드", num(models.reduce((a, m) => a + (m.nodes_total || 0), 0)),
        "3개 모델 node_embeddings 합계")
    )
  );
  const notes = Array.isArray(idx.notes) ? idx.notes : [];
  if (notes.length) {
    panel.appendChild(el("details", {},
      el("summary", { text: "생성기 주석 / 한계 표기" }),
      el("ul", {}, notes.map((n) => el("li", { class: "small", text: String(n) })))));
  }
  return panel;
}

/* ------------------------------------------------------------------ *
 * 탭 1 — 조례 유사도
 * ------------------------------------------------------------------ */

async function ordinanceTab(idx, ords, query) {
  const wrap = el("div", {});
  if (!ords.length) {
    wrap.appendChild(note("색인에 조례 shard 목록이 없습니다.", "warn"));
    return wrap;
  }

  const byKey = new Map(ords.map((o) => [String(o.key), o]));
  const initial = (query.ord && byKey.has(String(query.ord)) ? String(query.ord) : null)
    || (ords.find((o) => (o.gap_hits || 0) > 0) || ords[0]).key;

  const detail = el("div", {});
  wrap.appendChild(ordinanceSelector({ items: ords, current: initial, onChange: (k) => draw(k) }));
  wrap.appendChild(detail);

  let token = 0;
  async function draw(key) {
    const my = ++token;
    const meta = byKey.get(String(key));
    detail.innerHTML = "";
    detail.appendChild(loading(`${meta ? meta.name : key} 의 유사 조례를 불러오는 중…`));
    const rel = (meta && meta.path) || `ordinance/${key}.json`;
    let env;
    try { env = await loadShard(rel); }
    catch (e) { if (my === token) { detail.innerHTML = ""; detail.appendChild(errorPanel(e, `${DIR}/${rel} 로드 실패`)); } return; }
    if (my !== token) return;
    detail.innerHTML = "";
    if (!env) { detail.appendChild(missingPanel(new DataMissingError(`${DIR}/${rel}`, 404))); return; }
    detail.appendChild(ordinanceBody(env, `${DIR}/${rel}`));
  }

  await draw(initial);
  return wrap;
}

/** 400건 조례 선택기 — 이름/지자체/코드 검색 + gap 추천 등장분만 보기 */
function ordinanceSelector({ items, current, onChange }) {
  const sel = el("select", { class: "sel sel-wide", "aria-label": "조례 선택" });
  const filter = el("input", { class: "search-input", type: "search",
    placeholder: "조례명·지자체 검색 (예: 청년, 구미시)", "aria-label": "조례 검색" });
  const boxId = "neural-only-gap";
  const onlyGap = el("input", { type: "checkbox", id: boxId });
  const countLine = el("span", { class: "muted small" });
  const gapCount = items.filter((o) => (o.gap_hits || 0) > 0).length;

  function build() {
    const q = filter.value.trim().toLowerCase();
    sel.innerHTML = "";
    const groups = new Map();
    let shown = 0;
    for (const it of items) {
      if (onlyGap.checked && !(it.gap_hits > 0) && it.key !== current) continue;
      if (q) {
        const hay = `${it.name || ""} ${it.region_name || ""} ${it.key} ${(it.categories || []).join(" ")}`.toLowerCase();
        if (!hay.includes(q) && it.key !== current) continue;
      }
      const g = it.region_name || "지자체 미상";
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(it);
      shown++;
    }
    if (!groups.size) sel.appendChild(el("option", { value: "", text: "검색 결과 없음", disabled: "disabled" }));
    for (const [g, list] of groups) {
      const og = el("optgroup", { label: `${g} (${list.length})` });
      for (const it of list) {
        const mark = it.gap_hits > 0 ? "★ " : "";
        const rp = it.status === "repealed" ? "[폐지] " : "";
        const o = el("option", { value: it.key, text: `${mark}${rp}${it.name || it.key}` });
        if (String(it.key) === String(current)) o.selected = true;
        og.appendChild(o);
      }
      sel.appendChild(og);
    }
    countLine.textContent = `${shown}/${items.length}건 표시 · gap 추천 등장 ${gapCount}건(★)`;
  }

  filter.addEventListener("input", debounce(build, 150));
  onlyGap.addEventListener("change", build);
  sel.addEventListener("change", () => { if (sel.value) { current = sel.value; onChange(sel.value); } });
  build();

  return el("div", { class: "toolbar" },
    el("label", { text: "조례 " }), sel, filter,
    el("label", { class: "muted small", for: boxId }, onlyGap, document.createTextNode(" 격차분석 추천에 뜬 조례만")),
    countLine);
}

function ordinanceBody(env, path) {
  const d = env.data || {};
  const o = d.ordinance || {};
  const models = d.models || {};
  const names = Object.keys(models);
  const wrap = el("div", {});

  wrap.appendChild(section(o.name || "조례",
    el("div", { class: "chip-row" },
      statusBadge(o.status),
      o.ord_kind ? badge(o.ord_kind, "badge-plain") : null,
      badge(o.region_name || o.sig_cd || "지자체 미상", "badge-info"),
      ...(o.categories || []).map((c) => badge(
        `${categoryName(c.code) || c.name || c.code} (${c.code}${c.confidence != null ? ` ${fx(c.confidence, 2)}` : ""})`,
        "badge-plain")),
      o.verification_status ? badge(o.verification_status, "badge-plain") : null
    ),
    el("div", { class: "kv-row" },
      el("span", { class: "kv-k", text: "제정" }), el("span", { class: "kv-v", text: ymd(o.enacted_on) }),
      el("span", { class: "kv-k", text: " 시행" }), el("span", { class: "kv-v", text: ymd(o.effective_on) }),
      el("span", { class: "kv-k", text: " 조문" }), el("span", { class: "kv-v", text: num(o.article_count) }),
      el("span", { class: "kv-k", text: " 원문" }), el("span", { class: "kv-v" }, extLink(o.url, "law.go.kr"))
    ),
    asOfLine(`ordinance_id ${o.ordinance_id || "?"}`)
  ));

  // 모델 탭
  const modelBar = el("div", { class: "chip-row" });
  const modelBody = el("div", {});
  const mb = new Map();
  const pick = (n) => {
    for (const [k, b] of mb) b.classList.toggle("active", k === n);
    modelBody.innerHTML = "";
    modelBody.appendChild(neighborTable(models[n], "ordinance"));
  };
  for (const n of names) {
    const m = models[n] || {};
    const ev = NEURAL_EVAL.byModel[n];
    const liftTxt = ev && ev.lift != null ? `  분야 ${(ev.category_agreement * 100).toFixed(0)}% / 기준선 ${ev.lift}배` : "";
    const b = el("button", { class: "btn", text: `${n} (dim ${m.dim ?? "?"})${liftTxt}`, onclick: () => pick(n) });
    mb.set(n, b);
    modelBar.appendChild(b);
  }
  const sec = section(`유사 조례 Top-${(d.method && d.method.top_k) || 10} — 모델 3종 비교`, modelBar, modelBody);
  // 기본 탭은 평가에서 가장 나은 모델로 연다(api/neural_eval.json 의 best_model).
  // 평가 파일이 없으면 종전대로 첫 모델을 연다.
  if (names.length) pick(names.includes(NEURAL_EVAL.best) ? NEURAL_EVAL.best : names[0]);
  else sec.appendChild(note("이 조례에 대한 모델 결과가 없습니다.", "warn"));
  wrap.appendChild(sec);

  const ev = evalPanel();
  if (ev) wrap.appendChild(ev);
  wrap.appendChild(agreementPanel(d));
  wrap.appendChild(methodPanel(d.method, env, path));
  return wrap;
}

/** 모델 1종의 이웃 표 + 저장본 대조 */
function neighborTable(m, kind) {
  if (!m) return note("모델 결과 없음", "warn");
  const box = el("div", {});
  const stored = m.stored_covered
    ? `저장본(neural_similarity) ${num(m.stored_rows)}행 중 ${num(m.stored_overlap)}건 일치 · 일치율 ${fx(m.stored_agreement, 3)}`
    : "저장본(neural_similarity)에 이 노드가 없다 — 재계산본만 있음";
  box.appendChild(el("div", { class: "as-of", text:
    `dim ${m.dim ?? "?"} · 후보 pool ${num(m.candidate_pool)} · ${stored}` }));
  if (m.stored_note) box.appendChild(note(m.stored_note, "small"));

  const rows = (m.neighbors || []).map((n) => {
    if (kind === "region") {
      return [
        n.rank,
        fx(n.cosine, 4),
        n.name || n.sig_cd,
        n.sig_cd,
        n.level ?? "—",
        n.stored_rank == null ? "저장본 없음" : `#${n.stored_rank}`,
      ];
    }
    return [
      n.rank,
      fx(n.cosine, 4),
      el("span", {}, extLink(n.url, n.name || n.key), document.createTextNode(" "),
        n.repealed ? badge("폐지", "badge-repealed") : null),
      n.region_name || "—",
      ymd(n.enacted_on),
      el("span", {}, ...(n.categories || []).flatMap((c) => {
        const b = badge(c, "badge-plain");
        b.title = categoryName(c) || c;
        return [b, document.createTextNode(" ")];
      })),
      n.stored_rank == null ? "저장본 없음" : `#${n.stored_rank}`,
    ];
  });
  if (!rows.length) { box.appendChild(note("이웃이 없습니다.", "warn")); return box; }
  box.appendChild(table(
    kind === "region"
      ? ["순위", "코사인", "지자체", "sig_cd", "level", "저장본 순위"]
      : ["순위", "코사인", "조례", "지자체", "제정일", "분류", "저장본 순위"],
    rows));
  return box;
}

/**
 * 모델 평가 공시.
 *
 * 절대 정확도만 보여주면 오독한다 — 조례 다수가 C01(행정) 같은 흔한 분야를 갖고 있어
 * 무작위로 이웃을 뽑아도 30%대가 나온다. 그래서 기준선을 나란히 놓고 배수(lift)를 본다.
 */
function evalPanel() {
  const models = Object.values(NEURAL_EVAL.byModel);
  if (!models.length) return null;
  const m0 = NEURAL_EVAL.method || {};
  const sec = section("모델 평가 — 이웃이 실제로 같은 분야인가",
    note("표본 " + num(m0.sample) + "건 × top-" + (m0.top_k ?? 10)
      + " 로 측정했다. 같은 후보 풀에서 이웃만 무작위로 바꾼 값이 기준선이고, "
      + "배수가 1.0 이면 무작위와 구별되지 않는다는 뜻이다."));
  const rows = models
    .slice()
    .sort((a, b) => (b.lift ?? 0) - (a.lift ?? 0))
    .map((m) => [
      m.model === NEURAL_EVAL.best
        ? el("span", {}, el("b", { text: m.model }), document.createTextNode(" "),
            badge("기본 선택", "badge-active"))
        : m.model,
      pct(m.category_agreement, 1),
      pct(m.random_baseline, 1),
      m.lift == null ? "—"
        : el("span", { class: m.lift >= 1.2 ? "badge badge-active" : "badge badge-warn",
                       text: `${m.lift.toFixed(2)}배` }),
      m.region_spread_mean == null ? "—" : `${m.region_spread_mean} 곳`,
      m.same_region_share == null ? "—" : pct(m.same_region_share, 1),
      num(m.n_sources),
    ]);
  sec.appendChild(table(
    ["모델", "분야 일치", "무작위 기준선", "기준선 대비", "이웃 지역 폭", "같은 지자체", "표본"], rows));
  if (m0.caveat) sec.appendChild(note(m0.caveat));
  sec.appendChild(note(
    "'이웃 지역 폭' 은 top-10 이 몇 개 서로 다른 지자체에서 왔는지다. "
    + "이 기능의 용도가 '다른 지자체는 이걸 어떻게 만들었나' 이므로 후보에서 같은 지자체는 제외한다 "
    + "— '같은 지자체' 열이 0% 인 이유다. 같은 시·군의 다른 조례는 지역 상세 화면에서 본다.", "info"));
  return sec;
}

function agreementPanel(d) {
  const ag = d.model_agreement || {};
  const pairs = Object.entries(ag);
  const cons = Array.isArray(d.consensus) ? d.consensus : [];
  const sec = section("모델 간 일치도",
    note("세 모델은 같은 그래프를 서로 다른 방식으로 본다 — node2vec 은 이웃 관계, "
      + "metapath2vec 은 '지역→조례→상위법' 같은 지정 경로, GraphSAGE 는 노드 속성까지 "
      + "함께 학습한다. 여러 모델이 같이 꼽은 이웃일수록 특정 학습 방식의 우연이 아닐 가능성이 높다."));
  if (pairs.length) {
    sec.appendChild(table(["모델쌍", "공통 이웃", "Jaccard", "비교 대상 수"],
      pairs.map(([k, v]) => [k.replace("|", "  ↔  "), num(v.overlap), fx(v.jaccard, 3), num(v.of)])));
  }
  if (cons.length) {
    sec.appendChild(el("h3", { text: `2개 이상 모델이 공통으로 꼽은 이웃 ${cons.length}건` }));
    sec.appendChild(table(["이웃", "지지 모델", "평균 코사인", "지자체"],
      cons.map((c) => [
        c.url ? el("span", {}, extLink(c.url, c.name || c.key || c.sig_cd)) : (c.name || c.key || c.sig_cd || "—"),
        Array.isArray(c.models) ? c.models.join(", ") : (c.support ?? "—"),
        fx(c.mean_cosine ?? c.avg_cosine, 4),
        c.region_name || c.sig_cd || "—",
      ])));
  } else {
    // consensus 0 은 두 가지 원인이 있고 구분해서 알려야 한다.
    // (1) 모델이 실제로 서로 다른 이웃을 꼽음 — 해석의 문제
    // (2) 이 조례에 대해 일부 모델의 kNN 이 아예 없음 — 데이터 커버리지 문제
    const covered = pairs.filter(([, v]) => (v.of ?? 0) > 0).length;
    sec.appendChild(note(
      covered === pairs.length && pairs.length
        ? "이 조례에서는 세 모델이 공통으로 꼽은 이웃이 없습니다. 모델마다 다른 축을 보고 있다는 뜻이므로, "
          + "아래 모델별 결과를 각각 보면 서로 다른 종류의 유사성을 확인할 수 있습니다."
        : "일부 모델에 이 조례의 이웃 계산 결과가 없어 비교가 성립하지 않습니다. "
          + "system/make_neural_full.py 로 kNN 을 전량 재계산하면 세 모델이 같은 조례 집합을 갖습니다.",
      "info"));
  }
  return sec;
}

function methodPanel(method, env, path) {
  const m = method || {};
  // 제목을 '한계'가 아니라 '방법 공시'로 둔다. 내용(재계산 범위·후보 pool·폐지 조례
  // 포함 여부)은 그대로 남긴다 — 이 값들은 결과 해석을 실제로 바꾸므로 지우지 않는다.
  const sec = section("산출 방법 공시");
  const rows = [
    ["top_k", m.top_k ?? "—"],
    ["유사도", m.similarity || "—"],
    ["재계산 범위", m.recomputed_over || m.candidate_restriction || "—"],
    ["재계산 이유", m.why_recomputed || "—"],
    ["후보 pool 상태", m.candidate_pool_status ? Object.entries(m.candidate_pool_status).map(([k, v]) => `${k}=${num(v)}`).join(" · ") : "—"],
    ["pool 내 폐지 조례", m.repealed_in_pool == null ? "—" : num(m.repealed_in_pool)],
  ].filter(([, v]) => v !== "—" || true);
  sec.appendChild(table(["항목", "값"], rows));
  if (m.repealed_note) sec.appendChild(note(m.repealed_note, "warn"));
  if (m.note) sec.appendChild(note(m.note));
  sec.appendChild(el("div", { class: "as-of", text: `데이터 소스: 전국 shard — ${path}` }));
  const foot = envelopeFooter(env);
  if (foot) sec.appendChild(foot);
  return sec;
}

/* ------------------------------------------------------------------ *
 * 탭 2 — 지자체 유사도 (신경망 vs 통계 peers 나란히)
 * ------------------------------------------------------------------ */

async function regionTab(idx, regs, query) {
  const wrap = el("div", {});
  if (!regs.length) {
    wrap.appendChild(note("색인에 지자체 shard 목록이 없습니다.", "warn"));
    return wrap;
  }
  const byCd = new Map(regs.map((r) => [String(r.sig_cd), r]));
  const covered = new Set(byCd.keys());

  let cat;
  try { cat = await loadRegionCatalog(); }
  catch (e) { cat = null; }

  const items = cat ? cat.items.slice() : [];
  const seen = new Set(items.map((i) => i.sig_cd));
  for (const cd of covered) {
    if (seen.has(cd)) continue;
    const r = byCd.get(cd);
    items.push({ sig_cd: cd, name: r.name || null, level: r.level ?? null,
                 sido: cat ? cat.sidoOf(cd) : null });
    seen.add(cd);
  }
  items.sort((a, b) => (a.sig_cd < b.sig_cd ? -1 : a.sig_cd > b.sig_cd ? 1 : 0));
  const sidoOf = cat ? cat.sidoOf : ((cd) => String(cd).slice(0, 2));

  const initial = (query.sig && covered.has(String(query.sig)) ? String(query.sig) : null)
    || (covered.has("47190") ? "47190" : [...covered][0]);

  const detail = el("div", {});
  wrap.appendChild(regionSelector({
    items, sidoOf, current: initial, covered,
    onChange: (sig) => draw(sig),
    coveredLabel: "신경망 shard 있는 곳만", coveredWord: "신경망",
  }));
  wrap.appendChild(el("div", { class: "as-of", text:
    `신경망 지자체 shard ${covered.size}곳 · 선택 가능 ${items.length}곳` }));
  wrap.appendChild(detail);

  let token = 0;
  async function draw(sig) {
    const my = ++token;
    detail.innerHTML = "";
    detail.appendChild(loading(`${sig} 의 신경망 이웃과 통계 유사지자체를 불러오는 중…`));
    const meta = byCd.get(String(sig));
    const rel = (meta && meta.path) || `region/${sig}.json`;
    const [env, peersRes] = await Promise.all([
      loadShard(rel).catch(() => null),
      loadRegionalShard("peers", sig).catch(() => ({ data: null, missing: true })),
    ]);
    if (my !== token) return;
    detail.innerHTML = "";
    if (!env) {
      detail.appendChild(el("div", { class: "panel panel-warn" },
        el("h2", { class: "panel-title", text: "이 지자체는 신경망 shard 가 없습니다" }),
        el("p", { text: `${DIR}/${rel} 이 없습니다. 선택기에서 ✓ 표시된 곳을 고르거나 생성기를 다시 돌리세요.` }),
        el("p", { class: "hint" }, el("code", { text: `python ${GENERATOR}` }))));
      return;
    }
    detail.appendChild(regionBody(env, peersRes, `${DIR}/${rel}`));
  }

  await draw(initial);
  return wrap;
}

/* ------------------------------------------------------------------ *
 * 탭 3 — 지역 전량 (api/neural/by-region/{sig_cd}.json)
 *
 *   조례 유사도 탭은 400건 표본이다. 이 탭은 저장본 전량을 지역 단위로 연다.
 *   파일은 용량 때문에 노드를 한 번만 싣고(nodes[]) 엣지는 그 배열의 정수 인덱스로
 *   가리킨다. 규약은 파일 안의 data.node_fields / data.edge_fields 에 들어 있다.
 * ------------------------------------------------------------------ */

async function fullRegionTab(query) {
  const wrap = el("div", {});
  const idxEnv = await maybe(FULL_REGION_INDEX);
  if (!idxEnv) {
    wrap.appendChild(el("div", { class: "panel panel-warn" },
      el("h2", { class: "panel-title", text: "지역 전량 신경망 묶음이 없습니다" }),
      el("p", { text: `${FULL_REGION_INDEX} 를 찾지 못했습니다.` }),
      el("p", { class: "hint" }, el("code", {
        text: "python system/make_full_vote_neural.py --only neural --neural-topk 10" }))));
    return wrap;
  }
  const d = idxEnv.data || idxEnv;
  const regions = Array.isArray(d.regions) ? d.regions : [];
  const totals = d.totals || {};
  if (!regions.length) {
    wrap.appendChild(note("색인에 지역 목록이 없습니다.", "warn"));
    return wrap;
  }

  wrap.appendChild(el("div", { class: "stat-grid" },
    statCard("지역 묶음", num(regions.length), "조례를 보유한 지역 전량(교육청·폐지 지자체 포함)"),
    statCard("조례 노드", num(totals.src_in_shards ?? totals.src_covered ?? 0), "유사도 저장본이 있는 조례"),
    statCard("유사도 엣지", num(totals.edges_in_shards ?? totals.similarity_rows_total ?? 0),
      `Top-${totals.top_k ?? 10} · neural_similarity 전량`),
    statCard("모델", num((totals.models || []).length || 3),
      (totals.models || []).join(" · ") || "graphsage · metapath2vec · node2vec")));

  wrap.appendChild(note(
    "여기 순위는 그래프 구조 임베딩의 코사인이다. 조문 의미 유사도가 아니고 선례 추천도 아니다. "
    + "폐지 조례가 이웃으로 나올 수 있으니 상태 배지를 반드시 확인할 것.", "warn"));

  const byCd = new Map(regions.map((r) => [String(r.key), r]));
  let cat = null;
  try { cat = await loadRegionCatalog(); } catch (e) { cat = null; }
  const sidoOf = cat ? cat.sidoOf : ((cd) => String(cd).slice(0, 2));
  const covered = new Set(byCd.keys());
  const items = [...covered].sort().map((cd) => {
    const r = byCd.get(cd);
    const fromCat = cat ? cat.items.find((i) => i.sig_cd === cd) : null;
    return { sig_cd: cd, name: (fromCat && fromCat.name) || r.name || cd,
             level: fromCat ? fromCat.level : null, sido: sidoOf(cd) };
  });

  const initial = (query.fullsig && covered.has(String(query.fullsig)) ? String(query.fullsig) : null)
    || (covered.has("52180") ? "52180" : items[0].sig_cd);

  const detail = el("div", {});
  wrap.appendChild(regionSelector({
    items, sidoOf, current: initial, covered,
    onChange: (sig) => draw(sig),
    coveredLabel: "전량 묶음이 있는 곳만", coveredWord: "신경망 전량",
  }));
  wrap.appendChild(detail);

  let token = 0;
  async function draw(sig) {
    const my = ++token;
    detail.innerHTML = "";
    detail.appendChild(loading(`${sig} 의 조례 유사도 전량을 불러오는 중…`));
    const meta = byCd.get(String(sig));
    const rel = "api/" + ((meta && meta.path) || `neural/by-region/${sig}.json`);
    const env = await maybe(rel);
    if (my !== token) return;
    detail.innerHTML = "";
    if (!env) {
      detail.appendChild(note(`${rel} 이 없습니다.`, "warn"));
      return;
    }
    detail.appendChild(fullRegionBody(env, rel));
  }
  await draw(initial);
  return wrap;
}

function fullRegionBody(env, path) {
  const d = env.data || {};
  const r = d.region || {};
  const cov = d.coverage || {};
  const nodeFields = d.node_fields || ["ordinance_id", "name", "sig_cd", "repealed_on", "status"];
  // 필드 이름은 번들 판본에 따라 src/src_index 두 가지가 다 나온다. 둘 다 받는다.
  const edgeFields = d.edge_fields || ["src", "dst", "sim", "rank"];
  const at = (...names) => {
    for (const n of names) { const i = edgeFields.indexOf(n); if (i >= 0) return i; }
    return -1;
  };
  const iSrc = at("src", "src_index");
  const iDst = at("dst", "dst_index");
  const iSim = at("sim", "cosine_sim");
  const iRank = at("rank");
  const fIdx = Object.fromEntries(nodeFields.map((f, i) => [f, i]));
  const urlTpl = d.url_template || null;

  const nodes = d.nodes || [];
  const node = (i) => {
    const n = nodes[i];
    if (!n) return null;
    const o = {};
    for (const [f, k] of Object.entries(fIdx)) o[f] = n[k];
    if (urlTpl && o.ordinance_id) {
      o.url = urlTpl.replace("{mst}", String(o.ordinance_id).split(":").pop());
    }
    return o;
  };

  const wrap = el("div", {});
  wrap.appendChild(section(`${r.full_name || r.name || d.sig_cd} — 조례 유사도 전량`,
    el("div", { class: "chip-row" },
      badge(`sig_cd ${d.sig_cd}`, "badge-info"),
      badge(`Top-${d.top_k ?? 10}`, "badge-plain")),
    asOfLine(`데이터 소스: ${path}`)));

  wrap.appendChild(el("div", { class: "stat-grid" },
    statCard("이 지역 조례", num(cov.ordinances_in_region || 0), "자치법규 전량"),
    statCard("유사도 있는 조례", num(cov.src_covered || 0),
      `없는 것 ${num(cov.src_missing || 0)}건 — 임베딩 학습 대상에서 빠진 조례`),
    statCard("노드", num(cov.nodes ?? nodes.length), "이웃으로 등장하는 조례 포함(타 지역 포함)"),
    statCard("엣지", num(cov.edges ?? 0),
      Object.entries(cov.edges_by_model || {}).map(([k, v]) => `${k.replace("-numpy", "")} ${num(v)}`).join(" · "))));

  if (cov.src_missing) {
    wrap.appendChild(note(
      `이 지역 조례 ${num(cov.ordinances_in_region || 0)}건 중 ${num(cov.src_missing)}건은 `
      + "임베딩 유사도가 계산되지 않았다(그래프에서 고립됐거나 학습 대상 밖). 여기 목록에 나오지 않는다.", "warn"));
  }

  // 지역-지역 유사도(같은 번들 안에 동봉되어 있다)
  const rn = d.region_neighbors || {};
  const rnModels = Object.keys(rn);
  if (rnModels.length) {
    const det = el("details", {}, el("summary", { text: `지자체끼리의 유사도 (${rnModels.length}개 모델)` }));
    for (const m of rnModels) {
      det.appendChild(el("h4", { style: "margin:8px 0 4px", text: m }));
      det.appendChild(table(["순위", "코사인", "지자체", "sig_cd"],
        (rn[m] || []).map((n) => [n.rank, fx(n.sim ?? n.cosine_sim, 4), n.name || "—", n.sig_cd])));
    }
    wrap.appendChild(det);
  }

  // 모델별 · 기준조례별 이웃
  const models = Object.keys(d.edges || {});
  if (!models.length) {
    wrap.appendChild(note("이 지역에는 유사도 엣지가 없습니다.", "warn"));
    wrap.appendChild(envelopeFooter(env));
    return wrap;
  }

  const modelSel = el("select", { class: "sel", "aria-label": "모델 선택" });
  for (const m of models) modelSel.appendChild(el("option", { value: m, text: m }));
  const srcSel = el("select", { class: "sel sel-wide", "aria-label": "기준 조례 선택" });
  const filter = el("input", { class: "search-input", type: "search", placeholder: "조례명 검색",
                               "aria-label": "기준 조례 검색" });
  const count = el("span", { class: "muted small" });
  const out = el("div", {});

  function srcListFor(model) {
    const seen = new Map();
    for (const e of d.edges[model] || []) {
      const si = e[iSrc];
      if (!seen.has(si)) seen.set(si, 0);
      seen.set(si, seen.get(si) + 1);
    }
    return [...seen.entries()]
      .map(([i, n]) => ({ i, n, node: node(i) }))
      .filter((x) => x.node)
      .sort((a, b) => String(a.node.name || "").localeCompare(String(b.node.name || ""), "ko"));
  }

  let srcList = [];
  function buildSrc() {
    const q = filter.value.trim().toLowerCase();
    srcSel.innerHTML = "";
    let shown = 0;
    for (const x of srcList) {
      if (q && !String(x.node.name || "").toLowerCase().includes(q)) continue;
      srcSel.appendChild(el("option", { value: String(x.i),
        text: `${x.node.name || x.node.ordinance_id} (이웃 ${x.n})` }));
      shown++;
    }
    if (!shown) srcSel.appendChild(el("option", { value: "", text: "검색 결과 없음", disabled: "disabled" }));
    count.textContent = `${shown}/${srcList.length}건 표시`;
  }

  function drawNeighbors() {
    out.innerHTML = "";
    const model = modelSel.value;
    const si = parseInt(srcSel.value, 10);
    if (Number.isNaN(si)) return;
    const src = node(si);
    const rows = (d.edges[model] || [])
      .filter((e) => e[iSrc] === si)
      .sort((a, b) => (a[iRank] || 0) - (b[iRank] || 0))
      .map((e) => {
        const t = node(e[iDst]) || {};
        const repealed = !!t.repealed_on || t.status === "repealed";
        return [
          e[iRank],
          fx(e[iSim], 4),
          el("span", {}, extLink(t.url, t.name || t.ordinance_id || "?"),
            document.createTextNode(" "),
            repealed ? badge("폐지", "badge-repealed") : null),
          t.sig_cd || "—",
          t.repealed_on ? ymd(t.repealed_on) : "—",
        ];
      });
    out.appendChild(el("h4", { style: "margin:10px 0 4px" },
      document.createTextNode(src ? (src.name || src.ordinance_id) : "?"),
      document.createTextNode(" "),
      src && (src.repealed_on || src.status === "repealed") ? badge("폐지", "badge-repealed") : null));
    if (src && src.url) out.appendChild(el("div", { class: "as-of" }, extLink(src.url, "law.go.kr 원문")));
    out.appendChild(table(["순위", "코사인", "유사 조례", "지자체", "폐지일"], rows));
  }

  modelSel.addEventListener("change", () => { srcList = srcListFor(modelSel.value); buildSrc(); drawNeighbors(); });
  srcSel.addEventListener("change", drawNeighbors);
  filter.addEventListener("input", debounce(() => { buildSrc(); drawNeighbors(); }, 150));

  wrap.appendChild(el("div", { class: "toolbar" },
    el("label", { text: "모델 " }), modelSel,
    el("label", { text: " 기준 조례 " }), srcSel, filter, count));
  wrap.appendChild(out);

  srcList = srcListFor(modelSel.value);
  buildSrc();
  drawNeighbors();

  wrap.appendChild(envelopeFooter(env));
  return wrap;
}

function regionBody(env, peersRes, path) {
  const d = env.data || {};
  const r = d.region || {};
  const models = d.models || {};
  const names = Object.keys(models);
  const wrap = el("div", {});

  wrap.appendChild(section(`${r.full_name || r.name || r.sig_cd} — 신경망 유사 지자체`,
    el("div", { class: "chip-row" },
      badge(`sig_cd ${r.sig_cd}`, "badge-info"),
      r.level != null ? badge(`level ${r.level}`, "badge-plain") : null),
    asOfLine(`region_id ${r.region_id || "?"}`)));

  const left = el("div", {});
  const modelBar = el("div", { class: "chip-row" });
  const modelBody = el("div", {});
  const mb = new Map();
  const pick = (n) => {
    for (const [k, b] of mb) b.classList.toggle("active", k === n);
    modelBody.innerHTML = "";
    modelBody.appendChild(neighborTable(models[n], "region"));
  };
  for (const n of names) {
    const m = models[n] || {};
    const ev = NEURAL_EVAL.byModel[n];
    const liftTxt = ev && ev.lift != null ? `  분야 ${(ev.category_agreement * 100).toFixed(0)}% / 기준선 ${ev.lift}배` : "";
    const b = el("button", { class: "btn", text: `${n} (dim ${m.dim ?? "?"})${liftTxt}`, onclick: () => pick(n) });
    mb.set(n, b);
    modelBar.appendChild(b);
  }
  left.appendChild(el("h3", { text: "① 그래프 구조 기반 (신경망 임베딩)" }));
  left.appendChild(modelBar);
  left.appendChild(modelBody);
  if (names.length) pick(names[0]);
  else left.appendChild(note("모델 결과 없음", "warn"));

  const right = el("div", {});
  right.appendChild(el("h3", { text: "② 지역특성 기반 (analytics.peers · 통계)" }));
  right.appendChild(peersBlock(peersRes));

  wrap.appendChild(section("두 축 나란히 비교",
    note((d.method && d.method.note)
      || "신경망 이웃은 그래프 구조(조례·예산·인접·분류 연결)를, 통계 유사지자체는 인구·면적·재정자립도 등 지역특성을 본다. "
      + "두 목록이 다른 것은 오류가 아니라 축이 다르기 때문이다."),
    el("div", { class: "two-col" }, left, right)));

  wrap.appendChild(methodPanel(d.method, env, path));
  return wrap;
}

function peersBlock(res) {
  if (!res || !res.data) {
    return note("이 지자체의 통계 기반 유사지자체(api/peers/{sig}.json)가 현재 번들에 없습니다. "
      + "python system/make_nationwide.py --only peers 로 생성합니다.", "warn");
  }
  const d = res.data;
  const peers = Array.isArray(d.peers) ? d.peers : [];
  const box = el("div", {});
  const t = d.target || {};
  const ind = t.indicators || {};
  box.appendChild(el("div", { class: "as-of", text:
    `기준: 인구 ${num(ind.population)} · 재정자립도 ${pct(ind.fiscal_self_ratio)} · 복지비중 ${pct(ind.welfare_ratio)}` }));
  if (!peers.length) { box.appendChild(note("유사 지자체 결과가 비어 있습니다.", "warn")); return box; }
  box.appendChild(table(["순위", "유사도", "지자체", "정책프로필 코사인", "인구", "재정자립도"],
    peers.slice(0, 10).map((p, i) => [
      i + 1,
      fx(p.similarity, 4),
      p.name || p.sig_cd,
      fx(p.policy_profile_cosine, 4),
      num(p.indicators && p.indicators.population),
      pct(p.indicators && p.indicators.fiscal_self_ratio),
    ])));
  const sl = sourceLine(res);
  if (sl) box.appendChild(sl);
  return box;
}

/* ------------------------------------------------------------------ *
 * 탭 3 — 모델 품질
 * ------------------------------------------------------------------ */

async function qualityTab() {
  const wrap = el("div", {});
  let env;
  try { env = await loadQuality(); }
  catch (e) { wrap.appendChild(errorPanel(e, `${DIR}/quality.json 로드 실패`)); return wrap; }
  if (!env) { wrap.appendChild(missingPanel(new DataMissingError(`${DIR}/quality.json`, 404))); return wrap; }

  const d = env.data || {};
  const models = d.models || {};
  const names = Object.keys(models);

  wrap.appendChild(section("임베딩이 실제로 학습됐는가",
    note("무작위 조례쌍의 코사인 평균이 0 근처이고 분산이 크며 유효차원이 높을수록 변별력이 좋다. "
      + "평균이 1 에 붙고 유효차원이 1 에 가까우면 임베딩이 붕괴(collapse)한 것이다."),
    el("div", { class: "banner banner-est", role: "note" },
      el("span", { class: "banner-tag", text: "in-sample" }),
      el("span", { class: "banner-body", text:
        "아래 분리 AUC 는 학습에 쓰인 엣지(IN_CATEGORY·HAS_ORDINANCE)로 계산한 in-sample 재현 지표다. "
        + "일반화 성능(held-out AUC)이 아니며 그렇게 불러서도 안 된다." }))
  ));

  // 요약 표 — 모델 × 노드종류
  const rows = [];
  for (const n of names) {
    const m = models[n] || {};
    for (const [kind, k] of Object.entries(m.kinds || {})) {
      const rp = k.random_pair_cosine || {};
      rows.push([
        n, kind, k.dim ?? "—", num(k.n_nodes_sampled),
        fx(rp.mean), fx(rp.std), fx(rp.p50), fx(rp.p99), fx(rp.max),
        rp.frac_gt_0999 == null ? "—" : fx(rp.frac_gt_0999, 5),
        k.effective_dim == null ? "—" : fx(k.effective_dim, 1),
        k.category_separation_auc ? fx(k.category_separation_auc.auc, 4) : "—",
        k.region_separation_auc ? fx(k.region_separation_auc.auc, 4) : "—",
      ]);
    }
  }
  const qsec = section("모델 품질 지표 (무작위쌍 코사인 분포 · 분리 AUC)",
    table(["모델", "노드", "dim", "표본", "평균", "표준편차", "p50", "p99", "최대",
           ">0.999 비율", "유효차원", "카테고리 AUC", "지자체 AUC"], rows));

  const ps = d.pair_sampling || {};
  if (ps.method) {
    qsec.appendChild(el("div", { class: "as-of", text:
      `표본추출 ${ps.method} · 노드 ${num(ps.sample_size)} · 쌍 ${num(ps.pairs)} · seed ${ps.seed ?? "?"}` }));
    if (ps.why) qsec.appendChild(note(ps.why, "small"));
  }
  wrap.appendChild(qsec);

  // 차트 — AUC 대비 랜덤 기준선 0.5
  const chartBox = el("div", { class: "chart-box chart-box-sm" }, el("canvas", { id: "neural-auc-chart" }));
  const chartSec = section("분리 AUC — 랜덤 기준선 0.5 대비", chartBox);
  wrap.appendChild(chartSec);
  try {
    await ensureChart();
    whenMounted(chartBox, () => drawAucChart(chartBox.querySelector("canvas"), models));
  } catch (e) {
    chartBox.remove();
    chartSec.appendChild(cdnFailPanel("Chart.js(차트)", e,
      table(["모델", "카테고리 분리 AUC", "지자체 분리 AUC", "랜덤 기준선"],
        names.map((n) => {
          const o = ((models[n] || {}).kinds || {}).Ordinance || {};
          return [n,
            o.category_separation_auc ? fx(o.category_separation_auc.auc, 4) : "—",
            o.region_separation_auc ? fx(o.region_separation_auc.auc, 4) : "—",
            "0.5"];
        }))));
  }

  // held-out AUC — 값이 없다는 사실과 사유를 그대로 노출한다(빈칸 금지)
  const hsec = section("held-out AUC (일반화 성능)");
  for (const n of names) {
    const m = models[n] || {};
    hsec.appendChild(el("div", { class: "card" },
      el("div", { class: "card-head" },
        el("span", { class: "card-title", text: n }),
        m.held_out_auc == null
          ? badge(`산출 불가 (${m.held_out_auc_status || "unavailable"})`, "badge-warn")
          : badge(fx(m.held_out_auc, 4), "badge-verified")),
      el("p", { class: "small", text: m.held_out_auc_reason || "사유 표기 없음" })));
  }
  wrap.appendChild(hsec);

  // 모델 메타
  const msec = section("모델 메타 (DB node_embeddings / neural_similarity 실측)");
  msec.appendChild(table(
    ["모델", "dim", "노드 수", "유사도 저장행", "학습 시각", "알고리즘", "지도학습"],
    names.map((n) => {
      const meta = (models[n] || {}).meta || {};
      const alg = meta.algorithm || {};
      return [n, meta.dim ?? "—", num(meta.nodes_total), num(meta.similarity_rows),
              meta.computed_at || "—", alg.family || "—", alg.supervision || "—"];
    })));
  for (const n of names) {
    const meta = (models[n] || {}).meta || {};
    const alg = meta.algorithm || {};
    if (!alg.note && !meta.nodes_by_kind) continue;
    msec.appendChild(el("details", {},
      el("summary", { text: `${n} — 구현 주석 · 노드 종류별 수` }),
      alg.impl ? el("p", { class: "small", text: `impl: ${alg.impl}` }) : null,
      alg.note ? el("p", { class: "small", text: alg.note }) : null,
      meta.nodes_by_kind
        ? table(["노드 종류", "수"], Object.entries(meta.nodes_by_kind).map(([k, v]) => [k, num(v)]))
        : null));
  }
  wrap.appendChild(msec);

  const foot = envelopeFooter(env);
  if (foot) wrap.appendChild(foot);
  const warns = Array.isArray(d.warnings) ? d.warnings : [];
  if (warns.length) wrap.appendChild(note(`경고 ${warns.length}건: ${warns.join(" / ")}`, "warn"));
  return wrap;
}

let aucChart = null;

function drawAucChart(canvas, models) {
  if (!canvas || !window.Chart) return;
  const names = Object.keys(models);
  const catAuc = [];
  const regAuc = [];
  for (const n of names) {
    const o = ((models[n] || {}).kinds || {}).Ordinance || {};
    catAuc.push(o.category_separation_auc ? o.category_separation_auc.auc : null);
    regAuc.push(o.region_separation_auc ? o.region_separation_auc.auc : null);
  }
  if (aucChart) { try { aucChart.destroy(); } catch (e) { /* noop */ } aucChart = null; }
  aucChart = new window.Chart(canvas, {
    type: "bar",
    data: {
      labels: names,
      datasets: [
        { label: "카테고리 분리 AUC (in-sample)", data: catAuc, backgroundColor: "#8ab4e2" },
        { label: "지자체 분리 AUC (in-sample)", data: regAuc, backgroundColor: "#2c66a8" },
        { label: "랜덤 기준선 0.5", data: names.map(() => 0.5), type: "line",
          borderColor: "#d7301f", borderDash: [5, 4], pointRadius: 0, fill: false },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { y: { min: 0.4, max: 1.0, title: { display: true, text: "AUC" } } },
      plugins: { legend: { position: "bottom" } },
    },
  });
}
