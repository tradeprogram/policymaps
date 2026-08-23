// 15. 조례 상세 — 완전판(로컬 DB)이면 조문 '본문'까지, 배포본이면 조문 '제목'까지.
//
//  경로: #/ordinance/{mst 또는 ordin:mst}?sig={sig_cd}
//  데이터 경로 두 갈래
//    A) 완전판 : GET /api/db/ordinance/{id}  → 조문 본문 + 근거 상위법 + 분류 + 예산연결 + 연혁
//    B) 배포본 : api/ordinance/{sig}.json(columnar-v1) + api/ordinance/articles/{sig}.json
//               조문 본문은 없다(236만 행 490MB — 정적 배포 불가). 그 사실을 화면에 명시한다.
//  sig 가 없으면 api/graph/ordinance/{mst}.json 의 seed.sig_cd 로 한 번 더 시도한다.
import { el, num, ymd, extLink } from "../util.js";
import { full, fullGet, getJSON, loadOrdinanceBundle, loadArticleTitles,
         categoryName, DataMissingError } from "../api.js";
import { section, table, note, loading, badge, statusBadge, verificationBadge,
         errorPanel, envelopeFooter, asOfLine } from "../components.js";
import { go } from "../router.js";

/** 'ordin:123' / '123' / 'ordinance:ordin:123' 어느 쪽이 와도 (id, mst) 로 */
function idOf(raw) {
  let s = String(raw || "").trim();
  if (s.startsWith("ordinance:")) s = s.slice("ordinance:".length);
  const mst = s.startsWith("ordin:") ? s.slice("ordin:".length) : s;
  return { id: `ordin:${mst}`, mst };
}

export async function render(root, params, query) {
  const { id, mst } = idOf(params.id);
  const sigHint = query && query.sig ? String(query.sig) : null;
  root.appendChild(loading(`${id} 을(를) 불러오는 중…`));

  let view = null;
  let err = null;

  if (full.enabled) {
    try {
      view = await fromFullApi(id);
    } catch (e) {
      err = e;   // 완전판이 실패해도 정적 폴백을 시도한다
    }
  }
  if (!view) {
    try {
      view = await fromStatic(mst, sigHint);
    } catch (e) {
      err = err || e;
    }
  }

  root.innerHTML = "";
  if (!view) {
    root.appendChild(errorPanel(err || new Error("데이터 없음"),
      `조례 ${id} 를 찾지 못했습니다. `
      + (full.enabled
        ? "완전판 API 가 이 id 를 모르는 경우입니다."
        : "배포본은 지역 번들에서 찾기 때문에 지역 코드가 필요합니다 — 목록에서 들어오면 ?sig= 가 붙습니다. "
          + "조문 본문까지 보려면 로컬에서 `python viz/serve_full.py` 를 띄우고 ?full=1 로 여세요.")));
    root.appendChild(backRow(sigHint));
    return;
  }
  draw(root, view, sigHint);
}

/* ---------------- A) 완전판 ---------------- */

async function fromFullApi(id) {
  const env = await fullGet(`ordinance/${encodeURIComponent(id)}`);
  const d = (env && env.data) || null;
  if (!d || !d.ordinance) return null;
  const o = d.ordinance;
  return {
    mode: "full",
    env,
    ord: o,
    region: d.region,
    sig: (d.region && d.region.sig_cd) || null,
    articles: (d.articles || []).map((a) => ({
      article_no: a.article_no, title: a.title, body: a.body,
    })),
    hasBodies: true,
    articleBodyChars: d.article_body_chars ?? null,
    legalBasis: d.legal_basis || [],
    citations: d.citations || [],
    categories: d.categories || [],
    budgetLinks: d.budget_links || [],
    budgetLinkCount: d.budget_link_count ?? (d.budget_links || []).length,
    versions: d.versions || [],
    changeLog: d.change_log || [],
    neural: d.neural_similar || [],
    appendices: d.appendices || [],
  };
}

/* ---------------- B) 배포본(정적 shard) ---------------- */

async function findSig(mst, sigHint) {
  if (sigHint) return sigHint;
  // 사전계산된 조례 그래프 shard(1,000건)에 seed.sig_cd 가 들어 있다.
  try {
    const env = await getJSON(`api/graph/ordinance/${mst}.json`);
    const seed = ((env && env.data) || {}).seed || {};
    if (seed.sig_cd) return String(seed.sig_cd);
  } catch (e) {
    if (!(e instanceof DataMissingError)) throw e;
  }
  return null;
}

async function fromStatic(mst, sigHint) {
  const sig = await findSig(mst, sigHint);
  if (!sig) return null;
  const bundle = await loadOrdinanceBundle(sig);
  const ord = bundle.items.find((x) => String(x.mst) === String(mst));
  if (!ord) return null;

  let articles = [];
  try {
    const at = await loadArticleTitles(sig);
    articles = (at.byMst[String(mst)] || []).map(([no, title]) => ({
      article_no: no, title, body: null,
    }));
  } catch (e) {
    if (!(e instanceof DataMissingError)) throw e;
  }

  return {
    mode: "static",
    env: bundle.env,
    ord,
    region: bundle.region,
    sig,
    articles,
    hasBodies: false,
    categories: ord.category ? [{ category_code: ord.category, method: "rule" }] : [],
    legalBasis: [], citations: [], budgetLinks: [], budgetLinkCount: null,
    versions: [], changeLog: [], neural: [], appendices: [],
  };
}

/* ---------------- 그리기 ---------------- */

function draw(root, v, sigHint) {
  const o = v.ord;
  const repealed = o.status === "repealed" || !!o.repealed_on;

  root.appendChild(backRow(v.sig || sigHint));

  root.appendChild(section(o.name || o.ordinance_id || "조례",
    el("div", { class: "chip-row" },
      badge(v.mode === "full" ? "완전판 · DB 직결" : "배포본 · 정적 shard",
            v.mode === "full" ? "badge-verified" : "badge-info"),
      statusBadge(o.status),
      verificationBadge(o.verification_status),
      o.ord_kind ? badge(o.ord_kind, "badge-plain") : null,
      o.mst ? badge(`MST ${o.mst}`, "badge-plain") : null,
      v.region ? badge(v.region.full_name || v.region.name || v.sig, "badge-plain") : null
    ),
    repealed
      ? note(`⚠ 폐지된 자치법규입니다(폐지일 ${ymd(o.repealed_on) || "미상"}). `
             + "현행 근거로 인용하면 안 되고, 선례 추천에도 쓰면 안 됩니다.", "warn")
      : null,
    table(["항목", "값"], [
      ["ordinance_id", o.ordinance_id || `ordin:${o.mst}`],
      ["자치법규 종류", o.ord_kind || "—"],
      ["소관", o.org_name || o.department || "—"],
      ["제정/개정 구분", o.rr_cls_cd || "—"],
      ["공포일", ymd(o.enacted_on) || "—"],
      ["시행일", ymd(o.effective_on) || "—"],
      ["폐지일", ymd(o.repealed_on) || "—"],
      ["DB 기재 조문 수", o.article_count ?? "—"],
      ["검증상태", o.verification_status || "—"],
    ]),
    el("div", { class: "chip-row" },
      o.official_url ? extLink(o.official_url, "law.go.kr 원문") : null,
      o.canonical_url && o.canonical_url !== o.official_url
        ? extLink(o.canonical_url, "canonical_url") : null)
  ));

  // ---- 조문 ----
  const artSec = section(v.hasBodies
    ? `조문 본문 ${num(v.articles.length)}개`
    : `조문 목록 ${num(v.articles.length)}개 (제목까지)`);
  root.appendChild(artSec);

  if (v.hasBodies) {
    artSec.appendChild(asOfLine(
      v.articleBodyChars != null ? `본문 ${num(v.articleBodyChars)}자 · DB ordinance_articles 직접 조회` : null));
  } else {
    // 번들에도 body_excluded 안내문이 들어 있지만, 실행 명령은 여기서 최신 것으로 말한다.
    artSec.appendChild(note(
      "배포본에는 조문 '본문'이 없습니다 — ordinance_articles 236만 행의 본문 합계가 약 490MB 라 "
      + "정적 배포 용량을 넘습니다. 본문을 보려면 저장소 루트에서 `python viz/serve_full.py` 를 "
      + "띄우고 ?full=1 로 여세요.", "warn"));
  }

  if (!v.articles.length) {
    artSec.appendChild(note("이 자치법규에는 수집된 조문이 없습니다(데이터 수집 단계의 공백).", "warn"));
  } else if (v.hasBodies) {
    for (const a of v.articles) {
      artSec.appendChild(el("div", { class: "article" },
        el("div", { class: "article-head" },
          el("b", { text: articleLabel(a) })),
        el("p", { class: "article-text", text: a.body || "(본문 없음)" })
      ));
    }
  } else {
    artSec.appendChild(table(["조문", "제목"],
      v.articles.map((a) => [a.article_no, a.title || "—"])));
  }

  // ---- 근거 상위법 ----
  if (v.legalBasis.length) {
    root.appendChild(section(`근거 상위법 ${num(v.legalBasis.length)}건`,
      table(["상위법", "상위 조문", "이 조례 조문", "관계", "경로", "원문"],
        v.legalBasis.map((b) => [
          // 상위법 원문이 수집된 것(instrument_id)만 상세로 넘어간다.
          // 'lawname:' 만 있는 행은 법령 테이블에 없으므로 링크를 걸지 않는다(단정 금지).
          String(b.parent_id || "").startsWith("lawname:")
            ? (b.parent_name || b.parent_id)
            : el("button", { class: "btn-link", text: b.parent_name || b.parent_id,
                onclick: () => go(`/statute/${encodeURIComponent(b.parent_id)}`) }),
          b.parent_article || "—",
          b.child_article || "—",
          b.relation || "—",
          b.source_path || "—",
          extLink(b.official_url, b.official_url ? "law.go.kr" : "—"),
        ])),
      note("상위법이 이름(lawname:)으로만 남아 있는 행은 법령 원문이 수집되지 않은 것이다. "
         + "위임관계는 조문 인용 파싱 결과이며 법적 효력 판단이 아니다.")));
  }
  if (v.citations.length) {
    root.appendChild(section(`명시 인용 ${num(v.citations.length)}건`,
      table(["인용 대상", "인용 원문", "유형", "이 조례 조문"],
        v.citations.map((c) => [
          c.cited_name || c.dst_id, c.citation_text || "—",
          c.citation_type || "—", c.src_article || "—",
        ]))));
  }

  // ---- 분류 ----
  if (v.categories.length) {
    root.appendChild(section("정책분야 분류",
      table(["코드", "분야", "신뢰도", "방법"],
        v.categories.map((c) => [
          c.category_code,
          c.category_name || categoryName(c.category_code),
          c.confidence != null ? Number(c.confidence).toFixed(4) : "—",
          c.method || "—",
        ])),
      note("규칙기반(method=rule) 자동분류 결과다 — 추정값이며 소관 부서의 공식 분류가 아니다.")));
  }

  // ---- 예산 연결 ----
  if (v.budgetLinkCount) {
    root.appendChild(section(`예산 연결 ${num(v.budgetLinkCount)}건`,
      table(["회계연도", "세부사업", "분야", "예산현액", "지출액", "confidence", "검증"],
        v.budgetLinks.slice(0, 100).map((b) => [
          b.fyr ?? "—", b.dbiz_nm || b.budget_id, b.field || "—",
          b.budget_now != null ? num(b.budget_now) : "—",
          b.exe_amt != null ? num(b.exe_amt) : "—",
          b.confidence != null ? Number(b.confidence).toFixed(3) : "—",
          b.verified ? "확인됨" : "추정 연결",
        ])),
      note("verified=1 만 '확인됨'이다. 나머지는 이름·분야 매칭에 기반한 추정 연결이며 "
         + "그 조례에 그 예산이 집행됐다는 증거가 아니다.")));
  }

  // ---- 연혁 / 변경 이력 ----
  if (v.versions.length > 1) {
    root.appendChild(section(`같은 work 의 판본 ${num(v.versions.length)}개`,
      table(["ordinance_id", "version", "공포", "시행", "폐지", "상태"],
        v.versions.map((x) => [
          x.ordinance_id, x.version_no ?? "—", ymd(x.enacted_on) || "—",
          ymd(x.effective_on) || "—", ymd(x.repealed_on) || "—", x.status || "—",
        ]))));
  }
  if (v.changeLog.length) {
    root.appendChild(section(`변경 이력 ${num(v.changeLog.length)}건`,
      table(["시각", "이벤트", "바뀐 필드", "출처"],
        v.changeLog.map((c) => [c.ts, c.event || "—", c.fields_changed || "—", c.source || "—"]))));
  }

  // ---- 신경망 유사 ----
  if (v.neural.length) {
    root.appendChild(section(`신경망 유사 조례 ${num(v.neural.length)}건`,
      table(["모델", "순위", "코사인", "조례", "지자체", "상태"],
        v.neural.slice(0, 40).map((n) => [
          n.model_name, n.rank, Number(n.cosine_sim ?? 0).toFixed(4),
          el("button", { class: "btn-link", text: n.dst_name || n.dst_id,
            onclick: () => go(`/ordinance/${encodeURIComponent(n.dst_id)}`) }),
          n.dst_org || "—", n.dst_status || "—",
        ])),
      note("코사인 유사도는 그래프·텍스트 임베딩 기반 추정치이며 법적 동등성을 뜻하지 않는다.")));
  }

  // ---- 2홉 서브그래프 (완전판 전용 · 사전계산 불필요) ----
  if (v.mode === "full") root.appendChild(subgraphSection(v.ord.ordinance_id || `ordin:${o.mst}`));

  if (v.appendices.length) {
    root.appendChild(section(`별표·서식 ${num(v.appendices.length)}건`,
      table(["번호", "제목", "종류", "파일"],
        v.appendices.map((a) => [a.appendix_no || "—", a.title || "—", a.appendix_kind || "—",
          extLink(a.file_url, a.file_name || (a.file_url ? "내려받기" : "—"))]))));
  }

  root.appendChild(envelopeFooter(v.env));
}

/**
 * 임의 조례 2홉 서브그래프 — GET /api/db/graph/{id}.
 * 배포본은 사전계산된 1,000건만 shard 를 갖지만, 완전판은 199,858건 어느 것이든 즉석 계산한다.
 * 펼쳤을 때만 요청한다(첫 호출은 Degrees 색인 구축 때문에 수십 초 걸릴 수 있다).
 */
function subgraphSection(id) {
  const body = el("div", {});
  const det = el("details", {}, el("summary", { text: "2홉 서브그래프 (DB 즉석 계산)" }), body);
  let started = false;
  det.addEventListener("toggle", async () => {
    if (!det.open || started) return;
    started = true;
    body.appendChild(loading("서브그래프를 계산하는 중… (첫 호출은 색인 구축으로 수십 초 걸립니다)"));
    try {
      const env = await fullGet(`graph/${encodeURIComponent(id)}`);
      const d = (env && env.data) || {};
      body.innerHTML = "";
      body.appendChild(el("div", { class: "chip-row" },
        badge(`노드 ${num((d.nodes || []).length)}`, "badge-info"),
        badge(`엣지 ${num((d.edges || []).length)}`, "badge-info"),
        ...Object.entries((d.stats || {}).by_relation || {})
          .map(([k, n]) => badge(`${k} ${n}`, "badge-plain"))));
      const nameOf = new Map((d.nodes || []).map((n) => [n.id, n.name || n.id]));
      body.appendChild(table(["관계", "출발", "도착", "검증"],
        (d.edges || []).slice(0, 300).map((e) => [
          e.relation || "—",
          nameOf.get(e.source) || e.source,
          nameOf.get(e.target) || e.target,
          e.verification_status || "—",
        ])));
      if (d.truncated) {
        body.appendChild(note(`상한에 걸려 잘린 것: ${JSON.stringify(d.truncated)}`, "warn"));
      }
      body.appendChild(envelopeFooter(env));
    } catch (e) {
      body.innerHTML = "";
      body.appendChild(note(`서브그래프 계산 실패 — ${e.message}`, "warn"));
    }
  });
  return section("법령 위계 (완전판)", det,
    note("배포본은 사전계산된 조례 1,000건만 서브그래프 shard 를 갖는다. "
       + "완전판은 199,858건 어느 것이든 DB 에서 즉석 계산한다."));
}

function articleLabel(a) {
  const no = String(a.article_no || "");
  // 완전판은 DB 원문 6자리('000802'), 배포본은 축약형('8-2') 이다. 둘 다 사람이 읽게 만든다.
  let human = no;
  if (/^\d{6}$/.test(no)) {
    const main = parseInt(no.slice(0, 4), 10);
    const branch = parseInt(no.slice(4), 10);
    human = branch ? `제${main}조의${branch}` : `제${main}조`;
  } else if (/^\d+-\d+$/.test(no)) {
    const [m, b] = no.split("-");
    human = `제${m}조의${b}`;
  } else if (/^\d+$/.test(no)) {
    human = `제${no}조`;
  }
  return a.title ? `${human} (${a.title})` : human;
}

function backRow(sig) {
  return el("div", { class: "toolbar" },
    sig
      ? el("button", { class: "btn", text: `지역 상세로 (${sig})`, onclick: () => go(`/region/${sig}`) })
      : null,
    el("button", { class: "btn", text: "검색으로", onclick: () => go("/search") }),
    el("button", { class: "btn", text: "대시보드", onclick: () => go("/dashboard") })
  );
}
