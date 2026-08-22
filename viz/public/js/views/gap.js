// 4. 유사 지자체 + 격차분석 (킬러 화면)
//    전국 shard: api/peers/{sig}.json · api/gap/{sig}.json
//    폴백:       api/peers.json · api/gap.json 의 data[sig] (사전계산 5곳)
//    "N곳 보유 / M곳 폐지" 경고 배지 필수.
import { el, num, pct, ymd, extLink } from "../util.js";
import { loadRegionCatalog, shardCoverage, loadRegionalShard, loadFixture } from "../api.js";
import { section, table, note, loading, asOfLine, errorPanel,
         badge, envelopeFooter, cdnFailPanel } from "../components.js";
import { regionSelector, notPrecomputedPanel, sourceLine } from "../nationwide.js";
import { ensureChart } from "../vendor.js";

/** 완료판정 시나리오(구미시)를 기본으로 노출하고, 없으면 사전계산된 첫 곳으로 */
const PREFERRED = ["47190", "11110"];

export async function render(root) {
  root.appendChild(loading("전국 지자체 목록을 불러오는 중…"));

  let cat;
  try {
    cat = await loadRegionCatalog();
  } catch (e) {
    root.innerHTML = "";
    root.appendChild(errorPanel(e, "지역 목록(regions/index.json · api/index.json)을 읽지 못했습니다."));
    return;
  }

  // 사전계산 커버리지 — api/index.json 이 있으면 거기서, 없으면 기존 단일 fixture 의 키에서.
  const covered = new Set();
  for (const s of await Promise.all([shardCoverage("gap"), shardCoverage("peers")])) {
    for (const cd of s) covered.add(cd);
  }
  let fixtureOnly = false;
  if (!covered.size) {
    fixtureOnly = true;
    for (const kind of ["gap", "peers"]) {
      try {
        const env = await loadFixture(kind);
        const keys = Array.isArray(env.regions) ? env.regions : Object.keys(env.data || {});
        for (const cd of keys) if (/^\d{4,5}$/.test(String(cd))) covered.add(String(cd));
      } catch (e) { /* 없으면 커버리지 미상 */ }
    }
  }

  // 선택기 목록 = 전국 level 1·2 + (사전계산된 일반구처럼 목록 밖이지만 결과가 있는 곳)
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

  const initial = PREFERRED.find((cd) => covered.has(cd))
    || [...covered][0]
    || PREFERRED.find((cd) => seen.has(cd))
    || items[0].sig_cd;

  const nameOf = (sig) => {
    const it = items.find((x) => x.sig_cd === sig);
    return it && it.name ? it.name : null;
  };

  const body = el("div", {});
  const picker = regionSelector({
    items, sidoOf: cat.sidoOf, current: initial, covered,
    onChange: (sig) => { draw(sig); },
  });
  root.appendChild(picker);
  root.appendChild(el("div", { class: "as-of", text:
    `전국 ${cat.items.length}곳 선택 가능`
    + (cat.hasApiIndex ? ` · 사전계산 ${covered.size}곳 (api/index.json)` : "")
    + (fixtureOnly ? ` · api/index.json 없음 → 기존 단일 fixture ${covered.size}곳만 사전계산됨` : "")
    + ` · 목록 출처 ${(cat.sources || []).join(", ") || "없음"}` }));
  root.appendChild(body);

  let token = 0;
  async function draw(sig) {
    const my = ++token;
    body.innerHTML = "";
    body.appendChild(loading(`${nameOf(sig) || sig} 결과를 불러오는 중…`));
    const [pRes, gRes] = await Promise.all([
      loadRegionalShard("peers", sig),
      loadRegionalShard("gap", sig),
    ]);
    if (my !== token) return; // 빠르게 바꾸면 늦게 온 응답은 버린다
    body.innerHTML = "";

    if (!pRes.data && !gRes.data) {
      body.appendChild(notPrecomputedPanel({
        kind: "gap", sig, name: nameOf(sig),
        tried: [...(gRes.tried || []), ...(pRes.tried || [])],
        fixtureRegions: gRes.fixtureRegions || pRes.fixtureRegions || [...covered],
        onPick: (cd) => draw(cd),
      }));
      return;
    }

    if (pRes.data) { renderPeers(body, pRes.data, pRes.env, pRes); }
    else body.appendChild(notPrecomputedPanel({ kind: "peers", sig, name: nameOf(sig), tried: pRes.tried || [] }));

    if (gRes.data) { await renderGap(body, gRes.data, gRes.env, gRes); }
    else body.appendChild(notPrecomputedPanel({ kind: "gap", sig, name: nameOf(sig), tried: gRes.tried || [] }));
  }

  await draw(initial);
}

/** 어느 소스(전국 shard / 단일 fixture)에서 온 결과인지 패널에 밝힌다. */
function appendSource(sec, res) {
  const line = sourceLine(res);
  if (line) sec.appendChild(line);
}

/* ---------------- 유사 지자체 ---------------- */

function renderPeers(root, d, env, res) {
  const t = d.target || d.base_region || {};
  const method = d.method || {};

  const sec = section(`유사 지자체 — ${t.name || t.region_id || "?"}`,
    asOfLine(`k=${d.k ?? (d.peers || []).length} · engine=${d._engine || "analytics.peers.find_similar_governments"}`)
  );
  root.appendChild(sec);

  // 일반구(level=3) 등 비교 대상이 아닌 경우 — 엔진이 명시적으로 거부한다 (P1-2)
  if (d.reason && !(d.peers && d.peers.length)) {
    sec.appendChild(note(d.reason, "warn"));
    if (d.parent_region) {
      const pr = d.parent_region;
      sec.appendChild(note(`모(母) 자치단체 「${pr.name || pr.region_id || pr}」 로 조회하면 비교 결과가 나온다.`));
    }
    appendSource(sec, res);
    sec.appendChild(envelopeFooter(env));
    return;
  }

  if (t.indicators) {
    sec.appendChild(el("h3", { text: "기준 지자체 지표" }));
    sec.appendChild(table(["지표", "값"], indicatorRows(t.indicators)));
  }

  const peers = d.peers || [];
  sec.appendChild(el("h3", { text: `유사 지자체 ${peers.length}곳` }));
  // "0곳" 을 그냥 두면 '유사한 곳이 없다'는 결론으로 읽힌다. 실제 원인은 대개
  // 기준 지자체의 재정 지표가 DB 에 없어 유사도를 계산할 수 없는 것이다.
  // 원인을 숨기지 않는다 (표기 규율).
  if (!peers.length) sec.appendChild(missingIndicatorNote(t.indicators));
  sec.appendChild(table(
    ["순위", "지자체", "유형", "유사도", "가중거리", "인구", "재정자립도", "복지비율"],
    peers.map((p, i) => [
      i + 1,
      p.name,
      p.rtype || "—",
      typeof p.similarity === "number" ? p.similarity.toFixed(4) : "—",
      typeof p.weighted_distance === "number" ? p.weighted_distance.toFixed(4) : "—",
      p.indicators ? num(p.indicators.population) : "—",
      p.indicators ? pct(p.indicators.fiscal_self_ratio, 1) : "—",
      p.indicators ? pct(p.indicators.welfare_ratio, 1) : "—",
    ])
  ));

  // 산출 방법 (심사 정확성 항목 — 가중치 출처를 감추지 않는다)
  const w = method.weights || {};
  const prov = method.weight_provenance || {};
  sec.appendChild(el("details", { class: "method" },
    el("summary", { text: "산출 방법 · 가중치 출처" }),
    table(["지표", "가중치", "출처"],
      Object.keys(w).map((k) => [
        k, w[k],
        prov[k] === "mois_public" ? "행안부 공개기준" : (prov[k] === "ours" ? "자체 설정" : (prov[k] || "—")),
      ])),
    method.note ? note(method.note) : null,
    method.missing_mois_indicators?.length
      ? note(`행안부 기준 중 확보하지 못한 지표: ${method.missing_mois_indicators.join(", ")} — `
        + "이 지표들은 유사도 계산에 반영되지 않았다.", "warn")
      : null,
    note(`후보 풀 ${num(method.candidate_pool)}곳 · 동일유형 내 비교=${method.partition_by_type ? "예" : "아니오"} `
      + `· 지표 최소 커버리지 ${method.min_indicator_coverage ?? "—"}`)
  ));

  appendSource(sec, res);
  sec.appendChild(envelopeFooter(env));
}

/**
 * 유사도 계산에 쓰이는 지표 중 비어 있는 것을 짚어 준다.
 * 실측: 전국 243곳 중 32곳(전남광주통합특별시 28 + 인천 개편 4구)이
 * budget_total·fiscal_self_ratio·welfare_ratio 가 NULL 이라 peers 가 0곳이 된다.
 * 이 경우 "유사한 지자체가 없다"가 아니라 "계산할 수 없다"가 사실이다.
 */
function missingIndicatorNote(ind) {
  const need = {
    budget_total: "예산총액", fiscal_self_ratio: "재정자립도",
    welfare_ratio: "복지예산 비율", population: "인구", area_km2: "면적",
  };
  const miss = ind
    ? Object.keys(need).filter((k) => ind[k] === null || ind[k] === undefined)
    : Object.keys(need);
  if (!miss.length) {
    return note("비교 가능한 유사 지자체를 찾지 못했다. 후보 풀이 좁거나 "
      + "지표 커버리지 기준을 넘긴 후보가 없다는 뜻이다.", "warn");
  }
  return note(
    `이 지자체는 ${miss.map((k) => need[k]).join("·")} 지표가 DB에 없어 유사도를 계산할 수 없다. `
    + "표의 '0곳'은 «유사한 지자체가 없다»는 뜻이 아니라 «비교 자체가 불가능하다»는 뜻이며, "
    + "따라서 아래 격차분석(추천 후보)도 비어 있다. "
    + "2026년 통합·개편으로 신설된 지자체는 결산 통계가 아직 산출되지 않아 이 상태가 된다.",
    "warn");
}

function indicatorRows(ind) {
  const labels = {
    population: "인구", area_km2: "면적(㎢)", fiscal_self_ratio: "재정자립도",
    welfare_ratio: "복지예산 비율", budget_total: "예산총액(원)",
  };
  return Object.entries(ind).map(([k, v]) => [
    labels[k] || k,
    k.endsWith("_ratio") ? pct(v, 2) : num(typeof v === "number" ? Math.round(v * 100) / 100 : v),
  ]);
}

/* ---------------- 격차분석 ---------------- */

async function renderGap(root, d, env, res) {
  const t = d.target || {};
  const recs = d.recommendations || [];

  const sec = section(`격차분석 — ${t.name || "?"}에 없는 정책`, asOfLine(`engine=${d._engine || "analytics.peers.recommend_ordinances"}`));
  root.appendChild(sec);

  // 일반구(level=3) 등 조례 제정권이 없는 지자체 — 격차 비교 자체가 성립하지 않는다 (P1-2)
  if (d.reason && !recs.length) {
    sec.appendChild(el("div", { class: "caution" },
      el("b", { text: "⚠ 비교 대상이 아님 — " }),
      document.createTextNode(d.reason)));
    appendSource(sec, res);
    sec.appendChild(envelopeFooter(env));
    return;
  }

  const withRepealed = recs.filter((r) => (r.repealed_peer_count || 0) > 0).length;
  sec.appendChild(el("div", { class: "stat-grid" },
    kv("비교 대상", `${num(d.peer_pool_size)}곳`),
    kv("우리 보유 정책", `${num(d.my_policy_count)}종`),
    kv("추천 후보", `${num(recs.length)}건`),
    kv("폐지 사례 포함", `${num(withRepealed)}건`, withRepealed ? "warn" : "")
  ));

  sec.appendChild(note(
    "여기 나오는 것은 '유사 지자체가 가진 정책 중 우리에게 없는 것'이다. "
    + "정책명 기준 집계이므로 표기가 다른 동일 정책이 섞일 수 있고, 제정 필요 여부의 판단 근거가 아니라 검토 시작점이다."));
  if (d.suppressed_exact_duplicate || d.flagged_as_variant) {
    sec.appendChild(note(
      `완전 중복 ${num(d.suppressed_exact_duplicate)}건은 제외했고, `
      + `표기만 다른 것으로 의심되는 ${num(d.flagged_as_variant)}건은 아래에 '표기변이 의심'으로 표시했다.`));
  }

  if (!recs.length) {
    // 비교 대상이 0곳이면 "없다"가 아니라 "못 구했다"이다 — 원인을 같이 적는다.
    if (!(d.peers || []).length) {
      sec.appendChild(note("비교할 유사 지자체를 구하지 못해 추천 후보를 낼 수 없다.", "warn"));
      sec.appendChild(missingIndicatorNote((d.target || {}).indicators));
    } else {
      sec.appendChild(note("추천 후보가 없습니다.", "warn"));
    }
    appendSource(sec, res);
    sec.appendChild(envelopeFooter(env));
    return;
  }

  // 카드 목록
  const list = el("div", { class: "gap-list" });
  for (const r of recs) list.appendChild(gapCard(r));
  sec.appendChild(list);

  // 차트
  const canvas = el("canvas", { height: "320" });
  const chartBox = el("div", { class: "chart-box" }, canvas);
  sec.insertBefore(chartBox, list);
  const labels = recs.map((r) => r.policy_key);
  try {
    await ensureChart();
    new window.Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels,
        datasets: [
          { label: "보유 유사지자체 수", data: recs.map((r) => r.peer_count || 0), backgroundColor: "#2c66a8" },
          { label: "그 중 폐지", data: recs.map((r) => r.repealed_peer_count || 0), backgroundColor: "#c0392b" },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false, indexAxis: "y",
        scales: { x: { beginAtZero: true, stacked: false } },
      },
    });
  } catch (e) {
    chartBox.remove();
    sec.insertBefore(cdnFailPanel("Chart.js(차트)", e), list);
  }

  appendSource(sec, res);
  sec.appendChild(envelopeFooter(env));
}

function kv(label, value, kind = "") {
  return el("div", { class: `stat-card ${kind}` },
    el("div", { class: "stat-label", text: label }),
    el("div", { class: "stat-value", text: value }));
}

function gapCard(r) {
  const repealed = r.repealed_peer_count || 0;
  const card = el("div", { class: `card ${repealed ? "card-caution" : ""}` });

  card.appendChild(el("div", { class: "card-head" },
    el("h3", { class: "card-title", text: r.policy_key }),
    el("div", { class: "chip-row" },
      // ★ 요구된 "N곳 보유 / M곳 폐지" 배지
      badge(`${r.peer_count}곳 보유`, "badge-info"),
      repealed ? badge(`${repealed}곳 폐지`, "badge-repealed") : badge("폐지 사례 없음", "badge-active"),
      typeof r.peer_share === "number" ? badge(`보유율 ${pct(r.peer_share, 0)}`, "badge-plain") : null,
      r.likely_variant_of_mine ? badge("표기변이 의심", "badge-warn") : null
    )
  ));

  if (repealed) {
    card.appendChild(el("div", { class: "caution" },
      el("b", { text: "⚠ 폐지 경고 — " }),
      document.createTextNode(r.caution || "유사 지자체 중 폐지 사례가 있다. 상위법 개정 등으로 대체되었을 수 있으니 제정 전 확인이 필요하다.")
    ));
    card.appendChild(table(["폐지한 지자체", "폐지일"],
      (r.repealed_peers || []).map((p) => [p.name, ymd(p.repealed_on)])));
  }

  if (r.likely_variant_of_mine && r.closest_own) {
    card.appendChild(note(
      `우리 조례 「${r.closest_own.policy_key}」 와 유사도 ${r.closest_own.similarity} — `
      + "이미 같은 내용을 다른 이름으로 갖고 있을 수 있다.", "warn"));
  } else if (r.closest_own) {
    card.appendChild(el("div", { class: "muted small", text:
      `우리 조례 중 가장 가까운 것: 「${r.closest_own.policy_key}」 (유사도 ${r.closest_own.similarity})` }));
  }

  const peers = r.peers || [];
  card.appendChild(el("details", {},
    el("summary", { text: `선례 조례 ${peers.length}건 보기 (현행만)` }),
    table(["지자체", "조례명", "제정일", "원문"],
      peers.map((p) => [p.name, p.ordinance_name, ymd(p.enacted_on), extLink(p.url, "law.go.kr")])),
    note("선례 목록에는 현행(active) 조례만 담는다. 폐지된 조례는 선례로 추천하지 않는다.")
  ));

  return card;
}
