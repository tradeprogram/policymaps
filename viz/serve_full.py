"""완전판(로컬) 서버 — 정적 shard 대신 DB(4.3GB)를 직접 읽어 100% 를 서빙한다.

배포본(Vercel 정적)에는 조문 본문 236만 건(약 490MB)이 들어가지 않는다. 이 서버는
같은 화면을 로컬에서 열되 상세 데이터를 SQLite 에서 직접 꺼내 준다.

    python viz/serve_full.py                     # http://127.0.0.1:8743/viz/public/?full=1
    python viz/serve_full.py --port 9000
    python viz/serve_full.py --no-warm           # RAG 인덱스 예열 생략(첫 검색이 느려짐)

serve_ai.py 의 Handler 를 상속한다 — 정적 서빙과 /ai/chat(Gemini 프록시)이 그대로 동작하고,
여기서는 GET /api/db/* 만 얹는다. DB 는 읽기 전용(mode=ro)으로 연다.

엔드포인트 (모두 GET, 응답은 MCP 봉투 {data, as_of_date, stale, execution_allowed, disclaimer})
    /api/db/status                      DB 존재·테이블 행수·RAG 인덱스 상태
    /api/db/ordinance/{id}              조례 상세 + 조문 본문 전량(+분류·예산연결·연혁·변경이력)
    /api/db/search?q=&k=&mode=          조문 전문검색(RAG 하이브리드) / mode=name 은 조례명 LIKE
    /api/db/graph/{id}                  임의 조례 서브그래프(사전계산 불필요)
    /api/db/neural/{id}?k=&model=       임의 조례 신경망 유사
    /api/db/statute/{id}                법령 상세 + 조문 본문 + 하위 조례
    /api/db/ordinances?sig_cd=&q=       지역/이름으로 조례 목록(상세로 들어가는 입구)

로컬 전용이다. 기본 바인딩은 127.0.0.1 이고 DB 는 읽기 전용이며 쓰기 경로가 없다.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse, parse_qs

VIZ_DIR = Path(__file__).resolve().parent
ROOT = VIZ_DIR.parent
SYSTEM_DIR = ROOT / "system"

# serve_ai(정적 서빙 + Gemini 프록시)와 policymap 패키지를 그대로 재사용한다.
sys.path.insert(0, str(VIZ_DIR))
sys.path.insert(0, str(SYSTEM_DIR))

import serve_ai  # noqa: E402  (viz/serve_ai.py)

from policymap import db as D  # noqa: E402
from policymap.config import get_config  # noqa: E402
from policymap.mcp_server.server import Server, ToolError  # noqa: E402

# 키 살균 규율은 기존 생성기의 정규식을 그대로 쓴다(중복 구현 금지).
from make_more_fixtures import _URL_KEY_RE  # noqa: E402

# 한국어 Windows 콘솔은 기본이 cp949 라 '—' 같은 문자에서 UnicodeEncodeError 로 죽는다.
# 로그를 리다이렉트해도 같은 일이 난다. 출력 스트림을 UTF-8 로 고정해 둔다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 파이프·테스트 더블 등 reconfigure 없는 스트림
        pass

API_PREFIX = "/api/db"

# 상태 조회에 행수를 실을 테이블(전체 31개 중 화면이 쓰는 것들 + 규모 큰 것)
STATUS_TABLES = [
    "ordinances", "ordinance_articles", "articles", "legal_instrument",
    "delegations", "instrument_relations", "neural_similarity", "node_embeddings",
    "regions", "budget_lines", "ordinance_budget_link", "ordinance_category",
    "bills", "votes", "legislators", "change_log", "verification",
]


# --------------------------------------------------------------------------- #
# DB / 엔진 접속 (스레드별 연결, 무거운 인덱스는 공유)
# --------------------------------------------------------------------------- #
class Engine:
    """읽기 전용 DB 접속과 MCP 엔진(Server)을 스레드별로 들고 있는 홀더."""

    def __init__(self, db_path: str | None = None):
        self.cfg = get_config()
        self.db_path = str(Path(db_path or self.cfg.db_path).resolve())
        self._local = threading.local()
        self._graph_lock = threading.Lock()
        self._graph = None          # (resolver, degrees) — 만드는 데 수 초 걸려 공유한다
        self.rag_state = {"ready": False, "error": None, "seconds": None, "warming": False}
        # 테이블 행수는 COUNT(*) 17회다(ordinance_articles 236만 행 포함). DB 는 읽기
        # 전용이라 값이 변하지 않으므로 한 번만 세고 재사용한다. 프론트가 페이지마다
        # /status 를 부르는데 매번 전수 카운트를 돌리면 첫 렌더가 눈에 띄게 느려진다.
        self._counts: dict[str, int] | None = None
        self._counts_lock = threading.Lock()

    # ---- 존재 여부 ----
    def db_exists(self) -> bool:
        return Path(self.db_path).exists()

    def db_bytes(self) -> int:
        try:
            return Path(self.db_path).stat().st_size
        except OSError:
            return 0

    # ---- 스레드별 연결 ----
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            uri = f"file:{Path(self.db_path).as_posix()}?mode=ro"
            c = sqlite3.connect(uri, uri=True, timeout=30)
            c.row_factory = sqlite3.Row
            # 읽기 전용이라 journal_mode 는 건드리지 않는다(WAL 파일은 그대로 읽힌다).
            c.execute("PRAGMA busy_timeout = 30000")
            self._local.conn = c
        return c

    def server(self) -> Server:
        s = getattr(self._local, "srv", None)
        if s is None:
            s = Server(self.conn(), self.cfg)
            self._local.srv = s
        return s

    def table_counts(self) -> dict[str, int]:
        """STATUS_TABLES 행수. 읽기 전용 DB 라 한 번만 세고 캐시한다."""
        with self._counts_lock:
            if self._counts is None:
                conn = self.conn()
                out: dict[str, int] = {}
                for t in STATUS_TABLES:
                    try:
                        out[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                    except sqlite3.Error:
                        out[t] = -1
                self._counts = out
            return dict(self._counts)

    # ---- 그래프 보조 인덱스(Resolver/Degrees) — make_graph_fixtures 재사용 ----
    def graph_ctx(self):
        with self._graph_lock:
            if self._graph is None:
                import make_graph_fixtures as G  # noqa: PLC0415  (지연 로드: 수 초 걸린다)
                conn = self.conn()
                resolver = G.Resolver(conn)
                degrees = G.Degrees(conn, resolver)
                self._graph = (G, resolver, degrees)
            return self._graph

    # ---- RAG 인덱스 예열 ----
    def rag_index_dir(self) -> Path:
        override = os.environ.get("POLICYMAP_RAG_INDEX_DIR")
        if override:
            return Path(override)
        return Path(self.cfg.out_dir) / "index"

    def rag_meta(self) -> dict | None:
        meta = self.rag_index_dir() / "all" / "meta.json"
        if not meta.exists():
            return None
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            return None

    def warm_rag(self) -> None:
        """RAG 인덱스를 미리 로드한다. 첫 질의에서 2분 넘게 멈추는 것을 막는다."""
        self.table_counts()   # /status 의 COUNT(*) 17회도 시작 시점에 치른다
        if self.rag_meta() is None:
            self.rag_state["error"] = "인덱스 없음 — SQL LIKE 폴백으로 동작"
            return
        self.rag_state["warming"] = True
        t0 = time.time()
        try:
            from policymap.rag import load_index  # noqa: PLC0415
            idx = load_index("all", index_dir=str(self.rag_index_dir()))
            self.rag_state["stats"] = idx.stats()
            # load_index 는 meta 만 읽는다. 실제 비용은 세그먼트 10개의 term 사전을
            # 처음 쓸 때 치른다(실측: 첫 질의 151초 → 이후 2초). 그래서 여기서
            # 버리는 질의를 한 번 돌려 그 비용을 시작 시점으로 옮긴다.
            idx.bm25_search("예산 지원 조례", k=1)
            idx.dense_search("예산 지원 조례", k=1)
            self.rag_state.update(ready=True, seconds=round(time.time() - t0, 1))
        except Exception as exc:  # noqa: BLE001 - 예열 실패는 폴백으로 흡수한다
            self.rag_state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            self.rag_state["warming"] = False


ENGINE: Engine | None = None


# --------------------------------------------------------------------------- #
# 라우팅
# --------------------------------------------------------------------------- #
class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        n = int(str(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


def _flag(v, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).lower() not in ("0", "false", "no", "off", "")


# DB 의 official_url 상당수가 '/DRF/lawService.do?…' 상대경로다(실측: legal_instrument
# 29,811건 중 29,746건, ordinances 199,858건 중 40,406건 — 전부 폐지본). 그대로 내보내면
# 브라우저가 이 서버(127.0.0.1)로 붙여 버려 링크가 깨진다. make_full_ordinance.py 가
# 정적 번들에서 쓴 것과 같은 호스트로 절대화한다.
LAW_HOST = "https://www.law.go.kr"


def _fix_url(v: str) -> str:
    v = _URL_KEY_RE.sub(lambda m: m.group(1), v)      # API 키 제거(살균 규율)
    if v.startswith("/"):
        v = LAW_HOST + v                              # 상대경로 → 절대 URL
    return v


def sanitize_urls(obj):
    """URL 필드만 손본다 — API 키 제거 + 상대경로 절대화.

    make_more_fixtures._sanitize_keys 는 모든 문자열에 `?`/`&` 꼬리 정리까지 하는데,
    이 서버는 조문 본문 원문을 그대로 내보내야 하므로 본문을 건드리면 안 된다
    (물음표로 끝나는 조문이 있다). 그래서 키 이름에 url 이 들어간 값만 다룬다.
    """
    if isinstance(obj, list):
        return [sanitize_urls(v) for v in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and "url" in str(k).lower():
                out[k] = _fix_url(v)
            else:
                out[k] = sanitize_urls(v)
        return out
    return obj


def _norm_ordinance_id(ident: str) -> str:
    """'ordin:123' / '123' / 'ordin%3A123' / 그래프 노드 id 'ordinance:ordin:123' 모두 수용."""
    s = unquote(str(ident)).strip()
    if s.startswith("ordinance:"):     # graph.build 의 노드 id 는 라벨 접두가 하나 더 붙는다
        s = s[len("ordinance:"):]
    if s.startswith("ordin:"):
        return s
    if s.isdigit():
        return f"ordin:{s}"
    return s


def route(engine: Engine, path: str, q: dict) -> dict:
    """/api/db/... 를 처리해 봉투(dict)를 돌려준다. 실패는 HttpError."""
    rest = path[len(API_PREFIX):].strip("/")
    if not rest:
        rest = "status"
    head, _, tail = rest.partition("/")
    tail = tail.strip("/")

    if head == "status":
        return handle_status(engine)
    if not engine.db_exists():
        raise HttpError(503, f"DB 파일이 없습니다: {engine.db_path}")

    if head == "ordinance":
        if not tail:
            raise HttpError(400, "ordinance_id 가 필요합니다: /api/db/ordinance/{id}")
        return handle_ordinance(engine, tail, q)
    if head == "ordinances":
        return handle_ordinance_list(engine, q)
    if head == "search":
        return handle_search(engine, q)
    if head == "graph":
        if not tail:
            raise HttpError(400, "ordinance_id 가 필요합니다: /api/db/graph/{id}")
        return handle_graph(engine, tail, q)
    if head == "neural":
        if not tail:
            raise HttpError(400, "ordinance_id 가 필요합니다: /api/db/neural/{id}")
        return handle_neural(engine, tail, q)
    if head == "statute":
        if not tail:
            raise HttpError(400, "instrument_id 가 필요합니다: /api/db/statute/{id}")
        return handle_statute(engine, tail, q)
    raise HttpError(404, f"알 수 없는 엔드포인트: {API_PREFIX}/{rest}")


# --------------------------------------------------------------------------- #
# 1) /api/db/status
# --------------------------------------------------------------------------- #
def handle_status(engine: Engine) -> dict:
    exists = engine.db_exists()
    payload = {
        "full_edition": exists,
        "db_path": engine.db_path,
        "db_exists": exists,
        "db_bytes": engine.db_bytes(),
        "endpoints": [
            f"{API_PREFIX}/status",
            f"{API_PREFIX}/ordinance/{{ordinance_id}}",
            f"{API_PREFIX}/ordinances?sig_cd=&q=&limit=",
            f"{API_PREFIX}/search?q=&k=&mode=semantic|name",
            f"{API_PREFIX}/graph/{{ordinance_id}}",
            f"{API_PREFIX}/neural/{{ordinance_id}}?k=&model=",
            f"{API_PREFIX}/statute/{{instrument_id}}",
        ],
    }
    if not exists:
        payload["note"] = "DB 가 없어 완전판을 쓸 수 없습니다. 화면은 정적 shard 로 동작합니다."
        return {"data": payload, "as_of_date": None, "stale": True,
                "execution_allowed": False,
                "disclaimer": "이 응답은 참고용이며 법적 판단 근거가 아니다."}

    payload["tables"] = tables = engine.table_counts()

    meta = engine.rag_meta()
    payload["rag"] = {
        "index_dir": str(engine.rag_index_dir()),
        "exists": meta is not None,
        "n_docs": (meta or {}).get("n_docs"),
        "segments": len((meta or {}).get("segments") or []),
        "built_at": (meta or {}).get("built_at"),
        "model": (meta or {}).get("model"),
        **engine.rag_state,
    }
    payload["coverage"] = {
        "ordinance_article_bodies": tables.get("ordinance_articles"),
        "statute_article_bodies": tables.get("articles"),
        "note": "배포본(정적 shard)에는 조문 본문이 없다. 이 서버만 본문을 제공한다.",
    }
    payload["graph_ready"] = engine._graph is not None
    return engine.server()._envelope(payload)


# --------------------------------------------------------------------------- #
# 2) /api/db/ordinance/{id} — 조문 본문 전량
# --------------------------------------------------------------------------- #
def handle_ordinance(engine: Engine, ident: str, q: dict) -> dict:
    srv = engine.server()
    oid = _norm_ordinance_id(ident)
    include_articles = _flag((q.get("articles") or [None])[0], True)
    try:
        env = srv._tool_get_ordinance({"ordinance_id": oid,
                                       "include_articles": include_articles})
    except ToolError as exc:
        raise HttpError(404, str(exc)) from exc

    data = env["data"]
    real_id = (data.get("ordinance") or {}).get("ordinance_id") or oid
    conn = engine.conn()

    # MCP tool 이 담지 않는 것들을 얹어 '진짜 100%' 를 만든다.
    data["categories"] = D.fetchall(
        conn,
        """SELECT oc.category_code, c.name AS category_name, oc.confidence, oc.method
           FROM ordinance_category oc LEFT JOIN categories c ON c.code = oc.category_code
           WHERE oc.ordinance_id = ? ORDER BY oc.confidence DESC""",
        (real_id,))
    data["appendices"] = D.fetchall(
        conn,
        """SELECT appendix_id, appendix_no, appendix_branch, title, appendix_kind,
                  file_name, file_url, body
           FROM ordinance_appendix WHERE ordinance_id = ? ORDER BY appendix_no""",
        (real_id,))
    data["budget_links"] = D.fetchall(
        conn,
        """SELECT obl.budget_id, obl.match_method, obl.confidence, obl.verified,
                  obl.category_gate, obl.source_fyr,
                  bl.fyr, bl.dbiz_nm, bl.field, bl.sector,
                  bl.budget_now, bl.exe_amt
           FROM ordinance_budget_link obl
           LEFT JOIN budget_lines bl ON bl.budget_id = obl.budget_id
           WHERE obl.ordinance_id = ?
           ORDER BY obl.verified DESC, obl.confidence DESC LIMIT 200""",
        (real_id,))
    data["budget_link_count"] = int(conn.execute(
        "SELECT COUNT(*) FROM ordinance_budget_link WHERE ordinance_id=?", (real_id,)
    ).fetchone()[0])
    data["neural_similar"] = D.fetchall(
        conn,
        """SELECT ns.dst_id, ns.model_name, ns.cosine_sim, ns.rank,
                  o.name AS dst_name, o.org_name AS dst_org, o.status AS dst_status
           FROM neural_similarity ns
           LEFT JOIN ordinances o ON o.ordinance_id = ns.dst_id
           WHERE ns.src_id = ? ORDER BY ns.model_name, ns.rank LIMIT 60""",
        (real_id,))
    work_id = (data.get("ordinance") or {}).get("work_id")
    data["versions"] = D.fetchall(
        conn,
        """SELECT ordinance_id, version_no, name, enacted_on, effective_on, repealed_on,
                  status, lifecycle, rr_cls_cd
           FROM ordinances WHERE work_id = ? ORDER BY (version_no IS NULL), version_no""",
        (work_id,)) if work_id else []
    data["change_log"] = D.fetchall(
        conn,
        """SELECT ts, source, event, fields_changed, official_url
           FROM change_log WHERE entity_type='ordinance' AND entity_id=?
           ORDER BY ts DESC LIMIT 50""",
        (real_id,))
    arts = data.get("articles") or []
    data["article_body_chars"] = sum(len(a.get("body") or "") for a in arts)
    data["articles_with_body"] = sum(1 for a in arts if (a.get("body") or "").strip())
    data["_source"] = "db-direct"
    return env


# --------------------------------------------------------------------------- #
# 3) /api/db/ordinances — 목록(상세로 들어가는 입구)
# --------------------------------------------------------------------------- #
def handle_ordinance_list(engine: Engine, q: dict) -> dict:
    srv = engine.server()
    args = {
        "query": (q.get("q") or q.get("query") or [None])[0],
        "sig_cd": (q.get("sig_cd") or [None])[0],
        "region_id": (q.get("region_id") or [None])[0],
        "ord_kind": (q.get("ord_kind") or [None])[0],
        "status": (q.get("status") or ["all"])[0],
        "limit": _int((q.get("limit") or [50])[0], 50, 1, 200),
    }
    try:
        env = srv._tool_search_ordinance(args)
    except ToolError as exc:
        raise HttpError(400, str(exc)) from exc
    env["data"]["_source"] = "db-direct"
    return env


# --------------------------------------------------------------------------- #
# 4) /api/db/search — 임의 질의 전문검색
# --------------------------------------------------------------------------- #
def handle_search(engine: Engine, q: dict) -> dict:
    query = (q.get("q") or q.get("query") or [""])[0].strip()
    if not query:
        raise HttpError(400, "q(질의)가 필요합니다: /api/db/search?q=반려동물")
    k = _int((q.get("k") or [10])[0], 10, 1, 50)
    mode = ((q.get("mode") or ["semantic"])[0] or "semantic").lower()
    srv = engine.server()

    if mode in ("name", "like", "sql"):
        try:
            env = srv._tool_search_ordinance({"query": query, "limit": k, "status": "all"})
        except ToolError as exc:
            raise HttpError(400, str(exc)) from exc
        env["data"]["mode"] = "name"
        env["data"]["_source"] = "db-direct"
        return env

    args = {
        "query": query, "k": k,
        "scope": (q.get("scope") or ["all"])[0] or "all",
        "hops": _int((q.get("hops") or [1])[0], 1, 0, 2),
        "use_graph": _flag((q.get("graph") or [None])[0], True),
        "with_text": _flag((q.get("text") or [None])[0], True),
    }
    t0 = time.time()
    try:
        env = srv._tool_semantic_search_ordinance(args)
    except ToolError as exc:
        raise HttpError(400, str(exc)) from exc
    data = env["data"]
    data["mode"] = "semantic"
    data["elapsed_sec"] = round(time.time() - t0, 2)
    data["_source"] = "db-direct"
    _enrich_hits(engine, data.get("results") or [])
    return env


def _enrich_hits(engine: Engine, hits: list) -> None:
    """검색 히트에 조례명/지자체명을 채운다.

    RAG 히트는 name/org_name 은 주지만 parent_name·region_name·sig_cd 가 없다.
    화면(views/search.js)이 parent_name 을 쓰고, 상세로 넘어가려면 sig_cd 가 필요하다.
    """
    conn = engine.conn()
    for h in hits:
        pid = h.get("id") or h.get("parent_id")
        if not pid:
            continue
        h.setdefault("parent_id", pid)
        if h.get("name") and not h.get("parent_name"):
            h["parent_name"] = h["name"]
        if h.get("parent_name") and h.get("region_name"):
            continue
        if str(pid).startswith("ordin:"):
            row = D.fetchone(
                conn,
                """SELECT o.name, o.org_name, o.region_id, o.status, o.official_url,
                          o.verification_status, o.enacted_on, o.article_count,
                          r.sig_cd, r.full_name AS region_name
                   FROM ordinances o LEFT JOIN regions r ON r.region_id = o.region_id
                   WHERE o.ordinance_id = ?""", (pid,))
        else:
            row = D.fetchone(
                conn,
                """SELECT name, competent_authority AS org_name, NULL AS region_id, status,
                          official_url, verification_status, enacted_on,
                          NULL AS article_count, NULL AS sig_cd, NULL AS region_name
                   FROM legal_instrument WHERE instrument_id = ?""", (pid,))
        if not row:
            continue
        for key in ("name", "org_name", "region_id", "status", "official_url",
                    "verification_status", "enacted_on", "article_count",
                    "sig_cd", "region_name"):
            if row.get(key) is not None and not h.get(key):
                h[key] = row[key]
        if not h.get("parent_name"):
            h["parent_name"] = row.get("name")


# --------------------------------------------------------------------------- #
# 5) /api/db/graph/{id} — 임의 조례 서브그래프
# --------------------------------------------------------------------------- #
def handle_graph(engine: Engine, ident: str, q: dict) -> dict:
    oid = _norm_ordinance_id(ident)
    conn = engine.conn()
    seed = D.fetchone(
        conn,
        """SELECT o.ordinance_id, o.name, o.region_id, o.org_name, o.ord_kind, o.status,
                  r.full_name AS region_name, r.level AS region_level, r.sig_cd
           FROM ordinances o LEFT JOIN regions r ON r.region_id = o.region_id
           WHERE o.ordinance_id = ?""", (oid,))
    if not seed:
        raise HttpError(404, f"조례를 찾을 수 없음: {oid}")

    args = SimpleNamespace(
        max_nodes=_int((q.get("max_nodes") or [80])[0], 80, 10, 400),
        max_parents=_int((q.get("max_parents") or [20])[0], 20, 1, 100),
        max_cited=_int((q.get("max_cited") or [20])[0], 20, 0, 100),
        hub_parents=_int((q.get("hub_parents") or [4])[0], 4, 0, 20),
        peer_pool=_int((q.get("peer_pool") or [600])[0], 600, 10, 3000),
    )
    G, resolver, degrees = engine.graph_ctx()
    with engine._graph_lock:   # Degrees._peer_cache 는 공유 상태다
        env = G.ordinance_subgraph(conn, resolver, degrees, seed, args)
    if isinstance(env, dict) and "data" in env:
        env["data"]["_source"] = "db-direct"
        return env
    return engine.server()._envelope({**(env or {}), "_source": "db-direct"})


# --------------------------------------------------------------------------- #
# 6) /api/db/neural/{id}
# --------------------------------------------------------------------------- #
def handle_neural(engine: Engine, ident: str, q: dict) -> dict:
    srv = engine.server()
    args = {
        "ordinance_id": _norm_ordinance_id(ident),
        "k": _int((q.get("k") or [10])[0], 10, 1, 50),
    }
    model = (q.get("model") or [None])[0]
    if model:
        args["model"] = model
    try:
        env = srv._tool_similar_ordinances(args)
    except ToolError as exc:
        raise HttpError(404, str(exc)) from exc
    env["data"]["available_models"] = srv._neural_models()
    env["data"]["_source"] = "db-direct"
    return env


# --------------------------------------------------------------------------- #
# 7) /api/db/statute/{id} — 법령 상세 + 조문 본문
# --------------------------------------------------------------------------- #
def handle_statute(engine: Engine, ident: str, q: dict) -> dict:
    conn = engine.conn()
    iid = unquote(str(ident)).strip()
    row = D.fetchone(
        conn,
        """SELECT li.*, ik.national_tier AS kind_tier, ik.local_tier AS kind_local_tier,
                  ik.note AS kind_note
           FROM legal_instrument li LEFT JOIN instrument_kind ik ON ik.kind = li.kind
           WHERE li.instrument_id = ?""", (iid,))
    if row is None and iid.isdigit():
        row = D.fetchone(conn, "SELECT * FROM legal_instrument WHERE mst = ?", (iid,))
    if row is None:
        row = D.fetchone(
            conn,
            "SELECT * FROM legal_instrument WHERE name = ? "
            "ORDER BY (current_history='현행') DESC LIMIT 1", (iid,))
    if row is None:
        raise HttpError(404, f"법령을 찾을 수 없음: {iid}")
    real_id = row["instrument_id"]

    articles = D.fetchall(
        conn,
        """SELECT article_id, article_no, article_branch, title, body, effective_on
           FROM articles WHERE instrument_id = ?
           ORDER BY CAST(article_no AS INTEGER), (article_branch IS NULL), article_branch""",
        (real_id,)) if _flag((q.get("articles") or [None])[0], True) else []

    # 위임관계는 parent_id 가 instrument_id 이거나 'lawname:{정규화명}' 이다(둘 다 센다).
    # 정규화 규칙은 make_graph_fixtures.norm_name 그대로 재사용한다.
    alias_keys = [real_id]
    try:
        from make_graph_fixtures import norm_name  # noqa: PLC0415
        nk = norm_name(row.get("name"))
        if nk:
            alias_keys.append(f"lawname:{nk}")
    except Exception:  # noqa: BLE001 - 별칭 없이도 동작해야 한다
        pass
    ph = ",".join("?" for _ in alias_keys)
    child_total = int(conn.execute(
        f"SELECT COUNT(DISTINCT child_id) FROM delegations "
        f"WHERE child_kind='ordinance' AND parent_id IN ({ph})", alias_keys).fetchone()[0])
    children = D.fetchall(
        conn,
        f"""SELECT d.child_id AS ordinance_id, d.child_article, d.parent_article,
                   d.relation, d.delegation_type, d.source_path, d.verification_status,
                   o.name, o.org_name, o.region_id, o.status, o.enacted_on
            FROM delegations d JOIN ordinances o ON o.ordinance_id = d.child_id
            WHERE d.child_kind='ordinance' AND d.parent_id IN ({ph})
            GROUP BY d.child_id
            ORDER BY (o.status='active') DESC, o.region_id LIMIT ?""",
        [*alias_keys, _int((q.get("children") or [100])[0], 100, 1, 1000)])

    payload = {
        "instrument": row,
        "article_count": len(articles),
        "articles": articles,
        "article_body_chars": sum(len(a.get("body") or "") for a in articles),
        "child_ordinance_total": child_total,
        "child_ordinances": children,
        "alias_keys": alias_keys,
        "cites_from": D.fetchall(
            conn,
            """SELECT ir.src_kind, ir.src_id, ir.relation, ir.citation_text, ir.src_article
               FROM instrument_relations ir
               WHERE ir.dst_id = ? AND ir.relation IN ('CITES','INCORPORATES_STANDARD')
               LIMIT 100""", (real_id,)),
        "relations": D.fetchall(
            conn,
            """SELECT rel_id, dst_kind, dst_id, relation, citation_text, effective_on
               FROM instrument_relations WHERE src_id = ? LIMIT 200""", (real_id,)),
        "verification": D.fetchone(
            conn, "SELECT * FROM verification WHERE entity_type='instrument' AND entity_id=?",
            (real_id,)),
        "_source": "db-direct",
    }
    return engine.server()._envelope(payload, official_url=row.get("official_url"))


# --------------------------------------------------------------------------- #
# HTTP 핸들러
# --------------------------------------------------------------------------- #
class FullHandler(serve_ai.Handler):
    """serve_ai.Handler(정적 서빙 + /ai/chat) 위에 GET /api/db/* 를 얹는다."""

    server_version = "policymap-full/1.0"

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 규약)
        parsed = urlparse(self.path)
        if parsed.path == API_PREFIX or parsed.path.startswith(API_PREFIX + "/"):
            self._serve_api(parsed.path, parse_qs(parsed.query))
            return
        super().do_GET()

    def do_OPTIONS(self) -> None:  # noqa: N802
        # 별도 포트로 띄운 정적 서버에서 이 API 를 부를 수 있게 한다(로컬 전용).
        self.send_response(204)
        self._cors()
        self.send_header("content-length", "0")
        self.end_headers()

    def _cors(self) -> None:
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "content-type")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")

    def _serve_api(self, path: str, query: dict) -> None:
        assert ENGINE is not None
        try:
            env = route(ENGINE, path, query)
            self.send_json(200, sanitize_urls(env))
        except HttpError as exc:
            self.send_json(exc.status, {"error": exc.message, "path": path,
                                        "status": exc.status})
        except Exception as exc:  # noqa: BLE001 - 로컬 서버는 실패를 JSON 으로 드러낸다
            traceback.print_exc()
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}", "path": path,
                                 "traceback": traceback.format_exc()[-2000:]})

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self._cors()
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # 조용한 한 줄 로그
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    global ENGINE
    ap = argparse.ArgumentParser(description="완전판(로컬 DB 직결) 서버")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8743")))
    ap.add_argument("--db", default=None, help="policymap.db 경로(기본 system/data/policymap.db)")
    ap.add_argument("--no-warm", action="store_true",
                    help="RAG 인덱스 예열 생략(첫 전문검색이 수 분 걸릴 수 있다)")
    args = ap.parse_args()

    serve_ai.load_env_files()
    ENGINE = Engine(args.db)
    os.chdir(ROOT)

    print(f"DB: {ENGINE.db_path} ({ENGINE.db_bytes() / 1e9:.2f} GB, "
          f"{'존재' if ENGINE.db_exists() else '없음 — 정적 폴백만 가능'})")
    if not args.no_warm and ENGINE.db_exists():
        threading.Thread(target=ENGINE.warm_rag, daemon=True,
                         name="rag-warm").start()
        print("RAG 인덱스 예열 시작(백그라운드) — 완료 전 검색은 느립니다. "
              f"{API_PREFIX}/status 의 rag.ready 로 확인하세요.")

    httpd = ThreadingHTTPServer((args.host, args.port), FullHandler)
    base = f"http://{args.host}:{args.port}"
    print(f"완전판 화면: {base}/viz/public/index.html?full=1")
    print(f"API:        {base}{API_PREFIX}/status")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
