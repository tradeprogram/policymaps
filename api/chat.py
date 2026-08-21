"""Vercel 서버리스 함수 — /ai/chat (Gemini 프록시).

프론트는 /ai/chat 으로 POST 하고, vercel.json 의 rewrite 가 이 함수(/api/chat)로 보낸다.
브라우저에 API 키를 노출하지 않는다. 키는 Vercel 환경변수 GEMINI_API_KEY 에서 읽는다.

로컬 개발은 기존 viz/serve_ai.py 를 그대로 쓰면 된다(같은 로직). 이 파일은 그 배포판 shim.
"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_MODEL = "gemini-3.6-flash"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

SYSTEM_PROMPT = """너는 'AI 정책분석관'이다. 자치법규 정책지도 사이트 안에서 지자체 실무자의 의사결정을 돕는다.

반드시 지킬 규칙:
- 한국어로 답한다.
- 조례-예산 연결은 verified=1만 '확인됨'이라고 말하고, 나머지는 반드시 '추정 연결'이라고 말한다.
- 폐지 조례는 선례로 추천하지 않는다. 폐지 사례는 위험 신호로만 설명한다.
- 수치 판단에는 as_of_date 기준을 함께 언급한다.
- 데이터에 없는 내용은 추정하지 말고 '현재 화면 데이터로는 확인 불가'라고 말한다.
- 답변은 짧고 실행 중심으로 쓴다. 가능한 다음 화면 행동을 제안한다.
"""


def call_gemini(payload: dict) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 환경변수가 없습니다.")
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    message = str(payload.get("message", ""))[:3000]
    prompt = {
        "instruction": SYSTEM_PROMPT,
        "user_question": message,
        "current_route": payload.get("route"),
        "site_context": payload.get("context") or {},
        "recent_history": (payload.get("history") or [])[-8:],
        "output_format": "핵심만 담은 짧은 한국어. 3~4문장(또는 2~3개의 짧은 문단) 이내. 장황한 배경설명 없이 실행 중심으로. 순수 텍스트로만 쓰고 마크다운 기호(**, *, #, |, `)는 절대 쓰지 말 것.",
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
    return f"AI 응답 본문이 비어 있습니다 (finishReason={reason})."


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, call_gemini(payload))
        except HTTPError as e:
            self._send(200, {"error": f"Gemini HTTP {e.code}", "detail": e.read().decode("utf-8", "replace")[:800]})
        except (URLError, TimeoutError) as e:
            self._send(200, {"error": "Gemini request failed", "detail": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send(200, {"error": type(e).__name__, "detail": str(e)})

    def _send(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
