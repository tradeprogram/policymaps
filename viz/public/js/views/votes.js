// 8. 국회 표결 — 전국 shard api/votes/{의안번호}.json (목록은 api/index.json 의 votes)
//    폴백: api/votes.json 단일 1건. 정당별 찬반 스택 차트.
import { el, num, pct, ymd } from "../util.js";
import { loadCatalog, loadCatalogItem } from "../api.js";
import { section, table, note, loading, asOfLine, badge, statCard,
         envelopeFooter, cdnFailPanel } from "../components.js";
import { catalogSelector, notPrecomputedPanel, sourceLine } from "../nationwide.js";
import { ensureChart } from "../vendor.js";

const VOTE_COLORS = { "찬성": "#2c66a8", "반대": "#c0392b", "기권": "#f39c12", "기타": "#95a5a6" };
const VOTE_KEYS = ["찬성", "반대", "기권", "기타"];

export async function render(root) {
  root.appendChild(loading("의안 목록을 불러오는 중…"));
  const entries = await loadCatalog("votes");
  root.innerHTML = "";

  const picker = catalogSelector({
    entries, current: entries.length ? entries[0].key : null,
    label: "의안", onChange: (k) => draw(k),
  });
  if (picker) root.appendChild(picker);
  else if (!entries.length) {
    root.appendChild(note(
      "의안 목록(api/index.json 의 votes)이 없어 번들에 있는 1건만 표시한다. "
      + "make_nationwide.py 로 의안별 shard 를 구우면 여기에 선택기가 붙는다."));
  } else {
    root.appendChild(note(
      `사전계산된 의안이 「${entries[0].label}」 1건뿐이라 선택기를 띄우지 않는다. `
      + "make_nationwide.py 로 의안을 더 구우면 선택기가 붙는다."));
  }

  const body = el("div", {});
  root.appendChild(body);

  let token = 0;
  async function draw(key) {
    const my = ++token;
    body.innerHTML = "";
    body.appendChild(loading("표결 데이터를 불러오는 중…"));
    const entry = entries.find((e) => e.key === String(key)) || null;
    const res = await loadCatalogItem("votes", entry);
    if (my !== token) return;
    body.innerHTML = "";
    if (!res.data) {
      body.appendChild(notPrecomputedPanel({
        kind: "votes", sig: key, name: entry ? entry.label : null,
        subject: "의안",
        tried: res.tried || (res.path ? [res.path] : []),
        fixtureRegions: entries.map((e) => e.key).filter((k) => k !== String(key)),
        onPick: (k) => draw(k),
      }));
      return;
    }
    await renderBody(body, res.data, res.env, res);
  }

  await draw(entries.length ? entries[0].key : null);
}

/** 의안 1건의 표결 결과 렌더 */
async function renderBody(root, d, env, res) {
  const b = d.bill || {};
  const rep = d.tally_reported || {};
  const fromVotes = d.tally_from_votes || {};

  root.appendChild(section(b.name || "의안",
    asOfLine(),
    el("div", { class: "chip-row" },
      badge(`의안번호 ${b.bill_no || "—"}`, "badge-info"),
      badge(`제${b.age || "?"}대`, "badge-plain"),
      b.committee ? badge(b.committee, "badge-plain") : null,
      b.proc_result ? badge(b.proc_result, b.proc_result.includes("가결") ? "badge-active" : "badge-warn") : null
    ),
    table(["항목", "값"], [
      ["의안 ID", b.bill_id || "—"],
      ["발의일", ymd(b.propose_dt)],
      ["처리일", ymd(b.proc_dt)],
      ["처리 결과", b.proc_result || "—"],
    ])
  ));

  // 집계 대조 — 공표 집계와 개별 표결 합산이 다를 수 있다
  const sumFromVotes = VOTE_KEYS.reduce((a, k) => a + (fromVotes[k] || 0), 0);
  const mismatch = rep.vote_tcnt !== undefined && fromVotes["합계"] !== undefined
    && rep.vote_tcnt !== fromVotes["합계"];
  root.appendChild(section("집계",
    el("div", { class: "stat-grid" },
      statCard("재적", num(rep.member_tcnt), null),
      statCard("총투표", num(rep.vote_tcnt ?? fromVotes["합계"]), null),
      statCard("찬성", num(rep.yes_tcnt ?? fromVotes["찬성"]), null),
      statCard("반대", num(rep.no_tcnt ?? fromVotes["반대"]), null),
      statCard("기권/무효", num(rep.blank_tcnt), null),
      statCard("찬성률", pct(d.yes_ratio, 1), "찬성 / 총투표")
    ),
    table(["구분", "공표 집계(tally_reported)", "개별 표결 합산(tally_from_votes)"], [
      ["찬성", num(rep.yes_tcnt), num(fromVotes["찬성"])],
      ["반대", num(rep.no_tcnt), num(fromVotes["반대"])],
      ["기권", num(rep.blank_tcnt), num(fromVotes["기권"])],
      ["기타", "—", num(fromVotes["기타"])],
      ["합계", num(rep.vote_tcnt), num(fromVotes["합계"] ?? sumFromVotes)],
    ]),
    mismatch
      ? note("공표 집계와 개별 표결 합산이 일치하지 않는다. 원자료의 기권/무효 분류 차이일 수 있으므로 "
        + "어느 쪽을 쓰는지 명시해야 한다.", "warn")
      : note("공표 집계와 개별 표결 합산이 일치한다.")
  ));

  // 정당별 스택 차트
  const parties = d.party_breakdown || [];
  const psec = section("정당별 찬반");
  root.appendChild(psec);
  if (!parties.length) {
    psec.appendChild(note("party_breakdown 이 비어 있습니다.", "warn"));
  } else {
    const sorted = [...parties].sort((a, b2) => (b2["합계"] || 0) - (a["합계"] || 0));
    const canvas = el("canvas", { height: "320" });
    psec.appendChild(el("div", { class: "chart-box" }, canvas));
    const tbl = table(["정당", ...VOTE_KEYS, "합계", "찬성률"],
      sorted.map((p) => [p.party, ...VOTE_KEYS.map((k) => num(p[k])), num(p["합계"]), pct(p["찬성률"], 1)]));
    try {
      await ensureChart();
      new window.Chart(canvas.getContext("2d"), {
        type: "bar",
        data: {
          labels: sorted.map((p) => p.party),
          datasets: VOTE_KEYS.map((k) => ({
            label: k, data: sorted.map((p) => p[k] || 0), backgroundColor: VOTE_COLORS[k],
          })),
        },
        options: {
          responsive: true, maintainAspectRatio: false, indexAxis: "y",
          scales: { x: { stacked: true, beginAtZero: true }, y: { stacked: true } },
        },
      });
      psec.appendChild(el("details", {}, el("summary", { text: "표로 보기" }), tbl));
    } catch (e) {
      canvas.parentElement.remove();
      psec.appendChild(cdnFailPanel("Chart.js(차트)", e, tbl));
    }
    psec.appendChild(note("정당은 표결 시점 소속(party_at_vote) 기준이다. 현재 소속과 다를 수 있다."));
  }

  // 발의자
  const props = d.proposers || [];
  if (props.length) {
    const roleLabel = { RST: "대표발의", PUBLIC: "공동발의", SUP: "찬성" };
    root.appendChild(section(`발의자 ${props.length}명`,
      table(["역할", "의원", "현 소속", "지역구"],
        props.map((p) => [roleLabel[p.role] || p.role || "—", p.name, p.current_party || "—", p.district || "—"]))));
  }

  const src = sourceLine(res);
  if (src) root.appendChild(src);
  root.appendChild(envelopeFooter(env));
}
