# policy_maps 모듈 구현 계약 (CONTRACTS.md)

이 문서는 **단일 진실원천**이다. 이후 빌드 에이전트는 여기 규정된 함수 시그니처·입출력·사용 DB API·대상 테이블을 **그대로** 구현한다. 임의 변경 금지 — 변경이 필요하면 이 문서를 먼저 고친다.

- 스키마: `db/schema.sql` (테이블·컬럼 정의)
- DB 접점: `policymap/db.py` (아래 §0 API만 사용)
- 설정: `policymap/config.py` (`get_config()` → `Config`)
- 유틸: `policymap/util.py` (`HttpClient`, `NotFound`/`TransientError`/`PermanentError`, `compact`, `content_hash`, `stable_id`, `as_list`, `retry`, `now_kst_iso`, `today_kst`)

공통 규율:
- 코어는 표준라이브러리만으로 컴파일·동작. 무거운 의존성은 `try/except ImportError` + 폴백.
- 원문 미러링 금지: 조문 텍스트 저장 OK, 별표/첨부는 URL만.
- 폐지/소멸은 하드삭제 금지 → `db.soft_delete`(툼스톤).
- NOT_FOUND/빈결과 = 정상적 없음 → `util.NotFound` 로 처리(재시도·오류예산 제외).
- 모든 수집기는 파티션 완전 영속화 **후에만** `db.advance_cursor` 호출.

---

## 0. db.py 공개 API (모든 모듈이 이것만 사용)

```python
connect(db_path=None) -> sqlite3.Connection
init_db(conn=None, schema_path=None) -> sqlite3.Connection
tx(conn)                                  # with tx(conn): ... (commit/rollback)
table_columns(conn, table) -> list[str]

upsert(conn, table, row: dict, pk: str|Iterable[str], *, hash_col=None) -> str
    # 반환 'inserted'|'updated'|'unchanged'. hash_col 지정 시 해시 가드(무변경 스킵).
upsert_many(conn, table, rows, pk, *, hash_col=None) -> dict[str,int]   # {inserted,updated,unchanged}
soft_delete(conn, table, pk_values: dict, *, status='repealed',
            status_col='status', valid_to_col=None, valid_to=None) -> None

get_watermark(conn, source, scope) -> dict|None
set_watermark(conn, source, scope, **fields) -> None
advance_cursor(conn, source, scope, cursor, *, last_hash=None, status='ok',
               changed=0, rows_seen=0, run_id=None, note=None) -> None
mark_partition_status(conn, source, scope, status, *, note=None, bump_retry=False) -> None

log_change(conn, *, entity_type, entity_id, event, source=None, scope=None,
           entity_name=None, before=None, after=None, fields_changed=None,
           region_code=None, run_id=None, official_url=None, ts=None) -> str  # change_id

execute(conn, sql, params=()) -> Cursor
fetchone(conn, sql, params=()) -> dict|None
fetchall(conn, sql, params=()) -> list[dict]
count(conn, table, where='', params=()) -> int
```

`source` 열거: `'law' | 'ordin' | 'na_bill' | 'na_vote' | 'budget' | 'boundary'`
`scope` 예: `'global' | 'sig:26110' | 'age:22' | 'laf:26110|fyr:2026'`

---

## 1. collectors — 외부 API 수집

각 collector 파일은 아래 시그니처의 함수를 노출한다. 공통 인자:
`cfg: Config`, `http: util.HttpClient`, `conn: sqlite3.Connection`.

### 1.1 `collectors/law.py` — 국가법령정보 (명세[law_api])

```python
# 저수준 호출 (dict 반환, 에러 신호는 NotFound/PermanentError 로 정규화)
law_list(http, cfg, query, *, display=100, page=1, **params) -> tuple[int, list[dict]]
    # target=law. 반환 (totalCnt, law[]). LawSearch.law 는 1건 시 dict → as_list.
law_body(http, cfg, mst) -> dict                # target=law lawService. 루트 '법령'.
eflaw_list(http, cfg, query, **params) -> tuple[int, list[dict]]   # 시행예정 포함
ordin_list(http, cfg, *, query=None, org=None, sborg=None, knd='30001',
           nw=1, rr_cls_cd=None, display=100, page=1, **params) -> tuple[int, list[dict]]
    # target=ordin. 루트 OrdinSearch.law[]. MST=자치법규일련번호.
ordin_body(http, cfg, mst) -> tuple[dict, list[dict]]   # (기본정보, 조[]) 루트 LawService
admrul_list(http, cfg, query, *, knd=None, org=None, display=100, page=1) -> tuple[int, list[dict]]
admrul_body(http, cfg, serial) -> dict          # ID=행정규칙일련번호. 루트 AdmRulService
ls_stmd(http, cfg, mst) -> dict                 # target=lsStmd 체계도. 루트 법령체계도
ls_delegated(http, cfg, mst) -> dict            # target=lsDelegated. 루트 lsDelegated
lnk_ls_ord_jo(http, cfg, query, *, jo=None, jobr=None, display=100, page=1) -> tuple[int, list[dict]]
lnk_ls_ord(http, cfg, *, query=None, org=None, display=100, page=1) -> tuple[int, list[dict]]

# 고수준 수집(증분·저장까지). run.py 가 호출.
collect_statutes(http, cfg, conn, *, queries: list[str], run_id=None) -> dict
    # 위임 상위법(유한집합) upsert → legal_instrument(+articles via parsers.article).
    # source='law', scope='global'. cursor='ancYd:{최근공포일}'.
collect_ordinances(http, cfg, conn, *, region_id: str, org: str, sborg: str|None,
                   knds=('30001','30002'), run_id=None) -> dict
    # 지자체 파티션 1개. 1단 목록 워터마크 → 2단 본문 content_hash 가드.
    # source='ordin', scope=f'sig:{sig_cd}'. cursor='efYd:..|ancYd:..|maxMST:..'.
    # 신규/개정만 ordin_body 재조회. 폐지(rr=300204)는 soft_delete + log_change('repealed').
```

**반환 dict 공통 형태**(모든 고수준 collect_*): `{'inserted':int,'updated':int,'unchanged':int,'changed':int,'errors':int,'status':'ok'|'partial'|'error'}`.

**대상 테이블**: `legal_instrument`, `articles`, `ordinances`, `ordinance_articles`, `ordinance_appendix`. 위임 관계는 parsers.delegation 이 채운다(수집기는 원천 dict만 확보·전달 또는 raw 캐시).

**에러 신호 해석**(law.py 내부):
- 인증실패: 응답 `{"result":..,"msg":..}` → `PermanentError`.
- 본문 미매치: 루트 키 `Law` 가 문자열 → `NotFound`.
- 목록 정상: `resultCode=='00'`. 단 연계/체계도(lnk*/lsStmd/lsDelegated)는 resultCode 없음 → 루트객체/`law` 배열 존재로 판정.

### 1.2 `collectors/assembly.py` — 열린국회정보 (명세[assembly])

```python
_call(http, cfg, service, *, pIndex=1, pSize=1000, **params) -> tuple[int, list[dict]]
    # 공통 envelope 해석: 루트[slug][0].head[0].list_total_count, [1].row[].
    # INFO-200(없음)→NotFound. ERROR-290(키)→PermanentError. ERROR-336(>1000)→pSize 축소.

collect_bills(http, cfg, conn, *, age: int, run_id=None) -> dict
    # service='nzmimeepazxkubdpn'. 커서 BILL_NO 하이워터. upsert bills.
    # 발의자 RST_MONA_CD/PUBL_MONA_CD → bill_proposers(+legislators 최소행).
    # source='na_bill', scope=f'age:{age}', cursor='billno:{max}|prop:{max제안일}'.
collect_vote_tallies(http, cfg, conn, *, age: int, run_id=None) -> dict
    # service='ncocpgfiaoituanbr'. 의안별 집계 → bills(member/vote/yes/no/blank_tcnt).
collect_member_votes(http, cfg, conn, *, age: int, bill_ids: list[str], run_id=None) -> dict
    # service='nojepdqqaweusdfbi' (AGE+BILL_ID 필수). 의원별 → votes(party_at_vote=POLY_NM).
    # 미의결→의결 전이 의안만 워킹셋으로. source='na_vote', scope=f'age:{age}'.
collect_legislators(http, cfg, conn, *, run_id=None) -> dict
    # service='nwvrqwxyaytdsfvhu'(현직). MONA_CD 조인키 → legislators/parties.
```

**대상 테이블**: `bills`, `bill_proposers`, `votes`, `legislators`, `parties`.
조인키: `BILL_ID`(bills.bill_id), `MONA_CD`(legislators.legislator_id), `POLY_NM`(votes.party_at_vote).

### 1.3 `collectors/geo.py` — V-World + StanReginCd (명세[geo_budget])

```python
collect_regions(http, cfg, conn, *, run_id=None) -> dict
    # StanReginCd getStanReginCdList 전량 페이징 → regions(스파인).
    # 시도: sgg_cd=='000'&umd=='000'&ri=='00'(level1). 시군구: sgg!='000'&umd=='000'(level2).
    # region_id/sig_cd/sido_cd/sgg_cd/full_name/valid_from(adpt_de). source='boundary'.
collect_boundaries(http, cfg, conn, *, reorg_event=None, run_id=None) -> dict
    # V-World LT_C_ADSIGG_INFO(BOX 전국, size=100 페이징) → region_geometry(MultiPolygon).
    # geomFilter='BOX(124.0,33.0,132.0,43.0,EPSG:4326)'. sig_cd 로 regions 조인.
    # reorg_event 지정 시 신/구 sig_cd diff → region_succession + soft_delete(구).
build_crosswalk(conn) -> dict
    # code_crosswalk 구축: sig_cd=region_cd[:5](결정적) / law org·sborg·lofin laf_cd는 이름조인.
    # regions 의 vworld_sig_cd/law_org/law_sborg/lofin_laf_cd 채움.
compute_adjacency(conn, *, method='auto') -> dict
    # region_geometry → region_adjacency(Queen). shapely 있으면 정밀, 없으면 ring-share 폴백.
```

**대상 테이블**: `regions`, `region_geometry`, `region_adjacency`, `region_succession`, `code_crosswalk`.

### 1.4 `collectors/budget.py` — 지방재정365 QWGJK (명세[geo_budget])

```python
budget_list(http, cfg, *, fyr, laf_cd=None, dbiz_nm=None, pIndex=1, pSize=100) -> tuple[int, list[dict]]
    # /lf/hub/QWGJK. Type=json(대문자). ERROR-300/310 해석.
collect_budget(http, cfg, conn, *, laf_cd: str, fyr: int, run_id=None) -> dict
    # 파티션(laf_cd×fyr) 단위. 행 정렬 후 파티션 sha256 → last_hash 다를 때만 upsert.
    # budget_id='ehojo-{laf_cd}-{fyr}-{seq}'. 자연키 (laf_cd,fyr,dbiz_cd).
    # source='budget', scope=f'laf:{laf_cd}|fyr:{fyr}', cursor='exe:{max exe_ymd}'.
    # 확정연도(현재-2 이하)는 분기 1회만 재확인(동결).
```

**대상 테이블**: `budget_lines`. 조례↔예산 매칭은 parsers 아님 → graph/analysis 또는 별도 링커가 `ordinance_budget_link` 채움(§3.5 참조).

---

## 2. parsers — 파싱·파생

### 2.1 `parsers/article.py`

```python
parse_law_articles(body: dict, instrument_id: str) -> list[dict]
    # 법령.조문.조문단위[] → articles row[]. 조문여부=='조문'만. 항 있으면 항/호/목 접어 body.
    # article_id='statute:{mst}::제{no}조[의{branch}]'. content_hash=util.content_hash(body).
parse_ordinance_articles(jomun: list[dict], ordinance_id: str) -> list[dict]
    # LawService.조문.조[] → ordinance_articles row[]. 조문번호는 배열/6자리 중복 방어.
parse_admrul_articles(body: dict, instrument_id: str) -> list[dict]
    # AdmRulService.조문내용(통짜/배열 텍스트) 정규식 '제N조(제목)' 블록 분할.
save_articles(conn, rows: list[dict], *, table: str) -> dict[str,int]
    # upsert_many(conn, table, rows, pk='article_id'|'oa_id', hash_col='content_hash').
```

### 2.2 `parsers/delegation.py`

```python
parse_citations(text: str) -> list[dict]
    # 낫표 「법령명」 제N조제M항제K호O목 파싱(korea100 article-citations 규칙).
    # 반환 [{'law_name','article','clause','item','subitem','raw'}].
extract_delegations_from_lsdelegated(resp: dict, parent_id: str) -> list[dict]
    # lsDelegated.법령.위임조문정보[] → delegations row[]. 위임구분=='자치법규'만 조례.
    # source_path='lsDelegated'. 위임정보 배열/스칼라 as_list 방어.
extract_delegations_from_lsstmd(resp: dict, parent_id: str) -> list[dict]        # source_path='lsStmd'
extract_delegations_from_lnkjo(rows: list[dict], parent_id: str) -> list[dict]   # source_path='lnkLsOrdJo' (조문단위)
extract_delegations_from_lnkls(rows: list[dict], parent_id: str) -> list[dict]   # source_path='lnkLsOrd'  (문서단위)
merge_delegations(*sources: list[dict]) -> list[dict]
    # 4경로 합집합 dedup(자치법규일련번호+조문). 조문정밀 lnkLsOrdJo/lsDelegated 우선.
save_delegations(conn, rows: list[dict]) -> dict[str,int]
    # delegation_id=util.stable_id(child_id,parent_id,child_article,parent_article,source_path).
    # upsert_many(conn,'delegations',rows,pk='delegation_id').
save_citations(conn, rows: list[dict]) -> dict[str,int]
    # instrument_relations(relation='CITES'), rel_id=util.stable_id(...).
```

**대상 테이블**: `delegations`, `instrument_relations`.

### 2.3 `parsers/category.py`

```python
classify_ordinance(ordinance: dict, articles: list[dict], categories: list[dict]) -> list[dict]
    # 1차 룰기반(categories.keywords 앵커 매칭) → [{category_code,confidence,method:'rule'}].
    # 선택적 LLM 훅(있으면 method='llm'). 없으면 룰 결과만.
save_categories(conn, ordinance_id: str, rows: list[dict]) -> dict[str,int]
    # upsert_many(conn,'ordinance_category',rows,pk=('ordinance_id','category_code')).
```

### 2.4 `parsers/embedding.py`

```python
class Embedder:
    def __init__(self, model_name='char-ngram-tf'): ...    # sbert 있으면 model_name 교체
    def embed(self, text: str) -> dict|list                # 폴백: sparse {ngram:tf}
    def similarity(self, a, b) -> float                    # 코사인
embed_ordinances(conn, *, item_type='ordinance', model=None, run_id=None) -> dict
    # ordinances/ordinance_articles 텍스트 → embeddings(vector,norm).
build_similarity(conn, *, top_k=20, model_name=None) -> dict
    # kNN → similarity_edges(cosine_sim,rank). numpy 있으면 가속.
```

**대상 테이블**: `embeddings`, `similarity_edges`.

---

## 3. graph — 빌드·분석·export

### 3.1 `graph/build.py`

```python
build_graph(conn) -> "Graph"
    # SQLite → networkx.MultiDiGraph(있으면) 또는 폴백 dict 그래프.
    # 노드: Region/LegalInstrument/Ordinance/Bill/Legislator/Party/Category/BudgetLine/Article.
    # 엣지: HAS_ORDINANCE/DELEGATED_FROM/SUBORDINATE_TO/CITES/ADJACENT_TO/SUCCEEDED_BY/
    #       CONTAINS/SIMILAR_TO/IN_CATEGORY/FUNDED_BY/PROPOSED_BY/VOTED/MEMBER_OF/ENACTS.
node_id(kind: str, key: str) -> str        # 'region:26110' 등 접두 규약
```

### 3.2 `graph/analysis.py`

```python
find_peer_governments(conn, sig_cd: str, *, k=10, features=('budget','pop','structure')) -> list[dict]
compare_ordinance_coverage(conn, parent_instrument_id: str, *, region_level=2) -> dict
    # 동일 상위법 위임 대비 지자체별 제정/미제정 매트릭스.
trace_ordinance_diffusion(conn, template: str, *, since=None) -> dict
    # rrClsCd 제정일 + nw=2 연혁 시계열 확산(인접·유사 경로).
get_delegation_gap(conn, region_id: str) -> list[dict]
    # 위임 있으나 조례 부재(mandatory 미이행 포함).
compute_spatial_autocorrelation(conn, metric: str, *, method='moran') -> dict
    # region_adjacency 가중 Moran's I / LISA. numpy 있으면 가속, 없으면 순수파이썬.
link_ordinance_budget(conn, *, min_confidence=0.5) -> dict
    # 조례명↔dbiz_nm 유사 + 카테고리 게이트 → ordinance_budget_link.
```

### 3.3 `graph/export.py`

```python
export_static(conn, out_dir: str, *, as_of_date=None) -> dict
    # manifest.json + regions/{sig_cd}.json shard + graph/{nodes,edges}.json +
    # changes/latest.json + changes/feed-YYYY-MM.json (klocal 패턴).
    # 모든 조각에 as_of_date 동봉, 만료 시 stale:true.
export_graphml(conn, path: str) -> None
```

---

## 4. mcp_server — 순수 파이썬 stdio MCP

### 4.1 `mcp_server/server.py`

```python
class Server:
    def __init__(self, conn, cfg): ...
    def serve_stdio(self) -> None      # sys.stdin 줄단위 JSON-RPC 루프
    # 핸들러: initialize / tools/list / tools/call / resources/list / resources/read
main() -> None                          # entrypoint. 부팅 시 신선도 불변식 검증.
```

**tool 카탈로그**(구현 확정 — 사용자 표면 이름 **14종**, 입력 스키마는 JSON-Schema dict):

*기본 7종* — `search_ordinance`, `get_ordinance`, `similar_regions`, `gap_analysis`, `diffusion_timeline`, `region_profile`, `bill_vote_breakdown`.

*신경망·RAG 5종*(2단계 확장) — `semantic_search_ordinance`, `similar_ordinances`, `neural_similar_regions`, `ordinance_effectiveness`, `explain_path`.
위임: `semantic_search_ordinance`→`rag.hybrid_graph_search`/`rag.hybrid_search`, `similar_ordinances`·`neural_similar_regions`→`neural_similarity` 테이블(미학습 시 `similarity_edges` 또는 통계 peer 폴백), `ordinance_effectiveness`→`ordinance_budget_link ⋈ budget_lines` SQL, `explain_path`→SQL 양방향 BFS(관계당 이웃 상한 200 / 총 방문 상한 20,000).
추가 봉투 규율: 학습 파생 결과는 `verification_status:"unverified"` 를 명시하고, `ordinance_effectiveness` 는 `verified_links`/`auto_links` 를 분리 보고하며, `explain_path` 는 추정 관계(`SIMILAR_TO`/`NEURAL_SIMILAR`/`FUNDED_BY`)가 경로에 포함되면 전체를 `unverified` 로 강등한다. 인덱스 루트는 `POLICYMAP_RAG_INDEX_DIR` 로 override 가능.
각 tool은 내부적으로 graph.analysis 계약 함수에 위임한다(위임 실패 시 순수 SQL 폴백, provenance는 응답 `_engine`으로 표기):
`similar_regions`→`find_peer_governments`, `gap_analysis`→`get_delegation_gap`(+ `parent_instrument_id` 지정 시 `compare_ordinance_coverage`), `diffusion_timeline`→`trace_ordinance_diffusion`, `region_profile`→`build_region_profile`, `search_ordinance`/`get_ordinance`/`bill_vote_breakdown`→읽기전용 SQL.
모든 판단성 응답에 `as_of_date` + `official_url` + `disclaimer`("의사결정 지원, 법률판단 아님") + `execution_allowed:false` + `stale` 을 포함.
*정책확산 계량 2종*(3단계 확장 — `policymap.analytics` 위임) — `recommend_ordinances`, `spatial_autocorrelation`.
위임: `recommend_ordinances`→`analytics.peers.recommend_ordinances`(peer 집합은 `find_similar_governments`), `spatial_autocorrelation`→`analytics.spatial.moran`. 두 tool 은 numpy 를 요구하며 **폴백이 없다**(부재 시 ToolError). `recommend_ordinances` 는 조례 '명칭' 정규형 비교로 보유 여부를 판정하므로 `verification_status:"unverified"` 를 명시하고 근사일치를 `likely_variant`/`closest_own` 으로 노출한다.

**엔진 교체(2026-08-20)** — `similar_regions` 와 `diffusion_timeline` 의 1순위 위임처를 구 `graph.analysis` 에서 `policymap.analytics` 로 옮겼다. 구 엔진은 검증에서 방어 불가로 판정됐다: `find_peer_governments` 는 '정책구조' 축이 `ordinance_category` 커버리지 0.68% 위에서 계산돼 후보 226곳 유사도 평균 0.9402(변별력 0)이고 일반구(level=3)에 similarity 0.0 인 peer 를 반환했으며, `trace_ordinance_diffusion` 은 `enacted_on` 이 현행 판본 공포일이라 좌측절단·생존자편향이 있었다. 구 엔진은 analytics 미탑재 시 폴백으로 남기되 응답에 `caveat` 를 붙인다. `diffusion_timeline` 은 `engine:"legacy"` 로 구 엔진을 명시 선택할 수 있다.

(초기 계약 초안이 나열했던 `compute_spatial_autocorrelation` 은 위 `spatial_autocorrelation` 으로 tool 표면에 노출됐다. `read_graph_cypher` 는 미구현.)

**리소스**: `ordinance-graph://status`, `ordinance-graph://peers/{sig_cd}`, `ordinance-graph://changes?since=`.

---

## 5. run.py — 오케스트레이션 (엔트리포인트)

```python
main(argv=None) -> int
    # CLI: --sources law,ordin,na_bill,na_vote,budget,boundary
    #      --reorg-event TAG  --state PATH  --out DIR
    #      --concurrency 6 --retries 3 --error-budget 0.05
    #      subcommands: init | collect | parse | build | export | refresh | mcp
    #                   | seed | seed-regions | status | neural | index | search
    # init    : db.init_db
    # refresh : 소스별 collector 파티션 병렬(concurrency) 실행 + 오류예산 판정.
    #           GITHUB_OUTPUT 에 changed_count/error_count/summary_path 기록(refresh.yml 연동).
    # build   : graph.build.build_graph + graph.analysis.link_ordinance_budget
    # export  : graph.export.export_static
    # mcp     : mcp_server.server.main
    # neural  : 그래프 신경망 임베딩 학습(policymap.neural)
    #           --model graphsage|node2vec|metapath2vec|all --dim --epochs --test-frac
    #           --top-k --max-items --no-similarity --seed
    #           build_graph → GraphArrays → split_edges → 학습 → held-out AUC →
    #           save_node_embeddings → build_neural_similarity. numpy 부재 시 exit 3.
    # index   : GraphRAG 인덱스 구축(policymap.rag.build_index) + 커뮤니티 요약
    #           --scope all|ordinance|statute|sig:XXXXX|region:ID --force --index-dir
    #           --no-community. 증분이 기본(content_hash 비교, 무변경이면 쓰기 0).
    # search  : CLI 검색. --mode hybrid|bm25|dense|graph|context|global
    #           -k --scope --hops --graph-weight --index-dir --json
```

오류예산: `error_count > max(10, ceil(파티션수 * error_budget))` 일 때만 런 실패. 파티션 격리(한 실패가 전체 중단 금지) + 성공 파티션만 커서 전진.

---

## 6. 조인키·코드체계 빠른참조

| 축 | 키 | 테이블.컬럼 |
|---|---|---|
| 법령 본문 | MST(법령일련번호) | legal_instrument.mst |
| 법령 식별 | 법령ID 6자리 | legal_instrument.law_id |
| 행정규칙 본문 | 행정규칙일련번호 | legal_instrument.admrul_serial |
| 자치법규 본문 | 자치법규일련번호 | ordinances.mst |
| 의안 | BILL_ID(PRC_) | bills.bill_id |
| 의원 | MONA_CD | legislators.legislator_id |
| 정당(표결시점) | POLY_NM | votes.party_at_vote |
| 지역 스파인 | 법정동 10자리 | regions.region_cd |
| 지역(경계·조인) | sig_cd 5자리=region_cd[:5] | regions.sig_cd |
| 지역(법령API) | org/sborg(ELIS) | regions.law_org/law_sborg |
| 지역(예산) | laf_cd | regions.lofin_laf_cd |
| 예산 자연키 | (laf_cd,fyr,dbiz_cd) | budget_lines |

위임 4경로 합집합(dedup 자치법규일련번호): `lsDelegated`(조문·최다) ∪ `lsStmd`(문서·트리) ∪ `lnkLsOrdJo`(조문-조문) ∪ `lnkLsOrd`/`lnkOrg`(문서). 조문정밀 필요 시 lnkLsOrdJo/lsDelegated 우선, 근거문장은 ordin 본문 「」 인용과 대조.
