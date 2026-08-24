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
- 정책 도입 질문에는 반드시 다음 흐름을 포함한다: 판정, 근거, 위험, 예산/실효성, 다음 확인.
- panel_context가 있으면 해당 패널의 제목과 텍스트를 기준으로 초심자에게 용어와 읽는 법을 쉽게 설명한다.
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
    client_context = payload.get("context") or {}
    memory = client_context.get("agent_memory") or {}
    mode = (client_context.get("data_mode") or "real").lower()
    base = data_root(mode)
    manifest = read_json(base / "manifest.json") or {}
    sig = infer_region(message, route, client_context)
    plan = plan_tools(message)
    policy_keyword = infer_policy_keyword(message) or memory.get("policy_keyword")
    tool_trace: list[dict] = []
    evidence: list[dict] = []
    ctx = {
        "question": message,
        "route": route,
        "data_mode": "mock" if manifest.get("_mock") else mode,
        "as_of_date": manifest.get("as_of_date"),
        "intent": "panel_explain" if client_context.get("panel_context") else infer_intent(message, memory),
        "memory_used": bool(memory and (not infer_policy_keyword(message) or not explicit_region_mentioned(message, route))),
        "panel_context": client_context.get("panel_context"),
        "region": None,
        "gap": None,
        "peers": None,
        "diffusion": None,
        "effectiveness": None,
        "search": None,
        "policy_keyword": policy_keyword,
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
            evidence.append({
                "title": f"{ctx['gap'].get('target') or sig} 격차분석",
                "kind": "gap",
                "summary": gap_evidence_summary(ctx["gap"]),
                "as_of_date": ctx["gap"].get("as_of_date"),
            })

    if "peers" in plan:
        env = read_json(base / "api" / "peers.json")
        ctx["peers"] = summarize_peers(env, sig)
        record("load_peers_fixture", "ok" if ctx["peers"] else "missing", f"sig_cd={sig}")

    if "diffusion" in plan:
        env = read_json(base / "api" / "diffusion.json")
        ctx["diffusion"] = summarize_diffusion(env)
        record("load_diffusion_fixture", "ok" if ctx["diffusion"] else "missing")
        if ctx["diffusion"]:
            evidence.append({
                "title": f"{ctx['diffusion'].get('template')} 확산",
                "kind": "diffusion",
                "summary": diffusion_evidence_summary(ctx["diffusion"]),
                "as_of_date": ctx["diffusion"].get("as_of_date"),
            })

    if "effectiveness" in plan:
        env = read_json(base / "api" / "effectiveness.json")
        ctx["effectiveness"] = summarize_effectiveness(env)
        record("load_effectiveness_fixture", "ok" if ctx["effectiveness"] else "missing")
        if ctx["effectiveness"]:
            evidence.append({
                "title": "조례-예산 실효성",
                "kind": "effectiveness",
                "summary": effectiveness_evidence_summary(ctx["effectiveness"]),
                "as_of_date": ctx["effectiveness"].get("as_of_date"),
            })

    if "search" in plan:
        env = read_json(base / "api" / "search.json")
        ctx["search"] = summarize_search(env)
        record("load_search_fixture", "ok" if ctx["search"] else "missing")
        if ctx["search"]:
            evidence.append({
                "title": f"검색: {ctx['search'].get('query')}",
                "kind": "search",
                "summary": search_evidence_summary(ctx["search"]),
                "as_of_date": ctx["search"].get("as_of_date"),
            })

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
        "output_format": "한국어 순수 텍스트. 2~4개 짧은 문단, 300자 이내. 자연스러운 대화체로 쓴다. '판정:', '근거:', '위험:' 같은 고정 머리말 없이 흐르듯 이어지게. 마크다운 기호 금지.",
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
    if sig:
        return str(sig)
    memory_sig = ((client_context or {}).get("agent_memory") or {}).get("region_sig_cd")
    return str(memory_sig or "47190")


def explicit_region_mentioned(question: str, route: str) -> bool:
    return bool(re.search(r"구미|47190", question or "") or re.search(r"/region/\d+", route or ""))


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
    panel_context = ctx.get("panel_context") or {}

    if panel_context:
        return panel_explanation(ctx, panel_context)

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
        lines = [
            f"{r.get('policy')}: 유사 지자체 {r.get('peer_count')}곳 보유, 폐지 사례 {r.get('repealed_peer_count')}곳"
            for r in recs[:3]
        ]
        top = recs[0]
        return (
            f"판정: {target} 기준으로 우선 검토할 후보는 '{top.get('policy')}'입니다. 유사 지자체 보유 수가 가장 크고, 폐지 사례가 적을수록 발표에서 선례 근거로 쓰기 좋습니다.\n\n"
            "근거: 상위 후보는 다음과 같습니다.\n"
            + "\n".join(lines)
            + "\n\n위험: 폐지 조례는 선례로 추천하지 않고 위험 신호로만 봐야 합니다. 폐지 사례가 있는 후보는 현행 조례 선례와 분리해서 말해야 합니다."
            + "\n\n다음 확인: 격차분석에서 후보 카드를 먼저 확인한 뒤, 확산 화면에서 전국 채택률을 보고 실효성 화면에서 예산 연결 신뢰도를 확인하는 순서가 좋습니다."
            + f"\n\n기준일은 {as_of}입니다."
        )

    if "폐지" in q and keyword and matched:
        repealed = matched.get("repealed_peer_count") or 0
        examples = matched.get("active_examples") or []
        parts = [
            f"판정: {target} 기준 '{keyword}' 후보의 폐지 위험은 현재 fixture 기준 {'낮음' if repealed == 0 else '확인 필요'}입니다.",
            f"근거: 격차분석에서 잡힌 후보는 '{matched.get('policy')}'이고, 유사 지자체 {matched.get('peer_count')}곳이 현행 조례로 보유하고 있습니다.",
            f"위험: 폐지 사례는 {repealed}곳입니다. 폐지 조례는 선례로 추천하지 않고 위험 신호로만 설명해야 합니다.",
        ]
        if examples:
            parts.append("현행 선례 예시는 " + ", ".join(examples[:3]) + "입니다.")
        parts.append("다음 확인: 폐지 사례가 0곳이라면 발표에서는 '폐지 위험 신호는 확인되지 않음'으로 말하고, 이어서 확산률과 예산 연결을 확인하면 됩니다.")
        parts.append(f"기준일은 {as_of}입니다.")
        return "\n\n".join(parts)

    if keyword:
        grade = adoption_grade(matched, ctx.get("diffusion"))
        parts = [f"판정: {target} 기준 '{keyword}' 조례 도입 가능성은 {grade}으로 볼 수 있습니다. 이 판정은 유사 지자체 보유 수, 폐지 사례, 전국 확산률, 예산 연결 신뢰도를 함께 본 실무 검토용 판단입니다."]
        if matched:
            parts.append(
                f"근거: 격차분석에서 '{matched.get('policy')}' 후보가 잡혔고, 유사 지자체 {matched.get('peer_count')}곳이 보유하며 폐지 사례는 {matched.get('repealed_peer_count')}곳입니다."
            )
            examples = matched.get("active_examples") or []
            if examples:
                parts.append("선례: 현행 조례 예시는 " + ", ".join(examples[:3]) + "입니다. 이 예시는 폐지 조례가 아니라 현행 선례로만 설명해야 합니다.")
        elif recs:
            parts.append(f"근거: 격차분석 상위 후보에는 '{keyword}'가 직접 잡히지 않았습니다. 따라서 다른 후보와 섞어 말하지 않는 편이 정확합니다.")
        diff = ctx.get("diffusion")
        if diff:
            parts.append(f"확산: 확산 화면 기준 '{diff.get('template')}' 최종 채택률은 {percent(diff.get('final_adoption_rate'))}입니다. 이 수치는 조례가 전국적으로 얼마나 보편화됐는지 보여주는 보조 근거입니다.")
        eff = ctx.get("effectiveness")
        if eff:
            parts.append(f"예산/실효성: 조례-예산 연결은 {eff.get('link_count')}건입니다. verified=1만 확인됨이고 나머지는 confidence 등급이 붙은 추정 연결로 말해야 합니다.")
        parts.append("위험: 폐지 조례를 선례처럼 말하거나 조례-예산 연결을 확정 사실처럼 말하면 정확성 감점 요소가 됩니다. 폐지 사례는 위험 신호, 추정 연결은 추정 연결로 분리해야 합니다.")
        parts.append("다음 확인: '격차분석 실행'으로 선례 조례를 먼저 보고, 이어서 '확산곡선 확인'과 '예산 연결 확인'을 눌러 발표 근거를 보강하는 흐름이 좋습니다.")
        parts.append(f"기준일은 {as_of}입니다.")
        return "\n\n".join(parts)

    parts = [f"판정: {target} 기준으로 정책 도구 {len(agent['tool_trace'])}개를 확인했습니다. 현재 질문이 특정 정책명을 포함하지 않아 상위 격차 후보 중심으로 요약합니다."]
    if recs:
        top = recs[0]
        parts.append(f"근거: 가장 강한 격차 후보는 '{top.get('policy')}'이며 유사 지자체 {top.get('peer_count')}곳이 보유, 폐지 사례는 {top.get('repealed_peer_count')}곳입니다.")
    eff = ctx.get("effectiveness")
    if eff:
        parts.append(f"예산/실효성: 예산 연결은 {eff.get('link_count')}건입니다. verified=1만 확인됨이고 나머지는 추정 연결입니다.")
    parts.append("다음 확인: 정책명을 넣어 다시 질문하면 격차, 확산, 실효성 근거를 하나의 도입 판단으로 묶어 답할 수 있습니다.")
    parts.append(f"기준일은 {as_of}입니다.")
    return "\n\n".join(parts)


def adoption_grade(matched: dict | None, diffusion: dict | None) -> str:
    score = 0
    if matched:
        peer_count = matched.get("peer_count") or 0
        repealed = matched.get("repealed_peer_count") or 0
        if peer_count >= 10:
            score += 2
        elif peer_count >= 5:
            score += 1
        if repealed == 0:
            score += 1
    if diffusion:
        rate = diffusion.get("final_adoption_rate")
        try:
            if float(rate) >= 0.5:
                score += 2
            elif float(rate) >= 0.25:
                score += 1
        except Exception:
            pass
    if score >= 4:
        return "높음"
    if score >= 2:
        return "보통"
    return "추가 확인 필요"


def panel_explanation(ctx: dict, panel_context: dict) -> str:
    title = panel_context.get("title") or "현재 패널"
    text = panel_context.get("text") or ""
    as_of = ctx.get("as_of_date") or "화면 표시 기준일"
    route = panel_context.get("route") or ctx.get("route") or ""
    lower = f"{title} {text} {route}".lower()

    if "그래프 구성" in title:
        return (
            "판정: 이 패널은 법령 위계 그래프에 어떤 종류의 점과 선이 들어있는지 보여주는 안내판입니다. 처음 보는 사용자는 여기서 그래프가 '무엇으로 이루어져 있는가'를 먼저 이해하면 됩니다.\n\n"
            "노드: Region은 지자체, Ordinance는 자치법규, LegalInstrument는 상위 법령, Bill은 의안, Legislator는 의원, Party는 정당, Category는 정책분야입니다. 즉 표 왼쪽은 그래프에 등장하는 대상의 종류이고, 오른쪽 숫자는 그 대상이 몇 개 있는지입니다.\n\n"
            "엣지: HAS_ORDINANCE는 지자체가 조례를 가진 관계, DELEGATED_FROM/SUBORDINATE_TO는 조례가 상위법에 근거하는 관계, CITES는 인용 관계, PROPOSED_BY는 의안 발의자 관계, VOTED는 표결 관계입니다. 심사위원에게는 '조례를 법령·의안·예산·지역과 연결한 근거망'이라고 말하면 이해가 빠릅니다.\n\n"
            "주의: 하단의 제외 엣지는 화면에 그리면 너무 복잡해지는 관계입니다. 제외됐다고 데이터가 없다는 뜻은 아니고, 이 화면에서 해석 가능한 관계만 보여주기 위해 필터링한 것입니다.\n\n"
            f"기준일은 {as_of}입니다."
        )
    if "정책분야" in title:
        return (
            "판정: 이 패널은 조례가 어떤 정책분야에 많이 분포하는지 보여줍니다. 막대가 길수록 해당 분야의 조례가 더 많이 등장한다는 뜻입니다.\n\n"
            "읽는 법: 행정·자치·의회, 재정·세무·회계, 복지·돌봄처럼 분야명이 붙어 있고, 괄호 안 코드는 내부 분류 코드입니다. 사용자는 코드를 외울 필요 없이 분야명과 상대적인 막대 길이를 보면 됩니다.\n\n"
            "발표 표현: '이 지역 또는 번들에서 자치법규가 어느 정책영역에 집중되어 있는지 보여주는 분포도'라고 설명하면 됩니다. 단, 이 값은 전국 전체 점수가 아니라 화면에 표시된 shard 또는 사전계산 범위의 합산일 수 있으므로 범위 문구를 같이 봐야 합니다.\n\n"
            f"기준일은 {as_of}입니다."
        )
    if "격차" in title or "유사" in title:
        return (
            "판정: 이 패널은 비슷한 지자체들이 갖고 있지만 기준 지자체에는 없는 조례 후보를 찾는 곳입니다. 실무자는 여기서 '우리도 검토할 만한 정책 후보'를 좁힐 수 있습니다.\n\n"
            "읽는 법: 유사 지자체 보유 수가 많고 폐지 사례가 적은 후보일수록 선례 근거가 강합니다. 다만 폐지 조례는 추천 선례가 아니라 위험 신호로만 봐야 합니다.\n\n"
            "발표 표현: '전화나 검색으로 찾던 유사 지자체 선례를 한 화면에서 후보화한다'고 말하면 좋습니다. 이후 확산 화면과 실효성 화면으로 넘어가 채택률과 예산 연결을 확인하면 판단이 더 단단해집니다.\n\n"
            f"기준일은 {as_of}입니다."
        )
    if "예산" in title or "실효" in title or "연결" in title:
        return (
            "판정: 이 패널은 조례와 예산 세부사업이 실제로 연결되는지 보는 곳입니다. 조례가 선언에 그치는지, 예산 집행 근거까지 보이는지 확인하는 단계입니다.\n\n"
            "읽는 법: verified=1은 확인됨으로 말할 수 있지만, 그 외 자동 연결은 confidence가 붙은 추정 연결입니다. 따라서 숫자가 크더라도 전부 확정 사실처럼 말하면 안 됩니다.\n\n"
            "발표 표현: '조례와 예산의 연결 가능성을 보여주되, 확인됨과 추정 연결을 분리해 과장하지 않는다'고 말하면 정확성 기준에 맞습니다.\n\n"
            f"기준일은 {as_of}입니다."
        )
    if "확산" in title or "채택" in title:
        return (
            "판정: 이 패널은 특정 조례가 전국 지자체로 얼마나 퍼졌는지 보는 곳입니다. 도입률과 연도별 채택 추이를 통해 정책이 초기 실험인지, 이미 보편화된 흐름인지 판단합니다.\n\n"
            "읽는 법: 최종 채택률은 전체 지자체 중 해당 조례를 채택한 비율이고, 연도별 곡선은 언제부터 확산이 빨라졌는지 보여줍니다. 최초 채택 지자체는 선례 조사 출발점으로 쓰면 됩니다.\n\n"
            "발표 표현: '구미시만의 감이 아니라 전국 채택 흐름에서 도입 타이밍을 판단한다'고 설명하면 좋습니다.\n\n"
            f"기준일은 {as_of}입니다."
        )
    return (
        f"판정: '{title}' 패널은 현재 화면의 데이터를 해석하기 위한 근거 패널입니다. 처음 쓰는 사용자는 제목, 표의 첫 번째 열, 숫자 열을 순서대로 보면 됩니다.\n\n"
        "읽는 법: 숫자는 단독으로 결론을 내리는 값이 아니라 다른 패널의 선례, 폐지 여부, 예산 연결과 함께 보는 근거입니다. 표 안의 영문 코드나 내부 명칭은 원천 데이터 관계명을 보존한 것이므로, 발표에서는 쉬운 한국어로 풀어 말하는 편이 좋습니다.\n\n"
        f"현재 패널에서 읽힌 일부 내용은 다음과 같습니다. {text[:260]}\n\n"
        f"기준일은 {as_of}입니다."
    )


def infer_intent(question: str, memory: dict | None = None) -> str:
    q = question or ""
    if any(k in q for k in ("조문", "원문", "검색", "근거 찾아")):
        return "evidence_search"
    if any(k in q for k in ("폐지", "위험", "없는 정책", "많이 갖고", "후보", "추천")):
        return "gap_recommendation"
    if any(k in q for k in ("예산", "실효", "집행", "효과")):
        return "effectiveness_review"
    if any(k in q for k in ("확산", "채택", "추세", "도입률", "s곡선", "맨발")):
        return "adoption_analysis"
    if memory and memory.get("policy_keyword") and len(q.strip()) <= 20:
        return memory.get("intent") or "policy_brief"
    return "policy_brief"


def gap_evidence_summary(gap: dict | None) -> str | None:
    if not gap:
        return None
    m = gap.get("matched_policy")
    if m:
        return f"유사 {m.get('peer_count')}곳 보유 · 폐지 {m.get('repealed_peer_count')}곳"
    recs = gap.get("recommendations") or []
    if recs:
        top = recs[0]
        return f"상위 후보 {top.get('policy')} · 유사 {top.get('peer_count')}곳"
    return None


def diffusion_evidence_summary(diff: dict | None) -> str | None:
    if not diff:
        return None
    rate = percent(diff.get("final_adoption_rate"))
    adopters = diff.get("adopters")
    universe = diff.get("universe")
    return f"최종 채택률 {rate}" + (f" · {adopters}/{universe}곳" if adopters is not None and universe else "")


def effectiveness_evidence_summary(eff: dict | None) -> str | None:
    if not eff:
        return None
    return f"예산 연결 {eff.get('link_count')}건 · verified {eff.get('verified_links')}건"


def search_evidence_summary(search: dict | None) -> str | None:
    if not search:
        return None
    return f"사전계산 질의 '{search.get('query')}' · 결과 {search.get('count')}건"


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
        "suggested_questions": suggest_followup_questions(ctx),
    }


def suggest_followup_questions(ctx: dict) -> list[str]:
    keyword = ctx.get("policy_keyword") or "맨발걷기"
    intent = ctx.get("intent")
    if intent == "gap_recommendation":
        return [
            f"{keyword} 폐지 사례와 위험 신호를 확인해줘",
            f"{keyword} 전국 확산률까지 연결해서 설명해줘",
            f"{keyword} 예산 연결 근거를 확인해줘",
        ]
    if intent == "effectiveness_review":
        return [
            "verified=1과 추정 연결을 발표 문장으로 구분해줘",
            f"{keyword} 도입 판단을 한 문단으로 요약해줘",
            "심사에서 오해받을 표현을 걸러줘",
        ]
    if intent == "evidence_search":
        return [
            f"{keyword} 선례 조례와 폐지 사례를 구분해줘",
            f"{keyword} 조문 근거를 발표용으로 요약해줘",
            f"{keyword} 예산 연결 근거도 이어서 확인해줘",
        ]
    return [
        f"{keyword} 폐지 위험은 낮은지 확인해줘",
        f"{keyword} 도입 근거를 발표용으로 정리해줘",
        f"{keyword} 예산 연결의 신뢰도를 확인해줘",
    ]
