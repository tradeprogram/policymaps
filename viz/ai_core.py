"""Shared policy-map agent core for local and serverless chat endpoints."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemini-3.6-flash"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

SYSTEM_PROMPT = """너는 '자치법규 정책지도.agent'다. 자치법규 정책지도 사이트 안에서 지자체 실무자의 의사결정을 돕는다.

반드시 지킬 규칙:
- 한국어로 답한다.
- 조례-예산 연결은 verified=1만 '확인됨'이라고 말하고, 나머지는 반드시 '추정 연결'이라고 말한다.
- 폐지 조례는 선례로 추천하지 않는다. 폐지 사례는 위험 신호로만 설명한다.
- 수치 판단에는 as_of_date 기준을 함께 언급한다.
- 데이터에 없는 내용은 추정하지 말고 '현재 화면 데이터로는 확인 불가'라고 말한다.
- 답변은 발표자가 바로 읽을 수 있을 만큼 충분히 설명한다. 핵심 판단, 근거 수치, 주의할 한계, 다음 화면 행동을 함께 제안한다.
"""

ACTIONS = {
    "gap": {"label": "격차분석", "path": "/gap"},
    "effectiveness": {"label": "실효성", "path": "/effectiveness"},
    "diffusion": {"label": "정책 확산", "path": "/diffusion"},
    "graph": {"label": "법령 위계", "path": "/graph"},
    "search": {"label": "검색", "path": "/search"},
    "region": {"label": "지역 상세", "path": "/region/47190"},
}


def handle_chat(payload: dict) -> dict:
    load_env_files()
    agent = run_agent(payload)
    try:
        llm = call_gemini(payload, agent)
        answer = llm.get("answer") or local_answer(agent)
        model = llm.get("model")
        error = None
    except Exception as exc:  # noqa: BLE001 - endpoint returns structured fallback
        answer = local_answer(agent)
        model = None
        error = {"message": str(exc)[:500], "type": type(exc).__name__}
    out = {
        "answer": answer,
        "actions": agent["actions"],
        "evidence": agent["evidence"],
        "tool_trace": agent["tool_trace"],
        "handoff": agent.get("handoff"),
    }
    if model:
        out["model"] = model
    if error:
        out["error"] = error["type"]
        out["detail"] = error["message"]
    return out


def run_agent(payload: dict) -> dict:
    message = str(payload.get("message", ""))[:3000]
    route = str(payload.get("route") or "")
    mode = ((payload.get("context") or {}).get("data_mode") or "real").lower()
    base = data_root(mode)
    manifest = read_json(base / "manifest.json") or {}
    sig = infer_region(message, route, payload.get("context") or {})
    plan = plan_tools(message)
    tool_trace: list[dict] = []
    evidence: list[dict] = []
    ctx = {
        "question": message,
        "route": route,
        "data_mode": "mock" if manifest.get("_mock") else mode,
        "as_of_date": manifest.get("as_of_date"),
        "intent": infer_intent(message),
        "region": None,
        "gap": None,
        "peers": None,
        "diffusion": None,
        "effectiveness": None,
        "search": None,
        "policy_keyword": infer_policy_keyword(message),
        "rules": [
            "verified=1만 확인됨",
            "나머지 조례-예산 연결은 추정 연결",
            "폐지 조례는 선례 추천 금지",
            "as_of_date 기준 표시",
        ],
    }

    def record(tool: str, status: str, detail: str = "") -> None:
        tool_trace.append({"tool": tool, "status": status, "detail": detail})

    ctx["region"] = summarize_region(base, sig)
    record("load_region_profile", "ok" if ctx["region"] else "missing", sig)

    if "gap" in plan:
        env = read_json(base / "api" / "gap.json")
        ctx["gap"] = summarize_gap(env, sig, ctx["policy_keyword"])
        record("load_gap_fixture", "ok" if ctx["gap"] else "missing", f"sig_cd={sig}")
        if ctx["gap"]:
            evidence.append({"title": f"{ctx['gap'].get('target') or sig} 격차분석", "kind": "gap", "as_of_date": ctx["gap"].get("as_of_date")})

    if "peers" in plan:
        env = read_json(base / "api" / "peers.json")
        ctx["peers"] = summarize_peers(env, sig)
        record("load_peers_fixture", "ok" if ctx["peers"] else "missing", f"sig_cd={sig}")

    if "diffusion" in plan:
        env = read_json(base / "api" / "diffusion.json")
        ctx["diffusion"] = summarize_diffusion(env)
        record("load_diffusion_fixture", "ok" if ctx["diffusion"] else "missing")
        if ctx["diffusion"]:
            evidence.append({"title": f"{ctx['diffusion'].get('template')} 확산", "kind": "diffusion", "as_of_date": ctx["diffusion"].get("as_of_date")})

    if "effectiveness" in plan:
        env = read_json(base / "api" / "effectiveness.json")
        ctx["effectiveness"] = summarize_effectiveness(env)
        record("load_effectiveness_fixture", "ok" if ctx["effectiveness"] else "missing")
        if ctx["effectiveness"]:
            evidence.append({"title": "조례-예산 실효성", "kind": "effectiveness", "as_of_date": ctx["effectiveness"].get("as_of_date")})

    if "search" in plan:
        env = read_json(base / "api" / "search.json")
        ctx["search"] = summarize_search(env)
        record("load_search_fixture", "ok" if ctx["search"] else "missing")
        if ctx["search"]:
            evidence.append({"title": f"검색: {ctx['search'].get('query')}", "kind": "search", "as_of_date": ctx["search"].get("as_of_date")})

    actions = suggest_actions(plan, sig, ctx)
    return {"context": ctx, "actions": actions, "evidence": evidence[:5], "tool_trace": tool_trace, "handoff": build_handoff(ctx, plan, sig)}


def call_gemini(payload: dict, agent: dict) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 환경변수가 없습니다.")
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    prompt = {
        "instruction": SYSTEM_PROMPT,
        "user_question": str(payload.get("message", ""))[:3000],
        "current_route": payload.get("route"),
        "agent_context": agent["context"],
        "tool_trace": agent["tool_trace"],
        "recent_history": (payload.get("history") or [])[-8:],
        "output_format": "한국어 순수 텍스트. 보통 5~8개 짧은 문단, 약 900~1300자 분량으로 답한다. 첫 문단은 결론, 중간 문단은 근거 수치와 비교 선례, 마지막 문단은 주의사항과 다음 화면 행동을 담는다. 마크다운 기호는 쓰지 않는다.",
    }
    req_body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4096},
    }
    req = Request(
        f"{GEMINI_BASE}/models/{model}:generateContent",
        data=json.dumps(req_body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urlopen(req, timeout=45) as res:
        data = json.loads(res.read().decode("utf-8"))
    return {"answer": extract_text(data), "model": model}


def load_env_files() -> None:
    for path in (ROOT / ".env", ROOT / "system" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def data_root(mode: str) -> Path:
    candidates = []
    if mode == "mock":
        candidates = [ROOT / "viz" / "public" / "data", ROOT / "system" / "data"]
    else:
        candidates = [ROOT / "system" / "data", ROOT / "viz" / "public" / "data"]
    for path in candidates:
        if (path / "manifest.json").exists():
            return path
    return candidates[0]


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def infer_region(question: str, route: str, client_context: dict) -> str:
    if re.search(r"구미|47190", question):
        return "47190"
    m = re.search(r"/region/(\d+)", route or "")
    if m:
        return m.group(1)
    sig = (((client_context or {}).get("region") or {}).get("sig_cd")
           or ((client_context or {}).get("gap") or {}).get("target_sig_cd"))
    return str(sig or "47190")


def infer_policy_keyword(question: str) -> str | None:
    for kw in ("맨발걷기", "청년 월세", "출산장려", "반려동물", "미세먼지"):
        if kw in question:
            return kw
    m = re.search(r"([가-힣A-Za-z0-9 ]{2,20})(?: 조례| 정책| 지원)", question)
    return m.group(1).strip() if m else None


def plan_tools(question: str) -> list[str]:
    q = question.lower()
    plan = ["gap", "peers"]
    if any(k in q for k in ("확산", "채택", "추세", "s곡선", "맨발")):
        plan.append("diffusion")
    if any(k in q for k in ("예산", "실효", "집행", "근거", "맨발")):
        plan.append("effectiveness")
    if any(k in q for k in ("검색", "조문", "원문", "법령")):
        plan.append("search")
    return list(dict.fromkeys(plan))


def pick_fixture_data(env: dict | None, sig: str):
    data = (env or {}).get("data")
    if not data:
        return None
    if any(k in data for k in ("target", "recommendations", "peers")):
        return data
    return data.get(sig) or data.get(str(sig)) or data.get(next(iter(data), ""), None)


def summarize_region(base: Path, sig: str):
    doc = read_json(base / "regions" / f"{sig}.json")
    d = (doc or {}).get("data") or doc
    if not d:
        return None
    counts = d.get("counts") or {}
    budget = d.get("budget") or d.get("budget_summary") or {}
    return {
        "name": d.get("name") or d.get("region_name"),
        "sig_cd": d.get("sig_cd") or d.get("region_id") or sig,
        "level": d.get("level"),
        "ordinance_count": counts.get("ordinances") or counts.get("ordinance") or d.get("ordinance_count"),
        "population": d.get("population") or (d.get("indicators") or {}).get("population"),
        "budget_now": budget.get("budget_now") or budget.get("total_budget_now"),
        "as_of_date": (doc or {}).get("as_of_date") or d.get("as_of_date"),
    }


def summarize_peers(env: dict | None, sig: str):
    d = pick_fixture_data(env, sig)
    if not d:
        return None
    return {
        "target": (d.get("target") or {}).get("name"),
        "k": d.get("k") or len(d.get("peers") or []),
        "peers": [p.get("name") or p.get("region_id") for p in (d.get("peers") or [])[:6]],
        "as_of_date": (env or {}).get("as_of_date"),
    }


def summarize_gap(env: dict | None, sig: str):
    d = pick_fixture_data(env, sig)
    if not d:
        return None
    return {
        "target": (d.get("target") or {}).get("name"),
        "peer_pool_size": d.get("peer_pool_size"),
        "my_policy_count": d.get("my_policy_count"),
        "recommendations": [{
            "policy": r.get("policy_key"),
            "peer_count": r.get("peer_count"),
            "peer_share": r.get("peer_share"),
            "repealed_peer_count": r.get("repealed_peer_count") or 0,
            "likely_variant_of_mine": bool(r.get("likely_variant_of_mine")),
            "active_examples": [
                f"{p.get('name')} {p.get('ordinance_name') or ''}".strip()
                for p in (r.get("peers") or [])[:3]
            ],
        } for r in (d.get("recommendations") or [])[:6]],
        "as_of_date": (env or {}).get("as_of_date"),
    }


def summarize_diffusion(env: dict | None):
    d = (env or {}).get("data")
    if not d:
        return None
    last = (d.get("curve") or [{}])[-1]
    return {
        "template": d.get("template"),
        "universe": d.get("universe"),
        "adopters": d.get("adopters"),
        "final_adoption_rate": d.get("final_adoption_rate"),
        "latest_year": last.get("year"),
        "latest_cumulative": last.get("cumulative"),
        "rogers_stage": (d.get("rogers") or {}).get("stage") or d.get("stage"),
        "as_of_date": (env or {}).get("as_of_date"),
    }


def summarize_effectiveness(env: dict | None):
    d = (env or {}).get("data")
    if not d:
        return None
    t, v = d.get("totals") or {}, d.get("verification") or {}
    return {
        "link_count": d.get("link_count"),
        "budget_lines": d.get("budget_lines"),
        "verified_links": v.get("verified_links"),
        "auto_links": v.get("auto_links"),
        "precision_sample": "표본 584건 전체 64.9%, confidence>=0.8 구간 93.2%",
        "budget_now": t.get("budget_now"),
        "exe_amt": t.get("exe_amt"),
        "exec_rate_vs_now": t.get("exec_rate_vs_now"),
        "top_ordinances": [{
            "name": o.get("name"),
            "status": o.get("status"),
            "lines": o.get("lines"),
            "verification_status": o.get("verification_status"),
            "budget_now": o.get("budget_now"),
        } for o in (d.get("by_ordinance") or [])[:5]],
        "as_of_date": (env or {}).get("as_of_date"),
    }


def summarize_search(env: dict | None):
    d = (env or {}).get("data")
    if not d:
        return None
    return {
        "query": d.get("query"),
        "count": d.get("count"),
        "results": [{
            "name": r.get("parent_name"),
            "org": r.get("org_name"),
            "status": r.get("status"),
            "verified": r.get("verified"),
            "article": " ".join(x for x in (r.get("article_no"), r.get("article_title")) if x),
        } for r in (d.get("results") or [])[:5]],
        "as_of_date": (env or {}).get("as_of_date"),
    }


def suggest_actions(plan: list[str], sig: str) -> list[dict]:
    ids = []
    for tool in plan:
        if tool in ("gap", "peers"):
            ids.append("gap")
        elif tool == "effectiveness":
            ids.append("effectiveness")
        elif tool == "diffusion":
            ids.append("diffusion")
        elif tool == "search":
            ids.append("search")
    ids.append("region")
    out = []
    for action_id in dict.fromkeys(ids):
        action = dict(ACTIONS[action_id])
        if action_id == "region":
            action["path"] = f"/region/{sig}"
        out.append(action)
    return out[:5]


def local_answer(agent: dict) -> str:
    ctx = agent["context"]
    target = (ctx.get("gap") or {}).get("target") or (ctx.get("region") or {}).get("name") or "선택 지자체"
    parts = [f"{target} 기준으로 도구 {len(agent['tool_trace'])}개를 확인했습니다."]
    gap = ctx.get("gap") or {}
    recs = gap.get("recommendations") or []
    if recs:
        top = recs[0]
        parts.append(f"상위 후보는 「{top.get('policy')}」이고 유사 지자체 {top.get('peer_count')}곳이 보유, 폐지 사례는 {top.get('repealed_peer_count')}곳입니다.")
    eff = ctx.get("effectiveness")
    if eff:
        parts.append(f"예산 연결은 {eff.get('link_count')}건입니다. verified=1만 확인됨이고 나머지는 추정 연결로 말해야 합니다.")
    diff = ctx.get("diffusion")
    if diff:
        parts.append(f"확산 기준 「{diff.get('template')}」의 최종 채택률은 {percent(diff.get('final_adoption_rate'))}입니다.")
    parts.append(f"기준일은 {ctx.get('as_of_date') or '화면 표시 기준일'}입니다.")
    return "\n\n".join(parts)


def percent(v) -> str:
    try:
        return f"{float(v) * 100:.1f}%"
    except Exception:
        return "확인 불가"


def extract_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    parts = []
    for cand in candidates:
        for part in (cand.get("content") or {}).get("parts") or []:
            if isinstance(part.get("text"), str):
                parts.append(part["text"])
    if parts:
        text = "\n".join(parts).strip()
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"(?m)^\s*[*#-]\s+", "", text)
        return text.replace("**", "").strip()
    reason = (candidates[0].get("finishReason") if candidates else None) or "UNKNOWN"
    block = (data.get("promptFeedback") or {}).get("blockReason")
    hint = f"finishReason={reason}" + (f", blockReason={block}" if block else "")
    return f"AI 응답 본문이 비어 있습니다 ({hint})."


# The definitions below intentionally shadow the first-pass helpers above. They
# keep the demo useful when Gemini is unavailable and avoid repeating one generic
# answer for every policy question.
def infer_policy_keyword(question: str) -> str | None:
    q = question or ""
    for kw in ("맨발걷기", "청년 월세", "출산장려", "반려동물", "미세먼지"):
        if kw in q:
            return kw
    m = re.search(r"([가-힣A-Za-z0-9 ]{2,24})(?:\s*조례|\s*정책|\s*지원|\s*근거)", q)
    return m.group(1).strip() if m else None


def plan_tools(question: str) -> list[str]:
    q = question.lower()
    plan = ["gap", "peers"]
    if any(k in q for k in ("확산", "채택", "추세", "s곡선", "맨발")):
        plan.append("diffusion")
    if any(k in q for k in ("예산", "실효", "집행", "근거", "맨발")):
        plan.append("effectiveness")
    if any(k in q for k in ("검색", "조문", "원문", "법령", "근거")):
        plan.append("search")
    return list(dict.fromkeys(plan))


def _text_has(text, keyword: str | None) -> bool:
    if not keyword:
        return False
    return keyword.replace(" ", "") in str(text or "").replace(" ", "")


def summarize_gap(env: dict | None, sig: str, policy_keyword: str | None = None):
    d = pick_fixture_data(env, sig)
    if not d:
        return None
    raw_recs = d.get("recommendations") or []
    matched = [
        r for r in raw_recs
        if _text_has(r.get("policy_key"), policy_keyword)
        or any(_text_has(p.get("ordinance_name"), policy_keyword) for p in (r.get("peers") or []))
    ]
    ranked = (matched + [r for r in raw_recs if r not in matched])[:8]

    def pack(r: dict) -> dict:
        return {
            "policy": r.get("policy_key"),
            "peer_count": r.get("peer_count"),
            "peer_share": r.get("peer_share"),
            "repealed_peer_count": r.get("repealed_peer_count") or 0,
            "likely_variant_of_mine": bool(r.get("likely_variant_of_mine")),
            "active_examples": [
                f"{p.get('name')} {p.get('ordinance_name') or ''}".strip()
                for p in (r.get("peers") or [])[:3]
            ],
        }

    return {
        "target": (d.get("target") or {}).get("name"),
        "peer_pool_size": d.get("peer_pool_size"),
        "my_policy_count": d.get("my_policy_count"),
        "matched_policy": pack(matched[0]) if matched else None,
        "recommendations": [pack(r) for r in ranked],
        "repealed_candidates": [
            pack(r) for r in raw_recs
            if (r.get("repealed_peer_count") or 0) > 0
        ][:5],
        "as_of_date": (env or {}).get("as_of_date"),
    }


def local_answer(agent: dict) -> str:
    ctx = agent["context"]
    q = ctx.get("question") or ""
    target = (ctx.get("gap") or {}).get("target") or (ctx.get("region") or {}).get("name") or "선택 지자체"
    as_of = ctx.get("as_of_date") or "화면 표시 기준일"
    gap = ctx.get("gap") or {}
    recs = gap.get("recommendations") or []
    matched = gap.get("matched_policy")
    keyword = ctx.get("policy_keyword")

    if any(k in q for k in ("조문", "원문", "검색")):
        search = ctx.get("search") or {}
        query = search.get("query")
        if keyword and query and not _text_has(query, keyword):
            return (
                f"현재 정적 검색 fixture는 '{query}' 기준이라 '{keyword}' 조문 원문은 이 화면 데이터만으로는 확인하기 어렵습니다.\n\n"
                "발표에서는 검색 화면에서 같은 키워드로 재조회하거나, RAG 인덱스가 재생성된 환경에서 원문 근거를 붙이는 흐름으로 설명하는 편이 정확합니다.\n\n"
                f"기준일은 {as_of}입니다."
            )
        results = search.get("results") or []
        if results:
            lines = [f"{r.get('name') or r.get('org')}: {r.get('article') or '조문 제목 확인'}" for r in results[:3]]
            return f"검색 fixture에서 확인된 조문 후보입니다.\n\n" + "\n".join(lines) + f"\n\n기준일은 {as_of}입니다."
        return f"현재 화면 데이터에는 조문 원문 검색 결과가 없습니다. 검색 화면 또는 RAG 재색인 환경에서 확인해야 합니다.\n\n기준일은 {as_of}입니다."

    if any(k in q for k in ("없는 정책", "많이 갖고", "후보", "추천")) and recs:
        lines = []
        for r in recs[:3]:
            lines.append(f"{r.get('policy')}: 유사 지자체 {r.get('peer_count')}곳 보유, 폐지 사례 {r.get('repealed_peer_count')}곳")
        return (
            f"{target} 기준으로 유사 지자체에는 많고 우리 지역에는 없는 상위 후보는 다음과 같습니다.\n\n"
            + "\n".join(lines)
            + f"\n\n폐지 조례는 선례로 추천하지 않고 경고 신호로만 봐야 합니다. 기준일은 {as_of}입니다."
        )

    if keyword:
        parts = [f"{target} 기준 '{keyword}' 분석입니다."]
        if matched:
            parts.append(
                f"격차분석에서 '{matched.get('policy')}' 후보가 잡혔고, 유사 지자체 {matched.get('peer_count')}곳이 보유하며 폐지 사례는 {matched.get('repealed_peer_count')}곳입니다."
            )
            examples = matched.get("active_examples") or []
            if examples:
                parts.append("선례 예시는 " + ", ".join(examples[:3]) + "입니다.")
        elif recs:
            parts.append(f"격차분석 상위 후보에는 '{keyword}'가 직접 잡히지 않았습니다. 따라서 다른 후보와 섞어 말하지 않는 편이 정확합니다.")
        diff = ctx.get("diffusion")
        if diff:
            parts.append(f"확산 화면 기준 '{diff.get('template')}' 최종 채택률은 {percent(diff.get('final_adoption_rate'))}입니다.")
        eff = ctx.get("effectiveness")
        if eff:
            parts.append(f"조례-예산 연결은 {eff.get('link_count')}건입니다. verified=1만 확인됨이고 나머지는 추정 연결로 말해야 합니다.")
        parts.append("판정 흐름은 먼저 격차분석에서 유사 지자체 보유 여부와 폐지 사례를 확인하고, 그 다음 확산 화면에서 전국 채택률과 확산 단계를 본 뒤, 실효성 화면에서 예산 연결의 신뢰도를 확인하는 순서가 좋습니다.")
        parts.append("주의할 점은 조례-예산 연결을 확정 사실로 말하면 안 된다는 것입니다. verified=1은 확인됨으로 말할 수 있지만, 그 밖의 연결은 confidence 등급이 붙은 추정 연결로만 설명해야 합니다.")
        parts.append("다음 행동은 '격차분석 실행' 버튼으로 선례 조례를 먼저 보고, 이어서 '확산곡선 확인'과 '예산 연결 확인'을 눌러 발표 근거를 보강하는 것입니다.")
        parts.append(f"기준일은 {as_of}입니다.")
        return "\n\n".join(parts)

    parts = [f"{target} 기준으로 정책 도구 {len(agent['tool_trace'])}개를 확인했습니다."]
    if recs:
        top = recs[0]
        parts.append(f"가장 강한 격차 후보는 '{top.get('policy')}'이며 유사 지자체 {top.get('peer_count')}곳이 보유, 폐지 사례는 {top.get('repealed_peer_count')}곳입니다.")
    eff = ctx.get("effectiveness")
    if eff:
        parts.append(f"예산 연결은 {eff.get('link_count')}건입니다. verified=1만 확인됨이고 나머지는 추정 연결입니다.")
    parts.append(f"기준일은 {as_of}입니다.")
    return "\n\n".join(parts)


def infer_intent(question: str) -> str:
    q = question or ""
    if any(k in q for k in ("조문", "원문", "검색", "근거 찾아")):
        return "evidence_search"
    if any(k in q for k in ("없는 정책", "많이 갖고", "후보", "추천")):
        return "gap_recommendation"
    if any(k in q for k in ("예산", "실효", "집행", "효과")):
        return "effectiveness_review"
    if any(k in q for k in ("확산", "채택", "추세", "도입률", "s곡선", "맨발")):
        return "adoption_analysis"
    return "policy_brief"


def _path(path: str, **query) -> str:
    clean = {k: v for k, v in query.items() if v is not None and v != ""}
    return path + (("?" + urlencode(clean)) if clean else "")


def suggest_actions(plan: list[str], sig: str, ctx: dict | None = None) -> list[dict]:
    ctx = ctx or {}
    keyword = ctx.get("policy_keyword")
    intent = ctx.get("intent") or "policy_brief"
    matched = ((ctx.get("gap") or {}).get("matched_policy") or {}).get("policy") or keyword
    actions = []

    def add(label: str, path: str, kind: str, primary: bool = False, description: str | None = None) -> None:
        actions.append({
            "label": label,
            "path": path,
            "kind": kind,
            "primary": primary,
            "description": description,
        })

    add("격차분석 실행", _path("/gap", sig=sig, policy=matched), "gap",
        primary=intent in ("gap_recommendation", "adoption_analysis"),
        description="유사 지자체 보유 후보와 폐지 위험을 확인")
    if "diffusion" in plan or keyword:
        add("확산곡선 확인", _path("/diffusion", key=keyword), "diffusion",
            primary=intent == "adoption_analysis",
            description="전국 채택률과 확산 단계 확인")
    if "effectiveness" in plan or keyword:
        add("예산 연결 확인", _path("/effectiveness", sig=sig, policy=matched), "effectiveness",
            primary=intent == "effectiveness_review",
            description="verified=1과 추정 연결을 구분")
    if "search" in plan or intent == "evidence_search":
        add("조문 원문 검색", _path("/search", live=keyword or ctx.get("question")), "search",
            primary=intent == "evidence_search",
            description="원문/RAG 검색 화면으로 이동")
    add("지역 상세", f"/region/{sig}", "region", description="지역 지표와 보유 조례 확인")

    primary_seen = False
    out = []
    for action in actions:
        key = (action["label"], action["path"])
        if key in {(x["label"], x["path"]) for x in out}:
            continue
        if action.get("primary"):
            if primary_seen:
                action["primary"] = False
            primary_seen = True
        out.append(action)
    return out[:5]


def build_handoff(ctx: dict, plan: list[str], sig: str) -> dict:
    labels = {
        "gap": "격차분석",
        "peers": "유사 지자체",
        "diffusion": "정책 확산",
        "effectiveness": "조례-예산 실효성",
        "search": "조문 검색",
    }
    steps = []
    for tool in plan:
        status = "ok"
        if tool == "gap" and not ctx.get("gap"):
            status = "missing"
        elif tool == "peers" and not ctx.get("peers"):
            status = "missing"
        elif tool == "diffusion" and not ctx.get("diffusion"):
            status = "missing"
        elif tool == "effectiveness" and not ctx.get("effectiveness"):
            status = "missing"
        elif tool == "search" and not ctx.get("search"):
            status = "missing"
        steps.append({"label": labels.get(tool, tool), "status": status})
    return {
        "intent": ctx.get("intent"),
        "region_sig_cd": sig,
        "policy_keyword": ctx.get("policy_keyword"),
        "as_of_date": ctx.get("as_of_date"),
        "steps": steps,
        "next": next((a for a in suggest_actions(plan, sig, ctx) if a.get("primary")), None),
    }
