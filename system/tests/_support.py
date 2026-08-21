"""tests._support — 테스트 공용 헬퍼(픽스처·시드·스킵·독립실행 러너).

밑줄 접두(_support)라 pytest 가 테스트로 수집하지 않는다.

설계 목표:
  * pytest 있으면 그대로, 없으면 순수 assert 스크립트로도 동작(run_dict).
  * 무거운 의존성(numpy/networkx/shapely) 요구 금지 — 있으면 활용되나 없어도 통과.
  * 병렬 구현 중인 모듈(collectors/parsers/graph/mcp_server/run)은 need()로
    부재 시 graceful skip. 계약(CONTRACTS.md) 시그니처에 맞춰 테스트만 선작성.

공개 헬퍼:
  fresh_db(seed=True)  : 인메모리 SQLite + 스키마 적용(+샘플 시드) 연결
  raw_db()             : 시드 없는 초기화 연결
  seed_reference(conn) : instrument_kind/categories/parties 통제어휘 시드
  seed_sample(conn)    : 일관된 미니월드(지역/법령/조례/위임/예산/의안/표결) 시드
  load_json/load_text  : fixtures/ 로더
  need(dotted, *attrs) : 모듈·속성 확보 or skip(병렬 미구현 우회)
  skip(reason)         : pytest.skip 우선, 없으면 Skipped 예외
  run_dict(ns, title)  : namespace 의 test_* 를 직접 실행(순수 assert 모드)
  graph_counts(g)      : networkx/폴백 그래프 공통 노드·엣지 카운트
"""
import csv
import importlib
import io
import json
import os
import sys
import traceback
from pathlib import Path

# --------------------------------------------------------------------------- #
# 경로 부트스트랩: policymap 패키지(=system 루트)와 tests 디렉터리를 sys.path 에.
# --------------------------------------------------------------------------- #
TESTS_DIR = Path(__file__).resolve().parent
SYSTEM_ROOT = TESTS_DIR.parent
FIX_DIR = TESTS_DIR / "fixtures"
for _p in (str(SYSTEM_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 코어는 항상 임포트 가능해야 한다(표준라이브러리만 의존).
from policymap import config as pm_config  # noqa: E402
from policymap import db as pm_db          # noqa: E402
from policymap import util as pm_util      # noqa: E402

# --------------------------------------------------------------------------- #
# 스킵 메커니즘 (pytest 유무 양립)
# --------------------------------------------------------------------------- #
try:
    import pytest as _pytest  # type: ignore
except Exception:  # pragma: no cover - pytest 미설치 폴백
    _pytest = None


class Skipped(Exception):
    """순수 assert 러너용 스킵 신호(pytest 부재 시)."""


def skip(reason: str = "") -> None:
    """pytest 있으면 정식 skip, 없으면 Skipped 예외.

    두 경우 모두 run_dict/pytest 가 '건너뜀'으로 집계한다.
    """
    if _pytest is not None:
        _pytest.skip(reason)
    raise Skipped(reason)


def need(dotted: str, *attrs: str):
    """병렬 구현 대상 모듈을 확보. 부재/속성 누락 시 skip.

    예: mod = need('policymap.parsers.article', 'parse_law_articles')
    """
    try:
        mod = importlib.import_module(dotted)
    except Exception as exc:  # ImportError 및 모듈 내부 임포트 실패 모두
        skip(f"{dotted} 미구현/임포트 실패: {exc}")
        return None  # pragma: no cover
    missing = [a for a in attrs if not hasattr(mod, a)]
    if missing:
        skip(f"{dotted}: 미구현 심볼 {missing}")
    return mod


# --------------------------------------------------------------------------- #
# 픽스처 로더
# --------------------------------------------------------------------------- #
def load_json(name: str):
    return json.loads((FIX_DIR / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIX_DIR / name).read_text(encoding="utf-8")


def load_csv_rows(name: str) -> list[dict]:
    with (FIX_DIR / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------- #
# DB 헬퍼
# --------------------------------------------------------------------------- #
def raw_db():
    """스키마만 적용한 빈 인메모리 연결."""
    conn = pm_db.connect(":memory:")
    pm_db.init_db(conn)
    return conn


def fresh_db(seed: bool = True):
    """스키마 + (선택)샘플 시드 인메모리 연결."""
    conn = raw_db()
    if seed:
        seed_reference(conn)
        seed_sample(conn)
    return conn


def seed_reference(conn) -> None:
    """통제어휘(instrument_kind/categories/parties) 시드. FK 선행 요건 충족용."""
    kinds = [
        {"kind": "헌법", "source_type": "constitution", "national_tier": 0},
        {"kind": "법률", "source_type": "statute", "national_tier": 1},
        {"kind": "대통령령", "source_type": "statute", "national_tier": 2},
        {"kind": "부령", "source_type": "statute", "national_tier": 3},
        {"kind": "행정규칙", "source_type": "admin-rule", "national_tier": 4},
        {"kind": "조례", "source_type": "ordinance", "local_tier": "L1"},
        {"kind": "규칙", "source_type": "ordinance", "local_tier": "L2"},
    ]
    for k in kinds:
        pm_db.upsert(conn, "instrument_kind", k, "kind")

    cats = [
        {"code": "C01", "name": "교통ㆍ주차", "keywords": json.dumps(["주차", "주차장", "교통", "도로"], ensure_ascii=False)},
        {"code": "C02", "name": "문화ㆍ도서관", "keywords": json.dumps(["도서관", "문화", "독서", "평생교육"], ensure_ascii=False)},
        {"code": "C03", "name": "복지ㆍ돌봄", "keywords": json.dumps(["복지", "돌봄", "아동", "노인"], ensure_ascii=False)},
    ]
    for c in cats:
        pm_db.upsert(conn, "categories", c, "code")

    for p in [
        {"party_id": "P_DEM", "name": "더불어민주당"},
        {"party_id": "P_PPP", "name": "국민의힘"},
    ]:
        pm_db.upsert(conn, "parties", p, "party_id")
    conn.commit()


def seed_sample(conn) -> None:
    """일관된 미니월드 시드. graph/mcp/smoke 공용.

    구성: 광역2(서울11/부산26) + 기초4, 인접2쌍, 법률2(주차장법/도서관법),
    조례3(부산진구 26170 는 주차장 조례 부재 → 위임격차 유도), 위임, 예산, 의안·표결.
    as_of_date = 오늘(신선도 불변식 통과용).
    """
    today = pm_util.today_kst()

    # 1) 지역 — 광역 먼저(parent FK)
    regions = [
        # region_id, region_cd, sig_cd, sido, sgg, name, full, level, parent, laf
        ("11", "1100000000", "11000", "11", "000", "서울특별시", "서울특별시", 1, None, "11000"),
        ("26", "2600000000", "26000", "26", "000", "부산광역시", "부산광역시", 1, None, "26000"),
        ("11110", "1111000000", "11110", "11", "110", "종로구", "서울특별시 종로구", 2, "11", "11110"),
        ("11140", "1114000000", "11140", "11", "140", "중구", "서울특별시 중구", 2, "11", "11140"),
        ("26110", "2611000000", "26110", "26", "110", "중구", "부산광역시 중구", 2, "26", "26110"),
        ("26170", "2617000000", "26170", "26", "170", "부산진구", "부산광역시 부산진구", 2, "26", "26170"),
    ]
    for rid, rcd, sig, sido, sgg, nm, full, lvl, parent, laf in regions:
        pm_db.upsert(conn, "regions", {
            "region_id": rid, "region_cd": rcd, "sig_cd": sig,
            "sido_cd": sido, "sgg_cd": sgg, "name": nm, "full_name": full,
            "level": lvl, "parent_region": parent, "has_legislation": 1,
            "vworld_sig_cd": sig, "law_org": sido, "law_sborg": (None if lvl == 1 else sig),
            "lofin_laf_cd": laf, "status": "active",
            "as_of_date": today, "source": "seed", "updated_at": today,
        }, "region_id")

    # 2) 인접(무방향 → 양방향 2행)
    adj_pairs = [("11110", "11140", 1), ("26110", "26170", 1)]
    for a, b, same in adj_pairs:
        for x, y in ((a, b), (b, a)):
            pm_db.upsert(conn, "region_adjacency", {
                "region_id": x, "neighbor_id": y, "contiguity_type": "queen",
                "same_province": same, "method": "loaded", "computed_at": today,
            }, ("region_id", "neighbor_id"))

    # 3) 국가법령(상위법)
    statutes = [
        ("statute:001234", "001234", "주차장법", 1),
        ("statute:005678", "005678", "도서관법", 1),
    ]
    for iid, mst, nm, tier in statutes:
        pm_db.upsert(conn, "legal_instrument", {
            "instrument_id": iid, "kind": "법률", "source_type": "statute",
            "national_tier": tier, "mst": mst, "law_id": mst, "name": nm,
            "competent_authority": "국토교통부" if nm == "주차장법" else "문화체육관광부",
            "enacted_on": "20230615", "effective_on": "20240101",
            "current_history": "현행", "status": "active",
            "official_url": f"https://www.law.go.kr/DRF/lawService.do?MST={mst}",
            "as_of_date": today, "content_hash": pm_util.content_hash(nm),
            "verification_status": "source-linked", "updated_at": today,
        }, "instrument_id")

    # 4) 자치법규(조례) — 26170(부산진구) 은 주차장 조례 없음(위임격차)
    ords = [
        ("ordin:9001", "9001", "11110", "서울특별시 종로구 주차장 설치 및 관리 조례", "law-delegated"),
        ("ordin:9002", "9002", "11140", "서울특별시 중구 주차장 설치 및 관리 조례", "law-delegated"),
        ("ordin:9003", "9003", "26110", "부산광역시 중구 공공도서관 설치 및 운영 조례", "law-delegated"),
    ]
    for oid, mst, rid, nm, dtype in ords:
        pm_db.upsert(conn, "ordinances", {
            "ordinance_id": oid, "mst": mst, "ord_id": f"ORD{mst}",
            "region_id": rid, "org_name": nm.split(" ")[0] + " " + nm.split(" ")[1],
            "name": nm, "ord_kind": "조례", "local_tier": "L1",
            "enacted_by": "지방의회", "delegation_type": dtype,
            "enacted_on": "20220310", "effective_on": "20220401",
            "rr_cls_cd": "300202", "status": "active",
            "official_url": f"https://www.law.go.kr/DRF/lawService.do?MST={mst}",
            "as_of_date": today, "content_hash": pm_util.content_hash(nm),
            "verification_status": "source-linked", "updated_at": today,
        }, "ordinance_id")

    # 4-1) 조례 조문 몇 개(임베딩/유사도 테스트용 텍스트)
    oa_rows = [
        ("ordin:9001::000100", "ordin:9001", "000100", "목적",
         "이 조례는 「주차장법」 제12조에 따라 종로구 주차장의 설치 및 관리에 필요한 사항을 규정한다."),
        ("ordin:9002::000100", "ordin:9002", "000100", "목적",
         "이 조례는 「주차장법」 제12조에 따라 중구 주차장의 설치 및 관리에 필요한 사항을 규정한다."),
        ("ordin:9003::000100", "ordin:9003", "000100", "목적",
         "이 조례는 「도서관법」 제6조에 따라 공공도서관의 설치 및 운영에 필요한 사항을 규정한다."),
    ]
    for oaid, oid, no, title, body in oa_rows:
        pm_db.upsert(conn, "ordinance_articles", {
            "oa_id": oaid, "ordinance_id": oid, "article_no": no,
            "title": title, "body": body,
            "content_hash": pm_util.content_hash(body), "updated_at": today,
        }, "oa_id")

    # 4-2) 카테고리 귀속
    for oid, code in [("ordin:9001", "C01"), ("ordin:9002", "C01"), ("ordin:9003", "C02")]:
        pm_db.upsert(conn, "ordinance_category", {
            "ordinance_id": oid, "category_code": code,
            "confidence": 0.9, "method": "rule", "computed_at": today,
        }, ("ordinance_id", "category_code"))

    # 5) 위임(하위 조례 → 상위 법률). 26110 도서관 조례도 도서관법 위임.
    delegs = [
        ("ordin:9001", "statute:001234", "제1조", "제12조", "lsDelegated"),
        ("ordin:9002", "statute:001234", "제1조", "제12조", "lnkLsOrdJo"),
        ("ordin:9003", "statute:005678", "제1조", "제6조", "lsDelegated"),
    ]
    for child, parent, ca, pa, path in delegs:
        did = pm_util.stable_id(child, parent, ca, pa, path)
        pm_db.upsert(conn, "delegations", {
            "delegation_id": did, "child_kind": "ordinance", "child_id": child,
            "child_article": ca, "parent_kind": "instrument", "parent_id": parent,
            "parent_article": pa, "relation": "DELEGATED_FROM",
            "delegation_type": "law-delegated", "source_path": path,
            "verification_status": "source-linked", "computed_at": today,
        }, "delegation_id")

    # 6) 예산
    budgets = [
        ("ehojo-11110-2026-0001", 2026, "11110", "11110", "D0001", "주차장 설치 및 관리", 1000000, 800000, 500000),
        ("ehojo-26110-2026-0001", 2026, "26110", "26110", "D0101", "공공도서관 운영", 780000, 500000, 410000),
    ]
    for bid, fyr, laf, rid, dbiz, nm, now_amt, sgg_fund, exe in budgets:
        pm_db.upsert(conn, "budget_lines", {
            "budget_id": bid, "fyr": fyr, "laf_cd": laf, "region_id": rid,
            "dbiz_cd": dbiz, "dbiz_nm": nm, "field": "교통및물류",
            "budget_now": now_amt, "sigungu_fund": sgg_fund, "alloc_amt": now_amt,
            "exe_amt": exe, "exe_ymd": "20260630", "as_of_date": today, "updated_at": today,
        }, "budget_id")

    # 7) 국회 의안·의원·표결
    for L in [
        {"legislator_id": "M001", "mona_cd": "M001", "name": "홍길동",
         "current_party": "P_DEM", "district": "서울 종로", "elect_type": "지역구"},
        {"legislator_id": "M002", "mona_cd": "M002", "name": "김철수",
         "current_party": "P_PPP", "district": "부산 부산진", "elect_type": "지역구"},
    ]:
        pm_db.upsert(conn, "legislators", L, "legislator_id")

    pm_db.upsert(conn, "bills", {
        "bill_id": "PRC_0001", "bill_no": "2200001", "age": 22,
        "name": "주차장법 일부개정법률안", "committee": "국토교통위원회",
        "propose_dt": "20260101", "proc_dt": "20260210",
        "proc_result": "원안가결", "proc_result_cd": "가결",
        "member_tcnt": 300, "vote_tcnt": 250, "yes_tcnt": 200, "no_tcnt": 30, "blank_tcnt": 20,
        "enacted_instrument_id": "statute:001234", "enact_match_method": "법령명+공포일",
        "enact_verified": 0, "content_hash": pm_util.content_hash("PRC_0001|가결"),
        "updated_at": today,
    }, "bill_id")

    for leg, role in [("M001", "RST"), ("M002", "PUBL")]:
        pm_db.upsert(conn, "bill_proposers", {
            "bill_id": "PRC_0001", "legislator_id": leg, "role": role,
        }, ("bill_id", "legislator_id", "role"))

    for leg, vote, party, pcd in [
        ("M001", "찬성", "더불어민주당", "P_DEM"),
        ("M002", "반대", "국민의힘", "P_PPP"),
    ]:
        pm_db.upsert(conn, "votes", {
            "bill_id": "PRC_0001", "legislator_id": leg, "result_vote_mod": vote,
            "vote_date": "20260209", "party_at_vote": party, "party_cd_at_vote": pcd,
        }, ("bill_id", "legislator_id"))

    conn.commit()


# --------------------------------------------------------------------------- #
# 그래프 공통 카운트(networkx / 폴백 dict 모두 대응)
# --------------------------------------------------------------------------- #
def graph_counts(g) -> tuple[int, int]:
    """(노드수, 엣지수). networkx Graph 또는 폴백(dict/속성) 그래프 공통."""
    # networkx 계열
    if hasattr(g, "number_of_nodes") and hasattr(g, "number_of_edges"):
        return g.number_of_nodes(), g.number_of_edges()
    # 폴백: .nodes/.edges 가 컬렉션
    nodes = getattr(g, "nodes", None)
    edges = getattr(g, "edges", None)
    if nodes is not None and edges is not None:
        n = nodes() if callable(nodes) else nodes
        e = edges() if callable(edges) else edges
        return len(list(n)), len(list(e))
    # 폴백: dict 형태 {'nodes':..., 'edges':...}
    if isinstance(g, dict):
        return len(g.get("nodes", []) or []), len(g.get("edges", []) or [])
    raise AssertionError(f"알 수 없는 그래프 표현: {type(g)!r}")


# --------------------------------------------------------------------------- #
# 순수 assert 러너 (pytest 미설치 시)
# --------------------------------------------------------------------------- #
def run_dict(ns: dict, title: str | None = None) -> int:
    """namespace 의 test_* 함수를 직접 실행. 실패 있으면 1 반환.

    각 test_* 는 인자 없는 함수라는 계약(픽스처는 fresh_db 등 헬퍼로 대체).
    """
    names = sorted(k for k, v in ns.items() if k.startswith("test_") and callable(v))
    npass = nfail = nskip = 0
    fails: list[str] = []
    for name in names:
        try:
            ns[name]()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 - 러너는 모든 결과를 분류
            cls = type(exc).__name__
            if isinstance(exc, Skipped) or "Skip" in cls:
                print(f"  SKIP {name}: {exc}")
                nskip += 1
            elif isinstance(exc, AssertionError):
                print(f"  FAIL {name}: {exc}")
                fails.append(name)
                nfail += 1
            else:
                traceback.print_exc()
                print(f"  ERROR {name}: {cls}: {exc}")
                fails.append(name)
                nfail += 1
        else:
            print(f"  PASS {name}")
            npass += 1
    label = title or ns.get("__name__", "tests")
    print(f"[{label}] pass={npass} fail={nfail} skip={nskip}")
    if fails:
        print(f"  실패: {', '.join(fails)}")
    return 1 if nfail else 0


__all__ = [
    "SYSTEM_ROOT", "TESTS_DIR", "FIX_DIR",
    "pm_config", "pm_db", "pm_util",
    "Skipped", "skip", "need",
    "load_json", "load_text", "load_csv_rows",
    "raw_db", "fresh_db", "seed_reference", "seed_sample",
    "graph_counts", "run_dict",
]
