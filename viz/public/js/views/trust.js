// 11. 검증 공시 — "어디까지 검증했고, 어디부터 추정인가"를 한 화면에.
//
//    데이터: api/verification/summary.json (make_extend_fixtures.py)
//      · verification 1,205행      — 사람이 출처를 대조한 항목만
//      · delegations 421,627행 전수 — 인용 조문 존재 확인(article-verified/missing/unverifiable)
//      · temporal_audit 7,060행     — 시간 무결성 규칙 위반
//      · ordinance_budget_link 183,145행 — 확률적 매칭 + 표본 584건 사람 판정
//
//    표기 규율 — 검증된 항목과 추정 항목을 같은 무게로 보여주지 않는다.
//    수치마다 모집단과 검증 방식을 병기한다.
import { el, num, pct } from "../util.js";
import { getJSON } from "../api.js";
import { section, table, note, loading, asOfLine, badge, statCard,
         envelopeFooter } from "../components.js";

const PATH = "api/verification/summary.json";
const SUCCESSION_PATH = "api/succession.json";
const GENERATOR = "system/make_extend_fixtures.py";

const TIER_STYLE = {
  verified: { color: "#1e7a4b", label: "검증됨" },
  linked: { color: "#277086", label: "원문 확보" },
  estimated: { color: "#7d5ba6", label: "추정" },
  unknown: { color: "#8a929c", label: "미확인" },
  bad: { color: "#c0392b", label: "불일치" },
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

export async function render(root) {
  root.appendChild(loading("검증 공시 데이터를 불러오는 중…"));
  let env;
  try { env = await getJSON(PATH); }
  catch (e) {
    root.innerHTML = "";
    root.appendChild(el("div", { class: "panel panel-warn" },
      el("h2", { class: "panel-title", text: "검증 공시 데이터가 현재 번들에 없습니다" }),
      el("p", { text: `${PATH} 를 찾지 못했습니다.` }),
      el("p", { class: "hint" }, el("b", { text: "생성 방법: " }),
        el("code", { text: `python ${GENERATOR} --only verification` })),
      el("pre", { class: "err", text: `${e.name || "Error"}: ${e.message || e}` })));
    return;
  }
  root.innerHTML = "";

  const d = env.data || env;
  const cite = d.citation_verification || {};
  const ver = d.verification_table || {};
  const audit = d.temporal_audit || {};
  const link = d.ordinance_budget_link || {};
  const inst = d.instrument_status || {};

  const sec = section("검증 공시",
    asOfLine("무엇을 사람이 확인했고, 무엇이 기계 추정이며, 무엇이 아직 확인 불가인지"));
  root.appendChild(sec);
  sec.appendChild(note(
    "이 화면의 목적은 신뢰도를 높여 보이는 것이 아니라 한계를 드러내는 것이다. "
    + "아래 수치는 각각 모집단이 다르다 — 같은 분모로 비교하면 안 된다.", "warn"));

  /* ── 1. 검증 사다리 ─────────────────────────────────────────────── */
  const ladder = section("검증 사다리 — 근거의 강도별 규모",
    note("위로 갈수록 강한 근거다. 각 줄의 분모(모집단)가 다르므로 줄 사이 비율 비교는 성립하지 않는다."));
  const steps = [
    {
      tier: "verified", name: "사람이 출처를 대조함",
      n: ver.rows, denom: null,
      what: "verification 테이블. korea100 시드·기관 캔버스 등 사람이 원문과 맞춰 본 항목만.",
    },
    {
      tier: "verified", name: "인용 조문의 존재를 확인함",
      n: (cite.by_status || {})["article-verified"], denom: cite.total,
      what: "조례가 인용한 상위법 조문이 실제로 존재하는지 자동 대조. 해석·적용 타당성은 보지 않았다.",
    },
    {
      tier: "linked", name: "원문 링크를 확보함(source-linked)",
      n: (inst.ordinances || {})["source-linked"], denom: Object.values(inst.ordinances || {}).reduce((a, b) => a + b, 0),
      what: "law.go.kr 원문 URL 을 확보한 자치법규. 내용 검증이 아니라 출처 추적 가능성이다.",
    },
    {
      tier: "estimated", name: "확률적으로 추정 연결함(조례↔예산)",
      n: link.rows, denom: null,
      what: `자동 매칭 링크. 사람 확인(verified=1)은 ${num((link.by_verified || {})["1"])}건뿐이고 나머지는 추정이다.`,
    },
    {
      tier: "bad", name: "인용 조문을 찾지 못함",
      n: (cite.by_status || {})["article-missing"], denom: cite.total,
      what: "오인용이거나 상위법 개정으로 조문 번호가 바뀐 경우. 그대로 근거로 쓰면 안 된다.",
    },
    {
      tier: "unknown", name: "자동 확인 불가",
      n: (cite.by_status || {})["unverifiable"], denom: cite.total,
      what: "조문번호가 없거나 상위법 본문을 아직 수집하지 못해 대조 자체가 불가능한 인용.",
    },
  ];
  for (const s of steps) {
    if (s.n === undefined || s.n === null) continue;
    const st = TIER_STYLE[s.tier];
    const ratio = s.denom ? s.n / s.denom : null;
    ladder.appendChild(el("div", { class: "card" },
      el("div", { class: "card-head" },
        el("h3", { class: "card-title", text: s.name }),
        el("div", { class: "chip-row" },
          badge(st.label, s.tier === "verified" ? "badge-active"
            : s.tier === "estimated" ? "badge-est"
              : s.tier === "bad" ? "badge-repealed" : "badge-unknown"),
          badge(`${num(s.n)}건`, "badge-info"),
          ratio !== null ? badge(`모집단의 ${pct(ratio, 2)}`, "badge-plain") : null)),
      ratio !== null
        ? el("div", { style: "height:8px;border-radius:4px;background:rgba(0,0,0,.06);margin:6px 0" },
            el("div", { style: `height:8px;border-radius:4px;width:${(ratio * 100).toFixed(2)}%;background:${st.color}` }))
        : null,
      el("p", { class: "small muted", text: s.what })));
  }
  root.appendChild(ladder);

  /* ── 2. 인용 검증 전수 ──────────────────────────────────────────── */
  if (cite.total) {
    const cs = section(`인용 검증 — 위임 관계 ${num(cite.total)}건 전수`,
      note(`대상: ${cite.scope || "delegations"}`));
    cs.appendChild(shareBars(cite.by_status, {
      "article-verified": TIER_STYLE.verified.color,
      "article-missing": TIER_STYLE.bad.color,
      "unverifiable": TIER_STYLE.unknown.color,
    }, VERIF_KO));
    cs.appendChild(table(["상태", "건수", "비율", "뜻"],
      Object.entries(cite.by_status).sort((a, b) => b[1] - a[1]).map(([k, v]) => [
        VERIF_KO[k] || k, num(v),
        (cite.rates || {})[k] !== undefined ? pct((cite.rates || {})[k], 2) : pct(v / cite.total, 2),
        (cite.legend || {})[k] || "—"])));
    if (cite.note) cs.appendChild(note(cite.note, "warn"));
    cs.appendChild(el("p", { class: "hint" },
      document.createTextNode("이 관계를 실제 그래프로 보려면 "),
      el("a", { href: "#/graph?mode=hierarchy", text: "법령 위계 화면" }),
      document.createTextNode(" 으로 간다.")));
    root.appendChild(cs);
  }

  /* ── 3. 사람 검증(verification 테이블) ──────────────────────────── */
  if (ver.rows) {
    const vs = section(`사람이 대조한 항목 ${num(ver.rows)}건`,
      note(ver.note || "", "warn"));
    if (ver.verified_at_range?.length) {
      vs.appendChild(el("div", { class: "chip-row" },
        badge(`검증 기간 ${ver.verified_at_range[0]} ~ ${ver.verified_at_range[ver.verified_at_range.length - 1]}`, "badge-plain")));
    }
    if (ver.by_entity_status?.length) {
      vs.appendChild(table(["대상", "상태", "건수"],
        ver.by_entity_status.map((r) => [r.entity_type, VERIF_KO[r.status] || r.status, num(r.n)])));
    }
    const ct = ver.citation_totals || {};
    if (Object.keys(ct).length) {
      vs.appendChild(el("h3", { style: "font-size:14px;margin:10px 0 4px", text: "이 검증에서 대조한 인용" }));
      vs.appendChild(table(["항목", "건수"], [
        ["인용 항목", num(ct.citation_entries)],
        ["명시적 인용", num(ct.explicit_citation_entries)],
        ["조문 참조", num(ct.article_references)],
        ["확인된 참조", num(ct.verified_references)],
        ["찾지 못한 참조", num(ct.missing_references)],
        ["확인 불가", num(ct.uncheckable_references)],
      ]));
    }
    if (ver.top_methods?.length) {
      vs.appendChild(el("details", {}, el("summary", { text: "검증 방법별 건수" }),
        table(["방법", "건수"], ver.top_methods.map((m) => [m.method, num(m.n)]))));
    }
    root.appendChild(vs);
  }

  /* ── 4. 시간 무결성 감사 ────────────────────────────────────────── */
  if (audit.rows) {
    const as = section(`시간 무결성 감사 ${num(audit.rows)}건`,
      note(audit.note || "", "warn"));
    if (audit.by_rule?.length) {
      const total = audit.by_rule.reduce((a, r) => a + r.n, 0) || 1;
      as.appendChild(table(["규칙", "대상", "심각도", "자동보정", "건수", "비율"],
        [...audit.by_rule].sort((a, b) => b.n - a.n).map((r) => [
          r.rule, r.entity_type,
          r.severity === "warn" ? el("span", { class: "badge badge-warn", text: "warn" })
            : el("span", { class: "badge badge-plain", text: String(r.severity) }),
          r.repaired ? "예" : "아니오", num(r.n), pct(r.n / total, 1)])));
    }
    if (audit.samples?.length) {
      as.appendChild(el("details", {}, el("summary", { text: `위반 표본 ${audit.samples.length}건` }),
        table(["규칙", "대상", "id", "관측값", "조치"],
          audit.samples.map((s) => [s.rule, s.entity_type, s.entity_id, s.observed, s.repair_action]))));
    }
    as.appendChild(el("p", { class: "hint" },
      document.createTextNode("T7·T8 은 지자체 승계의 잔여다. 승계 관계는 "),
      el("a", { href: "#/lifecycle?tab=succession", text: "정책 생애주기 · 지자체 승계" }),
      document.createTextNode(" 에서 본다.")));
    root.appendChild(as);
  }

  /* ── 5. 조례↔예산 링크(추정) ───────────────────────────────────── */
  if (link.rows) {
    const ls = section(`조례↔예산 연결 ${num(link.rows)}건 — 전부 추정이다`);
    const bv = link.by_verified || {};
    ls.appendChild(el("div", { class: "stat-grid" },
      statCard("사람 확인(verified=1)", num(bv["1"]),
        link.rows ? `전체의 ${pct((bv["1"] || 0) / link.rows, 3)}` : null),
      statCard("미확인(verified=0)", num(bv["0"]), "자동 매칭 결과 그대로"),
      statCard("오탐 표시(verified=-1)", num(bv["-1"]), "사람이 틀렸다고 표시"),
      statCard("confidence≥0.8", num((link.by_confidence || []).find((b) => b.bucket === "conf>=0.8")?.n),
        "고신뢰 구간")));
    if (link.by_confidence?.length) {
      ls.appendChild(table(["신뢰도 구간", "건수", "평균 confidence"],
        link.by_confidence.map((b) => [b.bucket, num(b.n),
          typeof b.avg_conf === "number" ? b.avg_conf.toFixed(4) : "—"])));
    }
    const sv = link.sample_validation || {};
    if (sv.judged) {
      ls.appendChild(el("div", { class: "card card-caution" },
        el("div", { class: "card-head" },
          el("h3", { class: "card-title", text: "표본 검증 결과 (사람 판정)" }),
          el("div", { class: "chip-row" },
            badge(`표본 ${num(sv.judged)}건`, "badge-info"),
            badge("추정 지표", "badge-est"))),
        table(["지표", "값", "95% 신뢰구간"], [
          ["전체 정밀도(엄격)", pct(sv.precision_strict, 1),
           (sv.precision_strict_ci95 || []).map((x) => pct(x, 1)).join(" ~ ") || "—"],
          [`confidence≥${sv.high_conf_threshold} 정밀도`, pct(sv.precision_high_conf, 1),
           (sv.precision_high_conf_ci95 || []).map((x) => pct(x, 1)).join(" ~ ") || "—"],
        ]),
        el("p", { class: "small muted", text: `출처: ${sv.source || "—"} (${sv.source_kind || "—"})` }),
        sv.note ? note(sv.note, "warn") : null));
      ls.appendChild(note(
        "표본 검증 시점의 링크 모집단과 현재 모집단이 다르다. 위 정밀도를 현재 "
        + `${num(link.rows)}건 전체에 그대로 적용하면 과대·과소 추정이 된다.`, "warn"));
    }
    if (link.by_method?.length) {
      ls.appendChild(el("details", {}, el("summary", { text: "매칭 방법별 분해" }),
        table(["방법", "건수", "평균 confidence"],
          [...link.by_method].sort((a, b) => b.n - a.n).map((m) => [
            m.match_method, num(m.n),
            typeof m.avg_conf === "number" ? m.avg_conf.toFixed(4) : "—"]))));
    }
    if (link.note) ls.appendChild(note(link.note, "warn"));
    root.appendChild(ls);
  }

  /* ── 6. 원문 확보 상태 ──────────────────────────────────────────── */
  if (Object.keys(inst).length) {
    const is = section("원문 확보 상태 (verification_status)");
    for (const [tbl, m] of Object.entries(inst)) {
      const total = Object.values(m).reduce((a, b) => a + b, 0) || 1;
      is.appendChild(el("h3", { style: "font-size:14px;margin:10px 0 4px",
        text: tbl === "ordinances" ? "자치법규" : "국가 법령(legal_instrument)" }));
      is.appendChild(table(["상태", "건수", "비율"],
        Object.entries(m).sort((a, b) => b[1] - a[1])
          .map(([k, v]) => [VERIF_KO[k] || k, num(v), pct(v / total, 2)])));
    }
    is.appendChild(note("‘원문링크(source-linked)’는 출처 URL 을 확보했다는 뜻이지 내용이 검증됐다는 뜻이 아니다."));
    root.appendChild(is);
  }

  /* ── 7. 승계 잔여 요약(있으면) ──────────────────────────────────── */
  try {
    const sEnv = await getJSON(SUCCESSION_PATH);
    const sd = sEnv.data || sEnv;
    const st = sd.totals || {};
    root.appendChild(section("지자체 승계로 생긴 불확실성",
      el("div", { class: "stat-grid" },
        statCard("승계 관계", num(st.rows), "region_succession"),
        statCard("승계 대상 조례", num(st.ordinances_in_superseded_regions), "구 코드에 남은 조례"),
        statCard("T7 위반", num((sd.audit || {}).T7_region_no_longer_exists), "사라진 지자체 참조"),
        statCard("T8 위반", num((sd.audit || {}).T8_orphan_region), "regions 에 없는 코드")),
      sd.caveat ? note(sd.caveat, "warn") : null));
  } catch (e) { /* 승계 파일이 없으면 이 절만 생략한다 */ }

  /* ── 8. 총괄 주의문 ────────────────────────────────────────────── */
  if (d.coverage_caveat?.length) {
    const cc = section("이 화면을 읽을 때의 규칙");
    for (const c of d.coverage_caveat) cc.appendChild(note(c, "warn"));
    root.appendChild(cc);
  }
  root.appendChild(envelopeFooter(env));
}

/** 값 맵을 가로 100% 누적 막대 + 범례로. */
function shareBars(obj, colors, ko = {}) {
  const entries = Object.entries(obj).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((a, [, v]) => a + v, 0) || 1;
  const bar = el("div", { style: "display:flex;height:20px;border-radius:4px;overflow:hidden;margin:8px 0" });
  const leg = el("div", { class: "legend" });
  for (const [k, v] of entries) {
    const c = colors[k] || "#95a5a6";
    bar.appendChild(el("div", { style: `width:${(v / total) * 100}%;background:${c}`, title: `${k} ${num(v)}` }));
    leg.appendChild(el("span", { class: "legend-item" },
      el("i", { class: "swatch", style: `background:${c}` }),
      el("span", { text: `${ko[k] || k} ${num(v)} (${((v / total) * 100).toFixed(2)}%)` })));
  }
  return el("div", {}, bar, leg);
}
