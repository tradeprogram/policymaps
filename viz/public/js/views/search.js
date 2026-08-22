// 9. 검색 — 질의별 shard api/search/{slug}.json (목록은 api/search_index.json + api/index.json)
//    폴백: api/search.json 단일 1건. 조문 단위 결과 카드.
//    ★ 랭킹에 그래프 확장을 섞지 않는다(15 문서에서 무익함이 실증됨). 연결관계는 카드 하단에만.
import { el, num, extLink, debounce } from "../util.js";
import { loadFixture, loadCatalog, loadCatalogItem, state, categoryName } from "../api.js";
import { section, table, note, loading, asOfLine, fixtureMissingPanel, badge,
         statusBadge, verificationBadge, envelopeFooter } from "../components.js";
import { catalogSelector, sourceLine } from "../nationwide.js";
import { go } from "../router.js";

export async function render(root, params, query) {
  root.appendChild(loading("사전계산 질의 목록을 불러오는 중…"));

  let entries = [];
  try { entries = await loadCatalog("search"); } catch (e) { entries = []; }
  // 카테고리 코드가 있으면 라벨에 붙여 고르기 쉽게 한다.
  for (const e of entries) {
    const cc = e.meta && e.meta.category_code;
    if (cc && !String(e.label).includes(cc)) e.label = `${e.label} · ${cc} ${categoryName(cc) || ""}`.trim();
  }
  root.innerHTML = "";

  const initial = (query && query.q && entries.some((e) => e.key === String(query.q)))
    ? String(query.q)
    : (entries.length ? entries[0].key : null);

  const picker = catalogSelector({
    entries, current: initial, label: "사전계산 질의",
    onChange: (k) => draw(k),
  });
  if (picker) root.appendChild(picker);

  const body = el("div", {});
  root.appendChild(body);

  let token = 0;
  async function draw(key) {
    const my = ++token;
    body.innerHTML = "";
    body.appendChild(loading("검색 결과를 불러오는 중…"));
    const entry = entries.find((e) => e.key === String(key)) || null;

    let res;
    if (entry) res = await loadCatalogItem("search", entry);
    else {
      try { const env = await loadFixture("search"); res = { env, data: env.data || env, source: "fixture", path: "api/search.json" }; }
      catch (e) { res = { env: null, data: null, error: e }; }
    }
    if (my !== token) return;
    body.innerHTML = "";
    if (!res.data) { body.appendChild(fixtureMissingPanel("search", res.error)); return; }
    renderBody(body, res.data, res.env, res, entries.length);
  }

  await draw(initial);
}

function renderBody(root, d, env, res, catalogSize) {
  const all = d.results || [];

  const input = el("input", {
    type: "search", class: "search-input", value: "",
    placeholder: "결과 내 필터 (조례명·조문·기관)",
  });
  const counter = el("span", { class: "muted" });
  const list = el("div", { class: "result-list" });

  const sec = section("조문 검색",
    asOfLine(`engine=${d._engine || "?"} · scope=${d.scope || "?"} · hops=${d.hops ?? "?"}`),
    el("div", { class: "toolbar" },
      el("label", { text: "질의 " }),
      el("code", { class: "query-chip", text: d.query || "—" }),
      el("span", { class: "muted", text: ` · k=${d.k ?? "?"} · 결과 ${num(d.count ?? all.length)}건` })),
    note(state.isMock
      ? "가상데이터에는 사전계산된 질의 1건의 결과만 들어 있다. 아래 입력창은 그 결과 안에서의 필터다. "
        + "실제 검색은 MCP tool semantic_search_ordinance / search_ordinance 가 처리한다."
      : `정적 번들에는 RAG 인덱스(9.5GB)가 포함되지 않는다. 대신 사전계산 질의 ${num(catalogSize)}건의 `
        + "결과를 그대로 실었다(조문 원본은 ordinance_articles 236만 행). "
        + "임의 질의는 MCP tool semantic_search_ordinance / search_ordinance 가 수행한다."),
    el("div", { class: "toolbar" }, input, counter),
    list
  );
  root.appendChild(sec);

  if (d.verification_summary) {
    const vs = d.verification_summary;
    sec.insertBefore(el("div", { class: "chip-row" },
      badge(`원문 확보 ${num(vs.verified)}건`, "badge-verified"),
      badge(`미확보 ${num(vs.unverified)}건`, vs.unverified ? "badge-warn" : "badge-plain"),
      ...Object.entries(vs.by_status || {}).map(([k, v]) => badge(`${k}: ${v}`, "badge-plain"))
    ), list);
  }

  function paint() {
    const q = input.value.trim().toLowerCase();
    const rows = q
      ? all.filter((r) => [r.parent_name, r.article_title, r.text, r.org_name, r.article_no]
          .some((x) => String(x || "").toLowerCase().includes(q)))
      : all;
    counter.textContent = `${rows.length} / ${all.length}건 표시`;
    list.innerHTML = "";
    if (!rows.length) { list.appendChild(note("일치하는 결과가 없습니다.", "warn")); return; }
    for (const r of rows) list.appendChild(resultCard(r));
  }

  input.addEventListener("input", debounce(paint, 150));
  paint();

  const src = sourceLine(res);
  if (src) root.appendChild(src);
  root.appendChild(envelopeFooter(env));
}

function resultCard(r) {
  const card = el("div", { class: `card result-card ${r.status === "repealed" ? "card-caution" : ""}` });

  card.appendChild(el("div", { class: "card-head" },
    el("div", {},
      el("span", { class: "rank", text: `#${r.rank ?? "?"}` }),
      el("h3", { class: "card-title inline", text: r.parent_name || r.parent_id })),
    el("div", { class: "chip-row" },
      r.status ? statusBadge(r.status) : null,
      verificationBadge(r.verification_status),
      r.org_name ? badge(r.org_name, "badge-plain") : null,
      r.method ? badge(r.method, "badge-info") : null
    )));

  if (r.status === "repealed") {
    card.appendChild(el("div", { class: "caution" },
      el("b", { text: "⚠ 폐지된 조례 — " }),
      document.createTextNode("현행 근거로 인용하면 안 된다.")));
  }

  card.appendChild(el("div", { class: "article" },
    el("div", { class: "article-head" },
      el("b", { text: `${r.article_no || ""} ${r.article_title ? `(${r.article_title})` : ""}`.trim() }),
      el("span", { class: "muted small", text: ` · ${r.doc_key || ""}` })),
    el("p", { class: "article-text", text: r.text || "(본문 없음)" })
  ));

  // 점수 근거를 감추지 않는다 — 하이브리드 랭킹의 구성요소를 그대로 보여준다
  card.appendChild(el("div", { class: "chip-row score-row" },
    scoreChip("최종점수", r.score, 6),
    scoreChip("BM25", r.bm25_score, 3, r.bm25_rank),
    scoreChip("dense", r.dense_score, 4, r.dense_rank),
    r.article_hits ? badge(`매칭 조문 ${r.article_hits}개`, "badge-plain") : null,
    r.doc_len ? badge(`길이 ${num(r.doc_len)}자`, "badge-plain") : null
  ));

  if ((r.matched_articles || []).length > 1) {
    card.appendChild(el("details", {},
      el("summary", { text: `같은 조례의 매칭 조문 ${r.matched_articles.length}개` }),
      table(["조문", "제목", "점수"],
        r.matched_articles.map((a) => [a.article_no, a.article_title, (a.score ?? 0).toFixed(6)]))));
  }

  const foot = el("div", { class: "card-foot" });
  if (r.region_id) {
    foot.appendChild(el("button", { class: "btn-link", text: `지역 상세 (${r.region_id})`,
      onclick: () => go(`/region/${r.region_id}`) }));
  }
  if (r.official_url) foot.appendChild(extLink(r.official_url, "law.go.kr 원문"));
  if (r.content_hash) foot.appendChild(el("span", { class: "muted small", text: r.content_hash.slice(0, 23) + "…" }));
  card.appendChild(foot);

  return card;
}

function scoreChip(label, value, digits, rank) {
  if (value === null || value === undefined) return null;
  const txt = `${label} ${Number(value).toFixed(digits)}` + (rank ? ` (순위 ${rank})` : "");
  return badge(txt, "badge-plain");
}
