// 공용 유틸 — DOM, 포맷, 배지

/** HTML 이스케이프. 모든 데이터 문자열은 이걸 거쳐서 넣는다. */
export function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return n;
}

export function qs(sel, root = document) { return root.querySelector(sel); }

const NF = new Intl.NumberFormat("ko-KR");
export function num(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return NF.format(v);
}

/** 원 단위 금액을 조/억/만원으로 축약 */
export function won(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  const a = Math.abs(n);
  if (a >= 1e12) return NF.format(Math.round((n / 1e12) * 100) / 100) + "조원";
  if (a >= 1e8) return NF.format(Math.round((n / 1e8) * 10) / 10) + "억원";
  if (a >= 1e4) return NF.format(Math.round(n / 1e4)) + "만원";
  return NF.format(n) + "원";
}

export function pct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return (Number(v) * 100).toFixed(digits) + "%";
}

/** "20210709" 또는 ISO 문자열을 YYYY-MM-DD 로 */
export function ymd(v) {
  if (!v) return "—";
  const s = String(v);
  if (/^\d{8}$/.test(s)) return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
  if (s.length >= 10) return s.slice(0, 10);
  return s;
}

export function dtime(v) {
  if (!v) return "—";
  const s = String(v);
  return s.length >= 16 ? s.slice(0, 16).replace("T", " ") : s;
}

/** 동시 요청 수를 제한하는 map */
export async function mapLimit(items, limit, fn, onProgress) {
  const out = new Array(items.length);
  let idx = 0;
  let done = 0;
  const workers = new Array(Math.min(limit, items.length)).fill(0).map(async () => {
    for (;;) {
      const i = idx++;
      if (i >= items.length) return;
      try { out[i] = await fn(items[i], i); }
      catch (e) { out[i] = null; }
      done++;
      if (onProgress) onProgress(done, items.length);
    }
  });
  await Promise.all(workers);
  return out;
}

/** 값 배열에 대한 5분위 경계 (코로플레스 계급) */
export function quantileBreaks(values, k = 5) {
  const v = values.filter((x) => typeof x === "number" && !Number.isNaN(x)).sort((a, b) => a - b);
  if (!v.length) return [];
  const breaks = [];
  for (let i = 1; i < k; i++) breaks.push(v[Math.floor((v.length * i) / k)]);
  return breaks;
}

export function classOf(value, breaks) {
  if (typeof value !== "number" || Number.isNaN(value)) return -1;
  let c = 0;
  for (const b of breaks) { if (value >= b) c++; }
  return c;
}

export const CHOROPLETH_COLORS = ["#e8f0fb", "#bcd4f0", "#8ab4e2", "#5590cd", "#2c66a8"];

/** 안전한 새 탭 링크 */
export function extLink(url, label) {
  if (!url) return el("span", { class: "muted", text: label || "—" });
  return el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: label || "원문" });
}

export function debounce(fn, ms = 250) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
