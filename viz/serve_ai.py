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
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemini-2.5-flash"
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
        "output_format": "plain Korean text, 3 to 6 short paragraphs, no markdown table",
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
            "maxOutputTokens": 1200,
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
        return "\n".join(parts).strip()
    return "AI 응답을 받았지만 텍스트 본문을 찾지 못했습니다."


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8742)
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
