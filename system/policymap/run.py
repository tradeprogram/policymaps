"""policymap.run — 오케스트레이션 CLI 엔트리포인트.

pyproject `[project.scripts] policymap = "policymap.run:main"` 및
`python -m policymap.run {init|collect|parse|build|export|refresh|mcp|seed|status|
neural|index|search}` 로 참조된다.
refresh.yml 은 `python -m policymap.run init` / `refresh --sources … --concurrency … --retries …
--error-budget …` / `export --out data/` 를 호출하고, refresh 는 GITHUB_OUTPUT 에
changed_count / error_count / summary_path 를 기록한다(명세[realtime] §8).

역할(CONTRACTS.md §5): 소스별 collector 파티션 병렬 실행 + 순서 강제 + 오류예산 판정 +
변경로그 집계 + 워터마크 스냅숏 커밋백. 이 파일은 오케스트레이터이므로 collectors/parsers/
graph/mcp_server 의 실제 로직을 재구현하지 않고 CONTRACTS.md 에 규정된 고수준 함수를
"지연 import + graceful degradation" 으로 호출한다. 대상 모듈이 아직 없더라도(병렬 빌드 중)
run.py 자체는 표준라이브러리만으로 컴파일·기동되며, 각 단계는 'unavailable' 로 보고된다.

설계 규율 승계:
  * 파티션 격리(명세[realtime] §4): 한 파티션 실패가 전체 런을 중단시키지 않는다.
  * 성공 파티션만 커서 전진 — 커서 전진은 collector 가 파티션 완전 영속화 후 수행하고,
    run.py 는 실패 파티션을 mark_partition_status('error') 로 격리한다.
  * 오류예산: error_count > max(10, ceil(파티션수 * error_budget)) 일 때만 런 실패.
  * NOT_FOUND/빈결과 = 정상적 없음 → 오류로 세지 않는다(util.NotFound).
  * 소스 실행 순서: boundary(지역 스파인) → law → ordin → na_bill → na_vote → budget.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import config as _config
from . import db as _db
from . import util as _util

log = _util.get_logger("policymap.run")

# --------------------------------------------------------------------------- #
# 소스·별칭 규약
# --------------------------------------------------------------------------- #
# 실행 순서(의존성): 지역 스파인 먼저 → 법령/조례 → 국회 → 예산.
SOURCE_ORDER: tuple[str, ...] = ("boundary", "law", "ordin", "na_bill", "na_vote", "budget")
VALID_SOURCES = set(SOURCE_ORDER)

# refresh --sources 토큰 확장(별칭만; 6개 granular 이름은 그대로).
REFRESH_ALIASES: dict[str, tuple[str, ...]] = {
    "geo": ("boundary",),
    "assembly": ("na_bill", "na_vote"),
    "all": SOURCE_ORDER,
}
# collect <domain> 도메인 → granular 소스 매핑(task 명세: law|assembly|geo|budget|all).
COLLECT_DOMAINS: dict[str, tuple[str, ...]] = {
    "law": ("law", "ordin"),
    "assembly": ("na_bill", "na_vote"),
    "geo": ("boundary",),
    "budget": ("budget",),
    "all": SOURCE_ORDER,
}

# collectors 모듈 경로(지연 import).
_COLLECTOR_MODULES = {
    "law": "policymap.collectors.law",
    "assembly": "policymap.collectors.assembly",
    "geo": "policymap.collectors.geo",
    "budget": "policymap.collectors.budget",
}

DEFAULT_AGES: tuple[int, ...] = (22,)  # 현재 대수(2024~2028). --ages 로 override.


# --------------------------------------------------------------------------- #
# 지연 import (graceful degradation)
# --------------------------------------------------------------------------- #
class StepUnavailable(Exception):
    """대상 모듈/함수가 아직 구현되지 않음(병렬 빌드 중). 오류예산에서 제외."""


def _resolve(module_path: str, attr: str) -> Callable:
    """모듈을 지연 import 하고 함수를 반환. 부재 시 StepUnavailable."""
    try:
        mod = importlib.import_module(module_path)
    except ImportError as exc:
        raise StepUnavailable(f"{module_path} 미구현: {exc}") from exc
    fn = getattr(mod, attr, None)
    if not callable(fn):
        raise StepUnavailable(f"{module_path}.{attr} 부재")
    return fn


def _collector(source_family: str, attr: str) -> Callable:
    return _resolve(_COLLECTOR_MODULES[source_family], attr)


# --------------------------------------------------------------------------- #
# 실행 옵션 / 결과 구조
# --------------------------------------------------------------------------- #
@dataclass
class RunOpts:
    sources: tuple[str, ...] = ()
    reorg_event: Optional[str] = None
    out_dir: str = ""
    state_path: str = ""
    concurrency: int = 6
    retries: int = 3
    error_budget: float = 0.05
    ages: tuple[int, ...] = DEFAULT_AGES
    fyrs: tuple[int, ...] = ()
    run_id: str = ""


@dataclass
class Task:
    """단일 실행 단위(= 하나의 워터마크 파티션 또는 순차 복합단계)."""
    source: str
    scope: str
    fn: Callable[[Any, Any], dict]   # (http, conn) -> collector 결과 dict
    parallel: bool = True
    label: str = ""


@dataclass
class TaskResult:
    source: str
    scope: str
    kind: str            # 'ok' | 'empty' | 'partial' | 'error' | 'unavailable'
    counts: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class RunReport:
    run_id: str = ""
    sources: tuple[str, ...] = ()
    partitions: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    changed: int = 0
    errors: int = 0
    empty: int = 0
    unavailable: int = 0
    partial: int = 0
    by_source: dict = field(default_factory=dict)
    results: list = field(default_factory=list)
    change_events: dict = field(default_factory=dict)

    @property
    def error_threshold(self) -> int:
        return max(10, math.ceil(self.partitions * self._budget))

    _budget: float = 0.05

    @property
    def run_failed(self) -> bool:
        # 파티션이 하나도 없으면(전부 unavailable/empty) 실패 아님.
        return self.errors > self.error_threshold


def _zero_counts() -> dict:
    return {"inserted": 0, "updated": 0, "unchanged": 0, "changed": 0, "errors": 0, "status": "ok"}


def _merge_counts(acc: dict, r: dict) -> None:
    for k in ("inserted", "updated", "unchanged", "changed", "errors"):
        acc[k] = int(acc.get(k, 0) or 0) + int((r or {}).get(k, 0) or 0)


# --------------------------------------------------------------------------- #
# 파티션 열거(DB 조회)
# --------------------------------------------------------------------------- #
def _statute_queries(conn) -> list[str]:
    """위임 상위법 유한집합 질의어(= 이미 아는 법령명). 없으면 빈 목록."""
    try:
        rows = _db.fetchall(
            conn,
            "SELECT DISTINCT name FROM legal_instrument "
            "WHERE source_type='statute' AND (status IS NULL OR status='active') "
            "AND name IS NOT NULL LIMIT 500",
        )
        return [r["name"] for r in rows if r.get("name")]
    except Exception:
        return []


def _enumerate_tasks(source: str, conn, cfg, opts: RunOpts) -> list[Task]:
    """소스별 파티션 → Task 목록. DB 가 비면 0 파티션(정상)."""
    rid = opts.run_id
    reorg = opts.reorg_event or None

    if source == "boundary":
        def _boundary_fn(http, conn) -> dict:
            acc = _zero_counts()
            acc["steps"] = {}
            steps: list[tuple[str, Callable[[], dict]]] = [
                ("collect_regions",
                 lambda: _collector("geo", "collect_regions")(http, cfg, conn, run_id=rid)),
                ("collect_boundaries",
                 lambda: _collector("geo", "collect_boundaries")(
                     http, cfg, conn, reorg_event=reorg, run_id=rid)),
                ("build_crosswalk",
                 lambda: _collector("geo", "build_crosswalk")(conn)),
                ("compute_adjacency",
                 lambda: _collector("geo", "compute_adjacency")(conn)),
            ]
            for name, fn in steps:
                try:
                    _merge_counts(acc, fn() or {})
                    acc["steps"][name] = "ok"
                except _util.NotFound:
                    acc["steps"][name] = "empty"
                except StepUnavailable as exc:
                    acc["steps"][name] = f"unavailable: {exc}"
                except Exception as exc:  # 단계 실패 격리
                    acc["errors"] += 1
                    acc["status"] = "partial"
                    acc["steps"][name] = f"error: {exc}"
            return acc

        return [Task("boundary", "global", _boundary_fn, parallel=False, label="지역 스파인·경계")]

    if source == "law":
        def _law_fn(http, conn) -> dict:
            queries = _statute_queries(conn)
            return _collector("law", "collect_statutes")(
                http, cfg, conn, queries=queries, run_id=rid) or _zero_counts()

        return [Task("law", "global", _law_fn, parallel=False, label="위임 상위법")]

    if source == "ordin":
        try:
            regions = _db.fetchall(
                conn,
                "SELECT region_id, sig_cd, law_org, law_sborg FROM regions "
                "WHERE has_legislation=1 AND level IN (1,2) "
                "AND (status IS NULL OR status='active') AND law_org IS NOT NULL",
            )
        except Exception:
            regions = []
        tasks: list[Task] = []
        for r in regions:
            sig = r.get("sig_cd") or r.get("region_id")

            def _ordin_fn(http, conn, row=r) -> dict:
                return _collector("law", "collect_ordinances")(
                    http, cfg, conn,
                    region_id=row["region_id"], org=row.get("law_org"),
                    sborg=row.get("law_sborg"), run_id=rid) or _zero_counts()

            tasks.append(Task("ordin", f"sig:{sig}", _ordin_fn, label=str(sig)))
        return tasks

    if source == "na_bill":
        tasks = []

        def _leg_fn(http, conn) -> dict:
            return _collector("assembly", "collect_legislators")(
                http, cfg, conn, run_id=rid) or _zero_counts()

        tasks.append(Task("na_bill", "roster", _leg_fn, parallel=False, label="의원명부"))
        for age in opts.ages:
            def _bill_fn(http, conn, age=age) -> dict:
                return _collector("assembly", "collect_bills")(
                    http, cfg, conn, age=age, run_id=rid) or _zero_counts()

            tasks.append(Task("na_bill", f"age:{age}", _bill_fn, label=f"{age}대 발의"))
        return tasks

    if source == "na_vote":
        tasks = []
        for age in opts.ages:
            def _vote_fn(http, conn, age=age) -> dict:
                acc = _zero_counts()
                acc["steps"] = {}
                # 1) 의안별 표결 집계
                try:
                    _merge_counts(acc, _collector("assembly", "collect_vote_tallies")(
                        http, cfg, conn, age=age, run_id=rid) or {})
                    acc["steps"]["tallies"] = "ok"
                except _util.NotFound:
                    acc["steps"]["tallies"] = "empty"
                except StepUnavailable as exc:
                    acc["steps"]["tallies"] = f"unavailable: {exc}"
                except Exception as exc:
                    acc["errors"] += 1
                    acc["status"] = "partial"
                    acc["steps"]["tallies"] = f"error: {exc}"
                # 2) 미의결→의결 전이 의안 워킹셋만 의원별 표결 재조회
                try:
                    ws = _db.fetchall(
                        conn,
                        "SELECT bill_id FROM bills WHERE age=? "
                        "AND (proc_result_cd IS NOT NULL OR proc_dt IS NOT NULL)",
                        (age,),
                    )
                    bill_ids = [r["bill_id"] for r in ws if r.get("bill_id")]
                except Exception:
                    bill_ids = []
                if bill_ids:
                    try:
                        _merge_counts(acc, _collector("assembly", "collect_member_votes")(
                            http, cfg, conn, age=age, bill_ids=bill_ids, run_id=rid) or {})
                        acc["steps"]["member_votes"] = f"ok ({len(bill_ids)})"
                    except _util.NotFound:
                        acc["steps"]["member_votes"] = "empty"
                    except StepUnavailable as exc:
                        acc["steps"]["member_votes"] = f"unavailable: {exc}"
                    except Exception as exc:
                        acc["errors"] += 1
                        acc["status"] = "partial"
                        acc["steps"]["member_votes"] = f"error: {exc}"
                else:
                    acc["steps"]["member_votes"] = "no-working-set"
                return acc

            tasks.append(Task("na_vote", f"age:{age}", _vote_fn, label=f"{age}대 표결"))
        return tasks

    if source == "budget":
        try:
            lafs = _db.fetchall(
                conn,
                "SELECT DISTINCT lofin_laf_cd FROM regions "
                "WHERE lofin_laf_cd IS NOT NULL AND level IN (1,2) "
                "AND (status IS NULL OR status='active')",
            )
            laf_cds = [r["lofin_laf_cd"] for r in lafs if r.get("lofin_laf_cd")]
        except Exception:
            laf_cds = []
        fyrs = opts.fyrs or _default_fyrs()
        tasks = []
        for laf in laf_cds:
            for fyr in fyrs:
                def _budget_fn(http, conn, laf=laf, fyr=fyr) -> dict:
                    return _collector("budget", "collect_budget")(
                        http, cfg, conn, laf_cd=laf, fyr=fyr, run_id=rid) or _zero_counts()

                tasks.append(Task("budget", f"laf:{laf}|fyr:{fyr}", _budget_fn,
                                  label=f"{laf}/{fyr}"))
        return tasks

    return []


def _default_fyrs() -> tuple[int, ...]:
    """현행 FY + 직전 FY(명세[realtime] §2c)."""
    try:
        y = int(_util.today_kst()[:4])
    except Exception:
        y = 2026
    return (y, y - 1)


# --------------------------------------------------------------------------- #
# 워커(파티션 1개 실행 — 자체 conn/http 로 격리)
# --------------------------------------------------------------------------- #
def _classify(res: dict) -> str:
    status = (res or {}).get("status")
    errors = int((res or {}).get("errors", 0) or 0)
    if status == "error":
        return "error"
    if errors > 0 or status == "partial":
        return "partial"
    return "ok"


def _run_task(task: Task, cfg) -> TaskResult:
    """파티션 1개를 자체 연결/HTTP 로 실행. 예외는 파티션 격리(전체 중단 금지)."""
    conn = _db.connect()
    http = _util.HttpClient(cfg)
    try:
        try:
            res = task.fn(http, conn) or _zero_counts()
            conn.commit()
        except _util.NotFound:
            conn.rollback()
            return TaskResult(task.source, task.scope, "empty", _zero_counts(), "정상적 없음")
        return TaskResult(task.source, task.scope, _classify(res), res, str(res.get("status", "")))
    except StepUnavailable as exc:
        conn.rollback()
        return TaskResult(task.source, task.scope, "unavailable", _zero_counts(), str(exc))
    except (_util.PermanentError, _util.TransientError) as exc:
        conn.rollback()
        _safe_mark(conn, task.source, task.scope, str(exc))
        return TaskResult(task.source, task.scope, "error", _zero_counts(), str(exc))
    except Exception as exc:  # noqa: BLE001 — 파티션 격리 규율
        conn.rollback()
        _safe_mark(conn, task.source, task.scope, f"{type(exc).__name__}: {exc}")
        return TaskResult(task.source, task.scope, "error", _zero_counts(),
                          f"{type(exc).__name__}: {exc}")
    finally:
        http.close()
        conn.close()


def _safe_mark(conn, source: str, scope: str, note: str) -> None:
    """실패 파티션 격리 마킹(커서 미전진). 실패해도 조용히 통과."""
    try:
        _db.mark_partition_status(conn, source, scope, "error",
                                  note=note[:200], bump_retry=True)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 소스 실행 오케스트레이션
# --------------------------------------------------------------------------- #
def _absorb(report: RunReport, tr: TaskResult) -> None:
    report.results.append(tr)
    report.partitions += 1
    _merge_counts_report(report, tr.counts)
    if tr.kind == "error":
        report.errors += 1
    elif tr.kind == "empty":
        report.empty += 1
    elif tr.kind == "unavailable":
        report.unavailable += 1
    elif tr.kind == "partial":
        report.partial += 1
    bs = report.by_source.setdefault(
        tr.source, {"partitions": 0, "changed": 0, "errors": 0,
                    "empty": 0, "unavailable": 0, "partial": 0})
    bs["partitions"] += 1
    bs["changed"] += int((tr.counts or {}).get("changed", 0) or 0)
    # by_source 키는 초기 dict 와 동일한 단수형(errors/empty/unavailable/partial)만 사용.
    if tr.kind == "error":
        bs["errors"] += 1
    elif tr.kind == "empty":
        bs["empty"] += 1
    elif tr.kind == "unavailable":
        bs["unavailable"] += 1
    elif tr.kind == "partial":
        bs["partial"] += 1


def _merge_counts_report(report: RunReport, counts: dict) -> None:
    c = counts or {}
    report.inserted += int(c.get("inserted", 0) or 0)
    report.updated += int(c.get("updated", 0) or 0)
    report.unchanged += int(c.get("unchanged", 0) or 0)
    report.changed += int(c.get("changed", 0) or 0)


def _do_run(cfg, opts: RunOpts) -> RunReport:
    """지정 소스들을 순서대로 실행하고 파티션을 (병렬) 처리."""
    report = RunReport(run_id=opts.run_id, sources=opts.sources)
    report._budget = opts.error_budget
    ordered = [s for s in SOURCE_ORDER if s in opts.sources]

    main_conn = _db.connect()
    try:
        for source in ordered:
            try:
                tasks = _enumerate_tasks(source, main_conn, cfg, opts)
            except Exception as exc:  # 열거 자체 실패도 격리
                log.warning("소스 %s 파티션 열거 실패: %s", source, exc)
                tasks = []
            if not tasks:
                log.info("소스 %s: 파티션 0개(스킵)", source)
                report.by_source.setdefault(
                    source, {"partitions": 0, "changed": 0, "errors": 0,
                             "empty": 0, "unavailable": 0, "partial": 0})
                continue

            seq = [t for t in tasks if not t.parallel]
            par = [t for t in tasks if t.parallel]
            for t in seq:
                _absorb(report, _run_task(t, cfg))
            if par:
                workers = max(1, min(opts.concurrency, len(par)))
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = {ex.submit(_run_task, t, cfg): t for t in par}
                    for fut in as_completed(futs):
                        try:
                            _absorb(report, fut.result())
                        except Exception as exc:  # 워커 자체 예외(방어)
                            t = futs[fut]
                            _absorb(report, TaskResult(t.source, t.scope, "error",
                                                       _zero_counts(), str(exc)))
            log.info("소스 %s 완료: %s", source, report.by_source.get(source))
        # 변경로그 이벤트 집계(이번 run_id)
        report.change_events = _change_event_breakdown(main_conn, opts.run_id)
    finally:
        main_conn.close()
    return report


def _change_event_breakdown(conn, run_id: str) -> dict:
    try:
        rows = _db.fetchall(
            conn,
            "SELECT event, COUNT(*) AS n FROM change_log WHERE run_id=? GROUP BY event",
            (run_id,),
        )
        return {r["event"]: int(r["n"]) for r in rows if r.get("event")}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# 산출물: GITHUB_OUTPUT / 요약 마크다운 / 워터마크 스냅숏
# --------------------------------------------------------------------------- #
def _emit_github_output(**kv: Any) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")
    except OSError as exc:
        log.warning("GITHUB_OUTPUT 기록 실패: %s", exc)


def _write_summary(report: RunReport, path: Path) -> Path:
    """운영 이슈 본문용 마크다운(law-freshness 패턴: 변경 있을 때만 이슈 upsert)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    verdict = "실패" if report.run_failed else "성공"
    lines.append(f"# 데이터 증분 갱신 요약 ({verdict})")
    lines.append("")
    lines.append(f"- run_id: `{report.run_id}`")
    lines.append(f"- 시각(KST): {_util.now_kst_iso()}")
    lines.append(f"- 소스: {', '.join(report.sources) or '(없음)'}")
    lines.append(f"- 파티션: {report.partitions} "
                 f"(오류 {report.errors} / 부분 {report.partial} / "
                 f"빈결과 {report.empty} / 미구현 {report.unavailable})")
    lines.append(f"- 변경(changed): **{report.changed}** "
                 f"(신규 {report.inserted} / 갱신 {report.updated} / 무변경 {report.unchanged})")
    lines.append(f"- 오류예산 임계: {report.error_threshold} "
                 f"(error_budget={report._budget})")
    lines.append("")
    if report.change_events:
        lines.append("## 변경 이벤트")
        for ev, n in sorted(report.change_events.items()):
            lines.append(f"- {ev}: {n}")
        lines.append("")
    lines.append("## 소스별")
    lines.append("")
    lines.append("| 소스 | 파티션 | 변경 | 오류 | 부분 | 빈 | 미구현 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for src in SOURCE_ORDER:
        bs = report.by_source.get(src)
        if not bs:
            continue
        lines.append(
            f"| {src} | {bs['partitions']} | {bs['changed']} | "
            f"{bs['errors']} | {bs.get('partial', 0)} | "
            f"{bs.get('empty', 0)} | {bs.get('unavailable', 0)} |"
        )
    lines.append("")
    # 실패 파티션 샘플(최대 20)
    fails = [tr for tr in report.results if tr.kind == "error"][:20]
    if fails:
        lines.append("## 실패 파티션(샘플)")
        for tr in fails:
            lines.append(f"- `{tr.source}` / `{tr.scope}`: {tr.note}")
        lines.append("")
    lines.append("_원문은 미러링하지 않으며 law.go.kr 직링크로 위임한다. "
                 "본 자료는 의사결정 지원용이며 법률판단이 아니다._")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_watermark_snapshot(path: Path) -> Optional[Path]:
    """watermarks 테이블 → JSON 스냅숏(감사·롤백용 커밋백; 명세[realtime] §1)."""
    conn = _db.connect()
    try:
        rows = _db.fetchall(conn, "SELECT * FROM watermarks ORDER BY source, scope")
    except Exception as exc:
        log.warning("워터마크 스냅숏 실패: %s", exc)
        return None
    finally:
        conn.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generatedAt": _util.now_kst_iso(), "count": len(rows), "watermarks": rows}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 서브커맨드 핸들러
# --------------------------------------------------------------------------- #
def _cmd_init(cfg, args) -> int:
    conn = _db.init_db()
    try:
        tables = _db.fetchall(
            conn, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ver = _db.fetchone(conn, "SELECT value FROM schema_meta WHERE key='schema_version'")
        log.info("DB 초기화 완료: %s (테이블 %d개, schema_version=%s)",
                 cfg.db_path, len(tables), (ver or {}).get("value"))
    finally:
        conn.close()
    return 0


def _make_opts(cfg, args, sources: tuple[str, ...]) -> RunOpts:
    out_dir = getattr(args, "out", None) or cfg.out_dir
    state = getattr(args, "state", None) or str(Path(out_dir) / "state" / "watermarks.json")
    ages = _parse_int_csv(getattr(args, "ages", None)) or DEFAULT_AGES
    fyrs = _parse_int_csv(getattr(args, "fyr", None)) or ()
    run_id = os.environ.get("GITHUB_RUN_ID") or uuid.uuid4().hex[:12]
    reorg = (getattr(args, "reorg_event", None) or "").strip() or None
    return RunOpts(
        sources=sources,
        reorg_event=reorg,
        out_dir=str(out_dir),
        state_path=str(state),
        concurrency=int(getattr(args, "concurrency", None) or cfg.concurrency),
        retries=int(getattr(args, "retries", None) or cfg.http_retries),
        error_budget=float(getattr(args, "error_budget", None) or cfg.error_budget),
        ages=ages,
        fyrs=fyrs,
        run_id=run_id,
    )


def _parse_int_csv(raw: Optional[str]) -> tuple[int, ...]:
    if not raw:
        return ()
    out: list[int] = []
    for tok in str(raw).split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.append(int(tok))
    return tuple(out)


def _cmd_refresh(cfg, args) -> int:
    """증분 갱신(CDC). GITHUB_OUTPUT 에 changed_count/error_count/summary_path 기록."""
    raw = (getattr(args, "sources", "") or "").strip()
    sources = _expand_refresh_sources(raw)
    opts = _make_opts(cfg, args, sources)

    if not sources:
        log.info("refresh: 대상 소스 없음(빈 --sources) → 무작업")
        summary_path = Path(opts.out_dir) / "state" / "refresh-summary.md"
        empty = RunReport(run_id=opts.run_id, sources=())
        empty._budget = opts.error_budget
        _write_summary(empty, summary_path)
        _emit_github_output(changed_count=0, error_count=0, summary_path=str(summary_path))
        return 0

    log.info("refresh 시작: run_id=%s sources=%s concurrency=%d error_budget=%.3f",
             opts.run_id, ",".join(sources), opts.concurrency, opts.error_budget)
    report = _do_run(cfg, opts)

    summary_path = _write_summary(report, Path(opts.out_dir) / "state" / "refresh-summary.md")
    _write_watermark_snapshot(Path(opts.state_path))
    _emit_github_output(
        changed_count=report.changed,
        error_count=report.errors,
        summary_path=str(summary_path),
    )
    log.info("refresh 종료: changed=%d errors=%d/%d threshold=%d %s",
             report.changed, report.errors, report.partitions,
             report.error_threshold, "FAILED" if report.run_failed else "OK")
    return 1 if report.run_failed else 0


def _cmd_collect(cfg, args) -> int:
    """도메인 단위 수집(law|assembly|geo|budget|all). refresh 와 동일 코어."""
    domain = args.domain
    sources = COLLECT_DOMAINS.get(domain)
    if not sources:
        log.error("알 수 없는 도메인: %s (선택: %s)", domain, ", ".join(COLLECT_DOMAINS))
        return 2
    opts = _make_opts(cfg, args, sources)
    log.info("collect 시작: domain=%s sources=%s run_id=%s",
             domain, ",".join(sources), opts.run_id)
    report = _do_run(cfg, opts)
    _write_summary(report, Path(opts.out_dir) / "state" / "collect-summary.md")
    _write_watermark_snapshot(Path(opts.state_path))
    _emit_github_output(changed_count=report.changed, error_count=report.errors)
    log.info("collect 종료: changed=%d errors=%d/%d", report.changed,
             report.errors, report.partitions)
    return 0


def _expand_refresh_sources(raw: str) -> tuple[str, ...]:
    out: list[str] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok in REFRESH_ALIASES:
            out.extend(REFRESH_ALIASES[tok])
        elif tok in VALID_SOURCES:
            out.append(tok)
        else:
            log.warning("알 수 없는 소스 토큰 무시: %r", tok)
    # 순서 정규화 + 중복 제거
    seen: set[str] = set()
    return tuple(s for s in SOURCE_ORDER if s in out and not (s in seen or seen.add(s)))


def _cmd_parse(cfg, args) -> int:
    """DB 로컬 파생: 인용 파싱·분류·임베딩·유사도(+선택적 위임연계)."""
    conn = _db.connect()
    steps_report: dict[str, str] = {}
    try:
        steps_report["citations"] = _step_citations(conn)
        steps_report["category"] = _step_category(conn)
        steps_report["embedding"] = _step_embedding(conn)
        if getattr(args, "delegation", False):
            steps_report["delegation"] = _step_delegation(conn, cfg)
        conn.commit()
    finally:
        conn.close()
    for name, msg in steps_report.items():
        log.info("parse[%s]: %s", name, msg)
    return 0


def _step_citations(conn) -> str:
    try:
        parse_citations = _resolve("policymap.parsers.delegation", "parse_citations")
        save_citations = _resolve("policymap.parsers.delegation", "save_citations")
    except StepUnavailable as exc:
        return f"unavailable: {exc}"
    try:
        arts = _db.fetchall(
            conn, "SELECT ordinance_id, body FROM ordinance_articles WHERE body IS NOT NULL")
    except Exception as exc:
        return f"skip(no rows): {exc}"
    total = 0
    for a in arts:
        rows = parse_citations(a.get("body") or "") or []
        for r in rows:
            r.setdefault("src_id", a["ordinance_id"])
        if rows:
            try:
                save_citations(conn, rows)
                total += len(rows)
            except Exception as exc:
                return f"error: {exc}"
    return f"ok (인용 {total}건)"


def _step_category(conn) -> str:
    try:
        classify = _resolve("policymap.parsers.category", "classify_ordinance")
        save = _resolve("policymap.parsers.category", "save_categories")
    except StepUnavailable as exc:
        return f"unavailable: {exc}"
    try:
        cats = _db.fetchall(conn, "SELECT * FROM categories")
        ords = _db.fetchall(conn, "SELECT * FROM ordinances WHERE status='active'")
    except Exception as exc:
        return f"skip: {exc}"
    n = 0
    for o in ords:
        try:
            arts = _db.fetchall(
                conn, "SELECT * FROM ordinance_articles WHERE ordinance_id=?",
                (o["ordinance_id"],))
            rows = classify(o, arts, cats) or []
            if rows:
                save(conn, o["ordinance_id"], rows)
                n += 1
        except Exception as exc:
            return f"partial({n}): {exc}"
    return f"ok (분류 {n}건)"


def _step_embedding(conn) -> str:
    try:
        embed = _resolve("policymap.parsers.embedding", "embed_ordinances")
        build_sim = _resolve("policymap.parsers.embedding", "build_similarity")
    except StepUnavailable as exc:
        return f"unavailable: {exc}"
    try:
        r1 = embed(conn) or {}
        r2 = build_sim(conn) or {}
        return f"ok (embed={r1}, sim={r2})"
    except Exception as exc:
        return f"error: {exc}"


def _step_delegation(conn, cfg) -> str:
    """선택적 위임 4경로 연계(네트워크). 키 없으면 스킵."""
    if not getattr(cfg, "law_oc", None):
        return "skip: LAW_OC 없음"
    try:
        merge = _resolve("policymap.parsers.delegation", "merge_delegations")
        save = _resolve("policymap.parsers.delegation", "save_delegations")
        _resolve("policymap.collectors.law", "ls_delegated")
    except StepUnavailable as exc:
        return f"unavailable: {exc}"
    http = _util.HttpClient(cfg)
    try:
        parents = _db.fetchall(
            conn, "SELECT instrument_id, mst FROM legal_instrument "
                  "WHERE source_type='statute' AND mst IS NOT NULL LIMIT 100")
    except Exception as exc:
        http.close()
        return f"skip: {exc}"
    total = 0
    for p in parents:
        mst, pid = p.get("mst"), p["instrument_id"]
        buckets = []
        for attr, ex in (("ls_delegated", "extract_delegations_from_lsdelegated"),
                         ("ls_stmd", "extract_delegations_from_lsstmd")):
            try:
                resp = _resolve("policymap.collectors.law", attr)(http, cfg, mst)
                extractor = _resolve("policymap.parsers.delegation", ex)
                buckets.append(extractor(resp, pid) or [])
            except (_util.NotFound, StepUnavailable):
                continue
            except Exception:
                continue
        if buckets:
            merged = merge(*buckets) or []
            if merged:
                try:
                    save(conn, merged)
                    total += len(merged)
                except Exception:
                    continue
    http.close()
    return f"ok (위임 {total}건)"


def _cmd_build(cfg, args) -> int:
    """SQLite → 그래프 빌드 + 조례↔예산 링크(있으면)."""
    conn = _db.connect()
    try:
        try:
            g = _resolve("policymap.graph.build", "build_graph")(conn)
            n = _graph_size(g)
            log.info("build_graph: 노드 %s / 엣지 %s", n[0], n[1])
        except StepUnavailable as exc:
            log.warning("build_graph unavailable: %s", exc)
        try:
            r = _resolve("policymap.graph.analysis", "link_ordinance_budget")(conn)
            conn.commit()
            log.info("link_ordinance_budget: %s", r)
        except StepUnavailable as exc:
            log.info("link_ordinance_budget unavailable: %s", exc)
        except Exception as exc:
            log.warning("link_ordinance_budget 실패: %s", exc)
    finally:
        conn.close()
    return 0


def _graph_size(g: Any) -> tuple[Any, Any]:
    try:
        if hasattr(g, "number_of_nodes"):
            return g.number_of_nodes(), g.number_of_edges()
        if isinstance(g, dict):
            nodes = g.get("nodes")
            edges = g.get("edges")
            return (len(nodes) if nodes is not None else "?",
                    len(edges) if edges is not None else "?")
    except Exception:
        pass
    return "?", "?"


def _cmd_export(cfg, args) -> int:
    out_dir = getattr(args, "out", None) or cfg.out_dir
    conn = _db.connect()
    try:
        try:
            r = _resolve("policymap.graph.export", "export_static")(
                conn, str(out_dir), as_of_date=_util.today_kst())
            log.info("export_static: %s → %s", r, out_dir)
        except StepUnavailable as exc:
            log.warning("export_static unavailable: %s — 워터마크 스냅숏만 기록", exc)
    finally:
        conn.close()
    # 항상 워터마크 스냅숏은 남긴다(커밋백 대상).
    snap = _write_watermark_snapshot(Path(out_dir) / "state" / "watermarks.json")
    if snap:
        log.info("워터마크 스냅숏: %s", snap)
    return 0


def _cmd_mcp(cfg, args) -> int:
    try:
        main_fn = _resolve("policymap.mcp_server.server", "main")
    except StepUnavailable as exc:
        log.error("MCP 서버 미구현: %s", exc)
        return 3
    main_fn()
    return 0


def _cmd_seed(cfg, args) -> int:
    try:
        seed_fn = _resolve("policymap.seed_korea100", "seed")
    except StepUnavailable as exc:
        log.error("seed_korea100 미구현: %s", exc)
        return 3
    result = seed_fn(data_dir=getattr(args, "data_dir", None),
                     run_id=os.environ.get("GITHUB_RUN_ID"))
    log.info("seed 완료: %s", result)
    return 0


def _cmd_seed_regions(cfg, args) -> int:
    """reference/ 실데이터 → regions/geometry/adjacency/succession/population 실적재.

    순서: load_regions_bjd → attach_geometry → geo.compute_adjacency →
          apply_verified_reorgs → load_population. 각 단계 격리(한 단계 실패가 전체 중단 금지),
          멱등(재실행 unchanged). 지연 import(reference/geo 미가용 시 unavailable 보고).
    """
    ref_dir = getattr(args, "reference_dir", None)
    run_id = os.environ.get("GITHUB_RUN_ID") or uuid.uuid4().hex[:12]
    conn = _db.connect()
    summary: dict[str, Any] = {}
    rc = 0
    try:
        steps: list[tuple[str, Callable[[], dict]]] = [
            ("load_regions_bjd",
             lambda: _resolve("policymap.reference", "load_regions_bjd")(
                 conn, reference_dir=ref_dir, run_id=run_id)),
            ("attach_geometry",
             lambda: _resolve("policymap.reference", "attach_geometry")(
                 conn, reference_dir=ref_dir, run_id=run_id)),
            ("compute_adjacency",
             lambda: _resolve("policymap.collectors.geo", "compute_adjacency")(conn)),
            ("apply_verified_reorgs",
             lambda: _resolve("policymap.reference", "apply_verified_reorgs")(
                 conn, reference_dir=ref_dir, run_id=run_id)),
            ("load_population",
             lambda: _resolve("policymap.reference", "load_population")(
                 conn, reference_dir=ref_dir, run_id=run_id)),
        ]
        for name, fn in steps:
            try:
                r = fn() or {}
                conn.commit()
                summary[name] = r
                log.info("seed-regions[%s]: %s", name,
                         {k: v for k, v in r.items() if k != "detail"})
            except StepUnavailable as exc:
                summary[name] = {"status": "unavailable", "note": str(exc)}
                log.warning("seed-regions[%s] unavailable: %s", name, exc)
            except Exception as exc:  # 단계 격리
                conn.rollback()
                summary[name] = {"status": "error", "note": f"{type(exc).__name__}: {exc}"}
                log.error("seed-regions[%s] 실패: %s", name, exc)
                rc = 1
        # 최종 카운트 요약
        try:
            log.info("seed-regions 완료: regions=%d(active=%d) geometry=%d adjacency=%d "
                     "succession=%d population=%d",
                     _db.count(conn, "regions"),
                     _db.count(conn, "regions", "status='active'"),
                     _db.count(conn, "region_geometry"),
                     _db.count(conn, "region_adjacency"),
                     _db.count(conn, "region_succession"),
                     _db.count(conn, "regions", "population IS NOT NULL"))
        except Exception:
            pass
    finally:
        conn.close()
    return rc


def _cmd_neural(cfg, args) -> int:
    """그래프 신경망 임베딩 학습(numpy 단독) + node_embeddings/neural_similarity 적재.

    파이프라인: build_graph → GraphArrays → split_edges(held-out 10%) →
    {node2vec | metapath2vec | graphsage} 학습 → held-out AUC 평가 →
    save_node_embeddings → build_neural_similarity(조례·지역 Top-k).

    held-out 엣지는 워크·메시지패싱 그래프에서 제거한 뒤 학습한다(transductive 누수 차단).
    numpy 부재 시 3(unavailable)으로 종료한다 — 실패가 아니라 미가용.
    """
    models = [m.strip() for m in str(getattr(args, "model", "graphsage") or "").split(",")
              if m.strip()] or ["graphsage"]
    if "all" in models:
        models = ["node2vec", "metapath2vec", "graphsage"]
    dim = int(getattr(args, "dim", 128) or 128)
    epochs = getattr(args, "epochs", None)
    test_frac = float(getattr(args, "test_frac", 0.1) or 0.1)
    seed = int(getattr(args, "seed", 20260819) or 20260819)
    top_k = int(getattr(args, "top_k", 10) or 10)
    max_items = getattr(args, "max_items", None)
    skip_similarity = bool(getattr(args, "no_similarity", False))
    run_id = os.environ.get("GITHUB_RUN_ID") or uuid.uuid4().hex[:12]

    try:
        neural = importlib.import_module("policymap.neural")
    except ImportError as exc:
        log.error("policymap.neural 미가용: %s", exc)
        return 3
    try:
        import numpy  # noqa: F401
    except ImportError:
        log.error("numpy 부재 — 신경망 학습 불가. pip install numpy 후 재실행.")
        return 3

    conn = _db.connect()
    summary: dict[str, Any] = {"run_id": run_id, "models": {}}
    rc = 0
    try:
        G = _resolve("policymap.graph.build", "build_graph")(conn)
        n_nodes, n_edges = _graph_size(G)
        log.info("build_graph: 노드 %s / 엣지 %s", n_nodes, n_edges)
        ga = neural.GraphArrays.from_graph(G)
        split = neural.split_edges(ga, test_frac=test_frac, seed=seed)
        train_ga = ga.subset_edges(split["train_mask"])
        summary["graph"] = {"nodes": int(ga.num_nodes), "edges": int(ga.num_edges),
                            "train_edges": split["num_train"],
                            "test_edges": split["num_test"]}
        log.info("학습 대상: %d노드 / %d엣지(train %d / held-out %d)",
                 ga.num_nodes, ga.num_edges, split["num_train"], split["num_test"])

        neural.ensure_neural_tables(conn)
        for name in models:
            try:
                res = _train_one_model(neural, name, conn, ga, train_ga, split,
                                       dim=dim, epochs=epochs, seed=seed)
            except Exception as exc:  # 모델 격리 — 하나 실패해도 나머지 진행
                log.error("neural[%s] 학습 실패: %s: %s", name, type(exc).__name__, exc)
                summary["models"][name] = {"status": "error",
                                           "note": f"{type(exc).__name__}: {exc}"}
                rc = 1
                continue
            saved = neural.save_node_embeddings(conn, res, run_id=run_id)
            entry = {"status": "ok", "model_name": res.model_name, "dim": res.dim,
                     "nodes": len(res.nodes), "saved": saved.get("saved"),
                     "loss_first": (res.loss_history or [None])[0],
                     "loss_last": (res.loss_history or [None])[-1],
                     "auc": getattr(res, "auc", None)}
            if entry["auc"] is None:
                entry["auc"] = neural.evaluate_link_prediction(
                    res.matrix, ga, split["test_src"], split["test_dst"],
                    negative_sampling="type-matched").get("auc")
            if not skip_similarity:
                for kinds, label in ((("Ordinance",), "ordinance"), (("Region",), "region")):
                    sim = neural.build_neural_similarity(
                        conn, res, top_k=top_k, kinds=kinds,
                        max_items=int(max_items) if max_items else None,
                        replace=(label == "ordinance"), seed=seed)
                    entry[f"similarity_{label}"] = {"nodes": sim.get("nodes"),
                                                    "pairs": sim.get("pairs")}
            summary["models"][name] = entry
            log.info("neural[%s]: %s", name, entry)
        _db.set_watermark(conn, "neural", "train",
                          cursor=",".join(models), status="ok" if rc == 0 else "partial",
                          run_id=run_id, note=json.dumps(summary["graph"]))
        conn.commit()
    except StepUnavailable as exc:
        log.error("graph.build 미가용: %s", exc)
        return 3
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return rc


def _train_one_model(neural, name: str, conn, ga, train_ga, split, *,
                     dim: int, epochs, seed: int):
    """모델명 → 학습 호출(기본 하이퍼파라미터는 실측 채택값)."""
    if name == "node2vec":
        return neural.train_node2vec(
            train_ga, dim=dim, walks_per_node=10, walk_len=40, window=5,
            epochs=int(epochs or 5), negatives=5, p=0.5, q=2.0,
            model_name="node2vec-numpy", seed=seed, logger=log)
    if name == "metapath2vec":
        # 그래프에 없는 라벨을 요구하는 메타패스는 제외(작은/부분 그래프 대응).
        # METAPATHS 는 {이름: [라벨,...]} dict 이므로 스키마만 뽑아 쓴다.
        mps = neural.METAPATHS
        schemas = list(mps.values()) if isinstance(mps, dict) else list(mps)
        labels = set(getattr(train_ga, "label_names", ()) or ())
        paths = [mp for mp in schemas if not labels or set(mp) <= labels]
        if not paths:
            raise ValueError(
                f"적용 가능한 메타패스 없음(그래프 라벨 {sorted(labels)}). "
                "metapath2vec 대신 node2vec 을 쓰라.")
        return neural.train_node2vec(
            train_ga, dim=min(dim, 64), walks_per_node=10, walk_len=40, window=5,
            epochs=int(epochs or 3), negatives=5,
            metapaths=paths, model_name="metapath2vec-numpy",
            seed=seed, logger=log)
    if name == "graphsage":
        # JK concat: 최종 dim 은 그대로 유지되고 내부에서 (layers+1) 블록으로 쪼갠다.
        return neural.train_graphsage(
            ga, dim=dim, layers=2, epochs=int(epochs or 220), lr=0.01,
            negative_sampling="type-matched", jumping_knowledge=True,
            residual=True, fanout=32, split=split, conn=conn,
            model_name="graphsage-numpy", seed=seed, logger=log)
    raise ValueError(f"알 수 없는 모델: {name} (node2vec|metapath2vec|graphsage|all)")


def _cmd_index(cfg, args) -> int:
    """GraphRAG 검색 인덱스(BM25+Dense) 구축 + 커뮤니티 요약 리포트 생성.

    증분이 기본이다 — content_hash 가 바뀐 문서만 새 세그먼트에 추가하고 구버전은
    툼스톤 처리한다(변경 없으면 아무 것도 쓰지 않는다). --force 로 전체 재빌드.
    """
    scopes = [s.strip() for s in str(getattr(args, "scope", "all") or "all").split(",")
              if s.strip()] or ["all"]
    force = bool(getattr(args, "force", False))
    index_dir = getattr(args, "index_dir", None)
    skip_community = bool(getattr(args, "no_community", False))

    try:
        rag = importlib.import_module("policymap.rag")
    except ImportError as exc:
        log.error("policymap.rag 미가용: %s", exc)
        return 3

    conn = _db.connect()
    summary: dict[str, Any] = {"index": {}, "community": {}}
    rc = 0
    try:
        for scope in scopes:
            try:
                r = rag.build_index(conn, scope=scope, index_dir=index_dir,
                                    incremental=not force, force=force)
                summary["index"][scope] = r
                log.info("index[%s]: mode=%s docs=%s segments=%s bytes=%s %.2fs",
                         scope, r.get("mode"), r.get("docs_indexed"),
                         r.get("segments"), r.get("bytes"), r.get("elapsed_sec") or 0.0)
            except Exception as exc:  # scope 격리
                log.error("index[%s] 실패: %s: %s", scope, type(exc).__name__, exc)
                summary["index"][scope] = {"status": "error",
                                           "note": f"{type(exc).__name__}: {exc}"}
                rc = 1
        if not skip_community:
            for cscope in ("ordinance_similarity", "region_adjacency"):
                try:
                    r = rag.build_community_report(conn, scope=cscope, index_dir=index_dir)
                    summary["community"][cscope] = {
                        "backend": r.get("backend"), "modularity": r.get("modularity"),
                        "detected": r.get("num_communities_detected"),
                        "summarized": r.get("num_communities_summarized",
                                            len(r.get("communities") or [])),
                        "elapsed_sec": r.get("elapsed_sec")}
                    log.info("community[%s]: %s", cscope, summary["community"][cscope])
                except Exception as exc:
                    log.warning("community[%s] 실패: %s", cscope, exc)
                    summary["community"][cscope] = {"status": "error", "note": str(exc)}
    finally:
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return rc


def _cmd_search(cfg, args) -> int:
    """CLI 검색(GraphRAG). 하이브리드 검색 / 그래프확장 / 컨텍스트 조립 / 전역질의."""
    query = " ".join(getattr(args, "query", []) or []).strip()
    if not query:
        log.error("검색어가 필요하다: python -m policymap.run search '반려동물 등록 지원'")
        return 2
    k = int(getattr(args, "k", 10) or 10)
    mode = str(getattr(args, "mode", "hybrid") or "hybrid")
    scope = str(getattr(args, "scope", "all") or "all")
    hops = int(getattr(args, "hops", 1) or 1)
    index_dir = getattr(args, "index_dir", None)
    as_json = bool(getattr(args, "json", False))

    try:
        rag = importlib.import_module("policymap.rag")
    except ImportError as exc:
        log.error("policymap.rag 미가용: %s", exc)
        return 3

    conn = _db.connect()
    try:
        if mode == "context":
            out = rag.answer_context(conn, query, k=k, scope=scope, hops=hops,
                                     index_dir=index_dir)
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
            return 0
        if mode == "global":
            out = rag.global_search(conn, query, k=k, index_dir=index_dir)
            if as_json:
                print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
            else:
                for c in out:
                    print(f"{c.get('rank')}. [{c.get('score')}] {c.get('label')} "
                          f"(조례 {c.get('size')}건 / {c.get('region_count')}곳)")
                    print(f"    {str(c.get('narrative') or '')[:400]}")
            return 0
        fn = {"bm25": rag.bm25_search, "dense": rag.dense_search,
              "hybrid": rag.hybrid_search}.get(mode)
        if fn is not None:
            hits = fn(conn, query, k=k, scope=scope, index_dir=index_dir,
                      group_by="parent", with_text=False)
        elif mode == "graph":
            hits = rag.hybrid_graph_search(
                conn, query, k=k, scope=scope, hops=hops, index_dir=index_dir,
                graph_weight=float(getattr(args, "graph_weight", 0.5) or 0.5),
                with_text=False)
        else:
            log.error("알 수 없는 mode: %s (bm25|dense|hybrid|graph|context|global)", mode)
            return 2
        if as_json:
            print(json.dumps(hits, ensure_ascii=False, indent=2, default=str))
            return 0
        print(f"# search mode={mode} scope={scope} k={k}: {query}")
        for h in hits:
            name = h.get("name") or h.get("parent_name") or h.get("doc_key")
            origin = h.get("origin") or "search"
            via = f" via={h['via']}" if h.get("via") else ""
            art = f" {h.get('article_no')}" if h.get("article_no") else ""
            print(f"{h.get('rank'):>3}. [{h.get('score')}] ({origin}{via}) {name}{art}")
            if h.get("path"):
                print(f"      경로: {h['path']}")
    finally:
        conn.close()
    return 0


def _cmd_status(cfg, args) -> int:
    """워터마크·테이블 카운트 요약(운영 점검)."""
    conn = _db.connect()
    try:
        try:
            wms = _db.fetchall(conn, "SELECT * FROM watermarks ORDER BY source, scope")
        except Exception:
            wms = []
        print(f"# policymap status ({_util.now_kst_iso()})")
        print(f"db: {cfg.db_path}")
        print(f"watermarks: {len(wms)}")
        for w in wms[:50]:
            print(f"  - {w.get('source')}/{w.get('scope')}: "
                  f"status={w.get('status')} changed={w.get('changed')} "
                  f"cursor={w.get('cursor')}")
        for tbl in ("regions", "legal_instrument", "articles", "ordinances",
                    "ordinance_articles", "delegations", "bills", "votes",
                    "budget_lines", "verification", "change_log"):
            try:
                print(f"count {tbl}: {_db.count(conn, tbl)}")
            except Exception:
                print(f"count {tbl}: (없음)")
    finally:
        conn.close()
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def _add_run_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", help="정적 번들/상태 출력 디렉터리(기본: config.out_dir)")
    p.add_argument("--state", help="워터마크 스냅숏 경로(기본: <out>/state/watermarks.json)")
    p.add_argument("--reorg-event", dest="reorg_event", default="",
                   help="경계 개편 이벤트 태그(boundary 실행 시)")
    p.add_argument("--concurrency", type=int, help="파티션 병렬 워커 수(기본 6)")
    p.add_argument("--retries", type=int, help="HTTP 재시도 횟수(기본 3)")
    p.add_argument("--error-budget", dest="error_budget", type=float,
                   help="오류예산 비율(기본 0.05)")
    p.add_argument("--ages", help="국회 대수 CSV(기본 22)")
    p.add_argument("--fyr", help="예산 회계연도 CSV(기본 현행+직전)")
    p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 경로 override")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="policymap.run",
        description="policy_maps 오케스트레이션 CLI (수집·파싱·빌드·내보내기·증분갱신·MCP)")
    ap.add_argument("--db", help="SQLite 경로 override(전역)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="DB 스키마 생성(멱등)")

    p_collect = sub.add_parser("collect", help="도메인 단위 수집")
    p_collect.add_argument("domain", choices=sorted(COLLECT_DOMAINS),
                           help="law|assembly|geo|budget|all")
    _add_run_flags(p_collect)

    p_parse = sub.add_parser("parse", help="DB 로컬 파생(인용/분류/임베딩/유사도)")
    p_parse.add_argument("--delegation", action="store_true",
                         help="위임 4경로 연계도 실행(네트워크·LAW_OC 필요)")
    p_parse.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 경로 override")

    p_build = sub.add_parser("build", help="그래프 빌드 + 조례↔예산 링크")
    p_build.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 경로 override")

    p_export = sub.add_parser("export", help="정적 JSON 번들 재생성 + 워터마크 스냅숏")
    p_export.add_argument("--out", help="출력 디렉터리(기본: config.out_dir)")
    p_export.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 경로 override")

    p_refresh = sub.add_parser("refresh", help="증분 갱신(CDC, 워터마크)")
    p_refresh.add_argument("--sources", default="",
                           help="쉼표구분 소스(law,ordin,na_bill,na_vote,budget,boundary; "
                                "별칭 geo/assembly/all)")
    _add_run_flags(p_refresh)

    for name in ("mcp", "serve"):
        p = sub.add_parser(name, help="MCP stdio 서버 기동")
        p.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 경로 override")

    p_seed = sub.add_parser("seed", help="korea100 로컬 데이터로 시드(키 없이 데모)")
    p_seed.add_argument("--data-dir", dest="data_dir",
                        help="korea100 web/data 경로(기본 external/korea100/web/data)")
    p_seed.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 경로 override")

    p_seedr = sub.add_parser(
        "seed-regions",
        help="reference/ 실데이터 적재(regions·경계·인접·승계·인구, 키 없이)")
    p_seedr.add_argument("--reference-dir", dest="reference_dir",
                         help="reference 데이터 경로(기본 data/reference)")
    p_seedr.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 경로 override")

    p_status = sub.add_parser("status", help="워터마크·테이블 카운트 요약")
    p_status.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 경로 override")

    p_neural = sub.add_parser("neural", help="그래프 신경망 임베딩 학습(numpy 단독)")
    p_neural.add_argument("--model", default="graphsage",
                          help="graphsage(기본)|node2vec|metapath2vec|all(쉼표 다중)")
    p_neural.add_argument("--dim", type=int, default=128, help="임베딩 차원(기본 128)")
    p_neural.add_argument("--epochs", type=int, help="에폭 수(모델별 기본값 override)")
    p_neural.add_argument("--test-frac", dest="test_frac", type=float, default=0.1,
                          help="held-out 링크예측 평가 비율(기본 0.1)")
    p_neural.add_argument("--top-k", dest="top_k", type=int, default=10,
                          help="neural_similarity Top-k(기본 10)")
    p_neural.add_argument("--max-items", dest="max_items", type=int,
                          help="kNN 전쌍비교 대상 상한(기본 무제한)")
    p_neural.add_argument("--no-similarity", dest="no_similarity", action="store_true",
                          help="neural_similarity 적재 생략(임베딩만 저장)")
    p_neural.add_argument("--seed", type=int, default=20260819, help="난수 시드")
    p_neural.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 경로 override")

    p_index = sub.add_parser("index", help="GraphRAG 검색 인덱스(BM25+Dense) 구축")
    p_index.add_argument("--scope", default="all",
                         help="all(기본)|ordinance|statute|sig:XXXXX|region:ID (쉼표 다중)")
    p_index.add_argument("--force", action="store_true", help="증분 대신 전체 재빌드")
    p_index.add_argument("--index-dir", dest="index_dir", help="인덱스 루트(기본 <out>/index)")
    p_index.add_argument("--no-community", dest="no_community", action="store_true",
                         help="커뮤니티 요약 리포트 생성 생략")
    p_index.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 경로 override")

    p_search = sub.add_parser("search", help="CLI 검색(GraphRAG 하이브리드/그래프확장)")
    p_search.add_argument("query", nargs="+", help="검색어(자연어)")
    p_search.add_argument("--mode", default="hybrid",
                          help="hybrid(기본)|bm25|dense|graph|context|global")
    p_search.add_argument("-k", "--k", dest="k", type=int, default=10, help="반환 건수")
    p_search.add_argument("--scope", default="all", help="인덱스 scope(기본 all)")
    p_search.add_argument("--hops", type=int, default=1, help="그래프 확장 홉(기본 1)")
    p_search.add_argument("--graph-weight", dest="graph_weight", type=float, default=0.5,
                          help="mode=graph 의 그래프 랭크 가중(기본 0.5 — 실측 최적. "
                               "1.0 은 대칭 RRF 로 MRR 0.900→0.767 하락)")
    p_search.add_argument("--index-dir", dest="index_dir", help="인덱스 루트")
    p_search.add_argument("--json", action="store_true", help="JSON 원본 출력")
    p_search.add_argument("--db", default=argparse.SUPPRESS, help="SQLite 경로 override")

    return ap


_DISPATCH: dict[str, Callable] = {
    "init": _cmd_init,
    "collect": _cmd_collect,
    "parse": _cmd_parse,
    "build": _cmd_build,
    "export": _cmd_export,
    "refresh": _cmd_refresh,
    "mcp": _cmd_mcp,
    "serve": _cmd_mcp,
    "seed": _cmd_seed,
    "seed-regions": _cmd_seed_regions,
    "status": _cmd_status,
    "neural": _cmd_neural,
    "index": _cmd_index,
    "search": _cmd_search,
}


def main(argv: Optional[list[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    cfg = _config.get_config()
    # --db override(전역 또는 서브커맨드) → 캐시된 Config 에 반영(워커도 공유).
    db_override = getattr(args, "db", None)
    if db_override:
        cfg.db_path = db_override
    handler = _DISPATCH.get(args.cmd)
    if handler is None:
        ap.error(f"알 수 없는 명령: {args.cmd}")
        return 2
    try:
        return int(handler(cfg, args) or 0)
    except KeyboardInterrupt:
        log.warning("중단됨(KeyboardInterrupt)")
        return 130
    except Exception as exc:  # 최상위 안전망
        log.error("치명적 오류: %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
