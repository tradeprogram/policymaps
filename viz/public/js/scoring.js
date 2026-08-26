import { num, pct } from "./util.js";

const MAX = {
  peers: 40,
  repeal: 20,
  diffusion: 20,
  budget: 20,
};

export function scorePolicyCandidate(candidate, context = {}) {
  const peers = peerPart(candidate, context);
  const repeal = repealPart(candidate);
  const diffusion = diffusionPart(candidate, context.diffusion);
  const budget = budgetPart(candidate, context.effectiveness);
  const parts = [peers, repeal, diffusion, budget];
  const score = Math.max(0, Math.min(100, Math.round(parts.reduce((sum, p) => sum + p.score, 0))));
  return {
    score,
    grade: gradeOf(score),
    parts,
    disclaimer: "의사결정 보조 점수입니다. 조례 도입 필요성의 확정 판단이 아닙니다.",
  };
}

function peerPart(candidate, context) {
  const peerCount = Number(candidate?.peer_count || 0);
  const pool = Number(context.peerPoolSize || 0);
  const share = typeof candidate?.peer_share === "number"
    ? candidate.peer_share
    : (pool > 0 ? peerCount / pool : 0);
  const score = Math.round(Math.sqrt(Math.max(0, Math.min(1, share))) * MAX.peers);
  return {
    key: "peers",
    label: "유사 지자체",
    score,
    max: MAX.peers,
    detail: `${num(peerCount)}곳 보유${pool ? ` · 비교 ${num(pool)}곳 중 ${pct(share, 0)}` : ""}`,
  };
}

function repealPart(candidate) {
  const peerCount = Math.max(1, Number(candidate?.peer_count || 0));
  const repealed = Number(candidate?.repealed_peer_count || 0);
  const ratio = Math.max(0, Math.min(1, repealed / peerCount));
  const score = Math.round(MAX.repeal * Math.max(0, 1 - ratio * 2));
  return {
    key: "repeal",
    label: "폐지 위험",
    score,
    max: MAX.repeal,
    detail: repealed ? `폐지 사례 ${num(repealed)}곳 · 선례 추천 제외` : "폐지 사례 0곳",
    risk: repealed > 0,
  };
}

function diffusionPart(candidate, diffusion) {
  const d = diffusion?.data || diffusion;
  const template = d?.template;
  const rate = typeof d?.final_adoption_rate === "number" ? d.final_adoption_rate : null;
  if (!template || rate === null) {
    return unavailable("diffusion", "확산성", MAX.diffusion, "후보별 확산 fixture 없음");
  }
  if (!matchesCandidate(candidate, template)) {
    return unavailable("diffusion", "확산성", MAX.diffusion, `현재 확산 fixture는 '${template}' 기준`);
  }
  const score = Math.round(Math.max(0, Math.min(1, rate / 0.7)) * MAX.diffusion);
  return {
    key: "diffusion",
    label: "확산성",
    score,
    max: MAX.diffusion,
    detail: `전국 채택률 ${pct(rate, 1)}${d.adopters && d.universe ? ` · ${num(d.adopters)}/${num(d.universe)}곳` : ""}`,
  };
}

function budgetPart(candidate, effectiveness) {
  const d = effectiveness?.data || effectiveness;
  if (!d) return unavailable("budget", "예산 근거", MAX.budget, "예산 연결 fixture 없음");
  const matched = (d.by_ordinance || []).filter((o) => matchesText(candidate, o?.name));
  if (matched.length) {
    const lines = matched.reduce((sum, o) => sum + Number(o.lines || 0), 0);
    const verified = matched.filter((o) => o.verification_status === "verified" || o.verified === 1).length;
    const score = Math.min(MAX.budget, 12 + Math.min(6, lines) + Math.min(2, verified * 2));
    return {
      key: "budget",
      label: "예산 근거",
      score,
      max: MAX.budget,
      detail: `후보명 유사 예산 연결 ${num(lines)}건 · verified ${num(verified)}건`,
    };
  }
  if (Number(d.link_count || 0) > 0) {
    return {
      key: "budget",
      label: "예산 근거",
      score: 8,
      max: MAX.budget,
      detail: `지역 전체 예산 연결 ${num(d.link_count)}건 · 후보별 직접 연결 미확인`,
      limited: true,
    };
  }
  return unavailable("budget", "예산 근거", MAX.budget, "후보별 예산 연결 미확인");
}

function unavailable(key, label, max, detail) {
  return { key, label, score: 0, max, detail, unavailable: true };
}

function gradeOf(score) {
  if (score >= 75) return "우선 검토";
  if (score >= 60) return "검토 가능";
  if (score >= 40) return "추가 확인";
  return "보류";
}

function matchesCandidate(candidate, keyword) {
  return matchesText(candidate, keyword)
    || (candidate?.peers || []).some((p) => normalized(p?.ordinance_name).includes(normalized(keyword)));
}

function matchesText(candidate, text) {
  const needle = normalized(candidate?.policy_key);
  const hay = normalized(text);
  return Boolean(needle && hay && (hay.includes(needle) || needle.includes(hay)));
}

function normalized(value) {
  return String(value || "").replace(/[\s·ㆍ\-_/()「」,]/g, "").toLowerCase();
}
