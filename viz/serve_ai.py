"""Static demo server with the policy-map agent endpoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viz.ai_core import handle_chat, load_env_files  # noqa: E402


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/ai/chat":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(200, handle_chat(payload))
        except Exception as exc:  # noqa: BLE001 - demo server returns structured errors
            self.send_json(200, {"error": type(exc).__name__, "detail": str(exc)})

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    parser.add_argument("--host", default=os.environ.get("HOST", default_host))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8742")))
    args = parser.parse_args()
    load_env_files()
    os.chdir(ROOT)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving {ROOT} at http://{args.host}:{args.port}/viz/public/index.html")
    print("Agent endpoint: POST /ai/chat (GEMINI_API_KEY enables Gemini answers)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
