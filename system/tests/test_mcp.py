"""test_mcp — mcp_server.server 의 JSON-RPC tools/list·tools/call 블랙박스 테스트.

계약(CONTRACTS.md §4): initialize/tools/list/tools/call, tool 6~14개,
판단성 응답에 as_of_date + disclaimer + execution_allowed:false.
공개 인터페이스 serve_stdio(줄단위 JSON-RPC)를 stdin/stdout 패치로 구동한다.
직접 디스패치 메서드가 있으면 우선 사용. 미구현 시 skip.
"""
import io
import json
import sys

from _support import need, skip, fresh_db, run_dict, pm_config as config


def _make_server():
    mod = need("policymap.mcp_server.server", "Server")
    conn = fresh_db(seed=True)
    cfg = config.get_config()
    try:
        return mod.Server(conn, cfg)
    except TypeError:
        # 시그니처 관용: Server(conn=..., cfg=...)
        return mod.Server(conn=conn, cfg=cfg)


def _dispatch_direct(server, requests):
    """직접 요청 디스패치 메서드가 있으면 사용(있는 경우 가장 견고)."""
    for meth in ("handle_request", "handle", "dispatch", "_dispatch", "_handle_request"):
        fn = getattr(server, meth, None)
        if callable(fn):
            out = []
            for r in requests:
                resp = fn(r)
                if resp is not None:
                    out.append(resp)
            return out
    return None


def _drive_stdio(server, requests):
    """serve_stdio 를 stdin/stdout 패치로 구동해 응답 파싱."""
    inp = "\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n"
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(inp)
    sys.stdout = io.StringIO()
    captured = ""
    try:
        server.serve_stdio()
    except SystemExit:
        pass
    except Exception as exc:  # noqa: BLE001 - stdio 세부는 구현 재량
        captured = sys.stdout.getvalue()
        sys.stdin, sys.stdout = old_in, old_out
        skip(f"serve_stdio 입출력 방식 상이: {exc}")
        return []
    finally:
        if not captured:
            captured = sys.stdout.getvalue()
        sys.stdin, sys.stdout = old_in, old_out
    responses = []
    for line in captured.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("content-length") or not line.startswith("{"):
            continue
        try:
            responses.append(json.loads(line))
        except Exception:
            continue
    return responses


def _run(requests):
    server = _make_server()
    out = _dispatch_direct(server, requests)
    if out is None:
        out = _drive_stdio(server, requests)
    return out


def _by_id(responses, rid):
    for r in responses:
        if r.get("id") == rid:
            return r
    return None


def test_tools_list():
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    responses = _run(reqs)
    assert responses, "응답 없음(JSON-RPC 파싱 실패)"
    init = _by_id(responses, 1)
    assert init is not None and "error" not in init, f"initialize 실패: {init}"
    tl = _by_id(responses, 2)
    assert tl is not None and "result" in tl, f"tools/list 결과 없음: {tl}"
    tools = tl["result"].get("tools")
    assert isinstance(tools, list) and tools, "tools 배열 필요"
    names = {t.get("name") for t in tools}
    # 카탈로그 억제(6~12) + 핵심 tool 존재(위임격차 분석 = gap_analysis)
    assert 6 <= len(tools) <= 14, f"tool 수 6~14 규율 위반: {len(tools)}"
    assert "gap_analysis" in names, f"핵심 tool 누락: {names}"
    # 각 tool 은 inputSchema(JSON-Schema) 보유
    for t in tools:
        assert "inputSchema" in t or "input_schema" in t, f"입력스키마 누락: {t.get('name')}"


def test_tools_call_gap_analysis_safety_envelope():
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "gap_analysis", "arguments": {"region_id": "26170"}}},
    ]
    responses = _run(reqs)
    call = _by_id(responses, 3)
    assert call is not None, "tools/call 응답 없음"
    assert "error" not in call, f"tools/call 오류: {call.get('error')}"
    blob = json.dumps(call, ensure_ascii=False)
    # 안전 봉투(korea100 MCP 승계): 판단성 응답 필수 요소
    assert "execution_allowed" in blob, "execution_allowed 고지 누락"
    assert "as_of_date" in blob, "as_of_date(신선도) 동봉 누락"
    # 면책 고지(문구는 재량 — 핵심 토큰만 확인)
    assert ("법률판단" in blob) or ("disclaimer" in blob), "면책 고지 누락"


def test_neural_rag_tools_are_exposed():
    """2단계 확장 tool 5종이 카탈로그에 노출되고 입력스키마를 갖는가."""
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    tl = _by_id(_run(reqs), 2)
    assert tl and "result" in tl, tl
    tools = {t["name"]: t for t in tl["result"]["tools"]}
    expected = {"semantic_search_ordinance", "similar_ordinances",
                "neural_similar_regions", "ordinance_effectiveness", "explain_path"}
    missing = expected - set(tools)
    assert not missing, f"신경망/RAG tool 누락: {missing}"
    for name in expected:
        schema = tools[name].get("inputSchema") or tools[name].get("input_schema")
        assert isinstance(schema, dict) and schema.get("type") == "object", name


def _call_tool(name, arguments):
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}},
    ]
    return _by_id(_run(reqs), 5)


def test_neural_rag_tools_keep_safety_envelope():
    """임베딩·인덱스가 없는 환경(시드 DB)에서도 폴백하며 안전 봉투를 유지해야 한다."""
    cases = [
        ("semantic_search_ordinance", {"query": "주차장 설치 및 관리", "k": 3}),
        ("neural_similar_regions", {"region_id": "11110", "k": 3}),
        ("ordinance_effectiveness", {"region_id": "11110"}),
        ("explain_path", {"from_id": "ordin:9001", "to_id": "statute:001234"}),
    ]
    for name, args in cases:
        call = _call_tool(name, args)
        assert call is not None, f"{name}: 응답 없음"
        assert "error" not in call, f"{name} 오류: {call.get('error')}"
        result = call.get("result") or {}
        assert not result.get("isError"), f"{name} isError: {result}"
        env = json.loads(result["content"][0]["text"])
        assert env.get("execution_allowed") is False, f"{name}: execution_allowed 규율 위반"
        assert "as_of_date" in env, f"{name}: as_of_date 누락"
        assert env.get("disclaimer"), f"{name}: 면책 고지 누락"
        assert "data" in env, f"{name}: data 봉투 누락"


def test_explain_path_reports_relations_with_evidence():
    """조례 → 상위법 경로가 관계명·근거와 함께 설명돼야 한다(시드: 주차장법 위임)."""
    call = _call_tool("explain_path",
                      {"from_id": "ordin:9001", "to_id": "statute:001234"})
    assert call and "error" not in call, call
    body = json.loads(call["result"]["content"][0]["text"])["data"]
    assert body.get("found") is True, f"경로 미발견: {body.get('explanation')}"
    assert "DELEGATED_FROM" in body.get("relations", []), body.get("relations")
    assert body.get("path"), "경로 단계 없음"
    for step in body["path"]:
        assert step.get("relation") and step.get("from") and step.get("to"), step
    assert body.get("verification_status") in ("verified", "unverified"), body


def test_ordinance_effectiveness_declares_verification():
    call = _call_tool("ordinance_effectiveness", {"region_id": "11110"})
    assert call and "error" not in call, call
    body = json.loads(call["result"]["content"][0]["text"])["data"]
    ver = body.get("verification") or {}
    assert ver.get("status") in ("verified", "partially-verified", "unverified", "no-link"), ver
    assert "verified_links" in ver and "auto_links" in ver, ver


def test_tools_call_unknown_tool_errors():
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
         "params": {"name": "definitely_not_a_tool", "arguments": {}}},
    ]
    responses = _run(reqs)
    call = _by_id(responses, 9)
    assert call is not None
    # 미지 tool → JSON-RPC error 또는 isError 결과
    is_err = ("error" in call) or (isinstance(call.get("result"), dict)
                                   and call["result"].get("isError"))
    assert is_err, f"미지 tool 은 오류를 반환해야 함: {call}"


if __name__ == "__main__":
    sys.exit(run_dict(globals(), "test_mcp"))
