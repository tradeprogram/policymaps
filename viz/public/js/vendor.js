/**
 * 라이브러리 로더.
 *
 * CDN 의 각 항목은 [로컬 내장, 원격 CDN] 후보 배열이다(config.js 참조).
 * 앞에서부터 시도해 처음 성공한 것을 쓴다. 로컬 vendor/ 가 있으면 인터넷 없이도
 * 지도·차트·그래프가 뜬다. 모든 후보가 실패해야 예외를 던지고, 그때 각 화면이
 * 대체 렌더(표/목록)로 떨어진다.
 */
import { CDN } from "./config.js";

const loaded = new Map();
export const cdnStatus = { failed: new Set(), ok: new Set() };

/** URL 에 ?nocdn=1 을 붙이면 CDN 로드를 강제로 실패시킨다. 오프라인 대체 화면 점검용. */
const FORCE_FAIL = new URLSearchParams(location.search).get("nocdn") === "1";

/** 후보 하나를 <script> 로 싣는다. */
function loadOneScript(url) {
  if (loaded.has(url)) return loaded.get(url);
  const p = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = url;
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error(`로드 실패: ${url}`));
    document.head.appendChild(s);
    // onerror 가 안 오는 프록시 환경 대비 타임아웃.
    // 로컬 내장은 즉시 뜨므로 원격 후보에만 긴 시간을 준다.
    const ms = /^https?:/i.test(url) ? 15000 : 5000;
    setTimeout(() => reject(new Error(`응답 없음(${ms / 1000}초 초과): ${url}`)), ms);
  });
  loaded.set(url, p);
  return p;
}

/** 후보 배열을 앞에서부터 시도한다. 문자열 하나만 줘도 동작한다(구버전 호환). */
async function loadScript(src) {
  const cands = Array.isArray(src) ? src : [src];
  let last;
  for (const url of cands) {
    try {
      await loadOneScript(url);
      return url;
    } catch (e) {
      last = e;
    }
  }
  throw last || new Error("로드할 후보가 없습니다");
}

/**
 * CSS 후보를 앞에서부터 시도한다.
 * CSS 실패는 치명적이지 않으므로(지도는 뜨고 모양만 깨진다) 최종적으로도 reject 하지 않는다.
 */
function loadCss(src) {
  const cands = Array.isArray(src) ? src : [src];
  const key = cands.join("|");
  if (loaded.has(key)) return loaded.get(key);
  const p = (async () => {
    for (const url of cands) {
      const ok = await new Promise((resolve) => {
        const l = document.createElement("link");
        l.rel = "stylesheet";
        l.href = url;
        l.onload = () => resolve(true);
        l.onerror = () => resolve(false);
        document.head.appendChild(l);
      });
      if (ok) return url;
    }
    return null;
  })();
  loaded.set(key, p);
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
