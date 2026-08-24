// 7. 조례 실효성 — 전국 shard api/effectiveness/{sig}.json, 폴백 api/effectiveness.json
//    ★ 조례-예산 링크는 확률적 자동매칭이다. "추정 연결" 배지 + confidence 등급 표기 필수.
import { el, num, won, pct, extLink } from "../util.js";
import { loadRegionCatalog, shardCoverage, loadRegionalShard, loadFixture } from "../api.js";
import { section, table, note, loading, asOfLine, errorPanel, badge, statCard,
         confidenceBadge, statusBadge, envelopeFooter, cdnFailPanel } from "../components.js";
import { regionSelector, notPrecomputedPanel, sourceLine } from "../nationwide.js";
import { ensureChart } from "../vendor.js";

const PREFERRED = ["47190", "11110"];

export async function render(root, params = {}, query = {}) {
  root.appendChild(loading("전국 지자체 목록을 불러오는 중…"));

  let cat;
  try {
    cat = await loadRegionCatalog();
  } catch (e) {
    root.innerHTML = "";
    root.appendChild(errorPanel(e, "지역 목록(regions/index.json · api/index.json)을 읽지 못했습니다."));
    return;
  }

  const covered = await shardCoverage("effectiveness");
  let fixtureOnly = false;
  if (!covered.size) {
    // 색인이 없다 — 기존 단일 파일이 어느 지역 결과인지 scope 에서 읽는다.
    fixtureOnly = true;
    try {
      const env = await loadFixture("effectiveness");
      const dd = env.data || {};
      const keys = Object.keys(dd).every((k) => /^\d{4,5}$/.test(k)) && Object.keys(dd).length
        ? Object.keys(dd)
        : [dd.scope?.sig_cd || dd.scope?.region_id || dd.region_id].filter(Boolean);
      for (const cd of keys) covered.add(String(cd));
    } catch (e) { /* 커버리지 미상 */ }
  }

  const items = cat.items.slice();
  const seen = new Set(items.map((i) => i.sig_cd));
  for (const cd of covered) {
    if (seen.has(cd)) continue;
    const found = (cat.all || []).find((x) => x.sig_cd === cd);
    items.push(found || { sig_cd: cd, name: null, level: null, sido: cat.sidoOf(cd) });
    seen.add(cd);
  }
  items.sort((a, b) => (a.sig_cd < b.sig_cd ? -1 : a.sig_cd > b.sig_cd ? 1 : 0));

  root.innerHTML = "";
  if (!items.length) {
    root.appendChild(errorPanel(new Error("region list is empty"),
      "선택할 지자체가 없습니다. regions/index.json 또는 api/index.json 을 확인하세요."));
    return;
  }

  const querySig = query && query.sig ? String(query.sig) : null;
  const initial = (querySig && (covered.has(querySig) || seen.has(querySig)) ? querySig : null)
    || PREFERRED.find((cd) => covered.has(cd))
    || [...covered][0]
    || PREFERRED.find((cd) => seen.has(cd))
    || items[0].sig_cd;

  const nameOf = (sig) => {
    const it = items.find((x) => x.sig_cd === sig);
    return it && it.name ? it.name : null;
  };

  const body = el("div", {});
  root.appendChild(regionSelector({
    items, sidoOf: cat.sidoOf, current: initial, covered,
    onChange: (sig) => { draw(sig); },
  }));
  if (query && query.policy) {
    root.appendChild(note(`agent 정책 키워드: ${query.policy}`));
  }
  root.appendChild(el("div", { class: "as-of", text:
    `전국 ${cat.items.length}곳 선택 가능`
    + (cat.hasApiIndex ? ` · 사전계산 ${covered.size}곳 (api/index.json)` : "")
    + (fixtureOnly ? ` · api/index.json 없음 → 기존 단일 fixture ${covered.size}곳만 사전계산됨` : "") }));
  root.appendChild(body);

  let token = 0;
  async function draw(sig) {
    const my = ++token;
    body.innerHTML = "";
    body.appendChild(loading(`${nameOf(sig) || sig} 의 조례-예산 연결을 불러오는 중…`));
    const res = await loadRegionalShard("effectiveness", sig);
    if (my !== token) return;
    body.innerHTML = "";
    if (!res.data) {
      body.appendChild(notPrecomputedPanel({
        kind: "effectiveness", sig, name: nameOf(sig),
        tried: res.tried || [],
        fixtureRegions: res.fixtureRegions || [...covered],
        onPick: (cd) => draw(cd),
      }));
      return;
    }
    await renderBody(body, res.data, res.env, res);
  }

  await draw(initial);
}

/** 지역 1곳의 실효성 결과 렌더 */
async function renderBody(root, d, env, res) {
  const t = d.totals || {};
  const v = d.verification || {};

  // ★ 최상단 경고 — 이 화면 전체가 추정치임을 감추지 않는다
  root.appendChild(el("div", { class: "banner banner-est", role: "note" },
    el("span", { class: "banner-tag", text: "추정 연결" }),
    el("span", { class: "banner-body", text:
      "조례↔예산 연결은 도메인명사 교집합·분야게이트·부서가중 3채널 자동매칭 결과다. "
      + "verified=1 인 링크만 '확인됨'이며, 나머지는 모두 추정이다. 아래 집행률은 참고치다." })
  ));

  const scope = d.scope || {};
  root.appendChild(section(`연결 요약 — ${scope.name || scope.sig_cd || scope.region_id || "지역 미상"}`,
    asOfLine(`engine=${d._engine || "?"}`),
    el("div", { class: "stat-grid" },
      statCard("링크 수", num(d.link_count), `예산 세부사업 ${num(d.budget_lines)}건`),
      statCard("확인됨(verified)", num(v.verified_links), "수작업 검증 완료"),
      statCard("자동매칭", num(v.auto_links), "미검증 — 추정"),
      statCard("편성액(alloc)", won(t.alloc_amt), null),
      statCard("예산현액", won(t.budget_now), null),
      statCard("지출액", won(t.exe_amt), null),
      statCard("집행률(현액 대비)", pct(t.exec_rate_vs_now), null),
      statCard("집행률(편성 대비)", pct(t.exec_rate_vs_alloc), null)
    ),
    el("div", { class: "chip-row" },
      badge(`검증 상태: ${v.status || "unknown"}`, v.status === "verified" ? "badge-verified" : "badge-warn"),
      d.min_confidence !== undefined ? badge(`min_confidence=${d.min_confidence}`, "badge-plain") : null,
      d.fyr_filter ? badge(`회계연도 필터 ${d.fyr_filter}`, "badge-plain") : badge("회계연도 전체", "badge-plain")
    ),
    v.note ? note(v.note, "warn") : null,
    d.caveat ? note(d.caveat, "warn") : null,
    // 링크 0건을 그냥 두면 '이 지자체는 조례를 예산에 반영하지 않았다'로 오독된다.
    // 실제 원인은 예산 원자료 자체가 없는 것과, 예산은 있는데 매칭이 안 된 것 두 가지다.
    // 실측: 전국 243곳 중 32곳(전남광주통합특별시 28 + 인천 개편 4구)이 전자다.
    !d.link_count ? note(
      (d.region_budget_baseline || {}).lines
        ? "링크 0건 — 이 지역의 예산 세부사업은 있으나 조례와 자동매칭된 것이 없다. "
          + "«조례가 예산에 반영되지 않았다»는 뜻이 아니라 «매칭에 실패했다»는 뜻이며, "
          + "집행률을 이 지역의 정책 성과로 읽으면 안 된다."
        : "링크 0건 — 이 지역은 예산 원자료(세부사업)가 DB에 전혀 없어 조례-예산 연결을 계산할 수 없다. "
          + "2026년 통합·개편으로 신설된 지자체는 결산 통계가 아직 산출되지 않아 이 상태가 된다. "
          + "«예산을 집행하지 않았다»는 뜻이 결코 아니다.",
      "warn") : null,
    note("등급 기준: verified=1 → 확인됨 / confidence≥0.8 → 추정(높음) / 0.6~0.8 → 추정(중간) / 0.6 미만 → 추정(낮음). "
      + "표본 584건 수작업 검증에서 전체 정밀도 64.9%, confidence≥0.8 구간 93.2%였다 "
      + "(검증 시점 링크 93,964건 기준이라 현재 모집단에 그대로 적용되지는 않는다).")
  ));

  // 회계연도별
  const fy = d.by_fiscal_year || [];
  if (fy.length) {
    const sec = section("회계연도별");
    root.appendChild(sec);
    const canvas = el("canvas", { height: "300" });
    sec.appendChild(el("div", { class: "chart-box chart-box-sm" }, canvas));
    const tbl = table(["회계연도", "링크", "편성액", "예산현액", "지출액", "집행률(현액)", "집행률(편성)", "지출기준일"],
      fy.map((r) => [r.fyr, num(r.lines), won(r.alloc_amt), won(r.budget_now), won(r.exe_amt),
        pct(r.exec_rate_vs_now), pct(r.exec_rate_vs_alloc), r.exe_ymd || "—"]));
    try {
      await ensureChart();
      new window.Chart(canvas.getContext("2d"), {
        data: {
          labels: fy.map((r) => r.fyr),
          datasets: [
            { type: "bar", label: "예산현액", data: fy.map((r) => r.budget_now), backgroundColor: "#bcd4f0" },
            { type: "bar", label: "지출액", data: fy.map((r) => r.exe_amt), backgroundColor: "#2c66a8" },
            { type: "line", label: "집행률(현액 대비)", data: fy.map((r) => r.exec_rate_vs_now),
              borderColor: "#c0392b", yAxisID: "y1", tension: 0.2 },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          scales: {
            y: { beginAtZero: true, title: { display: true, text: "원" } },
            y1: { position: "right", beginAtZero: true, max: 1, grid: { drawOnChartArea: false },
                  title: { display: true, text: "집행률" } },
          },
        },
      });
      sec.appendChild(el("details", {}, el("summary", { text: "표로 보기" }), tbl));
    } catch (e) {
      canvas.parentElement.remove();
      sec.appendChild(cdnFailPanel("Chart.js(차트)", e, tbl));
    }
  }

  // 조례별
  const ords = d.by_ordinance || [];
  const sec2 = section(`조례별 연결 (${ords.length}건)`);
  root.appendChild(sec2);
  for (const o of ords) sec2.appendChild(ordinanceCard(o));

  if (d.region_budget_baseline) {
    root.appendChild(section("지역 예산 총량 대비",
      table(["항목", "값"], Object.entries(d.region_budget_baseline).map(([k, val]) =>
        [k, typeof val === "number" && Math.abs(val) > 1e6 ? won(val) : String(val)])),
      note("링크된 세부사업 금액이 지역 전체 예산에서 차지하는 비중을 보기 위한 참고값이다.")));
  }

  const src = sourceLine(res);
  if (src) root.appendChild(src);
  root.appendChild(envelopeFooter(env));
}

function ordinanceCard(o) {
  const programs = o.programs || [];
  const execNow = o.budget_now ? o.exe_amt / o.budget_now : (o.exec_rate_vs_now ?? null);
  const anyVerified = (o.verified_links || 0) > 0;

  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-head" },
    el("h3", { class: "card-title", text: o.name || o.ordinance_id }),
    el("div", { class: "chip-row" },
      o.status ? statusBadge(o.status) : null,
      badge(`세부사업 ${num(o.lines)}건`, "badge-info"),
      anyVerified ? badge(`확인됨 ${o.verified_links}건`, "badge-verified") : null,
      (o.auto_links || 0) > 0 ? badge(`추정 연결 ${o.auto_links}건`, "badge-est badge-mid") : null,
      o.verification_status ? badge(o.verification_status, "badge-plain") : null
    )));

  if (o.status === "repealed") {
    card.appendChild(el("div", { class: "caution" },
      el("b", { text: "⚠ 폐지된 조례 — " }),
      document.createTextNode("현행 정책의 근거로 인용하면 안 된다.")));
  }

  card.appendChild(el("div", { class: "kv-row" },
    kv("편성액", won(o.alloc_amt)), kv("예산현액", won(o.budget_now)),
    kv("지출액", won(o.exe_amt)), kv("집행률", pct(execNow)),
    o.region_id ? kv("지역", o.region_id) : null,
    o.official_url ? el("div", { class: "kv" }, el("span", { class: "kv-k", text: "원문" }),
      el("span", { class: "kv-v" }, extLink(o.official_url, "law.go.kr"))) : null
  ));

  if (o.methods) {
    card.appendChild(el("div", { class: "chip-row" },
      el("span", { class: "muted small", text: "매칭 채널: " }),
      ...Object.entries(o.methods).map(([m, c]) => badge(`${m} ${c}건`, "badge-plain"))));
  }

  card.appendChild(el("details", {},
    el("summary", { text: `연결된 세부사업 ${programs.length}건 — 개별 신뢰도 보기` }),
    table(["회계연도", "세부사업", "분야", "예산현액", "지출액", "집행률", "신뢰도", "매칭방법"],
      programs
        .slice()
        .sort((a, b) => (b.confidence || 0) - (a.confidence || 0))
        .map((p) => [
          p.fyr, p.dbiz_nm, p.field, won(p.budget_now), won(p.exe_amt), pct(p.exec_rate),
          confidenceBadge(p.confidence, p.verified), p.match_method || "—",
        ])),
    note("한 조례에 여러 회계연도·여러 분야의 사업이 붙는 것은 자동매칭 특성상 흔하다. "
      + "분야가 조례 주제와 어긋나는 행은 오매칭을 의심해야 한다.", "warn")
  ));

  return card;
}

function kv(k, v) {
  return el("div", { class: "kv" }, el("span", { class: "kv-k", text: k }), el("span", { class: "kv-v", text: v }));
}
