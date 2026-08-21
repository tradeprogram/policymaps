/**
 * 데이터 소스 설정 — 이 파일 한 곳만 바꾸면 가상데이터 <-> 실데이터가 전환된다.
 *
 * 이 사이트의 웹 루트는 viz/public/ 이다.
 * 서버를 F:/policy_maps 에서 띄우면(python -m http.server 8000) 두 경로가 모두 잡힌다.
 *   - 가상: /viz/public/data          -> "./data"
 *   - 실제: /system/data              -> "../../system/data"
 * viz/public/ 에서 직접 서버를 띄우면 상위 디렉터리는 접근 불가하므로 가상데이터만 쓸 수 있다.
 */

/** ★ 전환 지점 ★  "./data" = 가상데이터 / "../../system/data" = 실데이터 */
export const DATA_BASE = "./data";

/** 프리셋. ?src=real 또는 ?src=mock 쿼리스트링으로 임시 전환도 된다. */
export const DATA_SOURCES = {
  mock: "./data",
  real: "../../system/data",
};

/** 지도 경계 파일. export 번들에 포함되지 않는 참조 데이터라 DATA_BASE와 분리한다. */
export const GEO_URL = "./geo/municipalities.geojson";
export const ADM_DONG_GEO_URL = "./geo/adm_dong.geojson";

export const SATELLITE_TILE = {
  url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  attribution: "Imagery: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
};

/** CDN. 실패하면 화면마다 대체 렌더(표/목록)로 떨어지고 배너가 뜬다. */
export const CDN = {
  leafletCss: "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  leafletJs: "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
  chartJs: "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js",
  visNetworkJs: "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js",
};

/** 안전장치 — 실데이터 graph/nodes.json 은 134MiB 다. 무심코 받으면 브라우저가 죽는다. */
export const LIMITS = {
  /** 이 수를 넘는 그래프는 확인 버튼을 눌러야 로드한다 */
  graphNodeWarn: 20000,
  /** 지도/대시보드에서 동시에 받는 지역 shard 수 */
  fetchConcurrency: 8,
  /** 대시보드 카테고리 집계에 쓰는 지역 표본 수 (전수 아님을 화면에 표기) */
  categorySample: 40,
  /** 그래프 화면에서 한 번에 그리는 최대 노드 수 */
  graphRenderNodes: 300,
};

/**
 * 조례-예산 링크 신뢰도 등급.
 * 근거: 표본 584건 수작업 검증에서 전체 정밀도 64.9%, confidence>=0.8 구간 93.2%.
 *       (검증 시점 링크 93,964건 기준이며 현재 모집단 183,145건에 그대로 적용되지는 않는다.)
 */
export const CONFIDENCE_GRADES = [
  { min: 0.8, key: "high", label: "추정(높음)", note: "confidence≥0.8" },
  { min: 0.6, key: "mid", label: "추정(중간)", note: "0.6≤confidence<0.8" },
  { min: -1, key: "low", label: "추정(낮음)", note: "confidence<0.6" },
];

/**
 * 카테고리 코드 -> 이름.
 * 출처: system/tools_seed_categories.py 의 통제어휘 C01~C14 + 구 임시분류 C-BIRTH/C-PET.
 * graph/nodes.json 의 Category 노드가 로드되면 런타임에 덮어쓴다.
 */
export const CATEGORY_FALLBACK = {
  C01: "행정·자치·의회",
  C02: "재정·세무·회계",
  C03: "복지·돌봄",
  C04: "인구·출산·양육",
  C05: "청년·교육",
  C06: "보건·의료",
  C07: "환경·기후",
  C08: "안전·재난",
  C09: "도시·건축·주택",
  C10: "교통",
  C11: "경제·산업·일자리",
  C12: "농림·수산",
  C13: "문화·체육·관광",
  C14: "동물·반려",
  "C-BIRTH": "출산장려ㆍ양육지원",
  "C-PET": "반려동물ㆍ동물보호",
};
