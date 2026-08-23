// 16. 법령 상세 — 완전판(로컬 DB)이면 법령 조문 '본문'까지, 배포본이면 메타까지.
//
//  경로: #/statute/{instrument_id}   (예: statute:282559, admrul:1007031000)
//  데이터 경로 두 갈래
//    A) 완전판 : GET /api/db/statute/{id} → 조문 본문 + 하위 조례(위임) + 인용 + 검증기록
//    B) 배포본 : api/statute/index.json 의 fnv1a32 버킷 → api/statute/all/{bucket}.json 의 메타
//               법령 조문 본문(articles 86,745행)은 정적 번들에 없다.
import { el, num, ymd, extLink } from "../util.js";
import { full, fullGet, getJSON, DataMissingError } from "../api.js";
import { section, table, note, loading, badge, statusBadge, verificationBadge,
         errorPanel, envelopeFooter, asOfLine } from "../components.js";
import { go } from "../router.js";

/** api/statute/index.json 의 hash.js 참조구현 그대로 (fnv1a32) */
function fnv1a32(s) {
  let h = 0x811c9dc5;
  const b = new TextEncoder().encode(s);
  for (let i = 0; i < b.length; i++) { h ^= b[i]; h = Math.imul(h, 0x01000193) >>> 0; }
  return h >>> 0;
}
function bucketOf(id, n) { return String(fnv1a32(id) % n).padStart(2, "0"); }

export async function render(root, params) {
  const id = decodeURIComponent(String(params.id || "")).trim();
  root.appendChild(loading(`${id} 을(를) 불러오는 중…`));

  let view = null;
  let err = null;
  if (full.enabled) {
    try { view = await fromFullApi(id); } catch (e) { err = e; }
  }
  if (!view) {
    try { view = await fromStatic(id); } catch (e) { err = err || e; }
  }

  root.innerHTML = "";
  if (!view) {
    root.appendChild(errorPanel(err || new Error("데이터 없음"),
      `법령 ${id} 를 찾지 못했습니다. `
      + "배포본은 api/statute/all/{버킷}.json 에서 찾습니다(법령·행정규칙 29,811건 전량). "
      + "조문 본문까지 보려면 `python viz/serve_full.py` 를 띄우고 ?full=1 로 여세요."));
    root.appendChild(el("div", { class: "toolbar" },
      el("button", { class: "btn", text: "법령 위계로", onclick: () => go("/graph") })));
    return;
  }
  draw(root, view);
}

async function fromFullApi(id) {
  const env = await fullGet(`statute/${encodeURIComponent(id)}`);
  const d = (env && env.data) || null;
  if (!d || !d.instrument) return null;
  const i = d.instrument;
  return {
    mode: "full", env,
    inst: {
      id: i.instrument_id, name: i.name, kind: i.kind, tier: i.national_tier,
      source_type: i.source_type, status: i.status, current_history: i.current_history,
      enacted_on: i.enacted_on, effective_on: i.effective_on, repealed_on: i.repealed_on,
      competent_authority: i.competent_authority, official_url: i.official_url,
      verification_status: i.verification_status, tier_disputed: i.tier_disputed,
    },
    articles: d.articles || [],
    articleBodyChars: d.article_body_chars ?? null,
    children: d.child_ordinances || [],
    childTotal: d.child_ordinance_total ?? null,
    relations: d.relations || [],
    citedFrom: d.cites_from || [],
    verification: d.verification || null,
  };
}

async function fromStatic(id) {
  let idx;
  try { idx = await getJSON("api/statute/index.json"); }
  catch (e) { if (e instanceof DataMissingError) return null; throw e; }
  const x = (idx && idx.data) || idx || {};
  const n = x.bucket_count || (x.hash && x.hash.modulo) || 32;
  const env = await getJSON(`api/statute/all/${bucketOf(id, n)}.json`);
  const b = (env && env.data) || env || {};
  const rec = (b.instruments || []).find((r) => r.id === id);
  if (!rec) return null;

  const def = b.defaults || {};
  const key = id.includes(":") ? id.slice(id.indexOf(":") + 1) : id;
  const sourceType = rec.source_type || def.source_type;
  let url = rec.official_url;
  if (!url && b.url_templates && b.url_templates[sourceType]) {
    url = b.url_templates[sourceType]
      .replace("{key}", key)
      .replace("{effective_on_raw}", String(rec.effective_on || "").replace(/-/g, ""));
  }
  return {
    mode: "static", env,
    inst: {
      id: rec.id, name: rec.name, kind: rec.kind, tier: rec.tier,
      source_type: sourceType, status: rec.status || def.status,
      current_history: rec.current_history,
      enacted_on: rec.enacted_on, effective_on: rec.effective_on,
      repealed_on: rec.repealed_on, competent_authority: rec.competent_authority,
      official_url: url,
      verification_status: rec.verification_status || def.verification_status,
      tier_disputed: rec.tier_disputed,
    },
    articles: [], articleBodyChars: null,
    children: [], childTotal: rec.child_ordinances ?? rec.deleg_rows ?? null,
    relations: [], citedFrom: [], verification: null,
  };
}

function draw(root, v) {
  const i = v.inst;
  root.appendChild(el("div", { class: "toolbar" },
    el("button", { class: "btn", text: "법령 위계로", onclick: () => go("/graph") }),
    el("button", { class: "btn", text: "대시보드", onclick: () => go("/dashboard") })));

  root.appendChild(section(i.name || i.id,
    el("div", { class: "chip-row" },
      badge(v.mode === "full" ? "완전판 · DB 직결" : "배포본 · 정적 shard",
            v.mode === "full" ? "badge-verified" : "badge-info"),
      statusBadge(i.status),
      verificationBadge(i.verification_status),
      i.kind ? badge(i.kind, "badge-plain") : null,
      i.tier != null ? badge(`tier ${i.tier}${i.tier_disputed ? " (분쟁)" : ""}`, "badge-plain") : null),
    i.repealed_on
      ? note(`⚠ 폐지된 법령입니다(폐지일 ${ymd(i.repealed_on) || "미상"}). 현행 근거로 인용하면 안 됩니다.`, "warn")
      : null,
    i.tier_disputed
      ? note("이 종류는 국가 서열(tier)이 학설상 다툼이 있어 단정하지 않는다.", "warn")
      : null,
    table(["항목", "값"], [
      ["instrument_id", i.id],
      ["종류", i.kind || "—"],
      ["source_type", i.source_type || "—"],
      ["소관", i.competent_authority || "—"],
      ["연혁 구분", i.current_history || "—"],
      ["공포/제정일", ymd(i.enacted_on) || "—"],
      ["시행일", ymd(i.effective_on) || "—"],
      ["폐지일", ymd(i.repealed_on) || "—"],
      ["검증상태", i.verification_status || "—"],
    ]),
    el("div", { class: "chip-row" },
      i.official_url ? extLink(i.official_url, "law.go.kr 원문") : null)));

  const artSec = section(v.mode === "full"
    ? `법령 조문 본문 ${num(v.articles.length)}개`
    : "법령 조문 본문");
  root.appendChild(artSec);
  if (v.mode === "full") {
    artSec.appendChild(asOfLine(
      v.articleBodyChars != null ? `본문 ${num(v.articleBodyChars)}자 · DB articles 직접 조회` : null));
    if (!v.articles.length) {
      artSec.appendChild(note("이 법령의 조문은 수집되지 않았습니다(articles 테이블은 86,745행뿐 — "
        + "대표 법령 위주로 수집된 상태다). law.go.kr 원문으로 확인하세요.", "warn"));
    }
    for (const a of v.articles) {
      artSec.appendChild(el("div", { class: "article" },
        el("div", { class: "article-head" },
          el("b", { text: `제${a.article_no}조${a.article_branch ? `의${a.article_branch}` : ""}`
                          + (a.title ? ` (${a.title})` : "") })),
        el("p", { class: "article-text", text: a.body || "(본문 없음)" })));
    }
  } else {
    artSec.appendChild(note(
      "배포본에는 법령 조문 본문이 없습니다(메타 29,811건만 담습니다). "
      + "본문을 보려면 `python viz/serve_full.py` 를 띄우고 ?full=1 로 여세요.", "warn"));
  }

  if (v.childTotal != null || v.children.length) {
    const sec = section(`이 법령에 근거한 자치법규 ${num(v.childTotal ?? v.children.length)}건`);
    root.appendChild(sec);
    if (v.children.length) {
      sec.appendChild(table(["자치법규", "지자체", "상위 조문", "이 조례 조문", "경로", "상태"],
        v.children.slice(0, 200).map((c) => [
          el("button", { class: "btn-link", text: c.name || c.ordinance_id,
            onclick: () => go(`/ordinance/${encodeURIComponent(c.ordinance_id)}`) }),
          c.org_name || "—", c.parent_article || "—", c.child_article || "—",
          c.source_path || "—", c.status || "—",
        ])));
      if ((v.childTotal ?? 0) > v.children.length) {
        sec.appendChild(note(`상위 ${num(v.children.length)}건만 표시했습니다(전체 ${num(v.childTotal)}건).`));
      }
    } else {
      sec.appendChild(note("배포본은 위임관계를 지역별(api/delegation/{sig}.json)로 담습니다. "
        + "법령 기준 목록은 완전판에서 제공합니다."));
    }
  }

  if (v.citedFrom.length) {
    root.appendChild(section(`이 법령을 인용한 것 ${num(v.citedFrom.length)}건`,
      table(["출처 종류", "출처 id", "관계", "인용 원문"],
        v.citedFrom.slice(0, 100).map((c) => [
          c.src_kind || "—", c.src_id, c.relation || "—", c.citation_text || "—",
        ]))));
  }
  if (v.verification) {
    root.appendChild(section("검증 기록",
      table(["항목", "값"], Object.entries(v.verification).map(([k, val]) => [k, String(val ?? "—")]))));
  }

  root.appendChild(envelopeFooter(v.env));
}
