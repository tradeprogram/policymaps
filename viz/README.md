# 자치법규 정책지도 — 시각화 사이트 (스캐폴드)

빌드도구·npm 설치 없이 브라우저에서 바로 열리는 정적 사이트다.
가상(mock) 데이터로 지금 당장 14개 화면이 전부 동작하고, **데이터 경로 한 줄만 바꾸면 실데이터로 돌아간다.**
(실데이터 전용 화면 5종 — 신경망·공간분석·확산 위험모형·생애주기·검증공시 — 은 가상 모드에서 «아직 굽지 않았다» 안내 패널로 강등된다.)

- 사이트 루트: `viz/public/`
- 진입점: `viz/public/index.html` (편의용 리다이렉트: `viz/index.html`)
- 의존성: 없음. 지도·차트·그래프 라이브러리만 CDN 에서 받고, **CDN 이 막히면 표 형태 대체 화면으로 자동 전환**된다.

---

## 1. 실행법

### 1-1. 권장 — 저장소 루트에서 서버 띄우기

가상데이터와 실데이터를 모두 볼 수 있는 유일한 방법이다.

```bash
cd F:/policy_maps
python -m http.server 8000
```

- 가상데이터: <http://127.0.0.1:8000/viz/public/index.html>
- 실데이터: <http://127.0.0.1:8000/viz/public/index.html?src=real>

### 1-2. 가상데이터만 볼 때

```bash
cd F:/policy_maps/viz/public
python -m http.server 8000
# http://127.0.0.1:8000/
```

이 경우 `../../system/data` 는 서버 루트 밖이라 접근되지 않으므로 실데이터 모드는 쓸 수 없다.

> `file://` 로 직접 열면 브라우저가 `fetch` 를 차단해 아무것도 안 나온다. 반드시 HTTP 서버로 열어야 한다.

### 1-1-A. AI 정책분석관까지 함께 띄우기

Gemini API 를 쓰는 발표용 실행법이다. API 키는 브라우저에 넣지 않고 로컬 프록시 서버가 환경변수에서만 읽는다.

```bash
cd F:/policy_maps
echo GEMINI_API_KEY=발급받은_키 > .env
python viz/serve_ai.py --port 8742
```

- 접속: <http://127.0.0.1:8742/viz/public/index.html?src=real>
- 우하단 `AI` 버튼을 누르면 정책분석관 패널이 열린다.
- `.env` 는 `.gitignore` 로 제외되어 GitHub 에 올라가지 않는다.
- `GEMINI_API_KEY` 가 없거나 일반 `python -m http.server` 로 띄운 경우에도 로컬 요약 모드로 동작한다.

### 1-1-B. 완전판 — 조문 본문까지 (로컬 DB 직결)

배포본(Vercel 정적)에는 조문 **본문**이 없다. `ordinance_articles` 236만 행의 본문 합계가
약 490MB 라 GitHub·Vercel 정적 배포 용량을 넘기 때문이다. 배포본은 조문 **번호와 제목**까지만 담는다.

로컬에서 DB(`system/data/policymap.db`, 4.3GB)를 직접 읽는 서버를 띄우면 같은 화면이 100% 로 동작한다.

```bash
cd F:/policy_maps
python viz/serve_full.py              # 기본 127.0.0.1:8743
# 또는  run_full.bat            (윈도우: 브라우저까지 자동으로 연다)
# 또는  run_full.bat 9000       (포트 지정)
```

- 접속: <http://127.0.0.1:8743/viz/public/index.html?full=1>
- `serve_full.py` 는 `serve_ai.py` 를 상속한다 — 정적 서빙과 `/ai/chat`(Gemini 프록시)이 그대로 동작한다.
- DB 는 **읽기 전용**(`mode=ro`)으로 연다. 쓰기 경로가 없다.
- 시작하면 RAG 인덱스(9.5GB, 245만 문서)를 백그라운드로 예열한다(실측 141초).
  예열 전 첫 질의는 2분 넘게 걸리고, 예열 후에는 질의당 1~2초다. `--no-warm` 으로 끌 수 있다.
- **DB 가 없으면 화면은 자동으로 정적 shard 로 폴백한다.** 배포본은 이 경로로만 동작하므로
  완전판 코드가 배포본을 깨뜨리지 않는다.

#### 완전판에서 달라지는 것

| 화면 | 배포본(정적 shard) | 완전판(DB 직결) |
| --- | --- | --- |
| 검색 | 사전계산 40질의의 결과만 | **임의 질의** 조문 전문검색(BM25+dense+그래프, 236만 조문) |
| 조례 상세 `#/ordinance/{mst}` | 조문 번호·제목까지(2,365,068건 **전량**) | **조문 본문 전량** + 근거 상위법·분류·예산연결·판본·변경이력·신경망유사 |
| 법령 위계 | 지역 전량 247곳 + 조례 서브그래프 1,000건 | **199,858건 어느 것이든** 2홉 서브그래프 즉석 계산 |
| 법령 상세 `#/statute/{id}` | 대표 법령 200건의 조문만 | **법령 조문 86,745건 전량** |
| 지역 상세 | 자치법규 메타 199,858건 **전량** | 같음(+ 본문으로 바로 진입) |
| 신경망 유사도 | Top-10 엣지 1,877,420건 **전량** | 같음(+ 임의 조례 즉석 계산) |
| 국회 표결 | 의안 19,847건 · 표결 57,178행 **전량** | 같음 |

> 배포본에서 «전량»이라고 적은 것은 DB 건수와 산출 파일 건수를 대조해 확인한 값이다.
> 자산별 3열 비교표(DB / 배포본 / 완전판)는 [22 완성도 최종점검 §1.7](../22_완성도_최종점검.md) 에 있다.

#### API (모두 GET, 응답 봉투는 MCP 와 동일)

```
GET /api/db/status                      DB 존재·테이블 행수·RAG 색인 상태(프론트가 완전판 가용 여부 판단)
GET /api/db/ordinance/{ordinance_id}    조례 상세 + 조문 본문 전량
GET /api/db/search?q=&k=&mode=          조문 전문검색(mode=name 이면 조례명 LIKE)
GET /api/db/graph/{ordinance_id}        임의 조례 2홉 서브그래프
GET /api/db/neural/{ordinance_id}?k=    임의 조례 신경망 유사
GET /api/db/statute/{instrument_id}     법령 상세 + 조문 본문 + 하위 조례
GET /api/db/ordinances?sig_cd=&q=       지역/이름으로 조례 목록(상세로 들어가는 입구)
```

`ordinance_id` 는 `ordin:2137681` · `2137681` · `ordinance:ordin:2137681` 을 모두 받는다.

#### 완전판 판정과 폴백

`js/config.js` 의 `FULL_API.candidates` 를 순서대로 `GET {base}/status` 해서
`data.full_edition === true` 인 첫 번째를 쓴다. 전부 실패하면 조용히 정적 shard 로 간다.

- `?full=1` — 요청. 연결되면 초록 배너, 실패하면 실패 사유 배너가 뜬다.
- `?full=0` — 강제로 끈다(완전판 서버 위에서 배포본 동작을 확인할 때).
- 쿼리 없음 — 탐지는 하되 실패해도 조용하다(배포 환경의 기본 동작).

### 1-3. 가상데이터 재생성

```bash
cd F:/policy_maps
python viz/mock/generate_mock.py     # viz/public/data/ 에 39개 파일 생성
```

### 1-4. 지도 경계 파일 생성 (최초 1회, 이미 생성돼 있음)

```bash
python viz/tools/build_geo.py
# system/data/reference/skorea-municipalities-2018-geo.json (18.2MB, 250 feature)
#   + kostat_to_bjd.json 크로스워크
#   -> viz/public/geo/municipalities.geojson (0.63MB, 250 feature)
```

---

## 2. 데이터 소스 교체법

### 방법 A — 영구 전환 (권장)

`viz/public/js/config.js` 의 한 줄만 고친다.

```js
export const DATA_BASE = "../../system/data"; // 실데이터 (기본값)
// export const DATA_BASE = "./data";         // 가상데이터
```

기본값은 실데이터다. 가상데이터는 조례 302건·지역 27곳짜리 스캐폴드 표본이므로
개발과 회귀 시험 외에는 쓰지 않는다. 실수로 되돌리면 사이트가 목업을 보여준다.

### 방법 B — URL 로 임시 전환

`?src=mock` / `?src=real` 을 붙인다. 임의 경로도 된다: `?src=../../other/bundle`

### 실데이터로 바꾸면 달라지는 것

| 화면 | 가상데이터 | 실데이터(`system/data`) |
|---|---|---|
| 대시보드·지도·지역 상세 | 동작 | **동작** (manifest / regions / changes / graph-stats 전부 존재) |
| 법령 위계 그래프 | 동작 | 크기 경고 후 사용자가 확인해야 로드 (`graph/nodes.json` 134MiB) |
| 유사·격차분석 / 정책확산 / 실효성 / 표결 / 검색 | 동작 (대표 1건) | **동작 — 전국 243곳** (아래 2-A) |

뒤의 5개 화면은 `api/` 사전계산 결과를 소비한다. 요청 파라미터에 따라 계산되는 값이라 실행 중에 DB를 두드리지
않고, `make_nationwide.py` 가 미리 구워 둔 정적 JSON 을 읽는다. MCP 응답 봉투
(`{data, as_of_date, stale, execution_allowed, disclaimer}`)가 **그대로** 파일에 들어 있다.

---

## 2-A. 전국 shard — 사전계산 결과 배치

예전에는 대표 5곳만 담은 단일 파일(`api/gap.json` 하나에 5개 지역이 든 맵)이었다.
지금은 **전국 243곳 전부**를 지역별 파일로 쪼개 둔다. 뷰가 필요한 지역 하나만 받으므로
화면 전환이 빠르고, 파일 하나가 커져 GitHub 100MB 한계에 걸릴 일도 없다.

### 2-A-1. 레이아웃

```
system/data/api/                                        파일수      용량   생성기
├─ index.json                커버리지 카탈로그               1            make_nationwide.py
├─ gap/{sig_cd}.json         격차분석                     243   11.98 MB  make_nationwide.py
├─ peers/{sig_cd}.json       유사 지자체                   243    1.91 MB  make_nationwide.py
├─ effectiveness/{sig_cd}.json 조례-예산 실효성            243   25.01 MB  make_nationwide.py
├─ diffusion/{slug}.json     정책 확산                      6    0.17 MB  make_nationwide.py
├─ votes/{bill_no}.json      의안 표결                    149    0.48 MB  make_extend_fixtures.py
├─ votes_index.json          의안 카탈로그                   1            make_extend_fixtures.py
├─ search/{slug}.json        사전계산 질의                  40    0.57 MB  make_extend_fixtures.py
├─ search_index.json         질의 카탈로그                   1            make_extend_fixtures.py
├─ lifecycle/{sig_cd}.json   제정·개정·폐지 이력           243    5.04 MB  make_extend_fixtures.py
├─ lifecycle_index.json      전국 생애주기 집계               1            make_extend_fixtures.py
├─ statute/{slug}.json       상위법 + 조문 열람             200   12.12 MB  make_extend_fixtures.py
├─ statute_index.json        법령 카탈로그                   1            make_extend_fixtures.py
├─ verification/summary.json 검증 공시 전수                  1    0.01 MB  make_extend_fixtures.py
├─ succession.json           지자체 승계 17건                1            make_extend_fixtures.py
├─ graph/                    법령 위계 서브그래프            422   16.24 MB  make_graph_fixtures.py
│   ├─ ordinance/{key}.json    조례 2홉                   300
│   ├─ statute/{key}.json      법령 중심                   120
│   ├─ hierarchy.json          위계 개념도·통계
│   └─ index.json              커버 목록
├─ neural/                   임베딩 유사도                  645   11.55 MB  make_neural_fixtures.py
│   ├─ ordinance/ordin-{mst}.json  조례 400 · 모델 3종
│   ├─ region/{sig_cd}.json        지자체 243
│   ├─ quality.json               모델 품질지표
│   └─ index.json
├─ spatial/{slug}.json       Moran's I · LISA               7    0.49 MB  make_analytics_fixtures.py
├─ eha/{slug}.json           이산시간 위험모형                3    0.09 MB  make_analytics_fixtures.py
├─ community/summary.json    커뮤니티 탐지                   1    0.10 MB  make_analytics_fixtures.py
├─ peer_methods/{sig_cd}.json 유사 지자체 방법비교          227    1.23 MB  make_analytics_fixtures.py
│   └─ _guide.json              공통 안내문(1회만)
├─ analytics.json            공간·EHA·커뮤니티·방법비교 카탈로그       make_analytics_fixtures.py
│
│   ── 아래는 전량 생성기(2026-08-23) 산출. 전부 .json.gz 로 굽혀 있다 ──
├─ ordinance/{sig_cd}.json   자치법규 메타 199,858건       247    (전량)  make_full_ordinance.py
│   └─ articles/{sig_cd}.json  조문 제목 2,365,068건       248
├─ statute/all/{bucket}.json 법령 29,811건                 32            make_full_statute.py
│   └─ names/{bucket}.json     정규화 법령명 색인           32
├─ delegation/{sig_cd}.json  위임관계 421,627건            245            make_full_statute.py
├─ bill/{bucket}.json        의안 19,847건                207            make_full_vote_neural.py
├─ legislators.json          국회의원 320명                 1
├─ neural/by-region/{sig}.json 유사도 Top-10 1,877,420엣지 266            make_full_vote_neural.py
└─ graph/by-region/{sig}.json 지역 위임 서브그래프          247            make_full_graph.py
    └─ instruments.json        상위법 공용 사전 32,727건      1
                             ──────────────────────────────────────────────
                             합계 4,972파일 · 75.82 MB · 최대 파일 443.0 KB
                             (사전압축 전 405.6 MB → gzip 5.2배)
```

> **용량 규율**: `api/` 총량 250 MB 이하, 파일 하나 100 MB 미만(GitHub 한계).
> 실측 **75.82 MB / 최대 443.0 KB** 로 둘 다 만족한다.
> 전량화로 405.6 MB 까지 불어난 것을 **데이터를 깎지 않고** gzip 사전압축으로 맞췄다
> (위 «사전압축» 절 참조). 그래도 넘치면 줄일 순서는
> ① `make_full_vote_neural.py --neural-topk 3`(-11 MB) → ② `make_full_ordinance.py --skip-articles`(-14 MB)
> → ③ `make_full_graph.py --ord-limit 300`(-2 MB) 이다.

대상 243곳 = `status='active' AND has_legislation=1 AND level IN (1,2)`
(광역 16 + 기초 227). level 3 일반구 41곳은 조례 제정권이 없어 제외한다.

`gap`·`peers` shard 의 `data` 는 예전 단일 파일과 **똑같이** `{sig_cd: 결과}` 맵이다(원소 1개).
덕분에 뷰는 집계본이든 shard 든 같은 코드로 처리한다.

### 2-A-2. 생성법

```bash
cd F:/policy_maps/system
python make_nationwide.py                      # 전국 243곳 전체 (약 7분, 39MB)
python make_nationwide.py --limit 5            # 소규모 검증
python make_nationwide.py --only gap,peers --regions 11680,47190 --force
python make_nationwide.py --votes-top 30
```

| 옵션 | 뜻 |
|---|---|
| `--only` | `gap,peers,effectiveness,diffusion,votes,search` 중 선택 |
| `--regions` | `sig_cd` 직접 지정(쉼표). `--level`/`--limit` 무시 |
| `--force` | 이미 있는 shard 도 재생성 (기본은 건너뜀 = 재개 가능) |
| `--diffusion` | `"조례명패턴=파일slug,..."` |
| `--min-coverage` | 제정본 커버리지 경고 임계 (기본 0.30) |

#### 확장 생성기 4종 (2026-08-22 추가)

`make_nationwide.py` 가 덮지 않는 자산(신경망 임베딩·법령 조문·공간통계·생애주기 등)을 굽는다.
모두 `make_gap_fixtures.envelope` · `make_nationwide.write_json` · `make_more_fixtures._sanitize_keys`
를 그대로 재사용하므로 봉투 형식·키 살균·원자적 쓰기 규칙이 동일하다.

```bash
cd F:/policy_maps/system

# 신경망 임베딩 유사도 (조례 400 · 지자체 243 · 모델 3종)
python make_neural_fixtures.py --force                       # 약 17s

# 공간통계 + EHA + 커뮤니티 + 유사방법비교
python make_analytics_fixtures.py --permutations 999 --force  # 약 12분(커뮤니티 탐지가 대부분)
python make_analytics_fixtures.py --only spatial --permutations 99   # 빠른 검증
python make_analytics_fixtures.py --only peer_methods --force       # 약 3분

# 법령 위계 서브그래프 (전량 그래프 빌드 18.8GB 없이 DB에서 씨앗 중심으로)
python make_graph_fixtures.py --max-nodes 60 --statute-children 45 \
       --max-parents 15 --max-cited 15 --force               # 약 9s(콜드 144s)

# 검색 40질의 · 표결 149의안 · 생애주기 243곳 · 법령조문 200건 · 검증공시 · 승계
python make_extend_fixtures.py --force                        # 약 4s(콜드 수분)
```

| 생성기 | 주요 옵션 |
|---|---|
| `make_neural_fixtures.py` | `--limit` 조례 수 · `--sample` 품질표본 · `--pairs` 무작위쌍 |
| `make_analytics_fixtures.py` | `--only spatial,eha,community,peer_methods` · `--permutations` · `--peer-k` · `--peer-limit` |
| `make_graph_fixtures.py` | `--max-nodes`(용량 조절 1순위) · `--ord-limit` · `--statute-limit` · `--statute-children` |
| `make_extend_fixtures.py` | `--only search,votes,lifecycle,statute,verification,succession` · `--statute-top` · `--article-limit` |

이미 만들어진 파일은 건너뛰므로 중간에 끊겨도 다시 실행하면 이어서 굽는다.
`tmp` 에 쓰고 `os.replace` 로 바꾸는 원자적 쓰기라 반쪽 파일이 남지 않는다.
한 지역이 실패해도 나머지는 계속 만들고, 실패는 `index.json` 의 `errors` 에 쌓인다.

#### 전량 생성기 4종 (2026-08-23 추가) — 표본이 아니라 전수

위 확장 생성기가 «대표 표본»을 굽는다면, 이 넷은 **DB 전량**을 굽는다.

```bash
cd F:/policy_maps/system

# 자치법규 199,858건 + 조문 제목 2,365,068건 → api/ordinance/ 497파일
python make_full_ordinance.py                       # 재개 22s

# 법령 29,811건 + 위임관계 421,627건 → api/statute/all·names/, api/delegation/
python make_full_statute.py                         # 재개 1.7s

# 표결 200 + 의안 19,847 + 신경망 Top-10 1,877,420엣지
python make_full_vote_neural.py --neural-topk 10 --force    # 신경망만 약 305s

# 지역 위임 서브그래프 247곳 + 조례 서브그래프 1,000건
python make_full_graph.py                           # 재개 3.5s
```

| 생성기 | 용량 조절 레버 |
|---|---|
| `make_full_ordinance.py` | `--skip-articles`(조문 제목 번들 제외) · `--format objects` · `--sigs` |
| `make_full_statute.py` | `--with-citation`(낫표 인용 원문 +10MB) · `--buckets` |
| `make_full_vote_neural.py` | `--neural-topk`(10=전량 / 5 / 3) · `--bill-bucket-width` |
| `make_full_graph.py` | `--max-nodes` · `--ord-limit` · `--inline-instruments` |

#### 사전압축 — `api/` 는 `.json.gz` 로 굽혀 있다 (중요)

전량 생성을 마치면 `api/` 가 **405.6 MB** 가 된다. 배포 예산 250 MB 를 맞추려고
데이터를 깎는 대신 **gzip 으로 미리 구웠다**. JSON 이 5.2배로 줄어 **75.82 MB** 가 되고
**한 건도 잃지 않는다**.

```bash
cd F:/policy_maps
python system/tools_compress_api.py            # api/<디렉터리>/**/*.json -> .json.gz
python system/tools_compress_api.py --check    # 쓰지 않고 예상 용량만
python system/tools_compress_api.py --decompress   # 되돌리기
```

- 압축 대상은 **`api/` 하위 디렉터리의 shard 뿐**이다. 최상위 `api/*.json` 카탈로그 19개는
  비압축 그대로 둔다(부팅 경로를 단순하게 유지하려고).
- 원본 `.json` 은 지운다. 즉 `api/ordinance/11110.json` 을 직접 열면 **404** 이고
  `api/ordinance/11110.json.gz` 가 있다. 브라우저는 이 차이를 몰라도 된다 — 아래 참조.
- 재실행해도 같은 바이트가 나온다(gzip mtime 을 0 으로 고정). 다시 돌려도 git diff 가 생기지 않는다.

**읽는 쪽은 `viz/public/js/api.js` 의 `getJSONFromBase()` 한 곳이다.**

- `api/<디렉터리>/…json` 요청이면 `.json.gz` 를 먼저 받아 본다. 404 면 비압축 `.json` 으로 폴백한다
  (압축 전 번들·부분 압축 상태도 그대로 열린다).
- 서버가 `Content-Encoding: gzip` 을 붙이면 브라우저가 이미 풀어 놨고, 안 붙이면
  gzip 매직바이트 `0x1f8b` 를 보고 `DecompressionStream` 으로 직접 푼다.
  **`python -m http.server` · `serve_full.py` · Vercel 정적 어디서든 같은 파일이 그대로 동작한다.**
- 가상데이터(`viz/public/data`)에는 `api/` 하위 디렉터리가 없으므로 `.gz` 요청을 만들지 않는다.
  `?src=mock` 은 압축 이전과 완전히 동일하게 돈다.

> **브라우저 요건**: `DecompressionStream` — Chrome 80+ / Firefox 113+ / Safari 16.4+.
> 그보다 낮으면 «되돌리는 방법»을 적은 오류 메시지를 띄운다(조용한 빈 화면이 되지 않는다).
> 압축을 쓰고 싶지 않으면 `--decompress` 로 되돌리면 되고, 로더는 그대로 동작한다.

### 2-A-3. 로더 동작 — shard 우선, 단일 파일 폴백

`js/api.js` 는 이 순서로 찾는다. 셋 다 실패해야 안내를 띄운다.

1. `api/index.json` 이 지정한 경로 → 2. 관례 경로 `api/{kind}/{key}.json`
3. 예전 단일 파일 `api/{kind}.json` (그 안에 해당 지역/항목이 있을 때만)
4. 없으면 **에러가 아니라** "아직 사전계산되지 않았습니다" 안내 + 생성 명령어 표시

**확장 카탈로그 병합** — 목록형(`votes`·`search`)은 색인이 둘이다.
`api/index.json`(make_nationwide, 표결 15·검색 5)과 `config.EXTRA_CATALOGS` 가 가리키는
`api/votes_index.json`·`api/search_index.json`(make_extend, 표결 149·검색 40)을 `key` 로 합친다.
확장 색인이 없으면 조용히 무시하므로 가상데이터·구 번들에서도 그대로 돈다.

화면 하단에 `데이터 소스: 전국 shard — api/gap/11680.json` 처럼 **어디서 온 값인지 항상 밝힌다.**
3번 폴백은 `catalogKeyOf()` 로 동일 항목일 때만 쓴다. 그렇지 않으면 요청한 의안과 다른 의안을
그 의안인 것처럼 그리는 사고가 난다(실제로 있었던 버그다).

### 2-A-4. 선택기 사용법

| 화면 | 선택기 | 내용 |
|---|---|---|
| `#/gap` · `#/effectiveness` | 지역 선택기 | 전국 243곳, 시도별 optgroup 16개, 이름·코드 검색, `사전계산된 곳만` 필터 |
| `#/region/:sig_cd` | 지역 선택기 | 284곳(일반구 41곳 포함 — 지역 상세는 일반구도 shard 가 있다) |
| `#/diffusion` | 정책 템플릿 | 6종 |
| `#/votes` | 의안 | 149건 (표결수 상위 120 + 최근 처리 60, 중복 제거) |
| `#/search` | 사전계산 질의 | 40건 (C01~C14 전 분류 커버) |
| `#/lifecycle` | 지역 선택기 | 243곳 |
| `#/neural` | 조례 선택기 / 지역 선택기 | 조례 400건(격차분석 등장 225건 필터) · 지자체 243곳 |
| `#/spatial` | 지표 | 7종 |
| `#/analytics` | 정책 템플릿 / scope / 지역 선택기 | EHA 3종 · 커뮤니티 2 scope · 방법비교 227곳 |
| `#/graph` | 조례 / 법령 | 조례 300건 · 법령 120건 |

- 검색창에 `구미` 또는 `47` 을 넣으면 목록이 즉시 좁아진다. 지우면 243곳으로 복귀한다.
- 목록의 `✓` 는 사전계산된 곳, `·` 는 아직 안 구운 곳이다.
- 시도 그룹 이름은 하드코딩이 아니라 `regions/index.json` 의 `level=1` 항목에서 뽑는다.
  `전남광주통합특별시(12)` 처럼 표준 코드표에 없는 개편 지자체도 정확히 나온다.

### 2-A-5. 결과가 비는 32곳 — 버그가 아니다

전국 243곳 중 **32곳**(전남광주통합특별시 계열 28 + 인천 개편 4구:
제물포·영종·서해·검단)은 `gap`·`peers` 가 0건, `effectiveness` 가 링크 0건으로 나온다.

원인은 실측으로 확인했다. 이 32곳은 `region_features` 의
`budget_total`·`fiscal_self_ratio`·`welfare_ratio` 가 **NULL** 이다(2026년 통합·개편 신설이라
결산 통계가 아직 없다). 유사도 계산에 필요한 재정 지표가 없으니 peer 를 못 구하고,
peer 가 없으니 격차 추천도 못 낸다. 예산 원자료가 없으니 조례-예산 링크도 0건이다.

**엔진이 빈 결과를 낸 것이 정답이다.** 없는 값을 지어내면 안 되기 때문이다.
다만 화면에 `0곳` 만 찍히면 «유사한 지자체가 없다»는 결론으로 오독되므로,
`gap.js` 의 `missingIndicatorNote()` 와 `effectiveness.js` 의 링크 0건 분기가
**어떤 지표가 없어서 계산 자체가 불가능한지** 를 명시한다. 표기 규율 5번의 연장이다.

---

## 3. 화면 목록

14개 화면이다(초기 9개 + 2026-08-22 추가 5개). 모두 해시 라우팅이고 상태가 URL 에 남는다.

| # | 경로 | 화면 | 소비 데이터 |
|---|---|---|---|
| 1 | `#/dashboard` | 대시보드 — 전국 요약 카드, 정책분야 분포, 최근 변경 피드 | `manifest.json`, `regions/index.json`, `regions/*.json`(표본), `changes/latest.json`, `meta/graph-stats.json` |
| 2 | `#/map` | 시군구 코로플레스 — 지표 4종 전환, 클릭 시 상세 | `geo/municipalities.geojson`, `regions/index.json`, `regions/*.json` |
| 3 | `#/region/:sig_cd` | 지역 상세 — 조례수·분야·예산·최근변경 | `regions/{sig_cd}.json` |
| 4 | `#/gap` | **유사 지자체 + 격차분석 (킬러)** — "N곳 보유 / M곳 폐지" 배지 · 전국 243곳 선택기 | `api/index.json`, `api/peers/{sig_cd}.json`, `api/gap/{sig_cd}.json` |
| 5 | `#/graph` | 법령 위계 — ①위계 개념도 ②조례 2홉 서브그래프 ③법령 중심 + **조문 열람** | `api/graph/hierarchy.json`, `api/graph/ordinance|statute/*.json`, `api/statute/*.json` |
| 6 | `#/lifecycle` | **정책 생애주기** — 제정/개정/폐지 곡선 · 일괄폐지 코호트 · 지자체 승계 | `api/lifecycle/*.json`, `api/lifecycle_index.json`, `api/succession.json` |
| 7 | `#/diffusion` | 정책 확산 타임라인 — 채택곡선·로지스틱적합·Rogers·경로검정 | `api/diffusion/{slug}.json` |
| 8 | `#/effectiveness` | 조례 실효성 — 편성/지출/집행률 + **추정 연결 배지·confidence 등급** | `api/effectiveness/{sig_cd}.json` |
| 9 | `#/neural` | **신경망 유사도** — 조례/지자체 Top-10 이웃(모델 3종 비교) · 저장본 대조 · 모델 품질 | `api/neural/**`, `api/peers/*.json` |
| 10 | `#/spatial` | **공간 분석** — Moran's I · LISA 코로플레스(사분면/5분위) · FDR 보정 | `api/analytics.json`, `api/spatial/*.json`, `geo/municipalities.geojson` |
| 11 | `#/analytics` | **확산 위험모형** — ①EHA 이산시간 위험모형 ②커뮤니티 탐지 ③유사 지자체 방법비교 | `api/analytics.json`, `api/eha/*.json`, `api/community/summary.json`, `api/peer_methods/*.json` |
| 12 | `#/votes` | 국회 표결 — 정당별 찬반 스택 + 발의자 | `api/votes/{bill_no}.json`, `api/votes_index.json` |
| 13 | `#/search` | 조문 단위 검색 결과 카드 · 사전계산 40질의 선택기 | `api/search/{slug}.json`, `api/search_index.json` |
| 14 | `#/trust` | **검증 공시** — 검증 사다리 6단계 · 인용검증 · 시간감사 · 예산링크 신뢰도 | `api/verification/summary.json`, `api/succession.json` |

지표 4종(지도): 자치법규 수 / 예산 집행률 / 예산현액 / 선택 분야 조례 수·비중

### 3-A. 딥링크

화면 상태가 주소에 남아 그대로 공유된다.

```
#/graph?mode=hierarchy
#/graph?mode=ordinance&key=ordin-2139297
#/graph?mode=statute&key=statute-276653
#/neural?tab=quality            #/neural?tab=region&sig=47190
#/spatial?metric=ordinance-count
#/lifecycle?tab=region&sig=47190     #/lifecycle?tab=succession
#/analytics?tab=eha&slug=barefoot-walking
#/analytics?tab=community
#/analytics?tab=peers&sig=47190
```

---

## 4. 파일 구조

```
viz/
├─ index.html                  public/ 로 보내는 리다이렉트
├─ README.md                   이 문서
├─ mock/generate_mock.py       가상데이터 생성기 (별도 작업물)
├─ tools/build_geo.py          18MB 경계 GeoJSON -> 0.63MB 경량본 생성
└─ public/                     ★ 사이트 루트
   ├─ index.html
   ├─ css/style.css            외부 폰트·프레임워크 없음
   ├─ geo/municipalities.geojson   250 feature, sig_cd 로 키잉된 경량 경계
   ├─ data/                    가상데이터 번들 (generate_mock.py 산출, 39파일)
   └─ js/
      ├─ config.js             ★ DATA_BASE / CDN / 한계치 / confidence 등급 / API_SHARDS
      ├─ api.js                데이터 로더 + 전역 상태 + shard/fixture 폴백
      ├─ nationwide.js         전국 지역 선택기·항목 선택기·미생성 안내·데이터출처 줄
      ├─ util.js               포맷·DOM·동시성 제한·5분위 계급
      ├─ components.js         배너·배지·카드·표·에러 패널
      ├─ vendor.js             CDN 로더 (실패 감지)
      ├─ router.js             해시 라우터
      ├─ app.js                앱 셸
      └─ views/                화면 14개 (dashboard, map, region, gap, graph,
                               lifecycle, diffusion, effectiveness, neural,
                               spatial, analytics, votes, search, trust)
```

---

## 5. 표기 규율 — 코드에 박아 넣은 것

심사의 정확성 항목과 직결되므로 임의로 빼면 안 된다.

1. **가상 데이터 배너** — `manifest._mock === true` 면 상단에 노란 경고 배너가 상시 노출된다 (`components.mockBanner`).
2. **as_of_date 상시 노출** — 모든 화면 상단에 기준일이 찍힌다. `stale=true` 면 빨간 배너가 추가된다.
3. **조례↔예산은 "추정 연결"** — 실효성 화면 최상단에 보라색 경고 배너, 세부사업 행마다 신뢰도 배지.
   - `verified=1` → `확인됨(verified)` (초록)
   - `confidence ≥ 0.8` → `추정 연결 · 추정(높음)`
   - `0.6 ≤ confidence < 0.8` → `추정 연결 · 추정(중간)`
   - `confidence < 0.6` → `추정 연결 · 추정(낮음)` (빨강)
   - 근거로 표본 584건 검증(전체 정밀도 64.9%, conf≥0.8 구간 93.2%)을 화면에 적되, **그 값이 검증 시점 링크 93,964건 기준이라 현재 모집단에 그대로 적용되지 않는다**는 단서도 같이 적는다.
4. **폐지 조례 경고** — 격차분석은 선례 목록에 현행 조례만 넣고, 유사 지자체 중 폐지 사례가 있으면 `M곳 폐지` 빨간 배지 + 카드 좌측 빨간 띠 + 폐지 경고 문구 + 폐지 지자체·폐지일 표를 붙인다. 검색·실효성 화면도 `status=repealed` 면 동일하게 경고한다.
5. **제외된 엣지 표기** — `graph_stats.skipped_edges` 가 0이 아니면 대시보드·그래프 화면에 "이 관계는 그릴 수 없다"고 명시한다. (실번들에서 `FUNDED_BY` 는 183,145건 전량, `CITES` 는 810,150건이 제외돼 있다.)
6. **산출 방법 공개** — 유사 지자체 화면에 가중치와 그 출처(`mois_public` 행안부 공개기준 / `ours` 자체 설정)를 표로 펼쳐 두고, 확보 못 한 행안부 지표도 나열한다.
7. **통계적 유의성** — 확산 경로 순열검정의 `p_sim` 이 0.05 이상이면 "이 데이터로 인과 주장을 할 수 없다"는 경고를 자동으로 붙인다.
8. **execution_allowed=false** — MCP 봉투를 소비하는 화면은 하단에 자동 집행 불가와 disclaimer 를 그대로 노출한다.

---

## 6. 지도에서 반드시 알아야 할 것 (실측 확인됨)

### 6-1. 좌표계가 아니라 **코드체계**가 문제다

원본 경계 파일 `skorea-municipalities-2018-geo.json` 의 `properties.code` 는 **통계청(SGIS) 코드**다(종로구 `11010`).
우리 `sig_cd` 는 법정동 기준(`11110`)이라 그대로 조인하면 전부 어긋난다.
`build_geo.py` 가 `kostat_to_bjd.json` 으로 변환해 `properties.sig_cd` 로 다시 써 넣는다. **250건 전수 매칭, 미매칭 0건.**

### 6-2. 일반구를 둔 시는 경계 파일에 없다 — 상위 단위 롤업이 필수다

경계 파일에는 `수원시(41110)` 가 없고 `수원시장안구(41111)`·`권선구(41113)`… 만 있다.
그런데 일반구는 자치법규 제정권이 없어 shard 의 `ordinance_total` 이 **구조적으로 0** 이다(실데이터 level 3 = 41개 전부 0).

그대로 두면 수원시 867건이 지도에서 사라지고 0인 장안구가 칠해진다.
그래서 `map.js` 의 `lookupValue()` 가 이렇게 처리한다.

- 폴리곤이 index 에 **level 3** 으로 있거나 **아예 없으면** → 상위 시(`sig_cd[0:4] + "0"`, level 2 인 경우만) 값으로 채우고 **점선 테두리 + 툴팁에 "소속 시 ○○ 값으로 채움"** 표기
- 행정시(제주시 `50110`, 서귀포시 `50130`)만 예외적으로 시도(`50000`)까지 올린다
- **시도로의 일반 롤업은 금지**한다. 허용하면 shard 없는 자치구까지 광역 값으로 칠해져 "서울 모든 구 = 서울시 값" 같은 왜곡이 생긴다 (초안에서 실제로 발생해 113개가 잘못 칠해졌고, 규칙을 좁혀 16개로 교정했다)

실데이터 기준: 250개 폴리곤 중 **247개 매칭(그중 34개가 롤업)**.

### 6-3. 인천 3개 구는 매칭되지 않는다 — 정상이다

경계 파일은 2018년 기준인데 인천은 이후 개편됐다.

- 경계에만 있음(현행 스파인에 없음): `28110 중구`, `28140 동구`, `28260 서구`
- 스파인에만 있음(경계 없음): `28125 제물포구`, `28155 영종구`, `28275 서해구`, `28290 검단구`

**2026년 인천 지도는 이 경계 파일로 정확히 그릴 수 없다.** 최신 경계로 교체하거나, 화면에 시차를 명시해야 한다.

### 6-4. 경량화는 표시 전용이다

Douglas-Peucker(eps 0.002°≈200m) + 좌표 4자리 절삭으로 좌표점을 441,520 → 32,223개(7.3%)로 줄였다.
**면적·거리·인접 계산에 쓰면 안 된다.** 그런 계산은 `region_adjacency`(1,098건) 같은 DB 값을 써야 한다.

---

## 7. 성능 가드

`config.js` 의 `LIMITS` 로 조절한다.

| 항목 | 기본값 | 이유 |
|---|---|---|
| `graphNodeWarn` | 20,000 | 실데이터 `graph/nodes.json` 은 **134MiB**. 넘으면 자동 로드를 막고 확인 버튼을 띄운다 |
| `fetchConcurrency` | 8 | 지역 shard 동시 요청 수 (실데이터 284개) |
| `categorySample` | 40 | 대시보드 분야 집계 표본. **전수가 아님을 화면에 표기**한다 |
| `graphRenderNodes` | 300 | ego 확장 시 노드 상한. 넘으면 "잘림"을 표기한다 |

---

## 8. 오프라인/사내망 대응

CDN 3종(Leaflet 1.9.4 / Chart.js 4.4.1 / vis-network 9.1.9)이 막히면 각 화면이 안내 패널 + **표 대체 화면**으로 떨어진다.
`?nocdn=1` 을 붙이면 강제로 실패시켜 이 경로를 점검할 수 있다.

완전 오프라인 배포가 필요하면 세 파일을 내려받아 `public/vendor/` 에 두고 `config.js` 의 `CDN` 경로를 상대경로로 바꾸면 된다.

---

## 9. 공개 배포 전 확인 (중요)

`viz/public/data/` 의 가상데이터 `official_url` 은 **살균 완료**다 —
`OC=` 값이 전부 리터럴 `OC=MOCK` 이고 실키는 0건이다(2026-08-23 재확인).
남은 배포 전 확인 항목은 목업 번들 자체를 배포본에서 제외하는 것이며,
루트 `.vercelignore` 가 `viz/public/data/` 를 빼도록 되어 있다.
GitHub Pages 로 올리면 그대로 웹에 게시되므로, `20_깃허브_공개계획.md` 의 살균 게이트를 통과시킨 뒤 배포해야 한다.
사이트 코드(`viz/public/js/**`, `css`, `index.html`)와 `tools/build_geo.py` 에는 키가 없다.

---

## 10. 검증 기록 (2026-08-21)

- `node --check` — JS 16개 파일 전부 통과
- `python -m http.server` + 브라우저 — 9개 화면 전부 렌더, 콘솔 에러 0
- 가상데이터: 대시보드 카드 8종 / 지도 250 폴리곤(31개 착색, 16개 롤업 점선) / 격차분석 카드 14건 중 폐지경고 7건 / 실효성 신뢰도 배지 높음11·중간9·낮음8·확인됨2 / 그래프 서브그래프 82노드 101엣지 / 검색 결과카드 10건
- 실데이터(`?src=real`): 대시보드 실측치 일치(자치법규 199,858 · 위임 421,627 · 그래프 250,416노드) / 지도 247개 매칭 / 지역 상세 정상 / 그래프는 크기 가드 작동
- `?nocdn=1`: 8개 화면 전부 대체 표 렌더 확인

## 11. 전국 shard 검증 기록 (2026-08-22)

전국 생성 — `python system/make_nationwide.py`

- **243곳 완주** (광역 16 + 기초 227), 755파일 · 39.2 MB · 410.3초 · **실패 0 · 경고 0**
- `index.json` 대조: `regions` 243개, level 분포 `{1:16, 2:227}`,
  `gap`/`peers`/`effectiveness` 3종이 다 있는 지역 **243/243** (누락 0)
- 최대 파일 `effectiveness/28177.json` 130.3 KB — **100MB 초과 0건**, `.tmp` 잔여 0건
- 확산 6종 제정본 커버리지: 맨발걷기 93.8% · 안전보안관 91.4% · 청년 87.2% ·
  반려동물 72.5% · 생리용품 44.4% · 자살예방 30.0%

브라우저 검증 — Playwright(Chromium) + `python -m http.server 8788`

- `?src=real` 9화면 전부 렌더, `.panel-error` 0건, **JS 예외 0건**
- 지역 선택기 = **243 opts / optgroup 16개** ·
  `전국 243곳 선택 가능 · 사전계산 243곳 (api/index.json)`
- 임의 3곳 실측 (전부 `데이터 소스: 전국 shard` 로 표기됨)

  | 지역 | 격차·유사 | 실효성 |
  |---|---|---|
  | 서울 강남구 `11680` | 보유 407종 · peer 12곳(1위 서초구 0.516) · 추천 25건 | 링크 809건 |
  | 전남광주 목포시 `12110` | **peer 0곳 — 재정지표 NULL 안내 출력**(2-A-5) | **링크 0건 — 예산 원자료 없음 안내 출력** |
  | 강원 춘천시 `51110` | 보유 508종 · peer 12곳(1위 진주시 0.598) · 추천 25건 · 폐지경고 8건 | 링크 846건 · 집행률 81.3% |

- 확산 선택기 6종, 표결 선택기 15건 — 항목 전환 시 해당 shard 로 재로드 확인
- 확산 커버리지 배지가 데이터 기반으로 동작: 자살예방 30.0% · 생리용품 44.4% 는
  "그대로 인용하면 안 된다" 경고 출력, 맨발걷기 93.8% 는 경고 없음
- 가상데이터(`?src=mock`) 9화면 회귀 — 전부 렌더, `.panel-error` 0건, JS 예외 0건
- `bash system/tools_audit_keys.sh` → **커밋 대상 키 노출 0건**(비밀키 7개).
  교차검증으로 `system/data/api/` 755파일을 실키 7개로 직접 grep → 0건,
  `OC=` 파라미터 잔존 0건 (`_sanitize_keys()` 정상 동작)

---

## 12. 전 자산 반영 검증 기록 (2026-08-22, 최종)

DB·엔진의 미반영분을 전부 화면에 올린 뒤의 실측이다.
전체 대조표는 [22 완성도 최종점검](../22_완성도_최종점검.md) 을 본다.

### 12-1. 생성

| 생성기 | 산출 | 결과 |
|---|---|---|
| `make_neural_fixtures.py` | `api/neural/` 645파일 11.55 MB | 재개 11.8s · 실패 0 |
| `make_analytics_fixtures.py` | `api/spatial/` 7 · `api/eha/` 3 · `api/community/` 1 · `api/peer_methods/` 228 | peer_methods 227곳 신규 180.8s · 오류 0 |
| `make_graph_fixtures.py` | `api/graph/` 422파일 16.24 MB | 축소 재생성 8.4s · 오류 0 경고 0 |
| `make_extend_fixtures.py` | search 40 · votes 149 · lifecycle 243 · statute 200 · verification · succession | 재개 3.7s · 실패 0 · 경고 1 |

**합계 2,688파일 · 87.82 MiB · 최대 파일 237.8 KB** (90 MB 상한 · 100 MB 파일 한계 모두 충족)

### 12-2. 브라우저 실측 (`python -m http.server` + `?src=real`)

- **14화면 전부 렌더** · `.panel-error` 0 · **JS 예외 0 · `console.error` 0** · `as_of_date` 14/14 노출
- 지도 `#/map` Leaflet path 250개, 공간분석 `#/spatial` 250개
- `#/graph?mode=ordinance` 노드 58 / 엣지 72, `?mode=statute&key=statute-276653` 표본 고지 출력
- `#/votes` 선택기 **149항목**, `#/search` 선택기 **40항목** (확장 카탈로그 병합 후)
- `#/analytics` 3탭 전부 렌더 — EHA 3템플릿 × 4모형, 커뮤니티 2 scope, 방법비교 227곳
- 임의 지역 3곳(제물포구 28125 · 부안군 52800 · 곡성군 12720) × 6화면 = **전부 오류 0**
- 가상데이터 `?src=mock` 14화면 회귀 — 오류 0, 실데이터 전용 화면은 안내 패널로 정상 강등

### 12-3. 이번에 잡은 결함

1. `api/votes/` 149건·`api/search/` 40건이 **화면에서 선택 불가**였다.
   로더가 `api/index.json`(표결 15·검색 5)만 읽고 확장 색인을 몰랐다 →
   `config.EXTRA_CATALOGS` + `api.js loadCatalog` 병합으로 수정.
2. `views/search.js` 가 shard 를 아예 안 쓰고 단일 `api/search.json` 1건만 그렸다 → 질의 선택기 추가.
3. `api/eha/`·`api/community/` 가 생성만 되고 소비되지 않았다 → `views/analytics.js` 신규.
4. `compare_peer_methods` 를 `row_factory` 없는 커넥션으로 호출하면 legacy 경로가 조용히 죽어
   3방식 비교가 2방식(동일 결과)으로 쪼그라든다 → `policymap.db.connect()` 사용 + 주석.
5. legacy 가 돌려주는 지자체명에 시도 접두어가 없어 동명 지자체가 구분되지 않았다 →
   `region_features` 로 표기 통일.

---

## 13. 전량화 검증 기록 (2026-08-23)

표본이던 자산을 DB 전량으로 바꾸고, 그 데이터가 실제로 화면에서 열리는지 확인한 기록이다.
자산별 3열 커버리지표(DB / 배포본 / 완전판)는 [22 완성도 최종점검 §1.7](../22_완성도_최종점검.md) 에 있다.

### 13-1. 생성·용량

| 항목 | 값(실측) |
|---|---|
| `api/` 총량 | **75.82 MB / 4,972 파일** (압축 전 405.6 MB, 5.2배) |
| 최대 파일 | `api/graph/instruments.json.gz` **443.0 KB** (100 MB 한계 대비 225배 여유) |
| 한 디렉터리 최대 파일 수 | `api/graph/ordinance/` 1,000개 |
| 무결성 | `.json.gz` 4,953 + `.json` 19 = **4,972개 전부 파싱 성공, 실패 0** |

배포 예산 250 MB 를 맞추려고 **버린 데이터는 없다.** 오히려 압축으로 생긴 여유로
신경망을 Top-5(53.1 MB) → **Top-10(87.7 MB, 저장본 전량)** 으로 다시 구웠다.

### 13-2. 브라우저 실측 (`viz/serve_full.py` + `?src=real&full=1`)

- **14화면 + 신규 2탭 + 상세 2라우트 = 18개 경로 전부 렌더** ·
  오류 패널 0 · **JS 예외 0 · `console.error` 0 · 실패 요청(HTTP≥400) 0**
- 임의 지역 3곳 — 정읍시 52180 / 괴산군 43760 / 은평구 11380.
  지역 상세의 «자치법규 전량»이 **845 / 656 / 602건**으로 DB 건수와 정확히 일치.
- 임의 조례 3건 — `ordin:1807445`(8조) · `ordin:1731477`(18조) · `ordin:1900303`(15조).
  세 건 모두 «완전판 · DB 직결» 배지 + 조문 수가 DB 와 일치.
- 신규 «지역 전량» 탭(법령 위계) — 247곳 선택기, 정읍시에서 조례 노드 545 / 상위법 647 /
  위임 엣지 1,494, 그리고 «845건 중 545건만 위임 근거 수집»이라는 결손 고지까지 출력.
- 신규 «지역 전량» 탭(신경망) — 266곳, 총계 노드 154,310 / 엣지 1,877,420,
  정읍시 698개 조례 각각의 Top-10 이웃 + 지자체끼리 유사도.
- 신규 «의안 전량»(국회 표결) — 버킷 207개 / 의안 19,847건, 검색·버킷 전환 동작,
  `has_votes` 의안의 «표결 보기»가 위 표결 화면으로 연결.
- `?full=0` 정적 폴백 — «배포본 · 정적 shard» 배지 + 조문 제목 8개(압축 shard 디코딩) +
  «본문은 `serve_full.py` 로» 안내. 배포본 경로가 깨지지 않는다.
- `?src=mock` 회귀 — 7화면 오류 0, **`.gz` 요청 0건**(가상데이터에는 압축 대상이 없다).

### 13-3. 이번에 잡은 결함

1. **`serve_full.py` 가 한국어 Windows 콘솔에서 기동 즉시 죽었다.** 시작 로그의 `—`(em dash)가
   cp949 로 인코딩되지 않아 `UnicodeEncodeError`. 로그를 파일로 리다이렉트해도 같았다 →
   `sys.stdout/stderr` 를 UTF-8 로 고정.
2. 신규 전량 자산 3종(`neural/by-region`·`graph/by-region`·`bill`)이 **생성만 되고 어느 화면에서도
   열 수 없었다**(21 MB 사장). → `views/neural.js`·`views/graph.js`·`views/votes.js` 에 탭·브라우저 추가.
3. 의안 목록에서 «표결 보기»를 눌렀을 때 표결은 바뀌는데 위쪽 드롭다운이 옛 의안을 가리킨 채
   남았다 → 선택기 `<select>` 를 직접 움직이도록 수정.
4. `neural/by-region` 번들의 `edge_fields` 가 `["src","dst","sim","rank"]` 인데 뷰가
   `src_index`·`cosine_sim` 를 찾아 이웃 표가 비었다 → 두 이름을 모두 받도록 수정.
