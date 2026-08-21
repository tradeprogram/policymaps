/**
 * CDN 라이브러리 로더.
 * 실패하면 예외를 던지고, 각 화면은 대체 렌더(표/목록)로 떨어진다.
 * 오프라인/사내망 환경 대비.
 */
import { CDN } from "./config.js";

const loaded = new Map();
export const cdnStatus = { failed: new Set(), ok: new Set() };

/** URL 에 ?nocdn=1 을 붙이면 CDN 로드를 강제로 실패시킨다. 오프라인 대체 화면 점검용. */
const FORCE_FAIL = new URLSearchParams(location.search).get("nocdn") === "1";

function loadScript(url) {
  if (loaded.has(url)) return loaded.get(url);
  const p = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = url;
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error(`CDN 로드 실패: ${url}`));
    document.head.appendChild(s);
    // onerror 가 안 오는 프록시 환경 대비 타임아웃
    setTimeout(() => reject(new Error(`CDN 응답 없음(15초 초과): ${url}`)), 15000);
  });
  loaded.set(url, p);
  return p;
}

function loadCss(url) {
  if (loaded.has(url)) return loaded.get(url);
  const p = new Promise((resolve) => {
    const l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = url;
    l.onload = () => resolve();
    l.onerror = () => resolve(); // CSS 실패는 치명적이지 않음
    document.head.appendChild(l);
  });
  loaded.set(url, p);
  return p;
}

async function guard(name, fn, check) {
  try {
    if (FORCE_FAIL) throw new Error(`?nocdn=1 로 강제 실패시킴 (${name})`);
    await fn();
    if (check && !check()) throw new Error(`${name} 전역 객체 없음`);
    cdnStatus.ok.add(name);
    return true;
  } catch (e) {
    cdnStatus.failed.add(name);
    throw e;
  }
}

export function ensureLeaflet() {
  return guard("Leaflet(지도)", async () => {
    await loadCss(CDN.leafletCss);
    await loadScript(CDN.leafletJs);
  }, () => typeof window.L !== "undefined");
}

export function ensureChart() {
  return guard("Chart.js(차트)", () => loadScript(CDN.chartJs), () => typeof window.Chart !== "undefined");
}

export function ensureVisNetwork() {
  return guard("vis-network(그래프)", () => loadScript(CDN.visNetworkJs), () => typeof window.vis !== "undefined");
}
