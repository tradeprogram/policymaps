/**
 * 전국 shard 화면 공용 UI 조각.
 *
 * 왜 별도 파일인가 — 격차·실효성 두 화면이 같은 "전국 243곳 지역 선택기"를,
 * 확산·표결 두 화면이 같은 "항목 선택기"를 쓴다. 네 벌 복제하지 않으려고 뺐다.
 * components.js 는 다른 작업과 겹칠 수 있어 건드리지 않고 새 모듈로 둔다.
 */
import { el, debounce } from "./util.js";
import { NATIONWIDE_GENERATOR, API_SHARDS } from "./config.js";

let uid = 0;

/**
 * 시도별 optgroup 으로 묶은 전국 지역 선택기 + 이름/코드 검색.
 *
 * @param {object} o
 * @param {Array}  o.items    loadRegionCatalog().items
 * @param {Function} o.sidoOf sig_cd -> 시도명
 * @param {string} o.current  현재 선택 sig_cd
 * @param {Set|null} o.covered 사전계산된 sig_cd 집합. 비어 있으면 "커버리지 미상"으로 다룬다.
 * @param {Function} o.onChange (sig_cd) => void
 */
export function regionSelector({ items, sidoOf, current, covered, onChange, label = "기준 지자체",
                                coveredLabel = "사전계산된 곳만", coveredWord = "사전계산" }) {
  const known = covered instanceof Set && covered.size > 0;
  const nameOf = (it) => it.name || it.sig_cd;

  const sel = el("select", { class: "sel sel-wide", "aria-label": `${label} 선택` });
  const filter = el("input", {
    class: "search-input", type: "search", placeholder: "지자체명·코드 검색 (예: 구미, 47190)",
    "aria-label": "지자체 검색",
  });
  const boxId = `nw-only-covered-${++uid}`;
  const onlyCovered = el("input", { type: "checkbox", id: boxId });
  const countLine = el("span", { class: "muted small" });

  function build() {
    const q = filter.value.trim().toLowerCase();
    const wantCovered = known && onlyCovered.checked;
    sel.innerHTML = "";

    const groups = new Map();
    let shown = 0;
    for (const it of items) {
      if (wantCovered && !covered.has(it.sig_cd)) continue;
      const sido = it.sido || sidoOf(it.sig_cd);
      if (q) {
        const hay = `${nameOf(it)} ${it.sig_cd} ${sido}`.toLowerCase();
        if (!hay.includes(q) && it.sig_cd !== current) continue;
      }
      if (!groups.has(sido)) groups.set(sido, []);
      groups.get(sido).push(it);
      shown++;
    }
    // 필터로 현재 선택이 사라지면 select 가 값을 잃는다 — 항상 살려 둔다.
    if (!groups.size) {
      const opt = el("option", { value: "", text: "검색 결과 없음", disabled: "disabled" });
      sel.appendChild(opt);
    }
    for (const [sido, list] of groups) {
      const og = el("optgroup", { label: `${sido} (${list.length})` });
      for (const it of list) {
        const mark = known ? (covered.has(it.sig_cd) ? "✓ " : "· ") : "";
        const o = el("option", { value: it.sig_cd, text: `${mark}${nameOf(it)} (${it.sig_cd})` });
        if (it.sig_cd === String(current)) o.selected = true;
        og.appendChild(o);
      }
      sel.appendChild(og);
    }
    countLine.textContent = `${shown}/${items.length}곳 표시`
      + (known ? ` · ${coveredWord} ${covered.size}곳(✓)` : ` · ${coveredWord} 커버리지 미상`);
  }

  filter.addEventListener("input", debounce(build, 150));
  onlyCovered.addEventListener("change", build);
  sel.addEventListener("change", () => { if (sel.value) onChange(sel.value); });
  build();

  return el("div", { class: "toolbar" },
    el("label", { text: `${label} ` }), sel,
    filter,
    known ? el("label", { class: "muted small", for: boxId }, onlyCovered, document.createTextNode(" " + coveredLabel)) : null,
    countLine
  );
}

/** 확산 템플릿 / 의안 등 항목 선택기. entries 가 비면 null 을 돌려준다. */
export function catalogSelector({ entries, current, onChange, label }) {
  if (!entries || entries.length <= 1) return null;
  const sel = el("select", { class: "sel", "aria-label": `${label} 선택` });
  for (const e of entries) {
    const o = el("option", { value: e.key, text: e.label });
    if (e.key === String(current)) o.selected = true;
    sel.appendChild(o);
  }
  sel.addEventListener("change", () => onChange(sel.value));
  return el("div", { class: "toolbar" },
    el("label", { text: `${label} ` }), sel,
    el("span", { class: "muted small", text: `${entries.length}건 중 선택` })
  );
}

/**
 * shard 가 없는 지역/항목 안내. 에러가 아니라 "아직 안 구웠다"는 상태다.
 * @param {object} o kind, sig/key, name, tried(시도한 경로들), fixtureRegions(대신 있는 지역)
 */
export function notPrecomputedPanel({ kind, sig, name, tried = [], fixtureRegions = null, onPick = null,
                                      subject = "지역" }) {
  const label = name ? `${name} (${sig})` : String(sig ?? "");
  const p = el("div", { class: "panel panel-warn" },
    el("h2", { class: "panel-title", text: `이 ${subject}은 아직 사전계산되지 않았습니다` }),
    el("p", { text: `${label} 의 ${KIND_LABEL[kind] || kind} 결과가 현재 번들에 없습니다. `
      + `전국 shard 를 굽고 나면 이 화면이 그대로 채워집니다.` }),
    el("p", { class: "hint" },
      el("b", { text: "생성 방법: " }),
      el("code", { text: genCommand(kind, sig) }),
      document.createTextNode(` → `),
      el("code", { text: `${API_SHARDS[kind] || `api/${kind}`}/${sig ?? "{key}"}.json` })
    )
  );
  if (fixtureRegions && fixtureRegions.length) {
    const row = el("div", { class: "chip-row" }, el("span", { class: "muted small", text: `현재 번들에 있는 ${subject}: ` }));
    for (const cd of fixtureRegions.slice(0, 40)) {
      row.appendChild(onPick
        ? el("button", { class: "btn", text: cd, onclick: () => onPick(cd) })
        : el("span", { class: "badge badge-plain", text: cd }));
    }
    p.appendChild(row);
  }
  if (tried.length) {
    p.appendChild(el("details", {},
      el("summary", { text: "시도한 경로" }),
      el("pre", { class: "err", text: tried.join("\n") })));
  }
  return p;
}

/** make_nationwide.py 실제 CLI 에 맞춘 생성 명령 (argparse: --only/--regions/--diffusion/--votes-top/--search) */
function genCommand(kind, key) {
  const base = `python ${NATIONWIDE_GENERATOR} --only ${kind}`;
  if (kind === "gap" || kind === "peers" || kind === "effectiveness") return `${base} --regions ${key ?? "11110,47190"}`;
  if (kind === "diffusion") return `${base} --diffusion "정책명=${key ?? "slug"}"`;
  if (kind === "votes") return `${base} --votes-top 30`;
  if (kind === "search") return `${base} --search "질의=${key ?? "slug"}"`;
  return base;
}

const KIND_LABEL = {
  gap: "격차분석",
  peers: "유사 지자체",
  effectiveness: "조례-예산 실효성",
  diffusion: "확산 타임라인",
  votes: "표결",
  search: "검색",
};

/** 어느 소스에서 왔는지 한 줄로 밝힌다(표기 규율 — 데이터 출처를 감추지 않는다). */
export function sourceLine(res) {
  if (!res) return null;
  const map = {
    "shard": `전국 shard — ${res.path || "api/…"}`,
    "fixture-map": "단일 fixture 폴백 — api/*.json 의 사전계산 5곳 맵",
    "fixture-single": "단일 fixture 폴백 — 기준 지역 1곳만 담긴 파일",
    "fixture": "단일 fixture — 색인(api/index.json) 없음",
    "fixture-fallback": "단일 fixture 폴백 — 해당 항목 shard 없음",
  };
  const t = map[res.source];
  if (!t) return null;
  return el("div", { class: "as-of", text: `데이터 소스: ${t}` });
}
