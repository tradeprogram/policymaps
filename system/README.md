# policy_maps — 조례 그래프 분석 시스템

대한민국 자치법규(조례·규칙)를 **지자체 · 시간 · 공간 · 재정** 축으로 교차 분석하는 도메인 그래프 시스템. 공식 원문 검색은 기존 MCP(ChangooLee/chrisryugj)에 위임하고, 이 시스템은 **유사지자체 · 조례격차 · 확산패턴 · 공간통계 · 재정연계** 분석을 신규 제공한다(명세[mcp_survey] §8~9 포지셔닝).

## 아키텍처 한눈에

```
공식 API 4종 ─▶ collectors ─▶ SQLite(policymap.db) ─▶ graph.build(networkx) ─┬▶ export(정적 JSON 번들 + GitHub Pages)
 (law/assembly/                 (증분 CDC·워터마크)      graph.analysis          └▶ mcp_server(stdio MCP)
  vworld/lofin)                                          (peer/gap/diffusion/공간)
      │                                                           ▲
      └── parsers(조문·위임·분류·임베딩) ───────────────────────────┘
```

- **코어는 Python 표준라이브러리만으로 동작**(sqlite3/urllib/json/csv/xml). httpx·networkx·shapely·numpy·sentence-transformers·duckdb 는 전부 **선택적** — 없으면 순수파이썬 폴백. (Python 3.14는 C확장 휠 부재 가능 → 3.11~3.13 권장, 코어는 3.14도 동작.)
- **단일 진실원천**: `db/schema.sql`(스키마) · `CONTRACTS.md`(모듈 구현 계약). 빌드 에이전트는 이 둘을 그대로 따른다.

## 디렉터리

```
system/
  db/schema.sql              # 전체 DDL(regions/legal_instrument/ordinances/delegations/
                             #   bills/votes/budget_lines/watermarks/change_log …)
  policymap/
    config.py                # 환경설정(os.environ + .env)
    util.py                  # HttpClient(httpx/urllib 폴백)·재시도·해시·레이트리밋·로깅
    db.py                    # 연결·init_db·upsert(해시가드)·watermark·change_log·tx
    collectors/{law,assembly,geo,budget}.py    # (빌드 예정) 외부 API 수집
    parsers/{article,delegation,category,embedding}.py  # (빌드 예정) 파싱·파생
    graph/{build,analysis,export}.py           # (빌드 예정) 그래프·분석·export
    mcp_server/server.py     # (빌드 예정) 순수 파이썬 stdio MCP
    run.py                   # (빌드 예정) 오케스트레이션 CLI(init/refresh/export/mcp)
  .github/workflows/refresh.yml   # cron 증분 갱신(소스별 분기)
  CONTRACTS.md               # 모듈별 정확한 함수 시그니처·입출력·대상 테이블
  pyproject.toml / requirements-core.txt / requirements-full.txt
  .env.example
```

`(빌드 예정)` 모듈은 이 라운드에서 계약(CONTRACTS.md)과 패키지 스캐폴드만 확정했다. 각 파일은 후속 빌드 에이전트가 CONTRACTS.md 시그니처대로 구현한다.

## 설치

```bash
cd system
# 코어만(권장, 무의존성):
pip install -r requirements-core.txt        # 실질 설치 없음(표준라이브러리로 동작)
# 또는 편집설치 + 선택 extras:
pip install -e ".[http,graph,geo]"          # 가속·정밀 폴백 해제
pip install -e ".[full]"                    # 전체(3.11~3.13)
cp .env.example .env                         # 키 채우기(LAW_OC/ASSEMBLY_KEY/VWORLD_*/LOFIN_KEY/STANREGIN_KEY)
```

## 실행 순서(스켈레톤 — 상세 런북은 통합단계에서)

```bash
python -m policymap.run init                                  # 1. DB 스키마 생성(멱등)
python -m policymap.run refresh --sources boundary            # 2. 지역 스파인·경계 먼저
python -m policymap.run refresh --sources law,ordin           # 3. 법령·조례(위임 4경로)
python -m policymap.run refresh --sources na_bill,na_vote     # 4. 국회 발의·표결
python -m policymap.run refresh --sources budget              # 5. 지방재정 세출
python -m policymap.run export --out data/                    # 6. 정적 번들 재생성
python -m policymap.run mcp                                   # 7. MCP stdio 서버 기동
```

## 증분 갱신(CDC)

- **워터마크**: `watermarks(source, scope)` 복합키 + 파티션 커서. 완전 영속화 후에만 커서 전진(멱등).
- **콘텐츠 해시 2단**: 목록 워터마크(언제 조회) + 본문 sha256(실제 변경) → 메타만 바뀐 재파싱·재임베딩 회피, 별표 개정 포착.
- **툼스톤**: 폐지·통합 노드는 하드삭제 금지, `status`/`valid_to` 전환(2026-07-01 전남광주통합 등 과도기 보존).
- **변경로그**: `change_log` → `changes/*.json`(웹) + MCP `ordinance-graph://changes?since=`(에이전트) + GitHub 이슈(운영) 3채널 단일원천.
- **스케줄**: `.github/workflows/refresh.yml` cron(UTC) 소스별 분기. law+ordin 주1회 / na_* 일1회 / budget 월1회 / boundary 수동(workflow_dispatch).

> 커밋백 전제: 저장소 git 초기화 필요. 무커밋 대안은 `actions/cache` + `upload-artifact` 로 워터마크·change_log 관리.

## 데이터 출처

국가법령정보 Open API(law.go.kr/DRF) · 열린국회정보(open.assembly.go.kr) · V-World(api.vworld.kr) · 지방재정365(lofin365.go.kr) · 행정표준코드(data.go.kr StanReginCd). 원문은 **미러링하지 않고** law.go.kr 직링크로 위임한다.

## 위계·시간·검증 모델(핵심)

- 규범은 단일 `legal_instrument` 슈퍼노드 + `national_tier`(0 헌법~4 행정규칙)/`local_tier`(L1 조례/L2 규칙)로 위계 표현. 조약·헌법기관규칙은 `tier_disputed`(학설대립) 플래그.
- **bitemporal**: 유효시간(공포→시행→폐지) + 기록시간(as_of_date + verification 3단계: source-linked/article-verified/needs-review, korea100 승계).
- 효력우선(상위법·신법·특별법)은 저장 엣지가 아니라 tier·시간·scope 에서 **파생 계산**, 판례·부칙 확정분만 `PREVAILS_OVER` 명시.
