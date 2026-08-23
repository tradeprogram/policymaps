// 앱 셸 — 네비게이션, 배너, 라우팅
import { el, qs } from "./util.js";
import { BASE, state, loadManifest, probeFullApi } from "./api.js";
import { mockBanner, staleBanner, fullEditionBanner, errorPanel, loading } from "./components.js";
import { route, setNotFound, setOnNavigate, start, go } from "./router.js";
import { initAgent } from "./agent.js";

import * as dashboard from "./views/dashboard.js";
import * as mapView from "./views/map.js";
import * as region from "./views/region.js";
import * as gap from "./views/gap.js";
import * as graph from "./views/graph.js";
import * as diffusion from "./views/diffusion.js";
import * as effectiveness from "./views/effectiveness.js";
import * as votes from "./views/votes.js";
import * as search from "./views/search.js";
import * as neural from "./views/neural.js";
import * as spatial from "./views/spatial.js";
import * as lifecycle from "./views/lifecycle.js";
import * as trust from "./views/trust.js";
import * as analytics from "./views/analytics.js";
import * as ordinance from "./views/ordinance.js";
import * as statute from "./views/statute.js";

const NAV = [
  ["/dashboard", "대시보드", dashboard],
  ["/map", "지도", mapView],
  ["/region/:sig", "지역 상세", region],
  ["/gap", "유사·격차분석", gap],
  ["/graph", "법령 위계", graph],
  ["/lifecycle", "정책 생애주기", lifecycle],
  ["/diffusion", "정책 확산", diffusion],
  ["/effectiveness", "조례 실효성", effectiveness],
  ["/neural", "신경망 유사도", neural],
  ["/spatial", "공간 분석", spatial],
  ["/analytics", "확산 위험모형", analytics],
  ["/votes", "국회 표결", votes],
  ["/search", "검색", search],
  ["/trust", "검증 공시", trust],
];

const DEFAULT_REGION = "11110";

async function boot() {
  const app = qs("#app");
  const nav = qs("#nav");
  const banners = qs("#banners");

  try {
    await loadManifest();
  } catch (e) {
    app.innerHTML = "";
    app.appendChild(errorPanel(e,
      `manifest.json 을 ${BASE} 에서 읽지 못했습니다. `
      + "① HTTP 서버로 열었는지(file:// 는 fetch 차단) ② js/config.js 의 DATA_BASE 가 맞는지 확인하세요."));
    return;
  }

  // 완전판(로컬 DB 직결) 탐지 — 실패해도 조용히 정적 shard 로 간다(배포본 보호).
  await probeFullApi();

  for (const b of [mockBanner(), staleBanner(), fullEditionBanner()]) if (b) banners.appendChild(b);

  // 네비게이션
  for (const [path, label] of NAV) {
    const href = "#" + (path === "/region/:sig" ? `/region/${DEFAULT_REGION}` : path);
    nav.appendChild(el("a", { href, class: "nav-item", "data-pattern": path, text: label }));
  }

  for (const [path, label, mod] of NAV) {
    route(path, async (params, query) => {
      // 컨테이너에 직접 그린다. Leaflet 등은 DOM 에 붙은 상태여야 크기 계산이 된다.
      const container = qs("#app");
      container.innerHTML = "";
      try {
        await mod.render(container, params, query);
      } catch (e) {
        container.innerHTML = "";
        container.appendChild(errorPanel(e, `${label} 화면 렌더 중 오류`));
        console.error(e);
      }
      window.scrollTo(0, 0);
    });
  }

  // 네비게이션에 올리지 않는 상세 라우트. 목록·검색 결과에서만 들어온다.
  route("/ordinance/:id", async (params, query) => {
    const container = qs("#app");
    container.innerHTML = "";
    try {
      await ordinance.render(container, params, query);
    } catch (e) {
      container.innerHTML = "";
      container.appendChild(errorPanel(e, "조례 상세 화면 렌더 중 오류"));
      console.error(e);
    }
    window.scrollTo(0, 0);
  });

  route("/statute/:id", async (params, query) => {
    const container = qs("#app");
    container.innerHTML = "";
    try {
      await statute.render(container, params, query);
    } catch (e) {
      container.innerHTML = "";
      container.appendChild(errorPanel(e, "법령 상세 화면 렌더 중 오류"));
      console.error(e);
    }
    window.scrollTo(0, 0);
  });

  setNotFound((p) => {
    qs("#app").innerHTML = "";
    qs("#app").appendChild(el("div", { class: "panel" },
      el("h2", { text: "없는 경로" }),
      el("p", { text: p }),
      el("button", { class: "btn", text: "대시보드로", onclick: () => go("/dashboard") })));
  });

  setOnNavigate((pattern) => {
    for (const a of nav.querySelectorAll(".nav-item")) {
      a.classList.toggle("active", a.dataset.pattern === pattern);
    }
  });

  start();
  initAgent();
}

boot();
