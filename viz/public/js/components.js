// 화면 공통 조각 — 배너, 배지, 카드, 표, 에러 패널
import { el, esc, num, pct, ymd } from "./util.js";
import { state, DataMissingError, FIXTURE_TOOLS } from "./api.js";
import { CONFIDENCE_GRADES } from "./config.js";

/* ---------- 상단 배너 ---------- */

export function mockBanner() {
  return null;
}

export function staleBanner() {
  if (!state.stale) return null;
  return el("div", { class: "banner banner-stale", role: "alert" },
    el("span", { class: "banner-tag", text: "데이터 지연" }),
    el("span", {
      class: "banner-body",
      text: `기준일 ${state.asOfDate ?? "?"} 이후 갱신이 임계(${state.staleDays ?? "?"}일)를 넘었습니다. 최신 상태가 아닐 수 있습니다.`,
    })
  );
}

export function asOfLine(extra) {
  const bits = [`데이터 기준일 ${state.asOfDate ?? "확인 필요"}`];
  if (state.stale) bits.push("⚠ stale=true");
  if (extra) bits.push(extra);
  return el("div", { class: "as-of", text: bits.join(" · ") });
}

/* ---------- 배지 ---------- */

export function badge(text, kind = "") {
  return el("span", { class: `badge ${kind}`, text });
}

/** 조례-예산 링크 신뢰도 배지. verified=1 만 "확인됨". */
export function confidenceBadge(confidence, verified) {
  if (verified === true || verified === 1) {
    return badge("확인됨(verified)", "badge-verified");
  }
  const c = typeof confidence === "number" ? confidence : null;
  const grade = c === null
    ? { key: "low", label: "추정(등급 미상)", note: "confidence 값 없음" }
    : CONFIDENCE_GRADES.find((g) => c >= g.min);
  const b = badge(`추정 연결 · ${grade.label}`, `badge-est badge-${grade.key}`);
  b.title = `자동매칭 링크입니다(수작업 검증 없음). ${grade.note}` + (c === null ? "" : ` / confidence=${c.toFixed(4)}`);
  return b;
}

/** 조례 상태 배지. 폐지는 반드시 경고색. */
export function statusBadge(status) {
  if (status === "repealed" || status === "폐지") return badge("폐지", "badge-repealed");
  if (status === "active" || status === "현행") return badge("현행", "badge-active");
  return badge(status || "상태 미상", "badge-unknown");
}

export function verificationBadge(vs) {
  if (!vs) return null;
  const map = { "source-linked": ["원문 링크 확보", "badge-verified"], "unverified": ["미검증", "badge-warn"] };
  const [label, kind] = map[vs] || [vs, "badge-unknown"];
  return badge(label, kind);
}

/* ---------- 레이아웃 ---------- */

export function statCard(label, value, sub) {
  return el("div", { class: "stat-card" },
    el("div", { class: "stat-label", text: label }),
    el("div", { class: "stat-value", text: value }),
    sub ? el("div", { class: "stat-sub", text: sub }) : null
  );
}

export function section(title, ...children) {
  return el("section", { class: "panel" },
    title ? el("h2", { class: "panel-title", text: title }) : null,
    ...children
  );
}

export function note(text, kind = "") {
  return el("p", { class: `note ${kind}`, text });
}

export function table(headers, rows, opts = {}) {
  const t = el("table", { class: `data-table ${opts.class || ""}` });
  const thead = el("thead");
  thead.appendChild(el("tr", {}, headers.map((h) => el("th", { text: String(h) }))));
  t.appendChild(thead);
  const tb = el("tbody");
  for (const r of rows) {
    const tr = el("tr", opts.rowAttrs ? opts.rowAttrs(r) : {});
    for (const c of r) {
      tr.appendChild(c instanceof Node ? el("td", {}, c) : el("td", { text: c === null || c === undefined ? "—" : String(c) }));
    }
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  return el("div", { class: "table-wrap" }, t);
}

export function loading(msg = "불러오는 중…") {
  return el("div", { class: "loading" }, el("span", { class: "spinner" }), el("span", { text: msg }));
}

export function progressBar() {
  const bar = el("div", { class: "pbar-fill" });
  const label = el("span", { class: "pbar-label", text: "0%" });
  const wrap = el("div", { class: "pbar" }, bar, label);
  wrap.update = (done, total) => {
    const p = total ? Math.round((done / total) * 100) : 0;
    bar.style.width = p + "%";
    label.textContent = `${p}% (${done}/${total})`;
  };
  return wrap;
}

/* ---------- 에러/미제공 안내 ---------- */

export function errorPanel(err, context) {
  return el("div", { class: "panel panel-error" },
    el("h2", { class: "panel-title", text: "화면을 그리지 못했습니다" }),
    el("p", { text: context || "" }),
    el("pre", { class: "err", text: `${err?.name || "Error"}: ${err?.message || err}` })
  );
}

/**
 * 실데이터로 전환했을 때 api/*.json 이 없는 경우의 안내.
 * 정적 번들에 없는 값이라는 사실을 감추지 않는다.
 */
export function fixtureMissingPanel(key, err) {
  const tool = FIXTURE_TOOLS[key];
  return el("div", { class: "panel panel-warn" },
    el("h2", { class: "panel-title", text: "이 화면의 데이터가 현재 소스에 없습니다" }),
    el("p", {
      text: `이 화면은 사전계산 결과 파일(api/${key}.json)을 씁니다. 가상데이터 번들에는 들어 있지만 `
        + `정적 export 번들(system/data)에는 없습니다 — 요청 파라미터에 따라 계산되는 값이라 파일로 굽지 않기 때문입니다.`,
    }),
    tool ? el("p", { class: "hint" },
      el("b", { text: "실데이터로 채우는 법: " }),
      el("code", { text: `MCP tool ${tool}` }),
      document.createTextNode(" 를 호출해 응답 봉투를 그대로 "),
      el("code", { text: `api/${key}.json` }),
      document.createTextNode(" 로 저장하면 이 화면이 그대로 동작합니다.")
    ) : null,
    el("pre", { class: "err", text: `${err?.name || "Error"}: ${err?.message || err}` })
  );
}

export function cdnFailPanel(libName, err, fallbackNode) {
  const p = el("div", { class: "panel panel-warn" },
    el("h2", { class: "panel-title", text: `${libName} 를 불러오지 못했습니다` }),
    el("p", { text: "외부 CDN 접속이 막힌 환경으로 보입니다. 아래에 표 형태의 대체 화면을 표시합니다." }),
    el("pre", { class: "err", text: `${err?.name || "Error"}: ${err?.message || err}` })
  );
  return fallbackNode ? el("div", {}, p, fallbackNode) : p;
}

/** MCP 응답 봉투의 disclaimer/execution_allowed 표기 */
export function envelopeFooter(env) {
  if (!env) return null;
  const bits = [];
  if (env.as_of_date) bits.push(`기준일 ${env.as_of_date}`);
  if (env.stale) bits.push("stale=true");
  if (env.execution_allowed === false) bits.push("execution_allowed=false (자동 집행 불가)");
  return el("div", { class: "envelope-footer" },
    el("div", { class: "as-of", text: bits.join(" · ") }),
    env.disclaimer ? el("p", { class: "disclaimer", text: env.disclaimer }) : null
  );
}

export { DataMissingError };
