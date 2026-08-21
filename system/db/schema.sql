-- policy_maps 관계형 저장소 DDL — 단일 진실원천(SSOT)
-- 대상: SQLite 3.x (표준 sqlite3 모듈). 순수 표준라이브러리로 적용 가능.
-- 설계 근거:
--   명세[hierarchy] : 규범 위계(tier)·bitemporal 시간축·승계(SUCCEEDED_BY)·효력우선 파생
--   명세[realtime]  : watermarks 복합키·change_feed·콘텐츠 해시 2단·툼스톤(소프트삭제)
--   명세[law_api]   : MST/lawId 조인키, ordin 조 단위 구조, 위임 4경로, admrul 일련번호
--   명세[assembly]  : BILL_ID/MONA_CD/POLY_NM 조인키, 대표/공동발의, 의원별 표결
--   명세[geo_budget]: sig_cd 5자리=법정동 앞5자리, laf_cd, org/sborg ELIS 코드, QWGJK 25필드
--   korea100 data-contract.md : verification 3단계(source-linked/article-verified/needs-review),
--                               LegalBasisKind 열거형, asOfDate(기록시간) 규율 승계
--
-- 규약:
--   * 모든 시각 문자열은 ISO8601(Asia/Seoul) 또는 YYYYMMDD(원천 그대로) — 컬럼 코멘트에 명시.
--   * 폐지/통합/소멸 노드는 하드삭제 금지 → status/valid_to 툼스톤으로만 표현.
--   * FK는 선언하되 PRAGMA foreign_keys 는 런타임에서 db.py가 ON 설정.
--   * "(추정)" 표기 컬럼은 실키/실호출 확정 전 잠정 필드.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ===========================================================================
-- 0. 통제어휘 / 열거형 참조 테이블
-- ===========================================================================

-- 규범 종류 통제어휘(korea100 LegalBasisKind 12종 + 헌법/조약/표준/교육규칙 확장).
-- national_tier: 0 헌법 / 1 법률·조약 / 2 대통령령·헌법기관규칙 / 3 총리령·부령 / 4 행정규칙·고시·표준
-- local_tier   : 'L1' 조례·의회규칙 / 'L2' 규칙·교육규칙 (자치법규 축; national 과 분리)
CREATE TABLE IF NOT EXISTS instrument_kind (
  kind            TEXT PRIMARY KEY,     -- '헌법','법률','대통령령','총리령','부령','행정안전부령',
                                        -- '대법원규칙','감사원규칙','행정규칙','고시·지침','조약',
                                        -- '조례','규칙','교육규칙','의회규칙','표준','기타'
  source_type     TEXT NOT NULL,        -- 'statute'|'admin-rule'|'treaty'|'constitution'|'ordinance'|'standard'
  national_tier   INTEGER,              -- 0~4, 자치법규/미분류는 NULL
  local_tier      TEXT,                 -- 'L1'|'L2'|NULL
  tier_disputed   INTEGER DEFAULT 0,    -- 1이면 서열 학설대립(조약·헌법기관규칙) → 단정 금지
  api_target      TEXT,                 -- 'law'|'eflaw'|'admrul'|'ordin'|NULL
  api_knd         TEXT,                 -- ordin knd(30001 등) 또는 admrul knd(1~6)
  note            TEXT
);

-- 정책분야 통제어휘 C01~C12 (명세: categories). ordinfd 분야명과는 매핑으로 연결.
CREATE TABLE IF NOT EXISTS categories (
  code            TEXT PRIMARY KEY,     -- 'C01' ~ 'C12'
  name            TEXT NOT NULL,
  level           INTEGER DEFAULT 1,    -- 대분류1/중분류2
  parent_code     TEXT REFERENCES categories(code),
  definition      TEXT,                 -- 정의문(분류 근거)
  keywords        TEXT                  -- JSON 배열: 룰기반 1차 분류용 앵커 키워드
);

-- ===========================================================================
-- 1. 행정구역 (regions) — 스파인 = 법정동코드 10자리 (StanReginCd region_cd)
--    폐지·통합·승계 보존. sig_cd(5) = region_cd[:5] = V-World sig_cd.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS regions (
  region_id       TEXT PRIMARY KEY,     -- 정규 ID = 법정동코드 앞자리(광역2/기초5/읍면동8/리10)
  region_cd       TEXT,                 -- StanReginCd region_cd 10자리(해당 계층 대표코드)
  sig_cd          TEXT,                 -- 5자리 시군구코드(=region_cd[:5]); 광역은 앞2자리+'000'
  sido_cd         TEXT,                 -- 2자리
  sgg_cd          TEXT,                 -- 3자리
  name            TEXT NOT NULL,        -- 최하위 지명(locallow_nm)
  full_name       TEXT,                 -- 전체 주소명(locatadd_nm / V-World full_nm)
  level           INTEGER NOT NULL,     -- 1 광역 / 2 기초(시·군·자치구) / 3 행정시·일반구(자치입법권 없음)
  parent_region   TEXT REFERENCES regions(region_id),  -- CONTAINS/PART_OF 행정 포함위계
  has_legislation INTEGER DEFAULT 1,    -- 0=자치입법권 없음(행정시·일반구) → 조례 직접 귀속 금지
  -- 외부 코드체계 크로스워크(명세[geo_budget] §4)
  vworld_sig_cd   TEXT,                 -- V-World LT_C_ADSIGG_INFO sig_cd
  law_org         TEXT,                 -- 법령API org(광역 ELIS 기관코드)
  law_sborg       TEXT,                 -- 법령API sborg(기초 ELIS 기관코드)
  lofin_laf_cd    TEXT,                 -- 지방재정365 laf_cd(자치단체코드)
  locatjumin_cd   TEXT,                 -- 주민등록 행정동코드(근사 매핑용)
  -- 지오메트리(대용량 GeoJSON은 별도 region_geometry에)
  geometry_ref    TEXT,                 -- region_geometry.region_id 참조(=self) 또는 파일 shard 키
  centroid_lon    REAL, centroid_lat REAL,
  population      INTEGER,              -- (확인 필요) 인구
  -- bitemporal 유효시간
  valid_from      TEXT,                 -- 법정동코드 생성일(adpt_de)
  valid_to        TEXT,                 -- 폐지/통합일(툼스톤); NULL=현행
  status          TEXT DEFAULT 'active',-- 'active'|'abolished'|'merged'|'renamed'
  -- 기록시간
  as_of_date      TEXT,                 -- 우리 수집 기준일
  source          TEXT DEFAULT 'StanReginCd',
  updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_regions_sig       ON regions(sig_cd);
CREATE INDEX IF NOT EXISTS ix_regions_level      ON regions(level);
CREATE INDEX IF NOT EXISTS ix_regions_parent     ON regions(parent_region);
CREATE INDEX IF NOT EXISTS ix_regions_status     ON regions(status);
CREATE INDEX IF NOT EXISTS ix_regions_laf        ON regions(lofin_laf_cd);
CREATE INDEX IF NOT EXISTS ix_regions_law_org    ON regions(law_org, law_sborg);

-- 대용량 폴리곤 분리(선택). MultiPolygon GeoJSON 문자열.
CREATE TABLE IF NOT EXISTS region_geometry (
  region_id       TEXT PRIMARY KEY REFERENCES regions(region_id),
  crs             TEXT DEFAULT 'EPSG:4326',
  geojson         TEXT,                 -- GeoJSON geometry(MultiPolygon) 원문
  bbox            TEXT,                 -- JSON [minx,miny,maxx,maxy]
  ring_signature  TEXT,                 -- 순수파이썬 Queen 인접탐지용 경계 세그먼트 서명(JSON)
  fetched_at      TEXT
);

-- 지역 인접(Queen contiguity, E8). 무방향이나 양방향 2행으로 저장.
CREATE TABLE IF NOT EXISTS region_adjacency (
  region_id       TEXT NOT NULL REFERENCES regions(region_id),
  neighbor_id     TEXT NOT NULL REFERENCES regions(region_id),
  contiguity_type TEXT DEFAULT 'queen', -- 'queen'|'rook'|'knn_fallback'|'precomputed'
  same_province   INTEGER DEFAULT 0,    -- 동일 광역 여부
  shared_len      REAL,                 -- 공유 경계 길이(확인 필요; 폴백은 NULL)
  method          TEXT,                 -- 'shapely'|'ring-share'|'loaded'
  computed_at     TEXT,
  PRIMARY KEY (region_id, neighbor_id)
);
CREATE INDEX IF NOT EXISTS ix_adj_neighbor ON region_adjacency(neighbor_id);

-- 지역 승계(SUCCEEDED_BY, E10). 구→신. 2026-07-01 전남광주통합 등.
CREATE TABLE IF NOT EXISTS region_succession (
  old_region_id   TEXT NOT NULL REFERENCES regions(region_id),
  new_region_id   TEXT NOT NULL REFERENCES regions(region_id),
  succession_type TEXT,                 -- '통합'|'분리'|'개칭'|'승계'
  effective_date  TEXT,                 -- 시행일 YYYY-MM-DD
  legal_basis     TEXT,                 -- 설치·폐지법 명칭
  status_note     TEXT,                 -- 자치법규 정비 진행상태 요약
  PRIMARY KEY (old_region_id, new_region_id)
);

-- ===========================================================================
-- 2. 국가법령 규범 (legal_instrument) — Statute 슈퍼노드 (N2~N7 물리통합)
--    kind/tier 로 헌법/법률/시행령/시행규칙/행정규칙/조약/표준 구분.
--    조인키: mst(=법령일련번호/자치법규일련번호), law_id(6자리) 또는 admrul serial.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS legal_instrument (
  instrument_id   TEXT PRIMARY KEY,     -- 정규 ID: 'statute:{mst}' | 'admrul:{serial}' | 'treaty:{id}' | 'const'
  kind            TEXT NOT NULL REFERENCES instrument_kind(kind),
  source_type     TEXT NOT NULL,        -- statute|admin-rule|treaty|constitution|standard
  national_tier   INTEGER,              -- instrument_kind 기본값 복제(질의 편의); 학설대립은 tier_disputed
  tier_disputed   INTEGER DEFAULT 0,
  -- 원천 식별자(명세[law_api])
  mst             TEXT,                 -- 법령일련번호(=lawService MST). statute/eflaw 본문 조회키
  law_id          TEXT,                 -- 법령ID 6자리(lsStmd/lnkLsOrdJo 조인키)
  admrul_serial   TEXT,                 -- 행정규칙일련번호(admrul 본문 ID)
  admrul_id       TEXT,                 -- 행정규칙ID(별개)
  treaty_id       TEXT, treaty_number TEXT,
  name            TEXT NOT NULL,        -- 법령명/행정규칙명/조약명(한글)
  short_name      TEXT,                 -- 약칭
  competent_authority TEXT,             -- 소관부처명
  competent_authority_cd TEXT,
  admrul_knd      TEXT,                 -- 행정규칙 종류(훈령1/예규2/고시3/공고4/지침5/기타6)
  law_supplement_flag INTEGER DEFAULT 0,-- 법령보충적 행정규칙(대외 구속력) 여부
  generality      TEXT,                 -- 'general'|'special' (lex specialis 판정용)
  -- 표준(Standard) 부속 속성
  standard_kind   TEXT,                 -- 'KS'|'기술기준'|NULL
  standard_binding TEXT,                -- '임의'|'강제'|NULL
  -- bitemporal 유효시간(YYYYMMDD 원천)
  enacted_on      TEXT,                 -- 공포일자/발령일자
  promulgation_no TEXT,                 -- 공포번호/발령번호
  effective_on    TEXT,                 -- 시행일자
  repealed_on     TEXT,                 -- 폐지/실효일(툼스톤)
  rr_cls_cd       TEXT,                 -- 제개정구분코드
  current_history TEXT,                 -- '현행'|'연혁'|'시행예정'(eflaw)
  official_url    TEXT,                 -- law.go.kr 직링크(원문 미러링 금지)
  -- [표준정합 §9] FRBR/ELI/Akoma Ntoso NC (policymap.standards 가 채움)
  work_id         TEXT,                 -- FRBR Work 키('lw:{law_id}' 등)
  work_uri        TEXT,                 -- AKN NC Work IRI '/akn/kr/act/...'
  expression_uri  TEXT,                 -- AKN NC Expression IRI '.../kor@{시행일}'
  canonical_url   TEXT,                 -- 공개 영구 URL(OC 키 미포함)
  valid_from      TEXT,                 -- ISO 'YYYY-MM-DD' (eli:first_date_entry_in_force)
  valid_to        TEXT,                 -- ISO 'YYYY-MM-DD' (eli:date_no_longer_in_force), NULL=열림
  lifecycle       TEXT,                 -- 'in_force'|'pending'|'repealed'|'superseded'|'undetermined'
  version_no      INTEGER,              -- Work 내 Expression 순번(1부터)
  -- 기록시간 + 검증(korea100 승계)
  as_of_date      TEXT,
  content_hash    TEXT,                 -- 조문 정규화 sha256(콘텐츠 CDC 2단)
  status          TEXT DEFAULT 'active',-- 'active'|'repealed'|'superseded'
  verification_status TEXT DEFAULT 'needs-review', -- source-linked|article-verified|needs-review
  updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_li_mst    ON legal_instrument(mst);
CREATE INDEX IF NOT EXISTS ix_li_lawid  ON legal_instrument(law_id);
CREATE INDEX IF NOT EXISTS ix_li_serial ON legal_instrument(admrul_serial);
CREATE INDEX IF NOT EXISTS ix_li_kind   ON legal_instrument(kind, national_tier);
CREATE INDEX IF NOT EXISTS ix_li_name   ON legal_instrument(name);
CREATE INDEX IF NOT EXISTS ix_li_status ON legal_instrument(status, current_history);

-- 국가법령 조문(articles, N17). 항/호/목은 정규화 텍스트로 접어 저장.
CREATE TABLE IF NOT EXISTS articles (
  article_id      TEXT PRIMARY KEY,     -- 'statute:{mst}::제{no}조[의{branch}]'
  instrument_id   TEXT NOT NULL REFERENCES legal_instrument(instrument_id),
  article_no      TEXT NOT NULL,        -- 조문번호 문자열 "1"
  article_branch  TEXT,                 -- 조문가지번호(제N조의M의 M)
  title           TEXT,                 -- 조제목
  body            TEXT,                 -- 조문내용(제목+본문 전체; 항/호/목 포함 정규화)
  effective_on    TEXT,                 -- 조문시행일자(조문별 미래시행 대응)
  article_key     TEXT,                 -- 원천 조문키
  embedding_ref   TEXT,                 -- embeddings.item_id 참조(선택)
  content_hash    TEXT,
  updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_articles_instr ON articles(instrument_id, article_no);

-- ===========================================================================
-- 3. 자치법규 (ordinances) — 조례/규칙/교육규칙 (N8~N10)
--    본문 조회키 = 자치법규일련번호(mst). 자치법규ID(ord_id)와 구분.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS ordinances (
  ordinance_id    TEXT PRIMARY KEY,     -- 'ordin:{mst}' (mst=자치법규일련번호)
  mst             TEXT NOT NULL,        -- 자치법규일련번호(본문 조회키)
  ord_id          TEXT,                 -- 자치법규ID(별개)
  region_id       TEXT REFERENCES regions(region_id),  -- HAS_ORDINANCE(E1) 귀속 지자체
  org_name        TEXT,                 -- 지자체기관명("경기도 가평군")
  name            TEXT NOT NULL,        -- 자치법규명
  ord_kind        TEXT NOT NULL,        -- '조례'|'규칙'|'교육규칙'|'의회규칙'|'훈령'|'예규'|'고시'|'기타'
  local_tier      TEXT,                 -- 'L1'|'L2'
  enacted_by      TEXT,                 -- '지방의회'|'단체장'|'교육감'
  delegation_type TEXT,                 -- 'law-delegated'|'mandatory'|'autonomous'|NULL(§5)
  department      TEXT,                 -- 담당부서명
  phone           TEXT,
  category_field  TEXT,                 -- 자치법규분야명(지자체 내부 분류)
  -- bitemporal 유효시간
  enacted_on      TEXT,                 -- 공포일자 YYYYMMDD
  promulgation_no TEXT,                 -- 공포번호
  effective_on    TEXT,                 -- 시행일자
  repealed_on     TEXT,
  rr_cls_cd       TEXT,                 -- 제개정구분(300201제정/300202일부/300203전부/300204폐지…)
  article_count   INTEGER,
  official_url    TEXT,
  -- 승계 과도기(명세[hierarchy] §6): 통합·폐지 지자체 조례 상태
  succession_status TEXT,               -- '원지자체유지'|'이관'|'재정비'|'폐지'|NULL
  -- [표준정합 §9] FRBR/ELI/Akoma Ntoso NC (policymap.standards 가 채움)
  work_id         TEXT,                 -- FRBR Work 키('ow:{16hex}') — ord_id 는 Work 가 아님(§9 주석)
  work_uri        TEXT,                 -- AKN NC Work IRI '/akn/kr/ordinance/{author}/{ord_id}'
  expression_uri  TEXT,                 -- AKN NC Expression IRI '.../kor@{시행일}'
  canonical_url   TEXT,                 -- 공개 영구 URL(OC 키 미포함) ordinInfoP.do?ordinSeq={mst}
  valid_from      TEXT,                 -- ISO 'YYYY-MM-DD' (eli:first_date_entry_in_force)
  valid_to        TEXT,                 -- ISO 'YYYY-MM-DD' (eli:date_no_longer_in_force), NULL=열림
  lifecycle       TEXT,                 -- 'in_force'|'pending'|'repealed'|'superseded'|'undetermined'
  version_no      INTEGER,              -- Work 내 Expression 순번(1부터)
  -- 기록시간 + 검증 + 임베딩
  as_of_date      TEXT,
  content_hash    TEXT,                 -- 조내용 정규화 sha256(CDC 2단; 별표 개정 포착)
  embedding_ref   TEXT,                 -- embeddings.item_id
  status          TEXT DEFAULT 'active',-- 'active'|'repealed'|'superseded'
  verification_status TEXT DEFAULT 'needs-review',
  updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_ord_region ON ordinances(region_id, ord_kind);
CREATE INDEX IF NOT EXISTS ix_ord_mst    ON ordinances(mst);
CREATE INDEX IF NOT EXISTS ix_ord_ordid  ON ordinances(ord_id);
CREATE INDEX IF NOT EXISTS ix_ord_name   ON ordinances(name);
CREATE INDEX IF NOT EXISTS ix_ord_status ON ordinances(status);
CREATE INDEX IF NOT EXISTS ix_ord_rrcls  ON ordinances(rr_cls_cd);

-- 자치법규 조문(ordinance_articles). 원천 조 단위(조문번호/조제목/조내용).
CREATE TABLE IF NOT EXISTS ordinance_articles (
  oa_id           TEXT PRIMARY KEY,     -- 'ordin:{mst}::{조문번호6자리}'
  ordinance_id    TEXT NOT NULL REFERENCES ordinances(ordinance_id),
  article_no      TEXT NOT NULL,        -- 조문번호(6자리 zero-pad 원천)
  title           TEXT,                 -- 조제목
  body            TEXT,                 -- 조내용(상위법 「」 인용 포함)
  embedding_ref   TEXT,
  content_hash    TEXT,
  updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_oa_ord ON ordinance_articles(ordinance_id, article_no);

-- 자치법규 별표(선택; 파일 링크만 보존, 원문 미러링 금지)
CREATE TABLE IF NOT EXISTS ordinance_appendix (
  appendix_id     TEXT PRIMARY KEY,
  ordinance_id    TEXT NOT NULL REFERENCES ordinances(ordinance_id),
  appendix_no     TEXT, appendix_branch TEXT,
  title           TEXT, appendix_kind TEXT,
  file_name       TEXT, file_url TEXT,  -- flDownload.do URL
  body            TEXT
);

-- ===========================================================================
-- 4. 규범 간 관계 (엣지) — 위임/체계/인용/개정/승계/효력
-- ===========================================================================

-- 위임·체계 관계(E2 DELEGATED_FROM + E3 SUBORDINATE_TO 통합). 하위→상위.
-- 4경로(lsDelegated/lsStmd/lnkLsOrdJo/lnkLsOrd) 합집합, source_path로 provenance 보존.
CREATE TABLE IF NOT EXISTS delegations (
  delegation_id   TEXT PRIMARY KEY,     -- 해시 결정적 ID(중복 방지)
  -- 하위 규범(조례 또는 하위 법령)
  child_kind      TEXT NOT NULL,        -- 'ordinance'|'instrument'
  child_id        TEXT NOT NULL,        -- ordinances.ordinance_id | legal_instrument.instrument_id
  child_article   TEXT,                 -- 하위 조문(자치법규조번호/조항호목) — 조문단위 정밀도
  -- 상위 규범
  parent_kind     TEXT NOT NULL,        -- 'instrument'|'ordinance'|'constitution'
  parent_id       TEXT NOT NULL,        -- 상위 legal_instrument.instrument_id 등
  parent_article  TEXT,                 -- 상위 위임 조문(법령조번호/조정보.조문번호)
  relation        TEXT NOT NULL,        -- 'DELEGATED_FROM'|'SUBORDINATE_TO'
  delegation_type TEXT,                 -- 'law-delegated'|'mandatory'|'autonomous'|
                                        -- 'decree-to-law'|'rule-to-decree'|'metro-to-basic'
  trigger_text    TEXT,                 -- 위임 유발 문구("대통령령으로 정하는")
  citation_text   TEXT,                 -- 낫표 인용 원문
  -- provenance(4경로 어디서 왔는지 — 경로별 건수 불일치 실측 대응)
  source_path     TEXT NOT NULL,        -- 'lsDelegated'|'lsStmd'|'lnkLsOrdJo'|'lnkLsOrd'|'lnkOrg'|'citation'
  tier_gap        INTEGER,              -- 상하위 tier 차(효력판정 근사)
  inferred        INTEGER DEFAULT 0,    -- 1=추론(광역→기초 등 직접 API 부재)
  verification_status TEXT DEFAULT 'needs-review',
  computed_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_deleg_child  ON delegations(child_kind, child_id);
CREATE INDEX IF NOT EXISTS ix_deleg_parent ON delegations(parent_kind, parent_id);
CREATE INDEX IF NOT EXISTS ix_deleg_rel    ON delegations(relation, source_path);

-- 규범 간 인용(E4 CITES) + 표준 편입(E5) + 효력우선(E7 PREVAILS_OVER) 통합 관계.
CREATE TABLE IF NOT EXISTS instrument_relations (
  rel_id          TEXT PRIMARY KEY,
  src_kind        TEXT NOT NULL,        -- 'ordinance'|'instrument'
  src_id          TEXT NOT NULL,
  dst_kind        TEXT NOT NULL,
  dst_id          TEXT NOT NULL,
  relation        TEXT NOT NULL,        -- 'CITES'|'INCORPORATES_STANDARD'|'PREVAILS_OVER'|
                                        -- 'AMENDED_BY'|'REPEALED_BY'|'REPLACED_BY'
  -- CITES/표준
  citation_text   TEXT,
  citation_type   TEXT,                 -- 'delegation'|'reference'|'standard'
  src_article     TEXT,
  -- 효력우선(PREVAILS_OVER)
  priority_rule   TEXT,                 -- 'lex_superior'|'lex_posterior'|'lex_specialis'
  priority_basis  TEXT,                 -- 판정근거(판례·부칙)
  scope           TEXT,
  -- 개정 시간축(AMENDED_BY/REPEALED_BY/REPLACED_BY, E6)
  amend_type      TEXT,                 -- rrClsCd 매핑(일부/전부/폐지/타법)
  effective_on    TEXT,
  inferred        INTEGER DEFAULT 0,
  computed_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_ir_src ON instrument_relations(src_kind, src_id, relation);
CREATE INDEX IF NOT EXISTS ix_ir_dst ON instrument_relations(dst_kind, dst_id, relation);

-- ===========================================================================
-- 5. 국회 의안·의원·정당·발의·표결 (명세[assembly])
-- ===========================================================================
CREATE TABLE IF NOT EXISTS parties (
  party_id        TEXT PRIMARY KEY,     -- POLY_CD 있으면 그것, 없으면 정규화 정당명
  name            TEXT NOT NULL,        -- POLY_NM
  poly_cd         TEXT
);

CREATE TABLE IF NOT EXISTS legislators (
  legislator_id   TEXT PRIMARY KEY,     -- MONA_CD(국회의원코드) — 유일 조인키
  mona_cd         TEXT NOT NULL,        -- =legislator_id (원천명 보존)
  name            TEXT NOT NULL,        -- HG_NM
  name_hanja      TEXT,                 -- HJ_NM
  name_eng        TEXT,
  current_party   TEXT REFERENCES parties(party_id),  -- 현재 소속(명부; 표결당시와 다를 수 있음)
  district        TEXT,                 -- ORIG_NM 선거구
  elect_type      TEXT,                 -- ELECT_GBN_NM 지역구/비례
  reelection      TEXT,                 -- REELE_GBN_NM 재선구분
  units           TEXT,                 -- UNITS 당선횟수
  sex             TEXT,                 -- SEX_GBN_NM
  committees      TEXT,                 -- CMITS(JSON)
  age_first       INTEGER,              -- 최초 대수(역대 명부 조인 시)
  email           TEXT, homepage TEXT,
  updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_leg_party ON legislators(current_party);
CREATE INDEX IF NOT EXISTS ix_leg_name  ON legislators(name);

CREATE TABLE IF NOT EXISTS bills (
  bill_id         TEXT PRIMARY KEY,     -- BILL_ID(PRC_…) — 의안 유일키, 법령↔의안 조인 후보
  bill_no         TEXT,                 -- BILL_NO(대수 내 단조증가; 커서)
  age             INTEGER NOT NULL,     -- AGE(대수) — 파티션
  name            TEXT NOT NULL,        -- BILL_NAME
  committee       TEXT, committee_id TEXT,
  propose_dt      TEXT,                 -- PROPOSE_DT 제안일
  proc_dt         TEXT,                 -- PROC_DT 의결일
  proc_result     TEXT,                 -- PROC_RESULT 본회의심의결과
  proc_result_cd  TEXT,                 -- 표결현황의 PROC_RESULT_CD(가결/부결)
  bill_kind_cd    TEXT,                 -- BILL_KIND_CD 의안종류
  -- 의안별 표결 집계(ncocpgfiaoituanbr)
  member_tcnt     INTEGER,              -- 재적
  vote_tcnt       INTEGER,              -- 총투표
  yes_tcnt        INTEGER,              -- 찬성
  no_tcnt         INTEGER,              -- 반대
  blank_tcnt      INTEGER,              -- 기권
  detail_link     TEXT, link_url TEXT,
  -- Bill→Law 연계(E17 ENACTS; 공식 조인키 부재 → 휴리스틱)
  enacted_instrument_id TEXT REFERENCES legal_instrument(instrument_id),
  enact_match_method TEXT,              -- '법령명+공포일' 등
  enact_verified  INTEGER DEFAULT 0,
  content_hash    TEXT,                 -- 상태변동 감지용(PROC_RESULT/PROC_DT)
  updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_bills_age  ON bills(age, bill_no);
CREATE INDEX IF NOT EXISTS ix_bills_proc ON bills(proc_result_cd);
CREATE INDEX IF NOT EXISTS ix_bills_enact ON bills(enacted_instrument_id);

-- 발의자(PROPOSED_BY, E14). 대표/공동 구분.
CREATE TABLE IF NOT EXISTS bill_proposers (
  bill_id         TEXT NOT NULL REFERENCES bills(bill_id),
  legislator_id   TEXT NOT NULL REFERENCES legislators(legislator_id),
  role            TEXT NOT NULL,        -- 'RST'(대표발의)|'PUBL'(공동발의)
  PRIMARY KEY (bill_id, legislator_id, role)
);
CREATE INDEX IF NOT EXISTS ix_bp_leg ON bill_proposers(legislator_id);

-- 의원별 표결(VOTED, E15). 표결당시 정당(POLY_NM) 스냅샷 포함.
CREATE TABLE IF NOT EXISTS votes (
  bill_id         TEXT NOT NULL REFERENCES bills(bill_id),
  legislator_id   TEXT NOT NULL REFERENCES legislators(legislator_id),
  result_vote_mod TEXT,                 -- RESULT_VOTE_MOD 찬성/반대/기권 등
  vote_date       TEXT,                 -- VOTE_DATE
  party_at_vote   TEXT,                 -- POLY_NM(표결 당시 정당 — 역사적 정확)
  party_cd_at_vote TEXT,                -- POLY_CD
  session_cd      TEXT, currents_cd TEXT,
  PRIMARY KEY (bill_id, legislator_id)
);
CREATE INDEX IF NOT EXISTS ix_votes_leg   ON votes(legislator_id);
CREATE INDEX IF NOT EXISTS ix_votes_party ON votes(party_at_vote, result_vote_mod);

-- ===========================================================================
-- 6. 예산 (budget_lines) — 지방재정365 QWGJK 세부사업별 세출현황
--    자연키 = (laf_cd, fyr, 세부사업코드). ID = ehojo-{laf_cd}-{fyr}-{seq}(e호조).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS budget_lines (
  budget_id       TEXT PRIMARY KEY,     -- 'ehojo-{laf_cd}-{fyr}-{seq}'
  fyr             INTEGER NOT NULL,     -- 회계연도
  laf_cd          TEXT NOT NULL,        -- 자치단체코드
  region_id       TEXT REFERENCES regions(region_id),  -- laf_cd→region 크로스워크
  dbiz_cd         TEXT,                 -- 세부사업코드(자연키 구성)
  dbiz_nm         TEXT,                 -- 세부사업명
  dept_cd         TEXT,                 -- 부서코드
  acct_name       TEXT,                 -- 회계구분명
  field           TEXT,                 -- 분야(fld)
  sector          TEXT,                 -- 부문(part)
  budget_now      INTEGER,              -- 예산현액
  gov_fund        INTEGER,              -- 국비
  sido_fund       INTEGER,              -- 시도비
  sigungu_fund    INTEGER,              -- 시군구비
  alloc_amt       INTEGER,              -- 편성액
  exe_amt         INTEGER,              -- 지출액
  exe_ymd         TEXT,                 -- 집행일자(exe_ymd; 커서 후보)
  as_of_date      TEXT,
  source          TEXT DEFAULT 'e호조',
  updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_budget_key    ON budget_lines(laf_cd, fyr, dbiz_cd);
CREATE INDEX IF NOT EXISTS ix_budget_region ON budget_lines(region_id, fyr);
CREATE INDEX IF NOT EXISTS ix_budget_dbiznm ON budget_lines(dbiz_nm);

-- 조례↔예산 연결(FUNDED_BY, E13). 매칭 신뢰도·방법·검증.
CREATE TABLE IF NOT EXISTS ordinance_budget_link (
  ordinance_id    TEXT NOT NULL REFERENCES ordinances(ordinance_id),
  budget_id       TEXT NOT NULL REFERENCES budget_lines(budget_id),
  match_method    TEXT,                 -- 'name-similarity'|'category-gate'|'manual'
  confidence      REAL,                 -- 0~1
  verified        INTEGER DEFAULT 0,
  category_gate   TEXT,                 -- 매칭 시 통과한 카테고리
  source_fyr      INTEGER,
  computed_at     TEXT,
  PRIMARY KEY (ordinance_id, budget_id)
);
CREATE INDEX IF NOT EXISTS ix_obl_budget ON ordinance_budget_link(budget_id);

-- ===========================================================================
-- 7. 분류 / 유사도 / 임베딩
-- ===========================================================================

-- 조례→카테고리(IN_CATEGORY, E12)
CREATE TABLE IF NOT EXISTS ordinance_category (
  ordinance_id    TEXT NOT NULL REFERENCES ordinances(ordinance_id),
  category_code   TEXT NOT NULL REFERENCES categories(code),
  confidence      REAL,
  method          TEXT,                 -- 'rule'|'llm'|'embedding-knn'
  computed_at     TEXT,
  PRIMARY KEY (ordinance_id, category_code)
);
CREATE INDEX IF NOT EXISTS ix_ordcat_cat ON ordinance_category(category_code);

-- 임베딩 저장(조례/조문 공용). 기본 폴백=문자 n-gram TF 코사인.
CREATE TABLE IF NOT EXISTS embeddings (
  item_id         TEXT PRIMARY KEY,     -- 'ordin:{mst}' | 'oa:{...}' | 'article:{...}'
  item_type       TEXT NOT NULL,        -- 'ordinance'|'ordinance_article'|'article'
  model_name      TEXT NOT NULL,        -- 'char-ngram-tf'(폴백) | 'sbert:...'
  dim             INTEGER,
  vector          TEXT,                 -- JSON 배열 또는 sparse {token:weight}(폴백)
  norm            REAL,                 -- 사전계산 L2 노름(코사인 가속)
  computed_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_emb_type ON embeddings(item_type, model_name);

-- 조례 유사도(SIMILAR_TO, E11). kNN 상위만 저장.
CREATE TABLE IF NOT EXISTS similarity_edges (
  src_id          TEXT NOT NULL,        -- ordinances.ordinance_id
  dst_id          TEXT NOT NULL,
  cosine_sim      REAL NOT NULL,
  rank            INTEGER,              -- src 기준 근접 순위
  model_name      TEXT,
  computed_at     TEXT,
  PRIMARY KEY (src_id, dst_id)
);
CREATE INDEX IF NOT EXISTS ix_sim_dst ON similarity_edges(dst_id);
CREATE INDEX IF NOT EXISTS ix_sim_src_rank ON similarity_edges(src_id, rank);

-- ===========================================================================
-- 8. 검증 / 워터마크 / 변경로그 (명세[realtime])
-- ===========================================================================

-- 범용 검증 레코드(엔터티 공통). korea100 verification 3단계 승계.
CREATE TABLE IF NOT EXISTS verification (
  entity_type     TEXT NOT NULL,        -- 'ordinance'|'instrument'|'delegation'|'bill'|'budget'
  entity_id       TEXT NOT NULL,
  status          TEXT NOT NULL,        -- 'source-linked'|'article-verified'|'needs-review'
  verified_at     TEXT,
  method          TEXT,
  scope           TEXT,
  notes           TEXT,                 -- JSON
  -- 조문 검증 카운터(korea100 articleVerification 승계)
  citation_entries INTEGER,
  explicit_citation_entries INTEGER,
  article_references INTEGER,
  verified_references INTEGER,
  missing_references INTEGER,
  uncheckable_references INTEGER,
  PRIMARY KEY (entity_type, entity_id)
);

-- 워터마크(증분 CDC). PK=(source, scope) 복합키.
CREATE TABLE IF NOT EXISTS watermarks (
  source          TEXT NOT NULL,        -- 'law'|'ordin'|'na_bill'|'na_vote'|'budget'|'boundary'
  scope           TEXT NOT NULL,        -- 'global'|'sig:26110'|'age:22'|'laf:26110|fyr:2026'
  cursor          TEXT,                 -- 소스별 하이워터 토큰(JSON/구분자 문자열)
  last_run        TEXT,                 -- 이번 실행 시작(ISO8601 Asia/Seoul)
  last_success    TEXT,                 -- 파티션 완전 영속화 시각
  last_hash       TEXT,                 -- 'sha256:...' 파티션/본문 스냅숏 해시
  status          TEXT DEFAULT 'ok',    -- 'ok'|'partial'|'error'|'stale'|'manual-hold'
  rows_seen       INTEGER DEFAULT 0,
  changed         INTEGER DEFAULT 0,    -- 이번 사이클 upsert 건수
  retry_count     INTEGER DEFAULT 0,
  run_id          TEXT,
  note            TEXT,
  PRIMARY KEY (source, scope)
);
CREATE INDEX IF NOT EXISTS ix_wm_source ON watermarks(source, status);

-- 변경로그(change feed). 사람/에이전트/운영 3채널 단일원천.
CREATE TABLE IF NOT EXISTS change_log (
  change_id       TEXT PRIMARY KEY,     -- uuid
  ts              TEXT NOT NULL,        -- ISO8601(Asia/Seoul)
  source          TEXT, scope TEXT,
  entity_type     TEXT,                 -- 'ordinance'|'instrument'|'bill'|'vote'|'budget'|'boundary'
  entity_id       TEXT,
  entity_name     TEXT,
  event           TEXT,                 -- 'created'|'amended'|'repealed'|'vote_recorded'|
                                        -- 'budget_changed'|'boundary_reorg'
  before          TEXT,                 -- 버전 토큰(watermarks cursor 동형)
  after           TEXT,
  fields_changed  TEXT,                 -- JSON 배열
  region_code     TEXT,                 -- 지역 필터용(sig_cd)
  run_id          TEXT,
  official_url    TEXT                  -- law.go.kr 원문 직링크(미러링 금지)
);
CREATE INDEX IF NOT EXISTS ix_cl_ts     ON change_log(ts);
CREATE INDEX IF NOT EXISTS ix_cl_entity ON change_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_cl_region ON change_log(region_code, ts);
CREATE INDEX IF NOT EXISTS ix_cl_event  ON change_log(event, ts);

-- 코드 크로스워크(법정동 ↔ V-World ↔ 법령 org/sborg ↔ lofin laf_cd). 수기보정 대응.
CREATE TABLE IF NOT EXISTS code_crosswalk (
  sig_cd          TEXT PRIMARY KEY,     -- 5자리(스파인 = region_cd[:5])
  sido_nm         TEXT, sgg_nm TEXT,
  vworld_sig_cd   TEXT,
  law_org         TEXT, law_sborg TEXT,
  lofin_laf_cd    TEXT,
  region_id       TEXT REFERENCES regions(region_id),
  match_method    TEXT,                 -- 'deterministic'|'name-join'|'manual'
  confidence      REAL,
  verified        INTEGER DEFAULT 0,
  note            TEXT                  -- 통합/폐지 별칭 등
);

-- 스키마 메타(마이그레이션 추적)
CREATE TABLE IF NOT EXISTS schema_meta (
  key             TEXT PRIMARY KEY,
  value           TEXT
);
INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '1');
INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('created_by', 'policymap.db.init_db');

-- ===========================================================================
-- 9. 법령그래프 국제표준 정합 (ELI / Akoma Ntoso Naming Convention / FRBR)
--    구현: policymap/standards.py  (뷰는 standards.migrate() 가 생성 — SQLite 는
--    CREATE VIEW 시점에 컬럼을 해석하므로 미이행 DB 에서 executescript 가 깨지지 않게 분리)
--
--    [실측 주석] 자치법규 목록 API 의 자치법규ID(ord_id)는 FRBR Work 식별자가 아니다.
--    ordinances 159,452행에서 ord_id distinct = 159,452 (1:1). 동일 조례의 개정 전후가
--    서로 다른 ord_id 를 갖는 사례를 확인했다(예: '강진군 상징물 조례' ord_id 2021627 / 2250395).
--    따라서 Work 축은 (author_token, 정규화 자치법규명, ord_kind) 결정적 해시로 구성한다.
-- ===========================================================================

-- FRBR Work 축(자치법규). Expression = ordinances 각 행.
CREATE TABLE IF NOT EXISTS ordinance_work (
  work_id          TEXT PRIMARY KEY,    -- 'ow:{sha256 16hex}' 결정적
  work_uri         TEXT,                -- AKN NC Work IRI
  author_token     TEXT,                -- sig_cd | 'edu-{sido_cd}' | 'org-{8hex}'
  region_id        TEXT,
  org_name         TEXT,
  name             TEXT,                -- 최신 Expression 의 자치법규명
  name_norm        TEXT,                -- 정규화 명칭(그룹 키)
  ord_kind         TEXT,
  first_enacted_on TEXT,                -- ISO
  latest_effective_on TEXT,             -- ISO
  expression_count INTEGER DEFAULT 0,
  lifecycle        TEXT,                -- 최신 Expression 의 lifecycle
  succession_note  TEXT,                -- 소속 지자체 폐치·분합 상태(우리 고유 공간 승계축)
  computed_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_ow_author ON ordinance_work(author_token, name_norm);
CREATE INDEX IF NOT EXISTS ix_ow_count  ON ordinance_work(expression_count);
CREATE INDEX IF NOT EXISTS ix_ow_life   ON ordinance_work(lifecycle);

-- 시간 유효성 감사 로그(규칙 T1~T8). 보정은 명시적 apply 시에만.
CREATE TABLE IF NOT EXISTS temporal_audit (
  audit_id        TEXT PRIMARY KEY,     -- 'ta:{rule}:{entity_id}' 결정적(재실행 멱등)
  entity_type     TEXT NOT NULL,        -- 'ordinance'|'instrument'
  entity_id       TEXT NOT NULL,
  rule            TEXT NOT NULL,        -- 'T1_effective_before_promulgation' 등
  severity        TEXT NOT NULL,        -- 'error'|'warn'|'info'
  observed        TEXT,                 -- JSON(원본 값)
  repaired        INTEGER DEFAULT 0,
  repair_action   TEXT,
  checked_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_ta_rule   ON temporal_audit(rule, severity);
CREATE INDEX IF NOT EXISTS ix_ta_entity ON temporal_audit(entity_type, entity_id);

CREATE INDEX IF NOT EXISTS ix_ord_work   ON ordinances(work_id, version_no);
CREATE INDEX IF NOT EXISTS ix_ord_life   ON ordinances(lifecycle);
CREATE INDEX IF NOT EXISTS ix_ord_valid  ON ordinances(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS ix_ord_exuri  ON ordinances(expression_uri);
CREATE INDEX IF NOT EXISTS ix_li_work    ON legal_instrument(work_id, version_no);
CREATE INDEX IF NOT EXISTS ix_li_valid   ON legal_instrument(valid_from, valid_to);

INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('standards_section', '9');
