// 10. 정책 생애주기 — 제정 → 개정 → 폐지, 그리고 지자체 승계.
//
//    전국:   api/lifecycle_index.json  (연도곡선 · rr_cls_cd 분해 · 최다 폐지 정책 · 일괄폐지 코호트)
//    지역:   api/lifecycle/{sig_cd}.json (243곳)
//    승계:   api/succession.json        (region_succession 17건 · 사건 5건)
//
//    딥링크: #/lifecycle?tab=region&sig=47190
//
//    표기 규율 — 폐지 조례는 선례로 추천하지 않는다. 이 화면은 "왜 사라졌는가"의 이력이며
//    현행 근거가 아니다. 각 shard 의 caveat 를 그대로 노출한다.
import { el, num, ymd, extLink } from "../util.js";
import { getJSON, DataMissingError, loadRegionCatalog } from "../api.js";
import { section, table, note, loading, asOfLine, badge, statCard,
         envelopeFooter, cdnFailPanel, errorPanel } from "../components.js";
import { regionSelector } from "../nationwide.js";
import { ensureChart } from "../vendor.js";

const IDX_PATH = "api/lifecycle_index.json";
const EXTEND_INDEX = "api/extend_index.json";
const SUCCESSION_PATH = "api/succession.json";
const GENERATOR = "system/make_extend_fixtures.py";

const KIND_COLOR = {
  "제정": "#2c66a8", "일부개정": "#8ab4e2", "전부개정": "#5590cd",
  "타법개정": "#bcd4f0", "폐지": "#c0392b", "미상": "#bdc3c7",
};

export async function render(root, params, query = {}) {
  let tab = ["national", "region", "succession"].includes(query.tab) ? query.tab : "national";
  let sig = query.sig || null;

  const sec = section("정책 생애주기", asOfLine("제정 → 개정 → 폐지 이력과 지자체 승계"));
  root.appendChild(sec);

  const bar = el("div", { class: "toolbar" });
  const body = el("div", {});
  sec.appendChild(bar);
  sec.appendChild(body);

  const tabs = [
    { key: "national", label: "전국 요약" },
    { key: "region", label: "지자체별" },
    { key: "succession", label: "지자체 승계" },
  ];

  let token = 0;
  async function show() {
    const my = ++token;
    bar.innerHTML = "";
    for (const t of tabs) {
      const active = t.key === tab;
      bar.appendChild(el("button", {
        class: "btn", "aria-pressed": active ? "true" : "false",
        style: active ? "background:var(--brand);color:#fff;border-color:var(--brand);font-weight:600" : null,
        text: t.label,
        onclick: () => { if (!active) { tab = t.key; show(); } },
      }));
    }
    syncUrl(tab, sig);
    body.innerHTML = "";
    body.appendChild(loading("불러오는 중…"));
    const host = el("div", {});
    try {
      if (tab === "region") {
        // 목록 첫 항목으로 떨어졌을 때도 주소에 실제 sig_cd 를 남긴다.
        const resolved = await renderRegion(host, sig, (s) => { sig = s; show(); });
        if (resolved && resolved !== sig) { sig = resolved; syncUrl(tab, sig); }
      }
      else if (tab === "succession") await renderSuccession(host);
      else await renderNational(host);
    } catch (e) {
      host.appendChild(errorPanel(e, "정책 생애주기 렌더 실패"));
      console.error(e);
    }
    if (my !== token) return;
    body.innerHTML = "";
    body.appendChild(host);
  }

  await show();
}

function syncUrl(tab, sig) {
  const q = new URLSearchParams();
  q.set("tab", tab);
  if (sig && tab === "region") q.set("sig", sig);
  const next = `#/lifecycle?${q.toString()}`;
  if (location.hash !== next) history.replaceState(null, "", next);
}

function missingPanel(path, err, only) {
  return el("div", { class: "panel panel-warn" },
    el("h2", { class: "panel-title", text: "이 데이터가 현재 번들에 없습니다" }),
    el("p", { text: `${path} 를 찾지 못했습니다. 생애주기 화면은 폐지 조례 40,406건을 집계한 사전계산 파일을 씁니다.` }),
    el("p", { class: "hint" },
      el("b", { text: "생성 방법: " }),
      el("code", { text: `python ${GENERATOR} --only ${only}` })),
    err ? el("pre", { class: "err", text: `${err.name || "Error"}: ${err.message || err}` }) : null);
}

/* ==================================================================== *
 *  전국 요약
 * ==================================================================== */

async function renderNational(host) {
  let env;
  try { env = await getJSON(IDX_PATH); }
  catch (e) { host.appendChild(missingPanel(IDX_PATH, e, "lifecycle")); return; }
  const d = env.data || env;
  const t = d.totals || {};

  host.appendChild(el("div", { class: "stat-grid" },
    statCard("자치법규 전체", num(t.ordinances), "ordinances 행 수(개정본 포함)"),
    statCard("폐지", num(t.repealed),
      t.ordinances ? `전체의 ${((t.repealed / t.ordinances) * 100).toFixed(1)}%` : null),
    statCard("현행(in_force)", num((t.by_lifecycle || {}).in_force), "lifecycle 필드"),
    statCard("FRBR Work", num(t.work_rows), "ordinance_work 행")));

  if (t.by_rr_cls_cd) {
    host.appendChild(el("div", { class: "chip-row" },
      ...Object.entries(t.by_rr_cls_cd).sort((a, b) => b[1] - a[1])
        .map(([k, v]) => badge(`${k} ${num(v)}`, k === "폐지" ? "badge-repealed" : "badge-plain"))));
  }
  host.appendChild(note(
    "‘자치법규 전체’는 조례 종수가 아니라 ordinances 테이블의 행 수다. 같은 조례의 개정본이 여러 행으로 남는다 "
    + "— 제정 49,971 · 일부개정 100,109 처럼 rr_cls_cd 로 나뉜다."));

  // ── 공포 연도 곡선 (rr_cls_cd 누적) ──────────────────────────────
  const byKind = d.by_rr_cls_year || {};
  const years = [...new Set(Object.values(byKind).flat().map((r) => r.year))]
    .filter((y) => y >= 1995 && y <= 2030).sort((a, b) => a - b);
  const kindSec = section("연도별 공포 건수 (enacted_on) — 제정·개정·폐지 구분",
    note("enacted_on 은 그 본(本)의 공포일이다. 폐지본도 폐지 공포일로 여기 잡힌다. "
      + "곡선이 최근에 급증하는 것은 자치법규 수집·전산화가 최근일수록 촘촘하기 때문이기도 하다."));
  const kindRows = () => years.map((y) => [y, ...Object.keys(byKind).map((k) =>
    num((byKind[k].find((r) => r.year === y) || {}).n || 0))]);
  const kindTable = () => table(["연도", ...Object.keys(byKind)], kindRows());
  await lineChart(kindSec, {
    labels: years,
    datasets: Object.entries(byKind).map(([k, arr]) => ({
      type: "bar", label: k, stack: "s",
      backgroundColor: KIND_COLOR[k] || "#95a5a6",
      data: years.map((y) => (arr.find((r) => r.year === y) || {}).n || 0),
    })),
    yTitle: "건수", stacked: true, fallback: kindTable(),
  });
  kindSec.appendChild(el("details", {}, el("summary", { text: "표로 보기" }), kindTable()));
  host.appendChild(kindSec);

  // ── 폐지 연도 곡선 ───────────────────────────────────────────────
  const rep = (d.repealed_by_year || []).filter((r) => r.year >= 1995 && r.year <= 2030);
  const repSec = section("연도별 폐지 건수 (repealed_on)");
  const repTable = () => table(["연도", "폐지"], rep.map((r) => [r.year, num(r.n)]));
  await lineChart(repSec, {
    labels: rep.map((r) => r.year),
    datasets: [{ type: "line", label: "폐지", data: rep.map((r) => r.n),
                 borderColor: "#c0392b", backgroundColor: "#c0392b", tension: 0.25 }],
    yTitle: "건수", fallback: repTable(),
  });
  repSec.appendChild(el("details", {}, el("summary", { text: "표로 보기" }), repTable()));
  host.appendChild(repSec);

  // ── 최다 폐지 정책 ───────────────────────────────────────────────
  const top = d.top_repealed_policies || [];
  if (top.length) {
    host.appendChild(section(`가장 많은 지자체에서 사라진 정책 TOP ${Math.min(30, top.length)}`,
      note(`집계 기준: ${(d.method || {}).policy_key || "정규화 정책키"}`),
      table(["#", "정책(정규화 표시명)", "폐지한 지자체 수", "폐지 행 수"],
        top.slice(0, 30).map((p, i) => [i + 1, p.display_name || p.policy_key,
                                        num(p.regions), num(p.rows)])),
      top.length > 30
        ? el("details", {}, el("summary", { text: `나머지 ${top.length - 30}건` }),
            table(["#", "정책", "지자체 수", "행 수"],
              top.slice(30).map((p, i) => [i + 31, p.display_name || p.policy_key,
                                           num(p.regions), num(p.rows)])))
        : null));
  }

  // ── 일괄폐지 코호트 ──────────────────────────────────────────────
  const cohorts = d.mass_repeal_cohorts || [];
  if (cohorts.length) {
    const cs = section(`같은 날 무더기로 사라진 조례 — 일괄폐지 코호트 ${num(cohorts.length)}건`,
      note(`정의: ${(d.method || {}).cohort || "같은 정책키를 같은 날 다수 지자체가 폐지한 묶음"}`, "warn"));
    cs.appendChild(note(
      "위 TOP 표(‘몇 곳이 폐지했나’)와 이 코호트(‘같은 날 몇 곳이 동시에 폐지했나’)는 다른 지표다. "
      + "예컨대 저탄소 녹색성장 기본 조례는 전국 179곳이 폐지했지만 폐지일이 흩어져 있어 코호트로는 잡히지 않는다."));
    for (const c of cohorts) {
      const parents = (c.linked_parents || []).filter((p) => p.name);
      cs.appendChild(el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h3", { class: "card-title", text: c.display_name || c.policy_key }),
          el("div", { class: "chip-row" },
            badge(`${c.regions}곳 동시 폐지`, "badge-repealed"),
            badge(`폐지일 ${c.repealed_on}`, "badge-plain"))),
        parents.length
          ? el("div", { class: "chip-row" },
              el("span", { class: "muted small", text: "확인된 상위법 위임: " }),
              ...parents.map((p) => badge(`${p.name} (위임 ${num(p.delegation_rows)}행)`, "badge-info")))
          : note("이 코호트에서는 상위법 위임행이 확인되지 않았다.", "warn"),
        el("div", { class: "card-foot" },
          el("span", { class: "muted small",
            text: `표본 조례 ${(c.sample_ordinance_ids || []).slice(0, 3).join(", ")}` }))));
    }
    if (cohorts[0] && cohorts[0].linked_parents_note) cs.appendChild(note(cohorts[0].linked_parents_note, "warn"));
    host.appendChild(cs);
  }

  // ── 데이터 품질 ──────────────────────────────────────────────────
  const q = d.data_quality || {};
  host.appendChild(section("이 화면의 데이터 결함 공시",
    table(["항목", "값"], [
      ["폐지 조례 행", num(q.repealed_rows)],
      ["repealed_on 파싱 불가", num(q.unparseable_repealed_on)],
      ["sentinel '99991231'", num(q.sentinel_99991231)],
      ["official_url 상대경로 행", num(q.relative_official_url_rows)],
    ]),
    q.note ? note(q.note, "warn") : null,
    q.url_note ? note(q.url_note, "warn") : null));

  if (d.caveat) host.appendChild(note(d.caveat, "warn"));
  host.appendChild(envelopeFooter(env));
}

/* ==================================================================== *
 *  지자체별
 * ==================================================================== */

let coveredPromise = null;

/** api/extend_index.json 의 lifecycle 목록 → {sig_cd: {name, repealed, path}} */
async function lifecycleCoverage() {
  if (!coveredPromise) {
    coveredPromise = (async () => {
      try {
        const idx = await getJSON(EXTEND_INDEX);
        const out = new Map();
        for (const e of idx.lifecycle || []) {
          const sig = String(e.sig_cd || e.key || "");
          if (sig) out.set(sig, e);
        }
        return out;
      } catch (e) { return new Map(); }
    })();
  }
  return coveredPromise;
}

async function renderRegion(host, sig, onPick) {
  const cover = await lifecycleCoverage();
  const catalog = await loadRegionCatalog().catch(() => null);
  const covered = new Set(cover.keys());
  const current = sig && covered.size ? sig : (sig || [...covered][0] || null);

  if (catalog && catalog.items.length) {
    host.appendChild(regionSelector({
      items: catalog.items, sidoOf: catalog.sidoOf, current, covered,
      onChange: onPick, label: "지자체",
      coveredLabel: "생애주기 있는 곳만", coveredWord: "생애주기",
    }));
  } else if (covered.size) {
    const sel = el("select", { class: "sel sel-wide" },
      ...[...cover.values()].map((e) => {
        const o = el("option", { value: e.sig_cd, text: `${e.name} (${e.sig_cd})` });
        if (String(e.sig_cd) === String(current)) o.selected = true;
        return o;
      }));
    sel.addEventListener("change", () => onPick(sel.value));
    host.appendChild(el("div", { class: "toolbar" }, el("label", { text: "지자체 " }), sel));
  }

  if (!current) {
    host.appendChild(missingPanel("api/lifecycle/{sig_cd}.json", null, "lifecycle"));
    return null;
  }

  const detail = el("div", {});
  host.appendChild(detail);
  detail.appendChild(loading("지자체 생애주기를 불러오는 중…"));

  const path = `api/lifecycle/${current}.json`;
  let env;
  try { env = await getJSON(path); }
  catch (e) {
    detail.innerHTML = "";
    if (e instanceof DataMissingError) {
      const meta = catalog?.items.find((i) => i.sig_cd === String(current));
      detail.appendChild(el("div", { class: "panel panel-warn" },
        el("h2", { class: "panel-title", text: "이 지자체는 아직 사전계산되지 않았습니다" }),
        el("p", { text: `${meta?.name || current} (${current}) 의 생애주기 shard 가 없습니다. `
          + `현재 ${covered.size}곳이 구워져 있습니다.` }),
        el("p", { class: "hint" }, el("b", { text: "생성 방법: " }),
          el("code", { text: `python ${GENERATOR} --only lifecycle` }),
          document.createTextNode(" → "), el("code", { text: path }))));
    } else {
      detail.appendChild(errorPanel(e, `${path} 로드 실패`));
    }
    return current;
  }

  detail.innerHTML = "";
  const d = env.data || env;
  const r = d.region || {};
  const c = d.counts || {};

  detail.appendChild(el("h3", { style: "margin:6px 0", text: `${r.full_name || r.name || current}` }));
  detail.appendChild(el("div", { class: "as-of", text: `데이터 소스: ${path}` }));
  detail.appendChild(el("div", { class: "stat-grid" },
    statCard("자치법규 행", num(c.total)),
    statCard("현행", num(c.active)),
    statCard("폐지", num(c.repealed),
      c.total ? `${((c.repealed / c.total) * 100).toFixed(1)}%` : null),
    statCard("제정본", num((c.by_rr_cls_cd || {})["제정"]), "rr_cls_cd='제정'")));

  const ey = d.enacted_by_year || [];
  const ry = d.repealed_by_year || [];
  const years = [...new Set([...ey.map((x) => x.year), ...ry.map((x) => x.year)])].sort((a, b) => a - b);
  const tbl = () => table(["연도", "공포(제·개정)", "폐지"],
    years.map((y) => [y, num((ey.find((x) => x.year === y) || {}).n || 0),
                      num((ry.find((x) => x.year === y) || {}).n || 0)]));
  const cSec = section("연도별 공포·폐지");
  await lineChart(cSec, {
    labels: years,
    datasets: [
      { type: "bar", label: "공포(enacted_on)", data: years.map((y) => (ey.find((x) => x.year === y) || {}).n || 0),
        backgroundColor: "#8ab4e2" },
      { type: "line", label: "폐지(repealed_on)", data: years.map((y) => (ry.find((x) => x.year === y) || {}).n || 0),
        borderColor: "#c0392b", backgroundColor: "#c0392b", tension: 0.25 },
    ],
    yTitle: "건수", fallback: tbl(),
  });
  cSec.appendChild(el("details", {}, el("summary", { text: "표로 보기" }), tbl()));
  detail.appendChild(cSec);

  const top = d.top_repealed_policies || [];
  if (top.length) {
    detail.appendChild(section("이 지자체가 폐지한 정책",
      note("2차 정렬은 ‘전국에서 몇 곳이 같은 정책을 폐지했나’다. 전국적으로 사라진 정책이 위로 온다."),
      table(["정책", "이 지자체 폐지 행", "전국 폐지 지자체"],
        top.map((p) => [p.display_name || p.policy_key, num(p.rows), num(p.nationwide_regions)]))));
  }

  const recents = d.recent_repeals || [];
  if (recents.length) {
    detail.appendChild(section(`최근 폐지 ${num(recents.length)}건`,
      note("폐지 조례는 선례로 추천하지 않는다. 아래 링크는 이력 확인용 원문이다.", "warn"),
      table(["조례", "종류", "제정", "폐지", "원문"],
        recents.map((x) => [
          x.name, x.ord_kind || "—", ymd(x.enacted_on), ymd(x.repealed_on),
          extLink(x.official_url, "law.go.kr")]))));
  }

  const q = d.data_quality || {};
  if (q.unparseable_repealed_on || q.sentinel_99991231) {
    detail.appendChild(note(
      `이 지자체 데이터 결함: repealed_on 파싱 불가 ${num(q.unparseable_repealed_on)}건 · `
      + `sentinel '99991231' ${num(q.sentinel_99991231)}건 (연도 곡선에서 제외됨)`, "warn"));
  }
  if (d.caveat) detail.appendChild(note(d.caveat, "warn"));
  detail.appendChild(envelopeFooter(env));
  return current;
}

/* ==================================================================== *
 *  지자체 승계
 * ==================================================================== */

async function renderSuccession(host) {
  let env;
  try { env = await getJSON(SUCCESSION_PATH); }
  catch (e) { host.appendChild(missingPanel(SUCCESSION_PATH, e, "succession")); return; }
  const d = env.data || env;
  const t = d.totals || {};

  host.appendChild(el("div", { class: "stat-grid" },
    statCard("승계 관계", num(t.rows), "region_succession"),
    statCard("승계 사건", num(t.events), "시행일·유형 묶음"),
    statCard("승계로 사라진 지자체",
      num(((t.regions_by_status || {}).merged || 0) + ((t.regions_by_status || {}).renamed || 0)),
      `merged ${num((t.regions_by_status || {}).merged)} · renamed ${num((t.regions_by_status || {}).renamed)}`),
    statCard("승계 대상 조례", num(t.ordinances_in_superseded_regions), "구 코드에 남은 조례")));

  for (const ev of d.events || []) {
    host.appendChild(el("div", { class: "card" },
      el("div", { class: "card-head" },
        el("h3", { class: "card-title", text: `${ev.effective_date} · ${ev.succession_type}` }),
        el("div", { class: "chip-row" }, badge(`${ev.count}건`, "badge-info"))),
      el("div", { class: "kv-row" },
        el("div", { class: "kv" }, el("span", { class: "kv-k", text: "구 지자체" }),
          el("span", { class: "kv-v", text: (ev.old_regions || []).join(", ") })),
        el("div", { class: "kv" }, el("span", { class: "kv-k", text: "신 지자체" }),
          el("span", { class: "kv-v", text: (ev.new_regions || []).join(", ") }))),
      el("p", { class: "small muted", text: `근거: ${ev.legal_basis || "—"}` })));
  }

  host.appendChild(section("승계 관계 전수",
    table(["구 지자체", "조례", "→", "신 지자체", "조례", "유형", "시행일"],
      (d.successions || []).map((s) => [
        `${s.old.full_name || s.old.name} (${s.old.sig_cd})`, num(s.old.ordinances), "→",
        `${s.new.full_name || s.new.name} (${s.new.sig_cd})`, num(s.new.ordinances),
        s.succession_type, s.effective_date])),
    el("details", {}, el("summary", { text: "확인 근거(status_note)" }),
      table(["구 → 신", "확인 근거"],
        (d.successions || []).map((s) => [
          `${s.old.name} → ${s.new.name}`, s.status_note || "—"])))));

  const a = d.audit || {};
  host.appendChild(section("승계 잔여 — 시간 무결성 감사",
    table(["규칙", "건수", "뜻"], [
      ["T7_region_no_longer_exists", num(a.T7_region_no_longer_exists),
       "조례가 이미 사라진 지자체 코드를 가리킨다"],
      ["T8_orphan_region", num(a.T8_orphan_region), "조례의 region_id 가 regions 에 없다"],
    ]),
    a.note ? note(a.note, "warn") : null,
    el("p", { class: "hint" }, document.createTextNode("감사 전체는 "),
      el("a", { href: "#/trust", text: "검증 공시 화면" }),
      document.createTextNode(" 에서 본다."))));

  if (d.caveat) host.appendChild(note(d.caveat, "warn"));
  host.appendChild(envelopeFooter(env));
}

/* ==================================================================== *
 *  차트 헬퍼 — CDN 실패 시 표로 떨어진다
 * ==================================================================== */

async function lineChart(sec, { labels, datasets, yTitle, stacked = false, fallback = null }) {
  const canvas = el("canvas");
  const box = el("div", { class: "chart-box" }, canvas);
  sec.appendChild(box);
  try {
    await ensureChart();
    new window.Chart(canvas.getContext("2d"), {
      data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { stacked },
          y: { stacked, beginAtZero: true, title: { display: !!yTitle, text: yTitle || "" } },
        },
      },
    });
  } catch (e) {
    box.remove();
    sec.appendChild(cdnFailPanel("Chart.js(차트)", e, fallback));
  }
}
