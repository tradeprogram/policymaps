// 4. 유사 지자체 + 격차분석 (킬러 화면)
//    peers.json / gap.json 소비. "N곳 보유 / M곳 폐지" 경고 배지 필수.
import { el, num, pct, ymd, extLink } from "../util.js";
import { loadFixture, DataMissingError } from "../api.js";
import { section, table, note, loading, asOfLine, errorPanel, fixtureMissingPanel,
         badge, envelopeFooter, cdnFailPanel } from "../components.js";
import { ensureChart } from "../vendor.js";

export async function render(root) {
  root.appendChild(loading("유사 지자체·격차분석 결과를 불러오는 중…"));

  let peersEnv = null, gapEnv = null, peersErr = null, gapErr = null;
  try { peersEnv = await loadFixture("peers"); } catch (e) { peersErr = e; }
  try { gapEnv = await loadFixture("gap"); } catch (e) { gapErr = e; }
  root.innerHTML = "";

  if (peersErr && gapErr) {
    root.appendChild(fixtureMissingPanel("gap", gapErr));
    return;
  }

  // fixture 는 data 가 sig_cd 맵({"47190":{...}, ...})이고 envelope.regions 에 커버 목록이 있다.
  // 구(舊) 단일 구조(data.target 직접)도 하위호환한다.
  const src = gapEnv || peersEnv;
  const dd = src.data || {};
  const single = !!(dd.target || dd.recommendations || dd.peers);
  const regions = Array.isArray(src.regions) && src.regions.length
    ? src.regions.slice()
    : (single ? null : Object.keys(dd));

  const host = el("div", {});
  root.appendChild(host);

  const draw = (sig) => {
    host.innerHTML = "";
    if (regions && regions.length) host.appendChild(regionPicker(regions, sig, peersEnv, gapEnv, draw));
    const body = el("div", {});
    host.appendChild(body);
    const pSub = single ? (peersEnv && peersEnv.data) : (peersEnv && (peersEnv.data || {})[sig]);
    const gSub = single ? (gapEnv && gapEnv.data) : (gapEnv && (gapEnv.data || {})[sig]);
    if (peersEnv) renderPeers(body, pSub || {}, peersEnv);
    else body.appendChild(fixtureMissingPanel("peers", peersErr));
    if (gapEnv) renderGap(body, gSub || {}, gapEnv);
    else body.appendChild(fixtureMissingPanel("gap", gapErr));
  };

  // 완료판정 시나리오(구미시 47190)를 우선 노출, 없으면 첫 지자체.
  const initial = single ? null : (regions.includes("47190") ? "47190" : regions[0]);
  draw(initial);
}

function regionPicker(regions, current, peersEnv, gapEnv, onChange) {
  const nameOf = (sig) => {
    const g = (gapEnv && (gapEnv.data || {})[sig]) || {};
    const p = (peersEnv && (peersEnv.data || {})[sig]) || {};
    return (g.target && g.target.name) || (p.target && p.target.name) || sig;
  };
  const sel = el("select", { class: "sel" });
  for (const sig of regions) {
    const o = el("option", { value: sig, text: `${nameOf(sig)} (${sig})` });
    if (sig === current) o.selected = true;
    sel.appendChild(o);
  }
  sel.addEventListener("change", () => onChange(sel.value));
  return el("div", { class: "toolbar" },
    el("label", { text: "기준 지자체 " }), sel,
    el("span", { class: "muted small", text: `사전계산된 ${regions.length}곳 중 선택 · 다른 지자체는 make_gap_fixtures.py 로 추가` }));
}

/* ---------------- 유사 지자체 ---------------- */

function renderPeers(root, d, env) {
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
    sec.appendChild(envelopeFooter(env));
    return;
  }

  if (t.indicators) {
    sec.appendChild(el("h3", { text: "기준 지자체 지표" }));
    sec.appendChild(table(["지표", "값"], indicatorRows(t.indicators)));
  }

  const peers = d.peers || [];
  sec.appendChild(el("h3", { text: `유사 지자체 ${peers.length}곳` }));
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

  sec.appendChild(envelopeFooter(env));
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

async function renderGap(root, d, env) {
  const t = d.target || {};
  const recs = d.recommendations || [];

  const sec = section(`격차분석 — ${t.name || "?"}에 없는 정책`, asOfLine(`engine=${d._engine || "analytics.peers.recommend_ordinances"}`));
  root.appendChild(sec);

  // 일반구(level=3) 등 조례 제정권이 없는 지자체 — 격차 비교 자체가 성립하지 않는다 (P1-2)
  if (d.reason && !recs.length) {
    sec.appendChild(el("div", { class: "caution" },
      el("b", { text: "⚠ 비교 대상이 아님 — " }),
      document.createTextNode(d.reason)));
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
    sec.appendChild(note("추천 후보가 없습니다.", "warn"));
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
