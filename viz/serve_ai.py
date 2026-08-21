"""Static demo server with a Gemini proxy.

Run from the repository root:

    echo GEMINI_API_KEY=... > .env
    python viz/serve_ai.py --port 8742

The browser never receives the API key. Static files are served from the
repository root so existing URLs such as /viz/public/index.html keep working.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
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


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/ai/chat":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            answer = call_gemini(payload)
            self.send_json(200, answer)
        except HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            self.send_json(200, {"error": f"Gemini HTTP {e.code}", "detail": body[:800]})
        except (URLError, TimeoutError) as e:
            self.send_json(200, {"error": "Gemini request failed", "detail": str(e)})
        except Exception as e:  # noqa: BLE001 - demo server must surface failures as JSON
            self.send_json(200, {"error": type(e).__name__, "detail": str(e)})

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def call_gemini(payload: dict) -> dict:
    load_env_files()
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 환경변수가 없습니다.")

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    message = str(payload.get("message", ""))[:3000]
    context = payload.get("context") or {}
    history = payload.get("history") or []

    prompt = {
        "instruction": SYSTEM_PROMPT,
        "user_question": message,
        "current_route": payload.get("route"),
        "site_context": context,
        "recent_history": history[-8:],
        "output_format": "핵심만 담은 짧은 한국어. 3~4문장(또는 2~3개의 짧은 문단) 이내. 장황한 배경설명 없이 실행 중심으로. 순수 텍스트로만 쓰고 마크다운 기호(**, *, #, |, `)는 절대 쓰지 말 것.",
    }

    req_body = {
        "systemInstruction": {
            "parts": [{"text": SYSTEM_PROMPT}],
        },
        "contents": [{
            "role": "user",
            "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}],
        }],
        "generationConfig": {
            "temperature": 0.7,
            # thinking 모델은 출력 토큰을 내부 추론에 먼저 쓰므로 넉넉히 준다
            # (작으면 finishReason=MAX_TOKENS 로 본문이 빈 채 돌아온다).
            "maxOutputTokens": 4096,
        },
    }
    req = Request(
        f"{GEMINI_BASE}/models/{model}:generateContent",
        data=json.dumps(req_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-goog-api-key": api_key,
        },
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


def extract_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()
    candidates = data.get("candidates") or []
    parts = []
    for cand in candidates:
        for part in (cand.get("content") or {}).get("parts") or []:
            if isinstance(part.get("text"), str):
                parts.append(part["text"])
    if parts:
        text = "\n".join(parts).strip()
        # 방어적 마크다운 제거(프론트가 순수 텍스트로 타이핑하므로 기호가 그대로 보이면 안 된다)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"(?m)^\s*[*#-]\s+", "", text)
        return text.replace("**", "").strip()
    reason = (candidates[0].get("finishReason") if candidates else None) or "UNKNOWN"
    block = (data.get("promptFeedback") or {}).get("blockReason")
    hint = f" (finishReason={reason}{', blockReason=' + block if block else ''})"
    return "AI 응답 본문이 비어 있습니다" + hint + ". thinkingBudget/maxOutputTokens 또는 모델명을 확인하세요."


def main() -> None:
    parser = argparse.ArgumentParser()
    # 클라우드(Render 등)는 PORT 를 환경변수로 준다. 있으면 0.0.0.0 에 바인딩하고,
    # 로컬(PORT 없음)은 기존대로 127.0.0.1:8742 를 쓴다.
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    parser.add_argument("--host", default=os.environ.get("HOST", default_host))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8742")))
    args = parser.parse_args()
    load_env_files()
    os.chdir(ROOT)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving {ROOT} at http://{args.host}:{args.port}/viz/public/index.html")
    print("AI proxy: POST /ai/chat (GEMINI_API_KEY required for Gemini responses)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
