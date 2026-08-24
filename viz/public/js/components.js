// 화면 공통 조각 — 배너, 배지, 카드, 표, 에러 패널
import { el, esc, num, pct, ymd } from "./util.js";
import { state, full, DataMissingError, FIXTURE_TOOLS } from "./api.js";
import { CONFIDENCE_GRADES } from "./config.js";

/* ---------- 상단 배너 ---------- */

/**
 * 가상데이터 경고 배너.
 *
 * 커밋 91e813d 가 발표 스크린샷을 위해 이 배너를 통째로 없앴는데(항상 null 반환),
 * 그 결과 ?src=mock 으로 열면 조례 302건짜리 표본이 **아무 표시 없이** 실데이터와
 * 똑같은 모습으로 렌더됐다. 목업을 실측치로 오해하게 만드는 건 배너가 화면을 조금
 * 가리는 것보다 훨씬 나쁘다. 화면을 밀지 않는 얇은 띠로 되살린다.
 */
export function mockBanner() {
  if (!state.isMock) return null;
  return el("div", { class: "banner banner-mock", role: "alert" },
    el("span", { class: "banner-tag", text: "가상 데이터" }),
    el("span", {
      class: "banner-body",
      text: state.manifest?._mock_warning
        || "가상(mock) 데이터입니다. 실제 수치가 아니므로 정책 판단의 근거로 쓸 수 없습니다. "
           + "실데이터로 보려면 주소에서 ?src=mock 을 빼세요.",
    })
  );
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

/**
 * 완전판(로컬 DB 직결) 배너.
 *  - 연결됨: 조문 본문·임의 질의 검색이 켜졌다는 사실을 알린다.
 *  - ?full=1 로 요청했는데 실패: 왜 실패했는지 보여준다(조용히 넘어가면 오해를 준다).
 *  - 요청하지 않았는데 없음(=배포본 기본): 배너를 띄우지 않는다.
 */
export function fullEditionBanner() {
  if (!full.probed) return null;
  if (full.enabled) {
    const st = full.status || {};
    const t = st.tables || {};
    const rag = st.rag || {};
    const bits = [
      t.ordinance_articles ? `조문 본문 ${num(t.ordinance_articles)}건` : null,
      t.ordinances ? `자치법규 ${num(t.ordinances)}건` : null,
      rag.exists ? `전문검색 색인 ${num(rag.n_docs)}문서${rag.ready ? "" : " (예열 중 — 첫 질의가 느립니다)"}` : "전문검색 색인 없음 (이름 검색으로 강등)",
    ].filter(Boolean);
    return el("div", { class: "banner banner-full", role: "status" },
      el("span", { class: "banner-tag", text: "완전판" }),
      el("span", { class: "banner-body",
        text: `로컬 DB 연결됨 — ${bits.join(" · ")}. 상세 화면은 정적 shard 대신 DB 를 직접 읽습니다.` }),
      el("span", { class: "banner-src", text: full.base })
    );
  }
  if (full.requested === "1" || full.requested === "on" || full.requested === "true") {
    return el("div", { class: "banner banner-mock", role: "alert" },
      el("span", { class: "banner-tag", text: "완전판 실패" }),
      el("span", { class: "banner-body",
        text: "?full=1 로 요청했지만 DB API 에 연결하지 못해 정적 shard 로 동작합니다. "
            + "저장소 루트에서 `python viz/serve_full.py` 를 띄운 뒤 그 주소로 여세요." }),
      el("span", { class: "banner-src", text: full.error || "" })
    );
  }
  return null;
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
  const panel = el("section", { class: "panel" });
  if (title) {
    const qa = el("button", {
      class: "panel-qa",
      type: "button",
      title: "이 패널을 챗봇에게 설명시키기",
      text: "Q&A",
      onclick: () => {
        window.dispatchEvent(new CustomEvent("agent:panel-question", {
          detail: {
            title: String(title),
            route: window.location.hash.replace(/^#/, "") || "/dashboard",
            text: panel.textContent.replace(/\s+/g, " ").trim().slice(0, 900),
          },
        }));
      },
    });
    panel.appendChild(el("div", { class: "panel-title-row" },
      el("h2", { class: "panel-title", text: title }),
      qa
    ));
  }
  panel.append(...children.filter(Boolean));
  return panel;
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
      // 예전 문구는 '실데이터 번들에는 원래 없다'고 단언했는데 사실이 아니다 —
      // system/data/api/ 에 gap·peers·effectiveness·diffusion·votes·search 가 모두 있다.
      // 그 서술은 진짜 원인(경로·shard 누락)을 가리고 목업을 정답처럼 보이게 했다.
      text: `이 화면은 사전계산 결과 파일(api/${key}.json)을 씁니다. 현재 데이터 소스에서 그 파일을 찾지 못했습니다 — `
        + `데이터 경로(DATA_BASE)와 웹 루트를 확인하세요. 가상데이터(?src=mock)로 열었다면 표본만 들어 있습니다.`,
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
