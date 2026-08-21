"""policymap.parsers.category — 조례명·목적 → C01~C12 통제어휘 분류.

CONTRACTS.md §2.3 계약:
    classify_ordinance(ordinance, articles, categories) -> list[dict]
    save_categories(conn, ordinance_id, rows) -> dict[str,int]

설계:
  * 1차 룰기반: categories.keywords(JSON 배열) 앵커를 조례명·목적문에 매칭.
    - 조례명 매칭은 가중치 2.0, 목적문 매칭 1.5, 기타 조문 매칭 1.0(제목이 곧 정책분야 신호).
    - confidence = min(1.0, score / max(3.0, len(keywords)))  — 키워드 많은 분류의 과소평가 방지.
  * 선택적 LLM 훅(llm=callable): 있으면 그 결과를 method='llm' 로 반환(폴백 없이 우선).
  * 순수함수(classify_ordinance) → 픽스처(카테고리 사전 주입)로 단위테스트 가능.

categories 행 형태(스키마 categories): {code, name, level, parent_code, definition, keywords}.
keywords 는 JSON 문자열('["가로수","조경"]') 또는 파이썬 list 둘 다 수용.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable, Optional

from .. import db as _db
from .. import util as _util

_WS_RE = re.compile(r"\s+")

# 가중치(매칭 위치별 신호 강도)
_W_NAME = 2.0
_W_PURPOSE = 1.5
_W_BODY = 1.0
_CONF_FLOOR = 3.0  # 정규화 분모 하한(키워드 소수 분류가 소수 강매칭으로 과대평가되지 않게)


def _norm(text: Any) -> str:
    """소문자화 없이 공백만 단일화(한글은 대소문자 무관). 부분문자열 매칭 대상."""
    return _WS_RE.sub(" ", str(text or "")).strip()


def _load_keywords(cat: dict) -> list[str]:
    """categories.keywords → list[str]. JSON 문자열/리스트/None 방어."""
    kw = cat.get("keywords")
    if kw is None:
        return []
    if isinstance(kw, str):
        kw = kw.strip()
        if not kw:
            return []
        try:
            parsed = json.loads(kw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except (json.JSONDecodeError, ValueError):
            # JSON 아니면 쉼표 구분 폴백
            return [p.strip() for p in kw.split(",") if p.strip()]
        return []
    if isinstance(kw, list):
        return [str(x).strip() for x in kw if str(x).strip()]
    return []


def _find_purpose(articles: Iterable[dict]) -> str:
    """목적 조문(제목에 '목적' 포함) 본문 추출. 없으면 첫 조문 본문 폴백."""
    first_body = ""
    for a in _util.as_list(list(articles)):
        if not isinstance(a, dict):
            continue
        body = _norm(a.get("body"))
        if not first_body and body:
            first_body = body
        title = str(a.get("title") or "")
        if "목적" in title:
            return body or _norm(a.get("title"))
    return first_body


def classify_ordinance(
    ordinance: dict,
    articles: list[dict],
    categories: list[dict],
    *,
    llm: Optional[Callable[[str, list[dict]], list[dict]]] = None,
    top_k: Optional[int] = None,
) -> list[dict]:
    """조례 1건을 C01~C12 로 분류. 룰기반 1차(+ 선택적 LLM).

    반환 [{'category_code','confidence','method'}], confidence 내림차순.
    매칭 0건이면 빈 리스트. top_k 지정 시 상위 k개만.

    llm(text, categories) 제공 시 그 결과(각 {category_code, confidence})를 method='llm' 로 반환.
    """
    name = _norm(ordinance.get("name"))
    field = _norm(ordinance.get("category_field"))  # 지자체 내부 분류명(보조 신호)
    purpose = _find_purpose(articles)
    # 기타 조문(제목+본문) 합본
    body_parts: list[str] = []
    for a in _util.as_list(list(articles)):
        if isinstance(a, dict):
            body_parts.append(_norm(a.get("title")))
            body_parts.append(_norm(a.get("body")))
    body_text = " ".join(p for p in body_parts if p)
    full_text = " ".join(p for p in (name, field, purpose, body_text) if p)

    # --- 선택적 LLM 훅 ---
    if llm is not None:
        try:
            llm_rows = llm(full_text, categories) or []
        except Exception:  # noqa: BLE001 — LLM 실패는 룰 폴백
            llm_rows = []
        out = []
        for r in llm_rows:
            if isinstance(r, dict) and r.get("category_code"):
                out.append({
                    "category_code": r["category_code"],
                    "confidence": float(r.get("confidence") or 0.0),
                    "method": "llm",
                })
        if out:
            out.sort(key=lambda x: (-x["confidence"], x["category_code"]))
            return out[:top_k] if top_k else out
        # LLM 무응답 → 룰 폴백으로 진행

    # --- 룰기반 ---
    scored: list[dict] = []
    for cat in _util.as_list(list(categories)):
        if not isinstance(cat, dict):
            continue
        code = cat.get("code")
        keywords = _load_keywords(cat)
        if not code or not keywords:
            continue
        score = 0.0
        hits = 0
        for kw in keywords:
            if not kw:
                continue
            if kw in name:
                score += _W_NAME
                hits += 1
            elif kw in purpose or (field and kw in field):
                score += _W_PURPOSE
                hits += 1
            elif kw in body_text:
                score += _W_BODY
                hits += 1
        if hits <= 0:
            continue
        # 신뢰도는 어휘 크기와 무관해야 한다(포화 함수).
        # [수정] 구식 score/len(keywords) 는 키워드가 풍부한 분야를 부당하게 깎아
        # 조례명 1회 매칭(score 2.0)이 키워드 15개 분야에서 0.133 으로 떨어졌고,
        # 그 결과 전건 분류 커버리지가 13.3% 에 머물렀다. [실측]
        conf = score / (score + _CONF_FLOOR)
        scored.append({
            "category_code": code,
            "confidence": round(conf, 4),
            "method": "rule",
        })
    scored.sort(key=lambda x: (-x["confidence"], x["category_code"]))
    return scored[:top_k] if top_k else scored


def save_categories(conn, ordinance_id: str, rows: Iterable[dict]) -> dict[str, int]:
    """ordinance_category upsert. PK=(ordinance_id, category_code). computed_at 채움."""
    now = _util.now_kst_iso()
    prepared: list[dict] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("category_code"):
            continue
        prepared.append({
            "ordinance_id": ordinance_id,
            "category_code": r["category_code"],
            "confidence": r.get("confidence"),
            "method": r.get("method") or "rule",
            "computed_at": now,
        })
    if not prepared:
        return {"inserted": 0, "updated": 0, "unchanged": 0}
    with _db.tx(conn):
        return _db.upsert_many(conn, "ordinance_category", prepared,
                               ("ordinance_id", "category_code"))


__all__ = ["classify_ordinance", "save_categories"]
