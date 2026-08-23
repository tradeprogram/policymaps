/**
 * 데이터 소스 설정 — 이 파일 한 곳만 바꾸면 가상데이터 <-> 실데이터가 전환된다.
 *
 * 이 사이트의 웹 루트는 viz/public/ 이다.
 * 서버를 F:/policy_maps 에서 띄우면(python -m http.server 8000) 두 경로가 모두 잡힌다.
 *   - 가상: /viz/public/data          -> "./data"
 *   - 실제: /system/data              -> "../../system/data"
 * viz/public/ 에서 직접 서버를 띄우면 상위 디렉터리는 접근 불가하므로 가상데이터만 쓸 수 있다.
 */

/**
 * ★ 전환 지점 ★  "../../system/data" = 실데이터(기본) / "./data" = 가상데이터
 *
 * 기본을 실데이터로 둔다. 가상데이터는 조례 302건·지역 27곳뿐인 스캐폴드용 표본이라
 * 기본값으로 두면 사이트가 목업을 보여준다(실측: manifest.counts.ordinances 302 vs 199,858).
 * 가상데이터로 보려면 ?src=mock 을 붙인다.
 *
 * 주의: 이 상대경로는 웹 루트가 저장소 루트(F:/policy_maps)일 때만 잡힌다.
 * viz/public/ 에서 직접 서버를 띄우면 상위 디렉터리에 접근할 수 없어 실데이터가 404 가 된다.
 * serve_full.py 와 Vercel 배포는 둘 다 저장소 루트를 서빙하므로 문제없다.
 */
export const DATA_BASE = "../../system/data";

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

/**
 * 라이브러리 소스. 각 항목은 **[로컬 내장, 원격 CDN] 순서**로 시도한다.
 *
 * viz/public/vendor/ 에 leaflet·chart.js·vis-network 를 내장해 두었으므로(1.1MB)
 * 인터넷이 없거나 사내망이 unpkg/jsdelivr 를 막는 발표장에서도 지도·차트·그래프가
 * 그대로 뜬다. 로컬이 없으면 원격으로 넘어가고, 둘 다 실패해야 비로소 화면마다
 * 대체 렌더(표/목록)로 떨어지고 배너가 뜬다.
 */
export const CDN = {
  leafletCss: ["./vendor/leaflet.css", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"],
  leafletJs: ["./vendor/leaflet.js", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"],
  chartJs: ["./vendor/chart.umd.js", "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js"],
  visNetworkJs: ["./vendor/vis-network.min.js", "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"],
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

/* ==================================================================== *
 *  전국(nationwide) shard 레이아웃
 * ==================================================================== */

/**
 * 사전계산 결과를 지역/항목 단위 shard 로 쪼갠 배치.
 * make_nationwide.py 가 아래 경로로 굽는 것을 전제하되, 로더(api.js)는
 * shard 가 없으면 기존 단일 파일(api/gap.json 등)로 폴백한다 — 하위호환.
 *
 *   api/index.json                   커버리지 색인 (아래 스키마)
 *   api/gap/{sig_cd}.json            지역별 격차분석 봉투
 *   api/peers/{sig_cd}.json          지역별 유사 지자체 봉투
 *   api/effectiveness/{sig_cd}.json  지역별 조례-예산 실효성 봉투
 *   api/diffusion/{key}.json         정책 템플릿별 확산 봉투
 *   api/votes/{key}.json             의안별 표결 봉투
 *   api/search/{key}.json            질의별 검색 봉투(선택)
 *
 * api/index.json 스키마 — 모든 필드가 선택이고, 로더는 아래 변형을 모두 받는다.
 * {
 *   "as_of_date": "2026-08-22",
 *   "regions": [
 *     {"sig_cd":"47190","name":"구미시","sido":"경상북도","level":2,
 *      "has":{"gap":true,"peers":true,"effectiveness":true}}
 *   ],
 *   "gap":   ["47190", ...]  |  [{"sig_cd":"47190","file":"api/gap/47190.json"}],
 *   "peers": [...],  "effectiveness": [...],
 *   "diffusion": [{"key":"맨발걷기","label":"맨발걷기 (level2·122/130 제정본)",
 *                  "file":"api/diffusion/맨발걷기.json"}],
 *   "votes":     [{"key":"2215741","label":"소상공인 보호 및 지원에 관한 법률 …",
 *                  "file":"api/votes/2215741.json"}]
 * }
 * 배열 대신 {키: 객체} 맵, 객체 대신 문자열, regions 대신 items 도 허용한다.
 */
export const API_SHARDS = {
  index: "api/index.json",
  gap: "api/gap",
  peers: "api/peers",
  effectiveness: "api/effectiveness",
  diffusion: "api/diffusion",
  votes: "api/votes",
  search: "api/search",
};

/**
 * make_extend_fixtures.py 가 굽는 확장 카탈로그.
 * api/index.json(make_nationwide.py 산출)에는 표결 15건·검색 5건만 들어 있는데,
 * 확장 생성기가 표결 149건·검색 40건을 더 구웠다. 두 색인을 합쳐야 화면에서 전부 고를 수 있다.
 * 없으면 조용히 무시된다(가상데이터·구 번들 하위호환).
 */
export const EXTRA_CATALOGS = {
  votes: { path: "api/votes_index.json", lists: ["bills"] },
  search: { path: "api/search_index.json", lists: ["queries"] },
};

/** shard 가 없을 때 화면에 안내할 생성기 경로 */
export const NATIONWIDE_GENERATOR = "system/make_nationwide.py";

/**
 * 지역 선택기에 올릴 level.
 * 1=광역 16곳, 2=기초 227곳 → 243곳. level 3(일반구 41곳)은 조례 제정권이 없어 제외한다.
 */
export const PICKER_LEVELS = [1, 2];

/**
 * 시도 그룹 이름 폴백. sig_cd 앞 2자리 -> 이름.
 * ★ 1차 출처는 regions/index.json 의 level=1 항목이다(이 DB에는 '전남광주통합특별시(12)'
 *   처럼 표준 코드표에 없는 개편 지자체가 있어 하드코딩을 신뢰하면 안 된다).
 *   여기 값은 level=1 항목이 없는 접두어에만 쓰인다.
 */
export const SIDO_FALLBACK = {
  "11": "서울특별시", "12": "전남광주통합특별시", "26": "부산광역시", "27": "대구광역시",
  "28": "인천광역시", "29": "광주광역시", "30": "대전광역시", "31": "울산광역시",
  "36": "세종특별자치시", "41": "경기도", "42": "강원도", "43": "충청북도",
  "44": "충청남도", "45": "전라북도", "46": "전라남도", "47": "경상북도",
  "48": "경상남도", "50": "제주특별자치도", "51": "강원특별자치도", "52": "전북특별자치도",
};

/* ==================================================================== *
 *  완전판(로컬 DB 직결) API — viz/serve_full.py
 * ==================================================================== */

/**
 * 배포본(Vercel 정적)에는 조문 '본문' 236만 건(약 490MB)이 들어가지 않는다.
 * 로컬에서 `python viz/serve_full.py` 를 띄우면 같은 화면이 DB 를 직접 읽어
 * 본문·임의 질의 전문검색·임의 조례 서브그래프까지 100% 를 보여준다.
 *
 * 판정 규칙(api.js probeFullApi)
 *   - `?full=0` 이면 무조건 끈다(정적 shard 만 쓴다).
 *   - 그 외에는 candidates 를 순서대로 GET {base}/status 해 보고
 *     200 + data.full_edition=true 인 첫 번째를 쓴다.
 *   - 전부 실패하면 조용히 정적 shard 로 폴백한다. 배포본은 이 경로로만 동작한다.
 *   - `?full=1` 로 명시했는데 실패하면 배너로 알린다(조용히 넘어가면 오해를 준다).
 */
export const FULL_API = {
  /** 같은 오리진(serve_full.py 가 정적 파일까지 서빙하는 기본 구성) */
  base: "/api/db",
  /**
   * 정적 서버를 따로 띄운 경우를 위한 후보. serve_full.py 는 CORS 를 허용한다.
   * 배포 환경에서는 두 후보 모두 404 라 폴백된다.
   */
  candidates: ["/api/db", "http://127.0.0.1:8743/api/db"],
  /** 탐지 타임아웃(ms). 배포본에서 초기 렌더를 늦추지 않을 만큼 짧게. */
  probeTimeoutMs: 2500,
  /** 상세 요청 타임아웃(ms). 첫 검색은 RAG 인덱스 예열 때문에 오래 걸릴 수 있다. */
  requestTimeoutMs: 300000,
};
