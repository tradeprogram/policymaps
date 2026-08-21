# policy_maps 운영 런북 (RUNBOOK)

조례 그래프 분석 시스템의 설치·키발급·실행·증분갱신·MCP 연결·트러블슈팅 절차. 통합 검증 기준일 **2026-08-18**.

- 코어는 **Python 표준라이브러리만으로 동작**한다. 키·무거운 의존성 없이도 `seed → build → export → mcp` 전 구간이 돈다(무키 데모 참조).
- 단일 진실원천: [`db/schema.sql`](db/schema.sql)(스키마) · [`CONTRACTS.md`](CONTRACTS.md)(모듈 계약).

---

## 0. 현재 동작 / 미동작 상태 (정직한 요약)

| 구간 | 상태 | 근거 |
|---|---|---|
| DB init (28 테이블) | 동작 | `run init` 실측 |
| korea100 무키 시드 (627 instrument / 4,427 article / 1,205 verification) | 동작 | `run seed` 실측 |
| graph build (627 노드) / export (7 파일 번들) | 동작 | `run build`/`export` 실측 |
| 스모크 미니월드 (68 노드 / 24 엣지 / 13 파일) | 동작 | `tests/smoke.py` EXIT=0 |
| pytest 56케이스 | 통과 | `pytest -q` 56 passed (신경망 9 · RAG 10 신규 포함) |
| MCP stdio (12 tool) | 동작 | `initialize`+`tools/list`+`tools/call` 5종 왕복 실측 |
| 그래프 신경망 (`run neural`) | 동작 | node_embeddings 627,609행 / neural_similarity 79,380행 |
| GraphRAG 인덱스 (`run index`) | 동작 | 62,460 문서 / 294 MiB(308 MB) / 34.65초 |
| CLI 검색 (`run search`) | 동작 | hybrid/graph/global 모드 실행 실측 |
| refresh boundary (V-World/StanRegin **픽스처**) | 동작(합성좌표) | `refresh --sources boundary` changed=35 |
| refresh law/ordin/na_bill/na_vote/budget (**실키 필요**) | 미검증 | 키 부재 → 파티션 격리·에러 계상만 실측 |

**실키가 있어야 검증되는 것**: 국가법령·자치법규 실수집, 국회 발의/표결 실수집, V-World 전국 실경계, 지방재정 실세출. 이들은 무키 상태에서 **정상적으로 격리(errors 계상, 크래시 없음)**됨을 확인했고, 수집기 내부 파싱은 픽스처로 단위 검증됨. 실 API 200 응답 표본 확보 후 필드 매핑 재검증이 남아 있다(각 collector 상단 주석에 `확인 필요` 표기).

---

## 1. 설치

```bash
cd F:/policy_maps/system

# 코어만(권장, 무의존성 — 표준라이브러리로 동작):
pip install -r requirements-core.txt

# 선택 가속/정밀 폴백 해제(있으면 자동 사용, 없으면 순수파이썬 폴백):
pip install -e ".[http,graph,geo]"     # httpx / networkx / shapely
pip install -e ".[full]"               # 전체(numpy·sklearn·sbert·duckdb 등, Python 3.11~3.13 권장)

cp .env.example .env                    # 키 채우기(아래 §2)
```

- **Python 3.11~3.13 권장**. 코어는 3.14에서도 컴파일·동작하나(현재 통합 환경 = 3.14.3), sentence-transformers·duckdb 등 C확장 휠은 3.14에 부재할 수 있어 `.[full]`은 3.13 이하에서 설치한다. CI(`refresh.yml`)는 3.13 고정.
- 콘솔 엔트리포인트: `pip install -e .` 후 `policymap <subcommand>` == `python -m policymap.run <subcommand>`.

---

## 2. 키 발급 (4종 + 행정표준코드)

`.env` 에 채운다(로드 우선순위: `os.environ` > `.env` > 기본값). GitHub Actions 는 `secrets`/`vars` 로 주입.

### 2.1 `LAW_OC` — 국가법령정보 Open API (법령·자치법규·행정규칙·위임)
- 발급: <https://open.law.go.kr> → 회원가입 후 **OPEN API 활용신청**. 승인 후 `OC` 값은 **신청 이메일의 ID 부분**(예 `g4c@korea.kr` → `LAW_OC=g4c`).
- 운영키는 호출 IP/도메인 등록 필수. 개발 검증용 `LAW_OC=test` 가 현재 일부 동작(실운영 전 자체 발급 권장).
- 호출 도메인: `www.law.go.kr/DRF/lawSearch.do`(목록)·`lawService.do`(본문).

### 2.2 `ASSEMBLY_KEY` — 열린국회정보 (발의·표결·의원)
- 발급: <https://open.assembly.go.kr> → 회원가입 → **OPEN API → 인증키 신청**. 즉시~수일 내 발급.
- 미발급 시 `sample` 키는 `ERROR-290`(권한 없음)으로 10건 제한/거부 → **실키 필수**.
- 엔드포인트: `open.assembly.go.kr/portal/openapi/{서비스명}?KEY=&Type=json&pIndex=&pSize=`.

### 2.3 `VWORLD_KEY` (+ `VWORLD_DOMAIN`) — V-World 데이터 API (행정경계)
- 발급: <https://www.vworld.kr> → 회원가입 → **오픈API → 인증키 발급**. 발급 시 **서비스 URL(도메인) 등록**이 필수이며, 서버 호출의 `VWORLD_DOMAIN` 이 등록 도메인과 일치해야 한다(불일치 시 인증 실패).
- 한도 초과 시 `OVER_REQUEST_LIMIT`. 엔드포인트: `api.vworld.kr/req/data`.

### 2.4 `LOFIN_KEY` — 지방재정365 세부사업별 세출 (예산)
- 발급: <https://www.lofin365.go.kr> → 회원가입 → **OpenAPI 활용신청**(요청제한 없음, 신청형).
- 명세 URL: `www.lofin365.go.kr/lf/hub/QWGJK`. 인증키 파라미터명(`Key` vs `serviceKey`)은 실키로 확정 필요 — 수집기는 현재 양쪽 동일값 전달로 헤지.

### 2.5 `STANREGIN_KEY` — 행정표준코드 StanReginCd (법정동 스파인)
- 발급: <https://www.data.go.kr> → `행정표준코드관리시스템 법정동코드 조회` 검색 → **활용신청**. 개발계정 10,000건/일.
- **Decoding(raw) 키 사용 권장**(HttpClient 가 파라미터를 urlencode 하므로 이중 인코딩 회피). 엔드포인트: `apis.data.go.kr/1741000/StanReginCd`.

> 키가 하나도 없어도 §3 무키 데모는 완전 동작한다. 각 키는 해당 소스 수집에만 필요.

---

## 3. 무키 데모 실행 (키 없이 end-to-end)

로컬 korea100 실데이터(국가제도 578종)로 파이프라인 전 구간을 증명한다. **네트워크·키 불필요.**

```bash
cd F:/policy_maps/system

python -m policymap.run init                     # 1. 스키마 생성(28 테이블, 멱등)
python -m policymap.run seed                      # 2. korea100 시드(627 instrument/4,427 article)
python -m policymap.run build                     # 3. 그래프 빌드 + 조례↔예산 링크
python -m policymap.run export --out data/         # 4. 정적 JSON 번들(manifest+shard+changes)
python -m policymap.run status                     # 5. 워터마크·테이블 카운트 요약
python -m policymap.run mcp                         # 6. MCP stdio 서버 기동(Ctrl-D 종료)
```

- `--db PATH` 로 DB 경로 override(전역 옵션, 서브커맨드 앞/뒤 어디든): `python -m policymap.run --db C:/tmp/demo.db seed`.
- 실측 결과: init=28테이블 → seed=instruments 627/articles 4,427/verification 1,205 → build=627노드/0엣지(korea100은 국가법령 백본, 자치법규·인접엣지는 실키 수집분) → export=7파일(manifest.json, graph/nodes.json, graph/edges.json, meta/graph-stats.json, regions/index.json, changes/latest.json, state/watermarks.json).

미니월드까지 포함한 통합 증명은 스모크가 담당(자치법규 3건·위임 3건·의안/표결 포함):
```bash
python tests/smoke.py    # 6단계 OK, EXIT=0 (nodes=68, edges=24, 13파일, tools=12)
```

---

## 4. 전체 수집 실행 (실키)

키를 `.env` 에 채운 뒤 소스 순서를 지켜 수집한다. **강제 순서**: `boundary → law → ordin → na_bill → na_vote → budget`(지역 스파인이 조례·예산 FK의 선행 조건).

```bash
python -m policymap.run init
python -m policymap.run refresh --sources boundary            # 지역 스파인·경계(+크로스워크·인접)
python -m policymap.run refresh --sources law,ordin           # 법령·조례(위임 4경로)
python -m policymap.run refresh --sources na_bill,na_vote     # 국회 발의·표결
python -m policymap.run refresh --sources budget              # 지방재정 세출
python -m policymap.run parse                                  # 인용·분류·임베딩·유사도 파생
python -m policymap.run build                                  # 그래프 재빌드 + 조례↔예산 링크
python -m policymap.run export --out data/                     # 정적 번들 재생성
```

- `refresh` 는 소스별 파티션(지역/연령/연도)을 열거해 `--concurrency`(기본 6) 병렬 처리한다. 워커마다 자체 DB 연결+HttpClient 로 격리(WAL).
- 한 파티션 실패는 전체를 중단시키지 않는다(격리 후 `mark_partition_status('error')`). 성공 파티션만 커서 전진(멱등).
- `parse --delegation` 은 위임 4경로(lsDelegated/lsStmd/lnkLsOrdJo/lnkLsOrd) 저수준 조회를 조합하므로 `LAW_OC` 필요.

옵션(계약 §5):
```
--sources law,ordin,na_bill,na_vote,budget,boundary
--reorg-event 2026-07-01-전남광주통합      # 경계 개편 승계(boundary 실행 시)
--concurrency 6 --retries 3 --error-budget 0.05
--out DIR
```

---

## 5. 증분 갱신 (CDC)

- **워터마크**: `watermarks(source, scope)` 복합키 + 파티션 커서. 파티션 완전 영속화 후에만 `advance_cursor`(멱등).
- **콘텐츠 해시 2단**: 목록 워터마크(언제 조회) + 본문/파티션 sha256(실제 변경) → 메타만 바뀐 재파싱·재임베딩 회피. **무변경 = 무이벤트**(change_log 미기록).
- **툼스톤**: 폐지·통합 노드는 하드삭제 금지, `status`/`valid_to` 전환(2026-07-01 전남광주통합 과도기 보존; `geo.REORG_EVENTS`).
- **변경로그 3채널 단일원천**: `change_log` → `changes/*.json`(웹) + MCP `ordinance-graph://changes?since=`(에이전트) + GitHub 이슈(운영).
- **스케줄**([`.github/workflows/refresh.yml`](.github/workflows/refresh.yml), cron UTC): law+ordin 주1회(일 21:00 UTC), na_* 일1회(22:00 UTC), budget 월1회(1일 20:00 UTC), boundary 는 `workflow_dispatch` 수동. `run.py` 가 `GITHUB_OUTPUT` 에 `changed_count`/`error_count`/`summary_path` 기록.
- **오류예산**: `error_count > max(10, ceil(파티션수 * error_budget))` 일 때만 런 실패(exit 1). 그 이하는 부분성공으로 통과.
- 커밋백 전제: 저장소 git 초기화 필요(현재 `F:/policy_maps` 는 repo 아님). 무커밋 대안은 `actions/cache` + `upload-artifact`.

---

## 6. MCP 연결

순수 파이썬 stdio JSON-RPC 서버. stdout 은 JSON-RPC 전용, 로그는 stderr.

기동:
```bash
python -m policymap.run mcp                      # == python -m policymap.mcp_server.server
POLICYMAP_DB=C:/path/policymap.db python -m policymap.mcp_server.server
```

MCP 클라이언트 설정 예(`claude_desktop_config.json` 등):
```json
{
  "mcpServers": {
    "policymap": {
      "command": "python",
      "args": ["-m", "policymap.mcp_server.server"],
      "cwd": "F:/policy_maps/system",
      "env": { "POLICYMAP_DB": "F:/policy_maps/system/data/policymap.db" }
    }
  }
}
```

핸드셰이크 스모크(파이프):
```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
 | python -m policymap.mcp_server.server
```

**tool 12종**(모두 읽기전용, 응답 봉투에 `execution_allowed:false` + `as_of_date` + `disclaimer` + `stale`):

| 기본 7종 | 신경망·RAG 5종 (→ [13_신경망_RAG_계층](../13_신경망_RAG_계층.md)) |
|---|---|
| `search_ordinance` 조례명 검색 | `semantic_search_ordinance` 조문 **내용** 의미검색(BM25+Dense+그래프확장) |
| `get_ordinance` 조문·연혁·근거법 | `similar_ordinances` 그래프 임베딩 유사 조례 |
| `similar_regions` **통계** 유사 지자체 | `neural_similar_regions` **그래프 임베딩** 유사 지자체 |
| `gap_analysis` 위임격차·커버리지 | `ordinance_effectiveness` 조례↔예산 집행률 |
| `diffusion_timeline` 확산 시계열 | `explain_path` 두 노드 간 경로 설명(왜 유사한가) |
| `region_profile` 지역 프로파일 | |
| `bill_vote_breakdown` 표결 분해 | |

**리소스**: `ordinance-graph://status`(신경망 모델·RAG 엔진·tool 수 포함), `ordinance-graph://peers/{sig_cd}`, `ordinance-graph://changes?since=`.

- tool 8~12 는 무거운 계층이 없어도 **강등해서 답한다**: `policymap.rag` 미탑재/인덱스 부재 → 조례명 LIKE 폴백(`_engine:"fallback-sql-like"`), 임베딩 미학습 → `similarity_edges` 통계 폴백 또는 통계 peer 결과 첨부. 강등 사실은 항상 `_engine`/`note` 로 응답에 남는다.
- **검증상태 규율**: `similar_ordinances`/`neural_similar_regions` 는 학습 파생 추정치이므로 `unverified` 를 명시하고, `ordinance_effectiveness` 는 `verified_links`(수작업 검증)와 `auto_links`(자동매칭)를 분리 보고하며, `explain_path` 는 추정 관계(`SIMILAR_TO`/`NEURAL_SIMILAR`/`FUNDED_BY`)가 경로에 하나라도 끼면 전체를 `unverified` 로 강등한다.
- `POLICYMAP_RAG_INDEX_DIR` 로 서버가 읽을 인덱스 루트를 바꿀 수 있다(기본 `<out_dir>/index`).

- 신선도 게이트: `POLICYMAP_MCP_MAX_AGE_DAYS`(기본 30일) 초과 시 응답에 `stale:true` 경고만 붙고 답변은 계속 제공(읽기전용 자문 서버). 부팅 실패는 DB 스키마 미초기화(`schema_meta` 부재)일 때만 → 먼저 `run init`.

---

## 7. 트러블슈팅

| 증상 | 원인 | 조치 |
|---|---|---|
| `RuntimeError: 필수 설정 누락: assembly_key …` | 해당 소스 키 미설정 | `.env` 에 키 채움. 무키면 그 소스 제외하고 실행 |
| `refresh` errors 계상되나 exit 0 | 파티션 격리 + 오류예산 이하 | 정상. 키/네트워크 점검 후 재실행(멱등) |
| MCP 부팅 즉시 실패 | DB 미초기화(`schema_meta` 부재) | `python -m policymap.run init` 선행 |
| MCP 응답 전부 `stale:true` | 마지막 갱신 30일 초과 | 재수집(`refresh`) 또는 `POLICYMAP_MCP_MAX_AGE_DAYS` 상향 |
| `OVER_REQUEST_LIMIT`(V-World) | 일 한도 초과 | 익일 재시도 또는 키 상향. 픽스처로 개발 지속 |
| `ERROR-290`(국회) | sample/무권한 키 | 실 `ASSEMBLY_KEY` 발급 |
| `ERROR-336`(국회 pSize>1000) | 페이지 크기 초과 | 수집기가 자동 축소(TransientError). 재실행 |
| StanRegin 인증 실패 | Encoding 키 이중 인코딩 | data.go.kr **Decoding(raw)** 키 사용 |
| V-World 인증 실패인데 키는 맞음 | `VWORLD_DOMAIN` 불일치 | 등록 도메인과 `VWORLD_DOMAIN` 일치시킴 |
| boundary 가 fixtures 로만 동작 | `VWORLD_KEY`/`STANREGIN_KEY` 미설정 | 정상 폴백(WARNING + 반환 `fixtures:True`). 실경계는 실키 필요 |
| PowerShell 에서 로그가 빨간 에러로 보임 | stderr 를 NativeCommandError 로 래핑 | 정상. `2>$null` 또는 Bash 사용 |
| `.[full]` 설치 실패(3.14) | C확장 휠 부재 | Python 3.11~3.13 사용, 또는 코어만(폴백 동작) |
| 콘솔에 한글 깨짐 | cp949 렌더 아티팩트 | 데이터는 UTF-8 정상. `chcp 65001` 또는 파일로 확인 |


---

## 8. 신경망·GraphRAG 계층 (`neural` / `index` / `search`)

그래프 학습·검색 계층. 상세 설계·측정치는 [13_신경망_RAG_계층](../13_신경망_RAG_계층.md).
**torch·sklearn·gensim·faiss 불필요** — numpy 만 있으면 되고, numpy 도 선택적이다(부재 시 학습 호출만 명시적으로 실패).

### 8.1 `neural` — 그래프 신경망 임베딩 학습

```bash
python -m policymap.run neural                                  # graphsage 기본(200 epoch)
python -m policymap.run neural --model all                       # node2vec + metapath2vec + graphsage
python -m policymap.run neural --model node2vec --epochs 5 --dim 128
python -m policymap.run neural --top-k 10 --max-items 6000        # neural_similarity kNN 범위
python -m policymap.run neural --no-similarity                    # 임베딩만 저장(kNN 생략)
python -m policymap.run neural --db C:/tmp/copy.db                 # 사본에서 안전 시험
```

| 옵션 | 기본 | 뜻 |
|---|---|---|
| `--model` | `graphsage` | `graphsage`\|`node2vec`\|`metapath2vec`\|`all`(쉼표 다중) |
| `--dim` | 128 | 임베딩 차원(metapath2vec 은 min(dim,64)) |
| `--epochs` | 모델별 | graphsage 200 / node2vec 5 / metapath2vec 3 |
| `--test-frac` | 0.1 | held-out 링크예측 평가 비율 |
| `--top-k` | 10 | `neural_similarity` Top-k |
| `--max-items` | 무제한 | kNN 전쌍비교 대상 상한(N² 폭발 방지) |
| `--no-similarity` | off | `neural_similarity` 적재 생략 |
| `--seed` | 20260819 | 난수 시드(동일 시드 → 동일 결과) |

파이프라인: `build_graph` → `GraphArrays` → `split_edges`(held-out 10% 를 **메시지패싱 그래프에서 제거**) → 학습 → held-out AUC → `save_node_embeddings` → `build_neural_similarity`(조례·지역).

- 실측: 전체 그래프 1,114,320노드 → 고립 제거 **209,203노드 / 517,792엣지**. graphsage 200 epoch **701초**, node2vec 5 epoch **1,258초**, 전체 세 모델 약 35~40분.
- ⚠ **메모리 여유가 없으면 node2vec 이 수 배 느려진다.** 워크 코퍼스(83M 토큰)가 4 GB 안팎을 쓰기 때문에, 같은 머신에서 대용량 DB 사본·검색 인덱스를 동시에 열어 두면 스와핑으로 실측 20분짜리 작업이 90분을 넘길 수 있다(본 세션 관측). 학습은 단독 실행하라.
- 결과는 `node_embeddings`(627,609행) / `neural_similarity`(79,380행) 에 적재된다. **기존 `embeddings`·`similarity_edges` 는 건드리지 않는다.**
- ⚠ **같은 `model_name` 을 덮어쓴다.** 검증된 임베딩을 보존하려면 반드시 `--db` 로 사본을 지정해 시험하라.
- 모델 하나가 실패해도 나머지는 계속 학습한다(모델 격리, exit 1 로 보고).
- numpy 부재 시 exit **3**(unavailable) — 실패가 아니라 미가용이다.

### 8.2 `index` — GraphRAG 검색 인덱스 구축

```bash
python -m policymap.run index                        # BM25+Dense 증분 + 커뮤니티 요약
python -m policymap.run index --force                 # 전체 재빌드
python -m policymap.run index --scope ordinance,statute
python -m policymap.run index --index-dir D:/idx      # 인덱스 루트 override
python -m policymap.run index --no-community           # 인덱스만
```

- 출력: `<out_dir>/index/{scope}/`(기본 `data/index/all`) + `data/index/communities/*.json`.
- 실측: **62,460 문서 / 308,415,405 B(294 MiB) / 34.65초**(약 1,800 docs/s). 변경 없으면 `reused=true`, **0.3초**, 쓰기 0.
- 증분 규율: `content_hash` 비교 → 신규/변경분만 새 세그먼트 append, 삭제분 툼스톤. 툼스톤 25% 초과 또는 세그먼트 8개 초과 시 자동 compaction(`mode=compact`).
- **인덱스는 git 에 넣지 않는다** — 결정적이므로 언제든 35초에 재생성된다(같은 코퍼스면 바이트 동일).
- 커뮤니티 요약: `ordinance_similarity` modularity 0.4648(10개) / `region_adjacency` 0.4514(9개 요약).

### 8.3 `search` — CLI 검색

```bash
python -m policymap.run search "반려동물 등록 지원" -k 5
python -m policymap.run search "산후조리 지원 근거 법령" --mode graph --graph-weight 1.0
python -m policymap.run search "출산장려금 지급 기준" --mode bm25         # 채널 비교
python -m policymap.run search "청년 주거 지원" --mode context --json      # LLM 컨텍스트 묶음
python -m policymap.run search "전국에서 반려동물 조례는 어떤 패턴으로 퍼졌나" --mode global
```

| `--mode` | 동작 | 평균 지연(질의 5개, 3회 중앙값) |
|---|---|---:|
| `bm25` | BM25 단일 채널(진단용) | 0.9 ms |
| `dense` | Dense 코사인 단일 채널(진단용) | 0.8 ms |
| `hybrid`(기본) | RRF 융합 | 1.9 ms |
| `graph` | 하이브리드 ⊕ 그래프 확장 융합 | 22.9 ms |
| `context` | `answer_context` JSON(조문 원문 + 근거 경로 + 커뮤니티 + citations) | — |
| `global` | 커뮤니티 요약 위 전역 질의 | — |

- `--graph-weight` 기본 **0.5** 는 실측 최적이다. 1.0(대칭 RRF)으로 올리면 그래프 노드가 검색 상위를 밀어내 MRR 0.900→0.767 로 떨어진다. 대신 1.0 에서는 전문검색으로 도달 불가능한 근거 법령이 상위에 들어온다(「산후조리 지원 근거 법령」→ 저출산·고령사회기본법 2위, 모자보건법 4위).
- `--index-dir` 미지정 시 `<out_dir>/index` 를 읽는다. 인덱스가 없으면 예외 → 먼저 `run index`.

### 8.4 순서

```bash
python -m policymap.run build      # 그래프 + 조례↔예산 링크 (선행)
python -m policymap.run neural     # 임베딩 (build 산출 그래프 필요)
python -m policymap.run index      # 검색 인덱스 (DB 조문만 필요, neural 과 독립)
python -m policymap.run mcp        # tool 12종 서빙
```

`neural` 은 `build` 의 그래프가 있어야 한다. `index` 는 `neural` 과 독립이며 조문만 있으면 된다. MCP 는 셋 다 없어도 폴백으로 기동한다.

### 8.5 트러블슈팅 (신경망·RAG)

| 증상 | 원인 | 조치 |
|---|---|---|
| `run neural` 이 exit 3 | numpy 부재 | `pip install numpy`. 코어 기능은 numpy 없이도 동작 |
| `semantic_search_ordinance` 가 `_engine:"fallback-sql-like"` | 인덱스 부재/손상 | `python -m policymap.run index --force` |
| `similar_ordinances` 가 `_engine:"similarity_edges…"` | 신경망 미학습 | `python -m policymap.run neural` |
| `neural_similar_regions` 가 `_engine:"unavailable"` + `fallback` 필드 | `node_embeddings` 비어 있음 | 동일 |
| `explain_path` 가 `found:false` | 관계당 이웃 200개 상한 내 미발견 | `--max_hops` 상향(최대 6). "경로 없음"이 아니라 "상한 내 미발견"이다 |
| `run index` 가 매번 전체 재빌드 | 툼스톤 25% 초과 → 자동 compaction | 정상 동작 |
| 학습 결과가 실행마다 다름 | `--seed` 미고정 | 동일 `--seed` 면 max\|diff\|=0.0 (실측) |
| 학습 후 조례 코사인이 전부 ~0.99 | 균등 negative 로 학습(표현 붕괴) | `negative_sampling='type-matched'` 기본값 유지 |

---

## 9. 검증 재현 (한 줄)

```bash
cd F:/policy_maps/system
python -m compileall -q policymap                                                                    # 문법(COMPILEALL_OK)
python -m pytest tests/ -q                                                                          # 56 passed
python tests/smoke.py                                                                               # EXIT=0
python -m policymap.run build                                                                       # 그래프 + 조례↔예산 링크
python -m policymap.run export --out data/                                                          # 정적 번들
python -m policymap.run index                                                                       # RAG 인덱스(증분, reused)
python -m policymap.run search "반려동물 등록 지원" -k 5                                             # CLI 검색
```

MCP stdio tool 12종 왕복:
```bash
printf '%s
'  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"explain_path","arguments":{"from_id":"11110","to_id":"26110"}}}'  | python -m policymap.mcp_server.server
```
