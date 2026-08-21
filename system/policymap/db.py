"""policymap.db — SQLite 연결·스키마 적용·upsert·watermark·change_log.

표준 sqlite3 만 사용. 이 모듈이 모든 모듈의 DB 접점(단일 진실원천).
CONTRACTS.md 의 "db.py API" 절이 이 파일의 공개 시그니처와 일치해야 한다.

공개 API (요약):
    connect(db_path=None) -> sqlite3.Connection
    init_db(conn=None, schema_path=None) -> sqlite3.Connection
    tx(conn)                              # 트랜잭션 컨텍스트매니저
    upsert(conn, table, row, pk, *, hash_col=None) -> str  # 'inserted'|'updated'|'unchanged'
    upsert_many(conn, table, rows, pk, *, hash_col=None) -> dict[str,int]
    get_watermark(conn, source, scope) -> dict | None
    set_watermark(conn, source, scope, **fields) -> None
    advance_cursor(conn, source, scope, cursor, *, last_hash=None, ...) -> None
    log_change(conn, **fields) -> str     # change_id 반환
    fetchone / fetchall / execute        # 얇은 헬퍼
    table_columns(conn, table) -> list[str]
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config as _config
from .util import now_kst_iso

# 컬럼 목록 캐시(테이블 스키마 인트로스펙션 최소화)
_COL_CACHE: dict[int, dict[str, list[str]]] = {}


# --------------------------------------------------------------------------- #
# 연결 / 스키마
# --------------------------------------------------------------------------- #
def connect(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """SQLite 연결 생성. Row factory·FK·WAL 설정. 부모 디렉터리 자동생성."""
    cfg = _config.get_config()
    path = Path(db_path) if db_path else Path(cfg.db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 120000")  # 동시 수집·분석 에이전트 다수 → 락 대기 여유
    return conn


def init_db(
    conn: Optional[sqlite3.Connection] = None,
    schema_path: Optional[str | Path] = None,
) -> sqlite3.Connection:
    """schema.sql 을 실행(executescript)해 테이블 생성. 멱등(IF NOT EXISTS).

    conn 미지정 시 새 연결 생성. 적용 후 동일 conn 반환.
    """
    cfg = _config.get_config()
    if conn is None:
        conn = connect()
    sql_path = Path(schema_path) if schema_path else Path(cfg.schema_path)
    ddl = sql_path.read_text(encoding="utf-8")
    conn.executescript(ddl)
    conn.commit()
    _COL_CACHE.pop(id(conn), None)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """트랜잭션 컨텍스트. 정상 종료 시 commit, 예외 시 rollback."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --------------------------------------------------------------------------- #
# 인트로스펙션
# --------------------------------------------------------------------------- #
def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """테이블 컬럼명 목록(캐시). row dict 필터링에 사용."""
    cache = _COL_CACHE.setdefault(id(conn), {})
    if table not in cache:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        cache[table] = [r["name"] for r in rows]
    return cache[table]


def _pk_list(pk: str | Iterable[str]) -> list[str]:
    return [pk] if isinstance(pk, str) else list(pk)


# --------------------------------------------------------------------------- #
# upsert (해시 가드 포함)
# --------------------------------------------------------------------------- #
def upsert(
    conn: sqlite3.Connection,
    table: str,
    row: dict[str, Any],
    pk: str | Iterable[str],
    *,
    hash_col: Optional[str] = None,
) -> str:
    """결정적 자연키 upsert. INSERT … ON CONFLICT(pk) DO UPDATE.

    row 에서 테이블에 실존하는 컬럼만 골라 사용(여분 키 무시).
    hash_col 지정 시 해시 가드: 기존 해시와 같으면 UPDATE 생략하고 'unchanged' 반환
    (명세[realtime] §3-2: 무변경=무이벤트, version 미증가).

    반환: 'inserted' | 'updated' | 'unchanged'
    """
    cols = set(table_columns(conn, table))
    data = {k: v for k, v in row.items() if k in cols}
    pks = _pk_list(pk)
    if not data or any(k not in data for k in pks):
        raise ValueError(f"upsert({table}): PK {pks} 값이 row에 없음")

    # 해시 가드
    if hash_col and hash_col in data:
        where = " AND ".join(f"{k}=?" for k in pks)
        existing = conn.execute(
            f"SELECT {hash_col} FROM {table} WHERE {where}",
            [data[k] for k in pks],
        ).fetchone()
        if existing is not None:
            if existing[hash_col] == data[hash_col]:
                # seen_at 성격 컬럼만 갱신하고 종료
                if "updated_at" in cols and "updated_at" not in data:
                    conn.execute(
                        f"UPDATE {table} SET updated_at=? WHERE {where}",
                        [now_kst_iso()] + [data[k] for k in pks],
                    )
                return "unchanged"
            action = "updated"
        else:
            action = "inserted"
    else:
        action = None  # 아래에서 확정

    keys = list(data.keys())
    placeholders = ", ".join("?" for _ in keys)
    col_list = ", ".join(keys)
    update_cols = [k for k in keys if k not in pks]
    conflict = ", ".join(pks)
    if update_cols:
        set_clause = ", ".join(f"{k}=excluded.{k}" for k in update_cols)
        sql = (f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
               f"ON CONFLICT({conflict}) DO UPDATE SET {set_clause}")
    else:
        sql = (f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
               f"ON CONFLICT({conflict}) DO NOTHING")

    if action is None:
        where = " AND ".join(f"{k}=?" for k in pks)
        pre = conn.execute(f"SELECT 1 FROM {table} WHERE {where}",
                           [data[k] for k in pks]).fetchone()
        action = "updated" if pre else "inserted"

    conn.execute(sql, [data[k] for k in keys])
    return action


def upsert_many(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[dict[str, Any]],
    pk: str | Iterable[str],
    *,
    hash_col: Optional[str] = None,
) -> dict[str, int]:
    """여러 행 upsert. 카운터 {inserted, updated, unchanged} 반환.

    단일 트랜잭션으로 감싸려면 호출부에서 with tx(conn): 로 래핑.
    """
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for row in rows:
        action = upsert(conn, table, row, pk, hash_col=hash_col)
        counts[action] += 1
    return counts


def soft_delete(
    conn: sqlite3.Connection,
    table: str,
    pk_values: dict[str, Any],
    *,
    status: str = "repealed",
    status_col: str = "status",
    valid_to_col: Optional[str] = None,
    valid_to: Optional[str] = None,
) -> None:
    """툼스톤 소프트삭제(하드삭제 금지 규율). status 전환 + 선택적 valid_to 설정."""
    cols = set(table_columns(conn, table))
    sets, params = [], []
    if status_col in cols:
        sets.append(f"{status_col}=?")
        params.append(status)
    if valid_to_col and valid_to_col in cols:
        sets.append(f"{valid_to_col}=?")
        params.append(valid_to or now_kst_iso())
    if not sets:
        return
    where = " AND ".join(f"{k}=?" for k in pk_values)
    conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE {where}",
                 params + list(pk_values.values()))


# --------------------------------------------------------------------------- #
# watermark
# --------------------------------------------------------------------------- #
def get_watermark(conn: sqlite3.Connection, source: str, scope: str) -> Optional[dict]:
    """(source, scope) 워터마크 행을 dict로. 없으면 None."""
    row = conn.execute(
        "SELECT * FROM watermarks WHERE source=? AND scope=?", (source, scope)
    ).fetchone()
    return dict(row) if row else None


def set_watermark(conn: sqlite3.Connection, source: str, scope: str, **fields: Any) -> None:
    """워터마크 upsert. 전달된 필드만 갱신. last_run 미지정 시 자동 채움."""
    row = {"source": source, "scope": scope, **fields}
    if "last_run" not in row:
        row["last_run"] = now_kst_iso()
    upsert(conn, "watermarks", row, ("source", "scope"))


def advance_cursor(
    conn: sqlite3.Connection,
    source: str,
    scope: str,
    cursor: str,
    *,
    last_hash: Optional[str] = None,
    status: str = "ok",
    changed: int = 0,
    rows_seen: int = 0,
    run_id: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    """파티션이 완전 영속화된 뒤에만 커서 전진(명세[realtime] §3-4 멱등성).

    호출부는 반드시 데이터 upsert 성공 후에 이 함수를 호출한다.
    """
    fields: dict[str, Any] = {
        "cursor": cursor,
        "status": status,
        "changed": changed,
        "rows_seen": rows_seen,
        "last_success": now_kst_iso(),
    }
    if last_hash is not None:
        fields["last_hash"] = last_hash
    if run_id is not None:
        fields["run_id"] = run_id
    if note is not None:
        fields["note"] = note
    set_watermark(conn, source, scope, **fields)


def mark_partition_status(
    conn: sqlite3.Connection, source: str, scope: str, status: str,
    *, note: Optional[str] = None, bump_retry: bool = False,
) -> None:
    """커서 미전진 상태 전환(partial/error/stale). 실패 파티션 격리용."""
    wm = get_watermark(conn, source, scope) or {}
    fields: dict[str, Any] = {"status": status}
    if note is not None:
        fields["note"] = note
    if bump_retry:
        fields["retry_count"] = int(wm.get("retry_count") or 0) + 1
    set_watermark(conn, source, scope, **fields)


# --------------------------------------------------------------------------- #
# change_log (change feed)
# --------------------------------------------------------------------------- #
def log_change(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    event: str,
    source: Optional[str] = None,
    scope: Optional[str] = None,
    entity_name: Optional[str] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    fields_changed: Optional[str] = None,
    region_code: Optional[str] = None,
    run_id: Optional[str] = None,
    official_url: Optional[str] = None,
    ts: Optional[str] = None,
) -> str:
    """변경 1건을 change_log 에 append. change_id(uuid) 반환.

    무변경(upsert 'unchanged')일 때는 호출하지 않는다(무변경=무이벤트).
    """
    change_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO change_log
           (change_id, ts, source, scope, entity_type, entity_id, entity_name,
            event, before, after, fields_changed, region_code, run_id, official_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (change_id, ts or now_kst_iso(), source, scope, entity_type, entity_id,
         entity_name, event, before, after, fields_changed, region_code,
         run_id, official_url),
    )
    return change_id


# --------------------------------------------------------------------------- #
# 얇은 조회 헬퍼
# --------------------------------------------------------------------------- #
def execute(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    return conn.execute(sql, tuple(params))


def fetchone(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def fetchall(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def count(conn: sqlite3.Connection, table: str, where: str = "", params: Iterable[Any] = ()) -> int:
    sql = f"SELECT COUNT(*) AS n FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(sql, tuple(params)).fetchone()["n"])


__all__ = [
    "connect", "init_db", "tx", "table_columns",
    "upsert", "upsert_many", "soft_delete",
    "get_watermark", "set_watermark", "advance_cursor", "mark_partition_status",
    "log_change", "execute", "fetchone", "fetchall", "count",
]
